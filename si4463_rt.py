#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# Title: Si4463 Real-Time Packet Decoder (Raspberry Pi)
# Author: Neeraj
# Description: Live RTL-SDR decoder for the Si4463 sensor link, no GNU Radio
"""
The live decoder, with GNU Radio taken out of it.

Same chain, same decoder, same output as the Windows build; what changed is
that the two GNU Radio blocks in the middle are now numpy, and the samples come
from librtlsdr through ctypes instead of from gr-osmosdr:

    RTL-SDR Blog V3  ->  librtlsdr (async)  ->  si4463_rtlsdr.to_complex
                     ->  si4463_dsp.FreqXlatingDecimator
                     ->  si4463_core.Si4463Decoder  ->  stdout + CSV

si4463_core.py is untouched from the Windows tree - it was already numpy-only -
so a packet that decodes here decodes identically there, and si4463_selftest.py
is the same regression suite it always was.

Tuning. The LO is parked --lo-offset above the signal and the translating
filter shifts back down by the same amount, which puts the dongle's DC spike
and its 1/f noise 100 kHz away from the packet instead of underneath it. It
also makes tuner error harmless: the passband reaches 60 kHz, so tens of kHz of
LO error still lands inside it, and the decoder removes what is left burst by
burst. The RTL-SDR Blog V3's 1 ppm TCXO means --ppm 0 is genuinely right at
433 MHz, where 1 ppm is 435 Hz; a generic dongle's 30-60 ppm would be tens of
kHz and would still work, which is the point of the offset design.

Sample rate. The default is 1.2 MS/s rather than the 2.4 MS/s the Windows build
used. The signal is 45 kHz wide and the decoder wants 200 kS/s, so 2.4 MS/s
only ever bought a longer filter: the tap count scales with fs/transition, so
halving the input rate halves the work for a signal that was never near
Nyquist. On a Pi 5 that is the difference between a comfortable margin and a
needless one. 2.4e6 still works if something else wants it.

Run it:

    ./si4463_rt.py                       # live, logs to logs/packets_*.csv
    ./si4463_rt.py --wav capture.wav     # replay a baseband recording
    ./si4463_rt.py --list-devices
"""

import argparse
import signal
import sys
import time

import si4463_csv
import si4463_dsp
import si4463_rtlsdr
from si4463_core import Si4463Decoder

# Passband reaches 60 kHz so a signal displaced by tens of kHz of LO error
# still survives; the 45 kHz-wide 2GFSK burst itself only needs +/-25 kHz.
# These are the bench flowgraph's numbers and are not tuning knobs - the
# decoder's burst detection was characterised against this passband.
CUTOFF_HZ = 60e3
TRANSITION_HZ = 25e3

# 20 samples per symbol at 10 kbps: enough for the quarter-sample timing search
# without making the decoder do needless work.
TARGET_RATE = 200e3


