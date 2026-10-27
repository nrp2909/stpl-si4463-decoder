#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
si4463_tnc -- a Direwolf-shaped soundcard TNC for the Si4463 sensor link.

Direwolf takes audio from a receiver's discriminator and prints AX.25 frames.
This does the same job for the 2GFSK sensor packets: point a receiver at
435.000 MHz, send its demodulated audio into a virtual audio cable, and this
program prints each SensorPacket_t as it arrives.

    Si4463 --RF--> RTL-SDR --> NFM demod --> loopback audio --> si4463_tnc

The one thing that has to be right is that the audio is a *raw discriminator*
output. A 2GFSK signal spends long runs on one rail, so its information lives at
and near DC; anything that high-pass filters the audio -- de-emphasis, a DC
blocker, an audio AGC, a "voice" filter -- eats exactly the part that matters
and the packets stop framing. In SDR++ that means: NFM demodulator, bandwidth
wide enough for the +/-20 kHz deviation, de-emphasis None, squelch on, AGC off.
See --help and the accompanying notes for the full setup.

Deliberately depends on nothing but numpy and sounddevice, so PyInstaller can
freeze it into a single .exe the way Direwolf ships one. The GNU Radio flowgraph
in si4463_rt.py is the more sensitive receiver -- it works on complex baseband
and never gives up the DC content in the first place -- but it cannot be frozen
into a standalone binary.
"""

import argparse
import sys
import time
import wave

import numpy as np

from si4463_core import Si4463Decoder
from si4463_csv import PacketCsv, default_log_path

# Line separator for the multi-line guidance in _import_sounddevice().
SEP = chr(10)


def _import_sounddevice():
    """Import sounddevice, or explain what is missing.

    Kept lazy, and deliberately not needed by the --wav path: decoding a
    recording wants nothing but numpy, so a headless Pi with no audio stack can
    still do it. install.sh treats the audio packages as optional for exactly
    that reason, which makes this import failing an expected configuration
    rather than a broken install - so it should say what to add instead of
    putting an ImportError traceback in front of someone.

    Exception rather than ImportError is caught on purpose. The common failure
    on Debian is not that the module is absent but that it is present and
    PortAudio is not: python3-sounddevice loads its ctypes wrapper, finds no
    libportaudio, and raises OSError. Both cases need the same sentence.
    """
    try:
        import sounddevice as _sd
    except Exception as exc:
        raise SystemExit(SEP.join([
            'Live audio capture needs the sounddevice module and PortAudio,',
            'and one of them is missing.',
            '',
            '  Debian / Raspberry Pi OS:',
            '      sudo apt install python3-sounddevice libportaudio2',
            '  anywhere else:',
            '      pip install sounddevice',
            '',
            'Only live capture needs them. To decode a recording instead, use',
            '    python3 si4463_tnc.py --wav <file.wav>',
            'which needs only numpy. And on a Pi with the SDR attached',
            'directly, si4463_rt.py is the better path anyway -- it needs no',
            'audio stack at all.',
            '',
            '(the import failed with: %s)' % exc,
        ]))
    return _sd


def list_devices():
    sd = _import_sounddevice()
    print('Input devices (index: name -- channels @ default rate):')
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0:
            host = sd.query_hostapis(d['hostapi'])['name']
            print('  %2d: %-44s %d ch @ %6.0f Hz  [%s]'
                  % (i, d['name'].strip(), d['max_input_channels'],
                     d['default_samplerate'], host))
    print('\nPick the *input* half of your virtual cable -- for VB-CABLE B '
          'that is\n"CABLE-B Output (VB-Audio Cable B)", because what the cable '
          'outputs is\nwhat this program records.')


def run_audio(args):
    sd = _import_sounddevice()

    log = PacketCsv(args.csv) if args.csv else None
    dec = Si4463Decoder(fs=args.rate, symbol_rate=args.baud,
                        deviation=args.deviation,
                        packet_len_bytes=args.packet_len,
                        threshold_db=args.threshold, mode='audio',
                        squelch=args.squelch, heartbeat_s=args.heartbeat,
                        on_packet=log)
    dec.banner()
    if log is not None:
        print('Logging packets to %s' % log.path)
    dev = args.device
    if dev is not None and dev.lstrip('-').isdigit():
        dev = int(dev)
    info = sd.query_devices(dev, 'input')
    print('Input: %s @ %.0f Hz. Ctrl-C to stop.'
          % (info['name'].strip(), args.rate))

    # Roughly 20 ms of audio per callback: short enough that a packet is
    # reported almost as soon as it ends, long enough that the callback is not
    # the bottleneck. The callback itself only hands the block to the decoder;
    # a burst decode takes a few milliseconds and happens once a second or so,
    # which fits inside the block period with room to spare.
    block = max(256, int(args.rate * 0.02))

    def callback(indata, frames, t, status):
        if status:
            # Overflows mean the decoder or the machine fell behind, which
            # silently drops packets. Say so rather than hiding it.
            print('[audio] %s' % status, file=sys.stderr)
        dec.feed(indata[:, 0])

    try:
        with sd.InputStream(device=dev, channels=1, samplerate=args.rate,
                            blocksize=block, dtype='float32',
                            callback=callback):
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        print()
    finally:
        dec.close()
        if log is not None:
            print('Wrote %d row(s) to %s' % (log.nrows, log.path))
            log.close()


def run_wav(args):
    """Decode a WAV file of discriminator audio.

    Same code path as the live capture, so a file that decodes here will decode
    off the cable, and a regression can be checked without any hardware.
    """
    with wave.open(args.wav) as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype='<f4').astype(np.float32)
    else:
        raise SystemExit('unsupported WAV sample width: %d bytes' % width)
    if ch > 1:
        x = x.reshape(-1, ch)[:, 0]

    log = PacketCsv(args.csv) if args.csv else None
    dec = Si4463Decoder(fs=rate, symbol_rate=args.baud,
                        deviation=args.deviation,
                        packet_len_bytes=args.packet_len,
                        threshold_db=args.threshold, mode='audio',
                        squelch=args.squelch, heartbeat_s=0.0,
                        on_packet=log)
    dec.banner()
    print('Reading %s: %.2f s of %d-channel audio at %d Hz.'
          % (args.wav, len(x) / rate, ch, rate))
    block = max(256, int(rate * 0.02))
    for i in range(0, len(x), block):
        dec.feed(x[i:i + block])
    dec.close()
    if log is not None:
        print('Wrote %d row(s) to %s' % (log.nrows, log.path))
        log.close()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='si4463_tnc',
        description='Real-time Si4463 sensor-packet decoder for demodulated '
                    'audio, in the style of a Direwolf soundcard TNC.',
        epilog='SDR++ setup: NFM demodulator, bandwidth >= 60 kHz, '
               'de-emphasis None, AGC off, squelch on and set just above the '
               'noise, audio output routed to the virtual cable. The audio '
               'must be the raw discriminator output -- any high-pass, '
               'de-emphasis or audio AGC removes the near-DC content that '
               '2GFSK carries and packets will stop framing.')
    p.add_argument('-l', '--list-devices', action='store_true',
                   help='list input devices and exit')
    p.add_argument('-d', '--device', default=None,
                   help='input device index or name substring (default: the '
                        'system default input)')
    p.add_argument('-r', '--rate', type=int, default=96000,
                   help='audio sample rate in Hz (default: 96000). 48000 works '
                        'but leaves only 4.8 samples per symbol; set the '
                        'virtual cable to 96000 or 192000 in its control panel '
                        'for margin.')
    p.add_argument('-w', '--wav', default=None,
                   help='decode a WAV file of discriminator audio instead of '
                        'a live device, for testing')
    p.add_argument('-b', '--baud', type=float, default=10000.0,
                   help='symbol rate (default: 10000)')
    p.add_argument('--deviation', type=float, default=20000.0,
                   help='FSK deviation in Hz (default: 20000)')
    p.add_argument('--packet-len', type=int, default=45,
                   help='payload length in bytes (default: 45)')
    p.add_argument('-t', '--threshold', type=float, default=3.0,
                   help='burst detection threshold above the noise floor, in '
                        'dB (default: 3.0)')
    p.add_argument('-s', '--squelch', type=float, default=0.02,
                   help='absolute amplitude floor, 0..1 (default: 0.02). A '
                        'squelched receiver emits digital silence between '
                        'bursts, which would make a purely relative threshold '
                        'meaningless; raise this if silence is being detected '
                        'as bursts.')
    p.add_argument('--heartbeat', type=float, default=30.0,
                   help='seconds between "still listening" lines, 0 to '
                        'disable (default: 30)')
    p.add_argument('--csv', default=None,
                   help='write the packet log here instead of the default '
                        'logs/packets_<date>_<time>.csv beside this script. '
                        'One row per packet, flushed as it arrives; opens '
                        'directly in Excel. An existing file is appended to, '
                        'not overwritten.')
    p.add_argument('--no-csv', action='store_true',
                   help='decode to the screen only and write no log file')
    args = p.parse_args(argv)

    if args.list_devices:
        list_devices()
        return 0

    # On unless turned off, matching si4463_rt.py. A run without flags should
    # leave a record either way, and the two front ends must not disagree about
    # something as basic as whether the packets were kept.
    if args.no_csv:
        args.csv = None
    elif args.csv is None:
        args.csv = default_log_path()
    if args.wav:
        run_wav(args)
    else:
        run_audio(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
