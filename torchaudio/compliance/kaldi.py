from typing import Tuple

import math
import torch
from torch import Tensor
from torch.nn import functional as F

import torchaudio
import torchaudio._internal.fft

__all__ = [
    'get_mel_banks',
    'inverse_mel_scale',
    'inverse_mel_scale_scalar',
    'mel_scale',
    'mel_scale_scalar',
    'spectrogram',
    'fbank',
    'mfcc',
    'vtln_warp_freq',
    'vtln_warp_mel_freq',
    'resample_waveform',
]

# numeric_limits<float>::epsilon() 1.1920928955078125e-07
EPSILON = torch.tensor(torch.finfo(torch.float).eps)
# 1 milliseconds = 0.001 seconds
MILLISECONDS_TO_SECONDS = 0.001

# window types
HAMMING = 'hamming'
HANNING = 'hanning'
POVEY = 'povey'
RECTANGULAR = 'rectangular'
BLACKMAN = 'blackman'
WINDOWS = [HAMMING, HANNING, POVEY, RECTANGULAR, BLACKMAN]


def _get_epsilon(device, dtype):
    return EPSILON.to(device=device, dtype=dtype)


def _next_power_of_2(x: int) -> int:
    r"""Returns the smallest power of 2 that is greater than x
    """
    return 1 if x == 0 else 2 ** (x - 1).bit_length()


