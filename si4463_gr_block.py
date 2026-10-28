#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
"""
GNU Radio sink block around Si4463Decoder.

This is the last block of every chain in the project: the headless live
decoder (si4463_rt.py), the benchmark (si4463_benchmark.py) and the diagnostic
flowgraph (si4463_rpi.grc) all end in it. Everything upstream of it is stock
GNU Radio - osmosdr.source, filter.freq_xlating_fir_filter_ccf with
firdes.low_pass taps - so the only project code on the sample path is this
block and the decoder it wraps.

All the packet logic lives in si4463_core.py, which knows nothing about GNU
Radio. This is only the plumbing that turns a stream of complex samples into
calls to feed(), plus the bookkeeping the live path needs to prove it is
keeping up: how many samples arrived, when the last one arrived, and how much
time the decoder spent on them.

Why the block lives here rather than inside the .grc
----------------------------------------------------
GRC stores an Embedded Python Block's source as an escaped string inside the
flowgraph file, and regenerates `<flowgraph>_epy_block_0.py` from it. Code kept
that way cannot be imported, cannot be tested, and produces unreadable diffs.
So si4463_rpi.grc embeds only a short subclass that forwards to this module -
enough for GRC to introspect an __init__ and build its parameter dialog, with
the actual work here as ordinary source.

Checking it
-----------
The replay path is the live path with a wavfile_source in place of the
osmosdr source, so the GNU Radio chain is checked with no dongle and no
display by:

    ./si4463_rt.py --wav test_current_layout.wav
"""

import threading
import time
import traceback

import numpy as np
from gnuradio import gr

from si4463_core import Si4463Decoder
from si4463_csv import PacketCsv

# The front-end filter shape, shared by every chain that feeds this block so
# that the live decoder, the benchmark and the flowgraph cannot drift apart.
# These are the bench flowgraph's numbers: the passband is deliberately wider
# than the 45 kHz burst so tens of kHz of dongle LO error still gets through,
# and the decoder's burst detection was characterised against it.
CUTOFF_HZ = 60e3
TRANSITION_HZ = 25e3

# 20 samples per symbol at 10 kbps: enough for the quarter-sample timing search
# without making the decoder do needless work.
TARGET_RATE = 200e3


class Si4463Sink(gr.sync_block):
    """Complex baseband in, decoded packets out to stdout and optionally CSV."""

    def __init__(self, decim_rate=200000, symbol_rate=10000, deviation=20000,
                 packet_len_bytes=45, burst_threshold_db=3.0,
                 heartbeat_s=30.0, csv_path=None, quiet=False):
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
                                 on_packet=self.csv, quiet=quiet)
        self.quiet = quiet

        # Read from the main thread while work() runs in GNU Radio's, so the
        # counters are updated under a lock and read as a consistent snapshot.
        self._lock = threading.Lock()
        self.nsamples = 0
        self.proc_s = 0.0
        self.last_sample_t = None
        self.errors = 0
        self._last_trace_t = 0.0
        self.stopped = False

    def start(self):
        if not self.quiet:
            self.dec.banner()
            if self.csv is not None:
                print('Logging packets to %s' % self.csv.path)
        return True

    def work(self, input_items, output_items):
        x = input_items[0]
        t0 = time.perf_counter()
        try:
            self.dec.feed(x)
        except Exception:
            # An exception escaping work() ends this block's thread, and with
            # it the whole receiver - silently, from the log's point of view,
            # because a stalled decoder prints nothing. One malformed burst
            # must not cost every packet after it, so the error is reported
            # (at most once a minute, so a persistent fault cannot flood the
            # journal) and the stream carries on.
            self.errors += 1
            now = time.time()
            if now - self._last_trace_t > 60.0:
                self._last_trace_t = now
                print('!! decoder error #%d, continuing:' % self.errors)
                traceback.print_exc()
        dt = time.perf_counter() - t0
        with self._lock:
            self.nsamples += len(x)
            self.proc_s += dt
            self.last_sample_t = time.monotonic()
        return len(x)

    def snapshot(self):
        """(samples seen, decoder seconds, monotonic time of last sample)."""
        with self._lock:
            return self.nsamples, self.proc_s, self.last_sample_t

    def stop(self):
        # GNU Radio can call stop() more than once on teardown paths; the log
        # must be closed exactly once.
        if self.stopped:
            return True
        self.stopped = True
        if not self.quiet:
            self.dec.close()
        if self.csv is not None:
            print('Wrote %d row(s) to %s' % (self.csv.nrows, self.csv.path))
            self.csv.close()
        return True


# GRC's convention is that an embedded block's class is called `blk`. The
# alias means the same name works whether this module is imported by the
# flowgraph's shim or used directly.
blk = Si4463Sink
