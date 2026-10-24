"""TRIAGE — local-first FastAPI app. Binds to 127.0.0.1 only.

Canvas authentication stays in a dedicated Playwright profile on the student's
computer. This server never receives Canvas passwords or tokens.
"""
from __future__ import annotations
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from datetime import datetime,timedelta
from typing import Optional
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from storage import LOCAL,ROOT,read_json,write_json,now_local,get_progress
from engine import sourced_ledger,metrics,NOW,iso_time
from intelligence import analyze_syllabus,make_rescue,MODEL_DEFAULT
from course_website import crawl_site,validate_site

load_dotenv(ROOT/'.env',override=False)
app=FastAPI(title='Procrastinot Academic Companion',version='2.0')
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
lock=threading.RLock()
executor=ThreadPoolExecutor(max_workers=3,thread_name_prefix='triage')
WORKER=ROOT/'canvas_worker.py'

class ConnectionInput(BaseModel):
    canvas_url:str='https://canvas.ucsd.edu'
class SyncInput(BaseModel):
    ids:list[str]
class KeyInput(BaseModel):
    key:str=Field(min_length=12,max_length=250)
    model:str=MODEL_DEFAULT
class EventInput(BaseModel):
    course_id:str
    title:str=Field(min_length=2,max_length=180)
    kind:str='assignment'
    due_date:str=''
    due_time:str=''
    platform:str='Other'
class CompletionInput(BaseModel):
    id:str
    done:bool
class WebsiteInput(BaseModel):
    course_id:str
    url:str
class AnalysisInput(BaseModel):
    ids:list[str]=[]
class PlanInput(BaseModel):
    event_id:str
    preference:str='balanced'

@app.middleware('http')
async def restrict_origin(request:Request,call_next):
    # Host + Origin checks stop another website from submitting requests to this
    # local-only app using a victim's browser.
    host=request.headers.get('host','').split(':')[0].lower()
    if host not in ('127.0.0.1','localhost','[::1]','testserver'):
        return HTMLResponse('Local access only',status_code=403)
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin','')
        if origin:
            u=urlparse(origin)
            if u.hostname not in ('127.0.0.1','localhost'):
                return HTMLResponse('Cross-site requests not allowed',status_code=403)
    return await call_next(request)

@app.get('/')
def index():
    return FileResponse(ROOT/'static'/'index.html')

@app.get('/api/health')
def health():
    return {'ok':True,'version':'2.0'}


def bundles():
    course_data=read_json('canvas_courses.json',{})
    bundle=read_json('canvas_import.json',{})
    if not isinstance(bundle,dict):bundle={}
    if not bundle.get('courses'):
        bundle['courses']=course_data.get('courses',[])
    return bundle,course_data


def get_key():
    env=read_json('gemini_settings.json',{})
    return os.environ.get('GEMINI_API_KEY') or env.get('key') or ''

def get_model():
    model = os.environ.get('GEMINI_MODEL') or read_json('gemini_settings.json',{}).get('model') or MODEL_DEFAULT
    return MODEL_DEFAULT if model == 'gemini-2.5-flash' else model


def selected_ids(bundle):
    return list(map(str,bundle.get('imported_course_ids') or []))


