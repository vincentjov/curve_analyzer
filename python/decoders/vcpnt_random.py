#!/usr/bin/env python3
"""
vcpnt_decoder.py - Random (.rrn) decoder for m+p VibControl VcpNT files.

Usage:
    python3 vcpnt_decoder.py <file.rrn> [--out DIR] [--csv]

Writes:
    <name>.txt            - VcpNT-style decoded text export
    <name>_Measurement_N.csv - per-measurement CSVs

Shared code lives in vcpnt_common.py (parent directory).

Format notes (reverse-engineered):
  * One data section per measurement. A section is [section header][N
    measured (STEST) channel records][tail records]. The STEST record
    count equals the setup channel count (29 in 2.16.x files, 8/16/... in
    2.17.x-era files); record k's name sits at sec_start+24564+k*rec_size
    and its float data at name+840 in every observed version.
  * The binary STEST order differs from the .txt export order - records
    are mapped by their embedded name to the setup (txt) order.
  * Tail: measurement 1 carries Coherence, Drive, Control; later
    measurements carry the stored control Error (as a linear
    control/request ratio), Drive, Control plus two probability records
    ('Prob Function'/'Prob Density Function') that the export renders as
    EMPTY Error channels (PF/PDF, size 0).
  * Magnitudes are stored in SI ((m/s^2)^2/Hz = g^2/Hz * 9.81^2).
"""
import os
import re
import sys
import math
import struct
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vcpnt_common import VcpntFile, fmt_g, fmt_x_other, G2, read_cstr

HEADER_LEN = 24564          # name of record k sits at sec_start + HEADER_LEN + k*rec_size
MAG_OFF = 760               # float32 magnitude array start inside a record
                            # (2026-09-16: the array starts at the RECORDED grid's
                            # 0 Hz / DC bin; the old 840 skipped the first 20 lines
                            # because the legacy n_points came from the display band)
PH_PAD = 16                 # bytes between the magnitude array and the phase array
PH_PREFIX = 18 * 4          # 18-float prefix before the phase array (records 1..23,
                            # 2.16-era observation; kept as the historical layout)
                            # record layout: [name+760 mag n][pad 16][phase n][coherence n]
                            #                => rec_size = 776 + 12 * n
DESIGN_FACTOR = 1.002375    # empirical: stored design profile vs header breakpoints


# ---------------------------------------------------------------------------
# header parsing
# ---------------------------------------------------------------------------

class RandomHeader:
    def __init__(self, lines):
        self.lines = lines
        self.channels = []          # list of dicts (setup channel definitions)
        self.breakpoints = []       # (freq, psd g^2/Hz)
        self.fmin = 20.0
        self.fmax = 2000.0
        self.alarm_hi = 3.0
        self.alarm_lo = -3.0
        self.abort_hi = 6.0
        self.abort_lo = -6.0
        self.resolution = 1.0
        self.resolution_source = None
        cur = None
        for l in lines or []:
            if l.startswith("[Channel."):
                cur = {}
                self.channels.append(cur)
                continue
            if cur is None:
                m = re.match(r"Advanced\.FreqLimitLo:\t([\d,]+) Hz", l)
                if m:
                    self.fmin = float(m.group(1).replace(",", "."))
                m = re.match(r"Advanced\.FreqLimitHi:\t([\d,]+) Hz", l)
                if m:
                    self.fmax = float(m.group(1).replace(",", "."))
                m = re.match(r"Stimulation\.Resolution:\s*"
                             r"(\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s+Hz\b", l)
                if m:
                    self.resolution = float(m.group(1).replace(",", "."))
                    if not 0 < self.resolution < float("inf"):
                        raise ValueError("PSD frequency resolution must be finite and positive.")
                    self.resolution_source = "Stimulation.Resolution"
                m = re.match(r"Breakpoint\.Freq:\t([\d,]+) Hz", l)
                if m:
                    self.breakpoints.append((float(m.group(1).replace(",", ".")), None))
                m = re.match(r"Breakpoint\.Accel:\t([\d,]+(?:e-?\d+)?) g", l)
                if m and self.breakpoints:
                    self.breakpoints[-1] = (self.breakpoints[-1][0],
                                            float(m.group(1).replace(",", ".")))
                m = re.match(r"Breakpoint\.AlarmHi:\t(-?[\d,]+) dB", l)
                if m:
                    self.alarm_hi = float(m.group(1).replace(",", "."))
                m = re.match(r"Breakpoint\.AlarmLo:\t(-?[\d,]+) dB", l)
                if m:
                    self.alarm_lo = float(m.group(1).replace(",", "."))
                m = re.match(r"Breakpoint\.AbortHi:\t(-?[\d,]+) dB", l)
                if m:
                    self.abort_hi = float(m.group(1).replace(",", "."))
                m = re.match(r"Breakpoint\.AbortLo:\t(-?[\d,]+) dB", l)
                if m:
                    self.abort_lo = float(m.group(1).replace(",", "."))
                continue
            m = re.match(r"Channel\.(Number|Name|Type|SubType|Filtermode):\t?(.*)", l)
            if m:
                cur[m.group(1)] = m.group(2).strip()

    def setup_channel_names(self):
        return [c.get("Name", "") for c in self.channels if c.get("Name")]


