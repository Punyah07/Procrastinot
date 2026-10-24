from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from engine import sourced_ledger,metrics,schedule
from intelligence import analyze_syllabus
from canvas_worker import classify_course
import pytest

TZ=ZoneInfo('America/Los_Angeles')
NOW=datetime(2026,10,24,14,0,tzinfo=TZ)

def sample_bundle():
    return {'courses':[{'id':'1','name':'Math 011','course_code':'MATH 011'}],
            'assignments':[{'id':'17','course_id':'1','title':'Canvas Quiz','due_at':'2026-10-26T23:59:00-07:00',
            'submission_state':'submitted','source':'Canvas assignment'}], 'materials':[]}

def test_syllabus_pearson_not_lost():
    gem=[{'id':'sy1','course_id':'1','title':'Pearson Chapter 5 Homework','due_date':'2026-10-26',
          'platform':'Pearson','source_name':'Syllabus.pdf','evidence':'Pearson is due October 26',
          'kind':'assignment'},{'id':'sy2','course_id':'1','title':'Midterm 1','due_date':'2026-10-28',
          'kind':'exam','source_name':'Syllabus.pdf','evidence':'Midterm October 28'}]
    items=sourced_ledger(sample_bundle(),[],gem,{},NOW)
    assert any(i['platform']=='Pearson' and i['urgency']=='soon' for i in items)
    assert any(i['kind']=='exam' for i in items)
    quiz=next(i for i in items if i['title']=='Canvas Quiz')
    assert quiz['completed'] and quiz['submitted_by_canvas']
    assert metrics(items,NOW)['exams']==1

def test_unknown_time_not_invented():
    e=[{'course_id':'1','title':'Written homework','due_date':'2026-10-26','source_name':'Syllabus','platform':'Written'}]
    a=sourced_ledger(sample_bundle(),[],e,{},NOW)
    task=next(x for x in a if x['title']=='Written homework')
    assert task['date_only'] and not task['time_known']
    assert 'time not specified' in task['due_label']

def test_undated_not_treated_as_all_clear():
    e=[{'course_id':'1','title':'Every Monday assignment','date_text':'every Monday','source_name':'Syllabus'}]
    items=sourced_ledger(sample_bundle(),[],e,{},NOW)
    t=next(x for x in items if x['title']=='Every Monday assignment')
    assert t['urgency']=='soon' and t['needs_review']
    assert 'Mon, Oct 26' in t['due_label']

def test_conflicting_dates_flagged():
    e=[{'course_id':'1','title':'Homework 7','due_date':'2026-10-26','source_name':'Syllabus'},
       {'course_id':'1','title':'Homework 7','due_date':'2026-10-27','source_name':'Announcement'}]
    items=sourced_ledger(sample_bundle(),[],e,{},NOW)
    t=[x for x in items if x['title']=='Homework 7']
    assert len(t)==2 and all(x.get('conflict') for x in t)

def test_schedule_after_now_only():
    slots=schedule(NOW,NOW+timedelta(days=2),'evening')
    assert slots
    for t in slots:
        assert datetime.fromisoformat(t['start'])>NOW
        assert datetime.fromisoformat(t['end'])<=NOW+timedelta(days=2)

def test_course_status_fall_current():
    current={'term':{'name':'Fall 2026'},'workflow_state':'available'}
    past={'term':{'name':'Fall 2025'},'workflow_state':'available'}
    assert classify_course(current,set(),NOW)=='current'
    assert classify_course(past,set(),NOW)=='past'

def test_gemini_evidence_validation(monkeypatch):
    text='Midterm One is on October 28 2026. Pearson Written Homework is due October 26 2026 at 11:59 PM. Using ChatGPT on submitted homework is prohibited.'
    b={'courses':[{'id':'1','name':'Math 011','course_code':'MATH 011'}],
       'materials':[{'course_id':'1','name':'Syllabus.pdf','source':'Canvas PDF','text':text}],
       'assignments':[]}
    def fake(*a,**kw):
        return {'events':[{'title':'Pearson Written Homework','kind':'assignment','due_date':'2026-10-26',
                           'due_time':'23:59','platform':'Pearson','source_name':'Syllabus.pdf',
                           'evidence':'Pearson Written Homework is due October 26 2026 at 11:59 PM.'},
                          {'title':'Fabricated exam','kind':'exam','due_date':'2026-10-27',
                           'source_name':'Syllabus.pdf','evidence':'something not in PDF'}],
            'policy':{'status':'restricted','source_name':'Syllabus.pdf',
                     'quote':'Using ChatGPT on submitted homework is prohibited.'},'warnings':[]}
    monkeypatch.setattr('intelligence.json_response',fake)
    result=analyze_syllabus('test','fake',b,'1',NOW)
    assert result['policy']['status']=='restricted'
    assert result['events'][0]['support_verified']
    assert result['events'][1]['needs_review']
    assert result['events'][0]['platform']=='Pearson'

def test_model_policy_not_laundered(monkeypatch):
    b={'courses':[{'id':'1','name':'Math 011'}],
       'materials':[{'course_id':'1','name':'Syllabus.pdf','text':'Lecture topics on calculus.'}],
       'assignments':[]}
    monkeypatch.setattr('intelligence.json_response',lambda *args,**kwargs:{'events':[],
        'policy':{'status':'allowed','source_name':'Syllabus.pdf','quote':'All AI use is explicitly permitted.'}})
    assert analyze_syllabus('test','fake',b,'1',NOW)['policy']['status']=='unknown'

def test_ucsd_fa26_current_when_dates_absent():
    c={'name':'MATH 011 - Statistics [FA26]', 'course_code':'MATH011_FA26_001','workflow_state':'available'}
    assert classify_course(c,set(),NOW)=='current'
    c['name']='MATH 011 - Statistics [FA25]';c['course_code']='MATH011_FA25_001'
    assert classify_course(c,set(),NOW)=='past'
