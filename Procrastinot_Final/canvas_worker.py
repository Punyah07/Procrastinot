"""TRIAGE's integrated, single-user Canvas browser worker.

Runs in a separate process because a Streamlit script reruns often and a
Playwright persistent browser context should not be shared between threads.

Only fetches content accessible through the user's signed-in browser session.
No login cookies or credentials are copied into app state or sent to Gemini.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import fitz
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from storage import LOCAL, read_json, write_json, now_local
from planner import infer_kind, parse_due

MAX_PDF_BYTES = 12 * 1024 * 1024
MAX_PDFS_PER_COURSE = 35
MAX_DOCUMENT_TEXT = 40000
MAX_MODULES = 65
MAX_ITEMS_PER_MODULE = 120
MAX_COURSES = 200
MAX_LOGIN_SECONDS = 210


def progress(state, message, **fields):
    write_json('worker_status.json', {'state':state,'message':message,'pid':os.getpid(),'updated_at':datetime.now().isoformat(), **fields})


def clean_text(raw, limit=14000):
    soup = BeautifulSoup(raw or '', 'html.parser')
    for tag in soup(['script','style','iframe']):
        tag.decompose()
    return ' '.join(soup.get_text(' ',strip=True).split())[:limit]


def safe_name(name, fid):
    name = re.sub(r'[^\w. ()-]+', '_', str(name))[:90].strip('. ')
    return f'{fid}_{name or "canvas-document"}.pdf'


def api(page, path, max_pages=4):
    """Read an authorized Canvas GET with Playwright's shared browser cookies.

    Using BrowserContext.request avoids executing JS in a tab during SSO/redirects.
    Crucially this requests only Canvas URLs and returns actual errors rather
    than hanging in page.evaluate while a Canvas page is navigating.
    """
    if not path.startswith('/api/v1/'):
        return None, 'Invalid Canvas API path'
    origin=course_root(page)
    next_url=origin+path
    results=[]
    for _ in range(max_pages):
        try:
            response=page.context.request.get(next_url, timeout=10000,
                                              max_redirects=6,
                                              headers={'Accept':'application/json'})
            if urlparse(response.url).netloc != urlparse(origin).netloc:
                return None, 'Canvas session redirected to login'
            if response.status in (401,403):
                return None, f'Canvas API access denied (HTTP {response.status})'
            if not response.ok:
                return None, f'Canvas returned HTTP {response.status}'
            if 'json' not in response.headers.get('content-type','').lower():
                return None, 'Canvas did not return JSON; login might be incomplete'
            body=response.json()
            if not isinstance(body,list):
                return body,None
            results.extend(body)
            link=response.headers.get('link','')
            match=re.search(r'<([^>]+)>;\s*rel="next"',link)
            if not match:
                break
            next_url=match.group(1)
            next_parsed=urlparse(next_url)
            if next_parsed.scheme!='https' or next_parsed.netloc!=urlparse(origin).netloc or not next_parsed.path.startswith('/api/v1/'):
                return None, 'Unexpected Canvas pagination URL'
        except Exception as exc:
            return None, f'Canvas request failed ({type(exc).__name__})'
    return results,None


def verify_canvas_session(context, root):
    """Probe the same authorized GET that already worked in the CLI tests.

    It shares cookie storage with the TRIAGE browser, but does not depend on
    any particular tab, title, dashboard selector or cross-tab scripting.
    """
    url=root+'/api/v1/courses?enrollment_state=active&per_page=1'
    try:
        response=context.request.get(url, timeout=9000,
                                     max_redirects=5,
                                     headers={'Accept':'application/json'})
        if urlparse(response.url).netloc != urlparse(root).netloc:
            return False,'Canvas redirected to sign-in'
        if response.status in (401,403):
            return False,f'Canvas returned HTTP {response.status}'
        if not response.ok:
            return False,f'Canvas returned HTTP {response.status}'
        if 'json' not in response.headers.get('content-type','').lower():
            return False,'Canvas has not returned course data yet'
        return isinstance(response.json(),list), 'Canvas course API responded successfully'
    except Exception as exc:
        return False,f'Canvas connection failed ({type(exc).__name__})'


def select_canvas_page(context, initial_page, root):
    """Prefer an existing Canvas tab; don't access user Chrome or secrets."""
    for candidate in reversed(list(context.pages)):
        try:
            if urlparse(candidate.url).hostname == urlparse(root).hostname:
                return candidate
        except Exception:
            pass
    return initial_page

