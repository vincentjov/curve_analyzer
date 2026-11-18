#!/usr/bin/env python3
"""
vcpnt_decoder.py - Classical shock (.rcs) decoder for m+p VibControl VcpNT
files.

Usage:
    python3 vcpnt_decoder.py <file.rcs> [--out DIR] [--csv] [--all-channels]
                              [--all-sections]

Writes:
    <name>.txt            - VcpNT-style decoded text export. Like the
                            reference exports this lists the Drive time
                            history of the first section by default;
                            --all-channels additionally exports every
                            stored time-history record of the section
                            (the measured/control channels of the setup
                            and the Error record, when stored)
    <name>_Measurement_N.csv
    With --all-sections every recorded section (single shock) of the
    file is exported as its own measurement (see below).

Shared code lives in vcpnt_common.py (parent directory).

Format notes (reverse-engineered; two file generations are handled):
  * A section holds the measured time-history channel records (the count
    varies with what the run captured - from a handful of channels to the
    full setup) followed by the synthetic records. Every record is a
    null-terminated name + 760-byte header + float32 samples, so a
    record's size = 776 + 4*n_samples. 1024 samples @ 4096 Hz is the
    usual layout; the size is derived per file.
  * Record k of a section sits at grid_base + k*rec_size, where grid_base
    is the record grid's start INSIDE the section - it varies by file
    generation (sec_start + 24564 for the 2.16.x/2.17.x-era corpora,
    sec_start + 204 / + 2764 for the 2026 New_Data files) and is derived
    per section by aligning the printable record names. The measured
    channels start at a fixed slot offset (k = 5 in the New_Data files,
    with four empty reserved slots before them; k = 0 in the older
    corpora). In grid order the records are:
      k < k_drive         the measured/control channels of the setup
                          (Control_Z1.., TRI ACC..., TRI_ACC_X1.. in the
                          corpora seen; raw names come from the file)
      k = k_drive         Drive  (the driver time history, raw volts)
      after Drive         Error  (only sections with an active control
                          loop; first section of every observed file
                          predates the loop and has no Error)
      tail                Coherence, TF (frequency-domain functions -
                          NOT time histories, never exported)
  * One section = ONE recorded single shock of the test run. The newer
    shock files store many shocks (1..13 seen: section count == the
    number of per-shock txt exports VcpNT writes for the file); section
    index == shock index in recording order. The reference Drive block
    of the first section is byte-identical to the old-style exports; the
    txt exports VcpNT writes per shock are a different (tabular) format
    and additionally contain a synthesized 'Reference' column that is
    NOT stored in the binary.
  * Measured/control channels are stored in SI (m/s^2 = g * 9.81); the
    VcpNT txt exports display them in g, so --all-channels /
    --all-sections exports divide by 9.81 and label the unit g (verified
    against the 2026 New_Data per-shock exports: every value cell equals
    the stored float32 / 9.81). Drive is stored and exported raw in V.
"""
import os
import re
import sys
import struct
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vcpnt_common import VcpntFile, fmt_g, fmt_x_other, read_cstr, G

REC_HEADER = 760            # float data offset from a record name
N_POINTS = 1024             # exported time samples (fallback)
MIN_POINTS, MAX_POINTS = 64, 32768


class ShockHeader:
    def __init__(self, lines):
        self.lines = lines
        self.sample_rate = 4096.0
        self.sample_rate_source = None
        self.peak_request = 6.0
        self.levels = []          # (level_db, pulses) from the schedule
        for l in lines or []:
            m = re.match(r"((?:Advanced|Stimulation)\.SampleRate):\s*"
                         r"(\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?)\s+Hz\b", l)
            if m:
                rate = float(m.group(2).replace(",", "."))
                if not 0 < rate < float("inf"):
                    raise ValueError("Shock sample rate must be finite and positive.")
                if self.sample_rate_source and rate != self.sample_rate:
                    raise ValueError("Conflicting shock sample rates in the embedded setup.")
                self.sample_rate, self.sample_rate_source = rate, m.group(1)
            m = re.match(r"Stimulation\.MainPulsePeak:\t([\d,]+) g", l)
            if m:
                self.peak_request = float(m.group(1).replace(",", "."))
            m = re.match(r"Command\.Level:\t(-?[\d,]+)", l)
            if m:
                level = float(m.group(1).replace(",", "."))
                pulses = 0
                j = self.lines.index(l) if l in (self.lines or []) else -1
                for l2 in (self.lines or [])[j + 1:j + 4]:
                    m2 = re.match(r"Command\.Pulses:\t(\d+)", l2)
                    if m2:
                        pulses = int(m2.group(1))
                        break
                if pulses > 0:
                    self.levels.append((level, pulses))

    def total_pulses(self):
        return sum(p for _, p in self.levels)


