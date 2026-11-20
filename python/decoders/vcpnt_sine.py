#!/usr/bin/env python3
"""
vcpnt_decoder.py - Sine (.rsn) decoder for m+p VibControl VcpNT files.

Usage:
    python3 vcpnt_decoder.py <file.rsn> [--out DIR] [--csv]

Writes:
    <name>.txt            - VcpNT-style decoded text export (all measurements)
    <name>_Measurement_N.csv - per-measurement CSV (channel name header,
                               type/units sub-headers, then the data)

Shared code lives in vcpnt_common.py (parent directory).

Format notes (reverse-engineered):
  * One data section per measurement snapshot. A section is
    [24548-byte header][N measured (STEST) channel records][4 tail
    records]. 2.16.x files have 24564 bytes before the first record and
    29 STEST records; 2.17.x-era files can carry any setup channel
    count (8, 16, 29, ...) and the record names sit 16 bytes later -
    but the NAME of record k is at (sec_start + 24564 + k*rec_size) and
    the float data at name+760 in BOTH layouts, so all offsets below
    are version-independent.
  * Record size = 776 + 8*n_points (n_points = 1024/2048/4096/...).
    The first records carry their channel name; unnamed response
    records follow the setup order.
  * Tail = 4 synthetic records: Coherence (M1) or Error (M2+), Drive,
    Control channel, History.
  * Magnitudes are stored in SI (m/s^2 = g * 9.81); the export shows g.
  * Some files (2.17.x 'selected measurement' copies) have NO embedded
    setup text section: their .txt header cannot be reproduced, so the
    decoder falls back to defaults and decodes the measurement data.
"""
import os
import re
import sys
import struct
import math
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vcpnt_common import (VcpntFile, fmt_g, fmt_x_sine, G, read_cstr)

REC_HEADER = 760            # offset of the float32 magnitude array from a record name
TAIL_RECORDS = 4            # Coherence/Error, Drive, Control channel, History


# ---------------------------------------------------------------------------
# header text parsing
# ---------------------------------------------------------------------------

class SineHeader:
    def __init__(self, lines):
        self.lines = lines
        self.channels = []      # list of dicts (setup channel definitions)
        self.breakpoints = []   # list of (freq_hz, accel_g)
        self.no_sweeps = 1
        self.show_rate = False
        self.sweep_rate = 0.0
        self.sweep_rate_source = None
        self.sweep_unit = "Oct/min"
        self.sweep_type = "logarithmic"
        self.sweep_directions = []   # from the [Schedule.Sweep.N] table
        self.has_sweep_table = False
        self.alarm_lo = -10.0
        self.alarm_hi = 10.0
        self.abort_lo = -6.0
        self.abort_hi = 6.0
        self.alarm_is_db = False
        cur = None
        for l in lines or []:
            if l.startswith("[Channel."):
                cur = {}
                self.channels.append(cur)
                continue
            if cur is None:
                if l.startswith("[Schedule.Sweep."):
                    self.has_sweep_table = True
                m = re.match(r"Sweep\.Direction:\t(\w+)", l)
                if m and self.has_sweep_table:
                    self.sweep_directions.append(m.group(1))
                m = re.match(r"Breakpoint\.AlarmLo:\t(-?[\d,]+)( dB)?", l)
                if m:
                    self.alarm_lo = float(m.group(1).replace(",", "."))
                    if m.group(2):
                        self.alarm_is_db = True
                m = re.match(r"Breakpoint\.AlarmHi:\t(-?[\d,]+)( dB)?", l)
                if m:
                    self.alarm_hi = float(m.group(1).replace(",", "."))
                m = re.match(r"Breakpoint\.AbortLo:\t(-?[\d,]+)", l)
                if m:
                    self.abort_lo = float(m.group(1).replace(",", "."))
                m = re.match(r"Breakpoint\.AbortHi:\t(-?[\d,]+)", l)
                if m:
                    self.abort_hi = float(m.group(1).replace(",", "."))
                m = re.match(r"Schedule\.NoSweeps:\t(\d+)", l)
                if m:
                    self.no_sweeps = int(m.group(1))
                m = re.match(r"Schedule\.SweepTableActive\s+(\w+)", l)
                if m:
                    self.show_rate = m.group(1) != "Yes"
                m = re.match(r"Schedule\.SweepRate:\s*(\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s+([\w/]+)", l)
                if m:
                    self.sweep_rate = float(m.group(1).replace(",", "."))
                    self.sweep_unit = m.group(2)
                    self.sweep_type = "linear" if "Hz" in m.group(2) else "logarithmic"
                    self.sweep_rate_source = (
                        "Schedule.SweepRate" if self.sweep_unit in ("Oct/min", "Hz/min") else None
                    )
                m = re.match(r"Breakpoint\.Freq:\t([\d,]+) Hz", l)
                if m:
                    self.breakpoints.append((float(m.group(1).replace(",", ".")), None))
                m = re.match(r"Breakpoint\.Accel:\t([\d,]+) g", l)
                if m and self.breakpoints:
                    self.breakpoints[-1] = (self.breakpoints[-1][0],
                                            float(m.group(1).replace(",", ".")))
                continue
            m = re.match(r"Channel\.(Number|Name|Type|SubType|Filtermode):\t?(.*)", l)
            if m:
                cur[m.group(1)] = m.group(2).strip()

    def freq_range(self):
        fs = [b[0] for b in self.breakpoints if b[0]]
        if len(fs) >= 2:
            return fs[0], fs[-1]
        return 10.0, 2000.0

    def setup_channel_names(self):
        return [c.get("Name", "") for c in self.channels if c.get("Name")]