def canvas_dashboard_visible(page, root):
    """Recognize the real, authenticated Canvas UI without depending on /profile.

    The student's dashboard contains course links and a Courses navigation item.
    Merely reaching the Canvas hostname is not sufficient to prove sign-in.
    """
    try:
        if urlparse(page.url).hostname != urlparse(root).hostname:
            return False
        # Canvas cards often show course names without '.pdf' or API-dependent data.
        if page.locator('a.dashboard-card, a[href*="/courses/"]').count() > 0:
            return True
        heading = page.locator('h1').all_text_contents()
        return (any('dashboard' in str(h).lower() for h in heading)
                and page.locator('#global_nav_courses_link, a[href="/courses"]').count() > 0)
    except Exception:
        return False


def _user_signed_in(page, root):
    """Accept a visible Canvas dashboard OR authorized first-party read APIs.

    This does not bypass login: the first condition requires authenticated Canvas
    UI; the second requires a successful student-session GET.
    """
    if urlparse(page.url).hostname != urlparse(root).hostname:
        return False
    if canvas_dashboard_visible(page, root):
        return True
    profile, _ = api(page, '/api/v1/users/self/profile', 1)
    if isinstance(profile, dict) and bool(profile.get('id')):
        return True
    courses, _ = api(page, '/api/v1/courses?enrollment_state=active&per_page=1', 1)
    if isinstance(courses, list):
        return True
    courses, _ = api(page, '/api/v1/courses?per_page=1', 1)
    return isinstance(courses, list)


def wait_for_login(context, initial_page, root):
    """Bounded login check with real progress, an explicit Continue handshake.

    Never depend only on a Canvas UI selector. Poll the browser context's
    authorized Canvas course endpoint, which shares its cookies with Chromium.
    """
    progress('login', 'Canvas browser opened. Finish UCSD/Duo sign-in, then click Continue.', check='waiting')
    initial_page.goto(root, wait_until='domcontentloaded', timeout=50000)
    cutoff=time.monotonic()+MAX_LOGIN_SECONDS
    last_probe=0.0
    last_heartbeat=0.0
    last_marker=None
    last_error='Waiting for Canvas sign-in'
    while time.monotonic()<cutoff:
        # Dashboard DOM check: useful when institution disables the courses API.
        for candidate in reversed(list(context.pages)):
            try:
                if canvas_dashboard_visible(candidate,root):
                    progress('syncing','Canvas Dashboard detected. Loading your courses…',check='verified')
                    return candidate
            except Exception:
                continue

        marker=read_json('login_confirmation.json',{})
        manual=(marker.get('pid')==os.getpid())
        marker_id=marker.get('requested_at') if manual else None
        now_mono=time.monotonic()
        # Full shared-cookie probe every 8 seconds, or immediately on Continue.
        if (manual and marker_id!=last_marker) or now_mono-last_probe>=8:
            last_probe=now_mono
            if manual:
                last_marker=marker_id
                progress('login','Checking your authenticated Canvas session…',check='checking')
            ok, message=verify_canvas_session(context,root)
            if ok:
                progress('syncing','Canvas session verified. Discovering courses…',check='verified')
                return select_canvas_page(context,initial_page,root)
            last_error=message
            if manual:
                # Make failure explicit, never leave the same "Checking…" state.
                progress('login',f'Canvas has not connected yet: {message}. Keep the Canvas Dashboard open and try Continue again.',check='failed')
            else:
                progress('login','Waiting for Canvas sign-in. When your Dashboard is visible, click Continue.',check='waiting')
            # Clear the request after processing so another press is a new test.
            if manual:
                write_json('login_confirmation.json', {'pid':None})
            last_heartbeat=now_mono
        if now_mono-last_heartbeat>=12:
            progress('login','Waiting for Canvas login. Use the TRIAGE-opened Chromium window; click Continue after the Dashboard loads.',check='waiting',hint=last_error[:150])
            last_heartbeat=now_mono
        time.sleep(0.8)
    progress('error','Canvas sign-in check timed out. The browser may be signed in, but TRIAGE could not verify the session. Close this browser window and click Connect again.',check='timed_out')
    return None

