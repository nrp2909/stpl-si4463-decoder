# -*- coding: utf-8 -*-
"""
Streaming Si4463 packet decoder -- numpy only, no GNU Radio, no audio backend.

This is the engine shared by the two real-time front ends:

  * si4463_rt.py    complex baseband straight off an RTL-SDR, via GNU Radio.
  * si4463_tnc.py   demodulated audio off a soundcard or virtual cable, the
                    way Direwolf takes audio from a receiver's discriminator.

The physical layer is documented at length in si4463_bench_decode_epy_block_0.py
and is unchanged here: 16 bytes of 0xAA preamble, the sync word 0x2D 0xD4 as it
appears on air (AN626 3.2.3 sends preamble and sync LSB-first, so the 0xB4 0x2B
in SYNC_BITS_31_24/23_16 is bit-reversed on its way out), then 45 payload bytes
MSB-first with no whitening and no appended CRC. 63 bytes, 50.4 ms at 10 kbps.

What is new is that decoding happens *as the samples arrive*. The bench decoder
buffered the whole capture and decoded it in stop(); that cannot run live,
because the buffer grows without bound and nothing is reported until the
flowgraph ends. This class carries a small rolling window and a state machine --
learning the noise floor, idle, inside a burst -- and emits each packet about
10 ms after its last symbol lands.

Two things the batch version got for free and this one has to earn:

  * The noise floor. Batch took the median of the whole file. Live, the floor is
    a rolling median over fixed 10 ms slices of the stream, sampled only while
    idle, so a slice that happens to be full of burst cannot drag it. The
    transmitter's duty cycle is about 5% (51.7 ms every 1.12 s), so the median
    of an idle slice is noise, and the median over several seconds rides out a
    passing interferer. The slices are measured in time rather than in feed()
    calls on purpose: GNU Radio decides how big a block to hand over and it is
    not constant, so counting calls made both the warm-up and the adaptation
    rate depend on something the decoder does not control.
  * The end of a burst. Batch could see the whole envelope at once and rejoin
    fragments split by a fade. Live there is nothing to rejoin, because the
    frame is a fixed 63 bytes: once a start is detected the window that follows
    it is a known length, so detection only has to answer "is a packet starting
    near here" and never has to find the end. A burst whose middle is the only
    part above the floor still arrives at the demodulator whole, which is what
    a threshold-to-threshold segment could not do.

  * Not trusting one sample. A single sample over the threshold opens nothing;
    the envelope has to stay above it for two whole symbols. A real burst is
    504 symbols long, so the requirement is free, and without it a noisy
    estimate opens bursts on empty air several times a second -- each one
    holding a capture window that a real packet may have needed.

Two input modes, differing only in how a block of samples becomes a stream of
frequency deviations:

  'iq'     complex baseband. Power is |x|^2 and the discriminator is the phase
           difference between consecutive samples.
  'audio'  real samples that have *already* been FM-demodulated by the receiver,
           so they are the discriminator output. Power is the variance measured
           over the detection window, because a discriminator's DC level
           encodes the carrier offset rather than the signal's presence -- and
           that level is only constant locally, so the mean has to be taken
           over the same window as the power and not over everything buffered.
"""

import collections
import struct
import time

import numpy as np

# 0x2D 0xD4, MSB-first, as it appears on air.
SYNC_BITS = '0010110111010100'

# How much of the 128-bit 0xAA preamble must immediately precede a sync match
# for it to be believed. 32 bits leaves plenty of the run unexamined, so a late
# burst edge or a slow AGC does not cost a packet, while still making a chance
# hit vanishingly unlikely.
PREAMBLE_CHECK_BITS = 32

# Payload layout of SensorPacket_t in Core/Inc/sensor_packet.h. The frame now
# opens with a fixed-width ASCII timestamp -- HH-MM-SS,DD-MM-YYYY, 19 characters
# with no terminator -- and the 26 bytes of binary telemetry follow it. That is
# exactly the 45-byte frame with nothing left over, so the 0xC3 padding this
# decoder used to check for no longer exists: main.c still memsets the buffer
# with it, but the memcpy of the struct now overwrites every byte.
#
# Reading the old 26-byte layout off the new frame is what produces an absurd
# packet counter. The counter used to sit at offset 0; it sits at offset 19 now,
# and offset 0 is the first four characters of the timestamp, so 'counter' came
# out as those ASCII bytes read as a little-endian uint32 -- a nine- or
# ten-digit number that changes every second -- with every field after it
# shifted by 19 bytes and reading as garbage too.
TS_LEN = 19
PACKET_FMT = '<%dsIfffffH' % TS_LEN
PACKET_LEN = struct.calcsize(PACKET_FMT)

