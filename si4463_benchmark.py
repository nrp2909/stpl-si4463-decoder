#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
"""
Does this Pi keep up? Measure the chain against the clock.

The one question a port to a small machine has to answer, and the one the
packet log cannot answer for you: a decoder that falls behind drops input, and
dropped input is indistinguishable in the log from a band that was quiet. So
measure it directly, before trusting a deployment, by pushing synthetic samples
through the identical chain the live path uses - byte conversion, translating
decimator, decoder - and comparing CPU time against signal time.

The number printed is the real-time factor: processing seconds per signal
second. 0.25 means a quarter of one core, and three quarters of margin. Above
about 0.8 the margin is gone, because the figure measured here is a steady
average and the live path also has to absorb a burst arriving, the CSV being
flushed and whatever else the Pi is doing.

The samples are noise, not packets, deliberately. Burst detection is the cheap
part and decoding a packet happens 5% of the time; what costs is the filter,
which runs on every sample whether or not anything is there. Noise at a level
below the burst threshold exercises exactly the always-on path. --with-packets
adds synthesised bursts if you want the occasional decode included.

    ./si4463_benchmark.py                 # the default 1.2 MS/s chain
    ./si4463_benchmark.py --rate 2.4e6    # what the Windows build used
    ./si4463_benchmark.py --rate 250e3 --lo-offset 50e3
    ./si4463_benchmark.py --all           # sweep the sensible rates
"""

import argparse
import platform
import sys
import time

import numpy as np

import si4463_dsp
import si4463_rtlsdr
from si4463_core import Si4463Decoder

CUTOFF_HZ = 60e3
TRANSITION_HZ = 25e3
TARGET_RATE = 200e3


def cpu_name():
    """Best effort description of the machine, so a number has provenance."""
    try:
        with open('/proc/cpuinfo') as f:
            text = f.read()
        model = None
        for line in text.splitlines():
            if line.startswith('model name') or line.startswith('Model'):
                model = line.split(':', 1)[1].strip()
                break
        hw = None
        for line in text.splitlines():
            if line.startswith('Hardware') or line.startswith('Model'):
                hw = line.split(':', 1)[1].strip()
        return model or hw or platform.processor() or platform.machine()
    except OSError:
        return platform.processor() or platform.machine()


def synth_noise_bytes(n_samples, rng, level=0.02):
    """Interleaved uint8 IQ noise, as librtlsdr would deliver it.

    Produced as bytes rather than as complex floats so that to_complex() is
    inside the measurement: at 2.4 MS/s that conversion alone touches 4.8 MB/s
    and is not free on a Pi.
    """
    iq = rng.standard_normal(2 * n_samples) * (level * 128.0)
    return np.clip(iq + 127.4, 0, 255).astype(np.uint8)


def synth_packet_bytes(rate, rng, baud=10000.0, deviation=20000.0,
                       offset_hz=100e3, level=0.25):
    """One 63-byte 2GFSK burst at the LO offset, as uint8 IQ.

    Only roughly shaped - a Gaussian-filtered NRZ stream at the right rate and
    deviation. It exists to make the decoder's packet path run, not to be
    demodulated correctly, so the bit content is random.
    """
    nbits = (63) * 8
    sps = rate / baud
    bits = rng.integers(0, 2, nbits) * 2 - 1
    sym = np.repeat(bits, int(round(sps)))
    # Mild Gaussian shaping, BT ~ 0.5.
    g = np.exp(-0.5 * (np.linspace(-2, 2, int(round(sps)))) ** 2)
    g /= g.sum()
    sym = np.convolve(sym.astype(np.float64), g, mode='same')
    phase = 2.0 * np.pi * deviation * np.cumsum(sym) / rate
    t = np.arange(len(phase)) / rate
    sig = level * np.exp(1j * (phase + 2.0 * np.pi * offset_hz * t))
    i = np.clip(sig.real * 128.0 + 127.4, 0, 255)
    q = np.clip(sig.imag * 128.0 + 127.4, 0, 255)
    out = np.empty(2 * len(sig), dtype=np.uint8)
    out[0::2] = i.astype(np.uint8)
    out[1::2] = q.astype(np.uint8)
    return out