def dashboard_links(page, root):
    found={}
    try:
        page.goto(root+'/dashboard', wait_until='domcontentloaded',timeout=40000)
        page.wait_for_timeout(1500)
        for link in page.locator('a[href*="/courses/"]').all()[:250]:
            href=link.get_attribute('href') or ''
            match=re.search(r'/courses/(\d+)', href)
            if not match:
                continue
            title=(link.get_attribute('aria-label') or link.inner_text(timeout=600) or '').strip()
            if title and len(title)<200:
                found.setdefault(match.group(1),title)
    except Exception:
        pass
    return found


def classify_course(c, dashboard_ids, now):
    term=c.get('term') if isinstance(c.get('term'),dict) else {}
    start=parse_due(c.get('start_at') or term.get('start_at'))
    end=parse_due(c.get('end_at') or term.get('end_at'))
    state=str(c.get('workflow_state') or '')
    if state in ('completed','deleted') or (end and end<now):
        return 'past'
    if start and start>now:
        return 'other'
    if start and start<=now and (not end or end>=now):
        return 'current'
    if end and end>=now:
        return 'current'
    term_name=str(term.get('name',''))
    m=re.search(r'(?i)\b(fall|autumn|winter|spring|summer)\s+(20\d\d)\b',term_name)
    if m:
        season,year=m.group(1).lower(),int(m.group(2))
        months={'fall':(9,10,11,12),'autumn':(9,10,11,12),'winter':(1,2,3),'spring':(3,4,5,6),'summer':(6,7,8,9)}
        if year<now.year:
            return 'past'
        if year==now.year and now.month in months[season]:
            return 'current'
    # UCSD course titles often contain an academic term such as FA26, WI26, SP26.
    # Only classify these when the term is explicit; never mark every active
    # enrollment as current simply because Canvas retained an old course.
    candidates=(str(c.get('name') or '')+' '+str(c.get('course_code') or '')+' '+term_name)
    short=re.search(r'(?i)\b(FA|WI|SP|SU)[-_ ]?(20\d\d|\d\d)\b',candidates)
    if short:
        season,year=short.group(1).upper(),int(short.group(2))
        if year<100: year+=2000
        windows={'FA':(9,10,11,12),'WI':(1,2,3),'SP':(3,4,5,6),'SU':(6,7,8,9)}
        if year<now.year:return 'past'
        if year==now.year and now.month in windows[season]:return 'current'
    # Dashboard favorites might be historical; don't label as current without evidence.
    return 'other'


def collect_courses(page, root):
    dash=dashboard_links(page,root)
    raw,error=api(page,'/api/v1/courses?enrollment_state=active&include%5B%5D=term&per_page=100',4)
    if not isinstance(raw,list):
        raw,error=api(page,'/api/v1/courses?include%5B%5D=term&per_page=100',4)
    courses=[]
    now=now_local()
    if isinstance(raw,list):
        for c in raw[:MAX_COURSES]:
            if not isinstance(c,dict) or not c.get('id'):
                continue
            cid=str(c['id'])
            course_code=str(c.get('course_code') or '')
            name=str(c.get('name') or dash.get(cid) or course_code or 'Untitled course')
            term=c.get('term') if isinstance(c.get('term'),dict) else {}
            courses.append({'id':cid,'name':name,'course_code':course_code or name[:25],
                            'category':classify_course(c,set(dash),now),
                            'term':str(term.get('name') or ''), 'source':'Canvas'})
    else:
        courses=[{'id':cid,'name':name,'course_code':name[:25],'category':'other',
                  'term':'','source':'Canvas'} for cid,name in dash.items()]
    courses.sort(key=lambda x:({'current':0,'other':1,'past':2}.get(x['category'],3), x['name'].lower()))
    return courses, ([] if isinstance(raw,list) else [f'Course API limited: {error}. Dashboard listing fallback.'])


