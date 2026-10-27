#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
"""
RTL-SDR source: ctypes straight onto librtlsdr, with an rtl_sdr pipe fallback.

This replaces ``osmosdr.source``. The reasoning is the same as for si4463_dsp:
gr-osmosdr is a large and fragile dependency for one block, and on a Raspberry
Pi it is the piece most likely to be absent from a given image. librtlsdr
itself is a single apt package (``librtlsdr0``), already pulled in by
``rtl-sdr``, and ctypes needs nothing installed at all - so the whole decoder
ends up depending on python3-numpy and nothing else from pip.

Two sources are offered and they are not equivalent:

``RtlSdrSource``  -- ctypes onto librtlsdr. Preferred, and the default.
``RtlSdrPipe``    -- spawns ``rtl_sdr -`` and reads its stdout.

The fallback exists because it needs only the rtl-sdr command-line package and
will run where the shared library cannot be loaded, but it cannot express this
decoder's intended gain setting. ``rtl_sdr`` parses ``-g`` into tenths of a dB
and then treats the value zero as "enable the automatic gain control":

    if (0 == gain) { verbose_auto_gain(dev); } else { ... }

So ``rtl_sdr -g 0`` does not mean 0 dB, it means AGC - which is precisely what
this link must not use. The RTL's AGC hunts during the 1.07 s of silence
between bursts and is still recovering when the next preamble arrives, and the
original decoder's gain defaults are 0 dB manual for exactly that reason. The
nearest the pipe can get is the R820T2's next gain step up, 0.9 dB, and it says
so rather than silently enabling AGC. Through librtlsdr the distinction is
expressible: ``rtlsdr_set_tuner_gain_mode(dev, 1)`` then
``rtlsdr_set_tuner_gain(dev, 0)`` is genuinely manual 0.0 dB.

Sampling is asynchronous. librtlsdr's ``read_sync`` issues one libusb bulk
transfer per call and nothing is being queued between calls, so samples are
lost in the gaps - which for a 5% duty cycle signal means whole packets, at
random, with nothing in the log to say why. ``read_async`` keeps a ring of
transfers in flight and calls back from its own thread; that thread copies each
buffer into a bounded queue and the decoder drains it. If the queue fills the
overrun is counted and reported, because a decoder that silently skips input is
indistinguishable from a quiet band.
"""

import ctypes
import ctypes.util
import queue
import subprocess
import threading

import numpy as np

# gr-osmosdr's own conversion, reproduced so that signal levels printed by this
# decoder are comparable with the ones the Windows/GNU Radio runs printed. The
# RTL2832's 8-bit output does not sit at 127.5: gr-osmosdr's rtl_source_c uses
# (i - 127.4) * (1/128), and a decoder tuned against those numbers should see
# the same numbers here.
_DC_OFFSET = 127.4
_SCALE = 1.0 / 128.0

# Bytes per libusb transfer. Must be a multiple of 512. librtlsdr defaults to
# 16*32*512 = 262144, which at 1.2 MS/s is 109 ms of signal per callback; half
# that keeps the decoder's view of the stream fresher without making the
# callback rate high enough to matter.
DEFAULT_BUF_LEN = 131072

# Buffers the reader thread may queue before it starts dropping. At the default
# transfer size this is about 3.5 s of signal at 1.2 MS/s, which is far more
# slack than the decoder needs and still bounded.
DEFAULT_QUEUE_BUFFERS = 64


class RtlSdrError(RuntimeError):
    pass


def _load_librtlsdr():
    """Find librtlsdr without needing it to be a -dev package.

    Tries the versioned sonames first because that is what the runtime package
    installs; ``librtlsdr.so`` without a version is part of librtlsdr-dev and is
    frequently not present on a Pi that can nonetheless run rtl_test.
    """
    names = ['librtlsdr.so.0', 'librtlsdr.so.2', 'librtlsdr.so']
    found = ctypes.util.find_library('rtlsdr')
    if found:
        names.append(found)
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


_lib = _load_librtlsdr()

_READ_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte),
                            ctypes.c_uint32, ctypes.c_void_p)

