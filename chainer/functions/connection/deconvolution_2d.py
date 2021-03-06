import numpy
import six

import chainer
from chainer.backends import cuda
from chainer import configuration
from chainer import function_node
import chainer.functions
from chainer.functions.connection import convolution_2d
from chainer.utils import argument
from chainer.utils import conv
from chainer.utils import type_check

if cuda.cudnn_enabled:
    cudnn = cuda.cudnn
    libcudnn = cuda.cuda.cudnn
    _cudnn_version_ = libcudnn.getVersion()
    _fwd_pref = libcudnn.CUDNN_CONVOLUTION_FWD_SPECIFY_WORKSPACE_LIMIT
    _bwd_filter_pref = \
        libcudnn.CUDNN_CONVOLUTION_BWD_FILTER_SPECIFY_WORKSPACE_LIMIT
    _bwd_data_pref = \
        libcudnn.CUDNN_CONVOLUTION_BWD_DATA_SPECIFY_WORKSPACE_LIMIT
    _algorithm = {}


def get_algorithm(W, dy, dx, conv_param, handle, filter_desc, dy_desc,
                  conv_desc, dx_desc, workspace):
    key = (dx.shape, W.shape, dy.shape, conv_param)
    if key in _algorithm:
        return _algorithm[key]
    ret = libcudnn.findConvolutionBackwardDataAlgorithmEx(
        handle, filter_desc.value, W.data.ptr, dy_desc.value, dy.data.ptr,
        conv_desc.value, dx_desc.value, dx.data.ptr, 1, workspace.data.ptr,
        workspace.size)
    algo = ret[0]['algo']
    _algorithm[key] = algo
    return algo


def _pair(x):
    if hasattr(x, '__getitem__'):
        return x
    return x, x


