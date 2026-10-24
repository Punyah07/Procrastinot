from fastapi.testclient import TestClient
from server import app

client=TestClient(app)

def test_health():
    r=client.get('/api/health'); assert r.status_code==200 and r.json()['ok']

def test_page_has_brand():
    r=client.get('/'); assert r.status_code==200 and 'triage' in r.text.lower()

def test_origin_not_open_to_remote():
    r=client.post('/api/stop',headers={'origin':'https://evil.example'});assert r.status_code==403

def test_key_not_returned():
    st=client.get('/api/state').json();assert 'key' not in st
