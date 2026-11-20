# code belongs to: Vincent Jovan Wijaya
"""Browser adapter. Numerical processing and export formatting stay in desktop modules."""
import base64
import contextlib
import io
import json
import math
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path
import numpy as np
from curve_io import read_data, export_curves, screen, override_axis, default_log_y
from resonance_engine import Settings, export_results

sessions = {}
current = None
analysis = None
archive = None
archive_buffer = None
archive_names = set()

def clean(value):
    if isinstance(value, np.ndarray): return clean(value.tolist())
    if isinstance(value, np.generic): return clean(value.item())
    if isinstance(value, float) and not math.isfinite(value): return None
    if isinstance(value, dict): return {k: clean(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)): return [clean(v) for v in value]
    return value

def metadata():
    return dict(name=current.path.name, kind=current.kind, axis_verified=current.axis_verified,
                axis_note=current.axis_note, sweep_spacing=current.sweep_spacing,
                measurements=[m.label for m in current.measurements],
                settings=asdict(Settings()))

def measurement(index):
    m = current.measurements[index]
    eligible = {c.name for c in current.resonance_channels(m)}
    return dict(label=m.label, notes=m.notes, curves=[dict(name=c.name, x=c.x, y=c.y,
        quantity=c.quantity, x_unit=c.x_unit, y_unit=c.y_unit, role=c.role,
        eligible=c.name in eligible, log_y=default_log_y([c])) for c in m.curves])

def encoded(data): return base64.b64encode(data).decode('ascii')

def safe_path(name):
    parts = name.replace('\\','/').split('/')
    return '/'.join(p.replace(':','_') for p in parts if p not in ('','.', '..')) or 'recording'

def add_file(name, data):
    name = 'Curve_Analyzer_Export/' + safe_path(name)
    p = Path(name); candidate=name; n=2
    while candidate.casefold() in archive_names:
        candidate=str(p.with_name(f'{p.stem}_{n}{p.suffix}')); n+=1
    archive_names.add(candidate.casefold())
    archive.writestr(candidate, data)

def dispatch(req):
    global current, analysis, archive, archive_buffer, archive_names
    op=req['op']
    if op=='close':
        removed=sessions.pop(req['record_id'], None)
        if removed and current is removed[0]: current=None; analysis=None
        return True
    if op=='images_zip':
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
            for item in req['images']:
                z.writestr('Curve_Analyzer_Plots/'+safe_path(item['name']),base64.b64decode(item['data']))
        return dict(name='Curve_Analyzer_Plots.zip',data=encoded(buffer.getvalue()))
    if op=='load':
        with contextlib.redirect_stdout(io.StringIO()): data=read_data(req['path'])
        data.path=Path(Path(req.get("name", data.path.name)).name)
        current=data; analysis=None
        return metadata()
    if op=='measurement':
        analysis=None
        return measurement(req['index'])
    if op=='override':
        override_axis(current, **req['values']); analysis=None
        return metadata()
    if op=='analyze':
        analysis=None
        result=screen(current, current.measurements[req['index']], req['response'], req['reference'], Settings(**req['settings']))
        analysis=result
        return dict(x=result.sweep.frequency, response=result.sweep.channels[result.response].amplitude,
                    reference=result.sweep.channels[result.reference].amplitude,
                    amplification=result.transmissibility, phase=result.relative_phase_deg,
                    peaks=[asdict(p) for p in result.peaks], notes=result.notes,
                    response_name=result.response, reference_name=result.reference)
    if op=='export':
        ext=req['format']
        if ext not in ('csv','txt'): raise ValueError('Choose CSV or TXT.')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/('resonance_results.'+ext if req.get('analysis') else 'curves.'+ext)
            if req.get('analysis'):
                if analysis is None: raise ValueError('Run analysis first.')
                export_results(p,[analysis])
            else: export_curves(p,current.measurements[req['index']],req['names'])
            return dict(name=p.name, data=encoded(p.read_bytes()))
    if op=='batch_start':
        if archive: archive.close()
        archive_buffer=io.BytesIO(); archive=zipfile.ZipFile(archive_buffer,'w',zipfile.ZIP_DEFLATED)
        archive_names=set(); return True
    if op=='batch_add':
        ext=req['format']
        if ext not in ('csv','txt'): raise ValueError('Choose CSV or TXT.')
        with contextlib.redirect_stdout(io.StringIO()): data=read_data(req['path'])
        if not data.axis_verified: raise ValueError('Unverified X axis: open this file separately and supply test settings before exporting.')
        relative=Path(safe_path(req['relative']))
        folder=relative.parent/relative.stem
        # Stage every measurement before appending, so a failed input cannot leave partial output.
        staged=[]
        with tempfile.TemporaryDirectory() as d:
            for i,m in enumerate(data.measurements,1):
                name=f'{relative.name}_measurement_{i:03d}.{ext}'
                p=Path(d)/name
                export_curves(p,m,[c.name for c in m.curves])
                staged.append((str(folder/name),p.read_bytes()))
        for name,raw in staged: add_file(name,raw)
        return dict(measurements=len(staged))
    if op=='batch_finish':
        add_file('conversion_report.txt', req['report'].encode('utf-8'))
        archive.close(); archive=None
        result=dict(name='Curve_Analyzer_Export.zip',data=encoded(archive_buffer.getvalue()))
        archive_buffer=None
        return result
    raise ValueError('Unknown operation.')

def handle(raw):
    global current, analysis
    req=json.loads(raw)
    record_id=req.get('record_id')
    scoped=req['op'] in ('load','measurement','override','analyze','export')
    if record_id is not None and scoped:
        current,analysis=sessions.get(record_id,(None,None))
        if current is None and req['op']!='load':
            raise ValueError('Recording was closed. Open it again.')
    try:
        return json.dumps(clean(dispatch(req)),allow_nan=False)
    finally:
        if record_id is not None and scoped and current is not None:
            sessions[record_id]=(current,analysis)

