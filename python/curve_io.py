# code belongs to: Vincent Jovan Wijaya
"""Read-only adapters from shaker recordings to named curves with explicit units."""
from __future__ import annotations

import csv
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from resonance_engine import Channel, Sweep, Settings, analyze, read_workbook

SUPPORTED = (".rsn", ".rrn", ".rcs", ".xlsx", ".xlsm")
KIND_NAMES = {"sine": "Sine spectra", "random": "Random vibration / PSD", "shock": "Shock time histories"}


@dataclass
class Curve:
    name: str
    x: np.ndarray
    y: np.ndarray
    phase: np.ndarray | None
    x_unit: str
    y_unit: str
    function: str
    role: str = "measured"
    channel_type: str = ""
    coherence: np.ndarray | None = None

    @property
    def quantity(self):
        return f"{self.function} · {self.y_unit or 'dimensionless'} vs {self.x_unit or 'index'}"


@dataclass
class Measurement:
    label: str
    curves: list[Curve]
    notes: list[str] = field(default_factory=list)
    number: int | None = None
    timestamp: datetime | None = None

    def curve(self, name):
        return next(c for c in self.curves if c.name == name)


@dataclass
class CurveData:
    path: Path
    kind: str
    measurements: list[Measurement]
    axis_verified: bool = True
    axis_note: str = "X axis read from the file."
    sweep_spacing: str | None = None
    applied_sweep_spacing: str | None = None

    def resonance_channels(self, measurement):
        if self.kind != "sine":
            return []
        return [c for c in measurement.curves if c.x_unit == "Hz" and c.y_unit == "g"
                and c.role == "measured"]


def _curves(channels):
    curves, counts = [], {}
    for ch in channels:
        n = int(ch.get("size", len(ch["y"])))
        if n == 0:
            continue  # The decoder documents empty PF/PDF placeholder channels.
        x, y = np.asarray(ch["x"][:n], float), np.asarray(ch["y"][:n], float)
        phase = None if ch.get("ph") is None else np.asarray(ch["ph"][:n], float)
        if len(x) != n or len(y) != n or (phase is not None and len(phase) != n):
            raise ValueError(f"Incomplete array for channel {ch['name']}.")
        if not np.isfinite(x).all():
            raise ValueError(f"Non-finite X axis in channel {ch['name']}.")
        name = ch["name"].strip()
        counts[name] = counts.get(name, 0) + 1
        if counts[name] > 1:
            name += f" [{counts[name]}]"
        role = "measured" if ch.get("type", "M").strip() in ("C", "M") else "auxiliary"
        curves.append(Curve(name, x, y, phase, ch["units_x"], ch["units_y"], ch["ftype"], role,
                            ch.get("type", "").strip()))
    return curves


def read_data(path) -> CurveData:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError("Unsupported file type. Open .rsn (sine), .rrn (random), .rcs (shock), or the previous Excel export.")
    if ext in (".xlsx", ".xlsm"):
        sweeps = read_workbook(path)
        measurements = []
        for s in sweeps:
            curves = [Curve(c.name, s.frequency, c.amplitude, c.phase, "Hz",
                            "V" if c.name == "Drive" else "g", "Spectrum",
                            "auxiliary" if c.name in ("Drive", "Control") else "measured")
                      for c in s.channels.values()]
            if s.reference_profile is not None:
                curves.append(Curve("Reference profile", s.frequency, s.reference_profile,
                                    None, "Hz", "g", "Spectrum", "reference"))
            measurements.append(Measurement(f"{s.sheet} / {s.run}", curves, list(s.notes)))
        return CurveData(path, "sine", measurements)
    if path.stat().st_size < 64:
        raise ValueError("The file is too short to contain a VcpNT recording.")
    if ext == ".rsn":
        from decoders import vcpnt_sine as decoder
        kind, kwargs = "sine", {}
    elif ext == ".rrn":
        from decoders import vcpnt_random as decoder
        kind, kwargs = "random", {}
    else:
        from decoders import vcpnt_shock as decoder
        kind, kwargs = "shock", {"all_sections": True, "all_channels": True}
    try:
        raw = decoder.decode(str(path), return_data=True, **kwargs)
    except (struct.error, IndexError, OverflowError, ZeroDivisionError) as error:
        raise ValueError("Could not decode this recording: truncated data or a binary layout unsupported by the supplied decoder.") from error
    header = raw["header"] or []
    sweep_spacing = None
    if kind == "sine":
        hdr = decoder.SineHeader(header)
        if hdr.sweep_rate_source is not None:
            sweep_spacing = hdr.sweep_type
        verified = len(hdr.breakpoints) >= 2 and sweep_spacing is not None
    elif kind == "random":
        verified = decoder.RandomHeader(header).resolution_source is not None
    else:
        verified = raw["layout"].get("sample_rate_source") is not None
    note = "X axis reconstructed from the embedded setup." if verified else (
        "X-axis setup is incomplete. The preview may use estimated frequencies or times.")
    measurements = []
    for i, m in enumerate(raw["measurements"], 1):
        curves = _curves(m["channels"])
        if not curves:
            continue
        dt = m.get("dt")
        stamp = f" · {dt:%Y-%m-%d %H:%M:%S}" if dt is not None else ""
        label = f"Measurement {i:03d}{stamp}"
        if kind == "shock":
            label = f"Shock {i:03d}"
        if m.get("direction"):
            label += f" · {m['direction']}"
        design = np.asarray(m.get("design", []), float)
        if len(design):
            reference_unit = "g²/Hz" if kind == "random" else "g"
            for name, subtype, factor in m.get("references", []):
                title = f"{name} {subtype}".replace(" None", "")
                curves.append(Curve(title, np.asarray(m["x"], float), design*factor, None,
                                    "Hz", reference_unit, "PSD" if kind == "random" else "Spectrum", "reference"))
        notes = []
        if kind == "sine" and i == 1:
            notes.append("First stored snapshot; the decoder labels its measured channels STEST.")
        if kind == "shock":
            notes.append("Measured channels in g; Drive in V; Error in %. Frequency-domain tail records are excluded.")
        measurements.append(Measurement(label, curves, notes, i, dt))
    if not measurements:
        raise ValueError("No plottable measurement channels were decoded.")
    return CurveData(path, kind, measurements, verified, note, sweep_spacing=sweep_spacing)