class Deconvolution2DFunction(function_node.FunctionNode):

    cover_all = None

    def __init__(self, stride=1, pad=0, outsize=None, group=1, **kwargs):
        argument.check_unexpected_kwargs(
            kwargs,
            deterministic="deterministic argument is not supported anymore. "
            "Use chainer.using_config('cudnn_deterministic', value) context "
            "where value is either `True` or `False`.",
            requires_x_grad="requires_x_grad argument is not supported "
            "anymore. Just remove the argument. Note that whether to compute "
            "the gradient w.r.t. x is automatically decided during "
            "backpropagation."
        )
        dilate, = argument.parse_kwargs(kwargs, ('dilate', 1))

        self.sy, self.sx = _pair(stride)
        self.ph, self.pw = _pair(pad)
        self.outh, self.outw = (None, None) if outsize is None else outsize
        self.dy, self.dx = _pair(dilate)
        self.group = group

    def check_type_forward(self, in_types):
        n_in = in_types.size()
        type_check.expect(2 <= n_in, n_in <= 3)
        x_type, w_type = in_types[:2]

        type_check.expect(
            x_type.dtype.kind == 'f',
            w_type.dtype.kind == 'f',
            x_type.ndim == 4,
            w_type.ndim == 4,
            x_type.shape[1] == w_type.shape[0]
        )

        if self.outh is not None:
            lower_bound = conv.get_conv_outsize(
                self.outh, w_type.shape[2], self.sy, self.ph,
                d=self.dy)
            upper_bound = conv.get_conv_outsize(
                self.outh, w_type.shape[2], self.sy, self.ph, cover_all=True,
                d=self.dy)
            type_check.expect(
                lower_bound <= x_type.shape[2],
                x_type.shape[2] <= upper_bound)
        if self.outw is not None:
            lower_bound = conv.get_conv_outsize(
                self.outw, w_type.shape[3], self.sx, self.pw,
                d=self.dx)
            upper_bound = conv.get_conv_outsize(
                self.outw, w_type.shape[3], self.sx, self.pw, cover_all=True,
                d=self.dx)
            type_check.expect(
                lower_bound <= x_type.shape[3],
                x_type.shape[3] <= upper_bound)

        if type_check.eval(n_in) == 3:
            b_type = in_types[2]
            type_check.expect(
                b_type.dtype == x_type.dtype,
                b_type.ndim == 1,
                # Need to consider the case that group count > 1.
                # b_type.shape[0] == w_type.shape[1],
            )

    def _calc_out_size(self, x, W):
        """Calculates and stores `outh` and `outw`."""
        kh, kw = W.shape[2:]
        _, _, in_h, in_w = x.shape
        # - k, m, n: shape of out_channel
        # - b: number of inputs
        # - h, w: height and width of kernels
        # k, m, n, b, h, w -> b, k, m, n, h, w
        if self.outh is None:
            self.outh = conv.get_deconv_outsize(
                in_h, kh, self.sy, self.ph, d=self.dy)
            if self.outh <= 0:
                raise RuntimeError('Height in the output must be positive.')

        if self.outw is None:
            self.outw = conv.get_deconv_outsize(
                in_w, kw, self.sx, self.pw, d=self.dx)
            if self.outw <= 0:
                raise RuntimeError('Width in the output must be positive.')

    def forward_cpu(self, inputs):
        self.retain_inputs((0, 1))  # only retain x and W
        if len(inputs) == 2:
            (x, W), b = inputs, None
        else:
            x, W, b = inputs

        self._calc_out_size(x, W)

        if self.group > 1:
            y = self._forward_grouped_convolution(x, W, b)
        else:
            y = self._forward_cpu_core(x, W, b)
        return y,

    def _forward_cpu_core(self, x, W, b):
        gcol = numpy.tensordot(W, x, (0, 1)).astype(x.dtype, copy=False)
        gcol = numpy.rollaxis(gcol, 3)
        y = conv.col2im_cpu(
            gcol, self.sy, self.sx, self.ph, self.pw, self.outh, self.outw,
            dy=self.dy, dx=self.dx)
        # b, k, h, w
        if b is not None:
            y += b.reshape(1, b.size, 1, 1)
        return y

    def forward_gpu(self, inputs):
        self.retain_inputs((0, 1))  # only retain x and W
        if len(inputs) == 2:
            (x, W), b = inputs, None
        else:
            x, W, b = inputs

        self._calc_out_size(x, W)
        self._set_cover_all(x, W)

        use_cudnn = (
            chainer.should_use_cudnn('>=auto')
            and not self.cover_all
            and x.dtype == W.dtype
            and ((self.dy == 1 and self.dx == 1)
                 or (_cudnn_version_ >= 6000
                     and not configuration.config.cudnn_deterministic))
            and (self.group <= 1 or _cudnn_version_ >= 7000)
        )

        if use_cudnn:
            # cuDNN implementation
            return self._forward_cudnn(x, W, b)

        else:
            if self.group > 1:
                y = self._forward_grouped_convolution(x, W, b)
            else:
                y = self._forward_gpu_core(x, W, b)
            return y,

    def _forward_gpu_core(self, x, W, b):
        # Implementation using col2im
        gcol = cuda.cupy.tensordot(W, x, (0, 1)).astype(x.dtype,
                                                        copy=False)
        # - k, m, n: shape of out_channel
        # - b: number of inputs
        # - h, w: height and width of kernels
        # k, m, n, b, h, w -> b, k, m, n, h, w
        gcol = cuda.cupy.rollaxis(gcol, 3)
        y = conv.col2im_gpu(
            gcol, self.sy, self.sx, self.ph, self.pw, self.outh, self.outw,
            dy=self.dy, dx=self.dx)
        if b is not None:
            y += b.reshape(1, b.size, 1, 1)
        return y

    def _forward_grouped_convolution(self, x, W, b):
        # G: group count
        # N: batch size
        # kH, kW: kernel height, kernel width
        # xC, xH, xW: x channels, x height, x width
        # yC, yH, yW: y channels, y height, y width
        G = self.group
        N, xC, xH, xW = x.shape
        xCg = int(xC / G)
        _, yCg, kH, kW = W.shape

        xp = cuda.get_array_module(x)

        _x = x.reshape(N, G, xCg, xH, xW)
        _x = xp.rollaxis(_x, 1)  # (G, N, xCg, xH, xW)
        _W = W.reshape(G, xCg, yCg, kH, kW)
        if b is not None:
            _b = b.reshape(G, yCg)

        _ys = []
        for g in six.moves.range(G):
            _bg = None if b is None else _b[g, ]
            if xp is numpy:
                _y = self._forward_cpu_core(_x[g, ], _W[g, ], _bg)
            else:
                _y = self._forward_gpu_core(_x[g, ], _W[g, ], _bg)
            _ys.append(_y)

        y = xp.concatenate(_ys, axis=1)  # (N, yC, yH, yW)
        return y

    def _forward_cudnn(self, x, W, b):
        x = cuda.cupy.ascontiguousarray(x)
        W = cuda.cupy.ascontiguousarray(W)
        if b is not None:
            b = cuda.cupy.ascontiguousarray(b)

        n = x.shape[0]
        # out_c = W.shape[1]
        yCg = W.shape[1]
        yC = yCg * self.group

        use_tensor_core = chainer.should_use_cudnn_tensor_core(x.dtype)

        # cuDNN 7 supports dilation only in *_BWD_DATA_ALGO_0, but
        # it supports Tensor Cores only in *_BWD_DATA_ALGO_1.
        if (use_tensor_core and (self.dx > 1 or self.dy > 1)):
            use_tensor_core = False

        handle = cudnn.get_handle()
        x_desc = cudnn.create_tensor_descriptor(x)
        y = cuda.cupy.empty((n, yC, self.outh, self.outw),
                            dtype=x.dtype)
        y_desc = cudnn.create_tensor_descriptor(y)

        filter_desc = cudnn.create_filter_descriptor(W)
        conv_param = (self.ph, self.pw), (self.sy, self.sx), x.dtype
        dilation = (self.dy, self.dx)
        conv_desc = cudnn.create_convolution_descriptor(
            *conv_param, dilation=dilation,
            use_tensor_core=use_tensor_core,
            group=self.group)
        if b is not None:
            bias_desc = cudnn.create_tensor_descriptor(
                b[None, :, None, None])

        oz_dtype = 'd' if x.dtype == 'd' else 'f'
        one = numpy.array(1, dtype=oz_dtype).ctypes
        zero = numpy.array(0, dtype=oz_dtype).ctypes

        workspace_size = cuda.get_max_workspace_size()
        workspace = cuda.cupy.empty((workspace_size,), dtype='b')

        if configuration.config.cudnn_deterministic:
            algo = libcudnn.CUDNN_CONVOLUTION_BWD_DATA_ALGO_1
        elif configuration.config.autotune and _cudnn_version_ >= 5000:
            algo = get_algorithm(
                W, x, y, conv_param + (dilation,), handle, filter_desc,
                x_desc, conv_desc, y_desc, workspace)
        else:
            algo = libcudnn.getConvolutionBackwardDataAlgorithm(
                handle, filter_desc.value, x_desc.value, conv_desc.value,
                y_desc.value, _bwd_data_pref, workspace_size)

        if use_tensor_core:
            algo = self._tensor_core_adjust_algo()

        libcudnn.convolutionBackwardData_v3(
            handle, one.data, filter_desc.value, W.data.ptr,
            x_desc.value, x.data.ptr, conv_desc.value,
            algo, workspace.data.ptr, workspace_size,
            zero.data, y_desc.value, y.data.ptr)

        if b is not None:
            cudnn.add_tensor(
                handle, one.data, bias_desc.value, b.data.ptr,
                one.data, y_desc.value, y.data.ptr)

        return y,

    def _tensor_core_adjust_algo(self):
        # Only CUDNN_CONVOLUTION_BWD_DATA_ALGO_1 supports
        # Tensor-Core in cuDNN7
        return libcudnn.CUDNN_CONVOLUTION_BWD_DATA_ALGO_1

    def backward(self, indexes, grad_outputs):
        x, W = self.get_retained_inputs()
        gy, = grad_outputs

        ret = []
        if 0 in indexes:
            if self.cover_all is None:
                self._set_cover_all(x, W)
            gx = chainer.functions.convolution_2d(
                gy, W, stride=(self.sy, self.sx), pad=(self.ph, self.pw),
                cover_all=self.cover_all, dilate=(self.dy, self.dx),
                group=self.group)
            ret.append(gx)
        if 1 in indexes:
            if self.cover_all is None:
                self._set_cover_all(x, W)
            gW, = convolution_2d.Convolution2DGradW(self).apply((gy, x))
            ret.append(gW)
        if 2 in indexes:
            gb = chainer.functions.sum(gy, axis=(0, 2, 3))
            ret.append(gb)

        return ret

    def _set_cover_all(self, x, W):
        in_h, in_w = x.shape[2:]
        kh, kw = W.shape[2:]
        self.cover_all = (
            in_h != conv.get_conv_outsize(self.outh, kh, self.sy,
                                          self.ph, d=self.dy) or
            in_w != conv.get_conv_outsize(self.outw, kw, self.sx,
                                          self.pw, d=self.dx))


