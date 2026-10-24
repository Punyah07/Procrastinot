"""Local-only persistence. Canvas credentials and session cookies stay in Chromium profile.

This is designed for a single student's local hackathon demo, not a hosted multi-user app.
"""
from __future__ import annotations

import json
import errno
import os
from pathlib import Path
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / '.triage_local'
LOCAL.mkdir(exist_ok=True)

def read_json(name: str, default=None):
    path = LOCAL / name
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {} if default is None else default

def write_json(name: str, data):
    LOCAL.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.triage-', suffix='.json', dir=LOCAL)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LOCAL / name)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def now_local(tz='America/Los_Angeles'):
    return datetime.now(ZoneInfo(tz))

def get_progress():
    """Recover from orphaned 'syncing' statuses after a process crashes.

    Users should never be permanently locked out of Connect/Sync by a
    Streamlit restart or an unexpectedly terminated Playwright worker.
    """
    state = read_json('worker_status.json', {'state': 'idle', 'message': 'Ready'})
    if state.get('state') not in ('login', 'syncing'):
        return state
    pid = state.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        # A worker launch is usually assigned its PID immediately, but leave
        # a short grace period for the initial subprocess startup.
        try:
            updated = datetime.fromisoformat(state['updated_at'])
            age = (datetime.now(updated.tzinfo) - updated).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = 1000
        if age < 20:
            return state
        stale = True
    else:
        try:
            os.kill(pid, 0)
            stale = False
        except ProcessLookupError:
            stale = True
        except PermissionError:
            stale = False
    if not stale:
        try:
            updated = datetime.fromisoformat(state['updated_at'])
            age = (datetime.now(updated.tzinfo) - updated).total_seconds()
            # Canvas worker reports a heartbeat while waiting for login;
            # during PDF downloads a 10–35 second gap can be normal.
            if age > 85:
                return {'state':'error', 'message':
                        'Canvas stopped responding. Restart TRIAGE, and check the local worker log if this repeats.',
                        'worker_stopped': True}
        except (KeyError, TypeError, ValueError):
            pass
    if stale:
        return {'state':'error', 'message':
                'Canvas sync stopped unexpectedly. Click Connect/Import again. '
                'For details, check .triage_local/canvas_worker.log.',
                'worker_stopped': True}
    return state

def completed_map():
    return read_json('completed.json', {})

def set_completed(key, value):
    d = completed_map()
    d[str(key)] = bool(value)
    write_json('completed.json', d)

def as_course_names(course_list):
    return {str(c.get('id')): c.get('name', 'Untitled course') for c in course_list}
