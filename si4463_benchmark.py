#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
"""
Does this Pi keep up? Measure the GNU Radio chain against the clock.

The one question a port to a small machine has to answer, and the one the
packet log cannot answer for you: a decoder that falls behind drops input, and
dropped input is indistinguishable in the log from a band that was quiet. So
measure it directly, before trusting a deployment, by running the identical
flowgraph the live path uses - freq_xlating_fir_filter_ccf with the same
firdes taps, into the same decoder sink - on synthetic samples as fast as the
machine allows, and comparing the time taken against the signal time.

Two figures per rate:

  cpu   process CPU seconds (every GNU Radio thread) per second of signal.
        0.25 means a quarter of one core. This is the one to keep under about
        0.5: the live path also has to absorb USB interrupts, CSV flushes and
        whatever else the Pi is doing.
  wall  elapsed seconds per second of signal with nothing throttling the
        graph. GNU Radio runs each block on its own thread, so this can be
        lower than cpu on a multi-core Pi; above 1.0 the chain cannot run in
        real time at all.

The source is analog.fastnoise_source_c, a precomputed pool of Gaussian noise
that costs almost nothing to emit, at a level below the burst threshold. Burst
detection is the cheap part and decoding a packet happens 5% of the time; what
costs is the filter, which runs on every sample whether or not anything is
there, and noise exercises exactly that always-on path. The one live cost not
included is osmosdr's byte-to-complex conversion, which is C++ and small.

    ./si4463_benchmark.py                 # the default 1.2 MS/s chain
    ./si4463_benchmark.py --rate 2.4e6
    ./si4463_benchmark.py --rate 250e3 --lo-offset 50e3
    ./si4463_benchmark.py --all           # sweep the sensible rates
"""

import argparse
import platform
import sys
import time

from gnuradio import analog, blocks, gr

from si4463_gr_block import Si4463Sink
from si4463_rt import front_end


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


def run_one(rate, lo_offset, seconds):
    tb = gr.top_block('si4463 benchmark')
    src = analog.fastnoise_source_c(analog.GR_GAUSSIAN, 0.02, 1234, 1 << 16)
    head = blocks.head(gr.sizeof_gr_complex, int(rate * seconds))
    xlate, decim, out_rate, ntaps = front_end(rate, -lo_offset)
    sink = Si4463Sink(decim_rate=out_rate, heartbeat_s=0.0, quiet=True)
    tb.connect(src, head, xlate, sink)

    cpu0 = time.process_time()
    t0 = time.perf_counter()
    tb.run()
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0

    n, _, _ = sink.snapshot()
    signal_s = n / out_rate
    return dict(rate=rate, decim=decim, out_rate=out_rate, ntaps=ntaps,
                signal_s=signal_s,
                cpu=cpu / signal_s if signal_s else float('inf'),
                wall=wall / signal_s if signal_s else float('inf'),
                sps=out_rate / 10000.0)


def report(r):
    verdict = ('WILL NOT KEEP UP' if r['wall'] >= 0.8 else
               'comfortable' if r['cpu'] < 0.5 else
               'tight' if r['cpu'] < 0.8 else
               'WILL NOT KEEP UP')
    print('  %7.3f MS/s  decim %2d -> %6.1f kS/s  %4d taps  %4.1f sps   '
          'cpu %5.1f%%  wall %5.1f%%   %s'
          % (r['rate'] / 1e6, r['decim'], r['out_rate'] / 1e3, r['ntaps'],
             r['sps'], r['cpu'] * 100, r['wall'] * 100, verdict))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='si4463_benchmark',
        description='Measure whether this machine can run the GNU Radio '
                    'decoder chain in real time.')
    p.add_argument('-r', '--rate', type=float, default=1.2e6,
                   help='sample rate to test in Hz (default: 1.2e6)')
    p.add_argument('--lo-offset', type=float, default=100e3,
                   help='LO offset in Hz (default: 100e3)')
    p.add_argument('-s', '--seconds', type=float, default=10.0,
                   help='seconds of synthetic signal per rate (default: 10)')
    p.add_argument('--all', action='store_true',
                   help='sweep the sample rates the RTL2832 supports that make '
                        'sense here')
    args = p.parse_args(argv)

    print('si4463 GNU Radio chain benchmark')
    print('  machine   : %s' % cpu_name())
    print('  python    : %s, GNU Radio %s' % (platform.python_version(),
                                              gr.version()))
    print('  signal    : %.0f s of noise per rate\n' % args.seconds)

    if args.all:
        # 250 kS/s needs a smaller LO offset to keep the 60 kHz passband below
        # Nyquist; the live script refuses the combination outright, so the
        # sweep uses a legal one rather than measuring something unusable.
        plan = [(250e3, 50e3), (1.024e6, 100e3), (1.2e6, 100e3),
                (2.048e6, 100e3), (2.4e6, 100e3)]
    else:
        plan = [(args.rate, args.lo_offset)]

    results = []
    for rate, lo in plan:
        r = run_one(rate, lo, args.seconds)
        report(r)
        results.append(r)

    best = min(results, key=lambda r: r['cpu'])
    print('\nLowest load: %.3f MS/s at %.1f%% of one core.'
          % (best['rate'] / 1e6, best['cpu'] * 100))
    print('Leave margin: the live path also absorbs USB interrupts, CSV')
    print('flushes and whatever else is running.')
    if best['cpu'] >= 0.8 or best['wall'] >= 0.8:
        print('\n!! Nothing tested here has usable margin on this machine.')
        print('   Lower the rate further, or raise --threshold so fewer')
        print('   candidate bursts are demodulated.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
