"""
Embedded Python Block: Si4463 bench-test packet extractor.

Takes the *complex* baseband stream from the frequency-translating filter,
finds each transmit burst by its power envelope, removes the residual carrier
offset burst-by-burst, FSK-demodulates through a matched filter, recovers
symbol timing by taking the sample phase with the widest eye, and prints each
45-byte payload decoded as a SensorPacket_t.

Three things about the on-air format that a generic FSK receiver has to get
right, all of them documented in Core/Inc/si4463_config.h:

  * The sync word on air is 0x2D 0xD4, not the 0xB4 0x2B that appears in
    SYNC_BITS_31_24/23_16. AN626 3.2.3: preamble and sync bytes always go out
    LSB-first regardless of BIT_ORDER, so each register byte is bit-reversed
    on its way to the antenna. Only the data field follows BIT_ORDER, which is
    0 here, so payload bytes are MSB-first and need no reversal.
  * The preamble is 16 bytes of 0xAA (PREAMBLE_TX_LENGTH=0x10 with
    PREAMBLE_CONFIG bit D4 set, i.e. the length is in bytes, not nibbles), so
    128 alternating bits precede the sync word. Requiring some of that run
    before accepting a sync match is what keeps noise from producing false
    packets: a bare 16-bit sync pattern turns up by chance roughly every 65 k
    bits, which over a multi-second capture is many times per file.
  * Whitening is off (PKT_FIELD_1_CONFIG=0x04 is PN_START, not WHITEN), so the
    payload is on air verbatim and needs no de-scrambling.

The frame is therefore 16 x 0xAA, then 0x2D 0xD4, then 45 payload bytes with
no CRC appended (PKT_FIELD_1_CRC_CONFIG=0x80 enables the CRC engine but does
not set SEND_CRC) -- 63 bytes, 50.4 ms at 10 kbps.
"""

import struct

import numpy as np
from gnuradio import gr

# 0x2D 0xD4, MSB-first, as it appears on air.
SYNC_BITS = '0010110111010100'

# How much of the 128-bit 0xAA preamble must immediately precede a sync match
# for it to be believed. 32 bits leaves plenty of the run unexamined, so a late
# burst edge or a slow AGC does not cost a packet, while still making a chance
# hit vanishingly unlikely.
PREAMBLE_CHECK_BITS = 32

# Payload layout of SensorPacket_t in Core/Inc/sensor_packet.h. The frame opens
# with a fixed-width ASCII timestamp -- HH-MM-SS,DD-MM-YYYY, 19 characters with
# no terminator -- followed by the 26 bytes of binary telemetry, which is
# exactly the 45-byte frame with no 0xC3 padding left over.
TS_LEN = 19
PACKET_FMT = '<%dsIfffffH' % TS_LEN
PACKET_LEN = struct.calcsize(PACKET_FMT)
TS_MASK = 'dd-dd-dd,dd-dd-dddd'
# The layout sent before the timestamp was added: telemetry first, then 19
# bytes of 0xC3. Recordings of both exist, so both are recognised.
LEGACY_FMT = '<IfffffH'
LEGACY_LEN = struct.calcsize(LEGACY_FMT)