# What each position of the timestamp has to look like: 'd' is any ASCII digit,
# anything else is that literal separator character.
TS_MASK = 'dd-dd-dd,dd-dd-dddd'

# The layout the firmware sent before the timestamp was added: the same
# telemetry, but first in the frame, with the remaining 19 bytes filled with
# 0xC3. Recordings in this format exist and still decode, so both layouts are
# recognised rather than one being assumed -- assuming is what turns a packet
# counter into a ten-digit number. Conveniently each layout leaves exactly 19
# bytes of known content, so the two hypotheses are scored on equal terms and
# neither can score on the other's data: 0xC3 is not an ASCII digit, and a
# timestamp is not 0xC3.
LEGACY_FMT = '<IfffffH'
LEGACY_LEN = struct.calcsize(LEGACY_FMT)
PAD_BYTE = 0xC3

# How many of the 16 sync bits may be wrong before the sync word is disbelieved.
# Demanding an exact match discards a whole packet for one bad bit in sixteen,
# which at the edge of range is most of them. What keeps the false-alarm rate
# negligible is the preamble check that follows, not the sync word alone: a
# chance 14-of-16 match still has to be preceded by 32 bits that alternate.
SYNC_TOL = 2
_SYNC_ARR = np.array([int(c) for c in SYNC_BITS], dtype=np.uint8)

# Preamble + sync ahead of the payload, in bytes: 16 + 2.
FRAME_OVERHEAD_BYTES = 18