def _pad_type(t):
    t = t.strip()
    if t == "C":
        return "C    "
    if t == "M":
        return "    M"
    return t


def reference_design(hdr, xvals):
    """Interpolated design PSD profile (g^2/Hz) on the frequency grid,
    scaled by the empirical design factor."""
    bps = [(f, a) for f, a in hdr.breakpoints if f and a]
    if not bps:
        return []
    out = []
    for x in xvals:
        if x <= bps[0][0]:
            v = bps[0][1]
        elif x >= bps[-1][0]:
            v = bps[-1][1]
        else:
            v = bps[-1][1]
            for (f1, a1), (f2, a2) in zip(bps, bps[1:]):
                if f1 <= x <= f2:
                    if f2 == f1:
                        v = a2
                    else:
                        t = math.log(x / f1) / math.log(f2 / f1)
                        v = a1 * (a2 / a1) ** t
                    break
        out.append(v * DESIGN_FACTOR)
    return out


# ---------------------------------------------------------------------------
# file geometry
# ---------------------------------------------------------------------------

def file_layout(vf, hdr_lines, hdr):
    """Derive the measurement geometry of a random file.

    Returns a dict with n_points, rec_size and n_stest. Works for 2.16.x
    files (29 records, 1981 points) and 2.17.x-era files (8/16 channels,
    any resolution-derived point count).
    """
    sections = vf.find_sections()
    if not sections:
        raise ValueError("no data sections found in %s" % vf.path)
    s0 = sections[0][0]
    data = vf.data

    res = hdr.resolution if hdr_lines is not None else 1.0
    fmin = hdr.fmin if hdr_lines is not None else 20.0
    fmax = hdr.fmax if hdr_lines is not None else 2000.0
    # the DISPLAY/export band (Advanced.FreqLimit*) is NOT the recorded grid -
    # kept only as a fallback when the record geometry cannot be derived
    n_display = max(int(round((fmax - fmin) / res)) + 1, 1)
    n_points = n_display

    # record size from the spacing between named records (the binary
    # order differs from the txt order, but every record carries its name)
    probes = []
    if hdr_lines is not None:
        probes = [c.get("Name", "").encode("latin-1", "replace")
                  for c in hdr.channels if c.get("Name")]
    if not probes:
        probes = [b"Control_", b"Controle_", b"TRI ACC", b"Response_",
                  b"Silp_Table", b"Accel", b"Micro"]
    offs = []
    for pr in probes:
        if len(pr) < 3:
            continue
        pos = s0 + 20000
        end = s0 + 3000000
        n = 0
        while n < 300:
            i = data.find(pr, pos, end)
            if i < 0:
                break
            offs.append(i)
            pos = i + 1
            n += 1
    offs = sorted(set(offs))
    rec_size = None
    if len(offs) >= 2:
        o0 = offs[0]
        best_d, best_score = None, 0
        cands = sorted({offs[i + 1] - offs[i] for i in range(len(offs) - 1)})
        for d in cands:
            if not (900 < d < 400000):
                continue
            # the last named record plus up to 5 tail records must fit
            if offs[-1] + 5 * d > len(data) + 64:
                continue
            aligned = sum(1 for o in offs if (o - o0) % d <= 2 or (o - o0) % d >= d - 2)
            if aligned > best_score or (aligned == best_score and best_d and d < best_d):
                best_d, best_score = d, aligned
        # absolute quorum (stray ASCII hits inside float data inflate
        # len(offs); the true record size aligns every real record name)
        if best_d is not None and best_score >= 3:
            rec_size = best_d
    if rec_size is None:
        rec_size = 39188                     # 3201-point record (1981 legacy view)

    # The RECORDED grid follows from the record size: every record is
    # [776-byte header][mag n][16-byte pad][phase n][coherence n], i.e.
    # rec_size = 776 + 12n. The grid always starts at the DC bin (0 Hz), so
    # the recorded band is 0..(n-1)*resolution - WIDER than the 20..2000 Hz
    # Advanced.FreqLimit* window VcpNT's own text export applies.
    rest = int(rec_size) - 776
    if rest > 0 and rest % 12 == 0:
        cand = rest // 12
        if 64 <= cand <= 262144:
            n_points = cand
    else:
        n_points = n_display

    n_stest = None
    if hdr_lines is not None and hdr is not None and hdr.channels:
        n_stest = len(hdr.channels)
    if not n_stest or n_stest > 64:
        n_stest = 16
    n_stest = max(1, min(n_stest, 64))
    return {"n_points": n_points, "rec_size": rec_size, "n_stest": n_stest}