class blk(gr.sync_block):
    def __init__(self, decim_rate=200000, symbol_rate=10000, deviation=20000,
                 packet_len_bytes=45, burst_threshold_db=6.0):
        gr.sync_block.__init__(self, name='Si4463 Packet Decoder',
                               in_sig=[np.complex64], out_sig=None)
        self.fs = float(decim_rate)
        self.symbol_rate = float(symbol_rate)
        self.deviation = float(deviation)
        self.sps = self.fs / self.symbol_rate
        self.packet_len_bytes = int(packet_len_bytes)
        self.burst_threshold_db = float(burst_threshold_db)
        self._chunks = []

    def work(self, input_items, output_items):
        self._chunks.append(input_items[0].copy())
        return len(input_items[0])

    def stop(self):
        self._decode()
        self._chunks = []
        return True

    # ------------------------------------------------------------------
    # burst detection
    # ------------------------------------------------------------------

    def _find_bursts(self, x):
        """Return (start, stop) sample indices of each candidate burst."""
        win = max(1, int(self.sps / 2))
        env = np.convolve(np.abs(x) ** 2, np.ones(win) / win, mode='same')
        floor = np.median(env)
        if floor <= 0:
            return []
        thresh = floor * 10.0 ** (self.burst_threshold_db / 10.0)

        hot = env > thresh
        if not hot.any():
            return []
        edges = np.diff(hot.astype(np.int8))
        starts = list(np.where(edges == 1)[0] + 1)
        stops = list(np.where(edges == -1)[0] + 1)
        if hot[0]:
            starts.insert(0, 0)
        if hot[-1]:
            stops.append(len(hot))

        # A fade part-way through a burst drops the envelope below the
        # threshold and splits one packet into two fragments, neither of which
        # holds a whole preamble+sync+payload. Rejoin anything separated by
        # less than a tenth of a frame before applying the length test.
        merged = []
        max_gap = int(50 * self.sps)
        for a, b in zip(starts, stops):
            if merged and a - merged[-1][1] <= max_gap:
                merged[-1][1] = b
            else:
                merged.append([a, b])

        # A whole frame is 63 bytes; insist on at least the preamble plus sync
        # so a brief spur cannot masquerade as a packet.
        min_len = int(18 * 8 * self.sps)
        guard = int(4 * self.sps)
        return [(max(0, a - guard), min(len(x), b + guard))
                for a, b in merged if b - a >= min_len]

    # ------------------------------------------------------------------
    # demodulation
    # ------------------------------------------------------------------

    def _demod(self, x):
        """FSK-demodulate and remove the residual carrier offset.

        The offset between the recording's centre and the transmitter shows up
        as a DC term on the demodulated stream, and the slicer has to sit at
        zero for the bits to come out right. Estimate it as the midpoint
        between the two FSK rails, taken as robust percentiles, rather than as
        the mean or median of the stream: a burst whose payload happens to
        carry more ones than zeros drags mean and median toward the majority
        rail, which biases every subsequent decision. The rail midpoint does
        not care how the symbols are distributed.
        """
        d = np.angle(x[1:] * np.conj(x[:-1])) * (self.fs / (2 * np.pi * self.deviation))
        lo, hi = np.percentile(d, (10, 90))
        d = d - 0.5 * (lo + hi)
        # Matched filter for the NRZ symbols: integrate over one symbol period
        # rather than slicing a single sample. Picking one sample out of every
        # twenty throws away 13 dB of the available energy and is what leaves
        # scattered single-bit errors in an otherwise good burst.
        n = max(1, int(round(self.sps)))
        return np.convolve(d, np.ones(n) / n, mode='same')

    def _sample(self, d, phase, polarity):
        """Sample `d` at symbol centres, allowing a fractional start phase."""
        n = int((len(d) - phase) / self.sps)
        if n <= 0:
            return np.empty(0, dtype=np.float32)
        pos = phase + np.arange(n) * self.sps
        return np.interp(pos, np.arange(len(d)), d) * polarity

    def _bits_at_phase(self, d, phase, polarity):
        v = self._sample(d, phase, polarity)
        return ''.join('1' if q > 0 else '0' for q in v)

    # ------------------------------------------------------------------
    # framing
    # ------------------------------------------------------------------

    def _extract(self, bits):
        """Yield payloads for every preamble-backed sync match in `bits`."""
        need = self.packet_len_bytes * 8
        out = []
        i = 0
        while True:
            i = bits.find(SYNC_BITS, i)
            if i < 0 or i + 16 + need > len(bits):
                break
            pre = bits[max(0, i - PREAMBLE_CHECK_BITS):i]
            # Count transitions rather than demanding a perfect run: a marginal
            # burst puts the odd bit error in the preamble too, and insisting
            # on all 31 transitions throws away packets that decode fine. Three
            # slips still leaves the chance of noise faking both this and the
            # 16-bit sync at around 1e-11 per bit position.
            ok = (len(pre) == PREAMBLE_CHECK_BITS and
                  sum(pre[k] != pre[k + 1] for k in range(len(pre) - 1))
                  >= PREAMBLE_CHECK_BITS - 4)
            if ok:
                pb = bits[i + 16:i + 16 + need]
                out.append((i, bytes(int(pb[b:b + 8], 2)
                                     for b in range(0, need, 8))))
                i += 16 + need
            else:
                i += 1
        return out

    def _best_framing(self, d):
        """Recover symbol timing, then frame.

        With the matched filter in place the mean magnitude of the sampled
        stream peaks at the correct symbol phase -- that is the eye opening --
        so rank the quarter-sample phases by it and try the most open ones
        first instead of taking whichever phase happens to frame.

        Both polarities are tried because an I/Q swap in the recording, or
        MODEM_MAP_CONTROL's TX_INV bit, flips the sense of the demodulator
        output and turns every bit upside down.
        """
        phases = np.arange(0.0, self.sps, 0.25)
        ranked = []
        for polarity in (1, -1):
            for phase in phases:
                v = self._sample(d, phase, polarity)
                if len(v):
                    ranked.append((float(np.mean(np.abs(v))), polarity, float(phase)))
        ranked.sort(reverse=True)

        for _, polarity, phase in ranked:
            got = self._extract(self._bits_at_phase(d, phase, polarity))
            if got:
                return got, polarity, phase
        return [], None, None

    # ------------------------------------------------------------------

    def _decode(self):
        if not self._chunks:
            print('No samples reached the decoder.')
            return
        x = np.concatenate(self._chunks)
        dur = len(x) / self.fs
        print('Captured %.2f s at %.0f Hz (%.1f samples/symbol).'
              % (dur, self.fs, self.sps))

        bursts = self._find_bursts(x)
        if not bursts:
            print('No burst found %.1f dB above the noise floor.'
                  % self.burst_threshold_db)
            self._report_spectrum(x)
            return

        print('Found %d burst(s):' % len(bursts))
        found = 0
        for n, (a, b) in enumerate(bursts, 1):
            seg = x[a:b]
            print('  burst %d: t=%.3f s, %.1f ms'
                  % (n, a / self.fs, (b - a) / self.fs * 1000.0))
            packets, polarity, phase = self._best_framing(self._demod(seg))
            if not packets:
                print('    no preamble+sync match -- burst too weak, or the '
                      'symbol rate / deviation do not match the transmitter.')
                continue
            for off, payload in packets:
                found += 1
                print('    packet %d (polarity %+d, phase %.2f):'
                      % (found, polarity, phase))
                print('      ' + payload.hex(' '))
                self._report_packet(payload)

        if found == 0:
            print('Bursts were found but none framed. Check symbol_rate, '
                  'deviation, and that center_offset puts the signal inside '
                  'the low-pass filter.')
            self._report_spectrum(x)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def _report_packet(self, payload):
        """Decode the SensorPacket_t in Core/Inc/sensor_packet.h.

        45 packed little-endian bytes: a 19-character ASCII timestamp that is
        not null-terminated, then the 26 bytes of telemetry. The timestamp
        filled the last of the frame, so the 0xC3 padding this used to check is
        gone and the shape of the timestamp is the only integrity check the
        format still offers -- four separators at fixed offsets and fifteen
        ASCII digits, which a damaged burst has no real chance of reproducing.
        """
        if len(payload) < PACKET_LEN:
            return
        modern = sum((48 <= c <= 57) if m == 'd' else (c == ord(m))
                     for c, m in zip(payload[:TS_LEN], TS_MASK))
        legacy = sum(1 for c in payload[LEGACY_LEN:] if c == 0xC3)
        if legacy > modern:
            (counter, temp, hum, lat, lon, wind, direction) = struct.unpack(
                LEGACY_FMT, payload[:LEGACY_LEN])
            head, ok = '', legacy == TS_LEN
            note = 'tail padding is not all 0xC3'
            tag = ('layout=legacy -- pre-timestamp firmware: 19 bytes of 0xC3 '
                   'padding sit where the RTC timestamp now goes')
        else:
            (ts, counter, temp, hum, lat, lon, wind, direction) = struct.unpack(
                PACKET_FMT, payload[:PACKET_LEN])
            head = 'time=%s ' % ''.join(chr(c) if 32 <= c < 127 else '?'
                                        for c in ts)
            ok = modern == TS_LEN
            note = 'timestamp field is not HH-MM-SS,DD-MM-YYYY'
            tag = 'layout=current'
        print('      %scounter=%u temp=%.1f C hum=%.1f %% lat=%.5f '
              'lon=%.5f wind=%.1f m/s dir=%u deg'
              % (head, counter, temp, hum, lat, lon, wind, direction))
        print('      %s' % tag)
        if not ok:
            print('      WARNING: %s -- bit errors in this packet.' % note)

    def _report_spectrum(self, x):
        """Print where the energy actually is, so center_offset can be fixed."""
        n = 1 << 14
        if len(x) < n:
            return
        acc = np.zeros(n)
        step = max(n, len(x) // 64)
        for k in range(0, len(x) - n, step):
            acc += np.abs(np.fft.fft(x[k:k + n] * np.hanning(n))) ** 2
        acc = np.fft.fftshift(acc)
        f = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / self.fs))
        # Ignore the SDR's DC spike when looking for the signal.
        mask = np.abs(f) > 2 * self.symbol_rate
        peak = f[mask][int(np.argmax(acc[mask]))]
        print('Strongest energy still inside the passband sits %+0.0f Hz from '
              'the filter centre; if that is the transmitter, add it to '
              'center_offset.' % peak)
        print('That figure only covers what the low-pass let through, so it '
              'cannot see a signal the filter already removed. If it does not '
              'lead anywhere, open the recording in SDR++ or an FFT and read '
              'the burst offset off the waterfall directly, then check the '
              'recording actually contains the bursts at all -- a capture '
              'centred on the wrong frequency looks exactly like a dead '
              'transmitter from in here.')