# ---------------------------------------------------------------------------
# file geometry (record size / point count / STEST record count)
# ---------------------------------------------------------------------------

def _probe_offsets(data, s0, probes, lo=20000, hi=3000000):
    """Every offset of each probe string inside the first records region
    (a name prefix like 'Control_' appears once per record, so collecting
    ALL occurrences yields the record grid even when no full names are
    known)."""
    offs = []
    for pr in probes:
        if len(pr) < 3:
            continue
        pos = s0 + lo
        end = s0 + hi
        n = 0
        while n < 300:
            i = data.find(pr, pos, end)
            if i < 0:
                break
            offs.append(i)
            pos = i + 1
            n += 1
    # dedupe (different probes can hit the same offset)
    offs = sorted(set(offs))
    return offs


def _rec_size_from_offsets(offs, region_end):
    """Record size = the spacing that aligns MOST probe hits on one grid
    (every named record carries a probe hit, so the true size aligns all
    of them; stray ASCII matches inside float data align only by chance).
    Sine records have 776 + 8*n_points bytes."""
    if len(offs) < 2:
        return None
    o0 = offs[0]
    best_d, best_score, best_aligned = None, 0, 0
    cands = sorted({offs[i + 1] - offs[i] for i in range(len(offs) - 1)})
    n_offs = len(offs)
    for d in cands:
        if not (900 < d < 400000):
            continue
        n = (d - 776) // 8
        if n * 8 != d - 776 or not (64 <= n <= 65536):
            continue
        if region_end is not None:
            # the last named record plus up to 5 tail records must fit
            # (small single-section files end right after their tail)
            if offs[-1] + 5 * d > region_end + 64:
                continue
        aligned = sum(1 for o in offs if (o - o0) % d <= 2 or (o - o0) % d >= d - 2)
        if aligned > best_score or (aligned == best_score and best_d and d < best_d):
            best_d, best_score = d, aligned
    # note: probe hits include stray ASCII matches inside float data, so
    # the hit count is NOT a lower bound on real named records - an
    # absolute quorum is enough (the true record size aligns every real
    # record name, which is >= 3 for any real file)
    if best_d is None or best_score < 3:
        return None
    return best_d


