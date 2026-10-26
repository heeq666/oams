"""Check fictional two-department MVP against isolated PostgreSQL + local server."""
import os, re, sys, urllib.error, urllib.parse, urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


import pytest  # noqa: E402  (test-module wrapper)


pytestmark = pytest.mark.db  # needs a live PostgreSQL and a served app


def _script() -> None:
    """Body of the original standalone e2e script, unchanged."""

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
    import psycopg
    from workhub.security import issue_session
    base = os.environ.get('WORKHUB_DEMO_BASE','http://127.0.0.1:8132')
    with psycopg.connect(os.environ['WORKHUB_DATABASE_URL']) as conn:
        people = dict(conn.execute("SELECT employee_code, member_id FROM workhub.workforce_members WHERE employee_code IN ('MVP1001','MVP2001','ADMIN001')"))
        items = dict(conn.execute("SELECT title, work_item_id FROM workhub.work_items WHERE title LIKE 'MVP演示：%'"))
        hr_org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code='HR'").fetchone()[0]
        sales_org = conn.execute("SELECT org_unit_id FROM workhub.org_units WHERE org_code='SALES'").fetchone()[0]
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl): return None
    def request(opener, path, method='GET', form=None):
        payload = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(base+path, data=payload, method=method)
        if payload is not None: req.add_header('Content-Type','application/x-www-form-urlencoded')
        try:
            with opener.open(req,timeout=12) as resp: return resp.status,resp.read().decode(),dict(resp.headers)
        except urllib.error.HTTPError as err: return err.code,err.read().decode(),dict(err.headers)
    def client(code):
        token,_ = issue_session(people[code])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect,urllib.request.HTTPCookieProcessor(CookieJar()))
        status,_,_=request(opener,'/login','POST',{'token':token})
        assert status==302,(code,status)
        return opener
    admin,hr,sales=client('ADMIN001'),client('MVP1001'),client('MVP2001')
    hr_token,_=issue_session(people['MVP1001'])
    def bearer(path, method='GET', data=None):
        payload = data.encode() if data else None
        req=urllib.request.Request(base+path,data=payload,method=method,headers={'Authorization':'Bearer '+hr_token,'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=12) as resp: return resp.status,resp.read().decode()
        except urllib.error.HTTPError as err: return err.code,err.read().decode()
    status,center,_=request(admin,'/progress')
    assert status==200 and all(x in center for x in ('人事部','业务部','客户需求确认')),(status,center[:500])
    print('PASS company progress shows both departments')
    status,hr_page,_=request(hr,'/reports')
    assert status==200 and '招聘进度汇报' in hr_page and '客户需求确认' not in hr_page,(status,hr_page[:500])
    status,sales_page,_=request(sales,'/reports')
    assert status==200 and '客户需求确认' in sales_page and '招聘进度汇报' not in sales_page
    print('PASS form lists only assigned positions')
    status,hr_center,_=request(hr,'/progress')
    # MVP1001 manages 人事部, so as a department administrator they see every
    # department's detail -- while the reads below still keep them out of Sales *data*.
    assert status==200 and '人事部' in hr_center and '业务部' in hr_center,(status,hr_center[:500])
    status,sales_center,_=request(sales,'/progress')
    # MVP2001 has read and submit only: staff see the company roll-up plus their own
    # department, and the detail of somebody else's department stays hidden.
    assert status==200 and '业务部' in sales_center and '人事部' not in sales_center,(status,sales_center[:500])
    assert request(hr,'/board?org='+str(sales_org))[0]==403
    assert request(hr,'/board/body?org='+str(sales_org))[0]==403
    assert bearer(f'/v1/work-items/{items["MVP演示：客户需求确认"]}/reports')[0]==403
    assert bearer('/v1/work-items?org_unit_id='+str(sales_org))[0]==403
    assert bearer('/v1/board/org-units/'+str(sales_org))[0]==403
    status,body=bearer('/v1/work-items')
    assert status==200 and '招聘进度汇报' in body and '客户需求确认' not in body,(status,body[:500])
    print('PASS web and JSON reads stay inside assigned department')
    csrf=re.search(r'name="csrf" value="([^"]+)"',hr_page).group(1)
    hr_item,sales_item=items['MVP演示：招聘进度汇报'],items['MVP演示：客户需求确认']
    form={'csrf':csrf,'progress_value':'75','note':'演示复核：面试时间已确认','evidence':'虚构演示：面试日程 v2'}
    assert request(hr,f'/reports/{hr_item}','POST',{**form,'csrf':'bad-token'})[0]==403
    assert request(hr,f'/reports/{sales_item}','POST',form)[0]==403
    req=urllib.request.Request(base+f'/v1/work-items/{sales_item}/reports',
        data=b'{"submitted_via":"agent","evidence":"fictional cross-position attempt"}', method='POST',
        headers={'Content-Type':'application/json','Authorization':'Bearer '+hr_token})
    try:
        urllib.request.urlopen(req,timeout=12)
        raise AssertionError('JSON API accepted cross-position report')
    except urllib.error.HTTPError as err:
        assert err.code==403,err.code
    print('PASS invalid CSRF and cross-position reporting rejected on HTML and JSON API')
    status,_,headers=request(hr,f'/reports/{hr_item}','POST',form)
    assert status==303 and 'saved=1' in headers.get('location',''),(status,headers)
    with psycopg.connect(os.environ['WORKHUB_DATABASE_URL']) as conn:
        report=conn.execute('SELECT latest_note,latest_evidence,latest_progress_value FROM workhub.work_item_current_state WHERE work_item_id=%s',(hr_item,)).fetchone()
        latest=conn.execute("SELECT report_id,reported_by_member_id FROM workhub.work_item_reports WHERE work_item_id=%s ORDER BY reported_at DESC,report_id DESC LIMIT 1",(hr_item,)).fetchone()
        audit=conn.execute("SELECT count(*) FROM workhub.workforce_audit_events WHERE subject_type='work_item_report' AND event_type='work_item_report.created' AND subject_id=%s AND actor_member_id=%s",(latest[0],people['MVP1001'])).fetchone()[0]
    assert report==('演示复核：面试时间已确认','虚构演示：面试日程 v2',75),report
    assert latest[1]==people['MVP1001'] and audit==1,(latest,audit)
    status,board,_=request(admin,'/board?org='+str(hr_org))
    assert status==200 and '演示复核：面试时间已确认' in board and '面试日程 v2' in board
    print('PASS report updates canonical board state and audit')
    status,center,_=request(admin,'/progress')
    assert status==200 and all(x in center for x in ('人事部','业务部','MVP演示：客户需求确认'))
    print('PASS company overview retains other department')


def test_mvp_reporting(site) -> None:
    """Run the original standalone script as a pytest test."""

    _script()