if _lib is not None:
    _lib.rtlsdr_get_device_count.restype = ctypes.c_uint32
    _lib.rtlsdr_get_device_count.argtypes = []
    _lib.rtlsdr_get_device_name.restype = ctypes.c_char_p
    _lib.rtlsdr_get_device_name.argtypes = [ctypes.c_uint32]
    _lib.rtlsdr_open.restype = ctypes.c_int
    _lib.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.c_uint32]
    _lib.rtlsdr_close.restype = ctypes.c_int
    _lib.rtlsdr_close.argtypes = [ctypes.c_void_p]
    _lib.rtlsdr_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _lib.rtlsdr_get_sample_rate.restype = ctypes.c_uint32
    _lib.rtlsdr_get_sample_rate.argtypes = [ctypes.c_void_p]
    _lib.rtlsdr_set_center_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _lib.rtlsdr_get_center_freq.restype = ctypes.c_uint32
    _lib.rtlsdr_get_center_freq.argtypes = [ctypes.c_void_p]
    _lib.rtlsdr_set_freq_correction.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _lib.rtlsdr_set_tuner_gain_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _lib.rtlsdr_set_tuner_gain.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _lib.rtlsdr_get_tuner_gain.restype = ctypes.c_int
    _lib.rtlsdr_get_tuner_gain.argtypes = [ctypes.c_void_p]
    _lib.rtlsdr_get_tuner_gains.restype = ctypes.c_int
    _lib.rtlsdr_get_tuner_gains.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_int)]
    _lib.rtlsdr_set_agc_mode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _lib.rtlsdr_reset_buffer.argtypes = [ctypes.c_void_p]
    _lib.rtlsdr_read_async.restype = ctypes.c_int
    _lib.rtlsdr_read_async.argtypes = [ctypes.c_void_p, _READ_CB,
                                       ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.c_uint32]
    _lib.rtlsdr_cancel_async.argtypes = [ctypes.c_void_p]


def available():
    """True if librtlsdr could be loaded at all."""
    return _lib is not None


def device_count():
    return int(_lib.rtlsdr_get_device_count()) if _lib is not None else 0


def device_names():
    if _lib is None:
        return []
    out = []
    for i in range(device_count()):
        name = _lib.rtlsdr_get_device_name(ctypes.c_uint32(i))
        out.append(name.decode('utf-8', 'replace') if name else '(unnamed)')
    return out


def to_complex(raw):
    """Interleaved unsigned bytes -> complex64, the gr-osmosdr way.

    An odd trailing byte is dropped rather than paired with the next buffer's
    first byte. Keeping it would mean carrying half a sample of state for a case
    that cannot happen - libusb transfers are whole multiples of 512 bytes - and
    getting that state wrong would swap I and Q for the rest of the run, which
    inverts the demodulated bits.
    """
    raw = np.frombuffer(raw, dtype=np.uint8) if not isinstance(raw, np.ndarray) \
        else raw
    if raw.size & 1:
        raw = raw[:-1]
    f = raw.astype(np.float32)
    f -= np.float32(_DC_OFFSET)
    f *= np.float32(_SCALE)
    return (f[0::2] + 1j * f[1::2]).astype(np.complex64)


