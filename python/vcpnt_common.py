#!/usr/bin/env python3
"""
vcpnt_common.py - Shared helpers for decoding m+p VibControl (VcpNT) binary
measurement files (.rsn sine, .rrn random, .rcs shock).

Format facts (reverse-engineered from real files, VcpNT 2.16.18.0):
  * Global header (first 64 bytes) holds version fields, channel-record
    size and a list of section offsets.
  * The file consists of N data sections (one per measurement snapshot),
    then a text section containing the full INI-style header as embedded
    strings, then a log of 600-byte records holding timestamps.
  * Data sections hold fixed-size channel records: name + header +
    float32 magnitude (+phase) arrays in SI units:
      - sine spectra: mags stored as m/s^2 (= g * 9.81), phases in degrees
      - random PSDs : mags stored as (m/s^2)^2/Hz (= g^2/Hz * 9.81^2)
      - shock time : volts, stored as-is
  * Numbers in the .txt export use German decimal commas and "%.7g"
    (X axis for sine spectra: 4 decimals, trailing zeros stripped).
"""
import os
import re
import struct
import datetime

G = 9.81                # g used by the exporter (m/s^2 per g)
G2 = G * G              # 96.2361

# ---------------------------------------------------------------------------
# number formatting (German locale, matches the VcpNT .txt export)
# ---------------------------------------------------------------------------

def fmt_g(value):
    """Format like the exporter: '%.6g' with comma as decimal separator."""
    s = "%.6g" % value
    return s.replace(".", ",")


def fmt_x_sine(value):
    """Spectrum X axis: 6 significant digits ('%.6g'), comma decimal."""
    s = "%.6g" % value
    return s.replace(".", ",")


def fmt_x_other(value):
    """PSD / time X axis: same %.6g style as the sine axis."""
    return fmt_x_sine(value)


# ---------------------------------------------------------------------------
# generic binary parsing helpers
# ---------------------------------------------------------------------------

def u32(data, off):
    return struct.unpack_from("<I", data, off)[0]


def find_all(data, pattern):
    out = []
    pos = 0
    while True:
        j = data.find(pattern, pos)
        if j < 0:
            break
        out.append(j)
        pos = j + 1
    return out


def read_cstr(data, off, maxlen=256):
    end = data.find(b"\x00", off, off + maxlen)
    if end < 0:
        end = off + maxlen
    return data[off:end].decode("latin-1")


# ---------------------------------------------------------------------------
# file-level structure
# ---------------------------------------------------------------------------

