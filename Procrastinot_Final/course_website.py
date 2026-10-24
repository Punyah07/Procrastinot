"""Bounded same-domain import of an optional public course website.

Reads public pages only. Does not log in, bypass paywalls, or crawl arbitrary sites.
"""
from __future__ import annotations

from collections import deque
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import fitz
import httpx

MAX_PAGES=9
MAX_PDFS=8
MAX_PDF_BYTES=7*1024*1024


def validate_site(url: str):
    url=url.strip()
    if not url.startswith('https://'):
        raise ValueError('Please enter a public HTTPS address, such as https://cse29.site/')
    parsed=urlparse(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('Please provide a normal public HTTPS course website URL.')
    host=parsed.hostname
    if host in ('localhost','localhost.localdomain') or host.endswith(('.local','.internal')):
        raise ValueError('Local/private websites cannot be imported.')
    try:
        ips=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        if not ips or any(not ipaddress.ip_address(record[4][0]).is_global for record in ips):
            raise ValueError('This is not a public web address.')
    except socket.gaierror as exc:
        raise ValueError('Could not resolve the website domain.') from exc
    return url.rstrip('/')+'/' if not parsed.path or parsed.path=='/' else url


def crawl_site(url: str, course_id: str, course: str):
    start=validate_site(url)
    host=urlparse(start).hostname
    base=f'https://{host}'
    materials=[];warnings=[]
    seen=set();docs_seen=set();queue=deque([start]);num_pdfs=0
    robot=RobotFileParser()
    robot.set_url(base+'/robots.txt')
    try:
        robot.read()
    except Exception:
        warnings.append('robots.txt could not be checked; automated import should respect site rules.')
        robot=None
    with httpx.Client(timeout=12,follow_redirects=False,headers={'User-Agent':'TRIAGE-StudentOrganizer/1.0 (personal coursework)'}) as client:
        while queue and len(seen)<MAX_PAGES:
            page_url=queue.popleft()
            if page_url in seen: continue
            p=urlparse(page_url)
            if p.scheme!='https' or p.hostname!=host: continue
            if robot and not robot.can_fetch('TRIAGE-StudentOrganizer',page_url):
                warnings.append(f'Site rules disallow automated reading of {p.path or "/"}.')
                continue
            seen.add(page_url)
            try:
                r=client.get(page_url)
                if r.status_code!=200:
                    warnings.append(f'Could not read {p.path or "/"}: HTTP {r.status_code}')
                    continue
                if len(r.content)>2*1024*1024: continue
                if 'html' not in r.headers.get('content-type','').lower(): continue
                soup=BeautifulSoup(r.text,'html.parser')
                for tag in soup(['script','style','nav','footer','noscript']):tag.decompose()
                title=(soup.title.get_text(' ',strip=True) if soup.title else p.path) or 'Course website'
                text=' '.join(soup.get_text(' ',strip=True).split())[:22000]
                if len(text)>90:
                    materials.append({'course_id':course_id,'course':course,'name':f'Web: {title[:70]}',
                                      'text':text,'source':'Public course website','url':page_url})
                for anchor in soup.select('a[href]')[:320]:
                    href=urldefrag(urljoin(page_url,anchor.get('href','')))[0]
                    parsed=urlparse(href)
                    if parsed.scheme!='https' or parsed.hostname!=host: continue
                    if parsed.path.lower().endswith('.pdf') and num_pdfs<MAX_PDFS and href not in docs_seen:
                        docs_seen.add(href)
                        if robot and not robot.can_fetch('TRIAGE-StudentOrganizer',href):continue
                        try:
                            resp=client.get(href)
                            if (resp.status_code==200 and len(resp.content)<MAX_PDF_BYTES
                                    and resp.content.startswith(b'%PDF-')):
                                d=fitz.open(stream=resp.content,filetype='pdf')
                                text=' '.join(' '.join(page.get_text() for page in list(d)[:45]).split())[:24000]
                                d.close()
                                if text:
                                    materials.append({'course_id':course_id,'course':course,
                                                      'name':parsed.path.split('/')[-1] or 'PDF',
                                                      'text':text,'source':'Public course website PDF','url':href})
                                    num_pdfs+=1
                        except Exception as ex:
                            warnings.append(f'Public PDF import failed ({type(ex).__name__}).')
                    elif not re.search(r'\.(?:pdf|png|jpg|jpeg|gif|zip|js|css|svg|mp4)(?:$|\?)',parsed.path,re.I):
                        if href not in seen and href not in queue and len(queue)<50:
                            queue.append(href)
            except Exception as ex:
                warnings.append(f'Could not read course site page ({type(ex).__name__}).')
    return materials,warnings