def state_payload():
    bundle,course_data=bundles()
    extracts=read_json('syllabus_intel.json',{})
    extracted=[]
    for result in extracts.values():
        if isinstance(result,dict):extracted.extend(result.get('events') or [])
    ledger=sourced_ledger(bundle,read_json('manual_events.json',[]),extracted,
                           read_json('completion_overrides.json',{}))
    files=[{'course_id':m.get('course_id'),'name':m.get('name'),
            'source':m.get('source'),'url':m.get('url'),
            'characters':len(m.get('text') or '')} for m in bundle.get('materials',[])]
    coverage={str(cid):{'status':r.get('coverage') or 'analyzed',
             'documents_count':r.get('documents_count',0),
             'summary':r.get('coverage_summary',''),
             'warnings':r.get('warnings') or [],
             'policy':r.get('policy') or {},'course_rules':r.get('course_rules') or []} for cid,r in extracts.items() if isinstance(r,dict)}
    stats=metrics(ledger)
    stats['documents']=len(files)
    stats['synced_courses']=len(selected_ids(bundle))
    return {'courses':course_data.get('courses',bundle.get('courses',[])),
        'selected_course_ids':selected_ids(bundle),'events':ledger,'metrics':stats,
        'materials':files, 'coverage':coverage,'worker':get_progress(),
        'analysis':read_json('analysis_status.json',{'state':'idle','message':'Not analyzed yet'}),
        'website':read_json('website_status.json',{'state':'idle'}),
        'warnings':bundle.get('warnings') or [],
        'connected':bool(course_data.get('courses')),
        'synced_at':bundle.get('synced_at'),
        'gemini_configured':bool(get_key()), 'model':get_model(),
        'privacy':'Local files; Gemini receives course material only during AI analysis.'}

@app.get('/api/state')
def state():
    return state_payload()


def run_canvas(action:str,ids:list[str]|None=None,url='https://canvas.ucsd.edu'):
    with lock:
        status=get_progress()
        if status.get('state') in ('login','syncing'):
            raise HTTPException(409,'A Canvas connection is already running. Stop it first.')
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.path not in ('','/') or parsed.username or parsed.password or not parsed.hostname:
            raise HTTPException(400,'Enter a normal HTTPS Canvas homepage address.')
        worker_args=[sys.executable,str(WORKER),action,'--canvas',url.rstrip('/')]
        if ids:
            worker_args.extend(['--ids',','.join(ids)])
        logfile=LOCAL/'canvas_worker.log'
        log=logfile.open('a',encoding='utf8')
        try:
            proc=subprocess.Popen(worker_args,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,start_new_session=True)
        finally:
            log.close()
        write_json('worker_status.json',{'state':'login','message':'Canvas browser starting…','pid':proc.pid,
                  'updated_at':datetime.now().isoformat()})
        write_json('active_job.json',{'pid':proc.pid,'action':action,'ids':ids or [],'started':time.time(),
                                     'noticed':False})
        return {'ok':True,'pid':proc.pid,'message':'Canvas is opening in a new browser window.'}

@app.post('/api/connect')
def connect(inp:ConnectionInput):
    return run_canvas('connect',url=inp.canvas_url)

@app.post('/api/confirm-login')
def confirm_login():
    status=get_progress()
    pid=status.get('pid')
    if status.get('state') not in ('login','syncing') or not pid:
        raise HTTPException(409,'There is no active Canvas login to confirm.')
    write_json('login_confirmation.json',{'pid':pid,'requested_at':datetime.now().isoformat()})
    return {'ok':True,'message':'Canvas verification requested; check the connection status.'}

@app.post('/api/stop')
def stop():
    job=read_json('active_job.json',{})
    pid=job.get('pid')
    if isinstance(pid,int) and pid>0:
        try:
            # Worker is launched in its own process group; kill only our saved PID.
            os.killpg(pid,signal.SIGTERM)
        except (ProcessLookupError,PermissionError,OSError):
            pass
    write_json('worker_status.json',{'state':'stopped','message':'Stopped. You can connect again.',
          'updated_at':datetime.now().isoformat()})
    write_json('active_job.json',{})
    return {'ok':True}

@app.post('/api/sync')
def sync(inp:SyncInput):
    found={str(c['id']) for c in read_json('canvas_courses.json',{}).get('courses',[])}
    ids=list(dict.fromkeys(x for x in inp.ids if x in found))
    if not ids:raise HTTPException(400,'Choose at least one real Canvas course.')
    url=read_json('canvas_courses.json',{}).get('canvas_url') or 'https://canvas.ucsd.edu'
    return run_canvas('sync',ids,url)