class Chain(object):
    """Source -> translating decimator -> decoder, and the load bookkeeping.

    The load figure is the point of having this class rather than a loop. On a
    Pi it is the one number that says whether the decode is real-time, and it
    cannot be inferred from the packet log: a decoder that is falling behind
    drops input, and dropped input looks exactly like a quiet band. Processing
    time is measured against signal time, so 1.0 means the Pi is exactly
    keeping up and anything approaching it is a warning.
    """

    def __init__(self, src, args):
        self.src = src
        self.rate = src.actual_rate

        self.decim = max(1, int(round(self.rate / TARGET_RATE)))
        self.out_rate = self.rate / self.decim
        taps = si4463_dsp.lowpass_taps(self.rate, CUTOFF_HZ, TRANSITION_HZ)
        self.taps = taps

        shift = args.wav_offset if args.wav else -args.lo_offset
        self.xlate = si4463_dsp.FreqXlatingDecimator(
            taps, self.decim, shift, self.rate)

        self.csv = si4463_csv.PacketCsv(args.csv) if args.csv else None
        self.dec = Si4463Decoder(
            fs=self.out_rate, symbol_rate=args.baud, deviation=args.deviation,
            packet_len_bytes=args.packet_len, threshold_db=args.threshold,
            mode='iq', heartbeat_s=args.heartbeat, on_packet=self.csv)

        self.samples = 0
        self.proc_s = 0.0
        self.status_s = float(args.status)
        self._last_status = time.time()
        self._warned_load = False

        print('Chain: %.3f MS/s -> decimate %d -> %.1f kS/s '
              '(%.1f samples/symbol), shift %+.1f kHz, %d filter taps.'
              % (self.rate / 1e6, self.decim, self.out_rate / 1e3,
                 self.out_rate / args.baud, shift / 1e3, len(taps)))

    def run(self):
        self.dec.banner()
        if self.csv is not None:
            print('Logging packets to %s' % self.csv.path)
        for block in self.src.blocks():
            if block.size == 0:
                continue
            t0 = time.perf_counter()
            self.dec.feed(self.xlate(block))
            self.proc_s += time.perf_counter() - t0
            self.samples += block.size
            self._maybe_status()

    def load(self):
        signal_s = self.samples / self.rate if self.samples else 0.0
        return (self.proc_s / signal_s) if signal_s > 0.0 else 0.0

    def _maybe_status(self):
        now = time.time()
        load = self.load()

        # A load above 0.8 is said once, when it is first seen, rather than
        # every status line: it is a property of the machine and the settings,
        # not an event, and repeating it would bury the packet log.
        if load > 0.8 and not self._warned_load:
            self._warned_load = True
            print('!! DSP load %.0f%% of real time. This Pi is close to not '
                  'keeping up;' % (load * 100))
            print('   lower --rate (try 250e3 with --lo-offset 50e3) or raise '
                  '--threshold.')

        if self.status_s <= 0.0 or (now - self._last_status) < self.status_s:
            return
        self._last_status = now
        extra = ''
        if self.src.overruns:
            extra = ', %d DROPPED buffer(s)' % self.src.overruns
        print('   [%s  load %.0f%% of real time, %d buffers%s]'
              % (time.strftime('%H:%M:%S'), load * 100,
                 self.src.buffers_seen, extra))

    def close(self):
        self.dec.close()
        load = self.load()
        print('\nProcessed %.1f s of signal in %.1f s of CPU (load %.0f%%).'
              % (self.samples / self.rate if self.samples else 0.0,
                 self.proc_s, load * 100))
        if self.src.overruns:
            print('!! %d buffer(s) were dropped because the decoder could not '
                  'keep up.' % self.src.overruns)
            print('   Packets in those gaps were never seen. Lower --rate or '
                  'raise --threshold.')
        if self.csv is not None:
            print('Wrote %d row(s) to %s' % (self.csv.nrows, self.csv.path))
            self.csv.close()


def list_devices():
    if not si4463_rtlsdr.available():
        print('librtlsdr could not be loaded. Install it with:')
        print('    sudo apt install librtlsdr0 rtl-sdr')
        return 1
    names = si4463_rtlsdr.device_names()
    if not names:
        print('No RTL-SDR devices found.')
        print('If one is plugged in, the DVB-T driver has probably claimed it:')
        print('    lsmod | grep dvb_usb_rtl28xxu')
        print('Run ./install.sh to blacklist it, then replug the dongle.')
        return 1
    print('RTL-SDR devices:')
    for i, name in enumerate(names):
        print('  %d: %s' % (i, name))
    return 0


