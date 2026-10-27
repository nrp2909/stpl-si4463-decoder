#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression test for the Si4463 decoder. Needs numpy and nothing else -- no
GNU Radio, no soundcard, no radio:

    C:\\Users\\np841\\radioconda\\python.exe si4463_selftest.py

It builds the on-air frame the firmware sends -- 16 bytes of 0xAA, the sync
word 0x2D 0xD4, then the 45-byte SensorPacket_t, 2GFSK at 10 kbps with 20 kHz
deviation -- modulates it, buries it in noise, and feeds it to the decoder the
way the live front ends do. Every case states what it is protecting:

  * sensitivity, as an SNR sweep down to 0 dB
  * tuner error, as an offset sweep to +/-30 kHz
  * inverted bits, from an I/Q swap or a receiver that inverts its
    discriminator
  * block size, because GNU Radio decides how many samples to hand over and it
    is not constant. Three separate bugs lived here: the noise floor used to be
    learned per feed() call rather than per unit of time, only one burst could
    be found per call, and a run of samples over the threshold could be split
    across two calls and never counted.
  * both payload layouts, current and pre-timestamp
  * degenerate input -- pure noise, digital silence, constant DC, a clipping
    front end -- none of which may produce a packet

Run it after any change to si4463_core.py. It prints one line per case and
exits non-zero if anything fails.
"""

import contextlib
import io
import struct
import sys

import numpy as np

from si4463_core import Si4463Decoder

BAUD = 10000.0
DEV = 20000.0
PREAMBLE_SYNC = '10101010' * 16 + '0010110111010100'


# ---------------------------------------------------------------------------
# the two payload layouts
# ---------------------------------------------------------------------------

def payload(counter, sec=30):
    """The current 45-byte frame: 19-character timestamp, then telemetry."""
    ts = '16-45-%02d,05-09-2026' % (sec % 60)
    return ts.encode('ascii') + struct.pack(
        '<IfffffH', counter, 21.5, 55.25, 19.07283, 72.87, 4.25, 137)


def legacy_payload(counter):
    """The frame sent before the timestamp: telemetry, then 0xC3 padding."""
    return (struct.pack('<IfffffH', counter, 21.5, 55.25, 19.07283, 72.87,
                        4.25, 137) + b'\xc3' * 19)


def frame_bits(p):
    return PREAMBLE_SYNC + ''.join('{:08b}'.format(b) for b in p)


# ---------------------------------------------------------------------------
# signal generation
# ---------------------------------------------------------------------------

def _symbols(bits, fs, invert):
    sym = np.array([1.0 if b == '1' else -1.0 for b in bits])
    if invert:
        sym = -sym
    sps = fs / BAUD
    n = int(round(len(sym) * sps))
    return sym[np.minimum((np.arange(n) / sps).astype(int), len(sym) - 1)], n


def fsk_iq(bits, fs, rng, snr_db=20.0, offset_hz=0.0, invert=False):
    """Complex baseband, as the RTL-SDR chain delivers it."""
    s, n = _symbols(bits, fs, invert)
    x = np.exp(1j * np.cumsum(2 * np.pi * DEV * s / fs))
    x = x * np.exp(1j * 2 * np.pi * offset_hz * np.arange(n) / fs)
    a = 10 ** (-snr_db / 20.0)
    x = x + (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * a / np.sqrt(2)
    return x.astype(np.complex64)


def fsk_audio(bits, fs, rng, snr_db=20.0, dc=0.0, invert=False):
    """Discriminator output, as a receiver's audio delivers it."""
    s, n = _symbols(bits, fs, invert)
    a = 10 ** (-snr_db / 20.0)
    return (0.5 * s + dc + rng.standard_normal(n) * a * 0.5).astype(np.float32)


def noise_iq(n, snr_db, rng):
    a = 10 ** (-snr_db / 20.0)
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n))
            * a / np.sqrt(2)).astype(np.complex64)


def noise_audio(n, snr_db, rng, dc=0.0):
    a = 10 ** (-snr_db / 20.0)
    return (rng.standard_normal(n) * a * 0.5 + dc).astype(np.float32)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

FAILED = []


def check(name, ok, detail=''):
    print('  %-48s %s %s' % (name, 'PASS' if ok else '**FAIL**', detail))
    if not ok:
        FAILED.append(name)
    return ok


def run(mode, fs, bursts, gap_s=0.30, lead_s=0.6, block=4096, seed=1,
        floor_snr=15.0, squelch=0.0, legacy=False):
    """Feed a synthetic stream through the decoder and collect its packets.

    lead_s is idle noise before the first burst. The decoder needs about
    0.16 s of it to learn the noise floor before it will call anything a
    burst, which is inherent: nothing can know the floor before it has heard
    any.
    """
    rng = np.random.default_rng(seed)
    got = []
    dec = Si4463Decoder(fs=fs, symbol_rate=BAUD, deviation=DEV,
                        packet_len_bytes=45, threshold_db=3.0, mode=mode,
                        squelch=squelch, heartbeat_s=0.0, quiet=True,
                        on_packet=lambda p, f: got.append(f))
    mk = fsk_iq if mode == 'iq' else fsk_audio
    nz = noise_iq if mode == 'iq' else noise_audio
    parts = [nz(int(lead_s * fs), floor_snr, rng)] if lead_s else []
    for b in bursts:
        b = dict(b)
        c = b.pop('counter', 1)
        p = legacy_payload(c) if legacy else payload(c, b.pop('sec', 30))
        parts.append(mk(frame_bits(p), fs, rng, **b))
        parts.append(nz(int(gap_s * fs), floor_snr, rng))
    x = np.concatenate(parts)
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(0, len(x), block):
            dec.feed(x[i:i + block])
        dec.close()
    return dec, got