def fmt_time_axis(value):
    """Shock time axis: 6 significant digits, rounded half-up (the
    exporter rounds the exact dyadic halves like 0.0009765625 up)."""
    s = "%.6g" % (value + 1e-12) if value > 0 else "0"
    return s.replace(".", ",")


def _name_tokens(data, sec_start, sec_end):
    """Offsets of plausible record names (printable tokens followed by a
    null pad) inside a section."""
    out = []
    i = sec_start
    while True:
        i = data.find(b"\x00", i)
        if i < 0 or i >= sec_end:
            break
        j = i
        while j > sec_start and 32 <= data[j - 1] < 127:
            j -= 1
        tok = data[j:i]
        if 3 <= len(tok) <= 40 and any(c.isalpha() for c in tok.decode("latin-1", "replace")):
            out.append(j)
        i += 1
    return out


def _record_size_from_tokens(data, sec_start, sec_end):
    """Derive the record grid spacing from the aligned name tokens
    (name-to-name distances); None when fewer than two names exist."""
    toks = sorted(set(_name_tokens(data, sec_start, sec_end)))
    if len(toks) < 2:
        return None
    diffs = [toks[i + 1] - toks[i] for i in range(len(toks) - 1)]
    for d, _ in Counter(diffs).most_common():
        if MIN_POINTS * 4 + 776 <= d <= MAX_POINTS * 4 + 776:
            return d
    return None


def file_rec_size(vf):
    """Record-grid spacing for the whole file, derived from the name
    tokens of ALL data sections at once. The per-section derivation can
    be fooled by printable tokens inside the float data of a sparse
    section (a 1-2 channel shock section only has ~5 real names); over
    the whole file the real grid spacing dominates every other distance.
    Falls back to the per-section / 1024-sample constants."""
    txt_off = vf._text_section_start()
    sections = vf.find_sections()
    if sections:
        tokens = []
        for s0, s1 in sections:
            tokens.extend(_name_tokens(vf.data, s0, min(s1, txt_off)))
        tokens = sorted(set(tokens))
        if len(tokens) >= 2:
            diffs = [tokens[i + 1] - tokens[i] for i in range(len(tokens) - 1)]
            for d, _ in Counter(diffs).most_common():
                if MIN_POINTS * 4 + 776 <= d <= MAX_POINTS * 4 + 776:
                    return d
    return None


def find_drive_record(vf, sec_start, sec_end):
    """Locate the Drive record inside a section and return (offset of its
    float32 time data, record size). The record size is the spacing of the
    record grid (name-to-name); falls back to the 1024-sample size when a
    section holds a single record."""
    data = vf.data
    cand = []
    pos = sec_start
    while True:
        i = data.find(b"Drive\x00", pos, sec_end)
        if i < 0:
            break
        cand.append(i)
        pos = i + 1
    drive_off = None
    for i in cand:
        try:
            v = struct.unpack_from("<4f", data, i + REC_HEADER)
        except struct.error:
            continue
        if all(abs(x) < 1e9 for x in v):
            drive_off = i
            break
    if drive_off is None:
        # fall back to a bare 'Drive' token
        i = data.find(b"Drive", sec_start, sec_end)
        if i >= 0:
            drive_off = i
        else:
            return None, 776 + 4 * N_POINTS

    rec_size = _record_size_from_tokens(data, sec_start, sec_end)
    if rec_size is None:
        rec_size = 776 + 4 * N_POINTS
    return drive_off + REC_HEADER, rec_size