# ---------------------------------------------------------------------------
# measurement extraction
# ---------------------------------------------------------------------------

def parse_section(vf, sec_start, sec_end, layout, first_measurement=True):
    """Extract one measurement. Layout: [header][n_stest STEST records]
    [tail]. The tail holds (M1) Coherence/Drive/Control or (M2+)
    Error(ratio)/Drive/Control + 2 probability records.

    Phase layout: magnitudes at name+840, then a 4808-byte gap, then the
    phase array. Records 1..23 store 18 leading floats before their
    phase array (record 0 starts directly); records >= 24 (the named
    Silp_Table/Control_* records of 2.16.x files) store no phase data.
    """
    data = vf.data
    n = layout["n_points"]
    rec_size = layout["rec_size"]
    n_stest = layout["n_stest"]
    # 2026-09-16: the phase array sits directly after the magnitude block
    # (mag at +760, 16-byte pad, phase, coherence) - the old MAG_OFF/PH_GAP
    # pair was calibrated for the 1981-point DISPLAY slice and was 8 bytes
    # (2 samples) short of the real array start.
    ph_off = MAG_OFF + PH_PAD + 4 * n
    recs = {}
    for i in range(n_stest):
        rec = sec_start + HEADER_LEN + i * rec_size
        mags = struct.unpack_from("<%df" % n, data, rec + MAG_OFF)
        if i == 0:
            phs = struct.unpack_from("<%df" % n, data, rec + ph_off)
        elif i <= 23:
            phs = struct.unpack_from("<%df" % n, data, rec + ph_off + PH_PREFIX)
        else:
            phs = [0.0] * n
        recs[i] = (list(mags), list(phs))
    # synthetic channels in the tail
    tail0 = sec_start + HEADER_LEN + n_stest * rec_size
    synth = {}
    synth["Coherence"] = struct.unpack_from("<%df" % n, data, tail0 + 0 * rec_size + MAG_OFF)
    synth["Drive"] = struct.unpack_from("<%df" % n, data, tail0 + 1 * rec_size + MAG_OFF)
    synth["Control"] = struct.unpack_from("<%df" % n, data, tail0 + 2 * rec_size + MAG_OFF)
    return {"records": recs, "synth": synth}


def rms_of(mags, df):
    """Grms of a PSD: sqrt(sum(PSD * df))."""
    return math.sqrt(max(sum(m * df for m in mags), 0.0))


# ---------------------------------------------------------------------------
# txt generation
# ---------------------------------------------------------------------------

