#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
"""
The front-end filter, in numpy instead of GNU Radio.

This is the one piece the Raspberry Pi port had to rewrite. Everything else in
the decoder was already numpy or stdlib; the live path was not, because it ran
on ``filter.freq_xlating_fir_filter_ccf`` and ``firdes.low_pass`` out of
gnuradio, with the samples arriving from ``osmosdr.source``. GNU Radio and
gr-osmosdr can both be installed on Bookworm, but they are a large dependency
to carry for two blocks, and gr-osmosdr in particular is the part of that stack
most likely to be missing or mismatched on a given image. Reimplementing the
two blocks costs about a hundred lines and leaves the whole decoder depending
on nothing but numpy.

What is reproduced here, exactly:

  * ``firdes.low_pass(1, fs, cutoff, transition)`` -- a Hamming-windowed sinc
    with unity DC gain, and with GNU Radio's own tap-count heuristic, so the
    passband and stopband come out where the bench flowgraph put them.

  * ``freq_xlating_fir_filter_ccf(decim, taps, center, fs)`` -- mix the band of
    interest down to DC, low-pass it, and decimate, as one operation.

Why one operation rather than three. The naive reading is "multiply every input
sample by a complex exponential, then filter, then throw away all but every Dth
output". Done literally that is wasteful twice over: the exponential is
evaluated at the full input rate, and the filter computes D-1 outputs for every
one that is kept. Both are avoidable, and the identity that avoids them is what
GNU Radio's block is built on too:

    y[m] = SUM_k h[k] . x[n-k] . e^(-jw(n-k))         (n = the input index of
         = e^(-jwn) . SUM_k (h[k] e^(+jwk)) . x[n-k]   output m)

So fold the exponential into the taps once, at startup, and rotate only the
*output* samples - of which there are D times fewer. The filter then evaluates
only at the decimated positions. For the default chain that is a factor of six
off both costs.

Streaming is overlap-save: the last (ntaps-1) input samples are carried into the
next block, so a packet that straddles a block boundary is filtered exactly as
if the stream had been one array. The output sample grid is tracked in absolute
input samples, which is what keeps the decimation phase continuous across
blocks - a phase that slipped at every boundary would put a timing discontinuity
in the middle of a burst roughly once a second.
"""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# Largest number of output samples to form in one matrix multiply. The filter
# builds an (outputs x ntaps) matrix of windows to hand to BLAS, so this bounds
# that temporary: 16384 x 115 complex64 is about 15 MB, which stays in a sane
# fraction of a 4 GB Pi's memory and keeps the working set near cache. It has
# no effect on the output, only on the size of the intermediate.
_MAX_OUT_CHUNK = 16384


def lowpass_taps(fs, cutoff_hz, transition_hz):
    """Hamming-windowed sinc low-pass, equivalent to firdes.low_pass(1, ...).

    The tap count follows GNU Radio's heuristic rather than being chosen here,
    so that a chain built on these taps has the same transition width and the
    same stopband as the bench flowgraph did: for a Hamming window GNU Radio
    budgets about 53 dB of attenuation and solves

        ntaps = attenuation_dB * fs / (22 * transition_width)

    Forced odd, so the filter is symmetric about an actual sample and its group
    delay is a whole number of samples rather than half of one. That matters
    here because the decimation phase below is reckoned in whole input samples.
    """
    fs = float(fs)
    cutoff_hz = float(cutoff_hz)
    transition_hz = float(transition_hz)

    if not 0.0 < cutoff_hz < fs / 2.0:
        raise ValueError('cutoff %.1f Hz must be between 0 and fs/2 (%.1f Hz)'
                         % (cutoff_hz, fs / 2.0))
    if transition_hz <= 0.0:
        raise ValueError('transition width must be positive')

    ntaps = int(53.0 * fs / (22.0 * transition_hz))
    ntaps = max(ntaps, 3) | 1

    n = np.arange(ntaps, dtype=np.float64) - (ntaps - 1) / 2.0
    fc = cutoff_hz / fs                      # cycles per sample
    h = 2.0 * fc * np.sinc(2.0 * fc * n)     # np.sinc(x) = sin(pi x)/(pi x)
    h *= np.hamming(ntaps)
    h /= h.sum()                             # unity gain at DC
    return h


