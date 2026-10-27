#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
"""
Tests for si4463_dsp, the numpy replacement for the GNU Radio front end.

si4463_selftest.py covers the decoder; this covers the two blocks that used to
be GNU Radio's and now are not. The thing worth testing is not "does a filter
come out" but the three properties the live path silently depends on:

  * the fast form agrees with the obvious form. FreqXlatingDecimator folds the
    mixer into its taps and rotates only its outputs, which is an algebraic
    identity rather than an approximation - so it must match a literal
    mix-filter-decimate to float32 rounding, and reference_xlate is that
    literal version.

  * streaming agrees with one shot. The live source delivers whatever libusb
    hands it, in blocks that are neither equal nor aligned to anything, and a
    packet lands across those boundaries roughly once a second. Feeding the
    same samples in ragged pieces must give the same output as feeding them at
    once - to float rounding, not bit for bit. The difference is the output
    rotation's phase accumulator: it carries (phase + w*dn) mod 2pi forward
    from block to block, so a different block layout takes a different number
    of additions to reach the same sample and lands within an ulp or two of the
    same phase, not on it. Exact reproducibility would need the phase reckoned
    from the absolute sample index in exact arithmetic, and center/fs is not a
    rational with a usable denominator once it has been through float64 - so it
    would cost real complexity to remove an error 130 dB below full scale that
    the per-burst carrier estimate in si4463_core removes anyway.

  * the decimation phase does not slip. This is the specific failure the
    absolute-index bookkeeping exists to prevent, it is invisible in a
    single-block test, and unlike the above it is exactly testable: an impulse
    at a known input sample must come out at the same output index no matter
    how the input was chopped up. An off-by-one here is a half-symbol timing
    error that no amount of downstream work recovers.

Run after any change to si4463_dsp.py:

    python3 si4463_dsp_selftest.py
"""

import sys

import numpy as np

from si4463_dsp import (FreqXlatingDecimator, lowpass_taps, reference_xlate)

FAILS = []


def check(name, ok, detail=''):
    print('  %-52s %s' % (name, 'ok' if ok else 'FAIL'))
    if detail:
        print('      %s' % detail)
    if not ok:
        FAILS.append(name)


def peak_err(a, b):
    n = min(len(a), len(b))
    if n == 0:
        return float('inf')
    return float(np.max(np.abs(a[:n] - b[:n])))


def test_taps():
    print('low-pass design')
    fs, cutoff, trans = 200e3, 60e3, 25e3
    h = lowpass_taps(fs, cutoff, trans)

    check('tap count matches firdes heuristic (53*fs/(22*tw), odd)',
          len(h) == (int(53.0 * fs / (22.0 * trans)) | 1),
          'ntaps = %d' % len(h))
    check('odd length', len(h) % 2 == 1)
    check('unity DC gain', abs(h.sum() - 1.0) < 1e-12,
          'sum = %.15f' % h.sum())
    check('symmetric (linear phase)',
          np.allclose(h, h[::-1], atol=1e-15))

    # Response at a few frequencies, via the DTFT of the taps.
    def gain_db(f):
        n = np.arange(len(h))
        return 20 * np.log10(abs(np.sum(h * np.exp(-2j * np.pi * f * n / fs)))
                             + 1e-300)

    check('passband flat at DC and 40 kHz (< 1 dB)',
          abs(gain_db(0.0)) < 1.0 and abs(gain_db(40e3)) < 1.0,
          'DC %.2f dB, 40k %.2f dB' % (gain_db(0.0), gain_db(40e3)))
    # Stopband starts at cutoff + transition = 85 kHz; check past that.
    check('stopband attenuation beyond 90 kHz (> 35 dB)',
          gain_db(90e3) < -35.0, '90k %.1f dB' % gain_db(90e3))


def test_matches_reference():
    print('\nfast form vs literal mix-filter-decimate')
    rng = np.random.default_rng(20261027)
    fs, decim, center = 1.2e6, 6, 100e3
    h = lowpass_taps(fs, 60e3, 25e3)
    n = 60000
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)

    ref = reference_xlate(x, h, decim, center, fs)
    got = FreqXlatingDecimator(h, decim, center, fs)(x)

    check('output length agrees',
          abs(len(got) - len(ref)) <= 1,
          'fast %d, reference %d' % (len(got), len(ref)))
    err = peak_err(got, ref)
    # float32 taps and samples over a 115-tap sum: a few 1e-5 is rounding.
    check('samples agree to float32 rounding (peak err < 2e-4)',
          err < 2e-4, 'peak |err| = %.3e' % err)