def _analyze(ids):
    with lock:
        write_json('analysis_status.json',{'state':'running','message':'Gemini is reading your course syllabi…',
              'done':0,'total':len(ids)})
    bundle,_=bundles()
    existing=read_json('syllabus_intel.json',{})
    errors=[]
    for i,cid in enumerate(ids,1):
        course=next((c for c in bundle.get('courses',[]) if str(c['id'])==str(cid)),{})
        write_json('analysis_status.json',{'state':'running',
            'message':f'Gemini is checking {course.get("course_code") or course.get("name") or cid} for exams, Pearson, and syllabus deadlines…',
            'done':i-1,'total':len(ids)})
        try:
            result=analyze_syllabus(get_key(),get_model(),bundle,cid)
            existing[str(cid)]=result
            write_json('syllabus_intel.json',existing)
        except Exception as ex:
            # Keep Canvas deadlines visible; never falsely claim full coverage.
            errors.append(f'{course.get("course_code") or cid}: {str(ex)[:230]}')
    write_json('analysis_status.json',{'state':'error' if errors else 'done',
        'message':'Some documents need another scan.' if errors else 'Syllabus scan complete. Cross-check source-backed dates.',
        'errors':errors,'done':len(ids)-len(errors),'total':len(ids),
        'completed_at':NOW().isoformat()})


def launch_analysis(ids):
    ids=list(dict.fromkeys(map(str,ids)))
    if not ids:raise HTTPException(400,'Select an imported course first.')
    if not get_key():raise HTTPException(400,'Gemini API key is missing. Add it under Settings first.')
    s=read_json('analysis_status.json',{})
    if s.get('state')=='running':
        raise HTTPException(409,'Gemini is already analyzing materials.')
    write_json('analysis_status.json',{'state':'running','message':'Preparing your syllabus scan…',
              'done':0,'total':len(ids)})
    executor.submit(_analyze,ids)
    return {'ok':True,'message':'Gemini syllabus scan started.'}

@app.post('/api/analyze')
def analyze(inp:AnalysisInput):
    bundle,_=bundles()
    permitted=set(selected_ids(bundle))
    ids=[cid for cid in (inp.ids or list(permitted)) if cid in permitted]
    return launch_analysis(ids)

@app.post('/api/key')
def save_key(inp:KeyInput):
    key=inp.key.strip()
    if '\n' in key or '\r' in key or '\x00' in key:
        raise HTTPException(400,'Invalid API key.')
    if not re.fullmatch(r'[A-Za-z0-9_\-.]+',inp.model):
        raise HTTPException(400,'Invalid model name.')
    write_json('gemini_settings.json',{'key':key,'model':inp.model})
    (LOCAL/'gemini_settings.json').chmod(0o600)
    # No key is returned to browser; not committed to git.
    return {'ok':True,'configured':True}

@app.post('/api/manual')
def add_manual(inp:EventInput):
    course_ids={str(c['id']) for c in bundles()[0].get('courses',[])}
    if inp.course_id not in course_ids:raise HTTPException(400,'Choose a real course.')
    if inp.due_date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',inp.due_date):
        raise HTTPException(400,'Date must be YYYY-MM-DD.')
    from engine import event_id
    obj=inp.model_dump()
    obj['id']=event_id(inp.course_id,inp.title,inp.due_date+inp.due_time,'manual')
    obj.update({'source_name':'Added by student','evidence':'Added or verified by student.',
                'needs_review':not bool(inp.due_date)})
    events=read_json('manual_events.json',[])
    events=[e for e in events if e.get('id')!=obj['id']]+[obj]
    write_json('manual_events.json',events)
    return {'ok':True,'event':obj}

@app.post('/api/completion')
def mark(inp:CompletionInput):
    events=state_payload()['events']
    e=next((e for e in events if e['id']==inp.id),None)
    if not e:raise HTTPException(404,'Assignment not found')
    if e['submitted_by_canvas'] and not inp.done:
        raise HTTPException(409,'Canvas reports a submission; TRIAGE cannot undo it.')
    d=read_json('completion_overrides.json',{})
    d[inp.id]='done' if inp.done else 'open'
    write_json('completion_overrides.json',d)
    return {'ok':True}


