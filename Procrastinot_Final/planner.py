"""Date/urgency and study block calculations; Gemini supplies educational content."""
from __future__ import annotations
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import re

EXAMS = re.compile(r'\b(midterm|final|exam|quiz|test|assessment)\b', re.I)

def parse_due(value, tz='America/Los_Angeles'):
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=ZoneInfo(tz))
        return date.astimezone(ZoneInfo(tz))
    except (TypeError, ValueError):
        return None

def infer_kind(title, description=''):
    return 'exam' if EXAMS.search(title or '') else 'assignment'

def hours_to(due, now):
    return (due-now).total_seconds()/3600 if due else None

def urgency(due, now, completed=False):
    if completed:
        return 'done'
    if not due:
        return 'unknown'
    h = hours_to(due,now)
    return 'overdue' if h < 0 else 'critical' if h <= 24 else 'soon' if h <= 72 else 'later'

def when_label(due, now):
    if not due:
        return 'Date not confirmed'
    h = hours_to(due,now)
    if h < 0:
        return f'Overdue by {abs(h):.0f}h'
    if h < 1:
        return f'Due in {max(1,round(h*60))} min'
    if h < 48:
        return f'Due in {h:.0f}h'
    return f'Due in {h/24:.0f}d'

def make_study_blocks(now:datetime, due:datetime|None, rhythm='Balanced', max_blocks=9):
    """Time-aware suggestions, not assertions about availability or class schedule.

    No overnight work windows. Uses selectable broad preference, not a manual hours field.
    """
    end = due if due and due > now else now + timedelta(hours=24)
    end = min(end, now + timedelta(days=4))
    if rhythm=='Morning':
        windows=[(7,12),(14,17)]
    elif rhythm=='Evening':
        windows=[(15,18),(19,23)]
    else:
        windows=[(9,12),(15,18),(19,21)]
    blocks=[]
    day=now.date()
    while datetime.combine(day, time.min, tzinfo=now.tzinfo) < end and len(blocks)<max_blocks:
        for start_hr,end_hr in windows:
            beginning=datetime.combine(day, time(start_hr), tzinfo=now.tzinfo)
            cutoff=datetime.combine(day, time(end_hr), tzinfo=now.tzinfo)
            cursor=max(beginning,now)
            # near future 10-minute buffer for first session
            if cursor==now:
                cursor+=timedelta(minutes=10)
            # round to next 5 min
            if cursor.second or cursor.microsecond:
                cursor=cursor.replace(second=0,microsecond=0)+timedelta(minutes=1)
            cursor += timedelta(minutes=(-cursor.minute)%5)
            while cursor+timedelta(minutes=30)<=min(end,cutoff) and len(blocks)<max_blocks:
                finish=min(cursor+timedelta(minutes=50),cutoff,end)
                if (finish-cursor).total_seconds()<1800:
                    break
                blocks.append({'start':cursor.isoformat(),'end':finish.isoformat(),
                               'label':f'{cursor:%a %I:%M %p} – {finish:%I:%M %p}',
                               'minutes':round((finish-cursor).total_seconds()/60)})
                cursor=finish+timedelta(minutes=10)
        day+=timedelta(days=1)
    return blocks