class RtlSdrSource(object):
    """Asynchronous RTL-SDR capture through librtlsdr."""

    def __init__(self, freq_hz, rate_hz, gain_db=0.0, agc=False, rtl_agc=False,
                 ppm=0, device_index=0, bias_tee=False,
                 buf_len=DEFAULT_BUF_LEN,
                 queue_buffers=DEFAULT_QUEUE_BUFFERS, bandwidth_hz=0):
        if _lib is None:
            raise RtlSdrError(
                'librtlsdr could not be loaded. On Debian/Raspberry Pi OS:\n'
                '    sudo apt install librtlsdr0 rtl-sdr\n'
                'Then re-run. Use --pipe to go through the rtl_sdr command '
                'instead, but read the gain note in si4463_rtlsdr.py first.')

        self.dev = ctypes.c_void_p()
        rc = _lib.rtlsdr_open(ctypes.byref(self.dev),
                              ctypes.c_uint32(device_index))
        if rc != 0:
            raise RtlSdrError(_open_help(rc, device_index))

        self.requested_rate = float(rate_hz)
        self.buf_len = int(buf_len)
        self._q = queue.Queue(maxsize=int(queue_buffers))
        self._thread = None
        self._cb = _READ_CB(self._on_buffer)   # kept alive; C holds the pointer
        self._stopping = threading.Event()
        self.overruns = 0
        self.buffers_seen = 0

        _lib.rtlsdr_set_sample_rate(self.dev, ctypes.c_uint32(int(rate_hz)))
        _lib.rtlsdr_set_center_freq(self.dev, ctypes.c_uint32(int(freq_hz)))
        _lib.rtlsdr_set_freq_correction(self.dev, ctypes.c_int(int(ppm)))

        # Tuner gain. Manual unless AGC was asked for - see the module
        # docstring for why manual is the default on this link.
        _lib.rtlsdr_set_tuner_gain_mode(self.dev, ctypes.c_int(0 if agc else 1))
        self.gain_table = self._read_gain_table()
        if not agc:
            want = int(round(float(gain_db) * 10.0))
            _lib.rtlsdr_set_tuner_gain(self.dev, ctypes.c_int(want))

        # The RTL2832's own digital AGC, separate from the tuner's. Off unless
        # asked: it is a second control loop with the same objection as the
        # first.
        _lib.rtlsdr_set_agc_mode(self.dev, ctypes.c_int(1 if rtl_agc else 0))

        # Optional explicit tuner IF bandwidth; 0 lets librtlsdr pick from the
        # sample rate, which is what gr-osmosdr's set_bandwidth(0) did.
        self._maybe('rtlsdr_set_tuner_bandwidth',
                    ctypes.c_uint32(int(bandwidth_hz)))

        # The RTL-SDR Blog V3's bias tee feeds 4.5 V up the coax. Default off
        # and set explicitly either way, because it is stored in the dongle and
        # a previous session can have left it on - into a grounded antenna that
        # is a shorted supply.
        self.bias_tee_ok = self._maybe('rtlsdr_set_bias_tee',
                                       ctypes.c_int(1 if bias_tee else 0))

        self.actual_rate = float(_lib.rtlsdr_get_sample_rate(self.dev))
        self.actual_freq = float(_lib.rtlsdr_get_center_freq(self.dev))
        self.actual_gain_db = _lib.rtlsdr_get_tuner_gain(self.dev) / 10.0
        self.agc = bool(agc)

    def _maybe(self, fname, *args):
        """Call an optional librtlsdr entry point, reporting whether it exists.

        set_bias_tee and set_tuner_bandwidth are absent from older librtlsdr.
        Their absence is worth knowing about but is not worth refusing to run
        over, so it is reported rather than raised.
        """
        fn = getattr(_lib, fname, None)
        if fn is None:
            return False
        try:
            fn(self.dev, *args)
            return True
        except Exception:
            return False

    def _read_gain_table(self):
        n = _lib.rtlsdr_get_tuner_gains(self.dev, None)
        if n <= 0:
            return []
        arr = (ctypes.c_int * n)()
        _lib.rtlsdr_get_tuner_gains(self.dev, arr)
        return [g / 10.0 for g in arr]

    def _on_buffer(self, buf, length, _ctx):
        """librtlsdr's callback, on librtlsdr's thread.

        The buffer belongs to librtlsdr and is reused as soon as this returns,
        so it has to be copied here and not merely referenced. The copy is the
        only work done on this thread: anything slower would stall the transfer
        ring and turn a queue overrun into a USB one.
        """
        if self._stopping.is_set():
            return
        self.buffers_seen += 1
        data = np.ctypeslib.as_array(buf, shape=(int(length),)).copy()
        try:
            self._q.put_nowait(data)
        except queue.Full:
            self.overruns += 1

    def start(self):
        _lib.rtlsdr_reset_buffer(self.dev)
        self._thread = threading.Thread(target=self._run_async, daemon=True,
                                        name='rtlsdr-read')
        self._thread.start()

    def _run_async(self):
        # Blocks inside libusb until cancel_async; buf_num 0 means librtlsdr's
        # default ring depth.
        _lib.rtlsdr_read_async(self.dev, self._cb, None,
                               ctypes.c_uint32(0),
                               ctypes.c_uint32(self.buf_len))

    def blocks(self, timeout=1.0):
        """Yield complex64 blocks until stop() is called."""
        while not self._stopping.is_set():
            try:
                raw = self._q.get(timeout=timeout)
            except queue.Empty:
                if self._thread is not None and not self._thread.is_alive():
                    return
                continue
            yield to_complex(raw)

    def stop(self):
        self._stopping.set()
        if _lib is not None and self.dev:
            _lib.rtlsdr_cancel_async(self.dev)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def close(self):
        self.stop()
        if _lib is not None and self.dev:
            _lib.rtlsdr_close(self.dev)
            self.dev = ctypes.c_void_p()


def _open_help(rc, index):
    return (
        'rtlsdr_open(index=%d) failed with %d.\n'
        'The three things that cause this on a Raspberry Pi, in order of how '
        'often they do:\n'
        '  1. the DVB-T kernel driver claimed the dongle. Check with\n'
        '         lsmod | grep dvb_usb_rtl28xxu\n'
        '     and blacklist it (install.sh does this), then replug.\n'
        '  2. no permission. The udev rule from install.sh grants the plugdev\n'
        '     group access; without it only root can open the device.\n'
        '  3. something else already has it open - rtl_tcp, rtl_fm, SoapySDR,\n'
        '     another copy of this decoder. Only one process at a time can\n'
        '     hold an RTL-SDR.' % (index, rc))


