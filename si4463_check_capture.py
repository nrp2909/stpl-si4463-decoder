#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Is the Si4463 actually in this recording, and at what offset?

Answers the question that costs the most time when a decode comes up empty:
whether the problem is the decoder settings or whether the transmitter simply
was not on air. It sweeps the whole recorded band rather than one channel, so
a capture centred on the wrong frequency, or a transmitter that has wandered,
still shows up.

What it looks for is the shape of the frame rather than its content: the bench
firmware sends 16 bytes of preamble, 2 sync bytes and 45 payload bytes at
10 kbps, so every burst is 63 * 8 / 10000 = 50.4 ms long, and main.c sends one
per second. A run of same-length bursts at a steady spacing is the signature;
noise and unrelated ISM traffic do not produce it.

Usage:
    python si4463_check_capture.py <recording.wav> [more.wav ...]

If it finds the transmitter it prints the centre offset to put in
center_offset in si4463_bench_decode.py / .grc.
"""

import os
import struct
import sys
import wave

import numpy as np

SYMBOL_RATE = 10000.0
FRAME_BYTES = 16 + 2 + 45              # preamble + sync + payload
FRAME_SEC = FRAME_BYTES * 8 / SYMBOL_RATE
DEVIATION = 20000.0


def _format_tag(path):
    """Read wFormatTag out of the fmt chunk: 1 = integer PCM, 3 = IEEE float.

    A 32-bit SDR++ recording can be either, and the two are indistinguishable
    from the sample width alone -- reading int32 as float32 yields denormals
    and infinities rather than an obvious error, so it has to be checked.
    """
    with open(path, 'rb') as f:
        head = f.read(4096)
    i = head.find(b'fmt ')
    if i < 0:
        return 1
    return struct.unpack('<H', head[i + 8:i + 10])[0]


def load(path):
    with wave.open(path) as w:
        fs, n, sw = w.getframerate(), w.getnframes(), w.getsampwidth()
        data = w.readframes(n)
    tag = _format_tag(path)
    if sw == 2:
        raw = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0
    elif sw == 4 and tag == 3:
        raw = np.frombuffer(data, dtype='<f4').astype(np.float32)
    elif sw == 4:
        raw = np.frombuffer(data, dtype='<i4').astype(np.float32) / 2147483648.0
    else:
        raw = (np.frombuffer(data, dtype='u1').astype(np.float32) - 128) / 128.0
    raw = np.nan_to_num(raw, posinf=0.0, neginf=0.0)
    return fs, (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)


def check(path):
    print('=' * 78)
    print(os.path.basename(path))
    try:
        fs, iq = load(path)
    except Exception as exc:
        print('  cannot read: %s' % exc)
        return
    if len(iq) == 0:
        print('  empty recording (0 frames) -- SDR++ wrote only a header.')
        return

    dur = len(iq) / fs
    rms = 20 * np.log10(np.sqrt(np.mean(np.abs(iq) ** 2)) + 1e-20)
    print('  %.2f s at %.1f MS/s, RMS %.1f dBFS' % (dur, fs / 1e6, rms))
    if dur < 2 * FRAME_SEC:
        print('  too short to contain a burst.')
        return

    # ~1 ms x ~1 kHz spectrogram: fine enough in time to measure a 50 ms burst,
    # fine enough in frequency to separate the two FSK tones.
    n = 2048
    nblk = len(iq) // n
    spec = np.fft.fftshift(
        np.abs(np.fft.fft(iq[:nblk * n].reshape(nblk, n) * np.hanning(n), axis=1)) ** 2,
        axes=1)
    freq = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / fs))
    dt = n / fs

    # Slide a 50 kHz window across the band -- wide enough to hold the whole
    # 45 kHz-wide 2GFSK burst, narrow enough not to drown it in noise.
    width = 50e3
    step = 25e3
    best = []
    for lo in np.arange(-fs / 2, fs / 2 - width, step):
        sel = (freq >= lo) & (freq < lo + width)
        if sel.sum() < 4:
            continue
        power = 10 * np.log10(spec[:, sel].sum(1) + 1e-30)
        floor = np.median(power)
        idx = np.where(power > floor + 6)[0]
        if not len(idx):
            continue
        groups = np.split(idx, np.where(np.diff(idx) > 3)[0] + 1)
        # Keep bursts within 30 % of the expected frame length.
        good = [g for g in groups
                if 0.7 * FRAME_SEC <= len(g) * dt <= 1.3 * FRAME_SEC]
        if len(good) >= 3:
            best.append((len(good), lo + width / 2, good, power.max() - floor))

    if not best:
        print('  NO Si4463 BURSTS ANYWHERE in the recorded band.')
        print('  The transmitter was not on air, or was outside the %.1f MHz '
              'this capture covers.' % (fs / 1e6))
        loud = _loudest_burst(spec, freq, dt)
        if loud:
            print('  (The strongest burst-like signal here is %+0.0f Hz off '
                  'centre with %.0f ms bursts -- not a 63-byte frame, so it is '
                  'something else on the band.)' % loud)
        return

    best.sort(reverse=True)
    count, centre, groups, snr = best[0]
    starts = np.array([g[0] * dt for g in groups])
    lengths = np.array([len(g) * dt for g in groups]) * 1e3
    spacing = np.diff(starts)

    # Refine the centre from the burst spectrum itself. Two things matter
    # here. Look wider than the detection window, or a window that straddles
    # the signal drags the centroid toward its own middle; and subtract the
    # noise measured between bursts, or the centroid is pulled toward the
    # centre of whatever span is being averaged rather than toward the signal.
    rows = np.concatenate(groups)
    quiet = np.setdiff1d(np.arange(spec.shape[0]), rows)
    sel = (freq >= centre - width) & (freq < centre + width)
    prof = spec[rows][:, sel].mean(0)
    if len(quiet):
        prof = prof - spec[quiet][:, sel].mean(0)
    prof = np.clip(prof, 0, None)
    if prof.sum() > 0:
        centre = float(np.sum(freq[sel] * prof) / prof.sum())

    print('  FOUND: %d bursts, %.1f-%.1f ms long (expect %.1f), %.1f dB over noise'
          % (count, lengths.min(), lengths.max(), FRAME_SEC * 1e3, snr))
    if len(spacing):
        print('         spacing %.2f-%.2f s (firmware sends one per second)'
              % (spacing.min(), spacing.max()))
    print('         centre offset %+0.0f Hz  ->  set center_offset = %d'
          % (centre, int(round(centre / 100.0)) * 100))


def _loudest_burst(spec, freq, dt):
    """Describe whatever else is on the band, for context when nothing matched."""
    keep = np.abs(freq) > 25e3
    sk = spec[:, keep]
    floor = np.median(sk, axis=0)
    ratio = sk.max(axis=0) / (floor + 1e-30)
    k = int(np.argmax(ratio))
    # Measure the burst length over a whole channel around that bin, not the
    # single bin: one bin of a frequency-modulated signal goes in and out of
    # the passband as the signal moves, which chops a long burst into slivers.
    hot_f = freq[keep][k]
    sel = (freq >= hot_f - 25e3) & (freq < hot_f + 25e3)
    power = 10 * np.log10(spec[:, sel].sum(1) + 1e-30)
    idx = np.where(power > np.median(power) + 6)[0]
    if not len(idx):
        return None
    groups = np.split(idx, np.where(np.diff(idx) > 3)[0] + 1)
    med_len = np.median([len(g) * dt for g in groups]) * 1e3
    return hot_f, med_len


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    for arg in sys.argv[1:]:
        check(arg)