def file_layout(vf, hdr_lines, hdr):
    """Derive the measurement geometry of a sine file.

    Returns a dict with n_points, rec_size, n_stest (number of measured
    STEST channel records per section) and tail_records (= 4), working
    for every observed variant (2.16.x with 29 records and 1024/2048
    points; 2.17.x with 8/16 channels and up to 4096 points; files
    without an embedded setup text).
    """
    sections = vf.find_sections()
    if not sections:
        raise ValueError("no data sections found in %s" % vf.path)
    s0 = sections[0][0]
    data = vf.data


    # probe names: setup defs when available, generic prefixes otherwise
    probes = []
    if hdr_lines is not None and hdr is not None:
        probes = [c.get("Name", "").encode("latin-1", "replace")
                  for c in hdr.channels if c.get("Name")]
    if not probes:
        probes = [b"Control_", b"Controle_", b"TRI ACC", b"Response_",
                  b"Silp_Table", b"Accel", b"Micro"]
    offs = _probe_offsets(data, s0, probes)
    rec_size = _rec_size_from_offsets(offs, len(data))

    # fallback: derive from the section size when name hits are sparse
    if rec_size is None and hdr_lines is not None:
        n_stest_est = len(hdr.channels) if hdr is not None else 29
        if n_stest_est and len(sections) > 1:
            region = sections[1][0] - s0 - 24548
            if region > 0:
                cand = region / (n_stest_est + TAIL_RECORDS)
                if 900 < cand < 400000:
                    rec_size = int(round(cand / 8.0)) * 8

    if rec_size is None:
        rec_size = 8968                     # 1024-point legacy default
    n_points = (rec_size - 776) // 8
    if n_points < 64 or n_points > 65536:
        n_points = 1024
        rec_size = 8968

    # STEST record count: from the setup definitions...
    n_stest = None
    if hdr_lines is not None and hdr is not None and hdr.channels:
        n_stest = len(hdr.channels)
    if not n_stest or n_stest > 64:
        # ...otherwise: number of probe hits that sit on the record grid
        grid = [o for o in offs if (o - (s0 + 24564)) % rec_size == 0]
        if len(grid) > 1 and (s0 + 24564 + (len(grid) + TAIL_RECORDS) * rec_size
                              <= len(data) + 1):
            n_stest = len(grid)
        else:
            n_stest = 16
    n_stest = max(1, min(n_stest, 64))
    return {"n_points": n_points, "rec_size": rec_size,
            "n_stest": n_stest, "tail_records": TAIL_RECORDS}


def _preamble_freq_range(vf, s0):
    """Sweep frequency range for text-less files. 2.17.x copies without
    an embedded setup text sometimes store fmax/fmin as float32 pairs in
    the global preamble (before the first data section); returns
    (fmin, fmax) or None when no such table exists."""
    data = vf.data
    for off in range(0, max(s0 - 16, 0), 4):
        try:
            fmax = struct.unpack_from("<f", data, off)[0]
            fmin = struct.unpack_from("<f", data, off + 8)[0]
        except struct.error:
            break
        if 10.0 <= fmax <= 100000.0 and 0.5 <= fmin < fmax \
                and fmax / fmin <= 100000.0:
            return fmin, fmax
    return None


# ---------------------------------------------------------------------------
# measurement extraction
# ---------------------------------------------------------------------------

def parse_section(vf, sec_start, sec_end, layout):
    """Extract one measurement (one section) from the binary.

    Layout: [section header][n_stest STEST channel records][4 tail
    records].  STEST records hold the measured spectra in binary order;
    the tail holds Coherence (M1) / Error (M2+), Drive, Control channel
    and History (unnamed). The Control record stores n_points+4 float32
    samples at +752 (a leading pre-run value + the samples); the display
    uses samples[1:].
    Returns dict with per-channel arrays.
    """
    data = vf.data
    n = layout["n_points"]
    rec_size = layout["rec_size"]
    n_stest = layout["n_stest"]
    recs = {}
    for i in range(n_stest):
        rec = sec_start + 24564 + i * rec_size
        mags = struct.unpack_from("<%df" % n, data, rec + REC_HEADER)
        phs = struct.unpack_from("<%df" % n, data, rec + REC_HEADER + 4 * n)
        recs[i] = (list(mags), list(phs))

    # synthetic channels in the tail (order: Coherence/Error, Drive,
    # Control channel, History)
    tail0 = sec_start + 24564 + n_stest * rec_size
    synth = {}
    synth["Coherence"] = struct.unpack_from("<%df" % n, data, tail0 + 0 * rec_size + REC_HEADER)
    synth["Drive"] = struct.unpack_from("<%df" % n, data, tail0 + 1 * rec_size + REC_HEADER)
    # control record: the float32 samples start at +752; a variable
    # number of leading "pre-run" values (0.0 / negative) may precede the
    # real control samples. The displayed control starts at the first
    # positive sample; the error curve uses the same samples.
    ctrl_all = struct.unpack_from("<%df" % (n + 4), data, tail0 + 2 * rec_size + REC_HEADER - 8)
    start = 0
    for j, v in enumerate(ctrl_all):
        if v > 0:
            start = j
            break
    ctrl = list(ctrl_all[start:start + n])
    synth["Control"] = ctrl
    synth["Error"] = ctrl
    synth["History"] = struct.unpack_from("<%df" % n, data, tail0 + 3 * rec_size + REC_HEADER)
    return {"records": recs, "synth": synth}