class RtlSdrPipe(object):
    """Fallback source: read interleaved bytes from the rtl_sdr command.

    Equivalent in every respect but gain, where it cannot express 0 dB manual -
    see the module docstring. Used when librtlsdr will not load, or on request.
    """

    # The R820T2's lowest gain step above zero. rtl_sdr reads -g 0 as "enable
    # AGC", so this is as close to the intended 0 dB as the command can be
    # driven, and it is 0.9 dB of error rather than an unwanted control loop.
    LOWEST_NONZERO_GAIN_DB = 0.9

    def __init__(self, freq_hz, rate_hz, gain_db=0.0, agc=False, ppm=0,
                 device_index=0, bias_tee=False, read_bytes=DEFAULT_BUF_LEN):
        self.requested_rate = float(rate_hz)
        self.actual_rate = float(rate_hz)
        self.actual_freq = float(freq_hz)
        self.read_bytes = int(read_bytes)
        self.overruns = 0
        self.buffers_seen = 0
        self.agc = bool(agc)
        self.gain_table = []
        self.bias_tee_ok = False
        self.gain_substituted = False

        cmd = ['rtl_sdr', '-f', '%d' % int(freq_hz), '-s', '%d' % int(rate_hz),
               '-d', '%d' % int(device_index), '-p', '%d' % int(ppm)]
        if agc:
            cmd += ['-g', '0']           # rtl_sdr's spelling of "automatic"
        else:
            g = float(gain_db)
            if g == 0.0:
                g = self.LOWEST_NONZERO_GAIN_DB
                self.gain_substituted = True
            cmd += ['-g', '%.1f' % g]
        cmd.append('-')
        self.actual_gain_db = 0.0 if agc else float(cmd[cmd.index('-g') + 1])
        self.cmd = cmd
        self._proc = None
        self._stopping = threading.Event()

        if bias_tee:
            # rtl_sdr has no bias-tee switch; rtl_biast is a separate tool.
            self._bias_tee_requested = True
        else:
            self._bias_tee_requested = False

    def start(self):
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE,
                stderr=None, bufsize=0)
        except FileNotFoundError:
            raise RtlSdrError(
                'rtl_sdr not found. Install it with\n'
                '    sudo apt install rtl-sdr')

    def blocks(self, timeout=None):
        n = self.read_bytes
        while not self._stopping.is_set():
            raw = self._proc.stdout.read(n)
            if not raw:
                return
            self.buffers_seen += 1
            yield to_complex(raw)

    def stop(self):
        self._stopping.set()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def close(self):
        self.stop()


class WavSource(object):
    """Replay an SDR++ baseband WAV, for checking the chain against a file.

    SDR++ writes I and Q as the two channels of an ordinary WAV and puts the
    centre frequency, not the sample rate, in the filename, so the rate comes
    from the header. This is the same path the live source feeds, so a replay
    exercises the identical DSP and decoder - which is how the port was checked
    before it ever saw an antenna.
    """

    def __init__(self, path, block_samples=65536):
        import wave
        self.path = path
        try:
            self._w = wave.open(path, 'rb')
        except FileNotFoundError:
            raise RtlSdrError('no such file: %s' % path)
        except IsADirectoryError:
            raise RtlSdrError('%s is a directory, not a recording' % path)
        except PermissionError:
            raise RtlSdrError('cannot read %s (permission denied)' % path)
        except wave.Error as exc:
            raise RtlSdrError(
                '%s is not a readable WAV (%s).\n'
                'A baseband recording must be a RIFF WAV with I and Q as its '
                'two channels -\n'
                'headerless raw IQ, however it is named, cannot be read here.'
                % (path, exc))
        if self._w.getnchannels() != 2:
            raise RtlSdrError(
                '%s has %d channel(s); a baseband recording needs 2 (I and Q).'
                % (path, self._w.getnchannels()))
        self.width = self._w.getsampwidth()
        if self.width not in (1, 2, 4):
            raise RtlSdrError('unsupported sample width %d bytes' % self.width)
        self.actual_rate = float(self._w.getframerate())
        self.requested_rate = self.actual_rate
        self.actual_freq = 0.0
        self.actual_gain_db = 0.0
        self.agc = False
        self.gain_table = []
        self.overruns = 0
        self.buffers_seen = 0
        self.bias_tee_ok = False
        self.frames = self._w.getnframes()
        self.block_samples = int(block_samples)
        self._stopping = threading.Event()

    def start(self):
        pass

    def blocks(self, timeout=None):
        dt = {1: np.uint8, 2: np.int16, 4: np.int32}[self.width]
        full = float(1 << (8 * self.width - 1))
        while not self._stopping.is_set():
            raw = self._w.readframes(self.block_samples)
            if not raw:
                return
            self.buffers_seen += 1
            a = np.frombuffer(raw, dtype=dt).astype(np.float32)
            if self.width == 1:
                a = (a - 128.0)
            a /= np.float32(full)
            yield (a[0::2] + 1j * a[1::2]).astype(np.complex64)

    def stop(self):
        self._stopping.set()

    def close(self):
        self.stop()
        self._w.close()