def test_streaming_equals_one_shot():
    print('\nstreaming vs one shot')
    rng = np.random.default_rng(7)
    fs, decim, center = 1.2e6, 6, 100e3
    h = lowpass_taps(fs, 60e3, 25e3)
    n = 50000
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)

    one = FreqXlatingDecimator(h, decim, center, fs)(x)

    # Ragged blocks, deliberately not multiples of the decimation factor and
    # including blocks shorter than the filter - which is what libusb does when
    # a transfer is cut short.
    for label, sizes in (
            ('equal blocks of 4096', [4096] * (n // 4096 + 1)),
            ('ragged 1..9000 blocks', None),
            ('tiny blocks of 7', [7] * (n // 7 + 1)),
            ('one block of 1 then the rest', [1, n - 1]),
    ):
        if sizes is None:
            r = np.random.default_rng(99)
            sizes = []
            while sum(sizes) < n:
                sizes.append(int(r.integers(1, 9000)))
        dec = FreqXlatingDecimator(h, decim, center, fs)
        out, pos = [], 0
        for s in sizes:
            if pos >= n:
                break
            out.append(dec(x[pos:pos + s]))
            pos += s
        streamed = np.concatenate([o for o in out if o.size])

        same_len = len(streamed) == len(one)
        err = peak_err(streamed, one)
        # Length exactly: not one sample gained or lost, whatever the blocking.
        # Values to float32 rounding: see the note in the module docstring.
        check('%-34s same length as one shot' % label, same_len,
              'len %d vs %d' % (len(streamed), len(one)))
        check('%-34s agrees to float rounding' % label, err < 2e-6,
              'peak |err| = %.3e' % err)


def test_decimation_phase_is_exact():
    """An impulse must land at the same output sample for every blocking.

    This is the exact counterpart to the float-level comparison above. The
    output grid is reckoned in absolute input samples, so output m is always
    input sample m*D regardless of how the stream was delivered; if the
    bookkeeping instead restarted the grid per block, the impulse would move by
    up to D-1 samples depending on where the block boundaries happened to fall.
    At 20 samples per symbol a slip of a few samples is a fraction of a symbol
    - enough to cost the packet, not enough to look like anything but a weak
    signal in the log.
    """
    print('\ndecimation phase (exact)')
    fs, decim, center = 1.2e6, 6, 0.0
    h = lowpass_taps(fs, 60e3, 25e3)
    n = 40000
    spike_at = 12345

    x = np.zeros(n, dtype=np.complex64)
    x[spike_at] = 1.0

    positions = {}
    for label, sizes in (
            ('one block', [n]),
            ('blocks of 4096', [4096] * (n // 4096 + 1)),
            ('blocks of 7', [7] * (n // 7 + 1)),
            ('blocks of 1000 (not a multiple of 6)', [1000] * (n // 1000 + 1)),
            ('1, then 13, then the rest', [1, 13, n]),
    ):
        dec = FreqXlatingDecimator(h, decim, center, fs)
        out, pos = [], 0
        for sz in sizes:
            if pos >= n:
                break
            out.append(dec(x[pos:pos + sz]))
            pos += sz
        y = np.concatenate([o for o in out if o.size])
        positions[label] = int(np.argmax(np.abs(y)))

    peaks = set(positions.values())
    check('impulse lands at one output index for every blocking',
          len(peaks) == 1,
          ', '.join('%s: %d' % (k, v) for k, v in positions.items()))

    # And that index is where the arithmetic says it should be: the filter's
    # group delay is (ntaps-1)/2 input samples, so the peak sits at input
    # sample spike_at + (ntaps-1)/2, and the nearest output on the m*D grid is
    # that divided by D.
    expect = (spike_at + (len(h) - 1) // 2) // decim
    got = positions['one block']
    check('and at the index the group delay predicts',
          abs(got - expect) <= 1, 'got %d, predicted %d' % (got, expect))


def test_tone_lands_at_dc():
    print('\ntuning')
    fs, decim, center = 1.2e6, 6, 100e3
    h = lowpass_taps(fs, 60e3, 25e3)
    n = 120000
    t = np.arange(n) / fs

    # A tone exactly at the frequency being selected must come out at DC, i.e.
    # as a constant. This is the test that catches a sign error in the mixer -
    # which would otherwise show only as "no packets ever decode".
    x = np.exp(2j * np.pi * center * t).astype(np.complex64)
    y = FreqXlatingDecimator(h, decim, center, fs)(x)
    settled = y[len(h) // decim + 10:]
    ripple = float(np.std(np.abs(settled)))
    phase_spread = float(np.std(np.unwrap(np.angle(settled))))
    check('tone at +center comes out at DC (constant)',
          ripple < 1e-3 and phase_spread < 1e-3,
          '|y| std %.2e, phase std %.2e rad' % (ripple, phase_spread))
    check('and at unity amplitude',
          abs(float(np.mean(np.abs(settled))) - 1.0) < 1e-2,
          'mean |y| = %.4f' % float(np.mean(np.abs(settled))))

    # A tone far outside the passband must be rejected, not aliased in.
    x2 = np.exp(2j * np.pi * (center + 300e3) * t).astype(np.complex64)
    y2 = FreqXlatingDecimator(h, decim, center, fs)(x2)
    settled2 = y2[len(h) // decim + 10:]
    rej_db = 20 * np.log10(float(np.mean(np.abs(settled2))) + 1e-300)
    check('tone 300 kHz off is rejected (> 35 dB down)',
          rej_db < -35.0, 'residual %.1f dB' % rej_db)


def test_decim_one():
    print('\ndecimation by 1 (the 200 kHz WAV chain)')
    rng = np.random.default_rng(3)
    fs = 200e3
    h = lowpass_taps(fs, 60e3, 25e3)
    n = 20000
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    ref = reference_xlate(x, h, 1, -12600.0, fs)
    got = FreqXlatingDecimator(h, 1, -12600.0, fs)(x)
    check('matches reference at decim=1',
          peak_err(got, ref) < 2e-4,
          'peak |err| = %.3e, %d samples' % (peak_err(got, ref), len(got)))
    check('output length == input length at decim=1', len(got) == n,
          'got %d, expected %d' % (len(got), n))


def main():
    print('si4463_dsp selftest\n')
    test_taps()
    test_matches_reference()
    test_streaming_equals_one_shot()
    test_decimation_phase_is_exact()
    test_tone_lands_at_dc()
    test_decim_one()

    print('\n%s' % ('ALL PASS' if not FAILS
                    else 'FAILED: %d (%s)' % (len(FAILS), ', '.join(FAILS))))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