def deconvolution_2d(x, W, b=None, stride=1, pad=0, outsize=None, group=1,
                     **kwargs):
    """deconvolution_2d(x, W, b=None, stride=1, pad=0, outsize=None)

    Two dimensional deconvolution function.

    This is an implementation of two-dimensional deconvolution. In most of deep
    learning frameworks and papers, this function is called
    **transposed convolution**. But because of historical reasons (e.g. paper
    by Ziller `Deconvolutional Networks`_) and backward compatibility, this
    function is called **deconvolution** in Chainer.

    .. _Deconvolutional Networks: \
http://www.matthewzeiler.com/pubs/cvpr2010/cvpr2010.pdf

    It takes three variables: input image ``x``,
    the filter weight ``W``, and the bias vector ``b``.

    Notation: here is a notation for dimensionalities.

    - :math:`n` is the batch size.
    - :math:`c_I` and :math:`c_O` are the number of the input and output
      channels, respectively.
    - :math:`h_I` and :math:`w_I` are the height and width of the input image,
      respectively.
    - :math:`h_K` and :math:`w_K` are the height and width of the filters,
      respectively.
    - :math:`h_P` and :math:`w_P` are the height and width of the spatial
      padding size, respectively.

    Let :math:`(s_Y, s_X)` be the stride of filter application. Then, the
    output size :math:`(h_O, w_O)` is estimated by the following equations:

    .. math::

       h_O &= s_Y (h_I - 1) + h_K - 2h_P,\\\\
       w_O &= s_X (w_I - 1) + w_K - 2w_P.

    The output of this function can be non-deterministic when it uses cuDNN.
    If ``chainer.configuration.config.deterministic`` is ``True`` and
    cuDNN version is >= v3, it forces cuDNN to use a deterministic algorithm.

    Deconvolution links can use a feature of cuDNN called autotuning, which
    selects the most efficient CNN algorithm for images of fixed-size,
    can provide a significant performance boost for fixed neural nets.
    To enable, set `chainer.using_config('autotune', True)`

    .. warning::

        ``deterministic`` argument is not supported anymore since v2.
        Instead, use ``chainer.using_config('cudnn_deterministic', value)``
        (value is either ``True`` or ``False``).
        See :func:`chainer.using_config`.

    Args:
        x (:class:`~chainer.Variable` or :class:`numpy.ndarray` or \
        :class:`cupy.ndarray`):
            Input variable of shape :math:`(n, c_I, h_I, w_I)`.
        W (:class:`~chainer.Variable` or :class:`numpy.ndarray` or \
        :class:`cupy.ndarray`):
            Weight variable of shape :math:`(c_I, c_O, h_K, w_K)`.
        b (:class:`~chainer.Variable` or :class:`numpy.ndarray` or \
        :class:`cupy.ndarray`): Bias variable of length :math:`c_O` (optional).
        stride (:class:`int` or pair of :class:`int` s):
            Stride of filter applications. ``stride=s`` and ``stride=(s, s)``
            are equivalent.
        pad (:class:`int` or pair of :class:`int` s):
            Spatial padding width for input arrays.
            ``pad=p`` and ``pad=(p, p)`` are equivalent.
        outsize (:class:`tuple` of :class:`int`):
            Expected output size of deconvolutional operation.
            It should be pair of height and width :math:`(h_O, w_O)`.
            Default value is ``None`` and the outsize is estimated by
            input size, stride and pad.

    Returns:
        ~chainer.Variable:
            Output variable of shape :math:`(n, c_O, h_O, w_O)`.

    .. admonition:: Example

        >>> n = 10
        >>> c_i, c_o = 1, 3
        >>> h_i, w_i = 5, 10
        >>> h_k, w_k = 10, 10
        >>> h_p, w_p = 5, 5
        >>> x = np.random.uniform(0, 1, (n, c_i, h_i, w_i)).astype('f')
        >>> x.shape
        (10, 1, 5, 10)
        >>> W = np.random.uniform(0, 1, (c_i, c_o, h_k, w_k)).astype('f')
        >>> W.shape
        (1, 3, 10, 10)
        >>> b = np.random.uniform(0, 1, c_o).astype('f')
        >>> b.shape
        (3,)
        >>> s_y, s_x = 5, 5
        >>> y = F.deconvolution_2d(x, W, b, stride=(s_y, s_x), pad=(h_p, w_p))
        >>> y.shape
        (10, 3, 20, 45)
        >>> h_o = s_y * (h_i - 1) + h_k - 2 * h_p
        >>> w_o = s_x * (w_i - 1) + w_k - 2 * w_p
        >>> y.shape == (n, c_o, h_o, w_o)
        True


    """
    argument.check_unexpected_kwargs(
        kwargs, deterministic="deterministic argument is not "
        "supported anymore. "
        "Use chainer.using_config('cudnn_deterministic', value) "
        "context where value is either `True` or `False`.")
    dilate, = argument.parse_kwargs(kwargs, ('dilate', 1))

    func = Deconvolution2DFunction(stride, pad, outsize, dilate=dilate,
                                   group=group)
    if b is None:
        args = x, W
    else:
        args = x, W, b
    y, = func.apply(args)
    return y