def _grid_base(vf, sec_start, sec_end, rec_size):
    """Derive the record grid's absolute start inside a section: the
    offset X at which the printable record names sit on a rec_size grid
    (name at X + k*rec_size). Scans the name tokens and tests each as a
    grid slot k <= 8; falls back to the legacy 2.16.x/2.17.x-era offset
    sec_start + 24564 (2.16.x header length; the 2.17.x-era 24548-byte
    header + 16-byte shift lands on the same value)."""
    data = vf.data
    toks = sorted(set(_name_tokens(data, sec_start, sec_end)))
    scored = []
    for t in toks:
        for k in range(0, 9):
            x = t - k * rec_size
            if x < sec_start + 32 or x + rec_size > sec_end:
                continue
            # score: how many printable names sit on the grid from x
            cnt = 0
            for kk in range((sec_end - x) // rec_size + 1):
                off = x + kk * rec_size
                if off + 4 > sec_end:
                    break
                end = data.find(b"\x00", off, off + 64)
                nm = data[off:end].decode("latin-1", "replace") if end >= off else ""
                if nm and all(32 <= ord(c) < 127 for c in nm):
                    cnt += 1
            if cnt >= 2:
                scored.append((cnt, x))
    if scored:
        return max(scored)[1]
    # legacy constant (old corpus): grid starts right after the header
    return sec_start + 24564


def setup_channels(hdr_lines):
    """Ordered setup channel definitions from the embedded header text
    (its [Channel.N] blocks): list of (name, number, type) in definition
    order. Record names / numbers / type codes match the record grid."""
    out = []
    cur = None
    for l in hdr_lines or []:
        if re.match(r"^\[Channel\.\d+\]$", l):
            cur = {}
            out.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"^Channel\.Number:\t(\d+)$", l)
        if m:
            cur["number"] = int(m.group(1))
            continue
        m = re.match(r"^Channel\.Name:\t(.+)$", l)
        if m:
            cur["name"] = m.group(1).strip()
            continue
        m = re.match(r"^Channel\.Type:\t(\S+)$", l)
        if m:
            cur["type"] = m.group(1).strip()
            continue
        if l.startswith("["):
            cur = None
    return [(c.get("name"), c.get("number"), c.get("type")) for c in out]


def section_slots(vf, sec_start, sec_end, rec_size, grid_base=None):
    """Walk the record-name grid of a section. Returns the named grid
    slots in order as dicts {k, name, data_pos, n} - only slots with a
    printable name whose float block (name + 760, n_points floats) fits
    inside the section. n_points comes from the record size
    ((rec_size - 776) / 4)."""
    npts = (rec_size - 776) // 4 if rec_size and rec_size >= 776 else 0
    if not (MIN_POINTS <= npts <= MAX_POINTS):
        npts = N_POINTS
    base = grid_base if grid_base is not None else sec_start + 24564
    out = []
    data = vf.data
    k = 0
    while True:
        pos = base + k * rec_size
        if pos + 4 >= sec_end:
            break
        end = data.find(b"\x00", pos, pos + 64)
        nm = data[pos:end].decode("latin-1", "replace") if end >= pos else ""
        if pos + REC_HEADER + 4 * npts > sec_end:
            break
        if nm and all(32 <= ord(c) < 127 for c in nm):
            out.append({"k": k, "name": nm, "data_pos": pos + REC_HEADER, "n": npts})
        k += 1
    return out


def section_time_channels(vf, sec_start, sec_end, rec_size, hdr_lines):
    """Ordered list of the section's time-history records for export:
    the measured/control channels (named grid slots before Drive), Drive,
    and the Error record when the section stores one. The frequency-
    domain tail records (Coherence, TF) are excluded - their arrays are
    not time data. Each entry carries name, number, type, n, unit and the
    float32 samples SCALED to the display unit (g for the measured /
    Error channels, V for Drive - the measured channels are stored in
    SI m/s^2 and VcpNT's exports show them in g; see the module notes)."""
    rec_size = rec_size or _record_size_from_tokens(vf.data, sec_start, sec_end) \
        or 776 + 4 * N_POINTS
    npts = (rec_size - 776) // 4
    if not (MIN_POINTS <= npts <= MAX_POINTS):
        # the caller's record size was unusable for this section - retry
        # with the section's own token spacing, then the default
        rec_size = _record_size_from_tokens(vf.data, sec_start, sec_end) \
            or 776 + 4 * N_POINTS
    grid_base = _grid_base(vf, sec_start, sec_end, rec_size)
    slots = section_slots(vf, sec_start, sec_end, rec_size, grid_base)
    kd = next((i for i, s in enumerate(slots) if s["name"] == "Drive"), None)
    if kd is None:
        return []
    measured = [s for s in slots if s["k"] < slots[kd]["k"]]
    error = next((s for s in slots[kd + 1:] if s["name"] == "Error"), None)
    # Export order: Drive stays the first channel block (like the
    # reference exports), then the measured/control channels in grid
    # order, then the Error record when the section stores one.
    chosen = [slots[kd]] + measured + ([error] if error else [])

    defs = setup_channels(hdr_lines)
    if len(defs) == len(measured):
        defs_by_slot = dict((m["k"], (defs[i][1], defs[i][2])) for i, m in enumerate(measured))
    else:
        # counts differ (partial setup text): look each record name up in
        # the definitions instead (first definition of a name wins)
        defs_by_slot = {}
        first_def = {}
        for (nm, num, typ) in defs:
            first_def.setdefault(nm, (num, typ))
        for m in measured:
            defs_by_slot[m["k"]] = first_def.get(m["name"], (None, None))
    entries = []
    for s in chosen:
        name = s["name"]
        raw = struct.unpack_from("<%df" % s["n"], vf.data, s["data_pos"])
        if name == "Drive":
            scale, unit = 1.0, "V"
        elif name == "Error":
            # the control-error history: stored and exported raw in % of
            # the reference level (verified against the 2026 New_Data txt)
            scale, unit = 1.0, "%"
        else:
            # measured/control channels: stored m/s^2, displayed in g
            scale, unit = 1.0 / G, "g"
        number, ctype = defs_by_slot.get(s["k"], (None, None))
        if number is None:
            number = 0 if name == "Drive" else s["k"]
        if ctype is None:
            ctype = "D" if name == "Drive" else "M"
        entries.append({"name": name, "number": number, "type": ctype,
                        "n": s["n"], "values": tuple(v * scale for v in raw),
                        "unit": unit})
    return entries