class VcpntFile:
    """Parses the global header of a VcpNT binary measurement file."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.version_marker = u32(self.data, 0)      # e.g. 10008 (.rsn), 10259 (.rrn), 9481 (.rcs)
        self.record_size = u32(self.data, 4)         # 8997 (.rsn/.rrn), 9223 (.rcs) - informational
        # offset list from bytes 12..60 (pairs of u32, high word always 0)
        self.offsets = []
        for off in range(12, 60, 8):
            a = u32(self.data, off)
            if a:
                self.offsets.append(a)
        self.stem = os.path.basename(path)
        self.base = os.path.splitext(self.stem)[0]

    # -- text section -----------------------------------------------------
    def extract_header_text(self):
        """Extract the embedded INI header text.

        Sine/random files embed it starting at 'Run.Type:' (the .txt adds
        'VcpNT-VERSION', a blank line and '[Description]' in front).
        Shock files embed 'VcpNT-VERSION 2.16.18.0' directly.
        Returns the list of text lines as they appear in the .txt header
        (including the synthetic VcpNT-VERSION / [Description] lines and
        blank lines between sections), i.e. everything before
        '[Measurements]' in the reference export.

        Returns None when the file stores NO embedded setup text at all
        (seen in 2.17.x-era files whose text section was stripped, e.g.
        some 'selected measurement' copies): callers then fall back to
        synthesized defaults and decode the measurement data only.
        """
        data = self.data
        lines = []
        i = data.find(b"Run.Type:")
        if i < 0:
            # shock style: the text section starts with the version string
            i = data.find(b"VcpNT-VERSION")
            if i < 0:
                return None
            j = data.find(b"\x00", i)
            if j < 0:
                j = i + 30
            ver = data[i:j].decode("latin-1")
            lines.append(ver)
            lines.append("")
            lines.append("[Description]")
            blob = data[j:].decode("latin-1", errors="replace")
        else:
            ver = data[:40].decode("latin-1", errors="replace")
            m = re.search(r"VcpNT-VERSION\s+([\d.]+)", ver)
            if m:
                lines.append("VcpNT-VERSION " + m.group(1))
            else:
                lines.append("VcpNT-VERSION 2.16.18.0")
            lines.append("")
            lines.append("[Description]")
            blob = data[i:].decode("utf-8", errors="replace")

        # collect printable ini-like lines
        text_lines = []
        for l in blob.split("\n"):
            s = l.rstrip("\r").rstrip("\x00")
            if not s.strip():
                continue
            pr = sum(1 for c in s if 32 <= ord(c) < 127)
            if pr / max(len(s), 1) > 0.85 and len(s) < 300 and len(s) >= 4:
                if s.startswith("[") or any(c.isalpha() for c in s):
                    text_lines.append(s)

        # reconstruct with blank lines before each section header (the .txt
        # export puts a blank line before every section except [Description])
        for s in text_lines:
            if s.startswith("[") and s.endswith("]"):
                lines.append("")
            lines.append(s)
        return lines

    # -- data sections -----------------------------------------------------
    # Two observed section-start markers (the low u32 words differ by the
    # recording program's version family):
    #   b"\xe0\x2d\x00\x00\x01\x03\x00\x00" - 2.16.x / 2.17.x-era files
    #                                        (version markers 9481/9482)
    #   b"\xe0\x37\x00\x00\x02\x03\x00\x00" - 2.18.x-era files (marker 9483;
    #                                        New_Data corpus, 2026-09-09)
    _SECTION_MARKERS = (
        b"\xe0\x2d\x00\x00\x01\x03\x00\x00",
        b"\xe0\x37\x00\x00\x02\x03\x00\x00",
    )

    def find_sections(self, ext_hint=None):
        """Locate data sections. Each section starts with a fixed
        8-byte header (see _SECTION_MARKERS) followed by the source path
        string ("C:\\..." on the machines seen)."""
        data = self.data
        starts = []
        for marker in VcpntFile._SECTION_MARKERS:
            pos = 0
            while True:
                j = data.find(marker, pos)
                if j < 0:
                    break
                # a real section start is followed by the drive path string
                # (e.g. "C:\\DATA\\..." or "C:\\Users\\...")
                if (data[j + 8:j + 10] == b"C:"
                        or (data[j + 8:j + 9].isalpha()
                            and data[j + 9:j + 11] == b":\\")):
                    starts.append(j)
                pos = j + 1
        txt_off = self._text_section_start()
        starts = sorted(s for s in starts if s < txt_off)
        ends = starts[1:] + [txt_off]
        return list(zip(starts, ends))

    def _text_section_start(self):
        data = self.data
        # sine/random: 'Run.Type:' ; shock: 'VcpNT-VERSION'
        for marker in (b"Run.Type:", b"VcpNT-VERSION"):
            i = data.find(marker)
            if i >= 0:
                return i
        return len(data)

    # -- log records (timestamps) ------------------------------------------
    def measurement_timestamps(self, kind, count):
        """Return a list of 'count' measurement timestamps as
        datetime.datetime (CET), extracted from the end-of-file log."""
        if kind == "shock":
            # the measurement date = the first log record after the
            # run-start group (the first record whose time differs)
            recs = self._log_records()
            ts = None
            if recs:
                base = recs[0].get("dt")
                for r in recs:
                    if r.get("dt") and r["dt"] != base:
                        ts = r["dt"]
                        break
                if ts is None:
                    ts = base
            return [ts] * count if ts else [None] * count

        recs = self._log_records()
        if kind == "random":
            meas = [r for r in recs if r["type"] == 65]
            out = [r.get("dt") for r in meas[:count]]
            if len(meas) < count and recs:
                # the first measurement predates the log; use the first
                # log record as the closest stored timestamp
                out = [recs[0].get("dt")] + out[:count - 1]
            while len(out) < count:
                out.append(None)
            return out

        # sine: type 1120 marks the first measurement of a run; type 40
        # marks every following measurement; type 24 is the run-end record
        # (only used as a measurement when the run ended exactly at one).
        m1120 = [r for r in recs if r["type"] == 1120]
        m40 = [r for r in recs if r["type"] == 40]
        m24 = [r for r in recs if r["type"] == 24]
        out = []
        if m1120:
            recs_use = m1120 + m40
            if len(recs_use) < count and m24:
                recs_use = recs_use + m24
            out = [r.get("dt") for r in recs_use[:count]]
        else:
            # the first measurement of a run that started before logging
            # has no record of its own; the first event after the
            # run-start group is the closest stored timestamp
            runstart = None
            for r in recs:
                if r["type"] not in (149, 150, 75):
                    runstart = r.get("dt")
                    break
            out = [runstart] + [r.get("dt") for r in m40[:count - 1]]
        while len(out) < count:
            out.append(None)
        return out

    def _log_records(self):
        """Scan the tail of the file (after the text section) for log
        records carrying timestamps.

        The universal layout is the 8x u16 date breakdown:
          [min,sec][day,hour][year,mon][doy,wday]  (sine scan, offset +0)
          [unix ts][0][min,sec][day,hour][year,mon][doy,wday][type]
                                                  (random/sweep, +8)
        The breakdown therefore exists in every record type; scanning for
        it works for all formats. A type sanity bound (< 5000) separates
        real records (types 18..1120) from coincidental byte patterns.
        Scans absolute file offsets with step 1 (records are only
        2-byte aligned to the file start)."""
        data = self.data
        region_start = self._text_section_start()
        recs = []
        for off in range(region_start, len(data) - 20):
            sec, mn, hr, day, mon, yr, wd, doy = struct.unpack_from("<8H", data, off)
            if not (sec < 60 and mn < 60 and hr < 24 and 1 <= day <= 31
                    and mon < 12 and 100 <= yr <= 200 and wd < 8 and 0 <= doy < 366):
                continue
            typ = struct.unpack_from("<I", data, off + 16)[0]
            if typ >= 5000:
                continue
            try:
                dt = datetime.datetime(yr + 1900, mon + 1, day, hr, mn, sec)
            except ValueError:
                continue
            recs.append({"off": off, "dt": dt, "type": typ})
        return recs

    def _latest_log_timestamp(self):
        recs = self._log_records()
        valid = [r["dt"] for r in recs if r.get("dt")]
        if valid:
            return max(valid)
        return None


def fmt_date(dt):
    return dt.strftime("%m-%d-%Y") if dt else "01-01-2025"


def fmt_time(dt):
    return dt.strftime("%H:%M:%S") if dt else "00:00:00"