def _get_strided(waveform: Tensor, window_size: int, window_shift: int, snip_edges: bool) -> Tensor:
    r"""Given a waveform (1D tensor of size ``num_samples``), it returns a 2D tensor (m, ``window_size``)
    representing how the window is shifted along the waveform. Each row is a frame.

    Args:
        waveform (Tensor): Tensor of size ``num_samples``
        window_size (int): Frame length
        window_shift (int): Frame shift
        snip_edges (bool): If True, end effects will be handled by outputting only frames that completely fit
            in the file, and the number of frames depends on the frame_length.  If False, the number of frames
            depends only on the frame_shift, and we reflect the data at the ends.

    Returns:
        Tensor: 2D tensor of size (m, ``window_size``) where each row is a frame
    """
    assert waveform.dim() == 1
    num_samples = waveform.size(0)
    strides = (window_shift * waveform.stride(0), waveform.stride(0))

    if snip_edges:
        if num_samples < window_size:
            return torch.empty((0, 0), dtype=waveform.dtype, device=waveform.device)
        else:
            m = 1 + (num_samples - window_size) // window_shift
    else:
        reversed_waveform = torch.flip(waveform, [0])
        m = (num_samples + (window_shift // 2)) // window_shift
        pad = window_size // 2 - window_shift // 2
        pad_right = reversed_waveform
        if pad > 0:
            # torch.nn.functional.pad returns [2,1,0,1,2] for 'reflect'
            # but we want [2, 1, 0, 0, 1, 2]
            pad_left = reversed_waveform[-pad:]
            waveform = torch.cat((pad_left, waveform, pad_right), dim=0)
        else:
            # pad is negative so we want to trim the waveform at the front
            waveform = torch.cat((waveform[-pad:], pad_right), dim=0)

    sizes = (m, window_size)
    return waveform.as_strided(sizes, strides)


def _feature_window_function(window_type: str,
                             window_size: int,
                             blackman_coeff: float,
                             device: torch.device,
                             dtype: int,
                             ) -> Tensor:
    r"""Returns a window function with the given type and size
    """
    if window_type == HANNING:
        return torch.hann_window(window_size, periodic=False, device=device, dtype=dtype)
    elif window_type == HAMMING:
        return torch.hamming_window(window_size, periodic=False, alpha=0.54, beta=0.46, device=device, dtype=dtype)
    elif window_type == POVEY:
        # like hanning but goes to zero at edges
        return torch.hann_window(window_size, periodic=False, device=device, dtype=dtype).pow(0.85)
    elif window_type == RECTANGULAR:
        return torch.ones(window_size, device=device, dtype=dtype)
    elif window_type == BLACKMAN:
        a = 2 * math.pi / (window_size - 1)
        window_function = torch.arange(window_size, device=device, dtype=dtype)
        # can't use torch.blackman_window as they use different coefficients
        return (blackman_coeff - 0.5 * torch.cos(a * window_function) +
                (0.5 - blackman_coeff) * torch.cos(2 * a * window_function)).to(device=device, dtype=dtype)
    else:
        raise Exception('Invalid window type ' + window_type)


def _get_log_energy(strided_input: Tensor,
                    epsilon: Tensor,
                    energy_floor: float) -> Tensor:
    r"""Returns the log energy of size (m) for a strided_input (m,*)
    """
    device, dtype = strided_input.device, strided_input.dtype
    log_energy = torch.max(strided_input.pow(2).sum(1), epsilon).log()  # size (m)
    if energy_floor == 0.0:
        return log_energy
    return torch.max(
        log_energy, torch.tensor(math.log(energy_floor), device=device, dtype=dtype))


def _get_waveform_and_window_properties(waveform: Tensor,
                                        channel: int,
                                        sample_frequency: float,
                                        frame_shift: float,
                                        frame_length: float,
                                        round_to_power_of_two: bool,
                                        preemphasis_coefficient: float) -> Tuple[Tensor, int, int, int]:
    r"""Gets the waveform and window properties
    """
    channel = max(channel, 0)
    assert channel < waveform.size(0), ('Invalid channel {} for size {}'.format(channel, waveform.size(0)))
    waveform = waveform[channel, :]  # size (n)
    window_shift = int(sample_frequency * frame_shift * MILLISECONDS_TO_SECONDS)
    window_size = int(sample_frequency * frame_length * MILLISECONDS_TO_SECONDS)
    padded_window_size = _next_power_of_2(window_size) if round_to_power_of_two else window_size

    assert 2 <= window_size <= len(
        waveform), ('choose a window size {} that is [2, {}]'
                    .format(window_size, len(waveform)))
    assert 0 < window_shift, '`window_shift` must be greater than 0'
    assert padded_window_size % 2 == 0, 'the padded `window_size` must be divisible by two.' \
                                        ' use `round_to_power_of_two` or change `frame_length`'
    assert 0. <= preemphasis_coefficient <= 1.0, '`preemphasis_coefficient` must be between [0,1]'
    assert sample_frequency > 0, '`sample_frequency` must be greater than zero'
    return waveform, window_shift, window_size, padded_window_size


def _get_window(waveform: Tensor,
                padded_window_size: int,
                window_size: int,
                window_shift: int,
                window_type: str,
                blackman_coeff: float,
                snip_edges: bool,
                raw_energy: bool,
                energy_floor: float,
                dither: float,
                remove_dc_offset: bool,
                preemphasis_coefficient: float) -> Tuple[Tensor, Tensor]:
    r"""Gets a window and its log energy

    Returns:
        (Tensor, Tensor): strided_input of size (m, ``padded_window_size``) and signal_log_energy of size (m)
    """
    device, dtype = waveform.device, waveform.dtype
    epsilon = _get_epsilon(device, dtype)

    # size (m, window_size)
    strided_input = _get_strided(waveform, window_size, window_shift, snip_edges)

    if dither != 0.0:
        # Returns a random number strictly between 0 and 1
        x = torch.max(epsilon, torch.rand(strided_input.shape, device=device, dtype=dtype))
        rand_gauss = torch.sqrt(-2 * x.log()) * torch.cos(2 * math.pi * x)
        strided_input = strided_input + rand_gauss * dither

    if remove_dc_offset:
        # Subtract each row/frame by its mean
        row_means = torch.mean(strided_input, dim=1).unsqueeze(1)  # size (m, 1)
        strided_input = strided_input - row_means

    if raw_energy:
        # Compute the log energy of each row/frame before applying preemphasis and
        # window function
        signal_log_energy = _get_log_energy(strided_input, epsilon, energy_floor)  # size (m)

    if preemphasis_coefficient != 0.0:
        # strided_input[i,j] -= preemphasis_coefficient * strided_input[i, max(0, j-1)] for all i,j
        offset_strided_input = torch.nn.functional.pad(
            strided_input.unsqueeze(0), (1, 0), mode='replicate').squeeze(0)  # size (m, window_size + 1)
        strided_input = strided_input - preemphasis_coefficient * offset_strided_input[:, :-1]

    # Apply window_function to each row/frame
    window_function = _feature_window_function(
        window_type, window_size, blackman_coeff, device, dtype).unsqueeze(0)  # size (1, window_size)
    strided_input = strided_input * window_function  # size (m, window_size)

    # Pad columns with zero until we reach size (m, padded_window_size)
    if padded_window_size != window_size:
        padding_right = padded_window_size - window_size
        strided_input = torch.nn.functional.pad(
            strided_input.unsqueeze(0), (0, padding_right), mode='constant', value=0).squeeze(0)

    # Compute energy after window function (not the raw one)
    if not raw_energy:
        signal_log_energy = _get_log_energy(strided_input, epsilon, energy_floor)  # size (m)

    return strided_input, signal_log_energy


def _subtract_column_mean(tensor: Tensor, subtract_mean: bool) -> Tensor:
    # subtracts the column mean of the tensor size (m, n) if subtract_mean=True
    # it returns size (m, n)
    if subtract_mean:
        col_means = torch.mean(tensor, dim=0).unsqueeze(0)
        tensor = tensor - col_means
    return tensor


def spectrogram(waveform: Tensor,
                blackman_coeff: float = 0.42,
                channel: int = -1,
                dither: float = 0.0,
                energy_floor: float = 1.0,
                frame_length: float = 25.0,
                frame_shift: float = 10.0,
                min_duration: float = 0.0,
                preemphasis_coefficient: float = 0.97,
                raw_energy: bool = True,
                remove_dc_offset: bool = True,
                round_to_power_of_two: bool = True,
                sample_frequency: float = 16000.0,
                snip_edges: bool = True,
                subtract_mean: bool = False,
                window_type: str = POVEY) -> Tensor:
    r"""Create a spectrogram from a raw audio signal. This matches the input/output of Kaldi's
    compute-spectrogram-feats.

    Args:
        waveform (Tensor): Tensor of audio of size (c, n) where c is in the range [0,2)
        blackman_coeff (float, optional): Constant coefficient for generalized Blackman window. (Default: ``0.42``)
        channel (int, optional): Channel to extract (-1 -> expect mono, 0 -> left, 1 -> right) (Default: ``-1``)
        dither (float, optional): Dithering constant (0.0 means no dither). If you turn this off, you should set
            the energy_floor option, e.g. to 1.0 or 0.1 (Default: ``0.0``)
        energy_floor (float, optional): Floor on energy (absolute, not relative) in Spectrogram computation.  Caution:
            this floor is applied to the zeroth component, representing the total signal energy.  The floor on the
            individual spectrogram elements is fixed at std::numeric_limits<float>::epsilon(). (Default: ``1.0``)
        frame_length (float, optional): Frame length in milliseconds (Default: ``25.0``)
        frame_shift (float, optional): Frame shift in milliseconds (Default: ``10.0``)
        min_duration (float, optional): Minimum duration of segments to process (in seconds). (Default: ``0.0``)
        preemphasis_coefficient (float, optional): Coefficient for use in signal preemphasis (Default: ``0.97``)
        raw_energy (bool, optional): If True, compute energy before preemphasis and windowing (Default: ``True``)
        remove_dc_offset (bool, optional): Subtract mean from waveform on each frame (Default: ``True``)
        round_to_power_of_two (bool, optional): If True, round window size to power of two by zero-padding input
            to FFT. (Default: ``True``)
        sample_frequency (float, optional): Waveform data sample frequency (must match the waveform file, if
            specified there) (Default: ``16000.0``)
        snip_edges (bool, optional): If True, end effects will be handled by outputting only frames that completely fit
            in the file, and the number of frames depends on the frame_length.  If False, the number of frames
            depends only on the frame_shift, and we reflect the data at the ends. (Default: ``True``)
        subtract_mean (bool, optional): Subtract mean of each feature file [CMS]; not recommended to do
            it this way.  (Default: ``False``)
        window_type (str, optional): Type of window ('hamming'|'hanning'|'povey'|'rectangular'|'blackman')
         (Default: ``'povey'``)

    Returns:
        Tensor: A spectrogram identical to what Kaldi would output. The shape is
        (m, ``padded_window_size // 2 + 1``) where m is calculated in _get_strided
    """
    device, dtype = waveform.device, waveform.dtype
    epsilon = _get_epsilon(device, dtype)

    waveform, window_shift, window_size, padded_window_size = _get_waveform_and_window_properties(
        waveform, channel, sample_frequency, frame_shift, frame_length, round_to_power_of_two, preemphasis_coefficient)

    if len(waveform) < min_duration * sample_frequency:
        # signal is too short
        return torch.empty(0)

    strided_input, signal_log_energy = _get_window(
        waveform, padded_window_size, window_size, window_shift, window_type, blackman_coeff,
        snip_edges, raw_energy, energy_floor, dither, remove_dc_offset, preemphasis_coefficient)

    # size (m, padded_window_size // 2 + 1, 2)
    fft = torchaudio._internal.fft.rfft(strided_input)

    # Convert the FFT into a power spectrum
    power_spectrum = torch.max(fft.abs().pow(2.), epsilon).log()  # size (m, padded_window_size // 2 + 1)
    power_spectrum[:, 0] = signal_log_energy

    power_spectrum = _subtract_column_mean(power_spectrum, subtract_mean)
    return power_spectrum


def inverse_mel_scale_scalar(mel_freq: float) -> float:
    return 700.0 * (math.exp(mel_freq / 1127.0) - 1.0)


def inverse_mel_scale(mel_freq: Tensor) -> Tensor:
    return 700.0 * ((mel_freq / 1127.0).exp() - 1.0)


def mel_scale_scalar(freq: float) -> float:
    return 1127.0 * math.log(1.0 + freq / 700.0)


def mel_scale(freq: Tensor) -> Tensor:
    return 1127.0 * (1.0 + freq / 700.0).log()


def vtln_warp_freq(vtln_low_cutoff: float,
                   vtln_high_cutoff: float,
                   low_freq: float,
                   high_freq: float,
                   vtln_warp_factor: float,
                   freq: Tensor) -> Tensor:
    r"""This computes a VTLN warping function that is not the same as HTK's one,
    but has similar inputs (this function has the advantage of never producing
    empty bins).

    This function computes a warp function F(freq), defined between low_freq
    and high_freq inclusive, with the following properties:
        F(low_freq) == low_freq
        F(high_freq) == high_freq
    The function is continuous and piecewise linear with two inflection
        points.
    The lower inflection point (measured in terms of the unwarped
        frequency) is at frequency l, determined as described below.
    The higher inflection point is at a frequency h, determined as
        described below.
    If l <= f <= h, then F(f) = f/vtln_warp_factor.
    If the higher inflection point (measured in terms of the unwarped
        frequency) is at h, then max(h, F(h)) == vtln_high_cutoff.
        Since (by the last point) F(h) == h/vtln_warp_factor, then
        max(h, h/vtln_warp_factor) == vtln_high_cutoff, so
        h = vtln_high_cutoff / max(1, 1/vtln_warp_factor).
          = vtln_high_cutoff * min(1, vtln_warp_factor).
    If the lower inflection point (measured in terms of the unwarped
        frequency) is at l, then min(l, F(l)) == vtln_low_cutoff
        This implies that l = vtln_low_cutoff / min(1, 1/vtln_warp_factor)
                            = vtln_low_cutoff * max(1, vtln_warp_factor)
    Args:
        vtln_low_cutoff (float): Lower frequency cutoffs for VTLN
        vtln_high_cutoff (float): Upper frequency cutoffs for VTLN
        low_freq (float): Lower frequency cutoffs in mel computation
        high_freq (float): Upper frequency cutoffs in mel computation
        vtln_warp_factor (float): Vtln warp factor
        freq (Tensor): given frequency in Hz

    Returns:
        Tensor: Freq after vtln warp
    """
    assert vtln_low_cutoff > low_freq, 'be sure to set the vtln_low option higher than low_freq'
    assert vtln_high_cutoff < high_freq, 'be sure to set the vtln_high option lower than high_freq [or negative]'
    l = vtln_low_cutoff * max(1.0, vtln_warp_factor)
    h = vtln_high_cutoff * min(1.0, vtln_warp_factor)
    scale = 1.0 / vtln_warp_factor
    Fl = scale * l  # F(l)
    Fh = scale * h  # F(h)
    assert l > low_freq and h < high_freq
    # slope of left part of the 3-piece linear function
    scale_left = (Fl - low_freq) / (l - low_freq)
    # [slope of center part is just "scale"]

    # slope of right part of the 3-piece linear function
    scale_right = (high_freq - Fh) / (high_freq - h)

    res = torch.empty_like(freq)

    outside_low_high_freq = torch.lt(freq, low_freq) | torch.gt(freq, high_freq)  # freq < low_freq || freq > high_freq
    before_l = torch.lt(freq, l)  # freq < l
    before_h = torch.lt(freq, h)  # freq < h
    after_h = torch.ge(freq, h)  # freq >= h

    # order of operations matter here (since there is overlapping frequency regions)
    res[after_h] = high_freq + scale_right * (freq[after_h] - high_freq)
    res[before_h] = scale * freq[before_h]
    res[before_l] = low_freq + scale_left * (freq[before_l] - low_freq)
    res[outside_low_high_freq] = freq[outside_low_high_freq]

    return res


def vtln_warp_mel_freq(vtln_low_cutoff: float,
                       vtln_high_cutoff: float,
                       low_freq, high_freq: float,
                       vtln_warp_factor: float,
                       mel_freq: Tensor) -> Tensor:
    r"""
    Args:
        vtln_low_cutoff (float): Lower frequency cutoffs for VTLN
        vtln_high_cutoff (float): Upper frequency cutoffs for VTLN
        low_freq (float): Lower frequency cutoffs in mel computation
        high_freq (float): Upper frequency cutoffs in mel computation
        vtln_warp_factor (float): Vtln warp factor
        mel_freq (Tensor): Given frequency in Mel

    Returns:
        Tensor: ``mel_freq`` after vtln warp
    """
    return mel_scale(vtln_warp_freq(vtln_low_cutoff, vtln_high_cutoff, low_freq, high_freq,
                                    vtln_warp_factor, inverse_mel_scale(mel_freq)))


def get_mel_banks(num_bins: int,
                  window_length_padded: int,
                  sample_freq: float,
                  low_freq: float,
                  high_freq: float,
                  vtln_low: float,
                  vtln_high: float,
                  vtln_warp_factor: float) -> Tuple[Tensor, Tensor]:
    """
    Returns:
        (Tensor, Tensor): The tuple consists of ``bins`` (which is
        melbank of size (``num_bins``, ``num_fft_bins``)) and ``center_freqs`` (which is
        center frequencies of bins of size (``num_bins``)).
    """
    assert num_bins > 3, 'Must have at least 3 mel bins'
    assert window_length_padded % 2 == 0
    num_fft_bins = window_length_padded / 2
    nyquist = 0.5 * sample_freq

    if high_freq <= 0.0:
        high_freq += nyquist

    assert (0.0 <= low_freq < nyquist) and (0.0 < high_freq <= nyquist) and (low_freq < high_freq), \
        ('Bad values in options: low-freq {} and high-freq {} vs. nyquist {}'.format(low_freq, high_freq, nyquist))

    # fft-bin width [think of it as Nyquist-freq / half-window-length]
    fft_bin_width = sample_freq / window_length_padded
    mel_low_freq = mel_scale_scalar(low_freq)
    mel_high_freq = mel_scale_scalar(high_freq)

    # divide by num_bins+1 in next line because of end-effects where the bins
    # spread out to the sides.
    mel_freq_delta = (mel_high_freq - mel_low_freq) / (num_bins + 1)

    if vtln_high < 0.0:
        vtln_high += nyquist

    assert vtln_warp_factor == 1.0 or ((low_freq < vtln_low < high_freq) and
                                       (0.0 < vtln_high < high_freq) and (vtln_low < vtln_high)), \
        ('Bad values in options: vtln-low {} and vtln-high {}, versus '
         'low-freq {} and high-freq {}'.format(vtln_low, vtln_high, low_freq, high_freq))

    bin = torch.arange(num_bins).unsqueeze(1)
    left_mel = mel_low_freq + bin * mel_freq_delta  # size(num_bins, 1)
    center_mel = mel_low_freq + (bin + 1.0) * mel_freq_delta  # size(num_bins, 1)
    right_mel = mel_low_freq + (bin + 2.0) * mel_freq_delta  # size(num_bins, 1)

    if vtln_warp_factor != 1.0:
        left_mel = vtln_warp_mel_freq(vtln_low, vtln_high, low_freq, high_freq, vtln_warp_factor, left_mel)
        center_mel = vtln_warp_mel_freq(vtln_low, vtln_high, low_freq, high_freq, vtln_warp_factor, center_mel)
        right_mel = vtln_warp_mel_freq(vtln_low, vtln_high, low_freq, high_freq, vtln_warp_factor, right_mel)

    center_freqs = inverse_mel_scale(center_mel)  # size (num_bins)
    # size(1, num_fft_bins)
    mel = mel_scale(fft_bin_width * torch.arange(num_fft_bins)).unsqueeze(0)

    # size (num_bins, num_fft_bins)
    up_slope = (mel - left_mel) / (center_mel - left_mel)
    down_slope = (right_mel - mel) / (right_mel - center_mel)

    if vtln_warp_factor == 1.0:
        # left_mel < center_mel < right_mel so we can min the two slopes and clamp negative values
        bins = torch.max(torch.zeros(1), torch.min(up_slope, down_slope))
    else:
        # warping can move the order of left_mel, center_mel, right_mel anywhere
        bins = torch.zeros_like(up_slope)
        up_idx = torch.gt(mel, left_mel) & torch.le(mel, center_mel)  # left_mel < mel <= center_mel
        down_idx = torch.gt(mel, center_mel) & torch.lt(mel, right_mel)  # center_mel < mel < right_mel
        bins[up_idx] = up_slope[up_idx]
        bins[down_idx] = down_slope[down_idx]

    return bins, center_freqs


def fbank(waveform: Tensor,
          blackman_coeff: float = 0.42,
          channel: int = -1,
          dither: float = 0.0,
          energy_floor: float = 1.0,
          frame_length: float = 25.0,
          frame_shift: float = 10.0,
          high_freq: float = 0.0,
          htk_compat: bool = False,
          low_freq: float = 20.0,
          min_duration: float = 0.0,
          num_mel_bins: int = 23,
          preemphasis_coefficient: float = 0.97,
          raw_energy: bool = True,
          remove_dc_offset: bool = True,
          round_to_power_of_two: bool = True,
          sample_frequency: float = 16000.0,
          snip_edges: bool = True,
          subtract_mean: bool = False,
          use_energy: bool = False,
          use_log_fbank: bool = True,
          use_power: bool = True,
          vtln_high: float = -500.0,
          vtln_low: float = 100.0,
          vtln_warp: float = 1.0,
          window_type: str = POVEY) -> Tensor:
    r"""Create a fbank from a raw audio signal. This matches the input/output of Kaldi's
    compute-fbank-feats.

    Args:
        waveform (Tensor): Tensor of audio of size (c, n) where c is in the range [0,2)
        blackman_coeff (float, optional): Constant coefficient for generalized Blackman window. (Default: ``0.42``)
        channel (int, optional): Channel to extract (-1 -> expect mono, 0 -> left, 1 -> right) (Default: ``-1``)
        dither (float, optional): Dithering constant (0.0 means no dither). If you turn this off, you should set
            the energy_floor option, e.g. to 1.0 or 0.1 (Default: ``0.0``)
        energy_floor (float, optional): Floor on energy (absolute, not relative) in Spectrogram computation.  Caution:
            this floor is applied to the zeroth component, representing the total signal energy.  The floor on the
            individual spectrogram elements is fixed at std::numeric_limits<float>::epsilon(). (Default: ``1.0``)
        frame_length (float, optional): Frame length in milliseconds (Default: ``25.0``)
        frame_shift (float, optional): Frame shift in milliseconds (Default: ``10.0``)
        high_freq (float, optional): High cutoff frequency for mel bins (if <= 0, offset from Nyquist)
         (Default: ``0.0``)
        htk_compat (bool, optional): If true, put energy last.  Warning: not sufficient to get HTK compatible features
         (need to change other parameters). (Default: ``False``)
        low_freq (float, optional): Low cutoff frequency for mel bins (Default: ``20.0``)
        min_duration (float, optional): Minimum duration of segments to process (in seconds). (Default: ``0.0``)
        num_mel_bins (int, optional): Number of triangular mel-frequency bins (Default: ``23``)
        preemphasis_coefficient (float, optional): Coefficient for use in signal preemphasis (Default: ``0.97``)
        raw_energy (bool, optional): If True, compute energy before preemphasis and windowing (Default: ``True``)
        remove_dc_offset (bool, optional): Subtract mean from waveform on each frame (Default: ``True``)
        round_to_power_of_two (bool, optional): If True, round window size to power of two by zero-padding input
            to FFT. (Default: ``True``)
        sample_frequency (float, optional): Waveform data sample frequency (must match the waveform file, if
            specified there) (Default: ``16000.0``)
        snip_edges (bool, optional): If True, end effects will be handled by outputting only frames that completely fit
            in the file, and the number of frames depends on the frame_length.  If False, the number of frames
            depends only on the frame_shift, and we reflect the data at the ends. (Default: ``True``)
        subtract_mean (bool, optional): Subtract mean of each feature file [CMS]; not recommended to do
            it this way.  (Default: ``False``)
        use_energy (bool, optional): Add an extra dimension with energy to the FBANK output. (Default: ``False``)
        use_log_fbank (bool, optional):If true, produce log-filterbank, else produce linear. (Default: ``True``)
        use_power (bool, optional): If true, use power, else use magnitude. (Default: ``True``)
        vtln_high (float, optional): High inflection point in piecewise linear VTLN warping function (if
            negative, offset from high-mel-freq (Default: ``-500.0``)
        vtln_low (float, optional): Low inflection point in piecewise linear VTLN warping function (Default: ``100.0``)
        vtln_warp (float, optional): Vtln warp factor (only applicable if vtln_map not specified) (Default: ``1.0``)
        window_type (str, optional): Type of window ('hamming'|'hanning'|'povey'|'rectangular'|'blackman')
         (Default: ``'povey'``)

    Returns:
        Tensor: A fbank identical to what Kaldi would output. The shape is (m, ``num_mel_bins + use_energy``)
        where m is calculated in _get_strided
    """
    device, dtype = waveform.device, waveform.dtype

    waveform, window_shift, window_size, padded_window_size = _get_waveform_and_window_properties(
        waveform, channel, sample_frequency, frame_shift, frame_length, round_to_power_of_two, preemphasis_coefficient)

    if len(waveform) < min_duration * sample_frequency:
        # signal is too short
        return torch.empty(0, device=device, dtype=dtype)

    # strided_input, size (m, padded_window_size) and signal_log_energy, size (m)
    strided_input, signal_log_energy = _get_window(
        waveform, padded_window_size, window_size, window_shift, window_type, blackman_coeff,
        snip_edges, raw_energy, energy_floor, dither, remove_dc_offset, preemphasis_coefficient)

    # size (m, padded_window_size // 2 + 1)
    spectrum = torchaudio._internal.fft.rfft(strided_input).abs()
    if use_power:
        spectrum = spectrum.pow(2.)

    # size (num_mel_bins, padded_window_size // 2)
    mel_energies, _ = get_mel_banks(num_mel_bins, padded_window_size, sample_frequency,
                                    low_freq, high_freq, vtln_low, vtln_high, vtln_warp)
    mel_energies = mel_energies.to(device=device, dtype=dtype)

    # pad right column with zeros and add dimension, size (num_mel_bins, padded_window_size // 2 + 1)
    mel_energies = torch.nn.functional.pad(mel_energies, (0, 1), mode='constant', value=0)

    # sum with mel fiterbanks over the power spectrum, size (m, num_mel_bins)
    mel_energies = torch.mm(spectrum, mel_energies.T)
    if use_log_fbank:
        # avoid log of zero (which should be prevented anyway by dithering)
        mel_energies = torch.max(mel_energies, _get_epsilon(device, dtype)).log()

    # if use_energy then add it as the last column for htk_compat == true else first column
    if use_energy:
        signal_log_energy = signal_log_energy.unsqueeze(1)  # size (m, 1)
        # returns size (m, num_mel_bins + 1)
        if htk_compat:
            mel_energies = torch.cat((mel_energies, signal_log_energy), dim=1)
        else:
            mel_energies = torch.cat((signal_log_energy, mel_energies), dim=1)

    mel_energies = _subtract_column_mean(mel_energies, subtract_mean)
    return mel_energies


def _get_dct_matrix(num_ceps: int, num_mel_bins: int) -> Tensor:
    # returns a dct matrix of size (num_mel_bins, num_ceps)
    # size (num_mel_bins, num_mel_bins)
    dct_matrix = torchaudio.functional.create_dct(num_mel_bins, num_mel_bins, 'ortho')
    # kaldi expects the first cepstral to be weighted sum of factor sqrt(1/num_mel_bins)
    # this would be the first column in the dct_matrix for torchaudio as it expects a
    # right multiply (which would be the first column of the kaldi's dct_matrix as kaldi
    # expects a left multiply e.g. dct_matrix * vector).
    dct_matrix[:, 0] = math.sqrt(1 / float(num_mel_bins))
    dct_matrix = dct_matrix[:, :num_ceps]
    return dct_matrix


def _get_lifter_coeffs(num_ceps: int, cepstral_lifter: float) -> Tensor:
    # returns size (num_ceps)
    # Compute liftering coefficients (scaling on cepstral coeffs)
    # coeffs are numbered slightly differently from HTK: the zeroth index is C0, which is not affected.
    i = torch.arange(num_ceps)
    return 1.0 + 0.5 * cepstral_lifter * torch.sin(math.pi * i / cepstral_lifter)


def mfcc(
        waveform: Tensor,
        blackman_coeff: float = 0.42,
        cepstral_lifter: float = 22.0,
        channel: int = -1,
        dither: float = 0.0,
        energy_floor: float = 1.0,
        frame_length: float = 25.0,
        frame_shift: float = 10.0,
        high_freq: float = 0.0,
        htk_compat: bool = False,
        low_freq: float = 20.0,
        num_ceps: int = 13,
        min_duration: float = 0.0,
        num_mel_bins: int = 23,
        preemphasis_coefficient: float = 0.97,
        raw_energy: bool = True,
        remove_dc_offset: bool = True,
        round_to_power_of_two: bool = True,
        sample_frequency: float = 16000.0,
        snip_edges: bool = True,
        subtract_mean: bool = False,
        use_energy: bool = False,
        vtln_high: float = -500.0,
        vtln_low: float = 100.0,
        vtln_warp: float = 1.0,
        window_type: str = POVEY) -> Tensor:
    r"""Create a mfcc from a raw audio signal. This matches the input/output of Kaldi's
    compute-mfcc-feats.

    Args:
        waveform (Tensor): Tensor of audio of size (c, n) where c is in the range [0,2)
        blackman_coeff (float, optional): Constant coefficient for generalized Blackman window. (Default: ``0.42``)
        cepstral_lifter (float, optional): Constant that controls scaling of MFCCs (Default: ``22.0``)
        channel (int, optional): Channel to extract (-1 -> expect mono, 0 -> left, 1 -> right) (Default: ``-1``)
        dither (float, optional): Dithering constant (0.0 means no dither). If you turn this off, you should set
            the energy_floor option, e.g. to 1.0 or 0.1 (Default: ``0.0``)
        energy_floor (float, optional): Floor on energy (absolute, not relative) in Spectrogram computation.  Caution:
            this floor is applied to the zeroth component, representing the total signal energy.  The floor on the
            individual spectrogram elements is fixed at std::numeric_limits<float>::epsilon(). (Default: ``1.0``)
        frame_length (float, optional): Frame length in milliseconds (Default: ``25.0``)
        frame_shift (float, optional): Frame shift in milliseconds (Default: ``10.0``)
        high_freq (float, optional): High cutoff frequency for mel bins (if <= 0, offset from Nyquist)
         (Default: ``0.0``)
        htk_compat (bool, optional): If true, put energy last.  Warning: not sufficient to get HTK compatible
         features (need to change other parameters). (Default: ``False``)
        low_freq (float, optional): Low cutoff frequency for mel bins (Default: ``20.0``)
        num_ceps (int, optional): Number of cepstra in MFCC computation (including C0) (Default: ``13``)
        min_duration (float, optional): Minimum duration of segments to process (in seconds). (Default: ``0.0``)
        num_mel_bins (int, optional): Number of triangular mel-frequency bins (Default: ``23``)
        preemphasis_coefficient (float, optional): Coefficient for use in signal preemphasis (Default: ``0.97``)
        raw_energy (bool, optional): If True, compute energy before preemphasis and windowing (Default: ``True``)
        remove_dc_offset (bool, optional): Subtract mean from waveform on each frame (Default: ``True``)
        round_to_power_of_two (bool, optional): If True, round window size to power of two by zero-padding input
            to FFT. (Default: ``True``)
        sample_frequency (float, optional): Waveform data sample frequency (must match the waveform file, if
            specified there) (Default: ``16000.0``)
        snip_edges (bool, optional): If True, end effects will be handled by outputting only frames that completely fit
            in the file, and the number of frames depends on the frame_length.  If False, the number of frames
            depends only on the frame_shift, and we reflect the data at the ends. (Default: ``True``)
        subtract_mean (bool, optional): Subtract mean of each feature file [CMS]; not recommended to do
            it this way.  (Default: ``False``)
        use_energy (bool, optional): Add an extra dimension with energy to the FBANK output. (Default: ``False``)
        vtln_high (float, optional): High inflection point in piecewise linear VTLN warping function (if
            negative, offset from high-mel-freq (Default: ``-500.0``)
        vtln_low (float, optional): Low inflection point in piecewise linear VTLN warping function (Default: ``100.0``)
        vtln_warp (float, optional): Vtln warp factor (only applicable if vtln_map not specified) (Default: ``1.0``)
        window_type (str, optional): Type of window ('hamming'|'hanning'|'povey'|'rectangular'|'blackman')
         (Default: ``"povey"``)

    Returns:
        Tensor: A mfcc identical to what Kaldi would output. The shape is (m, ``num_ceps``)
        where m is calculated in _get_strided
    """
    assert num_ceps <= num_mel_bins, 'num_ceps cannot be larger than num_mel_bins: %d vs %d' % (num_ceps, num_mel_bins)

    device, dtype = waveform.device, waveform.dtype

    # The mel_energies should not be squared (use_power=True), not have mean subtracted
    # (subtract_mean=False), and use log (use_log_fbank=True).
    # size (m, num_mel_bins + use_energy)
    feature = fbank(waveform=waveform, blackman_coeff=blackman_coeff, channel=channel,
                    dither=dither, energy_floor=energy_floor, frame_length=frame_length,
                    frame_shift=frame_shift, high_freq=high_freq, htk_compat=htk_compat,
                    low_freq=low_freq, min_duration=min_duration, num_mel_bins=num_mel_bins,
                    preemphasis_coefficient=preemphasis_coefficient, raw_energy=raw_energy,
                    remove_dc_offset=remove_dc_offset, round_to_power_of_two=round_to_power_of_two,
                    sample_frequency=sample_frequency, snip_edges=snip_edges, subtract_mean=False,
                    use_energy=use_energy, use_log_fbank=True, use_power=True,
                    vtln_high=vtln_high, vtln_low=vtln_low, vtln_warp=vtln_warp, window_type=window_type)

    if use_energy:
        # size (m)
        signal_log_energy = feature[:, num_mel_bins if htk_compat else 0]
        # offset is 0 if htk_compat==True else 1
        mel_offset = int(not htk_compat)
        feature = feature[:, mel_offset:(num_mel_bins + mel_offset)]

    # size (num_mel_bins, num_ceps)
    dct_matrix = _get_dct_matrix(num_ceps, num_mel_bins).to(dtype=dtype, device=device)

    # size (m, num_ceps)
    feature = feature.matmul(dct_matrix)

    if cepstral_lifter != 0.0:
        # size (1, num_ceps)
        lifter_coeffs = _get_lifter_coeffs(num_ceps, cepstral_lifter).unsqueeze(0)
        feature *= lifter_coeffs.to(device=device, dtype=dtype)

    # if use_energy then replace the last column for htk_compat == true else first column
    if use_energy:
        feature[:, 0] = signal_log_energy

    if htk_compat:
        energy = feature[:, 0].unsqueeze(1)  # size (m, 1)
        feature = feature[:, 1:]  # size (m, num_ceps - 1)
        if not use_energy:
            # scale on C0 (actually removing a scale we previously added that's
            # part of one common definition of the cosine transform.)
            energy *= math.sqrt(2)

        feature = torch.cat((feature, energy), dim=1)

    feature = _subtract_column_mean(feature, subtract_mean)
    return feature


def _get_sinc_resample_kernel(orig_freq: int, new_freq: int, lowpass_filter_width: int,
                              device: torch.device, dtype: torch.dtype):
    assert lowpass_filter_width > 0
    kernels = []
    base_freq = min(orig_freq, new_freq)
    # This will perform antialiasing filtering by removing the highest frequencies.
    # At first I thought I only needed this when downsampling, but when upsampling
    # you will get edge artifacts without this, as the edge is equivalent to zero padding,
    # which will add high freq artifacts.
    base_freq *= 0.99

    # The key idea of the algorithm is that x(t) can be exactly reconstructed from x[i] (tensor)
    # using the sinc interpolation formula:
    #   x(t) = sum_i x[i] sinc(pi * orig_freq * (i / orig_freq - t))
    # We can then sample the function x(t) with a different sample rate:
    #    y[j] = x(j / new_freq)
    # or,
    #    y[j] = sum_i x[i] sinc(pi * orig_freq * (i / orig_freq - j / new_freq))

    # We see here that y[j] is the convolution of x[i] with a specific filter, for which
    # we take an FIR approximation, stopping when we see at least `lowpass_filter_width` zeros crossing.
    # But y[j+1] is going to have a different set of weights and so on, until y[j + new_freq].
    # Indeed:
    # y[j + new_freq] = sum_i x[i] sinc(pi * orig_freq * ((i / orig_freq - (j + new_freq) / new_freq))
    #                 = sum_i x[i] sinc(pi * orig_freq * ((i - orig_freq) / orig_freq - j / new_freq))
    #                 = sum_i x[i + orig_freq] sinc(pi * orig_freq * (i / orig_freq - j / new_freq))
    # so y[j+new_freq] uses the same filter as y[j], but on a shifted version of x by `orig_freq`.
    # This will explain the F.conv1d after, with a stride of orig_freq.
    width = math.ceil(lowpass_filter_width * orig_freq / base_freq)
    # If orig_freq is still big after GCD reduction, most filters will be very unbalanced, i.e.,
    # they will have a lot of almost zero values to the left or to the right...
    # There is probably a way to evaluate those filters more efficiently, but this is kept for
    # future work.
    idx = torch.arange(-width, width + orig_freq, device=device, dtype=dtype)

    for i in range(new_freq):
        t = (-i / new_freq + idx / orig_freq) * base_freq
        t = t.clamp_(-lowpass_filter_width, lowpass_filter_width)
        t *= math.pi
        # we do not use torch.hann_window here as we need to evaluate the window
        # at spectifics positions, not over a regular grid.
        window = torch.cos(t / lowpass_filter_width / 2)**2
        kernel = torch.where(t == 0, torch.tensor(1.).to(t), torch.sin(t) / t)
        kernel.mul_(window)
        kernels.append(kernel)

    scale = base_freq / orig_freq
    return torch.stack(kernels).view(new_freq, 1, -1).mul_(scale), width


def resample_waveform(waveform: Tensor,
                      orig_freq: float,
                      new_freq: float,
                      lowpass_filter_width: int = 6) -> Tensor:
    r"""Resamples the waveform at the new frequency. This matches Kaldi's OfflineFeatureTpl ResampleWaveform
    which uses a LinearResample (resample a signal at linearly spaced intervals to upsample/downsample
    a signal). LinearResample (LR) means that the output signal is at linearly spaced intervals (i.e
    the output signal has a frequency of ``new_freq``). It uses sinc/bandlimited interpolation to
    upsample/downsample the signal.

    https://ccrma.stanford.edu/~jos/resample/Theory_Ideal_Bandlimited_Interpolation.html
    https://github.com/kaldi-asr/kaldi/blob/master/src/feat/resample.h#L56

    Args:
        waveform (Tensor): The input signal of size (c, n)
        orig_freq (float): The original frequency of the signal
        new_freq (float): The desired frequency
        lowpass_filter_width (int, optional): Controls the sharpness of the filter, more == sharper
            but less efficient. We suggest around 4 to 10 for normal use. (Default: ``6``)

    Returns:
        Tensor: The waveform at the new frequency

    .. caution::
        After dividing `orig_freq` and `new_freq` by their GCD, both should be small
        for this implementation to be fast.
    """
    assert waveform.dim() == 2
    assert orig_freq > 0.0 and new_freq > 0.0

    orig_freq = int(orig_freq)
    new_freq = int(new_freq)
    gcd = math.gcd(orig_freq, new_freq)
    orig_freq = orig_freq // gcd
    new_freq = new_freq // gcd

    kernel, width = _get_sinc_resample_kernel(orig_freq, new_freq, lowpass_filter_width,
                                              waveform.device, waveform.dtype)

    num_wavs, length = waveform.shape
    waveform = F.pad(waveform, (width, width + orig_freq))
    resampled = F.conv1d(waveform[:, None], kernel, stride=orig_freq)
    resampled = resampled.transpose(1, 2).reshape(num_wavs, -1)
    target_length = int(math.ceil(new_freq * length / orig_freq))
    return resampled[..., :target_length]