def x_axis_sine(fmin, fmax, n, linear=False):
    """Frequency axis (log-spaced for Oct/min sweeps, linear for Hz/min).
    The exporter computes in double precision and stores the result as
    float32, so we round each value to f32 to reproduce the exact
    displayed digits."""
    if n <= 1:
        return [fmin]
    out = []
    for k in range(n):
        if linear:
            v = fmin + (fmax - fmin) * k / (n - 1)
        else:
            v = fmin * (fmax / fmin) ** (k / (n - 1))
        out.append(struct.unpack("<f", struct.pack("<f", v))[0])
    return out


def _pad_type(t):
    """The exporter pads the type code to 5 chars: 'C' right-aligned,
    'M' left-aligned (observed in the reference exports)."""
    t = t.strip()
    if t == "C":
        return "C    "
    if t == "M":
        return "    M"
    return t


def reference_profile(hdr, xvals):
    """Interpolated breakpoint reference profile (g) on the frequency grid."""
    bps = [(f, a) for f, a in hdr.breakpoints if f and a]
    if not bps:
        return []
    out = []
    for x in xvals:
        if x <= bps[0][0]:
            out.append(bps[0][1])
        elif x >= bps[-1][0]:
            out.append(bps[-1][1])
        else:
            for (f1, a1), (f2, a2) in zip(bps, bps[1:]):
                if f1 <= x <= f2:
                    if f2 == f1:
                        out.append(a2)
                    else:
                        t = math.log(x / f1) / math.log(f2 / f1)
                        out.append(a1 * (a2 / a1) ** t)
                    break
    return out


# ---------------------------------------------------------------------------
# txt generation
# ---------------------------------------------------------------------------

def channel_block(measurement_idx, ch_idx, name, number, ctype, subtype, filtermode,
                  units_x, units_y, ftype, fdata, size, xvals, yvals, phvals, rms=None):
    """Emit one [Measurement.N.Channel.K] block."""
    out = []
    out.append("[Measurement.%d.Channel.%d]" % (measurement_idx, ch_idx))
    out.append("Channel.Name:\t" + name)
    out.append("Channel.Number:\t" + str(number))
    out.append("Channel.Type:\t" + ctype)
    out.append("Channel.SubType:\t" + subtype)
    if filtermode is not None:
        out.append("Channel.Filtermode:\t" + filtermode)
    if rms is not None:
        out.append("RMS.Measured:\t" + fmt_g(rms))
        out.append("RMS.Unit:\tg")
    out.append("Units.X:\t" + units_x)
    out.append("Units.Y:\t" + units_y)
    out.append("Function.Type:\t" + ftype)
    out.append("Function.Data.Type:\t" + fdata)
    out.append("Function.Size:\t" + str(size))
    for k in range(size):
        if phvals is not None:
            out.append("Value.%d:\t%s\t%s\t%s" % (k + 1, fmt_x_sine(xvals[k]), fmt_g(yvals[k]), fmt_g(phvals[k])))
        else:
            out.append("Value.%d:\t%s\t%s" % (k + 1, fmt_x_sine(xvals[k]), fmt_g(yvals[k])))
    out.append("")
    return out


