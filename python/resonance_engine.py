#!/usr/bin/env python3
# code belongs to: Vincent Jovan Wijaya
"""Desktop resonance screening for Element sine-sweep Excel exports.

Run: python resonance_analyzer.py [workbook.xlsx]
Batch: python resonance_analyzer.py workbook.xlsx --batch results.csv

Dependencies: numpy, scipy, matplotlib, openpyxl; Tkinter for the desktop UI.
No measurement is modified. 'Accepted' always means accepted by the screening
rules, not an independently verified beam eigenfrequency. See README.md.
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from openpyxl import load_workbook
from scipy.signal import find_peaks, savgol_filter

VERSION = "1.2"
DB3 = 20.0 * math.log10(math.sqrt(2.0))
ACCEPTED = "Accepted (screening)"
REVIEW = "Candidate / review"
COLORS = {"tip": "#2563eb", "near": "#8b5cf6", "base": "#556579",
          "accepted": "#00856a", "candidate": "#c47716", "muted": "#8794a5"}


@dataclass
class Channel:
    name: str
    amplitude: np.ndarray
    phase: np.ndarray | None = None


@dataclass
class Sweep:
    sheet: str
    run: str
    frequency: np.ndarray
    channels: dict[str, Channel]
    reference_profile: np.ndarray | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def beams(self):
        return sorted({m.group(1) for name in self.channels
                       if (m := re.fullmatch(r"R_([A-Z])_\d+", name))})


@dataclass
class Settings:
    candidate_prominence_db: float = 3.0
    accepted_prominence_db: float = 6.0
    min_amplification: float = 2.0
    min_phase_change_deg: float = 45.0
    max_phase_change_deg: float = 210.0
    min_phase_consistency: float = 0.65
    response_prominence_db: float = 1.0
    max_reference_dip_db: float = 6.0
    reference_floor_g: float = 0.01
    smoothing_points: int = 1

    def validate(self):
        for name, value in asdict(self).items():
            if not np.isfinite(value):
                raise ValueError(f"{name} must be finite.")
        if self.candidate_prominence_db <= 0:
            raise ValueError("Candidate prominence must be positive.")
        if self.accepted_prominence_db < self.candidate_prominence_db:
            raise ValueError("Accepted prominence must be >= candidate prominence.")
        if self.min_amplification <= 0 or self.reference_floor_g <= 0:
            raise ValueError("Amplification and reference floor must be positive.")
        if not 0 <= self.min_phase_change_deg < self.max_phase_change_deg:
            raise ValueError("Phase change limits must satisfy 0 <= minimum < maximum.")
        if not 0 <= self.min_phase_consistency <= 1:
            raise ValueError("Phase consistency must be between 0 and 1.")
        if self.response_prominence_db < 0 or self.max_reference_dip_db <= 0:
            raise ValueError("Response prominence must be >= 0; reference dip > 0.")
        if self.smoothing_points not in (1, 5, 7, 9, 11):
            raise ValueError("Smoothing must be 1 (off), 5, 7, 9 or 11 points.")


@dataclass
class Peak:
    frequency_hz: float
    index: int
    amplification: float
    response_g: float
    reference_g: float
    prominence_db: float
    status: str
    phase_at_peak_deg: float | None = None
    phase_change_deg: float | None = None
    phase_consistency: float | None = None
    phase_low_hz: float | None = None
    phase_high_hz: float | None = None
    half_power_low_hz: float | None = None
    half_power_high_hz: float | None = None
    width_hz: float | None = None
    raw_response_peak_hz: float | None = None
    reference_dip_db: float | None = None
    tip_to_near_ratio: float | None = None
    control_mismatch_db: float | None = None
    control_phase_difference_deg: float | None = None
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    is_global_maximum: bool = False


@dataclass
class Analysis:
    sweep: Sweep
    beam: str
    response: str
    reference: str
    settings: Settings
    transmissibility: np.ndarray
    detection_db: np.ndarray
    relative_phase_deg: np.ndarray
    valid: np.ndarray
    peaks: list[Peak]
    notes: list[str]

    @property
    def accepted(self):
        return [p for p in self.peaks if p.status == ACCEPTED]


def _number(value):
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return np.nan


def read_workbook(path: str | Path) -> list[Sweep]:
    """Read by channel names and adjacent phase columns, never fixed positions.

    The second row in this export contains run IDs. Repeated channel groups
    (e.g. Powerlock_xy runs 002 and 003) remain independent sweeps.
    Charts-only worksheets are ignored. A workbook is opened read-only.
    """
    workbook = load_workbook(Path(path), read_only=True, data_only=True)
    sweeps = []
    try:
        for sheet in workbook:
            rows = list(sheet.values)
            if len(rows) < 3:
                continue
            header_index = next((i for i, r in enumerate(rows[:25])
                                 if any(re.search(r"(?:X-Data|Frequency|Frequenz).*\[Hz\]",
                                                  str(v), re.I) for v in r)), None)
            if header_index is None:
                continue
            header = rows[header_index]
            freq_col = next(i for i, h in enumerate(header)
                            if re.search(r"(?:X-Data|Frequency|Frequenz).*\[Hz\]", str(h), re.I))
            following = rows[header_index + 1:]
            metadata = following[0] if not np.isfinite(_number(following[0][freq_col])) else ()
            data = [[_number(v) for v in row] for row in following
                    if np.isfinite(_number(row[freq_col]))]
            if len(data) < 3:
                continue
            array = np.asarray(data, dtype=float)
            f = array[:, freq_col]
            if np.any(f <= 0):
                raise ValueError(f"{sheet.title}: frequency must be positive.")
            if not (np.all(np.diff(f) > 0) or np.all(np.diff(f) < 0)):
                raise ValueError(f"{sheet.title}: repeated or non-monotonic frequencies. "
                                 "Export each sweep direction separately.")
            order = np.argsort(f)
            f, array = f[order], array[order]
            groups: list[tuple[str, dict]] = []
            channels: dict[str, Channel] = {}
            label = ""
            profile = None
            for j, h in enumerate(header):
                text = str(h or "").strip()
                if text == "Reference [g]":
                    profile = array[:, j]
                    continue
                match = re.match(r"^(C\d+|R_[A-Z]_\d+|Control|Drive)(?:\s|$)", text)
                if not match:
                    continue
                name = match.group(1)
                if name in channels:
                    groups.append((label, channels))
                    channels, label = {}, ""
                if not label and metadata and metadata[j]:
                    label = str(metadata[j])
                phase = None
                if j + 1 < len(header) and re.match(r"Phase\s*\[Deg\]", str(header[j+1]), re.I):
                    phase = array[:, j+1].copy()
                channels[name] = Channel(name, array[:, j].copy(), phase)
            if channels:
                groups.append((label, channels))
            for k, (label, channels) in enumerate(groups):
                if "C3" not in channels or not any(n.startswith("R_") for n in channels):
                    continue
                notes = []
                if any(n.endswith("_3") for n in channels):
                    notes.append("Sensor _3 is available; its position and direction were not specified.")
                if len(groups) > 1:
                    notes.append("This worksheet contains separate runs; they are not merged or treated as repeatability evidence.")
                sweeps.append(Sweep(sheet.title, label or f"Run {k+1}", f.copy(), channels,
                                    None if profile is None else profile.copy(), notes))
    finally:
        workbook.close()
    if not sweeps:
        raise ValueError("No sweep found. Expected X-Data [Hz], C3 [g], R_A_2-style "
                         "channel names, and optional adjacent Phase [Deg] columns.")
    return sweeps


def segments(valid, frequency):
    """Return contiguous usable runs without bridging missing rows or large gaps."""
    ids = np.flatnonzero(valid)
    if not len(ids):
        return []
    step = np.median(np.diff(frequency))
    breaks = (np.diff(ids) > 1) | (np.diff(frequency[ids]) > 3.5 * step)
    return np.split(ids, np.flatnonzero(breaks) + 1)


def relative_phase(response, reference, valid, frequency):
    result = np.full(len(frequency), np.nan)
    if response.phase is None or reference.phase is None:
        return result
    good = valid & np.isfinite(response.phase) & np.isfinite(reference.phase)
    for ids in segments(good, frequency):
        # Subtract phases sharing the export's common reference. Wrapping the
        # difference first handles +180/-180 without introducing 360-deg steps.
        difference = np.deg2rad(response.phase[ids] - reference.phase[ids])
        result[ids] = np.rad2deg(np.unwrap(np.angle(np.exp(1j * difference))))
    return result


def _smooth(values, points):
    if points > 1 and len(values) >= points:
        return savgol_filter(values, points, polyorder=2, mode="interp")
    return values.copy()


def _crossing(f, y, p, boundary, side, level):
    """First actual level crossing inside this peak's prominence basin."""
    i = p
    while i != boundary:
        j = i + side
        if y[j] <= level:
            alpha = (level-y[i]) / (y[j]-y[i]) if y[j] != y[i] else 0.0
            return float(f[i] + alpha*(f[j]-f[i])), j
        i = j
    return None, None