def file_from_metadata(page, fid, course, label=''):
    data, error=api(page,f'/api/v1/files/{fid}',1)
    if not isinstance(data,dict):
        return None, f'File {fid}: metadata unavailable ({error})'
    if data.get('locked_for_user') or data.get('hidden_for_user'):
        return None, None
    name=str(data.get('display_name') or data.get('filename') or label or f'file_{fid}')
    mime=str(data.get('content-type') or data.get('content_type') or '').lower()
    if not (name.lower().endswith('.pdf') or mime=='application/pdf'):
        return None,None
    if isinstance(data.get('size'),int) and data['size']>MAX_PDF_BYTES:
        return None,f'Skipped large PDF: {name}'
    url=data.get('url')
    if not isinstance(url,str) or urlparse(url).scheme!='https':
        return None,f'Canvas did not provide a downloadable HTTPS URL: {name}'
    try:
        response=page.context.request.get(url,timeout=35000,max_redirects=8)
        if not response.ok:
            return None,f'File download unsuccessful: {name} (HTTP {response.status})'
        size=response.headers.get('content-length','')
        if size.isdigit() and int(size)>MAX_PDF_BYTES:
            return None,f'Skipped large PDF: {name}'
        body=response.body()
        if len(body)>MAX_PDF_BYTES or not body.startswith(b'%PDF-'):
            return None,f'Skipped non-PDF or oversized file: {name}'
        folder=LOCAL/'course_pdfs'/str(course['id'])
        folder.mkdir(parents=True,exist_ok=True)
        output=folder/safe_name(name,str(fid))
        output.write_bytes(body)
        doc=fitz.open(stream=body,filetype='pdf')
        segments=[]
        for p in list(doc)[:65]:
            segments.append(p.get_text('text'))
            if sum(len(s) for s in segments)>MAX_DOCUMENT_TEXT:
                break
        doc.close()
        text=' '.join(' '.join(segments).split())[:MAX_DOCUMENT_TEXT]
        return {'course_id':str(course['id']),'course':course.get('course_code') or course['name'],
                'name':name,'source':'Canvas PDF','url':f"{course_root(page)}/courses/{course['id']}/files/{fid}",
                'text':text,'local_path':str(output),'file_id':str(fid),'size':len(body)}, None
    except Exception as exc:
        return None, f'Could not import {name}: {type(exc).__name__}'


def course_root(page):
    u=urlparse(page.url)
    return f'{u.scheme}://{u.netloc}'