def _channel_lines(measurement_idx, ch_idx, name, number, ctype, values, n_points, sample_rate, unit="V"):
    """One [Measurement.N.Channel.K] block for a time-history record
    (same header order as the reference Drive block). No trailing blank
    line - the caller joins blocks."""
    out = []
    out.append("[Measurement.%d.Channel.%d]" % (measurement_idx, ch_idx))
    out.append("Channel.Name:\t" + name)
    out.append("Channel.Number:\t" + str(number))
    out.append("Channel.Type:\t" + ctype)
    out.append("Channel.SubType:\t      ")
    peak = max(values, key=lambda v: abs(v)) if values else 0.0
    out.append("Peak.Measured:\t" + fmt_peak4(peak))
    out.append("Units.X:\ts")
    out.append("Units.Y:\t" + unit)
    out.append("Function.Type:\tTime")
    out.append("Function.Data.Type:\tReal")
    out.append("Function.Size:\t%d" % n_points)
    for k in range(n_points):
        out.append("Value.%d:\t%s\t%s" % (k + 1, fmt_time_axis(k / sample_rate), fmt_g(values[k])))
    return out


def fmt_peak4(value):
    """Channel-block peak format: '%.4g' with a German decimal comma -
    the reference exports print the signed sample of largest magnitude
    with 4 significant digits (e.g. 0,101739 -> '0,1017', 0,457977 ->
    '0,458', 0 -> '0')."""
    if value == 0:
        value = 0.0          # avoid a '-0'
    return ("%.4g" % value).replace(".", ",")