def analyze(sweep: Sweep, beam="A", reference="C3", sensor="2",
            settings: Settings | None = None, *, response_name=None) -> Analysis:
    settings = settings or Settings()
    settings.validate()
    response = response_name if response_name is not None else f"R_{beam}_{sensor}"
    if response not in sweep.channels or reference not in sweep.channels:
        raise ValueError(f"Required channels {response} and {reference} are not available.")
    if response == reference:
        raise ValueError("Response and reference must be different channels.")
    tip, base = sweep.channels[response], sweep.channels[reference]
    f, n = sweep.frequency, len(sweep.frequency)
    valid = (np.isfinite(tip.amplitude) & np.isfinite(base.amplitude)
             & (tip.amplitude > 0) & (base.amplitude >= settings.reference_floor_g))
    t, db = np.full(n, np.nan), np.full(n, np.nan)
    t[valid] = tip.amplitude[valid] / base.amplitude[valid]
    phase = relative_phase(tip, base, valid, f)
    peaks: list[Peak] = []
    near = sweep.channels.get(f"R_{beam}_1")
    global_index = int(np.nanargmax(t)) if valid.any() else -1
    notes = list(sweep.notes)
    notes.append(f"{n - int(valid.sum())} rows excluded (zero, missing or below reference floor).")
    if not np.isfinite(phase).any():
        notes.append("Relative phase is unavailable; no peak can pass the phase check.")
    for ids in segments(valid, f):
        if len(ids) < 3:
            continue
        sf = f[ids]
        y = _smooth(20*np.log10(t[ids]), settings.smoothing_points)
        db[ids] = y
        raw_y = 20*np.log10(tip.amplitude[ids])
        raw_peaks, _ = find_peaks(raw_y, prominence=settings.response_prominence_db)
        found, props = find_peaks(y, prominence=settings.candidate_prominence_db)
        for k, p in enumerate(found):
            i = int(ids[p])
            lb, rb = int(props["left_bases"][k]), int(props["right_bases"][k])
            prom = float(props["prominences"][k])
            left, li = _crossing(sf, y, p, lb, -1, y[p]-DB3)
            right, ri = _crossing(sf, y, p, rb, 1, y[p]-DB3)
            # Phase is inspected across a finite neighborhood, not one adjacent
            # sample. Expand narrow half-power windows to 3 samples each side,
            # but stop at the peak's prominence bases. No cross-gap interpolation.
            lo = max(lb, min(li if li is not None else p, p-3))
            hi = min(rb, max(ri if ri is not None else p, p+3))
            pk = Peak(float(f[i]), i, float(t[i]), float(tip.amplitude[i]),
                      float(base.amplitude[i]), prom, REVIEW,
                      phase_low_hz=float(sf[lo]), phase_high_hz=float(sf[hi]),
                      half_power_low_hz=left, half_power_high_hz=right,
                      is_global_maximum=(i == global_index))
            if np.isfinite(phase[i]):
                pk.phase_at_peak_deg = float(phase[i])
            if left is not None and right is not None:
                pk.width_hz = right-left
                df = float(np.median(np.diff(sf[max(0,p-3):min(len(sf),p+4)])))
                if pk.width_hz < 4*df:
                    pk.notes.append("Narrow peak: fewer than four frequency intervals across -3 dB width; refine the sweep.")
            else:
                pk.notes.append("No isolated pair of -3 dB crossings; width is unavailable.")
            if pk.amplification < settings.min_amplification:
                pk.reasons.append(f"Amplification < {settings.min_amplification:g}x")
            if prom < settings.accepted_prominence_db:
                pk.reasons.append(f"Prominence < {settings.accepted_prominence_db:g} dB")
            ph = phase[ids[lo:hi+1]]
            if len(ph) < 5 or not np.isfinite(ph).all() or lo == p or hi == p:
                pk.reasons.append("Too few valid phase points on both sides")
            else:
                # Median pairs reduce isolated phase noise. Use the same trimmed
                # path for total variation so consistency cannot exceed one.
                start, end = float(np.median(ph[:2])), float(np.median(ph[-2:]))
                path = np.r_[start, ph[2:-2], end]
                change = end-start
                variation = float(np.abs(np.diff(path)).sum())
                consistency = min(1.0, abs(change)/variation) if variation > 1e-8 else 0.0
                pk.phase_change_deg, pk.phase_consistency = float(change), consistency
                if abs(change) < settings.min_phase_change_deg:
                    pk.reasons.append(f"Phase change < {settings.min_phase_change_deg:g} deg")
                if abs(change) > settings.max_phase_change_deg:
                    pk.reasons.append("Large phase rotation: possible overlapping modes / irregular data")
                if consistency < settings.min_phase_consistency:
                    pk.reasons.append("Phase transition is irregular")
            matches = raw_peaks[(raw_peaks >= lo) & (raw_peaks <= hi)]
            if len(matches):
                closest = int(matches[np.argmin(abs(sf[matches]-sf[p]))])
                pk.raw_response_peak_hz = float(sf[closest])
            else:
                pk.reasons.append("No matching peak in response acceleration; check reference dip")
            # A ratio may peak because its denominator dips. Inspect reference
            # shoulders over a slightly wider region bounded by the same basin.
            dl, dr = max(lb, p-2*max(p-lo,3)), min(rb, p+2*max(hi-p,3))
            bdb = 20*np.log10(base.amplitude[ids])
            shoulder = (float(np.median(bdb[dl:min(dl+3,p)]))
                        + float(np.median(bdb[max(p+1,dr-2):dr+1]))) / 2
            pk.reference_dip_db = max(0.0, shoulder-float(bdb[p]))
            if pk.reference_dip_db > settings.max_reference_dip_db:
                pk.reasons.append(f"Reference dip > {settings.max_reference_dip_db:g} dB")
            if near is not None and np.isfinite(near.amplitude[i]) and near.amplitude[i] > 0:
                pk.tip_to_near_ratio = float(tip.amplitude[i]/near.amplitude[i])
            c1, c2 = sweep.channels.get("C1"), sweep.channels.get("C2")
            if c1 and c2 and c1.amplitude[i] > 0 and c2.amplitude[i] > 0:
                pk.control_mismatch_db = float(20*np.log10(c1.amplitude[i]/c2.amplitude[i]))
                if abs(pk.control_mismatch_db) > 3:
                    pk.notes.append("C1/C2 amplitudes differ by >3 dB here; inspect table/fixture motion.")
                if c1.phase is not None and c2.phase is not None:
                    d = (c1.phase[i]-c2.phase[i]+180) % 360 - 180
                    if np.isfinite(d):
                        pk.control_phase_difference_deg = float(d)
                        if abs(d) > 20:
                            pk.notes.append("C1/C2 phases differ by >20 deg here; inspect table/fixture motion.")
            if not pk.reasons:
                pk.status = ACCEPTED
            peaks.append(pk)
        # A rising edge at a truncated sweep is not a bracketed resonance.
        for p, neighbor in ((0,1),(len(ids)-1,len(ids)-2)):
            i = int(ids[p])
            if t[i] >= settings.min_amplification and y[p] > y[neighbor]:
                peaks.append(Peak(float(f[i]), i, float(t[i]), float(tip.amplitude[i]),
                                  float(base.amplitude[i]), 0.0, REVIEW,
                                  reasons=["Peak at valid-data boundary: not bracketed by data"],
                                  is_global_maximum=(i == global_index)))
    peaks.sort(key=lambda p: p.frequency_hz)
    return Analysis(sweep, beam, response, reference, settings, t, db, phase, valid, peaks, notes)