def run_one(rate, lo_offset, seconds, with_packets, block_bytes,
            quiet_decoder=True):
    decim = max(1, int(round(rate / TARGET_RATE)))
    out_rate = rate / decim
    taps = si4463_dsp.lowpass_taps(rate, CUTOFF_HZ, TRANSITION_HZ)
    xlate = si4463_dsp.FreqXlatingDecimator(taps, decim, -lo_offset, rate)
    dec = Si4463Decoder(fs=out_rate, symbol_rate=10000.0, deviation=20000.0,
                        packet_len_bytes=45, threshold_db=3.0, mode='iq',
                        heartbeat_s=0.0, quiet=quiet_decoder)

    rng = np.random.default_rng(1234)
    samples_per_block = block_bytes // 2
    total_samples = int(rate * seconds)
    nblocks = max(1, total_samples // samples_per_block)

    # Pre-generate a few distinct noise blocks and cycle them. Generating noise
    # is slower than filtering it, so generating inside the timed loop would
    # measure numpy's RNG rather than the chain.
    pool = [synth_noise_bytes(samples_per_block, rng) for _ in range(4)]
    burst = synth_packet_bytes(rate, rng, offset_hz=lo_offset) \
        if with_packets else None

    # One pass outside the clock, so first-touch page faults and any lazy numpy
    # setup are not charged to the measurement.
    dec.feed(xlate(si4463_rtlsdr.to_complex(pool[0])))

    t0 = time.perf_counter()
    fed = 0
    for b in range(nblocks):
        raw = pool[b & 3]
        if burst is not None and (b % max(1, nblocks // 4)) == 1:
            raw = raw.copy()
            m = min(len(burst), len(raw))
            raw[:m] = burst[:m]
        dec.feed(xlate(si4463_rtlsdr.to_complex(raw)))
        fed += samples_per_block
    elapsed = time.perf_counter() - t0

    signal_s = fed / rate
    return dict(rate=rate, decim=decim, out_rate=out_rate, ntaps=len(taps),
                signal_s=signal_s, cpu_s=elapsed,
                factor=elapsed / signal_s if signal_s else float('inf'),
                sps=out_rate / 10000.0)


def report(r):
    verdict = ('comfortable' if r['factor'] < 0.5 else
               'tight' if r['factor'] < 0.8 else
               'WILL NOT KEEP UP')
    print('  %7.3f MS/s  decim %2d -> %6.1f kS/s  %4d taps  %4.1f sps   '
          '%5.1f%% of one core   %s'
          % (r['rate'] / 1e6, r['decim'], r['out_rate'] / 1e3, r['ntaps'],
             r['sps'], r['factor'] * 100, verdict))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='si4463_benchmark',
        description='Measure whether this machine can run the decoder in real '
                    'time.')
    p.add_argument('-r', '--rate', type=float, default=1.2e6,
                   help='sample rate to test in Hz (default: 1.2e6)')
    p.add_argument('--lo-offset', type=float, default=100e3,
                   help='LO offset in Hz (default: 100e3)')
    p.add_argument('-s', '--seconds', type=float, default=10.0,
                   help='seconds of synthetic signal per rate (default: 10)')
    p.add_argument('--with-packets', action='store_true',
                   help='include synthesised bursts, so the packet path runs '
                        'too (default: noise only, which is the always-on '
                        'cost)')
    p.add_argument('--block-bytes', type=int,
                   default=si4463_rtlsdr.DEFAULT_BUF_LEN,
                   help='bytes per block, matching the USB transfer size '
                        '(default: %d)' % si4463_rtlsdr.DEFAULT_BUF_LEN)
    p.add_argument('--all', action='store_true',
                   help='sweep the sample rates the RTL2832 supports that make '
                        'sense here')
    args = p.parse_args(argv)

    print('si4463 chain benchmark')
    print('  machine : %s' % cpu_name())
    print('  python  : %s, numpy %s' % (platform.python_version(),
                                        np.__version__))
    print('  signal  : %.0f s of %s per rate\n'
          % (args.seconds, 'noise + bursts' if args.with_packets else 'noise'))

    if args.all:
        # 250 kS/s needs a smaller LO offset to keep the 60 kHz passband below
        # Nyquist; the live script refuses the combination outright, so the
        # sweep uses a legal one rather than measuring something unusable.
        plan = [(250e3, 50e3), (1.024e6, 100e3), (1.2e6, 100e3),
                (2.048e6, 100e3), (2.4e6, 100e3)]
    else:
        plan = [(args.rate, args.lo_offset)]

    print('  %-7s     %-18s %-11s %-9s %-20s %s'
          % ('rate', 'decimation', 'taps', 'sps', 'load', 'verdict'))
    results = []
    for rate, lo in plan:
        r = run_one(rate, lo, args.seconds, args.with_packets,
                    args.block_bytes)
        report(r)
        results.append(r)

    best = min(results, key=lambda r: r['factor'])
    print('\nLowest load: %.3f MS/s at %.1f%% of one core.'
          % (best['rate'] / 1e6, best['factor'] * 100))
    print('A Pi 5 has four cores and this chain uses one, so the figure to')
    print('watch is per-core, not total. Leave margin: the live path also')
    print('absorbs USB interrupts, CSV flushes and whatever else is running.')
    if best['factor'] >= 0.8:
        print('\n!! Nothing tested here has usable margin on this machine.')
        print('   Lower the rate further, or raise --threshold so fewer')
        print('   candidate bursts are demodulated.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
