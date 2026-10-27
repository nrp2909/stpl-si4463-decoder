# -*- coding: utf-8 -*-
"""
CSV logging for the live Si4463 decoder.

One row per decoded packet, written and flushed the moment the packet is
reported, so the file is a live record rather than something that only exists
once the program is stopped. Excel opens it directly -- the header carries a
UTF-8 BOM, which is what stops Excel mangling the file, and every value is
written in a form Excel reads as a number or a date rather than as text.

Deliberately CSV rather than a real .xlsx. Writing xlsx needs openpyxl, and the
TNC's whole point is that it depends on nothing but numpy and sounddevice so
PyInstaller can freeze it into one .exe the way Direwolf ships one; a workbook
writer is also the wrong shape for an append-one-row-per-second log, because it
has to rewrite the entire archive on every save. If a workbook is wanted at the
end, open the CSV in Excel and save it as one.

Hooked in through Si4463Decoder's on_packet callback, so the decoder itself
stays unaware of it:

    from si4463_csv import PacketCsv
    log = PacketCsv('packets.csv')
    dec = Si4463Decoder(..., on_packet=log)
    ...
    log.close()
"""

import csv
import io
import os
import time

# Where a log goes when nobody says. Beside the scripts rather than in the
# working directory, so the file is in the same place whether the decoder was
# double-clicked, launched from a shortcut, or run from a prompt somewhere else
# entirely -- "where did it go" should not have a different answer each time.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')


def default_log_path(prefix='packets'):
    """Path for this run's log: logs/<prefix>_YYYYmmdd_HHMMSS.csv, beside the
    scripts. One file per run, named for when the run started."""
    return os.path.join(LOG_DIR, '%s_%s.csv'
                        % (prefix, time.strftime('%Y%m%d_%H%M%S')))


# Column order.
#
# The two time columns are recorded, not reconciled. 'received' is this
# machine's clock at the moment the packet was decoded, and 'timestamp' is
# whatever ASCII the payload carried, copied through byte for byte. Nothing
# here compares them, corrects one by the other, or drops a row because they
# disagree: a log that quietly adjusts what it was given is no longer evidence.
# If the transmitter's clock is wrong, the column shows it as wrong.
COLUMNS = (
    'received',            # YYYY-MM-DD HH:MM:SS.mmm, local clock
    'timestamp',           # HH-MM-SS,DD-MM-YYYY, verbatim from the payload
    'counter',
    'temperature_c',
    'humidity_pct',
    'latitude_deg',
    'longitude_deg',
    'wind_speed_mps',
    'wind_direction_deg',
    'snr_db',
    'offset_hz',
    'ok',                  # 1 if the packet's known bytes framed intact
    'layout',              # 'current', or 'legacy' for a pre-timestamp frame
)


class PacketCsv(object):
    """Append one row per packet to a CSV file.

    Parameters
    ----------
    path : str
        File to write. It is opened for append, so restarting the decoder
        continues an existing log instead of destroying it, and the header is
        written only when the file is new or empty.
    include_bad : bool
        Whether to log packets whose timestamp field did not frame intact.
        Default True: a row with ok=0 is how a marginal link shows itself, and
        dropping those rows silently makes the log look better than the link
        is. Filter on the ok column in Excel if only clean packets are wanted.
    """

    def __init__(self, path, include_bad=True):
        # Resolved to an absolute path, and kept that way, because self.path is
        # what both front ends print on start and on stop. A relative --csv
        # lands relative to the working directory of whatever launched the
        # decoder, which is not always the folder the scripts live in -- so
        # echoing back the bare name that was typed answers the wrong question.
        # It says what the file is called, not where it went.
        path = os.path.abspath(path)
        self.path = path
        self.include_bad = bool(include_bad)
        self.nrows = 0
        # Create the containing folder rather than failing on it. The default
        # path names a logs/ directory that will not exist on a first run, and
        # a decoder that refuses to start because it could not make a folder is
        # a decoder that loses the packets it was started to catch.
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        # newline='' is required by the csv module on Windows; without it every
        # row is followed by a blank one. utf-8-sig puts the BOM Excel wants at
        # the front, and only when the file is new -- appending a second BOM
        # part-way through would show up as a stray character in a cell.
        self._fh = io.open(path, 'a', newline='',
                           encoding='utf-8-sig' if new else 'utf-8')
        self._w = csv.writer(self._fh)
        if new:
            self._w.writerow(COLUMNS)
            self._fh.flush()

    def __call__(self, payload, f):
        """on_packet hook: append this packet, unless it is bad and filtered."""
        ok = bool(f.get('ts_ok', True))
        if not ok and not self.include_bad:
            return
        t = time.time()
        received = (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))
                    + '.%03d' % (t % 1 * 1000))
        self._w.writerow((
            received,
            f.get('timestamp', ''),
            f['counter'],
            '%.2f' % f['temp'],
            '%.2f' % f['hum'],
            '%.6f' % f['lat'],
            '%.6f' % f['lon'],
            '%.2f' % f['wind'],
            f['direction'],
            '%.1f' % f.get('snr_db', 0.0),
            '%.0f' % f.get('offset_hz', 0.0),
            1 if ok else 0,
            f.get('layout', 'current'),
        ))
        # Flush every row. A packet arrives about once a second, so the cost is
        # nothing, and it is what makes the file readable while the decoder is
        # still running -- an unflushed row sits in a buffer for however long
        # the next 8 kB takes to accumulate, which at this rate is minutes.
        self._fh.flush()
        self.nrows += 1

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