class Si4463Decoder(object):
    """Feed it sample blocks, it prints packets.

    Parameters
    ----------
    fs : float
        Sample rate of the stream handed to feed(), in Hz.
    mode : {'iq', 'audio'}
        See the module docstring.
    squelch : float
        Absolute amplitude below which a block is definitely silence. Only
        meaningful in audio mode, where a squelched receiver emits digital zero
        between bursts: the adaptive floor would then be zero, every sample
        would clear a purely relative threshold, and the decoder would chase
        noise. The threshold used is whichever of the two is higher.
    on_packet : callable or None
        Called as on_packet(payload_bytes, fields_dict) for every framed packet,
        after the built-in report is printed. Somewhere to hang a KISS TNC, a
        log file, or an MQTT publish without touching this file.
    """

    def __init__(self, fs, symbol_rate=10000.0, deviation=20000.0,
                 packet_len_bytes=45, threshold_db=3.0, mode='iq',
                 squelch=0.02, heartbeat_s=30.0, on_packet=None, quiet=False):
        if mode not in ('iq', 'audio'):
            raise ValueError("mode must be 'iq' or 'audio'")
        self.fs = float(fs)
        self.symbol_rate = float(symbol_rate)
        self.deviation = float(deviation)
        self.sps = self.fs / self.symbol_rate
        if self.sps < 3.0:
            raise ValueError(
                'sample rate %.0f Hz gives only %.1f samples per symbol at '
                '%.0f baud; timing recovery needs at least 3, and 8 or more '
                'is comfortable.' % (self.fs, self.sps, self.symbol_rate))
        self.packet_len_bytes = int(packet_len_bytes)
        self.threshold_db = float(threshold_db)
        self.mode = mode
        self.squelch = float(squelch) if mode == 'audio' else 0.0
        self.heartbeat_s = float(heartbeat_s)
        self.on_packet = on_packet
        self.quiet = bool(quiet)

        # The frame is a fixed 63 bytes, so once its leading edge is seen
        # there is nothing to decide about where it ends: capture a fixed window
        # and stop. The previous design cut the segment at the last sample that
        # exceeded the threshold, which is fine on a strong signal and wrong on
        # a weak one -- a burst whose middle is the only part above the floor
        # arrives at the demodulator with its preamble and its tail missing, and
        # no amount of demodulator work recovers bits that were discarded before
        # it ran. Detection now only has to answer "is a packet starting near
        # here", and the window is generous enough that the answer can be late.
        self._frame = int((self.packet_len_bytes + FRAME_OVERHEAD_BYTES)
                          * 8 * self.sps)
        self._pre_guard = int(0.35 * self._frame)
        self._capture_len = int(1.35 * self._frame)

        # Detection averages over sixteen symbols rather than half of one.
        # The envelope of a weak burst is mostly noise variance, and the spread
        # of that estimate falls as the square root of the number of samples
        # averaged, which is what lets the threshold sit a few dB above the
        # floor instead of six.
        #
        # Sixteen rather than four because the window has to be judged in
        # samples, not in symbols: four symbols is 80 samples of complex
        # baseband at 20 samples per symbol, but only 19 samples of a 48 kHz
        # discriminator feed, and a 19-sample power estimate scatters far too
        # widely for a 3 dB threshold to mean anything. Sixteen symbols is
        # 1.6 ms against a 50 ms burst, so the edges are still resolved to
        # better than a thirtieth of the packet, and the capture window does
        # not depend on them anyway.
        self._win = max(1, int(16 * self.sps))
        # A burst has to stay above the threshold for two whole symbols before
        # it is believed. One sample over the line is not evidence: the
        # envelope is a noisy estimate, and in audio mode -- where the window
        # is shorter and the underlying power estimate has far fewer degrees of
        # freedom than the |x|^2 of complex baseband -- a single-sample rule
        # opens several bursts a second on pure noise. Each of those consumes a
        # capture window, and one that lands shortly before a real packet takes
        # the window that packet needed, so the cost is not just noise in the
        # log: it is lost packets. A real burst is 504 symbols long, so
        # demanding two costs nothing.
        self._min_len = max(1, int(2 * self.sps))

        dtype = np.complex64 if mode == 'iq' else np.float32
        self._dtype = dtype
        # Rolling window: _buf[0] is absolute sample index _buf_start, and the
        # first _scanned samples of it have already been classified.
        self._buf = np.empty(0, dtype=dtype)
        self._buf_start = 0
        self._scanned = 0
        self._nsamp = 0

        # Noise floor history, long enough to span several transmit cycles.
        # One entry per fixed slice of the stream rather than one per feed()
        # call, so neither the warm-up nor the adaptation rate depends on the
        # caller's block size. 10 ms is short enough that a slice is never
        # mostly burst, and 512 of them is about five seconds of history --
        # several transmit cycles, so a passing interferer cannot move the
        # median. Slices never run longer than the guard band, or trimming
        # could drop a slice before it is complete.
        self._floor_slice = min(max(self._win, int(0.010 * self.fs)),
                                max(self._win, self._pre_guard))
        self._floor_hist = collections.deque(maxlen=512)
        self._floor_min = 16          # 160 ms before the first decision
        self._floor_pos = 0           # absolute index of the next slice
        self._floor = None

        self._in_burst = False
        self._burst_start = 0
        self._last_hot = 0
        self._peak = 0.0
        self._offset_hz = 0.0
        # Kept so the on_packet callback can log the burst's quality alongside
        # its contents: a row of telemetry says nothing about how close the
        # link was to failing, and these two are what a log needs to answer
        # that afterwards.
        self._snr_db = 0.0

        self.npkt = 0
        self.nburst = 0
        self.nbad = 0
        self.nlost = 0
        self._last_counter = None
        self._said = set()
        self._t_start = time.time()
        self._t_heartbeat = self._t_start
        self._t_lastpkt = None

    # ------------------------------------------------------------------
    # public interface
    # ------------------------------------------------------------------

    def banner(self):
        self._say('Si4463 decoder: %s mode, %.0f Hz, %.2f samples/symbol, '
                  '%d-byte payload, threshold %.1f dB%s.'
                  % (self.mode, self.fs, self.sps, self.packet_len_bytes,
                     self.threshold_db,
                     ', squelch %.3f' % self.squelch if self.squelch else ''))
        self._t_start = time.time()
        self._t_heartbeat = self._t_start

    def feed(self, samples):
        """Absorb one block of samples and decode whatever completes."""
        x = np.asarray(samples, dtype=self._dtype).reshape(-1)
        if not len(x):
            return
        self._buf = np.concatenate((self._buf, x))
        self._nsamp += len(x)
        # Watch the input level. An RTL-SDR that is clipping produces a raised
        # noise floor and intermodulation that no decoder change can undo, and
        # from the packet log alone it looks exactly like a weak signal.
        if len(x):
            pk = float(np.max(np.abs(x)))
            if pk > self._peak:
                self._peak = pk
        self._process()
        self._maybe_heartbeat()

    def close(self):
        """Flush a burst that was still open when the stream ended."""
        if self._in_burst:
            self._close_burst(self._last_hot)
        run = time.time() - self._t_start
        self._say('Stopped after %.1f s (%.1f s of signal): %d packet(s) from '
                  '%d burst(s), %d with bit errors, %d lost by counter.'
                  % (run, self._nsamp / self.fs, self.npkt, self.nburst,
                     self.nbad, self.nlost))

    # ------------------------------------------------------------------
    # burst tracking
    # ------------------------------------------------------------------

    def _power(self, x):
        """Per-sample power of one segment, for reporting its SNR.

        A discriminator sitting on a carrier offset has a large DC term that
        says nothing about whether a packet is present, so measure the swing
        about the mean instead of the absolute level. Here the segment is a
        single burst or a single stretch of noise, so its own mean is the right
        thing to subtract; detection needs a local mean instead and uses
        _envelope().
        """
        if self.mode == 'iq':
            return np.abs(x) ** 2
        return (np.asarray(x, dtype=np.float64) - np.mean(x)) ** 2

    def _envelope(self, x, win):
        """Smoothed power along the stream, for burst detection.

        In audio mode this is the local variance, not the deviation from the
        mean of everything currently buffered. The discriminator's DC level is
        only constant locally: a buffer that straddles the edge of a burst has
        a mean belonging to neither side of it, which makes the quiet samples
        next to a packet look energetic and opens a spurious burst on the tail
        of every strong one. Subtracting the mean of the same window the power
        is averaged over removes the DC wherever it happens to sit.
        """
        k = np.ones(win) / win
        if self.mode == 'iq':
            return np.convolve(np.abs(x) ** 2, k, mode='same')
        d = np.asarray(x, dtype=np.float64)
        m = np.convolve(d, k, mode='same')
        return np.maximum(np.convolve(d * d, k, mode='same') - m * m, 0.0)

    def _process(self):
        win = self._win
        if len(self._buf) < 2 * win:
            return
        # mode='same' leaves the trailing win samples averaged over a partial
        # window, so hold them back and classify them on the next call.
        env = self._envelope(self._buf, win)
        valid = len(env) - win
        if valid <= self._scanned:
            return

        self._sample_floor(env, valid)
        if len(self._floor_hist) < self._floor_min:
            self._scanned = valid
            self._trim()
            return

        self._floor = float(np.median(self._floor_hist))
        thresh = max(self._floor * 10.0 ** (self.threshold_db / 10.0),
                     self.squelch ** 2)
        if thresh <= 0:
            self._scanned = valid
            self._trim()
            return

        # Loop rather than handle one burst per call. A block is whatever
        # size the caller's source hands over -- GNU Radio's is not constant
        # and can be tens of thousands of samples -- so a block can easily span
        # a whole packet and the start of the next one. Returning after the
        # first burst silently discarded every later packet in the same block.
        while self._scanned < valid:
            i = self._scanned
            hot = env[i:valid] > thresh

            if not self._in_burst:
                k = _first_run(hot, self._min_len)
                if k < 0:
                    # Hold back the tail of the window: a run that straddles
                    # the boundary would otherwise be split in two and never
                    # counted.
                    self._scanned = max(self._scanned,
                                        valid - (self._min_len - 1))
                    break
                self._in_burst = True
                self._burst_start = self._buf_start + i + k
                self._last_hot = self._burst_start
                self._scanned = i + k + 1
                continue

            # A fixed-length frame needs no end detection: decode as soon as a
            # whole one could have arrived.
            stop = self._burst_start + self._capture_len
            lim = min(valid, stop - self._buf_start)
            if lim > i:
                idx = np.flatnonzero(hot[:lim - i])
                if len(idx):
                    self._last_hot = self._buf_start + i + int(idx[-1])
            if self._buf_start + valid < stop:
                self._scanned = valid
                break
            self._close_burst(self._last_hot)
            # Resume the search at the end of the window just decoded, so a
            # packet that followed inside this same block is still found.
            self._scanned = max(self._scanned, stop - self._buf_start)

        self._trim()

    def _sample_floor(self, env, valid):
        """Add one noise-floor estimate per completed slice of the stream.

        Slices are anchored to absolute sample positions, so the same stream
        produces the same history whatever size the blocks arrive in. Nothing
        is recorded while a burst is open -- that is the one stretch known not
        to be noise -- but the position still advances, so a long burst does
        not leave a backlog to catch up on afterwards.
        """
        n = self._floor_slice
        # Trimming can have moved the buffer past an incomplete slice.
        if self._floor_pos < self._buf_start:
            self._floor_pos = self._buf_start
        while self._floor_pos + n <= self._buf_start + valid:
            if not self._in_burst:
                a = self._floor_pos - self._buf_start
                self._floor_hist.append(float(np.median(env[a:a + n])))
            self._floor_pos += n

    def _close_burst(self, end):
        """Decode the fixed window that opened at the detected burst start.

        `end` is only used for the reported duration; the extracted window is
        anchored to the start and sized from the frame, so a detection that
        begins part-way into the packet still carries the preamble that came
        before it and the payload that comes after.
        """
        self._in_burst = False
        a = self._burst_start
        lo = max(self._buf_start, a - self._pre_guard)
        hi = min(self._buf_start + len(self._buf), a + self._capture_len)
        seg = self._buf[lo - self._buf_start:hi - self._buf_start]
        if len(seg) > self._frame // 2:
            self._decode_burst(lo, seg, hot_ms=(end - a) / self.fs * 1000.0)

    def _trim(self):
        """Drop samples that can no longer be needed."""
        if self._in_burst:
            keep = self._burst_start - self._pre_guard
        else:
            # Enough lookback that a burst starting just past the scan point
            # still has its guard band available.
            keep = (self._buf_start + self._scanned
                    - (self._pre_guard + self._win + 1))
        drop = min(keep - self._buf_start, len(self._buf))
        if drop <= 0:
            return
        self._buf = self._buf[drop:]
        self._buf_start += drop
        self._scanned = max(0, self._scanned - drop)

    # ------------------------------------------------------------------
    # demodulation -- carried over unchanged from the bench decoder
    # ------------------------------------------------------------------

    def _carrier_offset(self, x):
        """Residual carrier offset of the burst, in radians per sample.

        Two things matter here. First, *where* it is measured: the capture
        window deliberately runs wider than the packet so a late detection still
        catches the preamble, which leaves roughly a third of it noise, so the
        measurement is confined to the frame-length stretch carrying the most
        energy.

        Second, *how*. Taking percentiles of the discriminator and splitting the
        difference works on a strong signal and falls apart on a weak one, where
        a discriminator fed noise is uniform across the whole of +/-pi and drags
        both percentiles outward. Summing the phase rotation as a complex
        quantity instead lets noise average out incoherently while the carrier
        adds in phase; the angle of sum(x[k+n] * conj(x[k])) is then n times the
        offset, plus whatever the data contributed over that lag.

        The trick is choosing the lag so the data contributes nothing. Over
        fs/deviation samples each FSK rail turns through a whole number of
        cycles -- the modulation index here is exactly 4 -- so the two rails
        land on the same phase and cancel out of the sum identically, leaving
        the carrier alone. Lags that are not multiples of that carry a
        cos(n*w_dev) factor which goes negative for most n and flips the answer
        by pi.

        Such a lag is precise but wraps every fs/n Hz, so it is unwrapped
        against a one-sample estimate, which is unambiguous across the whole
        passband though far too coarse to use on its own. Two rungs of that
        ladder put the residual under 100 Hz at every offset and SNR this
        decoder can otherwise handle, against the roughly 2 kHz the matched
        filter wants.
        """
        n = min(self._frame, len(x))
        if n < 8:
            return 0.0
        if len(x) > n:
            pw = np.convolve(np.abs(x) ** 2, np.ones(n) / n, mode='valid')
            x = x[int(np.argmax(pw)):][:n]

        def lag(m):
            return float(np.angle(np.sum(x[m:] * np.conj(x[:-m]))))

        w = lag(1)
        n0 = int(round(self.fs / self.deviation))
        if n0 < 2:
            return w
        for m in (n0, 2 * n0):
            if m >= len(x):
                break
            k = round((w * m - lag(m)) / (2 * np.pi))
            w = (lag(m) + 2 * np.pi * k) / m
        return w

    def _demod_iq(self, x):
        """Non-coherent FSK matched filter -- the optimal detector for this
        modulation, and several dB better than a limiter-discriminator once the
        signal is close to the noise.

        A discriminator measures the phase step between adjacent samples, so at
        low SNR one noisy sample pair produces a full-scale 'click' that the
        boxcar afterwards can only average, not undo. Correlating the burst
        against the two FSK tones instead accumulates a whole symbol of signal
        coherently before any decision is taken, and noise adds only on power.

        The residual carrier offset is removed first, estimated as the midpoint
        between the two rails of the discriminator rather than its mean, so a
        payload with more ones than zeros cannot drag the estimate toward the
        majority rail.
        """
        x = np.asarray(x, dtype=np.complex128)
        w_off = self._carrier_offset(x)
        self._offset_hz = w_off * self.fs / (2 * np.pi)
        n = np.arange(len(x))
        x = x * np.exp(-1j * w_off * n)
        w = 2 * np.pi * self.deviation / self.fs
        k = np.ones(max(1, int(round(self.sps))))
        a = np.convolve(x * np.exp(-1j * w * n), k, mode='same')
        b = np.convolve(x * np.exp(+1j * w * n), k, mode='same')
        return np.abs(a) - np.abs(b)

    def _demod(self, x):
        """Turn a burst into a stream of symbol-rate soft decisions.

        The offset between the receiver's centre and the transmitter shows up as
        a DC term on the discriminator output, and the slicer has to sit at zero
        for the bits to come out right. Estimate that DC as the midpoint between
        the two FSK rails, taken as robust percentiles, rather than as the mean:
        a payload carrying more ones than zeros drags the mean toward the
        majority rail and biases every subsequent decision.
        """
        if self.mode == 'iq':
            return self._demod_iq(x)
        d = np.asarray(x, dtype=np.float64)
        lo, hi = np.percentile(d, (10, 90))
        d = d - 0.5 * (lo + hi)
        # Matched filter over one symbol period. Slicing a single sample out of
        # every twenty throws away 13 dB of the available energy and is what
        # leaves scattered single-bit errors in an otherwise good burst.
        n = max(1, int(round(self.sps)))
        return np.convolve(d, np.ones(n) / n, mode='same')

    def _sample(self, d, phase, polarity):
        """Sample `d` at symbol centres, allowing a fractional start phase."""
        n = int((len(d) - phase) / self.sps)
        if n <= 0:
            return np.empty(0, dtype=np.float64)
        pos = phase + np.arange(n) * self.sps
        return np.interp(pos, np.arange(len(d)), d) * polarity

    def _bits_at_phase(self, d, phase, polarity):
        v = self._sample(d, phase, polarity)
        return ''.join('1' if q > 0 else '0' for q in v)

    def _sync_positions(self, arr):
        """Offsets whose next 16 bits are within SYNC_TOL of the sync word."""
        s = _SYNC_ARR
        agree = (np.correlate(arr, s, 'valid')
                 + np.correlate(1 - arr, 1 - s, 'valid'))
        return np.flatnonzero(agree >= len(s) - SYNC_TOL)

    def _extract(self, bits):
        """Yield payloads for every preamble-backed sync match in `bits`."""
        need = self.packet_len_bytes * 8
        arr = np.frombuffer(bits.encode('ascii'), dtype=np.uint8) - 48
        out = []
        last = -1
        for pos in self._sync_positions(arr):
            i = int(pos)
            if (i <= last or i < PREAMBLE_CHECK_BITS
                    or i + 16 + need > len(bits)):
                continue
            # Count transitions rather than demanding a perfect run: a marginal
            # burst puts the odd bit error in the preamble too, and insisting on
            # all 31 transitions throws away packets that decode fine. Five
            # slips still leaves the chance of noise faking both this and a
            # 14-of-16 sync at well under 1e-8 per bit position.
            pre = arr[i - PREAMBLE_CHECK_BITS:i]
            if int(np.count_nonzero(pre[1:] != pre[:-1])) < \
                    PREAMBLE_CHECK_BITS - 5:
                continue
            pb = bits[i + 16:i + 16 + need]
            out.append((i, bytes(int(pb[b:b + 8], 2)
                                 for b in range(0, need, 8))))
            last = i + 16 + need
        return out

    def _best_framing(self, d):
        """Recover symbol timing, then frame.

        With the matched filter in place the mean magnitude of the sampled
        stream peaks at the correct symbol phase -- that is the eye opening --
        so rank the quarter-sample phases by it and try the most open ones first
        instead of taking whichever phase happens to frame.

        Both polarities are tried because an I/Q swap, MODEM_MAP_CONTROL's
        TX_INV bit, or a receiver that inverts its discriminator output flips
        the sense of every bit.
        """
        ranked = []
        for polarity in (1, -1):
            for phase in np.arange(0.0, self.sps, 0.25):
                v = self._sample(d, phase, polarity)
                if len(v):
                    ranked.append((float(np.mean(np.abs(v))), polarity,
                                   float(phase)))
        ranked.sort(reverse=True)
        # Keep the best framing, not the first one that happens to frame. The
        # 19 bytes each layout leaves known -- the timestamp now, the 0xC3
        # padding before it -- are a free 152-bit check on every candidate, and
        # at the edge of range a neighbouring phase routinely decodes a packet
        # cleanly that the highest-ranked one gets wrong.
        best_got, best_pol, best_ph, best_score = [], None, None, -1
        full = TS_LEN
        for _, polarity, phase in ranked:
            got = self._extract(self._bits_at_phase(d, phase, polarity))
            if not got:
                continue
            score = max(self._layout(p)[0] for _, p in got)
            if score > best_score:
                best_got, best_pol, best_ph, best_score = (got, polarity,
                                                           phase, score)
            if score >= full:
                break        # a perfect timestamp; nothing can beat it
        return best_got, best_pol, best_ph

    @staticmethod
    def _layout(payload):
        """Score this payload against both known layouts, best first.

        The frame carries no CRC, so the 19 bytes of known content each layout
        leaves are the only integrity check available -- the timestamp's four
        separators and fifteen digits in the current format, the 19 bytes of
        0xC3 padding in the one before it. Nineteen known bytes is 152 bits, so
        a misframed or bit-damaged burst has no real chance of reproducing
        them, which makes this both the packet's integrity check and the
        discriminator between candidate symbol timings.

        Returns (score out of 19, True if this is the current layout).
        """
        ts = payload[:TS_LEN]
        modern = 0
        if len(ts) >= TS_LEN:
            for c, m in zip(ts, TS_MASK):
                if m == 'd':
                    modern += (48 <= c <= 57)
                else:
                    modern += (c == ord(m))
        legacy = sum(1 for c in payload[LEGACY_LEN:] if c == PAD_BYTE)
        if legacy > modern:
            return int(legacy), False
        return int(modern), True

    @staticmethod
    def _ts_text(ts):
        """The timestamp field as printable text, damage and all."""
        return ''.join(chr(c) if 32 <= c < 127 else '?' for c in ts)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def _decode_burst(self, abs_start, seg, hot_ms=0.0):
        self.nburst += 1
        t = abs_start / self.fs
        snr = 10 * np.log10(max(float(np.mean(self._power(seg))), 1e-30)
                            / max(self._floor, 1e-30))
        self._snr_db = snr
        packets, polarity, phase = self._best_framing(self._demod(seg))
        if not packets:
            # Report the offset too: a burst that is consistently tens of kHz
            # off is a tuner-correction problem, not a decoder problem, and that
            # is invisible from the SNR alone.
            self._say('[%s] burst t=%.3f s, %.1f ms above floor, %.1f dB, '
                      'offset %+.1f kHz -- no preamble+sync match.'
                      % (_stamp(), t, hot_ms, snr, self._offset_hz / 1e3))
            return
        for _, payload in packets:
            self.npkt += 1
            self._t_lastpkt = time.time()
            print('[%s] packet %d  t=%.3f s  %.1f ms  %.1f dB  '
                  '(polarity %+d, phase %.2f, offset %+.1f kHz)'
                  % (_stamp(), self.npkt, t, hot_ms, snr, polarity, phase,
                     self._offset_hz / 1e3))
            print('  ' + payload.hex(' '))
            self._report_packet(payload)

    def _report_packet(self, payload):
        """Decode the SensorPacket_t in Core/Inc/sensor_packet.h.

        45 packed little-endian bytes: a 19-character ASCII timestamp that is
        not null-terminated, then the 26 bytes of telemetry. A packet in the
        older layout -- telemetry first, 19 bytes of 0xC3 after it -- is
        recognised and reported as such rather than being read through the
        wrong struct, which is what turns a packet counter into a ten-digit
        number and every reading into nonsense.
        """
        if len(payload) < PACKET_LEN:
            return
        score, modern = self._layout(payload)
        ok = score == TS_LEN
        if modern:
            (ts, counter, temp, hum, lat, lon, wind,
             direction) = struct.unpack(PACKET_FMT, payload[:PACKET_LEN])
            stamp = self._ts_text(ts)
        else:
            (counter, temp, hum, lat, lon, wind,
             direction) = struct.unpack(LEGACY_FMT, payload[:LEGACY_LEN])
            stamp = ''
        print('  %scounter=%u temp=%.1f C hum=%.1f %% lat=%.5f '
              'lon=%.5f wind=%.1f m/s dir=%u deg'
              % ('time=%s ' % stamp if modern else '', counter, temp, hum,
                 lat, lon, wind, direction))
        if not modern:
            self._say_once('legacy',
                           '  (pre-timestamp payload layout: telemetry first, '
                           '0xC3 padding after it. This recording predates the '
                           'firmware that added the timestamp field.)')
        if not ok:
            self.nbad += 1
            print('  WARNING: %s -- bit errors in this packet.'
                  % ('timestamp field is not HH-MM-SS,DD-MM-YYYY' if modern
                     else 'tail padding is not all 0xC3'))
        # The counter is the only sequencing the frame carries, so a gap in it
        # is the one way to notice a packet that was missed entirely rather than
        # received badly. Trust it only when the known bytes say the frame is
        # clean, and ignore a decrease, which is the transmitter restarting.
        if ok:
            if (self._last_counter is not None
                    and counter > self._last_counter + 1):
                gap = counter - self._last_counter - 1
                self.nlost += gap
                print('  MISSED %d packet(s): counter jumped %u -> %u.'
                      % (gap, self._last_counter, counter))
            self._last_counter = counter
        if self.on_packet is not None:
            self.on_packet(payload, dict(timestamp=stamp, counter=counter,
                                         temp=temp, hum=hum, lat=lat, lon=lon,
                                         wind=wind, direction=direction,
                                         ts_ok=ok, snr_db=self._snr_db,
                                         offset_hz=self._offset_hz,
                                         layout='current' if modern
                                         else 'legacy'))

    def _maybe_heartbeat(self):
        """Say something periodically, so silence is distinguishable from a
        crashed or mis-tuned receiver."""
        if self.heartbeat_s <= 0 or self.quiet:
            return
        now = time.time()
        if now - self._t_heartbeat < self.heartbeat_s:
            return
        self._t_heartbeat = now
        quiet = ('none yet' if self._t_lastpkt is None
                 else '%.0f s ago' % (now - self._t_lastpkt))
        floor = ('learning' if not self._floor
                 else '%.1f dBFS' % (10 * np.log10(max(self._floor, 1e-30))))
        print('[%s] listening: %d packet(s), %d burst(s), %d bad, %d lost; '
              'noise floor %s; peak %.2f; last packet %s'
              % (_stamp(), self.npkt, self.nburst, self.nbad, self.nlost,
                 floor, self._peak, quiet))
        if self._peak >= 0.98:
            print('  input is at full scale -- the front end is clipping. '
                  'Reduce --gain until the peak sits near 0.5.')
        self._peak = 0.0

    def _say(self, msg):
        if not self.quiet:
            print(msg)

    def _say_once(self, key, msg):
        """Print msg the first time only. For facts about the stream that are
        worth stating but not worth repeating once per packet."""
        if key not in self._said:
            self._said.add(key)
            print(msg)


def _first_run(hot, n):
    """Index where the first run of `n` consecutive True values starts, or -1.

    A sliding count over the boolean array; `n` is small (two symbols) and the
    array is one block long, so the cumulative sum costs nothing measurable.
    """
    if n <= 1:
        return int(np.argmax(hot)) if hot.any() else -1
    if len(hot) < n:
        return -1
    c = np.concatenate(([0], np.cumsum(hot, dtype=np.int64)))
    idx = np.flatnonzero(c[n:] - c[:-n] >= n)
    return int(idx[0]) if len(idx) else -1


def _stamp():
    t = time.time()
    return time.strftime('%H:%M:%S', time.localtime(t)) + '.%03d' % (t % 1 * 1000)