def write_txt(vf, hdr_lines, measurements, out_path):
    """Write the full .txt export."""
    lines = list(hdr_lines or [])
    if lines:
        lines.append("")
    lines.append("[Measurements]")
    lines.append("Measurements.Count:\t%d" % len(measurements))
    lines.append("")
    stem = vf.stem
    for mi, m in enumerate(measurements):
        lines.append("[Measurement.%d]" % (mi + 1))
        lines.append("Measurement.Number:\t%d" % (mi + 1))
        lines.append("")
        lines.append("[Measurement.%d.Description]" % (mi + 1))
        dt = m["dt"]
        from vcpnt_common import fmt_date, fmt_time
        lines.append("Date:\t" + fmt_date(dt))
        lines.append("Time:\t" + fmt_time(dt))
        lines.append("Source.Filename:\t" + stem)
        for c in range(1, 26):
            lines.append("User.Comment.%d:\t" % c)
        lines.append("")
        lines.append("Time.Elapsed:\t" + m["elapsed"])
        lines.append("Time.Remaining:\t" + m["remaining"])
        lines.append("Sweep.Type:\t" + m["sweep_type"])
        lines.append("Sweep.Done:\t%d" % m["sweep_done"])
        lines.append("Sweep.Requested:\t%d" % m["sweep_requested"])
        lines.append("Sweep.Direction:\t" + m["direction"])
        if m["show_rate"]:
            lines.append("Sweep.Rate:\t" + ("%.3f" % m["sweep_rate"]).replace(".", ","))
            lines.append("Sweep.Unit:\t" + m["sweep_unit"])
        lines.append("Control.Strategy:\tAverage")
        lines.append("Control.Loop:\tClosed loop")
        lines.append("")
        lines.append("[Measurement.%d.Channels]" % (mi + 1))
        lines.append("Channels.Count:\t%d" % len(m["channels"]))
        lines.append("")
        for ch in m["channels"]:
            lines.extend(channel_block(mi + 1, ch["idx"], ch["name"], ch["number"],
                                       ch["type"], ch["subtype"], ch.get("filtermode"),
                                       ch["units_x"], ch["units_y"], ch["ftype"],
                                       ch["fdata"], ch["size"], ch["x"], ch["y"], ch.get("ph")))
        # reference profiles (design + alarm/abort limits) - only when the
        # file stored breakpoints (absent in text-less copies)
        if m["references"]:
            lines.append("[Measurement.%d.References]" % (mi + 1))
            lines.append("References.Count:\t5")
            lines.append("")
            for rk, (rtype, subtype, factor) in enumerate(m["references"], start=1):
                lines.append("[Measurement.%d.Reference.%d]" % (mi + 1, rk))
                lines.append("Type:\t" + rtype)
                lines.append("SubType:\t" + subtype)
                lines.append("Units.X:\tHz")
                lines.append("Units.Y:\tg")
                lines.append("Function.Size:\t%d" % len(m["x"]))
                for k, x in enumerate(m["x"]):
                    lines.append("Value.%d:\t%s\t%s" % (k + 1, fmt_x_sine(x), fmt_g(m["design"][k] * factor)))
                lines.append("")
    with open(out_path, "w", encoding="latin-1", newline="") as fh:
        prev = ""
        for line in lines:
            # quirk of the original exporter: the Channels.Count line inside
            # the [Measurement.N.Channels] blocks ends with a bare LF,
            # everything else (including the header [Channels] count) uses CRLF
            if line.startswith("Channels.Count:\t") and prev.startswith("[Measurement."):
                ending = "\n"
            else:
                ending = "\r\n"
            fh.write(line + ending)
            prev = line
    return out_path


# ---------------------------------------------------------------------------
# csv generation
# ---------------------------------------------------------------------------

def write_csv(vf, measurement, out_path):
    """Write one CSV per measurement. Main header = channel name,
    sub headers = measurement type + units."""
    rows = []
    for ch in measurement["channels"]:
        rows.append("Channel: %s" % ch["name"])
        unit_str = "%s | %s" % (ch["units_x"], ch["units_y"])
        if ch.get("ph") is not None:
            unit_str += " | deg"
        rows.append("Type: %s (%s), Units: %s" % (ch["ftype"], ch["fdata"], unit_str))
        if ch.get("ph") is not None:
            rows.append("X, Magnitude, Phase")
        else:
            rows.append("X, Y")
        for k in range(ch["size"]):
            if ch.get("ph") is not None:
                rows.append("%s,%s,%s" % (ch["x"][k], ch["y"][k], ch["ph"][k]))
            else:
                rows.append("%s,%s" % (ch["x"][k], ch["y"][k]))
        rows.append("")
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(rows))
    return out_path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _direction(i, hdr):
    """Sweep direction shown for measurement i (0-based). With a sweep
    table it is the direction of the last completed sweep; without one the
    exporter alternates up/down per completed sweep."""
    if hdr.sweep_directions:
        if i == 0:
            return hdr.sweep_directions[0]
        return hdr.sweep_directions[min(i - 1, len(hdr.sweep_directions) - 1)]
    if i == 0 or i % 2 == 1:
        return "up"
    return "down"