def decode(path, out_dir=None, write_csvs=True, all_sections=False, all_channels=False, *, return_data=False):
    """Decode a .rcs file. Default: one txt export (the first section's
    Drive time history, reference-identical) + one CSV per measurement.
    --all-channels: first section, every stored time-history record.
    --all-sections: every recorded section (single shock) of the file,
    one [Measurement.N] per section. Sections are decoded in file order;
    a section's shock index = its position (section 0 = shock 1)."""
    vf = VcpntFile(path)
    hdr_lines = vf.extract_header_text()
    hdr = ShockHeader(hdr_lines if hdr_lines is not None else [])
    sections = vf.find_sections()
    if not sections:
        raise ValueError("no data sections found in %s (unrecognised layout?)" % path)
    stamps = vf.measurement_timestamps("shock", 1)
    dt = stamps[0] if stamps else None

    chosen_sections = sections if all_sections else sections[:1]
    count = len(chosen_sections)
    rec_file = file_rec_size(vf)      # robust file-wide grid spacing
    lines = list(hdr_lines or [])
    if lines:
        lines.append("")
    lines.append("[Measurements]")
    lines.append("Measurements.Count:\t ")
    peak_channel_desc = None
    all_channel_blocks = []       # (measurement_idx, channels) for the csvs

    for mi, (s0, s1) in enumerate(chosen_sections, start=1):
        rec_size = rec_file
        npts = (rec_size - 776) // 4 if rec_size else 0
        if not rec_size or not (MIN_POINTS <= npts <= MAX_POINTS):
            _, rec_size = find_drive_record(vf, s0, s1)
        if all_channels:
            channels = section_time_channels(vf, s0, s1, rec_size, hdr_lines)
            if not channels:
                raise ValueError("no time-history records found in section %d of %s"
                                 % (mi, path))
        else:
            drive_data_off, rs_sec = find_drive_record(vf, s0, s1)
            if drive_data_off is None:
                raise ValueError("no Drive record found in section %d of %s" % (mi, path))
            if not rec_size or not (MIN_POINTS <= ((rec_size - 776) // 4) <= MAX_POINTS):
                rec_size = rs_sec
            n_points = (rec_size - 776) // 4
            if not (MIN_POINTS <= n_points <= MAX_POINTS):
                n_points = N_POINTS
            drive = struct.unpack_from("<%df" % n_points, vf.data, drive_data_off)
            channels = [{"name": "Drive", "number": 0, "type": "D",
                         "n": n_points, "values": drive, "unit": "V"}]
        all_channel_blocks.append((mi, channels))

        peak_channel = max(max(c["values"], key=lambda v: abs(v)) for c in channels)
        if peak_channel_desc is None or abs(peak_channel) > abs(peak_channel_desc):
            peak_channel_desc = min(channels[0]["values"]) if channels[0]["values"] else 0.0
        level_db = hdr.levels[0][0] if hdr.levels else -12.0
        level_pulses = hdr.levels[0][1] if hdr.levels else 5

        lines.append("")
        lines.append("[Measurement.%d]" % mi)
        lines.append("Measurement.Number:\t%d" % mi)
        lines.append("")
        lines.append("[Measurement.%d.Description]" % mi)
        from vcpnt_common import fmt_date, fmt_time
        lines.append("Date:\t" + fmt_date(dt))
        lines.append("Time:\t" + fmt_time(dt))
        lines.append("Source.Filename:\t" + vf.stem)
        for c in range(1, 26):
            lines.append("User.Comment.%d:\t" % c)
        lines.append("")
        lines.append("Pulses.Elapsed:\t0")
        lines.append("Pulses.Remaining:\t%d" % hdr.total_pulses())
        lines.append("Pulses.Level.Elapsed:\t0")
        lines.append("Pulses.Level.Remaining:\t%d" % level_pulses)
        lines.append("Level:\t" + ("%.3f" % level_db).replace(".", ","))
        lines.append("Resolution:\t" + fmt_g(1.0 / hdr.sample_rate))
        lines.append("Peak.Measured:\t" + fmt_g(peak_channel_desc))
        lines.append("Peak.Request:\t%d" % int(round(hdr.peak_request)))
        lines.append("")
        lines.append("[Measurement.%d.Channels]" % mi)
        if all_channels and len(channels) > 1:
            lines.append("Channels.Count:\t%d" % len(channels))
        else:
            lines.append("Channels.Count:\t ")
        lines.append("")
        for ci, ch in enumerate(channels):
            if ci:
                lines.append("")
            lines.extend(_channel_lines(mi, ci + 1, ch["name"], ch["number"],
                                        ch["type"], ch["values"], ch["n"],
                                        hdr.sample_rate, ch.get("unit", "V")))
    lines.append("")     # the reference export ends with a blank line

    # Curve Analyzer: expose all requested shock channels in memory.
    if return_data:
        measurements = []
        for mi, channels in all_channel_blocks:
            measurements.append({"dt": None, "channels": [
                {"name": ch["name"], "number": ch["number"], "type": ch["type"],
                 "units_x": "s", "units_y": ch["unit"], "ftype": "Time", "fdata": "Real",
                 "size": ch["n"], "x": [k / hdr.sample_rate for k in range(ch["n"])],
                 "y": ch["values"], "ph": None} for ch in channels]})
        return {"measurements": measurements, "header": hdr_lines,
                "layout": {"sample_rate": hdr.sample_rate,
                           "sample_rate_source": hdr.sample_rate_source}}

    with open(os.path.join(out_dir or os.path.dirname(os.path.abspath(path)),
                           vf.base + ".txt"), "w", encoding="utf-8", newline="") as fh:
        for line in lines:
            fh.write(line + "\r\n")

    csvs = []
    if write_csvs:
        for mi, channels in all_channel_blocks:
            csv_path = os.path.join(out_dir or os.path.dirname(os.path.abspath(path)),
                                    "%s_Measurement_%d.csv" % (vf.base, mi))
            blocks = []
            for ch in channels:
                b = ["Channel: %s" % ch["name"],
                     "Type: Time (Real), Units: s | %s" % ch.get("unit", "V"),
                     "Time, " + ch["name"]]
                for k in range(ch["n"]):
                    b.append("%s,%s" % (k / hdr.sample_rate, ch["values"][k]))
                blocks.append("\n".join(b))
            with open(csv_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n\n".join(blocks))
                fh.write("\n")
            csvs.append(csv_path)
    return (os.path.join(out_dir or os.path.dirname(os.path.abspath(path)), vf.base + ".txt"),
            csvs)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    out = None
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    txt, csvs = decode(src, out,
                       all_sections="--all-sections" in sys.argv,
                       all_channels="--all-channels" in sys.argv)
    print("wrote:", txt)
    print("csv files:", len(csvs))