def write_txt(vf, hdr_lines, measurements, out_path):
    lines = list(hdr_lines or [])
    if lines:
        lines.append("")
    lines.append("[Measurements]")
    lines.append("Measurements.Count:\t%d" % len(measurements))
    lines.append("")
    for mi, m in enumerate(measurements):
        lines.append("[Measurement.%d]" % (mi + 1))
        lines.append("Measurement.Number:\t%d" % (mi + 1))
        lines.append("")
        lines.append("[Measurement.%d.Description]" % (mi + 1))
        from vcpnt_common import fmt_date, fmt_time
        lines.append("Date:\t" + fmt_date(m["dt"]))
        lines.append("Time:\t" + fmt_time(m["dt"]))
        lines.append("Source.Filename:\t" + vf.stem)
        for c in range(1, 26):
            lines.append("User.Comment.%d:\t" % c)
        lines.append("")
        lines.append("Time.Elapsed:\t" + m["elapsed"])
        lines.append("Time.Remaining:\t" + m["remaining"])
        lines.append("Time.Level.Elapsed:\t" + m["level_elapsed"])
        lines.append("Time.Level.Remaining:\t" + m["level_remaining"])
        lines.append("DOF:\t%d" % m["dof"])
        lines.append("Level:\t" + m["level"])
        lines.append("Resolution:\t1")
        lines.append("RMS.Measured:\t" + fmt_g(m["rms_measured"]))
        lines.append("RMS.Request:\t" + fmt_g(m["rms_request"]))
        lines.append("RMS.Unit:\tg")
        lines.append("Control.Strategy:\tAverage")
        lines.append("")
        lines.append("[Measurement.%d.Channels]" % (mi + 1))
        lines.append("Channels.Count:\t%d" % len(m["channels"]))
        lines.append("")
        for ch in m["channels"]:
            out = []
            out.append("[Measurement.%d.Channel.%d]" % (mi + 1, ch["idx"]))
            out.append("Channel.Name:\t" + ch["name"])
            out.append("Channel.Number:\t" + str(ch["number"]))
            out.append("Channel.Type:\t" + ch["type"])
            out.append("Channel.SubType:\t" + ch["subtype"])
            if ch.get("rms") is not None:
                out.append("RMS.Measured:\t" + fmt_g(ch["rms"]))
                out.append("RMS.Unit:\t" + ch.get("rms_unit", "g"))
            out.append("Units.X:\t" + ch["units_x"])
            out.append("Units.Y:\t" + ch["units_y"])
            out.append("Function.Type:\t" + ch["ftype"])
            out.append("Function.Data.Type:\t" + ch["fdata"])
            out.append("Function.Size:\t" + str(ch["size"]))
            for k in range(ch["size"]):
                if ch.get("ph") is not None:
                    out.append("Value.%d:\t%s\t%s\t%s" % (k + 1, fmt_x_other(ch["x"][k]),
                                                          fmt_g(ch["y"][k]), fmt_g(ch["ph"][k])))
                else:
                    out.append("Value.%d:\t%s\t%s" % (k + 1, fmt_x_other(ch["x"][k]), fmt_g(ch["y"][k])))
            out.append("")
            lines.extend(out)
        # references
        if m["references"]:
            lines.append("[Measurement.%d.References]" % (mi + 1))
            lines.append("References.Count:\t5")
            lines.append("")
            for rk, (rtype, subtype, factor) in enumerate(m["references"], start=1):
                lines.append("[Measurement.%d.Reference.%d]" % (mi + 1, rk))
                lines.append("Type:\t" + rtype)
                lines.append("SubType:\t" + subtype)
                lines.append("Units.X:\tHz")
                lines.append("Units.Y:\tg²/Hz")
                lines.append("Function.Size:\t%d" % len(m["x"]))
                for k, x in enumerate(m["x"]):
                    lines.append("Value.%d:\t%s\t%s" % (k + 1, fmt_x_other(x), fmt_g(m["design"][k] * factor)))
                lines.append("")
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        prev = ""
        for line in lines:
            if line.startswith("Channels.Count:\t") and prev.startswith("[Measurement."):
                ending = "\n"
            else:
                ending = "\r\n"
            fh.write(line + ending)
            prev = line
    return out_path