def override_axis(data, *, low=None, high=None, logarithmic=True, step=None):
    """Use supplied setup parameters for every stored snapshot; never rewrite input."""
    if data.kind == "sine":
        if low is None or high is None or not np.isfinite([low, high]).all() or not 0 < low < high:
            raise ValueError("Enter positive frequency limits with minimum < maximum.")
    elif step is None or not np.isfinite(step) or step <= 0:
        raise ValueError("Enter a positive frequency resolution or sample rate.")
    for m in data.measurements:
        for c in m.curves:
            if data.kind == "sine":
                c.x = (np.geomspace if logarithmic else np.linspace)(low, high, len(c.x))
            elif data.kind == "random":
                c.x = np.arange(len(c.x), dtype=float) * step
            else:
                c.x = np.arange(len(c.x), dtype=float) / step
    if data.kind == "sine":
        data.applied_sweep_spacing = "logarithmic" if logarithmic else "linear"
    data.axis_verified = True
    data.axis_note = "X axis supplied manually from your test settings."


def screen(data, measurement, response, reference, settings=None):
    if data.kind != "sine":
        raise ValueError("Sine resonance screening is not applied to PSD or shock data.")
    if not data.axis_verified:
        raise ValueError("Supply the actual X-axis test settings before reporting resonance frequencies.")
    eligible = {c.name: c for c in data.resonance_channels(measurement)}
    if response not in eligible or reference not in eligible:
        raise ValueError("Choose two measured acceleration channels in g for response and reference.")
    tip, base = eligible[response], eligible[reference]
    if len(tip.x) != len(base.x) or not np.array_equal(tip.x, base.x):
        raise ValueError("Response and reference have different frequency grids; they cannot be compared point by point.")
    f = tip.x
    if len(f) < 3 or np.any(f <= 0) or not (np.all(np.diff(f) > 0) or np.all(np.diff(f) < 0)):
        raise ValueError("Screening needs a positive, strictly monotonic frequency grid.")
    order = np.argsort(f)
    channels = {c.name: Channel(c.name, c.y[order], None if c.phase is None else c.phase[order])
                for c in eligible.values() if np.array_equal(c.x, f)}
    sweep = Sweep(data.path.stem, measurement.label, f[order], channels, notes=list(measurement.notes))
    match = re.fullmatch(r"R_([A-Z])_\d+(?: \(STEST\))?", response)
    beam = match.group(1) if match else "selected channels"
    return analyze(sweep, beam=beam, reference=reference, settings=settings or Settings(), response_name=response)


def export_curves(path, measurement, names, *, include_phase=False):
    """CSV sample table or shaker-style TXT blocks, without resampling.

    Ordinary plotting exports magnitudes. Phase remains available to resonance
    screening and can be explicitly included by a caller. Unavailable coherence
    is never replaced with invented zeroes.
    """
    path = Path(path)
    if path.suffix.lower() not in (".csv", ".txt"):
        raise ValueError("Choose a .csv or .txt export filename.")
    selected = [measurement.curve(n) for n in dict.fromkeys(names)]
    if not selected:
        raise ValueError("Select at least one channel to export.")
    if path.suffix.lower() == ".txt":
        return _export_txt(path, measurement, selected, include_phase=include_phase)
    with open(path, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["measurement", "channel", "function", "x_unit", "y_unit", "x", "y"] +
                        (["phase_deg"] if include_phase else []))
        for c in selected:
            for i in range(len(c.x)):
                writer.writerow([measurement.label, c.name, c.function, c.x_unit, c.y_unit,
                                 c.x[i], c.y[i]] +
                                (["" if c.phase is None else c.phase[i]] if include_phase else []))


