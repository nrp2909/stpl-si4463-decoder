#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
"""
GNU Radio sink block around Si4463Decoder, for the diagnostic flowgraph.

This is the GNU Radio half of the project, and it is deliberately the only
part of it. The headless decoder (si4463_rt.py) does not import this file and
does not need GNU Radio installed; this exists so that si4463_rpi.grc can show
a live spectrum and waterfall next to the packet output, which is a thing the
headless path cannot do and should not try to.

All the signal processing still lives in si4463_core.py, which knows nothing
about GNU Radio. This is only the plumbing that turns a stream of complex
samples into calls to feed(). Keeping that split is what lets the same decoder
sit behind the numpy chain, behind the soundcard TNC and behind this
flowgraph - so a fix to the decoder fixes all three, and a packet that decodes
in one decodes in all of them.

Why the block lives here rather than inside the .grc
----------------------------------------------------
GRC stores an Embedded Python Block's source as an escaped string inside the
flowgraph file, and regenerates `<flowgraph>_epy_block_0.py` from it. Code kept
that way cannot be imported, cannot be tested, and produces unreadable diffs.
So si4463_rpi.grc embeds only a short subclass that forwards to this module -
enough for GRC to introspect an __init__ and build its parameter dialog, with
the actual work here as ordinary source.

Self-test
---------
Run this file directly to push a baseband recording through GNU Radio with no
dongle and no display, which is how the flowgraph's chain was verified before
it ever saw an antenna:

    python3 si4463_gr_block.py test_current_layout.wav

It builds wavfile_source -> float_to_complex -> freq_xlating_fir_filter ->
this block, which is exactly si4463_rpi.grc with the osmosdr source and the Qt
sinks removed, and reports what it decoded.
"""

import numpy as np
from gnuradio import gr

from si4463_core import Si4463Decoder
from si4463_csv import PacketCsv

# The flowgraph's filter shape, kept here so the self-test below builds the
# same chain the .grc does rather than a lookalike. These are the bench
# flowgraph's numbers: the passband is deliberately wider than the 45 kHz burst
# so tens of kHz of dongle LO error still gets through.
CUTOFF_HZ = 60e3
TRANSITION_HZ = 25e3
TARGET_RATE = 200e3


class Si4463Sink(gr.sync_block):
    """Complex baseband in, decoded packets out to stdout and optionally CSV."""

    def __init__(self, decim_rate=200000, symbol_rate=10000, deviation=20000,
                 packet_len_bytes=45, burst_threshold_db=3.0,
                 heartbeat_s=30.0, csv_path=None):
        gr.sync_block.__init__(self, name='Si4463 Decoder',
                               in_sig=[np.complex64], out_sig=None)
        # The CSV log is owned here rather than by the flowgraph, so it is
        # opened and closed on the same start/stop that runs the decoder and
        # cannot be left holding a half-written file if the graph is torn down
        # early.
        self.csv = PacketCsv(csv_path) if csv_path else None
        self.dec = Si4463Decoder(fs=decim_rate, symbol_rate=symbol_rate,
                                 deviation=deviation,
                                 packet_len_bytes=packet_len_bytes,
                                 threshold_db=burst_threshold_db,
                                 mode='iq', heartbeat_s=heartbeat_s,
                                 on_packet=self.csv)

    def start(self):
        self.dec.banner()
        if self.csv is not None:
            print('Logging packets to %s' % self.csv.path)
        return True

    def work(self, input_items, output_items):
        self.dec.feed(input_items[0])
        return len(input_items[0])

    def stop(self):
        self.dec.close()
        if self.csv is not None:
            print('Wrote %d row(s) to %s' % (self.csv.nrows, self.csv.path))
            self.csv.close()
        return True


# GRC's convention is that an embedded block's class is called `blk`. The
# alias means the same name works whether this module is imported by the
# flowgraph's shim or used directly.
blk = Si4463Sink


def _self_test(wav_path, center_offset=-12600.0, csv_path=None):
    """Run a baseband WAV through the flowgraph's chain, headless.

    Deliberately no Qt and no osmosdr: this has to be runnable over SSH on a
    Pi with no display and no dongle attached, because "is GNU Radio itself
    working here" is a question worth being able to answer separately from "is
    the radio working here".
    """
    import wave
    from gnuradio import blocks
    from gnuradio import filter as gr_filter
    from gnuradio.filter import firdes

    with wave.open(wav_path) as w:
        if w.getnchannels() != 2:
            raise SystemExit(
                '%s has %d channel(s); a baseband recording needs 2 (I and Q).'
                % (wav_path, w.getnchannels()))
        samp_rate = float(w.getframerate())

    decim = max(1, int(round(samp_rate / TARGET_RATE)))
    decim_rate = samp_rate / decim
    taps = firdes.low_pass(1, samp_rate, CUTOFF_HZ, TRANSITION_HZ)

    print('GNU Radio %s' % gr.version())
    print('Chain: %s at %.3f MS/s -> decimate %d -> %.1f kS/s '
          '(%.1f samples/symbol), shift %+.1f kHz, %d taps.'
          % (wav_path, samp_rate / 1e6, decim, decim_rate / 1e3,
             decim_rate / 10000.0, center_offset / 1e3, len(taps)))

    tb = gr.top_block('Si4463 GNU Radio self-test', catch_exceptions=True)
    src = blocks.wavfile_source(wav_path, False)
    f2c = blocks.float_to_complex(1)
    xlate = gr_filter.freq_xlating_fir_filter_ccf(decim, taps, center_offset,
                                                 samp_rate)
    sink = Si4463Sink(decim_rate=decim_rate, burst_threshold_db=3.0,
                      heartbeat_s=0.0, csv_path=csv_path)

    tb.connect((src, 0), (f2c, 0))
    tb.connect((src, 1), (f2c, 1))
    tb.connect(f2c, xlate, sink)
    tb.start()
    tb.wait()
    return 0


if __name__ == '__main__':
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog='si4463_gr_block',
        description='Push a baseband WAV through the GNU Radio chain with no '
                    'dongle and no display, to check GNU Radio itself works.')
    p.add_argument('wav', nargs='?', default='test_current_layout.wav',
                   help='baseband WAV, I and Q as the two channels '
                        '(default: test_current_layout.wav)')
    p.add_argument('--offset', type=float, default=-12600.0,
                   help='frequency shift in Hz (default: -12600, the measured '
                        'offset of the bundled reference capture)')
    p.add_argument('--csv', default=None,
                   help='also write a packet CSV here (default: screen only)')
    a = p.parse_args()
    sys.exit(_self_test(a.wav, a.offset, a.csv))