def write_csv(vf, measurement, out_path):
    rows = []
    for ch in measurement["channels"]:
        rows.append("Channel: %s" % ch["name"])
        rows.append("Type: %s (%s), Units: %s" % (ch["ftype"], ch["fdata"],
                                                   "%s | %s" % (ch["units_x"], ch["units_y"])))
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

def decode(path, out_dir=None, write_csvs=True, *, return_data=False):
    vf = VcpntFile(path)
    hdr_lines = vf.extract_header_text()
    textless = hdr_lines is None
    hdr = RandomHeader(hdr_lines if hdr_lines is not None else [])
    layout = file_layout(vf, hdr_lines, hdr)
    n_points = layout["n_points"]
    rec_size = layout["rec_size"]
    n_stest = layout["n_stest"]
    sections = vf.find_sections()
    stamps = vf.measurement_timestamps("random", len(sections))

    # the RECORDED grid starts at the DC bin: 0 .. (n-1) * resolution
    # (2026-09-16; the old hdr.fmin start = the display window's 20 Hz)
    xvals = [k * hdr.resolution for k in range(n_points)]
    design = reference_design(hdr, xvals)

    # txt channel order: 2.16.x files list Silp_Table + the control
    # channels in a fixed order that differs from the setup definitions;
    # 2.17.x-era files list the setup order. Binary records are mapped
    # by their embedded name to this order.
    def_names = hdr.setup_channel_names()
    legacy = ["Silp_Table", "Control_1Z", "Control_2", "Control_3",
              "Control_5", "Control_1X", "Control_1Y", "Control_4X",
              "Control_4Y", "Control_4Z"]
    if "Silp_Table" in def_names:
        order = [n for n in legacy if n in def_names]
        order += [n for n in def_names if n not in legacy]
    else:
        order = def_names

    alarm_f = 10.0 ** (hdr.alarm_hi / 10.0)
    alarm_lo_f = 10.0 ** (hdr.alarm_lo / 10.0)
    abort_f = 10.0 ** (hdr.abort_hi / 10.0)
    abort_lo_f = 10.0 ** (hdr.abort_lo / 10.0)
    references = []
    if design:
        references = [("Reference", "None", 1.0), ("Alarm", "Upper", alarm_f),
                      ("Alarm", "Lower", alarm_lo_f), ("Abort", "Upper", abort_f),
                      ("Abort", "Lower", abort_lo_f)]

    measurements = []
    first_ts = stamps[0] if stamps else None
    for i, (s0, s1) in enumerate(sections):
        first = (i == 0)
        sec = parse_section(vf, s0, s1, layout, first_measurement=first)
        # map records to the txt order by name
        named = {}
        for k in range(n_stest):
            nm = read_cstr(vf.data, s0 + HEADER_LEN + k * rec_size, 24)
            if nm:
                named.setdefault(nm, k)
        chans = []
        ch_idx = 1

        def add(name, number, ctype, subtype, ux, uy, ftype, fdata, yvals,
                phvals=None, scaled=False, rms=None, filtermode=None, size=None, rms_unit="g"):
            nonlocal ch_idx
            y = [v / G2 if scaled else v for v in yvals]
            chans.append({"idx": ch_idx, "name": name, "number": number, "type": ctype,
                          "subtype": subtype, "units_x": ux, "units_y": uy, "ftype": ftype,
                          "fdata": fdata, "size": size if size is not None else n_points,
                          "x": xvals, "y": y, "ph": phvals, "rms": rms, "rms_unit": rms_unit})
            ch_idx += 1

        suff = " (STEST)" if first else ""

        # run-state fields used by both the description and the Error channel
        rms_measured = rms_of([v / G2 for v in sec["synth"]["Control"]], 1.0)
        design_rms = math.sqrt(max(sum(m * 1.0 for m in design), 0.0)) if design else 0.0
        level_db = 20.0 * math.log10(rms_measured / design_rms) if rms_measured > 0 and design_rms > 0 else 0.0
        level_rounded = round(level_db / 3.0) * 3.0
        if i == 0:
            level_rounded = -6.0      # first measurement runs at the first level
        # the reference profiles are the design scaled by the current
        # level (PSD: 10*log10); the first measurement shows the full
        # design regardless of its level field
        level_ref = 0.0 if i == 0 else level_rounded
        design_meas = [d * (10.0 ** (level_ref / 10.0)) for d in design]

        # synthetic channels: the first measurement shows Coherence, later
        # ones show the control Error (stored as a linear control/request
        # ratio; the export shows 10*log10 of it in dB)
        add("Control channel", 0, "X", "      ", "Hz", "g²/Hz", "PSD", "Real",
            sec["synth"]["Control"], scaled=True,
            rms=rms_of([v / G2 for v in sec["synth"]["Control"]], 1.0))
        add("Drive", 0, "D", "      ", "Hz", "V²/Hz", "PSD", "Real", sec["synth"]["Drive"],
            rms=rms_of(sec["synth"]["Drive"], 1.0), rms_unit="V")
        if first:
            add("Coherence", 0, "Coh", "      ", "Hz", "", "PSD", "Real", sec["synth"]["Coherence"])
        else:
            # control loop error in dB from the STORED ratio record (tail
            # slot 0 of later measurements; ~control/request, so ~0 dB)
            err = []
            for v in sec["synth"]["Coherence"]:
                if v > 0:
                    err.append(10.0 * math.log10(v))
                else:
                    err.append(0.0)
            # fallback to the computed deviation when the stored record is
            # all zeros / missing (older files)
            if not any(x != 0.0 for x in err) and design_meas:
                ctrl_g = [v / G2 for v in sec["synth"]["Control"]]
                err = []
                for k in range(n_points):
                    req = design_meas[k] if k < len(design_meas) else 0.0
                    if req > 0 and ctrl_g[k] > 0:
                        err.append(10.0 * math.log10(ctrl_g[k] / req))
                    else:
                        err.append(0.0)
            add("Error", 0, "E", "      ", "Hz", "dB", "PSD", "Real", err)

        for name in order:
            k = named.get(name)
            if k is None:
                # no stored record carries this setup name: skip it (the
                # export omits channels that have no record in the file)
                continue
            chdef = next((c for c in hdr.channels if c.get("Name") == name), {})
            number = chdef.get("Number", 0)
            ctype = chdef.get("Type", "M")[:1]
            mags, phs = sec["records"][k]
            # random phases are already stored in degrees
            add(name + suff, number, _pad_type(ctype), _pad_type(ctype), "Hz", "g²/Hz",
                "PSD", "Complex", mags, phvals=list(phs), scaled=True,
                rms=rms_of([v / G2 for v in mags], 1.0))

        # measurements after the first carry two empty Error channels
        # (the export renders the stored probability records as size-0)
        if i >= 1:
            add("Error", 1, "PF", "      ", "", "dB", "PSD", "Real", [0.0] * 0, size=0)
            add("Error", 1, "PDF", "      ", "", "dB", "PSD", "Real", [0.0] * 0, size=0)

        # run-state description fields (approximated where the exporter
        # uses internal run state that is not stored in the file)
        dt = stamps[i] if i < len(stamps) else None
        if dt and first_ts:
            secs = max(int((dt - first_ts).total_seconds()), 0)
        else:
            secs = 0
        total = 16 * 60
        remaining = max(total - secs, 0)
        if i == 0:
            level_elapsed, level_rem = 0, 2 * 60
        else:
            level_elapsed, level_rem = secs, remaining
        rms_request = design_rms if i == 0 else design_rms * (10.0 ** (level_rounded / 20.0))

        measurements.append({
            "dt": dt,
            "elapsed": "%03d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60),
            "remaining": "%03d:%02d:%02d" % (remaining // 3600, (remaining % 3600) // 60, remaining % 60),
            "level_elapsed": "%03d:%02d:%02d" % (level_elapsed // 3600, (level_elapsed % 3600) // 60, level_elapsed % 60),
            "level_remaining": "%03d:%02d:%02d" % (level_rem // 3600, (level_rem % 3600) // 60, level_rem % 60),
            "dof": 1 if i == 0 else 90,
            "level": ("%.3f" % level_rounded).replace(".", ","),
            "rms_measured": rms_measured,
            "rms_request": rms_request,
            "references": references,
            "design": design_meas,
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