def _export_txt(path, measurement, selected, *, include_phase):
    def number(value):
        return "" if value is None or not np.isfinite(value) else format(float(value), ".17g").replace(".", ",")

    def heading(name, unit):
        return f"{name} [{unit}]" if unit else name

    stamp = (f"{measurement.number:03d}: " if measurement.number is not None else "")
    stamp += (f"[{measurement.timestamp:%d-%m-%y %H:%M}]" if measurement.timestamp is not None
              else measurement.label)
    with open(path, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\r\n")
        for c in selected:
            reference = next((r for r in measurement.curves if r is not c and r.role == "reference"
                              and r.name.strip().lower() in ("reference", "reference profile")
                              and (r.x_unit, r.y_unit, r.function) == (c.x_unit, c.y_unit, c.function)
                              and np.array_equal(r.x, c.x)), None)
            label = c.name + (f" ({c.channel_type})" if c.channel_type in ("C", "M") else "")
            headers = [heading("X-Data", c.x_unit)]
            if reference is not None:
                headers.append(heading("Reference", c.y_unit))
            channel_column = len(headers)
            headers.append(heading(label, c.y_unit))
            phase = include_phase and c.phase is not None
            if phase:
                headers.append("Phase [Deg]")
            if c.coherence is not None:
                if len(c.coherence) != len(c.x):
                    raise ValueError(f"Incomplete coherence array for {c.name}.")
                headers.append("Coherence")
            writer.writerow(headers)
            metadata = [""] * len(headers)
            metadata[channel_column] = stamp
            writer.writerow(metadata)
            for i, (x, y) in enumerate(zip(c.x, c.y)):
                row = [number(x)]
                if reference is not None:
                    row.append(number(reference.y[i]))
                row.append(number(y))
                if phase:
                    row.append(number(c.phase[i]))
                if c.coherence is not None:
                    row.append(number(c.coherence[i]))
                writer.writerow(row)
            writer.writerow([])


def default_log_y(curves):
    """Start spectral amplitudes/PSDs on log Y; keep signed errors/time linear."""
    return bool(curves) and all(
        c.function in ("Spectrum", "PSD") and c.y_unit in ("g", "V", "g²/Hz", "V²/Hz")
        and not np.any(c.y[np.isfinite(c.y)] < 0) for c in curves)


def draw_curves(figure, measurement, names, *, log_x=False, log_y=False, show_phase=False, title=""):
    """Plot one quantity at a time, so g, volts and PSD are never mixed."""
    figure.clear()
    selected = [measurement.curve(n) for n in names]
    if not selected:
        ax = figure.subplots()
        ax.text(.5, .5, "Select one or more channels", transform=ax.transAxes, ha="center", va="center")
        ax.set_axis_off()
        return [ax]
    if len({(c.x_unit, c.y_unit, c.function) for c in selected}) != 1:
        raise ValueError("Choose channels with the same quantity and units.")
    phase = show_phase and any(c.phase is not None for c in selected)
    axes = list(np.atleast_1d(figure.subplots(2 if phase else 1, 1, sharex=True)))
    for c in selected:
        x, y = c.x.copy(), c.y.copy()
        if log_x:
            x[x <= 0] = np.nan
        if log_y:
            y[y <= 0] = np.nan
        line, = axes[0].plot(x, y, label=c.name, lw=1.1)
        if phase and c.phase is not None:
            axes[1].plot(x, c.phase, label=c.name, color=line.get_color(), lw=1)
    c = selected[0]
    quantity = c.function
    if c.function == "Time":
        quantity = {"g": "Acceleration", "V": "Voltage", "%": "Control error"}.get(c.y_unit, "Amplitude")
    elif c.function == "Spectrum":
        quantity = "Acceleration amplitude" if c.y_unit == "g" else "Amplitude"
    axes[0].set_ylabel(f"{quantity} [{c.y_unit or 'dimensionless'}]")
    if log_y:
        axes[0].set_yscale("log")
    if phase:
        axes[1].set_ylabel("Channel phase [deg]")
    axes[-1].set_xlabel(f"{'Time' if c.x_unit == 's' else 'Frequency' if c.x_unit == 'Hz' else 'X'} [{c.x_unit}]")
    for ax in axes:
        if log_x:
            ax.set_xscale("log")
        ax.grid(True, alpha=.2)
    axes[0].legend(fontsize=8, loc="upper left", ncol=2 if len(selected) > 5 else 1)
    figure.suptitle(title or measurement.label, fontsize=11, fontweight="bold")
    return axes