def _site_import(cid,url):
    try:
        bundle,_=bundles()
        course=next(c for c in bundle.get('courses',[]) if str(c['id'])==str(cid))
        docs,warnings=crawl_site(url,cid,course.get('course_code') or course['name'])
        bundle,_=bundles() # refresh from the latest Canvas import
        seen={(str(d.get('course_id')),d.get('url')) for d in bundle.get('materials',[])}
        for d in docs:
            if (cid,d.get('url')) not in seen:bundle.setdefault('materials',[]).append(d)
        bundle['warnings']=list(dict.fromkeys((bundle.get('warnings') or [])+warnings))[-35:]
        write_json('canvas_import.json',bundle)
        write_json('website_status.json',{'state':'done','message':f'Added {len(docs)} public source documents.','warnings':warnings})
        if get_key():
            s=read_json('analysis_status.json',{})
            if s.get('state')!='running':
                _analyze([cid])
    except Exception as ex:
        write_json('website_status.json',{'state':'error','message':str(ex)[:400]})

@app.post('/api/website')
def site(inp:WebsiteInput):
    if inp.course_id not in selected_ids(bundles()[0]):
        raise HTTPException(400,'Import the Canvas course first.')
    try:validate_site(inp.url)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc
    s=read_json('website_status.json',{})
    if s.get('state')=='running':raise HTTPException(409,'A website is already being imported.')
    write_json('website_status.json',{'state':'running','message':'Reading permitted public course pages and PDFs…'})
    executor.submit(_site_import,inp.course_id,inp.url)
    return {'ok':True}


def _plan_task(job_id,course_id,event,preference):
    data=read_json('plans.json',{})
    try:
        bundle,_=bundles()
        result=make_rescue(get_key(),get_model(),bundle,course_id,event,
                           mode=event.get('kind','assignment'),preference=preference)
        data=read_json('plans.json',{})
        data[job_id]={'state':'done','plan':result,'event_id':event['id']}
    except Exception as ex:
        data=read_json('plans.json',{})
        data[job_id]={'state':'error','message':str(ex)[:500]}
    write_json('plans.json',data)

@app.post('/api/plan')
def plan(inp:PlanInput):
    events=state_payload()['events']
    event=next((e for e in events if e['id']==inp.event_id),None)
    if not event:raise HTTPException(404,'Event not found')
    if not get_key():raise HTTPException(400,'Add Gemini API key in Settings to generate personalized notes and plans.')
    import uuid
    job_id=uuid.uuid4().hex[:16]
    data=read_json('plans.json',{})
    data[job_id]={'state':'running','event_id':event['id']}
    write_json('plans.json',data)
    preference=inp.preference if inp.preference in ('morning','evening','balanced') else 'balanced'
    executor.submit(_plan_task,job_id,event['course_id'],event,preference)
    return {'ok':True,'job_id':job_id}

@app.get('/api/plan/{job_id}')
def poll_plan(job_id:str):
    data=read_json('plans.json',{})
    if job_id not in data:raise HTTPException(404,'Plan not found')
    return data[job_id]


def watch_canvas():
    last_sync=None
    while True:
        try:
            job=read_json('active_job.json',{})
            status=get_progress()
            if job.get('action')=='sync' and status.get('state')=='done':
                # Auto syllabus scan when Canvas finishes, so dashboard always
                # includes syllabus-only Pearson and midterm information.
                ident=f"{job.get('started')}-{status.get('updated_at')}"
                if ident!=last_sync:
                    last_sync=ident
                    job['noticed']=True
                    write_json('active_job.json',job)
                    ids=job.get('ids') or []
                    if get_key() and ids:
                        a=read_json('analysis_status.json',{})
                        if a.get('state')!='running':
                            try:launch_analysis(ids)
                            except HTTPException:pass
                    elif not get_key():
                        write_json('analysis_status.json',{'state':'needs_key',
                          'message':'Canvas imported, but syllabus/Pearson/exam scan requires a Gemini key. Add one in Settings.'})
            time.sleep(3)
        except Exception:
            time.sleep(5)

@app.on_event('startup')
def startup():
    threading.Thread(target=watch_canvas,daemon=True,name='triage-sync-monitor').start()