class FreqXlatingDecimator(object):
    """Mix by -center_hz, low-pass, and decimate - as one streaming operation.

    Call the instance with successive blocks of complex samples; it returns the
    decimated output for each. Blocks may be any length, including shorter than
    the filter, and the output of a run is identical to what the same samples
    would have produced had they arrived as a single array.
    """

    def __init__(self, taps, decim, center_hz, fs):
        self.decim = int(decim)
        if self.decim < 1:
            raise ValueError('decimation must be >= 1')
        self.fs = float(fs)
        self.center_hz = float(center_hz)
        self.ntaps = len(taps)

        # Radians per input sample of the shift. Negative center_hz shifts the
        # band up, matching GNU Radio's sign convention: the block tunes the
        # frequency given to DC, so passing +100 kHz selects the band sitting
        # 100 kHz above the LO.
        self._w = 2.0 * np.pi * self.center_hz / self.fs

        # The exponential folded into the taps, reversed once here so the inner
        # loop is a plain dot product rather than a reversed one.
        k = np.arange(self.ntaps, dtype=np.float64)
        hc = np.asarray(taps, dtype=np.float64) * np.exp(1j * self._w * k)
        self._hc_rev = hc[::-1].astype(np.complex64).copy()

        # Overlap-save state.
        self._hist = np.zeros(self.ntaps - 1, dtype=np.complex64)
        self._total = 0        # absolute index of the next unseen input sample
        self._n_next = 0       # absolute input index of the next output sample
        self._phase = 0.0      # the output rotation's phase at _n_next

    @property
    def output_rate(self):
        return self.fs / self.decim

    def __call__(self, x):
        x = np.asarray(x, dtype=np.complex64)
        buf = np.concatenate((self._hist, x))

        # Absolute input index of buf[0]. Negative on the first call, when the
        # history is still the zeros the filter starts with - which is the same
        # startup transient GNU Radio has, and is why the first few hundred
        # microseconds of a capture are not trusted by anything downstream.
        base = self._total - len(self._hist)

        # An output at absolute index n consumes buf[n-base-ntaps+1 .. n-base],
        # so n is producible while both ends of that window are inside buf.
        hi = base + len(buf) - 1
        ns = np.arange(self._n_next, hi + 1, self.decim, dtype=np.int64)

        out = self._filter(buf, base, ns)

        if ns.size:
            n_after = int(ns[-1]) + self.decim
            self._phase = (self._phase
                           + self._w * (n_after - self._n_next)) % (2.0 * np.pi)
            self._n_next = n_after

        self._total += len(x)
        if self.ntaps > 1:
            self._hist = buf[-(self.ntaps - 1):].copy()
        return out

    def _filter(self, buf, base, ns):
        if ns.size == 0:
            return np.empty(0, dtype=np.complex64)

        # Start of each output's window, as an index into buf.
        starts = ns - base - (self.ntaps - 1)
        windows = sliding_window_view(buf, self.ntaps)

        pieces = []
        for lo in range(0, starts.size, _MAX_OUT_CHUNK):
            sl = starts[lo:lo + _MAX_OUT_CHUNK]
            # Fancy-indexing the sliding view materialises just these rows.
            y = windows[sl] @ self._hc_rev

            # The output-rate rotation: e^(-jw n), written relative to _n_next
            # so the argument stays small no matter how long the run has been
            # going. Accumulating w*n absolutely would lose resolution after a
            # few hours of samples; this cannot.
            rel = (ns[lo:lo + _MAX_OUT_CHUNK] - self._n_next).astype(np.float64)
            y *= np.exp(-1j * (self._phase + self._w * rel)).astype(np.complex64)
            pieces.append(y.astype(np.complex64, copy=False))

        return pieces[0] if len(pieces) == 1 else np.concatenate(pieces)


def reference_xlate(x, taps, decim, center_hz, fs):
    """The same operation written the slow, obvious way, for testing only.

    Mix every sample, convolve, then keep every Dth output - no tap folding, no
    output-rate rotation, no streaming. FreqXlatingDecimator has to agree with
    this to within float32 rounding; if it ever does not, this is the one that
    is right.
    """
    x = np.asarray(x, dtype=np.complex128)
    n = np.arange(len(x), dtype=np.float64)
    mixed = x * np.exp(-2j * np.pi * float(center_hz) * n / float(fs))
    padded = np.concatenate((np.zeros(len(taps) - 1, dtype=np.complex128),
                             mixed))
    full = np.convolve(padded, np.asarray(taps, dtype=np.float64), mode='valid')
    return full[::int(decim)]