def build_source(args):
    if args.wav:
        return si4463_rtlsdr.WavSource(args.wav)

    lo_freq = args.freq + args.lo_offset
    common = dict(freq_hz=lo_freq, rate_hz=args.rate, gain_db=args.gain,
                  agc=args.agc, ppm=int(round(args.ppm)),
                  device_index=args.device_index, bias_tee=args.bias_tee)

    if args.pipe or not si4463_rtlsdr.available():
        if not args.pipe:
            print('!! librtlsdr not loadable; falling back to the rtl_sdr '
                  'command.')
        src = si4463_rtlsdr.RtlSdrPipe(**common)
        if src.gain_substituted:
            print('!! rtl_sdr reads "-g 0" as "enable AGC", so a manual 0 dB '
                  'cannot be asked')
            print('   for through the pipe. Using %.1f dB, the next gain step '
                  'up, instead.' % src.actual_gain_db)
            print('   Use the default librtlsdr path for a true 0 dB.')
        if src._bias_tee_requested:
            print('!! rtl_sdr has no bias-tee option. Run "rtl_biast -b 1" '
                  'separately if you need it.')
        src.start()
        print('RTL-SDR (via rtl_sdr): LO %.6f MHz, %.3f MS/s, gain %s.'
              % (lo_freq / 1e6, args.rate / 1e6,
                 'AGC' if args.agc else '%.1f dB' % src.actual_gain_db))
        return src

    src = si4463_rtlsdr.RtlSdrSource(**common)

    if abs(src.actual_rate - args.rate) > 1.0:
        print('!! Asked for %.6f MS/s; the dongle is running %.6f MS/s.'
              % (args.rate / 1e6, src.actual_rate / 1e6))
        print('   The RTL2832 only supports 225.001-300.000 kS/s and '
              '0.900001-3.2 MS/s.')

    gain_txt = 'AGC' if args.agc else '%.1f dB' % src.actual_gain_db
    if not args.agc and src.gain_table and \
            abs(src.actual_gain_db - args.gain) > 0.05:
        gain_txt += ' (asked %.1f, nearest step in the tuner table)' % args.gain

    print('RTL-SDR: LO %.6f MHz (signal %.6f MHz %+.0f kHz), %.3f MS/s, '
          'gain %s, %+.1f ppm.'
          % (src.actual_freq / 1e6, args.freq / 1e6, args.lo_offset / 1e3,
             src.actual_rate / 1e6, gain_txt, args.ppm))
    if args.bias_tee:
        print('Bias tee: %s' % ('ON (4.5 V on the coax)' if src.bias_tee_ok
                                else 'REQUESTED but this librtlsdr has no '
                                     'set_bias_tee; use rtl_biast'))
    src.start()
    return src


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='si4463_rt',
        description='Real-time Si4463 sensor-packet decoder for RTL-SDR on '
                    'Raspberry Pi. Needs numpy and librtlsdr; no GNU Radio.')
    p.add_argument('-f', '--freq', type=float, default=435.000e6,
                   help='transmitter frequency in Hz (default: 435.000e6, the '
                        'Si4463 at FREQ_CONTROL_INTE=57, FRAC=0x080000, '
                        'outdiv 8)')
    p.add_argument('-r', '--rate', type=float, default=1.2e6,
                   help='SDR sample rate in Hz (default: 1.2e6). The RTL2832 '
                        'supports 225001-300000 and 900001-3200000 only.')
    p.add_argument('--lo-offset', type=float, default=100e3,
                   help='how far above the signal to park the LO, in Hz, to '
                        'keep the DC spike off the packet (default: 100e3)')
    p.add_argument('--ppm', type=float, default=0.0,
                   help='tuner frequency correction in ppm (default: 0, which '
                        'is right for the V3 and its 1 ppm TCXO). The filter '
                        'tolerates tens of kHz of error either way.')
    # Gain defaults to zero for the reason the Windows build did: over the
    # ranges this link is used at, the dongle sees far more signal than it can
    # take. +20 dBm out and about 31 dB of free-space loss at 2 m puts roughly
    # -11 dBm on the antenna, and an RTL-SDR front end clips near -10 dBm with
    # no gain at all. Anything on top of that is spent driving the tuner into
    # compression, which raises the noise floor and produces intermodulation -
    # and from the packet log that is indistinguishable from a weak signal.
    # Raise it only until packets are solid, watching the peak level in the
    # heartbeat line: it should sit near 0.5, never pinned at 1.0.
    p.add_argument('-g', '--gain', type=float, default=0.0,
                   help='tuner gain in dB, snapped to the nearest step the '
                        'R820T2 offers (default: 0, see the note in the '
                        'source)')
    p.add_argument('--agc', action='store_true',
                   help='use the tuner AGC instead of a fixed gain; not '
                        'recommended for a 5%% duty cycle signal, because the '
                        'loop hunts during the gap and is still recovering '
                        'when the next preamble arrives')
    p.add_argument('--rtl-agc', action='store_true',
                   help="also enable the RTL2832's digital AGC (default off)")
    p.add_argument('--bias-tee', action='store_true',
                   help='switch on the RTL-SDR Blog V3 bias tee (4.5 V up the '
                        'coax). Off by default, and set explicitly either way '
                        'because the dongle remembers it between sessions.')
    p.add_argument('-d', '--device-index', type=int, default=0,
                   help='which RTL-SDR to use (default: 0)')
    p.add_argument('--list-devices', action='store_true',
                   help='list attached RTL-SDRs and exit')
    p.add_argument('--pipe', action='store_true',
                   help='read from the rtl_sdr command instead of librtlsdr. '
                        'Cannot express a manual 0 dB gain -- see '
                        'si4463_rtlsdr.py.')
    p.add_argument('-w', '--wav', default=None,
                   help='replay an SDR++ baseband WAV instead of using the SDR')
    p.add_argument('--wav-offset', type=float, default=-12600.0,
                   help='frequency shift for --wav mode in Hz (default: '
                        '-12600, the measured offset of the 26-08-2026 '
                        'recording)')
    p.add_argument('-b', '--baud', type=float, default=10000.0,
                   help='symbol rate (default: 10000)')
    p.add_argument('--deviation', type=float, default=20000.0,
                   help='FSK deviation in Hz (default: 20000)')
    p.add_argument('--packet-len', type=int, default=45,
                   help='payload length in bytes (default: 45)')
    p.add_argument('-t', '--threshold', type=float, default=3.0,
                   help='burst detection threshold above the noise floor, in '
                        'dB (default: 3.0)')
    p.add_argument('--heartbeat', type=float, default=30.0,
                   help='seconds between "still listening" lines, 0 to disable '
                        '(default: 30)')
    p.add_argument('--status', type=float, default=60.0,
                   help='seconds between DSP load/drop lines, 0 to disable '
                        '(default: 60)')
    p.add_argument('--csv', default=None,
                   help='write the packet log here instead of the default '
                        'logs/packets_<date>_<time>.csv beside this script. '
                        'One row per packet, flushed as it arrives. An '
                        'existing file is appended to, not overwritten.')
    p.add_argument('--no-csv', action='store_true',
                   help='decode to the screen only and write no log file')
    args = p.parse_args(argv)

    if args.list_devices:
        return list_devices()

    # Logging is on unless it is turned off. It used to be off unless it was
    # turned on, which meant every run started without a flag decoded
    # perfectly and left nothing behind. Packets that were never written down
    # cannot be recovered afterwards, so the safe default keeps them.
    if args.no_csv:
        args.csv = None
    elif args.csv is None:
        args.csv = si4463_csv.default_log_path()

    # The passband has to fit below Nyquist once the LO has been offset, or the
    # filter is selecting a band that is partly folded. This is a real trap at
    # the low sample rates a Pi might want: at 250 kS/s, Nyquist is 125 kHz and
    # the default 100 kHz offset leaves only 25 kHz for a 60 kHz passband.
    if not args.wav:
        nyquist = args.rate / 2.0
        if args.lo_offset + CUTOFF_HZ > nyquist:
            room = max(0.0, nyquist - CUTOFF_HZ)
            sys.stderr.write(
                'error: --lo-offset %.0f kHz plus the %.0f kHz passband '
                'exceeds Nyquist (%.0f kHz)\n'
                '       at --rate %.3f MS/s, so the selected band would be '
                'partly aliased.\n'
                '       Use --lo-offset up to %.0f kHz at this rate, or raise '
                '--rate.\n'
                % (args.lo_offset / 1e3, CUTOFF_HZ / 1e3, nyquist / 1e3,
                   args.rate / 1e6, room / 1e3))
            return 2

    try:
        src = build_source(args)
    except si4463_rtlsdr.RtlSdrError as exc:
        sys.stderr.write('%s\n' % exc)
        return 1

    chain = Chain(src, args)

    stopping = {'done': False}

    def sig_handler(_sig=None, _frame=None):
        if stopping['done']:
            return
        stopping['done'] = True
        src.stop()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        chain.run()
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
        chain.close()
        src.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