def _sweep_durations(hdr):
    """Per-sweep durations in seconds from the [Schedule.Sweep.N] table,
    or [Duration]/[NoSweeps] when no table exists."""
    durations = []
    for l in hdr.lines or []:
        m = re.match(r"Sweep\.Duration:\t\d+:\d+:\d+:\d+", l)
        if m:
            parts = l.split(":")[-4:]          # D:HH:MM:SS
            dd, hh, mm, ss = (int(p) for p in parts)
            durations.append(dd * 86400 + hh * 3600 + mm * 60 + ss)
    if durations:
        return durations
    total = 0
    for l in hdr.lines or []:
        m = re.match(r"Schedule\.Duration:\t(\d+):(\d+):(\d+)", l)
        if m:
            hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
            total = hh * 3600 + mm * 60 + ss
            break
    n = max(hdr.no_sweeps, 1)
    return [total / n] * n


def _elapsed(i, hdr):
    """Accumulated sweep time for measurement i (0-based)."""
    if i == 0:
        return "000:00:00"
    durs = _sweep_durations(hdr)
    secs = int(sum(durs[:min(i, len(durs))]))
    return "%03d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)


def _remaining(i, hdr):
    if i == 0:
        return "000:00:00"
    durs = _sweep_durations(hdr)
    secs = int(sum(durs[min(i, len(durs)):]))
    return "%03d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)


