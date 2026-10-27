#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Si4463 Bench Packet Decoder
# Author: Neeraj
# Description: Si4463 bench-test packet decoder for SDR++ WAV recordings
# GNU Radio version: 3.10.12.0

from gnuradio import blocks
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
import argparse
import sys
import signal
import wave
import si4463_bench_decode_epy_block_0 as epy_block_0  # embedded python block
import threading


def wav_sample_rate(path):
    """Read the sample rate out of the WAV header.

    SDR++ writes baseband recordings at whatever rate the SDR was running at
    -- 2.4 MS/s for an RTL-SDR -- and puts the *centre frequency*, not the
    sample rate, in the filename. Hard-coding a rate here silently rescales
    the decimation, the filter cutoffs and the samples-per-symbol, so read it
    from the file instead.
    """
    with wave.open(path) as w:
        return w.getframerate()


# The recording decoded when none is named on the command line. Every capture
# taken so far predates the RTC timestamp, so anything replayed from here comes
# out as the legacy layout -- correctly, because that is what those bytes are.
# Pass a path to decode a newer one:
#
#     python si4463_bench_decode.py <recording.wav> [--center-offset HZ]
#
DEFAULT_WAV = ('C:\\Users\\np841\\Downloads\\'
               'baseband_435001000Hz_18-21-06_26-08-2026.wav')


class si4463_bench_decode(gr.top_block):

    def __init__(self, wav_file=DEFAULT_WAV, center_offset=-12600):
        gr.top_block.__init__(self, "Si4463 Bench Packet Decoder", catch_exceptions=True)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.wav_file = wav_file
        print('Decoding %s' % wav_file)
        self.samp_rate = samp_rate = wav_sample_rate(wav_file)
        self.decim = decim = max(1, int(samp_rate / 200000))
        self.symbol_rate = symbol_rate = 10000
        self.deviation = deviation = 20000
        self.decim_rate = decim_rate = samp_rate / decim
        # Offset of the transmitter from the recording's centre frequency, in
        # Hz. The filename says 435.001 MHz and the Si4463 is on 435.000 MHz
        # (FREQ_CONTROL_INTE=57, FRAC=0x080000, outdiv 8), so nominally -1000
        # Hz -- but the RTL-SDR's LO is about 11.6 kHz low, which is ordinary
        # for an uncompensated tuner. The measured centre of the burst is
        # -12.6 kHz. The low-pass below is deliberately wide enough that this
        # value only has to be right to within a few tens of kHz; the decoder
        # block removes what is left burst by burst.
        self.center_offset = center_offset

        ##################################################
        # Blocks
        ##################################################

        # Passband reaches 60 kHz so a signal displaced by tens of kHz of LO
        # error still survives; the 45 kHz-wide 2GFSK burst itself only needs
        # +/-25 kHz.
        self.freq_xlating_fir_filter_xxx_0 = filter.freq_xlating_fir_filter_ccf(decim, firdes.low_pass(1, samp_rate, 60e3, 25e3), center_offset, samp_rate)
        self.epy_block_0 = epy_block_0.blk(decim_rate=decim_rate, symbol_rate=symbol_rate, deviation=deviation, packet_len_bytes=45, burst_threshold_db=6.0)
        self.blocks_wavfile_source_0 = blocks.wavfile_source(wav_file, False)
        self.blocks_float_to_complex_0 = blocks.float_to_complex(1)

        ##################################################
        # Connections
        ##################################################
        self.connect((self.blocks_float_to_complex_0, 0), (self.freq_xlating_fir_filter_xxx_0, 0))
        self.connect((self.blocks_wavfile_source_0, 1), (self.blocks_float_to_complex_0, 1))
        self.connect((self.blocks_wavfile_source_0, 0), (self.blocks_float_to_complex_0, 0))
        self.connect((self.freq_xlating_fir_filter_xxx_0, 0), (self.epy_block_0, 0))


    def get_wav_file(self):
        return self.wav_file

    def set_wav_file(self, wav_file):
        self.wav_file = wav_file

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.set_decim(max(1, int(self.samp_rate / 200000)))
        self.set_decim_rate(self.samp_rate / self.decim)
        self.freq_xlating_fir_filter_xxx_0.set_taps(firdes.low_pass(1, self.samp_rate, 60e3, 25e3))

    def get_decim(self):
        return self.decim

    def set_decim(self, decim):
        self.decim = decim
        self.set_decim_rate(self.samp_rate / self.decim)

    def get_symbol_rate(self):
        return self.symbol_rate

    def set_symbol_rate(self, symbol_rate):
        self.symbol_rate = symbol_rate

    def get_deviation(self):
        return self.deviation

    def set_deviation(self, deviation):
        self.deviation = deviation

    def get_decim_rate(self):
        return self.decim_rate

    def set_decim_rate(self, decim_rate):
        self.decim_rate = decim_rate

    def get_center_offset(self):
        return self.center_offset

    def set_center_offset(self, center_offset):
        self.center_offset = center_offset
        self.freq_xlating_fir_filter_xxx_0.set_center_freq(self.center_offset)




def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description='Decode Si4463 packets out of an SDR++ baseband WAV.')
    ap.add_argument('wav', nargs='?', default=DEFAULT_WAV,
                    help='recording to decode (default: the 26-08-2026 bench '
                         'capture, which predates the RTC timestamp and so '
                         'decodes as the legacy layout)')
    ap.add_argument('--center-offset', type=float, default=-12600,
                    metavar='HZ',
                    help='transmitter offset from the recording centre '
                         'frequency (default: -12600, the measured LO error '
                         'of the RTL-SDR used for the bench captures)')
    return ap.parse_args(argv)


def main(top_block_cls=si4463_bench_decode, options=None):
    if options is None:
        options = parse_args()
    tb = top_block_cls(wav_file=options.wav,
                       center_offset=options.center_offset)

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()
    tb.flowgraph_started.set()

    # The WAV source is not set to repeat, so the flowgraph ends on its own at
    # the end of the file and the decoder block prints from stop(). Waiting
    # here instead of prompting means the script can be run non-interactively.
    tb.wait()


if __name__ == '__main__':
    main()