def expect(name, dec, got, want, layout='current'):
    ok = (len(got) == want
          and all(f['ts_ok'] for f in got)
          and all(f['layout'] == layout for f in got))
    check(name, ok, '(%d/%d packets, %d burst, %d bad)'
          % (len(got), want, dec.nburst, dec.nbad))


def main():
    print('Si4463 decoder self-test')

    print('\nIQ mode at 200 kHz -- the si4463_rt.py chain')
    for snr in (30, 20, 15, 10, 6, 3, 0):
        d, g = run('iq', 200000.0,
                   [dict(counter=c, sec=30 + c, snr_db=snr, offset_hz=3500.0)
                    for c in range(3)], floor_snr=snr)
        expect('SNR %2d dB, +3.5 kHz offset' % snr, d, g, 3)

    for off in (-30000, -12600, 0, 12600, 30000):
        d, g = run('iq', 200000.0,
                   [dict(counter=c, sec=30 + c, snr_db=15, offset_hz=off)
                    for c in range(3)])
        expect('tuner error %+6.1f kHz' % (off / 1e3), d, g, 3)

    d, g = run('iq', 200000.0, [dict(counter=1, snr_db=15, invert=True)])
    expect('inverted polarity (I/Q swap, TX_INV)', d, g, 1)

    # GNU Radio picks the block size and it varies; none of it may matter.
    for blk in (256, 1024, 4096, 16384, 65536):
        d, g = run('iq', 200000.0,
                   [dict(counter=c, sec=30 + c, snr_db=15) for c in range(3)],
                   block=blk)
        expect('feed block %6d samples' % blk, d, g, 3)

    d, g = run('iq', 200000.0,
               [dict(counter=c, sec=30 + c, snr_db=15) for c in range(6)],
               gap_s=0.05)
    expect('6 bursts 50 ms apart', d, g, 6)

    d, g = run('iq', 200000.0,
               [dict(counter=c, snr_db=15) for c in range(3)], legacy=True)
    expect('pre-timestamp payload layout', d, g, 3, layout='legacy')

    print('\naudio mode -- the si4463_tnc.py chain')
    for fs in (48000.0, 96000.0, 192000.0):
        d, g = run('audio', fs,
                   [dict(counter=c, sec=30 + c, snr_db=15) for c in range(3)])
        expect('%6.0f Hz (%.1f samples/symbol)' % (fs, fs / BAUD), d, g, 3)

    for snr in (30, 20, 15, 10, 6):
        d, g = run('audio', 96000.0,
                   [dict(counter=c, sec=30 + c, snr_db=snr) for c in range(3)],
                   floor_snr=snr)
        expect('SNR %2d dB' % snr, d, g, 3)

    d, g = run('audio', 96000.0, [dict(counter=1, snr_db=15, dc=0.30)])
    expect('discriminator sitting on +0.30 DC', d, g, 1)

    d, g = run('audio', 96000.0, [dict(counter=1, snr_db=15, invert=True)])
    expect('inverted discriminator', d, g, 1)

    print('\ndegenerate input -- none of this may produce a packet')
    rng = np.random.default_rng(3)
    cases = (
        ('pure noise, 5 s, iq', 'iq', 200000.0, noise_iq(1000000, 20, rng)),
        ('pure noise, 5 s, audio', 'audio', 96000.0,
         noise_audio(480000, 20, rng)),
        ('digital silence (squelched receiver)', 'audio', 96000.0,
         np.zeros(480000, dtype=np.float32)),
        ('constant DC', 'audio', 96000.0,
         np.full(480000, 0.4, dtype=np.float32)),
        ('front end clipping at full scale', 'iq', 200000.0,
         (np.sign(rng.standard_normal(400000))
          + 1j * np.sign(rng.standard_normal(400000))).astype(np.complex64)),
    )
    for name, mode, fs, x in cases:
        dec = Si4463Decoder(fs=fs, symbol_rate=BAUD, deviation=DEV, mode=mode,
                            heartbeat_s=0.0, quiet=True)
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(0, len(x), 4096):
                dec.feed(x[i:i + 4096])
            dec.close()
        check(name, dec.npkt == 0,
              '(%d packet(s), %d burst(s))' % (dec.npkt, dec.nburst))

    print('\napi')
    dec = Si4463Decoder(fs=200000.0, mode='iq', heartbeat_s=0.0, quiet=True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            for n in (0, 1, 5, 100):
                dec.feed(np.zeros(n, dtype=np.complex64))
            dec.close()
        check('empty and undersized feed blocks', True)
    except Exception as exc:                       # noqa: BLE001
        check('empty and undersized feed blocks', False, repr(exc))

    try:
        Si4463Decoder(fs=20000.0, mode='iq')
        check('rejects a rate below 3 samples/symbol', False)
    except ValueError:
        check('rejects a rate below 3 samples/symbol', True)

    try:
        Si4463Decoder(fs=200000.0, mode='fm')
        check("rejects an unknown mode", False)
    except ValueError:
        check("rejects an unknown mode", True)

    print()
    if FAILED:
        print('%d FAILED: %s' % (len(FAILED), ', '.join(FAILED)))
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