def export_results(path, analyses):
    """CSV or tab-separated TXT: candidates, reasons and reproducible settings."""
    path = Path(path)
    if path.suffix.lower() not in (".csv", ".txt"):
        raise ValueError("Choose a .csv or .txt export filename.")
    txt = path.suffix.lower() == ".txt"
    fields = ["sheet", "run", "beam", "response", "reference"]
    fields += list(Peak.__dataclass_fields__) + ["settings", "analysis_notes"]
    with open(path, "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields, delimiter="\t" if txt else ",")
        writer.writeheader()
        for result in analyses:
            for peak in result.peaks:
                row = asdict(peak)
                row["reasons"] = "; ".join(peak.reasons) or "All screening checks passed"
                row["notes"] = "; ".join(peak.notes)
                row.update(sheet=result.sweep.sheet, run=result.sweep.run, beam=result.beam,
                           response=result.response, reference=result.reference,
                           settings=str(asdict(result.settings)), analysis_notes="; ".join(result.notes))
                if txt:
                    row = {key: (format(float(value), ".17g").replace(".", ",")
                                 if isinstance(value, (float, np.floating)) else value)
                           for key, value in row.items()}
                writer.writerow(row)


def draw_peak_selection(axes, result, peak):
    """Add selection decorations without rebuilding axes or losing navigation."""
    artists = []
    if peak is None:
        return artists
    for ax in axes:
        artists.append(ax.axvline(peak.frequency_hz, color="#203148", lw=.9, ls=":", alpha=.7))
        if peak.phase_low_hz is not None:
            artists.append(ax.axvspan(peak.phase_low_hz, peak.phase_high_hz,
                                      color="#e5ae53", alpha=.13))
    if peak.width_hz is not None:
        level = 10**(result.detection_db[peak.index]/20)/math.sqrt(2)
        artists.extend(axes[1].plot([peak.half_power_low_hz, peak.half_power_high_hz],
                                    [level, level], color="#203148", lw=1.2, marker="|"))
    return artists


class PlotNavigation:
    """Cursor-centred wheel zoom and middle-drag pan, sharing toolbar history.

    Axis transforms make zoom work on both logarithmic amplitudes and signed
    linear phase. Matplotlib's shared axes keep the frequency plots aligned.
    The same controller works with desktop and headless Matplotlib canvases.
    """
    def __init__(self, canvas, toolbar):
        self.canvas, self.toolbar = canvas, toolbar
        self.axes = []
        self.y_only = False
        self._drag = None
        self._connections = [canvas.mpl_connect(name, method) for name, method in [
            ("scroll_event", self.on_scroll), ("button_press_event", self.on_press),
            ("motion_notify_event", self.on_motion), ("button_release_event", self.on_release)]]

    def capture_view(self):
        return [(ax.get_xlim(), ax.get_ylim()) for ax in self.axes]

    def restore_view(self, view):
        for ax, (xlim, ylim) in zip(self.axes, view):
            if ax.get_xscale() != "log" or min(xlim) > 0:
                ax.set_xlim(xlim)
            if ax.get_yscale() != "log" or min(ylim) > 0:
                ax.set_ylim(ylim)

    def set_axes(self, axes, previous_view=None):
        if self._drag is not None:
            self._drag.end_pan()
            self._drag = None
        self.axes = list(axes)
        mode = str(self.toolbar.mode).lower()
        for ax in self.axes:
            ax.set_navigate_mode("PAN" if "pan" in mode else "ZOOM" if "zoom" in mode else None)
        # Register the newly drawn full range as Home before restoring a zoom.
        self.toolbar.update()
        self.toolbar.push_current()
        if previous_view is not None:
            self.restore_view(previous_view)
            self.toolbar.push_current()

    def inspect(self):
        """Leave toolbar pan/box modes so left clicks select resonance peaks."""
        mode = str(self.toolbar.mode).lower()
        if "pan" in mode:
            self.toolbar.pan()
        elif "zoom" in mode:
            self.toolbar.zoom()

    def reset(self):
        if self.axes:
            self.toolbar.home()

    @staticmethod
    def _scaled_limits(axis, limits, centre, factor):
        if centre is None or not np.isfinite(centre):
            return None
        transform = axis.get_transform()
        with np.errstate(all="ignore"):
            values = transform.transform(np.asarray([*limits, centre], dtype=float))
            new = values[2] + (values[:2] - values[2]) * factor
            result = np.asarray(transform.inverted().transform(new), dtype=float)
        if (not np.isfinite(result).all() or result[0] == result[1]
                or (axis.get_scale() == "log" and np.any(result <= 0))):
            return None
        return tuple(result)

    def on_scroll(self, event):
        ax = event.inaxes
        if ax not in self.axes or self._drag is not None:
            return
        step = event.step
        if not step or not np.isfinite(step):
            return
        factor = 1.25 ** (-float(np.clip(step, -8, 8)))
        keys = set((event.key or "").lower().split("+"))
        # Shift constrains the gesture to frequency; Ctrl constrains amplitude/phase.
        only_y = bool(keys & {"control", "ctrl"}) or (self.y_only and "shift" not in keys)
        only_x = "shift" in keys and not only_y
        xlim = None if only_y else self._scaled_limits(ax.xaxis, ax.get_xlim(), event.xdata, factor)
        ylim = None if only_x else self._scaled_limits(ax.yaxis, ax.get_ylim(), event.ydata, factor)
        if xlim is None and ylim is None:
            return
        if xlim is not None:
            ax.set_xlim(xlim)
        if ylim is not None:
            ax.set_ylim(ylim)
        self.toolbar.push_current()
        self.canvas.draw_idle()

    def on_press(self, event):
        if event.inaxes not in self.axes:
            return
        if event.button == 1 and event.dblclick and not self.toolbar.mode:
            self.reset()
        elif event.button == 2:
            self._drag = event.inaxes
            self._drag.start_pan(event.x, event.y, 1)

    def on_motion(self, event):
        if self._drag is not None and event.x is not None and event.y is not None:
            self._drag.drag_pan(1, event.key, event.x, event.y)
            self.canvas.draw_idle()

    def on_release(self, event):
        if self._drag is not None:
            self._drag.end_pan()
            self._drag = None
            self.toolbar.push_current()
            self.canvas.draw_idle()

    def zoom_frequency(self, low, high):
        if self.axes and np.isfinite([low, high]).all() and low < high:
            self.axes[0].set_xlim(low, high)
            self.toolbar.push_current()
            self.canvas.draw_idle()


def draw_analysis(figure, result: Analysis, selected=None, log_frequency=True,
                  show_near=True, show_candidates=True, xlim=None):
    """Shared plotting path for the desktop window and headless verification."""
    from matplotlib.lines import Line2D
    from matplotlib.ticker import ScalarFormatter
    figure.clear()
    axes = figure.subplots(3, 1, sharex=True, gridspec_kw={"height_ratios": [1, 1.25, 1]})
    f, ch = result.sweep.frequency, result.sweep.channels
    valid = result.valid
    tip, base = ch[result.response], ch[result.reference]
    near_name = f"R_{result.beam}_1"
    for name, color, label in [(result.response, COLORS["tip"], f"{result.response} response"),
                                (result.reference, COLORS["base"], f"{result.reference} reference")]:
        axes[0].plot(f, np.where(valid, ch[name].amplitude, np.nan), color=color, lw=1.25, label=label)
    if show_near and near_name in ch and near_name not in (result.response, result.reference):
        axes[0].plot(f, np.where(valid, ch[near_name].amplitude, np.nan),
                     color=COLORS["near"], lw=.85, alpha=.8, label=f"{near_name} near mount")
        ratio = np.divide(ch[near_name].amplitude, base.amplitude,
                          out=np.full(len(f), np.nan), where=valid)
        axes[1].plot(f, ratio, color=COLORS["near"], lw=.85, alpha=.65,
                     label=f"{near_name}/{result.reference}")
    axes[1].plot(f, result.transmissibility, color=COLORS["tip"], lw=1.2,
                 label=f"{result.response}/{result.reference}")
    if result.settings.smoothing_points > 1:
        axes[1].plot(f, 10**(result.detection_db/20), color="#132b4d", lw=.8, alpha=.65,
                     label="Smoothed detection curve")
    axes[1].axhline(result.settings.min_amplification, color=COLORS["muted"],
                   lw=.8, ls="--", label="Minimum amplification")
    axes[2].plot(f, result.relative_phase_deg, color="#7352ad", lw=1.05)
    if not np.isfinite(result.relative_phase_deg).any():
        axes[2].text(.5,.5,"No relative phase data: peaks remain candidates",
                     transform=axes[2].transAxes, ha="center", va="center")
    for status, color, marker in [(REVIEW,COLORS["candidate"],"o"),(ACCEPTED,COLORS["accepted"],"D")]:
        group = [p for p in result.peaks if p.status == status and (show_candidates or status == ACCEPTED)]
        for ax_index, attr in [(0,"response_g"),(1,"amplification"),(2,"phase_at_peak_deg")]:
            g = [p for p in group if getattr(p,attr) is not None]
            axes[ax_index].scatter([p.frequency_hz for p in g], [getattr(p,attr) for p in g],
                                   s=24 if status==REVIEW else 38, marker=marker,
                                   facecolors="none" if status==REVIEW else color,
                                   edgecolors=color, linewidths=1, zorder=5)
    # Label accepted peaks only; selectable candidate details stay in the table.
    labelled=[]
    for p in sorted(result.accepted,key=lambda p:p.amplification,reverse=True):
        if any(abs(math.log(p.frequency_hz/other.frequency_hz)) < .20 for other in labelled):
            continue
        labelled.append(p)
    for j, p in enumerate(sorted(labelled,key=lambda p:p.frequency_hz)):
        leftward=p.frequency_hz > .72*f[-1]
        axes[1].annotate(f"{p.frequency_hz:.1f} Hz", (p.frequency_hz,p.amplification),
                         xytext=(-4 if leftward else 4,8+(j%2)*10), textcoords="offset points", fontsize=8,
                         color=COLORS["accepted"],ha="right" if leftward else "left",rotation=0)
    draw_peak_selection(axes, result, selected)
    axes[0].set_yscale("log")
    axes[1].set_yscale("log")
    axes[0].set_ylabel("Acceleration [g]")
    axes[1].set_ylabel("Amplification [x]")
    axes[2].set_ylabel("Relative phase [deg]\n(unwrapped)")
    axes[2].set_xlabel("Excitation frequency [Hz]")
    for ax in axes:
        ax.grid(True,which="major",alpha=.17,color="#63758c")
        ax.set_axisbelow(True)
        ax.spines[["top","right"]].set_visible(False)
        ax.tick_params(labelsize=8)
        ax.set_xscale("log" if log_frequency else "linear")
    if log_frequency:
        axes[2].xaxis.set_major_formatter(ScalarFormatter())
    good_f = f[valid]
    if len(good_f):
        axes[2].set_xlim(xlim if xlim else (good_f[0], good_f[-1]))
    axes[0].legend(loc="upper left", fontsize=7.5, framealpha=.85, ncol=3)
    handles, labels = axes[1].get_legend_handles_labels()
    handles += [Line2D([],[],marker="D",ls="",color=COLORS["accepted"],label="Accepted (screening)"),
                Line2D([],[],marker="o",ls="",color=COLORS["candidate"],mfc="none",label="Candidate / review")]
    axes[1].legend(handles=handles,loc="upper left",fontsize=7.2,ncol=2,framealpha=.85)
    figure.suptitle(f"Beam {result.beam}  |  {result.sweep.sheet}  |  {result.response} / {result.reference}",
                    fontsize=11,fontweight="bold",color="#203148")
    return axes


def draw_control_sensors(figure,result,log_frequency=True):
    """Draw the control channels for the embedded or detached graph."""
    figure.clear()
    axes=figure.subplots(2,1,sharex=True)
    s=result.sweep;f=s.frequency
    c1=s.channels.get("C1")
    for name,color in [("C1","#2563eb"),("C2","#00856a"),("C3","#a359bc"),("Control","#475569")]:
        channel=s.channels.get(name)
        if channel is None:continue
        valid=np.isfinite(channel.amplitude)&(channel.amplitude>0)
        axes[0].plot(f,np.where(valid,channel.amplitude,np.nan),label=name,lw=1,color=color)
        if c1 is not None and channel.phase is not None:
            phase=relative_phase(channel,c1,valid&(c1.amplitude>0),f)
            axes[1].plot(f,phase,label=f"{name} - C1",lw=1,color=color)
    axes[0].set_ylabel("Acceleration [g]");axes[0].set_yscale("log")
    axes[1].set_ylabel("Relative phase [deg]\n(unwrapped)");axes[1].set_xlabel("Excitation frequency [Hz]")
    for ax in axes:
        ax.set_xscale("log" if log_frequency else "linear");ax.grid(alpha=.2);ax.legend(fontsize=8)
    figure.suptitle(f"Control sensors  |  {s.sheet}",fontsize=11,fontweight="bold")
    return axes


METHOD_TEXT = """HOW THE SCREENING WORKS

1. Read each test sheet and run separately. C3 is the default measured base
   reference. R_A_2, R_B_2 or R_C_2 is the default tip response. Sensor _1 is
   shown for comparison; optional _3 channels have no assumed axis or position.

2. Divide response acceleration by reference acceleration at each frequency.
   Find local maxima of 20 log10(response/reference) with sufficient prominence.
   Defaults: candidates >=3 dB prominence; accepted peaks >=6 dB and >=2x gain.
   The global maximum receives no special acceptance privilege.

3. Subtract the reference phase from the response phase and unwrap the difference.
   This assumes all channel phase columns use the same controller reference and
   convention within that run. The sign is retained; either transition direction
   is allowed. There is no 'phase >=90 degrees' test.

4. Inspect the phase across the peak. Use -3 dB crossings when available, expanding
   narrow windows to 3 samples on each side, limited by the prominence bases.
   If crossings are absent, use the available neighboring points. Require >=5
   finite phase samples, a change of 45 to 210 degrees and consistency >=0.65.
   Consistency is absolute net phase change divided by total phase variation
   after median endpoint pairs. A jump of +180 to -180 alone is removed by
   unwrapping. An unresolved >180-degree change between samples can still be
   ambiguous and needs a finer sweep.

5. Require a nearby local maximum in the measured response acceleration with
   >=1 dB prominence. Flag a reference dip >6 dB relative to nearby shoulders.
   This conservative check can leave real, complex modes for manual review.

GREEN = accepted by these screening rules. AMBER = candidate needing review.
The table explains every failed check. The shaded selection marks the phase
window. The short horizontal line marks the -3 dB width, when measurable.
These are adjustable engineering heuristics, not universal resonance criteria.

FREQUENCY AND WIDTH
Reported frequency is the measured maximum of the selected detection curve.
With smoothing OFF this is an actual sampled transmissibility maximum. Smoothing
can move/attenuate narrow peaks, so OFF is the default. The -3 dB width is optional
descriptive information; it does not prove resonance. No modal damping is claimed
from an unresolved or overlapping peak. Resolution warnings do not reject an
otherwise supported peak: they limit the precision of its frequency/width.

WHAT THE EXPORT CAN ESTABLISH
Acceptance is evidence of resonance in the mounted system. Fixture/table modes,
beam modes and coupled modes require engineering interpretation. No raw time
signals, coherence, overload flags or sweep-rate history are supplied, so clipping,
coherence, steady-state settling and independent repeatability cannot be certified.
C1/C2 amplitude/phase differences are diagnostics, not full rotation measurements.
The two Powerlock_xy runs are kept separate; they may share recorded history.

USING THE WINDOW
Choose worksheet, run and beam. In Inspect mode click a table row or peak for its
checks and phase window. Selecting a peak preserves the current zoom and history.
Wheel: zoom at the cursor. Shift+wheel: frequency only. Ctrl+wheel: vertical only.
Middle-drag: pan. Zoom box: left-drag a rectangle. Pan: left-drag to move the view.
Inspect: return to peak selection. Double-click in Inspect mode or use Full range
to reset the current tab. Toolbar Back/Forward recall previous views. Frequency
zoom/pan stays aligned across plots; vertical zoom affects the plot under the
cursor. Zoom to peak focuses the selected frequency. Both plot tabs support these
controls. Enable Wheel: Y only to adjust the vertical range without holding Ctrl.
Detach graph opens a resizable snapshot with Y-only wheel zoom enabled. Close and
reopen it to display another selection. Apply changed thresholds to reanalyse.
Export results or graphs.
All workbook reads are read-only. The app never commands the shaker.
"""


class ResonanceApp:
    def __init__(self, root, initial_path=None):
        import tkinter as tk
        from tkinter import ttk
        from tkinter import font as tkfont
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.path, self.sweeps, self.result, self.selected = None, [], None, None
        self.cache, self.rows = {}, {}
        self.axes, self.selection_artists = [], []
        self.settings = Settings()
        root.title(f"Resonance Analyzer {VERSION}")
        root.geometry(f"{min(1440,root.winfo_screenwidth()-24)}x{min(940,root.winfo_screenheight()-45)}")
        root.minsize(1060,760)
        root.configure(background="#eef2f7")
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame",background="#eef2f7")
        style.configure("TLabel",background="#eef2f7",foreground="#203148",font=("Segoe UI",10))
        style.configure("Title.TLabel",font=("Segoe UI",19,"bold"))
        style.configure("Muted.TLabel",foreground="#607084",font=("Segoe UI",9))
        style.configure("Group.TLabel",font=("Segoe UI",10,"bold"))
        style.configure("TButton",padding=(9,6),font=("Segoe UI",9))
        style.configure("TCheckbutton",background="#eef2f7",font=("Segoe UI",9))
        style.configure("TRadiobutton",background="#eef2f7",font=("Segoe UI",10))
        style.configure("Treeview",font=("Segoe UI",9),rowheight=25)
        style.configure("Treeview.Heading",font=("Segoe UI",9,"bold"))
        available=set(tkfont.families(root))
        font_family=next((name for name in ("Segoe UI","DejaVu Sans","Arial") if name in available),"TkDefaultFont")
        for name,size,bold in [("TLabel",10,False),("Title.TLabel",19,True),("Muted.TLabel",9,False),
                               ("Group.TLabel",10,True),("TButton",9,False),("TCheckbutton",9,False),
                               ("TRadiobutton",10,False),("Treeview",9,False),("Treeview.Heading",9,True)]:
            style.configure(name,font=(font_family,size,"bold" if bold else "normal"))
        style.map("Treeview",background=[("selected","#dae7f5")],foreground=[("selected","#142840")])
        header = ttk.Frame(root,padding=(18,12))
        header.pack(fill="x")
        ttk.Label(header,text="Resonance Analyzer",style="Title.TLabel").pack(side="left")
        ttk.Button(header,text="Open workbook...",command=self.open_workbook).pack(side="right",padx=(6,0))
        ttk.Button(header,text="How it works",command=self.show_method).pack(side="right")
        self.file_var = tk.StringVar(value="Choose a sine-sweep Excel workbook to begin.")
        ttk.Label(root,textvariable=self.file_var,style="Muted.TLabel",padding=(20,0,10,9)).pack(fill="x")
        body = tk.PanedWindow(root,orient="horizontal",sashwidth=5,bg="#dce3ed",bd=0)
        body.pack(fill="both",expand=True,padx=12,pady=(0,8))
        sidebar_outer=ttk.Frame(body,width=270)
        side_canvas=tk.Canvas(sidebar_outer,bg="#eef2f7",highlightthickness=0,width=260)
        side_scroll=ttk.Scrollbar(sidebar_outer,orient="vertical",command=side_canvas.yview)
        side_canvas.configure(yscrollcommand=side_scroll.set)
        side_scroll.pack(side="right",fill="y");side_canvas.pack(side="left",fill="both",expand=True)
        sidebar=ttk.Frame(side_canvas,padding=(10,6))
        side_window=side_canvas.create_window((0,0),window=sidebar,anchor="nw")
        sidebar.bind("<Configure>",lambda e:side_canvas.configure(scrollregion=side_canvas.bbox("all")))
        side_canvas.bind("<Configure>",lambda e:side_canvas.itemconfigure(side_window,width=e.width))
        main = ttk.Frame(body,padding=(10,8))
        body.add(sidebar_outer,minsize=270,stretch="never")
        body.add(main,minsize=720,stretch="always")
        self.sheet_var,self.run_var = tk.StringVar(),tk.StringVar()
        self.beam_var,self.sensor_var,self.ref_var = tk.StringVar(value="A"),tk.StringVar(value="2"),tk.StringVar(value="C3")

        def combo(label,variable,callback,width=27):
            ttk.Label(sidebar,text=label,style="Group.TLabel").pack(anchor="w",pady=(8,3))
            widget=ttk.Combobox(sidebar,textvariable=variable,state="readonly",width=width)
            widget.pack(fill="x")
            widget.bind("<<ComboboxSelected>>",callback)
            return widget

        self.sheet_box=combo("Test configuration",self.sheet_var,self.on_sheet)
        self.run_box=combo("Run (as exported)",self.run_var,self.on_run)
        ttk.Label(sidebar,text="Beam",style="Group.TLabel").pack(anchor="w",pady=(13,5))
        beamrow=ttk.Frame(sidebar);beamrow.pack(fill="x")
        self.beam_buttons={}
        for beam in "ABC":
            button=ttk.Radiobutton(beamrow,text=f"Beam {beam}",variable=self.beam_var,value=beam,command=self.on_beam)
            button.pack(side="left",expand=True)
            self.beam_buttons[beam]=button
        self.sensor_box=combo("Response sensor",self.sensor_var,self.on_sensor)
        self.ref_box=combo("Base reference",self.ref_var,self.refresh)
        ttk.Separator(sidebar).pack(fill="x",pady=15)
        ttk.Label(sidebar,text="Screening thresholds",style="Group.TLabel").pack(anchor="w")
        grid=ttk.Frame(sidebar);grid.pack(fill="x",pady=(5,0));grid.columnconfigure(0,weight=1)
        self.setting_vars={}
        for row,(label,attr) in enumerate([
            ("Candidate prominence [dB]","candidate_prominence_db"),
            ("Accepted prominence [dB]","accepted_prominence_db"),
            ("Minimum amplification [x]","min_amplification"),
            ("Minimum phase change [deg]","min_phase_change_deg")]):
            ttk.Label(grid,text=label,style="Muted.TLabel").grid(row=row,column=0,sticky="w",pady=4)
            variable=tk.StringVar(value=f"{getattr(self.settings,attr):g}")
            self.setting_vars[attr]=variable
            ttk.Entry(grid,textvariable=variable,width=6).grid(row=row,column=1,sticky="e",padx=(4,0))
        ttk.Label(sidebar,text="Smoothing [points; 1 = off]",style="Muted.TLabel").pack(anchor="w",pady=(7,3))
        self.smooth_var=tk.StringVar(value="1")
        ttk.Combobox(sidebar,textvariable=self.smooth_var,values=[1,5,7,9,11],state="readonly",width=8).pack(anchor="w")
        actions=ttk.Frame(sidebar);actions.pack(fill="x",pady=8)
        ttk.Button(actions,text="Apply",command=self.apply_settings).pack(side="left",fill="x",expand=True)
        ttk.Button(actions,text="Advanced...",command=self.advanced).pack(side="left",padx=(5,0))
        self.log_var=tk.BooleanVar(value=True)
        self.near_var=tk.BooleanVar(value=True)
        self.candidate_var=tk.BooleanVar(value=True)
        ttk.Checkbutton(sidebar,text="Logarithmic frequency axis",variable=self.log_var,command=self.change_frequency_scale).pack(anchor="w",pady=2)
        ttk.Checkbutton(sidebar,text="Show near-mount sensor _1",variable=self.near_var,command=self.redraw).pack(anchor="w",pady=2)
        ttk.Checkbutton(sidebar,text="Show candidates needing review",variable=self.candidate_var,command=self.toggle_candidates).pack(anchor="w",pady=2)
        ttk.Separator(sidebar).pack(fill="x",pady=12)
        ttk.Button(sidebar,text="Export current results...",command=self.export_current).pack(fill="x",pady=2)
        ttk.Button(sidebar,text="Export all tip results...",command=self.export_all).pack(fill="x",pady=2)
        ttk.Button(sidebar,text="Save graph...",command=self.save_graph).pack(fill="x",pady=2)
        ttk.Label(sidebar,text="Green: accepted by screening\nAmber: candidate / review\n\nAcceptance is evidence in the\nmounted system. Check fixture\nand table motion for attribution.",
                  style="Muted.TLabel",justify="left").pack(anchor="w",pady=(12,0))
        self.summary_var=tk.StringVar(value="No workbook loaded")
        self.freq_var=tk.StringVar(value="")
        ttk.Label(main,textvariable=self.summary_var,style="Group.TLabel").pack(anchor="w")
        self.frequency_label=ttk.Label(main,textvariable=self.freq_var,style="Muted.TLabel",wraplength=980)
        self.frequency_label.pack(fill="x",pady=(3,6))
        main.bind("<Configure>",lambda e:self.frequency_label.configure(wraplength=max(e.width-25,400)))
        self.notebook=ttk.Notebook(main);self.notebook.pack(fill="both",expand=True)
        plotpage=ttk.Frame(self.notebook);controlpage=ttk.Frame(self.notebook)
        self.notebook.add(plotpage,text="Response and resonance");self.notebook.add(controlpage,text="Control sensors")
        self.figure=Figure(figsize=(9,3),dpi=100,layout="constrained")
        self.figure.set_facecolor("#ffffff")
        self.canvas=FigureCanvasTkAgg(self.figure,master=plotpage)
        self.canvas.get_tk_widget().pack(fill="both",expand=True)
        toolrow=ttk.Frame(plotpage);toolrow.pack(fill="x")
        self.toolbar=NavigationToolbar2Tk(self.canvas,toolrow,pack_toolbar=False)
        self.toolbar.pack(side="left",fill="x",expand=True)
        ttk.Button(toolrow,text="Detach graph",command=self.detach_graph).pack(side="right",padx=5)
        self.navigation=PlotNavigation(self.canvas,self.toolbar)
        self.add_navigation_controls(plotpage,self.navigation,with_peak=True)
        self.control_figure=Figure(figsize=(9,3),dpi=100,layout="constrained")
        self.control_canvas=FigureCanvasTkAgg(self.control_figure,master=controlpage)
        self.control_canvas.get_tk_widget().pack(fill="both",expand=True)
        control_toolrow=ttk.Frame(controlpage);control_toolrow.pack(fill="x")
        self.control_toolbar=NavigationToolbar2Tk(self.control_canvas,control_toolrow,pack_toolbar=False)
        self.control_toolbar.pack(side="left",fill="x",expand=True)
        ttk.Button(control_toolrow,text="Detach graph",command=self.detach_graph).pack(side="right",padx=5)
        self.control_navigation=PlotNavigation(self.control_canvas,self.control_toolbar)
        self.add_navigation_controls(controlpage,self.control_navigation)
        ttk.Label(controlpage,text="C1/C2 differences indicate nonuniform motion; they do not measure every rotation. Phase uses C1 as reference.",
                  style="Muted.TLabel",wraplength=900).pack(fill="x",padx=8,pady=5)
        tableframe=ttk.Frame(main);tableframe.pack(fill="x",pady=(9,0))
        cols=("status","freq","gain","prom","phase","width","reason")
        self.table=ttk.Treeview(tableframe,columns=cols,show="headings",height=5,selectmode="browse")
        widths=(150,85,65,85,95,85,310)
        labels=("Decision","Frequency Hz","Gain x","Prom. dB","Phase delta deg","Width Hz","Reason / notes")
        for c,width,label in zip(cols,widths,labels):
            self.table.heading(c,text=label)
            self.table.column(c,width=width,minwidth=55,anchor="w" if c in ("status","reason") else "e",stretch=c=="reason")
        self.table.tag_configure("accepted",foreground=COLORS["accepted"])
        self.table.tag_configure("review",foreground="#975a0e")
        scroll=ttk.Scrollbar(tableframe,orient="vertical",command=self.table.yview)
        hscroll=ttk.Scrollbar(tableframe,orient="horizontal",command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set,xscrollcommand=hscroll.set)
        self.table.grid(row=0,column=0,sticky="nsew");scroll.grid(row=0,column=1,sticky="ns")
        hscroll.grid(row=1,column=0,sticky="ew");tableframe.columnconfigure(0,weight=1)
        self.table.bind("<<TreeviewSelect>>",self.on_select)
        self.table.bind("<Double-1>",lambda e:self.zoom_peak())
        from tkinter.scrolledtext import ScrolledText
        self.detail=ScrolledText(main,height=4,wrap="word",font=(font_family,9),bg="#f6f8fb",fg="#33465f",relief="flat",padx=8,pady=5)
        self.detail.pack(fill="x",pady=(5,0));self.detail.configure(state="disabled")
        self.status_var=tk.StringVar(value="Ready")
        ttk.Label(root,textvariable=self.status_var,style="Muted.TLabel",padding=(18,2,18,7)).pack(fill="x")
        self.canvas.mpl_connect("button_press_event",self.on_plot_click)
        root.bind("<Control-o>",lambda e:self.open_workbook())
        root.bind("<Control-q>",lambda e:root.destroy())
        if initial_path:
            root.after(100,lambda:self.load_path(initial_path))

    def add_navigation_controls(self,parent,navigation,with_peak=False):
        row=self.ttk.Frame(parent,padding=(4,2,4,0));row.pack(fill="x")
        buttons=[("Inspect",navigation.inspect),("Zoom box",navigation.toolbar.zoom),
                 ("Pan",navigation.toolbar.pan)]
        if with_peak:buttons.append(("Zoom to peak",self.zoom_peak))
        buttons.append(("Full range",navigation.reset))
        for text,command in buttons:
            self.ttk.Button(row,text=text,command=command).pack(side="left",padx=(0,5))
        y_only=self.tk.BooleanVar(value=navigation.y_only)
        self.ttk.Checkbutton(row,text="Wheel: Y only",variable=y_only,
                             command=lambda:setattr(navigation,"y_only",y_only.get())).pack(side="left")
        # Retain the Tk variable for the lifetime of the controls.
        row.y_only=y_only
        self.ttk.Label(parent,text="Wheel: zoom at cursor  |  Shift: frequency only  |  Ctrl: vertical only\n"
                       "Middle-drag: pan  |  Double-click in Inspect: reset  |  Toolbar arrows: back / forward",
                       style="Muted.TLabel",padding=(6,3,2,3)).pack(anchor="w")

    def detach_graph(self):
        """Open an independent, resizable view of the current graph."""
        if not self.result:return
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        win=self.tk.Toplevel(self.root)
        control=self.notebook.index("current")==1
        title="Control sensors" if control else f"Beam {self.result.beam}"
        win.title(f"{title} | {self.result.sweep.sheet} | {self.result.sweep.run}")
        win.geometry("1100x750");win.minsize(640,480)
        footer=self.ttk.Frame(win,padding=5);footer.pack(side="bottom",fill="x")
        self.ttk.Label(footer,text="Snapshot of the current graph. Close and reopen for another beam or run.",
                       style="Muted.TLabel").pack(anchor="w")
        figure=Figure(figsize=(10,7),dpi=100,layout="constrained")
        canvas=FigureCanvasTkAgg(figure,master=win)
        canvas.get_tk_widget().pack(fill="both",expand=True)
        toolbar=NavigationToolbar2Tk(canvas,footer,pack_toolbar=False)
        toolbar.pack(side="left",fill="x",expand=True)
        navigation=PlotNavigation(canvas,toolbar);navigation.y_only=True
        if control:
            axes=draw_control_sensors(figure,self.result,self.log_var.get())
            view=self.control_navigation.capture_view()
        else:
            axes=draw_analysis(figure,self.result,self.selected,self.log_var.get(),
                               self.near_var.get(),self.candidate_var.get())
            view=self.navigation.capture_view()
        navigation.set_axes(axes,view)
        y_only=self.tk.BooleanVar(value=True)
        self.ttk.Checkbutton(footer,text="Wheel: Y only",variable=y_only,
                             command=lambda:setattr(navigation,"y_only",y_only.get())).pack(side="left",padx=5)
        self.ttk.Button(footer,text="Full range",command=navigation.reset).pack(side="left")
        # Matplotlib keeps weak references to event handlers; retain the controller.
        win.graph=(figure,canvas,toolbar,navigation,y_only)
        def close():
            canvas.get_tk_widget().destroy()
            win.destroy()
        win.protocol("WM_DELETE_WINDOW",close)
        canvas.draw_idle()

    def error(self, title, error):
        from tkinter import messagebox
        messagebox.showerror(title,str(error),parent=self.root)

    def open_workbook(self):
        from tkinter import filedialog
        path=filedialog.askopenfilename(title="Open sine-sweep export",filetypes=[("Excel workbooks","*.xlsx *.xlsm")],parent=self.root)
        if path:self.load_path(path)

    def load_path(self,path):
        self.status_var.set("Reading workbook...")
        self.root.configure(cursor="watch");self.root.update_idletasks()
        try:
            sweeps=read_workbook(path)
            self.path,self.sweeps,self.cache=Path(path),sweeps,{}
            self.file_var.set(f"{self.path.name}    |    {len(set(s.sheet for s in sweeps))} configurations, {len(sweeps)} runs")
            self.sheet_box["values"]=list(dict.fromkeys(s.sheet for s in sweeps))
            self.sheet_var.set(sweeps[0].sheet)
            self.on_sheet()
        except Exception as error:
            self.error("Could not read workbook",error)
            self.status_var.set("Workbook could not be loaded.")
        finally:self.root.configure(cursor="")

    def on_sheet(self,event=None):
        self.sheet_sweeps=[s for s in self.sweeps if s.sheet==self.sheet_var.get()]
        labels=[s.run for s in self.sheet_sweeps]
        # Preserve display identity even for duplicate metadata strings.
        if len(labels)!=len(set(labels)):
            labels=[f"{x} (block {i+1})" for i,x in enumerate(labels)]
        self.run_box["values"]=labels
        best=max(range(len(self.sheet_sweeps)),key=lambda i:np.sum(self.sheet_sweeps[i].channels["C3"].amplitude>0))
        self.run_box.current(best)
        self.on_run()

    def current_sweep(self):
        return self.sheet_sweeps[self.run_box.current()]

    def on_run(self,event=None):
        if not self.sweeps:return
        sweep=self.current_sweep()
        for b,button in self.beam_buttons.items():
            button.configure(state="normal" if b in sweep.beams else "disabled")
        if self.beam_var.get() not in sweep.beams:self.beam_var.set(sweep.beams[0])
        self.on_beam()

    def on_beam(self):
        if not self.sweeps:return
        names=self.current_sweep().channels
        beam=self.beam_var.get()
        sensors=sorted(n.rsplit("_",1)[1] for n in names if n.startswith(f"R_{beam}_"))
        self.sensor_box["values"]=sensors
        if self.sensor_var.get() not in sensors:self.sensor_var.set("2" if "2" in sensors else sensors[0])
        self.on_sensor()

    def on_sensor(self,event=None):
        sweep=self.current_sweep();response=f"R_{self.beam_var.get()}_{self.sensor_var.get()}"
        refs=[x for x in ["C3","C1","C2",f"R_{self.beam_var.get()}_1"] if x in sweep.channels and x!=response]
        self.ref_box["values"]=refs
        if self.ref_var.get() not in refs:self.ref_var.set("C3")
        self.refresh()

    def apply_settings(self):
        try:
            changes={k:float(v.get().replace(",",".")) for k,v in self.setting_vars.items()}
            new=Settings(**{**asdict(self.settings),**changes,"smoothing_points":int(self.smooth_var.get())})
            new.validate();self.settings=new
            self.cache.clear();self.refresh()
        except (ValueError,TypeError) as error:self.error("Check thresholds",error)

    def advanced(self):
        tk,ttk=self.tk,self.ttk
        win=tk.Toplevel(self.root);win.title("Advanced screening settings");win.transient(self.root)
        box=ttk.Frame(win,padding=20);box.pack(fill="both",expand=True)
        fields=[("Maximum phase change [deg]","max_phase_change_deg"),
                ("Minimum phase consistency [0-1]","min_phase_consistency"),
                ("Response peak prominence [dB]","response_prominence_db"),
                ("Maximum reference dip [dB]","max_reference_dip_db"),
                ("Reference floor [g]","reference_floor_g")]
        variables={}
        for i,(label,key) in enumerate(fields):
            ttk.Label(box,text=label).grid(row=i,column=0,sticky="w",pady=6)
            variables[key]=tk.StringVar(value=f"{getattr(self.settings,key):g}")
            ttk.Entry(box,textvariable=variables[key],width=12).grid(row=i,column=1,padx=12)
        def save():
            try:
                new=Settings(**{**asdict(self.settings),**{k:float(v.get().replace(",",".")) for k,v in variables.items()}})
                new.validate();self.settings=new;self.cache.clear();win.destroy();self.refresh()
            except ValueError as e:self.error("Check settings",e)
        ttk.Button(box,text="Apply",command=save).grid(row=len(fields),column=1,pady=(12,0))
        ttk.Label(box,text="These settings flag uncertain peaks for review.\nThey do not certify a beam mode.",style="Muted.TLabel").grid(row=len(fields)+1,column=0,columnspan=2,pady=12)

    def refresh(self,event=None):
        if not self.sweeps:return
        try:
            s=self.current_sweep()
            key=(id(s),self.beam_var.get(),self.ref_var.get(),self.sensor_var.get(),tuple(asdict(self.settings).values()))
            if key not in self.cache:
                self.cache[key]=analyze(s,self.beam_var.get(),self.ref_var.get(),self.sensor_var.get(),self.settings)
            self.result=self.cache[key];self.selected=None
            accepted=self.result.accepted
            strongest=max(accepted,key=lambda p:p.amplification) if accepted else None
            text=f"{len(accepted)} accepted by screening    |    {len(self.result.peaks)-len(accepted)} candidates need review"
            if strongest:text+=f"    |    Strongest accepted: {strongest.frequency_hz:.2f} Hz"
            self.summary_var.set(text)
            self.freq_var.set("Accepted frequencies: "+(", ".join(f"{p.frequency_hz:.2f}" for p in accepted)+" Hz" if accepted else "none with current settings"))
            self.populate_table();self.redraw(reset_view=True);self.draw_controls()
            mapping=f"Sensor {self.sensor_var.get()}: "+{"1":"near mount","2":"tip","3":"position/axis unspecified"}.get(self.sensor_var.get(),"unspecified")
            valid_f=s.frequency[self.result.valid]
            span=f"  |  Valid range {valid_f[0]:.2f}-{valid_f[-1]:.2f} Hz" if len(valid_f) else "  |  No valid rows"
            self.status_var.set(mapping+span+"  |  "+self.result.notes[-1])
            self.set_detail("Select a peak in the table or graph to see the acceptance checks and phase window. Green means accepted by the current screening rules. Sensor _1 is near the mount; _2 is at the tip. The phase columns are assumed to share the controller's reference.")
        except Exception as error:self.error("Analysis error",error)

    @staticmethod
    def fmt(value,digits=1):
        return "-" if value is None else f"{value:.{digits}f}"

    def populate_table(self):
        self.table.delete(*self.table.get_children());self.rows={}
        if not self.result:return
        for i,p in enumerate(self.result.peaks):
            if p.status==REVIEW and not self.candidate_var.get():continue
            key=str(i);self.rows[key]=p
            reason="; ".join(p.reasons) if p.reasons else "Checks passed"
            if p.notes:reason+="; "+p.notes[0]
            self.table.insert("","end",iid=key,values=(p.status,self.fmt(p.frequency_hz,2),self.fmt(p.amplification,2),
                              self.fmt(p.prominence_db),self.fmt(p.phase_change_deg),self.fmt(p.width_hz,2),reason),
                              tags=("accepted" if p.status==ACCEPTED else "review",))

    def toggle_candidates(self):
        self.populate_table();self.redraw()

    def redraw(self,reset_view=False):
        if not self.result:return
        previous=None if reset_view else self.navigation.capture_view()
        self.axes=draw_analysis(self.figure,self.result,None,self.log_var.get(),self.near_var.get(),
                                self.candidate_var.get())
        self.navigation.set_axes(self.axes,previous)
        self.selection_artists=[]
        self.highlight_selection()

    def highlight_selection(self):
        view=self.navigation.capture_view()
        for artist in self.selection_artists:artist.remove()
        self.selection_artists=draw_peak_selection(self.axes,self.result,self.selected)
        self.navigation.restore_view(view)
        self.canvas.draw_idle()

    def change_frequency_scale(self):
        self.redraw()
        self.draw_controls(reset_view=False)

    def draw_controls(self,reset_view=True):
        previous=None if reset_view else self.control_navigation.capture_view()
        if not self.result:return
        axes=draw_control_sensors(self.control_figure,self.result,self.log_var.get())
        self.control_navigation.set_axes(axes,previous)
        self.control_canvas.draw_idle()

    def set_detail(self,text):
        self.detail.configure(state="normal");self.detail.delete("1.0","end")
        self.detail.insert("1.0",text);self.detail.configure(state="disabled")

    def on_select(self,event=None):
        selection=self.table.selection()
        if not selection or selection[0] not in self.rows:return
        p=self.rows[selection[0]];self.selected=p
        txt=f"{p.frequency_hz:.2f} Hz - {p.status}. Gain {p.amplification:.2f}x; prominence {p.prominence_db:.1f} dB. "
        txt+=("All screening checks passed. " if not p.reasons else "Review: "+"; ".join(p.reasons)+". ")
        txt+=f"Phase change {self.fmt(p.phase_change_deg)} deg; consistency {self.fmt(p.phase_consistency,2)}. "
        if p.phase_low_hz is not None:txt+=f"Phase window {p.phase_low_hz:.2f}-{p.phase_high_hz:.2f} Hz. "
        txt+=f"Reference dip {self.fmt(p.reference_dip_db)} dB; response/near-mount {self.fmt(p.tip_to_near_ratio,2)}x. "
        if p.is_global_maximum:txt+="Global measured amplification maximum. "
        if p.raw_response_peak_hz is not None:txt+=f"Response acceleration peak at {p.raw_response_peak_hz:.2f} Hz. "
        txt+=" ".join(p.notes)
        self.set_detail(txt)
        # Update only the decorations: retain x/y zoom, pan and toolbar history.
        self.highlight_selection()

    def on_plot_click(self,event):
        if not self.result or event.inaxes not in self.axes or event.xdata is None:return
        if self.toolbar.mode or event.button!=1 or event.dblclick:return
        if event.xdata<=0:return
        visible=list(self.rows.values())
        if not visible:return
        p=min(visible,key=lambda p:abs(math.log(p.frequency_hz/event.xdata)))
        if abs(math.log(p.frequency_hz/event.xdata))>.08:return
        key=next(key for key,value in self.rows.items() if value is p)
        self.table.selection_set(key);self.table.see(key)

    def zoom_peak(self):
        if not self.selected or not self.result:return
        p=self.selected
        span=max((p.width_hz or 0)*4,p.frequency_hz*.06,5.0)
        f=self.result.sweep.frequency[self.result.valid]
        if len(f):self.navigation.zoom_frequency(max(f[0],p.frequency_hz-span),min(f[-1],p.frequency_hz+span))

    def full_range(self):
        navigation=self.navigation if self.notebook.index("current")==0 else self.control_navigation
        navigation.reset()

    def export_current(self):
        from tkinter import filedialog
        if not self.result:return
        path=filedialog.asksaveasfilename(defaultextension=".csv",initialfile=f"resonances_{self.result.sweep.sheet}_beam_{self.result.beam}.csv",
                                        filetypes=[("CSV","*.csv")],parent=self.root)
        if path:
            try:export_results(path,[self.result]);self.status_var.set(f"Saved {Path(path).name}")
            except Exception as error:self.error("Export failed",error)

    def export_all(self):
        from tkinter import filedialog
        if not self.sweeps:return
        path=filedialog.asksaveasfilename(defaultextension=".csv",initialfile="all_resonance_candidates.csv",
                                        filetypes=[("CSV","*.csv")],parent=self.root)
        if not path:return
        try:
            results=[analyze(s,b,"C3","2",self.settings) for s in self.sweeps for b in s.beams if f"R_{b}_2" in s.channels]
            export_results(path,results)
            self.status_var.set(f"Saved all {len(results)} tip/C3 analyses to {Path(path).name}")
        except Exception as error:self.error("Export failed",error)

    def save_graph(self):
        from tkinter import filedialog
        if not self.result:return
        path=filedialog.asksaveasfilename(defaultextension=".png",initialfile=f"resonance_beam_{self.result.beam}.png",
                                        filetypes=[("PNG","*.png"),("PDF","*.pdf")],parent=self.root)
        if path:
            try:
                figure=self.figure if self.notebook.index("current")==0 else self.control_figure
                figure.savefig(path,dpi=180,bbox_inches="tight")
                self.status_var.set(f"Saved {Path(path).name}")
            except Exception as error:self.error("Could not save graph",error)

    def show_method(self):
        from tkinter.scrolledtext import ScrolledText
        win=self.tk.Toplevel(self.root);win.title("Method and interpretation");win.geometry("800x680")
        text=ScrolledText(win,wrap="word",font=("Segoe UI",10),padx=18,pady=16)
        text.pack(fill="both",expand=True);text.insert("1.0",METHOD_TEXT);text.configure(state="disabled")


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workbook",nargs="?",type=Path)
    parser.add_argument("--batch",type=Path,metavar="CSV",help="analyse every run and beam tip using C3; no desktop required")
    args=parser.parse_args(argv)
    if args.batch:
        if not args.workbook:parser.error("--batch requires a workbook path")
        sweeps=read_workbook(args.workbook)
        results=[analyze(s,b) for s in sweeps for b in s.beams if f"R_{b}_2" in s.channels]
        export_results(args.batch,results)
        for r in results:
            print(f"{r.sweep.sheet} / {r.sweep.run} / Beam {r.beam}: "
                  +(", ".join(f"{p.frequency_hz:.2f} Hz" for p in r.accepted) or "none accepted"))
        print(f"Saved {args.batch}")
        return 0
    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is required for the window. On Ubuntu install python3-tk; "
              "on Windows use Python with Tcl/Tk support.",file=sys.stderr)
        return 1
    try:
        root=tk.Tk()
    except tk.TclError as error:
        print(f"Could not open a desktop window: {error}\nRun on a computer with a desktop, "
              "or use --batch results.csv for headless analysis.",file=sys.stderr)
        return 1
    path=args.workbook
    if path is None:
        sample=Path(__file__).resolve().parent / "Resonanzsuche_preliminary.xlsx"
        if sample.exists():path=sample
    ResonanceApp(root,path)
    root.mainloop()
    return 0


if __name__=="__main__":
    raise SystemExit(main())