def decode(path, out_dir=None, write_csvs=True, *, return_data=False):
    vf = VcpntFile(path)
    hdr_lines = vf.extract_header_text()
    textless = hdr_lines is None
    hdr = SineHeader(hdr_lines if hdr_lines is not None else [])
    layout = file_layout(vf, hdr_lines, hdr)
    n_points = layout["n_points"]
    n_stest = layout["n_stest"]
    rec_size = layout["rec_size"]
    sections = vf.find_sections()

    fmin, fmax = hdr.freq_range()
    if textless and not hdr.breakpoints:
        fr = _preamble_freq_range(vf, sections[0][0]) if sections else None
        if fr:
            fmin, fmax = fr
    xvals = x_axis_sine(fmin, fmax, n_points, linear=(hdr.sweep_type == "linear"))
    design = reference_profile(hdr, xvals)

    # reference limits: alarm = percent of design, abort = dB (amplitude)
    alarm_f = 1.0 + hdr.alarm_hi / 100.0
    alarm_lo_f = 1.0 + hdr.alarm_lo / 100.0
    abort_f = 10.0 ** (hdr.abort_hi / 20.0)
    abort_lo_f = 10.0 ** (hdr.abort_lo / 20.0)
    references = []
    if design:
        references = [("Reference", "None", 1.0), ("Alarm", "Upper", alarm_f),
                      ("Alarm", "Lower", alarm_lo_f), ("Abort", "Upper", abort_f),
                      ("Abort", "Lower", abort_lo_f)]

    # measurement timestamps from the log
    stamps = vf.measurement_timestamps("sine", len(sections))

    measurements = []
    for i, (s0, s1) in enumerate(sections):
        sec = parse_section(vf, s0, s1, layout)
        chans = []
        ch_idx = 1

        def add(name, number, ctype, subtype, filtermode, ux, uy, ftype, fdata, yvals, phvals=None, scaled=False):
            nonlocal ch_idx
            y = [v / G if scaled else v for v in yvals]
            chans.append({"idx": ch_idx, "name": name, "number": number, "type": ctype,
                          "subtype": subtype, "filtermode": filtermode, "units_x": ux,
                          "units_y": uy, "ftype": ftype, "fdata": fdata, "size": n_points,
                          "x": xvals, "y": y, "ph": phvals})
            ch_idx += 1

        first = (i == 0)          # only the first measurement gets "(STEST)"
        suff = " (STEST)" if first else ""

        # synthetic channels. For the first measurement of a run that has
        # not started yet the stored control samples sit at the reference
        # level (design*9.81 with float32 noise); the exporter then shows
        # the clean design profile instead of the stored data.
        ctrl_stored = sec["synth"]["Control"]
        ctrl_vals = ctrl_stored
        ctrl_scaled = True
        if first and design:
            d = design[0] * G
            if d and max(abs(v - d) for v in ctrl_stored[1:51]) < 0.01 * d:
                ctrl_vals = [design[0]] + [v / G for v in ctrl_stored[1:]]
                ctrl_scaled = False
        add("Control channel", 0, "X", "      ", "", "Hz", "g", "Spectrum", "Real",
            ctrl_vals, scaled=ctrl_scaled)
        add("Drive", 0, "D", "      ", "", "Hz", "V", "Spectrum", "Real",
            sec["synth"]["Drive"])
        if first:
            add("Coherence", 0, "Coh", "      ", "", "Hz", "", "Spectrum", "Real",
                sec["synth"]["Coherence"])
        else:
            # control error in dB: the tail stores the control/reference
            # RATIO; the export shows 20*log10 of it (bit-exact match).
            # Fall back to the computed curve only when no ratio is stored.
            err = []
            for v in sec["synth"]["Coherence"]:
                if v > 0:
                    err.append(20.0 * math.log10(v))
                else:
                    err.append(0.0)
            if not any(err):
                ref_si = hdr.breakpoints[0][1] * G if hdr.breakpoints and hdr.breakpoints[0][1] else 0.5 * G
                for v in sec["synth"]["Error"]:
                    if v > 0:
                        err.append(20.0 * math.log10(v / ref_si))
                    else:
                        err.append(0.0)
                err = err[len(err) - n_points:]
            add("Error", 0, "E", "      ", "", "Hz", "dB", "Spectrum", "Real", err)
        add("History", 0, "H", "      ", "", "Hz", "", "Spectrum", "Real",
            sec["synth"]["History"])

        # measured channels (STEST): map each record to its setup
        # definition BY NAME. Newer files name every record; 2.16.x files
        # name the first records only and the unnamed responses follow the
        # setup order.
        data = vf.data
        named = []
        for k in range(n_stest):
            rec = s0 + 24564 + k * rec_size
            named.append(read_cstr(data, rec, 24))
        resp_names = [c["Name"] for c in hdr.channels if c.get("Name", "").startswith("Response_")]
        order = []
        for k in range(n_stest):
            if named[k]:
                order.append(named[k])
            else:
                n_resp_used = len([o for o in order if o.startswith("Response_")])
                order.append(resp_names[n_resp_used] if n_resp_used < len(resp_names)
                             else "Ch%d" % (k + 1))
        for k, name in enumerate(order):
            chdef = next((c for c in hdr.channels if c.get("Name") == name), {})
            number = chdef.get("Number", 0)
            ctype = chdef.get("Type", "M")[:1]
            mags, phs = sec["records"][k]
            # phases are stored in radians, wrapped; the export keeps the
            # trajectory continuous by shifting values that jump by a full
            # cycle (|delta| > 300 deg) - observed exporter behaviour
            ph_deg = [p * 57.29577951308232 for p in phs]
            for j in range(1, len(ph_deg)):
                # bounded: real phase jumps need at most a few cycles; the
                # cap keeps garbage data from hanging the unwrap
                guard = 0
                while ph_deg[j] - ph_deg[j - 1] > 300 and guard < 8:
                    ph_deg[j] -= 360
                    guard += 1
                guard = 0
                while ph_deg[j] - ph_deg[j - 1] < -300 and guard < 8:
                    ph_deg[j] += 360
                    guard += 1
            add(name + suff, number, _pad_type(ctype), _pad_type(ctype), "Filtered",
                "Hz", "g", "Spectrum", "Complex", mags, phvals=ph_deg, scaled=True)

        measurements.append({
            "dt": stamps[i] if i < len(stamps) else None,
            "elapsed": _elapsed(i, hdr),
            "remaining": _remaining(i, hdr),
            "sweep_done": min(i, hdr.no_sweeps),
            "sweep_requested": hdr.no_sweeps,
            "direction": _direction(i, hdr),
            "sweep_type": hdr.sweep_type,
            "show_rate": hdr.show_rate,
            "sweep_rate": hdr.sweep_rate,
            "sweep_unit": hdr.sweep_unit,
            "references": references,
            "design": design,
            "x": xvals,
            "channels": chans,
        })

    # Curve Analyzer: return unrounded numeric data without writing exports.
    if return_data:
        return {"measurements": measurements, "header": hdr_lines, "layout": layout}

    if out_dir is None:
        out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    txt_path = os.path.join(out_dir, vf.base + ".txt")
    write_txt(vf, hdr_lines, measurements, txt_path)

    csv_paths = []
    if write_csvs:
        for mi, m in enumerate(measurements):
            csv_path = os.path.join(out_dir, "%s_Measurement_%d.csv" % (vf.base, mi + 1))
            write_csv(vf, m, csv_path)
            csv_paths.append(csv_path)
    return txt_path, csv_paths


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    out = None
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    txt, csvs = decode(src, out)
    print("wrote:", txt)
    print("csv files:", len(csvs))
