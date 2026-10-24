"""TRIAGE source-aware obligation ledger and practical, time-aware planning.

Never equate an empty Canvas Assignments list with 'nothing due'. Syllabus
and external-platform obligations live in the same ledger with attribution.
"""
from __future__ import annotations
from datetime import datetime, date, timedelta, time
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo
import hashlib
import re
from typing import Any

WEEKDAY = {'monday':0,'tuesday':1,'wednesday':2,'thursday':3,'friday':4,'saturday':5,'sunday':6}

TZ = ZoneInfo('America/Los_Angeles')
NON_TASK_RE = re.compile(r'^(?:week\s+\w+\s*[,—:-]?\s*day\s+\w+|week\s+\d+\s*[,—:-]?\s*day\s+\d+|module\s+\d+|course overview)\b', re.I)
EXAM_RE = re.compile(r'\b(midterm|final|exam(?:ination)?|test|quiz)\b', re.I)
NOW = lambda: datetime.now(TZ)


def iso_time(value):
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def kind(title, provided=''):
    return 'exam' if provided=='exam' or (provided not in ('assignment','exam') and EXAM_RE.search(title or '')) else 'assignment'


def event_id(course_id, title, day, origin):
    raw=f'{course_id}|{title}|{day}|{origin}'.lower()
    return hashlib.sha1(raw.encode('utf8')).hexdigest()[:18]


def _norm_title(title):
    s=re.sub(r'[^a-z\d ]+', ' ',str(title).lower())
    return ' '.join(x for x in s.split() if x not in {'due','assignment','homework','hw','the','for'})


def sourced_ledger(bundle: dict, manual: list, extracted: list, overrides: dict, now=None):
    now=now or NOW()
    all_events=[]
    courses={str(c['id']):c for c in bundle.get('courses', [])}
    for a in bundle.get('assignments', []):
        course_id=str(a.get('course_id',''))
        dt=iso_time(a.get('due_at'))
        if not dt:
            # Assignments without due dates are not an upcoming due item, but remain
            # visible in an 'undated' source list if useful.
            pass
        if NON_TASK_RE.search(str(a.get('title') or '')) and not a.get('points_possible'):
            continue  # Canvas module/calendar headings are not graded homework
        sub=str(a.get('submission_state') or 'unknown').lower()
        verified_sub=sub in ('submitted','graded','pending_review')
        code=courses.get(course_id,{}).get('course_code') or a.get('course') or 'Course'
        eid=f"canvas-{course_id}-{a.get('id') or event_id(course_id,a.get('title'),a.get('due_at'),'canvas')}"
        all_events.append(dict(id=eid,course_id=course_id,course=code,title=a.get('title') or 'Untitled',
            kind=kind(a.get('title'),a.get('kind')),due_at=dt.isoformat() if dt else '',
            date_only=False,time_known=bool(dt),date_text='',source=a.get('source') or 'Canvas',
            source_url=a.get('html_url') or '',source_name='Canvas assignment or event',
            evidence=(a.get('description') or '')[:380],description=(a.get('description') or '')[:5000],
            certainty='verified',submission='submitted' if verified_sub else 'unconfirmed',
            submitted_by_canvas=verified_sub,platform='Canvas',needs_review=not bool(dt)))
    for e in (extracted or [])+(manual or []):
        if not isinstance(e,dict) or not e.get('title') or NON_TASK_RE.search(str(e.get('title') or '')):
            continue
        course_id=str(e.get('course_id') or '')
        if course_id not in courses:
            continue
        date_str=str(e.get('due_date') or '')
        t_str=str(e.get('due_time') or '')
        dt=iso_time(e.get('due_at'))
        date_only=False
        # Deterministic recurrence preview: only for explicitly stated weekly
        # obligations, e.g. written homework due every Monday. We know the next
        # calendar weekday, but not the instructor's clock time or exceptions.
        recurrent_text=(str(e.get('date_text') or '')+' '+str(e.get('evidence') or '')).lower()
        is_weekly=bool(re.search(r'\b(every|each|weekly)\b',recurrent_text))
        if is_weekly and not date_str and not dt:
            wd=next((index for day,index in WEEKDAY.items() if re.search(r'\b'+day+r'\b',recurrent_text)),None)
            if wd is not None:
                today=now.date();advance=(wd-today.weekday())%7
                # For today's undated weekly tasks, show today as an occurrence,
                # but keep a review warning. No submission time is invented.
                date_str=(today+timedelta(days=advance)).isoformat()
        if not dt and re.fullmatch(r'\d{4}-\d{2}-\d{2}',date_str):
            try:
                d=date.fromisoformat(date_str)
                if re.fullmatch(r'\d{2}:\d{2}',t_str):
                    dt=datetime.combine(d,time.fromisoformat(t_str),TZ)
                else:
                    dt=datetime.combine(d,time(23,59),TZ)
                    date_only=True
            except ValueError:
                dt=None
        source_name=str(e.get('source_name') or e.get('source') or 'Student')
        origin='manual' if e in (manual or []) else 'syllabus'
        eid=str(e.get('id') or event_id(course_id,e['title'],date_str or str(dt),origin))
        raw_needs_review=e.get('needs_review',False) or is_weekly
        all_events.append(dict(id=eid,course_id=course_id,
            course=courses[course_id].get('course_code') or courses[course_id]['name'],
            title=str(e['title'])[:190],kind=kind(e.get('title'),e.get('kind')),
            due_at=dt.isoformat() if dt else '', date_only=date_only,
            time_known=bool(dt and not date_only),date_text=e.get('date_text') or '',
            source='Added by you' if origin=='manual' else 'Syllabus / course material',
            source_name=source_name,source_url=e.get('source_url') or '',
            evidence=str(e.get('evidence') or '')[:350],description=str(e.get('description') or '')[:3000],
            certainty='manual' if origin=='manual' else 'document',
            submission='unconfirmed',submitted_by_canvas=False,
            platform=e.get('platform') or ('Manual' if origin=='manual' else 'Course materials'),
            needs_review=bool(raw_needs_review or not dt)))
    # Collapse the same task from several places while retaining all proof.
    unique=[]
    for ev in sorted(all_events,key=lambda x:(x['certainty']!='verified',x['title'])):
        same=None
        for old in unique:
            if old['course_id']!=ev['course_id'] or old['kind']!=ev['kind']:
                continue
            od=iso_time(old.get('due_at')); ed=iso_time(ev.get('due_at'))
            if not od or not ed or od.date()!=ed.date():
                continue
            similarity=SequenceMatcher(None,_norm_title(old['title']),_norm_title(ev['title'])).ratio()
            # Do not conflate different homework just because they share a day.
            if similarity>=0.80 and len(_norm_title(ev['title']))>=3:
                same=old; break
        if same:
            sources=same.setdefault('also_in',[])
            sources.append({'source':ev['source'],'source_name':ev['source_name'],
                            'evidence':ev['evidence']})
            if ev['submitted_by_canvas']:
                same['submitted_by_canvas']=True; same['submission']='submitted'
            # A syllabus can explain a Canvas assignment, but don't replace a
            # Canvas timestamp with a vague date.
            continue
        unique.append(ev)
    # Highlight conflicts: same named task with significantly different dates.
    for i,a in enumerate(unique):
        for b in unique[i+1:]:
            if a['course_id']!=b['course_id'] or not a.get('due_at') or not b.get('due_at'):
                continue
            if SequenceMatcher(None,_norm_title(a['title']),_norm_title(b['title'])).ratio()<0.86:
                continue
            if iso_time(a['due_at']).date()!=iso_time(b['due_at']).date():
                a['needs_review']=True; b['needs_review']=True
                a['conflict']='Sources disagree on this deadline'; b['conflict']='Sources disagree on this deadline'
    for ev in unique:
        state=overrides.get(ev['id'])
        if state in ('done','open') and not ev['submitted_by_canvas']:
            ev['submission']='done' if state=='done' else 'unconfirmed'
        due=iso_time(ev.get('due_at'))
        completed=ev['submission'] in ('submitted','done')
        ev['completed']=completed
        if not due:
            ev['urgency']='review'; ev['hours_left']=None; ev['due_label']=ev.get('date_text') or 'Date needs checking'
            continue
        h=(due-now).total_seconds()/3600
        ev['hours_left']=round(h,1)
        if completed:
            ev['urgency']='done'
        elif h<0:
            ev['urgency']='overdue'
        elif h<=48:
            ev['urgency']='critical'
        elif h<=120:
            ev['urgency']='soon'
        else:
            ev['urgency']='later'
        if ev['date_only']:
            ev['due_label']=due.strftime('%a, %b %-d')+' · time not specified'
        else:
            ev['due_label']=due.strftime('%a, %b %-d · %-I:%M %p')
    unique.sort(key=lambda ev:(ev['completed'], ev['due_at']=='' ,ev['due_at'] or '9999',ev['title'].lower()))
    return unique