def import_course(page, root, course):
    cid=str(course['id'])
    code=course.get('course_code') or course['name']
    warnings=[]
    progress('syncing',f'Checking {code}: Canvas assignments and submission status…')
    page.goto(root+'/dashboard',wait_until='domcontentloaded',timeout=40000)
    assignments=[]
    raw,err=api(page,f'/api/v1/courses/{cid}/assignments?include%5B%5D=submission&per_page=100',5)
    if isinstance(raw,list):
        for a in raw[:400]:
            if not isinstance(a,dict) or not a.get('id'):
                continue
            sub=a.get('submission') or {}
            substate=sub.get('workflow_state') if isinstance(sub,dict) else None
            if isinstance(sub,dict) and sub.get('submitted_at'):
                substate='submitted'
            assignments.append({'id':str(a['id']),'course_id':cid,'course':code,'title':a.get('name') or 'Untitled assignment',
                   'due_at':a.get('due_at'),'points_possible':a.get('points_possible'),
                   'html_url':a.get('html_url') or f'{root}/courses/{cid}/assignments/{a["id"]}',
                   'description':clean_text(a.get('description'),9000),'submission_state':str(substate or 'unknown'),
                   'kind':infer_kind(a.get('name') or ''),'source':'Canvas assignment',
                   'rubric':str(a.get('rubric') or '')[:3000]})
    else:
        warnings.append(f'Canvas assignments unavailable for {code}: {err}')
    # Some courses use the Canvas To Do calendar rather than the Assignments tab.
    # External Pearson/written homework often still appears only in the syllabus.
    # All these sources are merged later instead of treating Canvas as exhaustive.
    # Some instructors announce exams as calendar events rather than assignments.
    calendar,cal_error=api(page,f'/api/v1/calendar_events?type=event&context_codes%5B%5D=course_{cid}&per_page=100',2)
    if isinstance(calendar,list):
        for ev in calendar[:100]:
            if not isinstance(ev,dict) or not ev.get('id'):
                continue
            title=ev.get('title') or 'Canvas calendar event'
            if infer_kind(title)!='exam':
                continue
            assignments.append({'id':'calendar-'+str(ev['id']),'course_id':cid,'course':code,
                     'title':title,'due_at':ev.get('start_at'),'points_possible':None,
                     'html_url':ev.get('html_url') or f'{root}/calendar',
                     'description':clean_text(ev.get('description') or '',6000),
                     'submission_state':'unknown','kind':'exam','source':'Canvas calendar event',
                     'rubric':''})
    materials=[]
    course_detail,e=api(page,f'/api/v1/courses/{cid}?include%5B%5D=syllabus_body',1)
    if isinstance(course_detail,dict) and course_detail.get('syllabus_body'):
        materials.append({'course_id':cid,'course':code,'name':'Canvas syllabus page',
                          'text':clean_text(course_detail.get('syllabus_body'),24000),
                          'source':'Canvas syllabus','url':f'{root}/courses/{cid}/assignments/syllabus'})
    progress('syncing',f'Checking {code}: Modules, readings and PDF files…', found_assignments=len(assignments))
    modules,moderr=api(page,f'/api/v1/courses/{cid}/modules?per_page=100',2)
    file_ids={}
    page_links=[]
    if isinstance(modules,list):
        for module in modules[:MAX_MODULES]:
            mid=module.get('id')
            if not mid:
                continue
            items,err=api(page,f'/api/v1/courses/{cid}/modules/{mid}/items?per_page=100',2)
            if not isinstance(items,list):
                continue
            for item in items[:MAX_ITEMS_PER_MODULE]:
                if item.get('type')=='File' and item.get('content_id'):
                    file_ids.setdefault(str(item['content_id']),item.get('title') or '')
                elif item.get('type')=='Page' and item.get('page_url'):
                    page_links.append((item.get('title') or 'Course page', item['page_url']))
    else:
        warnings.append(f'Could not list modules in {code}: {moderr}')
    # Catch PDFs not attached to modules, where authorized.
    listing,listingerr=api(page,f'/api/v1/courses/{cid}/files?content_types%5B%5D=application%2Fpdf&per_page=100',2)
    if isinstance(listing,list):
        for item in listing[:130]:
            if isinstance(item,dict) and item.get('id'):
                file_ids.setdefault(str(item['id']),item.get('display_name') or '')
    # A syllabus is the primary source of external deadlines. Prioritize it over
    # lecture slides, independent of whether Canvas hides the .pdf extension.
    def importance(item):
        label=str(item[1]).lower()
        terms=[('syllabus',0),('schedule',1),('calendar',2),('course outline',3),
               ('midterm',4),('exam',4),('review',5),('homework',6),
               ('assignment',6),('lecture',8)]
        score=next((rank for term,rank in terms if term in label),7)
        return (score,label)
    ranked=sorted(file_ids.items(),key=importance)
    # Page text may contain course calendars, policies and externally-hosted homework.
    # Keep a bounded set of module pages; treat their HTML as data, not instructions.
    for label,slug in page_links[:28]:
        from urllib.parse import quote
        detail,page_err=api(page, f'/api/v1/courses/{cid}/pages/{quote(str(slug),safe="")}',1)
        if isinstance(detail,dict) and detail.get('body'):
            body=str(detail['body'])
            materials.append({'course_id':cid,'course':code,'name':f'Module page: {label}',
                  'text':clean_text(body,20000),'source':'Canvas module page',
                  'url':f'{root}/courses/{cid}/pages/{quote(str(slug),safe="")}'})
            for fid in re.findall(r'/(?:courses/\d+/)?files/(\d+)',body):
                file_ids.setdefault(fid,label)
    # Syllabus text frequently links to a PDF not surfaced under Files.
    if isinstance(course_detail,dict):
        for fid in re.findall(r'/(?:courses/\d+/)?files/(\d+)',str(course_detail.get('syllabus_body') or '')):
            file_ids.setdefault(fid,'Syllabus-linked document')
    for i,(fid,label) in enumerate(ranked[:MAX_PDFS_PER_COURSE],1):
        progress('syncing',f'{code}: importing document {i} of {min(len(file_ids), MAX_PDFS_PER_COURSE)}…',
                 found_assignments=len(assignments),found_files=len(materials))
        material,warning=file_from_metadata(page,fid,course,label)
        if material:
            materials.append(material)
        if warning:
            warnings.append(warning)
    if len(file_ids)>MAX_PDFS_PER_COURSE:
        warnings.append(f'For speed, only the first {MAX_PDFS_PER_COURSE} file candidates were checked in {code}.')
    return assignments, materials, warnings


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['connect','sync'])
    parser.add_argument('--canvas', default='https://canvas.ucsd.edu')
    parser.add_argument('--ids',default='')
    args=parser.parse_args()
    root=args.canvas.strip().rstrip('/')
    u=urlparse(root)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in ('','/'):
        progress('error','Canvas URL must be a normal HTTPS Canvas domain.');return
    profile=LOCAL/'canvas_browser_profile'
    try:
        with sync_playwright() as pw:
            context=pw.chromium.launch_persistent_context(str(profile),headless=False,
                            viewport={'width':1280,'height':850},accept_downloads=True)
            try:
                page=context.pages[0] if context.pages else context.new_page()
                signed_in_page=wait_for_login(context,page,root)
                if signed_in_page is None:
                    progress('error','Canvas sign-in was not detected. Use the Chromium window opened by TRIAGE, reach your Dashboard, and click the manual Continue button.');return
                page=signed_in_page
                progress('syncing','Canvas connected! Discovering your courses…')
                courses,warnings=collect_courses(page,root)
                if not courses:
                    progress('error','Connected to Canvas, but no courses were found. Check Dashboard access.');return
                write_json('canvas_courses.json',{'courses':courses,'warnings':warnings,'canvas_url':root,
                                                  'connected_at':datetime.now().isoformat()})
                if args.action=='connect':
                    progress('done',f'Connected! Found {len(courses)} courses; pick your current courses in TRIAGE.',
                             count=len(courses));return
                requested={s for s in args.ids.split(',') if s.isdecimal()}
                chosen=[c for c in courses if str(c['id']) in requested]
                if not chosen:
                    progress('error','None of the chosen courses appeared in Canvas. Reconnect and select again.');return
                stored=read_json('canvas_import.json',{'demo':False,'courses':[],'assignments':[], 'materials':[], 'warnings':[], 'imported_course_ids':[]})
                chosen_ids={str(c['id']) for c in chosen}
                a_all=[a for a in stored.get('assignments',[]) if str(a.get('course_id')) not in chosen_ids]
                m_all=[m for m in stored.get('materials',[]) if str(m.get('course_id')) not in chosen_ids]
                completed_ids=set(map(str, stored.get('imported_course_ids', [])))
                errors=[]
                for course in chosen:
                    a,m,w=import_course(page,root,course)
                    a_all.extend(a);m_all.extend(m);errors.extend(w)
                    completed_ids.add(str(course['id']))
                    write_json('canvas_import.json',{'demo':False,'courses':courses,'imported_course_ids':sorted(completed_ids),
                                    'assignments':a_all,'materials':m_all,'warnings':errors,
                                    'synced_at':now_local().isoformat()})
                progress('done',f'Imported {len(chosen)} course(s): {len(a_all)} assignments and {len(m_all)} documents.',
                         imported_courses=len(chosen), found_assignments=len(a_all),found_files=len(m_all))
            finally:
                context.close()
    except Exception as exc:
        progress('error',f'Canvas importer stopped: {type(exc).__name__}. Check the local worker log for details.')
        raise

if __name__=='__main__':
    main()