def metrics(ledger, now=None):
    now=now or NOW()
    unresolved=[e for e in ledger if not e['completed']]
    return {'needs_action':sum(e['urgency'] in ('overdue','critical') for e in unresolved),
        'this_week':sum(e['urgency'] in ('critical','soon') for e in unresolved),
        'exams':sum(e['kind']=='exam' and e['urgency'] in ('critical','soon','later') for e in unresolved),
        'needs_review':sum(e['needs_review'] for e in unresolved),
        'documents_missing':False,
        'next_event':next((e for e in unresolved if e['urgency'] in ('overdue','critical','soon','later')),None)}


def schedule(now: datetime, due: datetime | None, preference='balanced', maximum=16):
    """Respect future daylight windows; suggestions, not knowledge of actual availability."""
    if not due or due<=now:
        horizon=now+timedelta(days=2)
    else:
        horizon=min(due, now+timedelta(days=7))
    windows={'morning':[(7,11),(13,15)],'evening':[(16,19),(19,23)],
             'balanced':[(9,12),(14,17),(19,21)]}.get(preference,[(9,12),(14,17),(19,21)])
    result=[]; day=now.date()
    while datetime.combine(day,time(0),now.tzinfo)<horizon and len(result)<maximum:
        for h0,h1 in windows:
            start=max(datetime.combine(day,time(h0),now.tzinfo),now+timedelta(minutes=10))
            limit=min(datetime.combine(day,time(h1),now.tzinfo),horizon)
            while start+timedelta(minutes=35)<=limit and len(result)<maximum:
                end=min(start+timedelta(minutes=55),limit)
                result.append({'start':start.isoformat(),'end':end.isoformat(),
                  'label':start.strftime('%a · %-I:%M %p')+' – '+end.strftime('%-I:%M %p'),
                  'minutes':round((end-start).total_seconds()/60)})
                start=end+timedelta(minutes=15)
        day+=timedelta(days=1)
    return result
