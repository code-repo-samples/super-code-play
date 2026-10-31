r"""
SPICE job-log analyzer -> interactive HTML dashboard.

Files needed: this script + jobs.txt (one job id per line, blank lines and
lines starting with # are ignored).

Usage:
    PowerShell:   $env:SPICE_TOKEN = "eyJ..." ; python spice_report.py
    cmd.exe:      set SPICE_TOKEN=eyJ...      & python spice_report.py
    (If SPICE_TOKEN is not set, you are prompted and the input is hidden.)

Options:
    python spice_report.py [path\to\jobs.txt] [--no-open]

Requires: requests, pytz   (pandas is no longer needed)
The report is one self-contained .html file. All thresholds, Top-N sizes,
filters and column choices are adjustable inside the report itself.
"""
import json
import os
import re
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from getpass import getpass
from pathlib import Path

import pytz
import requests

# --------------------------------------------------------------------------
# CONFIG  (the thresholds below are only the *defaults*; you can change them
#          live in the report without re-running anything)
# --------------------------------------------------------------------------
INPUT_FILE = 'jobs.txt'
URL = 'https://qa.nonprod.int.coke.com/graphql'
MAX_WORKERS = 3
TIMEOUT_SEC = 30
RETRIES = 2                       # extra attempts per job on network/server errors
TIMEZONE_NAME = 'US/Eastern'
TIMEZONE = pytz.timezone(TIMEZONE_NAME)

SLOW_THRESHOLD_SEC = 5            # step wait or process longer than this = flagged
JOB_LATENCY_THRESHOLD = 900.0     # job longer than this = "long job"
KEY_COMPONENTS = ['ROUTER', 'XMLMETADATA_PROCESSOR', 'XMLEXTRACT_PROCESSOR']
TOP_N_DEFAULT = 10                # default rows in "Top errors" / leaderboard

# Matched case-insensitively against business-log messages.
STRICT_ERROR_WORDS = ['No more retries. Marking job as FAILED.', 'RestError', 'no such file']

QUERY = """
query Query($id: String!, $asc: Boolean) {
  job(id: $id) {
    status id fileSize creationTimestamp deliveryTimestamp failureTimestamp
    sender { name } receiver { name } dataType { name } metadata
    logs(first: 500, asc: $asc) {
      edges { node { component timestamp failed businessLog { type message timestamp } } }
    }
  }
}
"""


# --------------------------------------------------------------------------
# INPUT
# --------------------------------------------------------------------------
def read_job_ids(path):
    ids, seen = [], set()
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            s = line.strip().strip(',').strip('"').strip("'").strip()
            if not s or s.startswith('#') or s in seen:
                continue
            seen.add(s)
            ids.append(s)
    return ids


def get_token():
    tok = os.environ.get('SPICE_TOKEN', '').strip()
    if not tok:
        tok = getpass('Bearer token (input hidden): ').strip()
    if tok.lower().startswith('bearer '):
        tok = tok[7:].strip()
    return tok


# --------------------------------------------------------------------------
# FETCH
# --------------------------------------------------------------------------
def fetch_job_data(job_id, headers):
    """Returns (job_dict, None) on success or (None, reason) on failure."""
    last_err = 'Unknown error'
    for attempt in range(RETRIES + 1):
        try:
            r = requests.post(
                URL, headers=headers, timeout=TIMEOUT_SEC,
                json={'query': QUERY, 'variables': {'id': job_id, 'asc': True}})
            if r.status_code in (401, 403):
                return None, f'HTTP {r.status_code}: token rejected or expired'
            r.raise_for_status()
            body = r.json()
            gql_errors = body.get('errors')
            job = (body.get('data') or {}).get('job')
            if job:
                return job, None
            if gql_errors:
                last_err = str(gql_errors[0].get('message', 'GraphQL error'))[:200]
            else:
                return None, 'Job not found'
        except Exception as e:  # network, JSON, HTTP 5xx ...
            last_err = f'{type(e).__name__}: {e}'[:200]
        time.sleep(1.5 * (attempt + 1))
    return None, last_err


# --------------------------------------------------------------------------
# ANALYSIS
# --------------------------------------------------------------------------
_RE_UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
_RE_FILE = re.compile(r'\S+\.(?:xml|dat|edi|csv|txt|json|ont|zip|gz|pdf)\b', re.I)
_RE_HEX = re.compile(r'\b[0-9a-fA-F]{16,}\b')
_RE_NUM = re.compile(r'\d{4,}')
_RE_WS = re.compile(r'\s+')


def error_key(msg):
    """Normalize an error message so the same error groups together
    (ids, file names and long numbers are replaced by placeholders)."""
    k = _RE_UUID.sub('<id>', str(msg))
    k = _RE_FILE.sub('<file>', k)
    k = _RE_HEX.sub('<hex>', k)
    k = _RE_NUM.sub('<n>', k)
    return _RE_WS.sub(' ', k).strip()[:160]


def get_steps(job):
    """Merge consecutive log nodes of the same component into one step and
    compute wait (gap since previous step ended) and process (own duration)."""
    edges = ((job.get('logs') or {}).get('edges')) or []
    strict = [w.lower() for w in STRICT_ERROR_WORDS]
    raw = []
    for edge in edges:
        node = edge.get('node') or {}
        logs = node.get('businessLog') or []
        ts_list = [l['timestamp'] for l in logs if l.get('timestamp') is not None]
        if not ts_list and node.get('timestamp') is not None:
            ts_list = [node['timestamp']]
        if not ts_list:
            continue
        err_msg = 'System reported failure.' if node.get('failed') else None
        for l in logs:
            msg = str(l.get('message') or '')
            if l.get('type') == 'ERROR' or any(w in msg.lower() for w in strict):
                err_msg = msg
                break
        raw.append({
            'comp': node.get('component') or '?',
            'start_ts': min(ts_list), 'end_ts': max(ts_list),
            'failed': bool(node.get('failed')) or err_msg is not None,
            'err_msg': err_msg,
            'last_msg': str(logs[-1].get('message') or '-') if logs else '-',
        })

    merged = []
    if raw:
        curr = dict(raw[0])
        for nxt in raw[1:]:
            if nxt['comp'] == curr['comp']:
                curr['end_ts'] = max(curr['end_ts'], nxt['end_ts'])
                curr['failed'] = curr['failed'] or nxt['failed']
                if not curr['err_msg']:
                    curr['err_msg'] = nxt['err_msg']
                curr['last_msg'] = nxt['last_msg']
            else:
                merged.append(curr)
                curr = dict(nxt)
        merged.append(curr)

    steps, prev_end = [], job['creationTimestamp']
    for p in merged:
        msg = p['err_msg'] if p['failed'] and p['err_msg'] else ''
        if p['failed'] and not msg:
            msg = 'System reported failure.'
        steps.append({
            'n': p['comp'],
            'w': round(max(0, (p['start_ts'] - prev_end) / 1000), 3),
            'pr': round(max(0, (p['end_ts'] - p['start_ts']) / 1000), 3),
            'e': msg,
            'ek': error_key(msg) if msg else '',
            'lm': p['last_msg'][:300],
        })
        prev_end = p['end_ts']
    return steps, (merged[-1]['end_ts'] if merged else None)


def build_record(job_id, job):
    created = job['creationTimestamp']
    steps, last_step_end = get_steps(job)
    end_ts = job.get('deliveryTimestamp') or job.get('failureTimestamp') or last_step_end
    total = (end_ts - created) / 1000 if end_ts else 0
    meta = job.get('metadata') or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    filename = (meta.get('filename') if isinstance(meta, dict) else None) or job_id
    created_dt = datetime.fromtimestamp(created / 1000, pytz.utc).astimezone(TIMEZONE)
    return {
        'id': job_id,
        'file': filename,
        'status': job.get('status') or '-',
        'sender': (job.get('sender') or {}).get('name') or '-',
        'receiver': (job.get('receiver') or {}).get('name') or '-',
        'dtype': (job.get('dataType') or {}).get('name') or '-',
        'size': job.get('fileSize'),
        'created': created_dt.strftime('%Y-%m-%d %H:%M:%S'),
        'created_ms': created,
        'total': round(total, 3),
        'steps': steps,
    }


# --------------------------------------------------------------------------
# REPORT
# --------------------------------------------------------------------------
def generate_report(records, fetch_errors, source_name, out_dir='.'):
    ts_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    payload = {
        'jobs': records,
        'fetch_errors': fetch_errors,
        'meta': {
            'generated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'source': source_name,
            'excel_name': f'Spice_Data_{ts_str}',
        },
    }
    config = {
        'step': SLOW_THRESHOLD_SEC, 'job': JOB_LATENCY_THRESHOLD,
        'key': KEY_COMPONENTS, 'topN': TOP_N_DEFAULT, 'tz': 'America/New_York',
    }

    def js(obj):
        s = json.dumps(obj, ensure_ascii=True, separators=(',', ':'))
        return s.replace('</', '<\\/').replace('<!--', '<\\!--')

    page = HTML_TEMPLATE.replace('__DATA__', js(payload)).replace('__CONFIG__', js(config))
    path = Path(out_dir) / f'Spice_Report_{ts_str}.html'
    path.write_text(page, encoding='utf-8')
    return path


def main(argv):
    flags = [a for a in argv[1:] if a.startswith('--')]
    args = [a for a in argv[1:] if not a.startswith('--')]
    path = args[0] if args else INPUT_FILE
    if not os.path.exists(path):
        print(f'Error: {path} not found. Create it and add one Job ID per line.')
        return 1
    ids = read_job_ids(path)
    if not ids:
        print(f'Error: {path} has no job IDs.')
        return 1

    token = get_token()
    if not token:
        print('Error: no token provided.')
        return 1
    headers = {'authorization': f'Bearer {token}', 'content-type': 'application/json'}

    print(f'Analyzing {len(ids)} jobs from {path} ...')
    order = {jid: i for i, jid in enumerate(ids)}
    records, errors = [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_job_data, jid, headers): jid for jid in ids}
        for n, fut in enumerate(as_completed(futures), 1):
            jid = futures[fut]
            job, err = fut.result()
            if job is None:
                errors.append({'id': jid, 'reason': err})
            else:
                try:
                    records.append(build_record(jid, job))
                except Exception as e:
                    errors.append({'id': jid, 'reason': f'Could not parse job: {type(e).__name__}: {e}'[:200]})
            print(f'\r  {n}/{len(ids)} done', end='', flush=True)
    print()
    records.sort(key=lambda r: order[r['id']])
    errors.sort(key=lambda r: order[r['id']])

    if not records:
        print('No jobs could be retrieved. First problems seen:')
        for e in errors[:5]:
            print(f"  {e['id'][:60]}...  {e['reason']}")
        return 1

    report = generate_report(records, errors, os.path.basename(path))
    failed = sum(1 for r in records if r['status'] == 'FAILED')
    print(f'Done. {len(records)} jobs analyzed, {failed} failed, {len(errors)} could not be fetched.')
    print(f'Report: {report.resolve()}')
    if '--no-open' not in flags:
        try:
            webbrowser.open(report.resolve().as_uri())
        except Exception:
            pass
    return 0


# --------------------------------------------------------------------------
# HTML TEMPLATE  (raw string: JS backslashes are preserved)
# --------------------------------------------------------------------------
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPICE Analytical Reporting</title>
<style>
:root{--pri:#0d47a1;--bd:#e0e4ea;--bg:#f4f6f9;--tx:#1f2933;--mut:#6b7785;--ok:#2e7d32;--okbg:#e8f5e9;--wt:#e65100;--wtbg:#fff3e0;--pr:#0d47a1;--prbg:#e3f2fd;--er:#dc3545;--erbg:#fdecee}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);font:13px/1.45 'Segoe UI',system-ui,sans-serif}
header{background:linear-gradient(90deg,#002060,#054a91);color:#fff;padding:14px 22px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
h1{margin:0;font-size:20px;font-weight:600}
.sub{font-size:12px;opacity:.8;margin-top:2px}
main{padding:16px 22px 40px;max-width:1700px;margin:0 auto}
.btn{border:1px solid #c5ccd6;background:#fff;color:var(--tx);border-radius:6px;padding:5px 11px;font:inherit;cursor:pointer}
.btn:hover{background:#eef2f7}
.btn.green{background:#198754;border-color:#198754;color:#fff;font-weight:600}
.btn.green:hover{background:#146c43}
.btn.ghost{background:transparent;color:#fff;border-color:rgba(255,255,255,.5)}
.btn.on{background:var(--pri);border-color:var(--pri);color:#fff}
.btn.err{color:var(--er);border-color:var(--er)}.btn.err.on{background:var(--er);color:#fff}
input,select{font:inherit;border:1px solid #c5ccd6;border-radius:6px;padding:4px 7px;background:#fff;color:var(--tx)}
input[type=number]{width:78px}
.bar{background:#fff;border:1px solid var(--bd);border-radius:10px;padding:10px 14px;margin-bottom:12px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center}
.bar label{display:flex;align-items:center;gap:6px;color:var(--mut)}
.bar .grow{flex:1;min-width:220px}.bar .grow input{flex:1;min-width:120px}
.bar b{color:var(--tx)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:12px}
.card{background:#fff;border:1px solid var(--bd);border-bottom:4px solid #c5ccd6;border-radius:10px;padding:12px;text-align:center;cursor:pointer}
.card:hover{background:#fafbfd}
.card .l{color:var(--mut);font-size:12px}.card .v{font-size:28px;font-weight:600;line-height:1.2}
.card.bad{border-bottom-color:var(--er)}.card.bad .v{color:var(--er)}
.card.warn{border-bottom-color:var(--wt)}.card.warn .v{color:var(--wt)}
.card.info{border-bottom-color:var(--pr)}.card.info .v{color:var(--pr)}
.card.on{outline:2px solid var(--pri);outline-offset:-2px;background:var(--prbg)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px}
.fchip{background:var(--prbg);color:var(--pr);border-radius:14px;padding:2px 4px 2px 10px;display:inline-flex;gap:6px;align-items:center}
.fchip i{font-style:normal;cursor:pointer;border-radius:50%;width:18px;height:18px;line-height:17px;text-align:center;background:#fff}
.fchip i:hover{background:var(--er);color:#fff}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:12px}
.panel{background:#fff;border:1px solid var(--bd);border-radius:10px;padding:12px 14px;min-width:0}
.panel.span2{grid-column:1/-1}
.panel.hide{display:none}
.ph{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.ph h3{margin:0;font-size:14px;font-weight:600}
.ph .ctl{display:flex;gap:8px;align-items:center;color:var(--mut);flex-wrap:wrap}
.note{color:var(--mut);font-size:12px;margin:0 0 8px}
.empty{color:var(--mut);padding:14px 0;text-align:center}
table{border-collapse:collapse;width:100%}
th{background:#f8f9fa;text-transform:uppercase;font-size:10.5px;color:var(--mut);text-align:left;padding:7px 8px;border-bottom:1px solid var(--bd);white-space:nowrap}
th.sort{cursor:pointer;user-select:none}th.sort:hover{color:var(--tx)}
td{padding:7px 8px;border-bottom:1px solid #eef0f3;vertical-align:middle}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr.clk{cursor:pointer}tr.clk:hover td{background:#f6f9fe}tr.sel td{background:var(--prbg)}
.wrap{overflow-x:auto}
.badge{display:inline-block;border-radius:4px;padding:1px 7px;font-size:11px;font-weight:600}
.badge.ok{background:var(--okbg);color:var(--ok)}.badge.bad{background:var(--er);color:#fff}
.chip{display:inline-block;border-radius:4px;padding:1px 6px;margin:1px 3px 1px 0;font-size:11px;white-space:nowrap}
.chip.err{background:var(--er);color:#fff}.chip.long{background:var(--wtbg);color:var(--wt);border:1px solid var(--wt)}
.chip.wait{background:var(--wtbg);color:var(--wt);font-weight:600}.chip.proc{background:var(--prbg);color:var(--pr);font-weight:600}
.chip.okc{background:var(--okbg);color:var(--ok)}.chip.more{background:#eceff3;color:var(--mut)}
.warn{color:var(--wt);font-weight:600}.blue{color:var(--pr);font-weight:600}.red{color:var(--er);font-weight:600}
.hbar{display:flex;height:12px;border-radius:3px;overflow:hidden;background:#eceff3;min-width:140px}
.hbar span{display:block;height:100%}
.sw{background:#a5d6a7}.sq{background:#fb8c00}.sp{background:#1976d2}.se{background:#dc3545}.swo{background:#ffe0b2}
.row{display:grid;grid-template-columns:150px minmax(0,1fr) 150px;gap:8px;align-items:center;padding:4px 0;cursor:pointer}
.row:hover{background:#f6f9fe}.row.sel{background:var(--prbg)}
.row .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.row .vl{font-size:11px;color:var(--mut);text-align:right;white-space:nowrap}
.fn{font-weight:600;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.jid{color:var(--mut);font-size:10.5px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
td.car{width:20px;color:var(--mut)}
tr.detail td{background:#fafbfd;padding:10px 14px 14px}
.dmeta{color:var(--mut);margin-bottom:6px;font-size:12px;word-break:break-all}
.dt td,.dt th{padding:4px 8px}.dt td.cw{background:var(--wtbg);color:var(--wt);font-weight:600}.dt td.cp{background:var(--prbg);color:var(--pr);font-weight:600}
.pager{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap;color:var(--mut)}
.banner{background:#fff8e1;border:1px solid #ffca28;border-radius:10px;padding:10px 14px;margin-bottom:12px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--mut);font-size:12px;margin-top:8px}
.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
svg text{font-size:10.5px;fill:#6b7785}
details summary{cursor:pointer}
@media (max-width:1000px){.grid{grid-template-columns:minmax(0,1fr)}}
@media print{.noprint{display:none!important}body{background:#fff}.panel,.card{break-inside:avoid}header{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
</style>
</head>
<body>
<header>
  <div><h1>SPICE Analytical Reporting</h1><div class="sub" id="sub"></div></div>
  <div class="noprint"><button class="btn green" id="btnX">Download Excel</button> <button class="btn ghost" id="btnP">Print</button></div>
</header>
<main>
  <div id="banner"></div>

  <section class="bar noprint">
    <b>Thresholds</b>
    <label>Step over <input id="stepT" type="number" min="0" step="0.5"> s</label>
    <label>Job over <input id="jobT" type="number" min="0" step="10"> s</label>
    <label class="grow">Key components (comma separated) <input id="keyC" type="text"></label>
    <button class="btn" id="btnReset">Reset</button>
  </section>

  <section class="cards" id="cards"></section>

  <section class="bar noprint">
    <b>Filters</b>
    <span>
      <button class="btn err" data-act="quick" data-v="errors" id="qErr">Errors</button>
      <button class="btn" data-act="quick" data-v="slow" id="qSlow">Slowness</button>
      <button class="btn" data-act="quick" data-v="all" id="qAll">Show all</button>
    </span>
    <label>Status <select id="fStatus"></select></label>
    <label>Sender <select id="fSender"></select></label>
    <label>Receiver <select id="fReceiver"></select></label>
    <label>Component <select id="fComp"></select></label>
    <label>Total over <input id="minT" type="number" min="0" step="10"> s</label>
    <label class="grow">Search <input id="fText" type="text" placeholder="job id, file, sender, error text..."></label>
    <button class="btn" data-act="clear">Clear filters</button>
  </section>
  <div class="chips" id="chips"></div>

  <section class="bar noprint" id="viewBar">
    <b>Show</b>
    <label><input type="checkbox" data-panel="time" checked> Time breakdown</label>
    <label><input type="checkbox" data-panel="err" checked> Top errors</label>
    <label><input type="checkbox" data-panel="lb" checked> Leaderboard</label>
    <label><input type="checkbox" data-panel="tl" checked> Timeline</label>
    <label><input type="checkbox" data-panel="fail" checked> Failures</label>
    <b style="margin-left:12px">Columns</b>
    <label><input type="checkbox" data-col="id" checked> Job ID</label>
    <label><input type="checkbox" data-col="sender" checked> Sender</label>
    <label><input type="checkbox" data-col="receiver" checked> Receiver</label>
    <label><input type="checkbox" data-col="created" checked> Created</label>
    <label><input type="checkbox" data-col="bar" checked> Time bar</label>
    <label><input type="checkbox" data-col="analysis" checked> Analysis</label>
  </section>

  <div class="grid">
    <section class="panel" id="p-time"></section>
    <section class="panel" id="p-err"></section>
    <section class="panel" id="p-lb"></section>
    <section class="panel" id="p-tl"></section>
    <section class="panel span2" id="p-fail"></section>
  </div>

  <section class="panel" id="p-jobs"></section>

  <details class="legend" style="margin-top:12px"><summary>Legend</summary>
    <div class="legend">
      <span><span class="dot sw"></span>Healthy step</span>
      <span><span class="dot sq"></span>Slow queue / wait handoff</span>
      <span><span class="dot sp"></span>Slow internal processing</span>
      <span><span class="dot se"></span>Error step</span>
      <span>&#10006; Job failed</span><span>&#8856; Step error</span><span>&#9650; Long job</span><span>&#9873; Key component</span>
    </div>
  </details>
</main>

<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<script>
(function(){
'use strict';
const DATA = __DATA__;
const CFG = __CONFIG__;
const $ = id => document.getElementById(id);
const esc = s => String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const f1 = x => (Math.round(x*10)/10).toLocaleString(undefined,{minimumFractionDigits:1,maximumFractionDigits:1});
const f2 = x => (Math.round(x*100)/100).toFixed(2);
const pct = x => (Math.round(x*1000)/10).toFixed(1)+'%';
function hms(sec){ sec=Math.round(sec); const h=Math.floor(sec/3600), m=Math.floor(sec%3600/60), s=sec%60; return (h?h+'h ':'')+((h||m)?m+'m ':'')+s+'s'; }
function short(s,n){ s=String(s); return s.length>n ? s.slice(0,n-1)+'\u2026' : s; }

const PREF_KEY='spice_report_prefs_v1';
function loadPrefs(){ try{ return JSON.parse(localStorage.getItem(PREF_KEY)||'{}'); }catch(e){ return {}; } }
function savePrefs(){ try{ localStorage.setItem(PREF_KEY, JSON.stringify({hidden:S.hidden,cols:S.cols,pageSize:S.pageSize,topN:S.topN,lbN:S.lbN,errSort:S.errSort,lbGroup:S.lbGroup})); }catch(e){} }

const P = loadPrefs();
const S = {
  stepT:CFG.step, jobT:CFG.job, key:new Set(CFG.key),
  quick:'all', status:'', sender:'', receiver:'', comp:'', text:'', minTotal:0, errKey:'', only:'',
  sortCol:'total', sortDir:-1, page:0, pageSize:(P.pageSize!=null?P.pageSize:25),
  topN:(P.topN!=null?P.topN:CFG.topN), lbN:(P.lbN!=null?P.lbN:CFG.topN), errSort:P.errSort||'count',
  lbGroup:P.lbGroup||'component', lbKey:'', lbDir:-1,
  open:new Set(), hidden:P.hidden||{},
  cols:Object.assign({id:true,sender:true,receiver:true,created:true,bar:true,analysis:true}, P.cols||{})
};

DATA.jobs.forEach((j,i)=>{
  j.i=i;
  j.hay=[j.id,j.file,j.sender,j.receiver,j.status,j.dtype].concat(j.steps.map(s=>s.n+' '+s.e+' '+s.lm)).join(' ').toLowerCase();
});

function enrich(){
  DATA.jobs.forEach(j=>{
    j.failed = j.status==='FAILED';
    j.longJob = j.total>S.jobT;
    let q=0,p=0,e=0;
    j.steps.forEach(s=>{ s.q=s.w>S.stepT; s.p=s.pr>S.stepT; s.err=!!s.e; s.key=S.key.has(s.n); if(s.q)q++; if(s.p)p++; if(s.err)e++; });
    j.nq=q; j.np=p; j.ne=e;
    j.nflags=q+p+e+(j.longJob?1:0)+(j.failed?1:0);
  });
}

function hasErr(j,k){
  if(k==='__job_failed__') return j.failed && !j.steps.some(s=>s.err);
  return j.steps.some(s=>s.err && (s.ek||s.e)===k);
}
function passes(j,skip){
  skip=skip||{};
  const q=S.quick;
  if(q==='failed' && !j.failed) return false;
  if(q==='errors' && !(j.failed||j.ne>0)) return false;
  if(q==='slow' && !(j.longJob||j.nq>0||j.np>0)) return false;
  if(q==='queue' && j.nq===0) return false;
  if(q==='process' && j.np===0) return false;
  if(q==='long' && !j.longJob) return false;
  if(S.only && j.id!==S.only) return false;
  if(S.status && j.status!==S.status) return false;
  if(S.sender && !skip.sender && j.sender!==S.sender) return false;
  if(S.receiver && !skip.receiver && j.receiver!==S.receiver) return false;
  if(S.comp && !skip.comp && !j.steps.some(s=>s.n===S.comp)) return false;
  if(S.errKey && !skip.err && !hasErr(j,S.errKey)) return false;
  if(j.total<S.minTotal) return false;
  if(S.text && j.hay.indexOf(S.text.toLowerCase())<0) return false;
  return true;
}
function filtered(skip){ return DATA.jobs.filter(j=>passes(j,skip)); }

function sortVal(j,k){
  switch(k){
    case 'file': return j.file.toLowerCase();
    case 'status': return j.status;
    case 'total': return j.total;
    case 'sender': return j.sender.toLowerCase();
    case 'receiver': return j.receiver.toLowerCase();
    case 'created': return j.created_ms;
    case 'flags': return j.nflags;
  }
  return 0;
}
function sortedJobs(){
  const a=filtered(), k=S.sortCol, d=S.sortDir;
  a.sort((x,y)=>{ const u=sortVal(x,k), v=sortVal(y,k); return u<v?-d:(u>v?d:0); });
  return a;
}

function chipsFor(j){
  const c=[];
  if(j.failed) c.push({w:1000,cls:'err',t:'\u2716 Job failed'});
  if(j.longJob) c.push({w:800,cls:'long',t:'\u25B2 Long job '+f1(j.total)+'s'});
  j.steps.forEach((s,i)=>{
    const lab='S'+(i+1)+' '+s.n, fl=s.key?'\u2691 ':'';
    if(s.err) c.push({w:900,cls:'err',t:'\u2298 '+fl+lab+' error'});
    if(s.q) c.push({w:s.w,cls:'wait',t:fl+lab+' wait '+f1(s.w)+'s'});
    if(s.p) c.push({w:s.pr,cls:'proc',t:fl+lab+' process '+f1(s.pr)+'s'});
  });
  c.sort((a,b)=>b.w-a.w);
  return c;
}
function analysisText(j){ const c=chipsFor(j); return c.length ? c.map(x=>x.t).join('; ') : 'Healthy'; }

function panelHead(title,ctl){ return '<div class="ph"><h3>'+title+'</h3><div class="ctl noprint">'+(ctl||'')+'</div></div>'; }
function options(list,cur){ return list.map(o=>'<option value="'+esc(o[0])+'"'+(String(o[0])===String(cur)?' selected':'')+'>'+esc(o[1])+'</option>').join(''); }
const NCHOICES=[[5,'Top 5'],[10,'Top 10'],[25,'Top 25'],[50,'Top 50'],[0,'All']];

function renderCards(){
  const J=DATA.jobs;
  const defs=[
    ['all','Jobs sampled',J.length,''],
    ['failed','Failed jobs',J.filter(j=>j.failed).length,'bad'],
    ['errors','Jobs with errors',J.filter(j=>j.failed||j.ne>0).length,'bad'],
    ['long','Long jobs (over '+S.jobT+'s)',J.filter(j=>j.longJob).length,'warn'],
    ['queue','Queue wait (step over '+S.stepT+'s)',J.filter(j=>j.nq>0).length,'warn'],
    ['process','Slow process (step over '+S.stepT+'s)',J.filter(j=>j.np>0).length,'info']
  ];
  $('cards').innerHTML=defs.map(d=>'<div class="card '+d[3]+(S.quick===d[0]?' on':'')+'" data-act="quick" data-v="'+d[0]+'"><div class="l">'+esc(d[1])+'</div><div class="v">'+d[2]+'</div></div>').join('');
}

function renderTime(){
  const F=filtered({comp:true});
  let W=0,Pr=0; const by={};
  F.forEach(j=>j.steps.forEach(s=>{ W+=s.w; Pr+=s.pr; const b=by[s.n]||(by[s.n]={n:s.n,w:0,p:0}); b.w+=s.w; b.p+=s.pr; }));
  const arr=Object.values(by);
  let h=panelHead('Where the time went');
  if(!arr.length){ $('p-time').innerHTML=h+'<div class="empty">No steps in the current view.</div>'; return; }
  const tot=(W+Pr)||1;
  const topW=arr.slice().sort((a,b)=>b.w-a.w)[0];
  h+='<p class="note">Across '+F.length+' jobs, waiting is <b>'+pct(W/tot)+'</b> of all step time ('+hms(W)+' wait vs '+hms(Pr)+' processing). '
    +(W>0?'Largest wait: <b>'+esc(topW.n)+'</b> ('+pct(topW.w/(W||1))+' of all wait).':'')+'</p>';
  const rows=arr.sort((a,b)=>(b.w+b.p)-(a.w+a.p)).slice(0,10);
  const max=rows[0].w+rows[0].p||1;
  h+=rows.map(r=>{
    const ww=(r.w/max*100), pp=(r.p/max*100);
    return '<div class="row'+(S.comp===r.n?' sel':'')+'" data-act="comp" data-v="'+esc(r.n)+'" title="Click to filter jobs containing '+esc(r.n)+'">'
      +'<div class="nm">'+esc(r.n)+'</div>'
      +'<div class="hbar"><span class="sq" style="width:'+ww+'%"></span><span class="sp" style="width:'+pp+'%"></span></div>'
      +'<div class="vl">wait '+hms(r.w)+' \u00B7 proc '+hms(r.p)+'</div></div>';
  }).join('');
  h+='<div class="legend"><span><span class="dot sq"></span>Wait before step</span><span><span class="dot sp"></span>Processing</span></div>';
  $('p-time').innerHTML=h;
}

function errGroups(F){
  const g=new Map();
  F.forEach(j=>{
    let hit=false;
    j.steps.forEach(s=>{
      if(!s.err) return; hit=true;
      const k=s.ek||s.e; let o=g.get(k);
      if(!o){ o={k:k,msg:s.e,n:0,jobs:new Set(),comps:new Map()}; g.set(k,o); }
      o.n++; o.jobs.add(j.id); o.comps.set(s.n,(o.comps.get(s.n)||0)+1);
    });
    if(j.failed && !hit){
      const k='__job_failed__'; let o=g.get(k);
      if(!o){ o={k:k,msg:'Job FAILED (no step error recorded)',n:0,jobs:new Set(),comps:new Map()}; g.set(k,o); }
      o.n++; o.jobs.add(j.id);
    }
  });
  return [...g.values()];
}
function renderErr(){
  const F=filtered({err:true});
  let list=errGroups(F);
  list.sort((a,b)=> S.errSort==='jobs' ? (b.jobs.size-a.jobs.size || b.n-a.n) : (b.n-a.n || b.jobs.size-a.jobs.size));
  const total=list.length;
  const shown=S.topN>0 ? list.slice(0,S.topN) : list;
  const ctl='<select data-sel="topN">'+options(NCHOICES,S.topN)+'</select><select data-sel="errSort">'+options([['count','Sort: occurrences'],['jobs','Sort: jobs affected']],S.errSort)+'</select>';
  let h=panelHead('Top errors',ctl);
  if(!total){ $('p-err').innerHTML=h+'<div class="empty">No errors in the current view.</div>'; return; }
  h+='<p class="note">Same error text is grouped (ids, file names and long numbers ignored). Click a row to filter the job table.</p>';
  h+='<div class="wrap"><table><thead><tr><th>#</th><th>Error</th><th>Component</th><th class="num">Count</th><th class="num">Jobs</th></tr></thead><tbody>';
  h+=shown.map((o,i)=>{
    const comps=[...o.comps.entries()].sort((a,b)=>b[1]-a[1]).slice(0,2).map(c=>c[0]).join(', ')||'-';
    return '<tr class="clk'+(S.errKey===o.k?' sel':'')+'" data-act="err" data-v="'+esc(o.k)+'"><td>'+(i+1)+'</td><td title="'+esc(o.msg)+'">'+esc(short(o.msg,110))+'</td><td>'+esc(comps)+'</td><td class="num"><span class="badge bad">'+o.n+'</span></td><td class="num">'+o.jobs.size+'</td></tr>';
  }).join('');
  h+='</tbody></table></div><div class="note" style="margin-top:6px">Showing '+shown.length+' of '+total+' error types</div>';
  $('p-err').innerHTML=h;
}

function renderLB(){
  const g=S.lbGroup;
  const skip={comp:g==='component',sender:g==='sender',receiver:g==='receiver'};
  const F=filtered(skip);
  let rows=[], cols;
  if(g==='component'){
    const m=new Map();
    F.forEach(j=>j.steps.forEach(s=>{
      let o=m.get(s.n); if(!o){ o={name:s.n,c:0,w:0,p:0,mw:0,mp:0,over:0,err:0}; m.set(s.n,o); }
      o.c++; o.w+=s.w; o.p+=s.pr; if(s.w>o.mw)o.mw=s.w; if(s.pr>o.mp)o.mp=s.pr; if(s.q||s.p)o.over++; if(s.err)o.err++;
    }));
    rows=[...m.values()].map(o=>({name:o.name,c:o.c,aw:o.w/o.c,ap:o.p/o.c,mw:o.mw,mp:o.mp,over:o.over,err:o.err}));
    cols=[['name','Component'],['c','Steps'],['aw','Avg wait'],['ap','Avg process'],['mw','Max wait'],['mp','Max process'],['over','Over thr.'],['err','Errors']];
  } else {
    const m=new Map();
    F.forEach(j=>{
      const k=j[g]; let o=m.get(k); if(!o){ o={name:k,c:0,t:0,mx:0,long:0,f:0}; m.set(k,o); }
      o.c++; o.t+=j.total; if(j.total>o.mx)o.mx=j.total; if(j.longJob)o.long++; if(j.failed)o.f++;
    });
    rows=[...m.values()].map(o=>({name:o.name,c:o.c,avg:o.t/o.c,mx:o.mx,long:o.long,f:o.f}));
    cols=[['name',g==='sender'?'Sender':'Receiver'],['c','Jobs'],['avg','Avg total'],['mx','Max total'],['long','Long jobs'],['f','Failed']];
  }
  if(!cols.some(c=>c[0]===S.lbKey)){ S.lbKey=(g==='component'?'over':'long'); S.lbDir=-1; }
  const k=S.lbKey, d=S.lbDir;
  rows.sort((a,b)=>{
    const u=k==='name'?String(a.name).toLowerCase():a[k], v=k==='name'?String(b.name).toLowerCase():b[k];
    if(u<v) return -d; if(u>v) return d;
    return (b.mw||b.mx||0)-(a.mw||a.mx||0);
  });
  const total=rows.length;
  const shown=S.lbN>0?rows.slice(0,S.lbN):rows;
  const ctl='<select data-sel="lbGroup">'+options([['component','By component'],['sender','By sender'],['receiver','By receiver']],S.lbGroup)+'</select><select data-sel="lbN">'+options(NCHOICES,S.lbN)+'</select>';
  let h=panelHead('Leaderboard',ctl);
  if(!total){ $('p-lb').innerHTML=h+'<div class="empty">Nothing in the current view.</div>'; return; }
  const sel=(g==='component'?S.comp:S[g]);
  h+='<div class="wrap"><table><thead><tr>'+cols.map(c=>'<th class="sort'+(c[0]==='name'?'':' num')+'" data-act="lbsort" data-v="'+c[0]+'">'+c[1]+(k===c[0]?(d<0?' \u25BC':' \u25B2'):'')+'</th>').join('')+'</tr></thead><tbody>';
  h+=shown.map(r=>{
    let cells;
    if(g==='component'){
      cells='<td>'+esc(r.name)+'</td><td class="num">'+r.c+'</td><td class="num'+(r.aw>S.stepT?' warn':'')+'">'+f2(r.aw)+'s</td><td class="num'+(r.ap>S.stepT?' blue':'')+'">'+f2(r.ap)+'s</td><td class="num'+(r.mw>S.stepT?' warn':'')+'">'+f1(r.mw)+'s</td><td class="num'+(r.mp>S.stepT?' blue':'')+'">'+f1(r.mp)+'s</td><td class="num">'+r.over+'</td><td class="num'+(r.err?' red':'')+'">'+r.err+'</td>';
    } else {
      cells='<td>'+esc(r.name)+'</td><td class="num">'+r.c+'</td><td class="num">'+f1(r.avg)+'s</td><td class="num'+(r.mx>S.jobT?' warn':'')+'">'+f1(r.mx)+'s</td><td class="num'+(r.long?' warn':'')+'">'+r.long+'</td><td class="num'+(r.f?' red':'')+'">'+r.f+'</td>';
    }
    return '<tr class="clk'+(sel===r.name?' sel':'')+'" data-act="grp" data-v="'+esc(r.name)+'">'+cells+'</tr>';
  }).join('');
  h+='</tbody></table></div><div class="note" style="margin-top:6px">Showing '+shown.length+' of '+total+'. Click a row to filter.</div>';
  $('p-lb').innerHTML=h;
}

function renderTimeline(){
  const F=filtered();
  let h=panelHead('Timeline: job duration by creation time');
  if(!F.length){ $('p-tl').innerHTML=h+'<div class="empty">No jobs in the current view.</div>'; return; }
  const W=640,H=240,L=54,R=14,T=14,B=32;
  const xs=F.map(j=>j.created_ms);
  let x0=Math.min.apply(null,xs), x1=Math.max.apply(null,xs);
  if(x0===x1){ x0-=1800000; x1+=1800000; }
  const ymax=Math.max(S.jobT*1.15, Math.max.apply(null,F.map(j=>j.total)))*1.05||1;
  const X=v=>L+(v-x0)/(x1-x0)*(W-L-R), Y=v=>T+(1-v/ymax)*(H-T-B);
  const fmt=ms=>new Date(ms).toLocaleString('en-US',{timeZone:CFG.tz,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  let svg='<svg viewBox="0 0 '+W+' '+H+'" width="100%" role="img" aria-label="Scatter of job duration against creation time">';
  for(let i=0;i<=4;i++){
    const v=ymax*i/4, y=Y(v);
    svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+y+'" y2="'+y+'" stroke="#e8ebef"/><text x="'+(L-6)+'" y="'+(y+3)+'" text-anchor="end">'+Math.round(v)+'s</text>';
    const xv=x0+(x1-x0)*i/4, x=X(xv);
    svg+='<text x="'+x+'" y="'+(H-12)+'" text-anchor="'+(i===0?'start':(i===4?'end':'middle'))+'">'+esc(fmt(xv))+'</text>';
  }
  const ty=Y(S.jobT);
  svg+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+ty+'" y2="'+ty+'" stroke="#e65100" stroke-dasharray="5 4"/><text x="'+(W-R)+'" y="'+(ty-4)+'" text-anchor="end" style="fill:#e65100">job threshold '+S.jobT+'s</text>';
  const pts=F.slice().sort((a,b)=>(a.failed?1:0)-(b.failed?1:0));
  pts.forEach(j=>{
    const col=j.failed?'#dc3545':(j.longJob?'#e65100':'#1976d2');
    svg+='<circle data-act="pick" data-v="'+j.i+'" cx="'+X(j.created_ms).toFixed(1)+'" cy="'+Y(j.total).toFixed(1)+'" r="4.5" fill="'+col+'" fill-opacity=".75" stroke="#fff" stroke-width="1" style="cursor:pointer"><title>'+esc(j.file)+'\n'+f1(j.total)+'s \u00B7 '+esc(j.created)+'\n'+esc(j.sender)+' \u2192 '+esc(j.receiver)+'</title></circle>';
  });
  svg+='</svg>';
  h+='<p class="note">Slow jobs that cluster in time usually point to one backlog event. Click a dot to open that job.</p>'+svg
    +'<div class="legend"><span><span class="dot" style="background:#1976d2"></span>Normal</span><span><span class="dot" style="background:#e65100"></span>Long job</span><span><span class="dot" style="background:#dc3545"></span>Failed</span></div>';
  $('p-tl').innerHTML=h;
}

function renderFail(){
  const F=filtered().filter(j=>j.failed);
  let h=panelHead('Failure spotlight');
  if(!F.length){ $('p-fail').innerHTML=h+'<div class="empty">No failed jobs in the current view.</div>'; return; }
  h+='<div class="wrap"><table><thead><tr><th>Job</th><th>Route</th><th class="num">Total</th><th>Failing step</th><th>Error</th></tr></thead><tbody>';
  h+=F.map(j=>{
    let si=j.steps.findIndex(s=>s.err);
    const s=si>=0?j.steps[si]:null;
    return '<tr class="clk" data-act="pick" data-v="'+j.i+'"><td><div class="fn" title="'+esc(j.file)+'">'+esc(j.file)+'</div></td><td>'+esc(j.sender)+' \u2192 '+esc(j.receiver)+'</td><td class="num">'+f1(j.total)+'s</td><td>'+(s?'S'+(si+1)+' '+esc(s.n):'-')+'</td><td class="red" title="'+esc(s?s.e:'')+'">'+esc(s?short(s.e,140):'No step error recorded')+'</td></tr>';
  }).join('');
  h+='</tbody></table></div>';
  $('p-fail').innerHTML=h;
}

function barFor(j){
  const base=j.steps.reduce((a,s)=>a+s.w+s.pr,0)||1;
  let h='<div class="hbar" title="Step time: '+hms(base)+' (hover segments for detail)">';
  j.steps.forEach((s,i)=>{
    const tip=esc('S'+(i+1)+' '+s.n+': wait '+f2(s.w)+'s, process '+f2(s.pr)+'s');
    if(s.w>0) h+='<span class="'+(s.q?'sq':'swo')+'" style="width:'+(s.w/base*100)+'%;min-width:'+(s.w>0.5?1:0)+'px" title="'+tip+'"></span>';
    if(s.pr>0||s.w===0) h+='<span class="'+(s.err?'se':(s.p?'sp':'sw'))+'" style="width:'+(s.pr/base*100)+'%;min-width:1px" title="'+tip+'"></span>';
  });
  return h+'</div>';
}
function detailFor(j,span){
  let h='<tr class="detail"><td colspan="'+span+'"><div class="dmeta"><b>'+esc(j.file)+'</b><br>ID: '+esc(j.id)+' \u00B7 Type: '+esc(j.dtype)+(j.size!=null?' \u00B7 Size: '+esc(j.size):'')+' \u00B7 Created: '+esc(j.created)+' \u00B7 Total: '+f1(j.total)+'s ('+hms(j.total)+')</div>';
  h+='<div class="wrap"><table class="dt"><thead><tr><th>#</th><th>Component</th><th class="num">Wait</th><th class="num">Process</th><th>Result</th><th>Last log message</th></tr></thead><tbody>';
  h+=j.steps.map((s,i)=>'<tr><td>'+(i+1)+'</td><td>'+(s.key?'\u2691 ':'')+esc(s.n)+'</td><td class="num'+(s.q?' cw':'')+'">'+f2(s.w)+'s</td><td class="num'+(s.p?' cp':'')+'">'+f2(s.pr)+'s</td><td>'+(s.err?'<span class="red" title="'+esc(s.e)+'">'+esc(short(s.e,120))+'</span>':'<span style="color:var(--ok)">OK</span>')+'</td><td title="'+esc(s.lm)+'">'+esc(short(s.lm,120))+'</td></tr>').join('');
  return h+'</tbody></table></div></td></tr>';
}

function renderTable(){
  const list=sortedJobs(), n=list.length;
  const ps=S.pageSize>0?S.pageSize:(n||1);
  const pages=Math.max(1,Math.ceil(n/ps));
  if(S.page>=pages) S.page=pages-1; if(S.page<0) S.page=0;
  const slice=list.slice(S.page*ps,(S.page+1)*ps);
  const th=(k,l,cls)=>'<th class="sort '+(cls||'')+'" data-act="sort" data-v="'+k+'">'+l+(S.sortCol===k?(S.sortDir<0?' \u25BC':' \u25B2'):'')+'</th>';
  let span=4+(S.cols.sender?1:0)+(S.cols.receiver?1:0)+(S.cols.created?1:0)+(S.cols.bar?1:0)+(S.cols.analysis?1:0);
  let h=panelHead('Jobs ('+n+')','<select data-sel="pageSize">'+options([[10,'10 / page'],[25,'25 / page'],[50,'50 / page'],[100,'100 / page'],[0,'All']],S.pageSize)+'</select>');
  h+='<div class="wrap"><table><thead><tr><th></th>'+th('file','Job')+th('status','Status')+th('total','Total','num')
    +(S.cols.sender?th('sender','Sender'):'')+(S.cols.receiver?th('receiver','Receiver'):'')+(S.cols.created?th('created','Created'):'')
    +(S.cols.bar?'<th>Time breakdown</th>':'')+(S.cols.analysis?th('flags','Analysis'):'')+'</tr></thead><tbody>';
  if(!slice.length) h+='<tr><td colspan="'+span+'" class="empty">No jobs match the current filters.</td></tr>';
  slice.forEach(j=>{
    const open=S.open.has(j.id);
    let ch=chipsFor(j), chipHtml;
    if(!ch.length) chipHtml='<span class="chip okc">\u25CF Healthy</span>';
    else {
      const lim=open?ch.length:4;
      chipHtml=ch.slice(0,lim).map(c=>'<span class="chip '+c.cls+'">'+esc(c.t)+'</span>').join('');
      if(ch.length>lim) chipHtml+='<span class="chip more">+'+(ch.length-lim)+' more</span>';
    }
    h+='<tr class="clk" data-act="toggle" data-v="'+j.i+'"><td class="car">'+(open?'\u25BE':'\u25B8')+'</td>'
      +'<td><div class="fn" title="'+esc(j.file)+'">'+esc(j.file)+'</div>'+(S.cols.id?'<div class="jid" title="'+esc(j.id)+'">'+esc(j.id)+'</div>':'')+'</td>'
      +'<td><span class="badge '+(j.failed?'bad':'ok')+'">'+esc(j.status)+'</span></td>'
      +'<td class="num'+(j.longJob?' warn':'')+'">'+f1(j.total)+'s</td>'
      +(S.cols.sender?'<td>'+esc(j.sender)+'</td>':'')+(S.cols.receiver?'<td>'+esc(j.receiver)+'</td>':'')
      +(S.cols.created?'<td style="white-space:nowrap">'+esc(j.created.slice(5))+'</td>':'')
      +(S.cols.bar?'<td style="min-width:160px">'+barFor(j)+'</td>':'')
      +(S.cols.analysis?'<td>'+chipHtml+'</td>':'')+'</tr>';
    if(open) h+=detailFor(j,span);
  });
  h+='</tbody></table></div>';
  h+='<div class="pager"><span>'+(n?('Showing '+(S.page*ps+1)+'\u2013'+Math.min(n,(S.page+1)*ps)+' of '+n):'0 jobs')+'</span><span class="noprint"><button class="btn" data-act="page" data-v="-1"'+(S.page<=0?' disabled':'')+'>Prev</button> Page '+(S.page+1)+' of '+pages+' <button class="btn" data-act="page" data-v="1"'+(S.page>=pages-1?' disabled':'')+'>Next</button></span></div>';
  $('p-jobs').innerHTML=h;
}

function renderChips(){
  const c=[];
  const QL={failed:'Failed jobs',errors:'Jobs with errors',slow:'Slowness',queue:'Queue wait',process:'Slow process',long:'Long jobs'};
  if(S.quick!=='all') c.push(['quick','View: '+QL[S.quick]]);
  if(S.status) c.push(['status','Status: '+S.status]);
  if(S.sender) c.push(['sender','Sender: '+S.sender]);
  if(S.receiver) c.push(['receiver','Receiver: '+S.receiver]);
  if(S.comp) c.push(['comp','Component: '+S.comp]);
  if(S.errKey) c.push(['err','Error: '+short(S.errKey==='__job_failed__'?'Job failed':S.errKey,50)]);
  if(S.only){ const j=DATA.jobs.find(x=>x.id===S.only); c.push(['only','Job: '+short(j?j.file:S.only,50)]); }
  if(S.text) c.push(['text','Search: '+S.text]);
  if(S.minTotal>0) c.push(['min','Total over '+S.minTotal+'s']);
  $('chips').innerHTML=c.map(x=>'<span class="fchip">'+esc(x[1])+'<i data-act="rm" data-v="'+x[0]+'" title="Remove">\u00D7</i></span>').join('');
}

function syncControls(){
  const set=(id,v)=>{ const e=$(id); if(e.value!==String(v)) e.value=v; };
  set('fStatus',S.status); set('fSender',S.sender); set('fReceiver',S.receiver); set('fComp',S.comp);
  set('fText',S.text); set('minT',S.minTotal>0?S.minTotal:'');
  ['qErr:errors','qSlow:slow','qAll:all'].forEach(p=>{ const a=p.split(':'); $(a[0]).classList.toggle('on',S.quick===a[1]); });
  ['time','err','lb','tl','fail'].forEach(k=>$('p-'+k).classList.toggle('hide',!!S.hidden[k]));
}

function renderAll(){
  renderCards(); renderChips(); syncControls();
  renderTime(); renderErr(); renderLB(); renderTimeline(); renderFail(); renderTable();
  savePrefs();
}

function resetFilters(){
  S.quick='all'; S.status=''; S.sender=''; S.receiver=''; S.comp=''; S.text=''; S.minTotal=0; S.errKey=''; S.only=''; S.page=0;
}

function act(a,v){
  switch(a){
    case 'quick': S.quick=(S.quick===v&&v!=='all')?'all':v; S.page=0; break;
    case 'sort':
      if(S.sortCol===v) S.sortDir*=-1; else { S.sortCol=v; S.sortDir=(v==='file'||v==='sender'||v==='receiver'||v==='status')?1:-1; }
      break;
    case 'toggle': { const j=DATA.jobs[+v]; if(S.open.has(j.id)) S.open.delete(j.id); else S.open.add(j.id); break; }
    case 'comp': S.comp=(S.comp===v?'':v); S.page=0; break;
    case 'err': S.errKey=(S.errKey===v?'':v); S.page=0; break;
    case 'grp': { const key=(S.lbGroup==='component'?'comp':S.lbGroup); S[key]=(S[key]===v?'':v); S.page=0; break; }
    case 'lbsort': if(S.lbKey===v) S.lbDir*=-1; else { S.lbKey=v; S.lbDir=(v==='name')?1:-1; } break;
    case 'pick': { const j=DATA.jobs[+v]; S.only=j.id; S.open.add(j.id); S.page=0; break; }
    case 'page': S.page+=(+v); break;
    case 'clear': resetFilters(); break;
    case 'rm': {
      const map={quick:()=>S.quick='all',status:()=>S.status='',sender:()=>S.sender='',receiver:()=>S.receiver='',comp:()=>S.comp='',err:()=>S.errKey='',only:()=>S.only='',text:()=>S.text='',min:()=>S.minTotal=0};
      if(map[v]) map[v](); S.page=0; break;
    }
    default: return;
  }
  renderAll();
}
document.addEventListener('click',e=>{
  const t=e.target.closest('[data-act]'); if(!t) return;
  if(t.disabled) return;
  act(t.dataset.act,t.dataset.v);
});
document.addEventListener('change',e=>{
  const t=e.target, d=t.dataset||{};
  if(d.sel){
    const num=['topN','lbN','pageSize'].indexOf(d.sel)>=0;
    S[d.sel]=num?+t.value:t.value;
    if(d.sel==='lbGroup'){ S.lbKey=''; }
    if(d.sel==='pageSize') S.page=0;
    renderAll();
  } else if(d.panel){ S.hidden[d.panel]=!t.checked; renderAll(); }
  else if(d.col){ S.cols[d.col]=t.checked; renderAll(); }
});

function uniq(f){ return [...new Set(DATA.jobs.map(f))].sort((a,b)=>String(a).localeCompare(String(b))); }
function fillSelect(id,label,vals){ $(id).innerHTML='<option value="">'+label+'</option>'+vals.map(v=>'<option value="'+esc(v)+'">'+esc(v)+'</option>').join(''); }
fillSelect('fStatus','All',uniq(j=>j.status));
fillSelect('fSender','All',uniq(j=>j.sender));
fillSelect('fReceiver','All',uniq(j=>j.receiver));
fillSelect('fComp','All',[...new Set([].concat.apply([],DATA.jobs.map(j=>j.steps.map(s=>s.n))))].sort());
$('fStatus').onchange=e=>{ S.status=e.target.value; S.page=0; renderAll(); };
$('fSender').onchange=e=>{ S.sender=e.target.value; S.page=0; renderAll(); };
$('fReceiver').onchange=e=>{ S.receiver=e.target.value; S.page=0; renderAll(); };
$('fComp').onchange=e=>{ S.comp=e.target.value; S.page=0; renderAll(); };
$('minT').oninput=e=>{ S.minTotal=parseFloat(e.target.value)||0; S.page=0; renderAll(); };
let tmr=null;
$('fText').oninput=e=>{ const v=e.target.value; clearTimeout(tmr); tmr=setTimeout(()=>{ S.text=v.trim(); S.page=0; renderAll(); },150); };

$('stepT').value=S.stepT; $('jobT').value=S.jobT; $('keyC').value=CFG.key.join(', ');
$('stepT').oninput=e=>{ const v=parseFloat(e.target.value); if(isNaN(v)) return; S.stepT=v; enrich(); renderAll(); };
$('jobT').oninput=e=>{ const v=parseFloat(e.target.value); if(isNaN(v)) return; S.jobT=v; enrich(); renderAll(); };
$('keyC').onchange=e=>{ S.key=new Set(e.target.value.split(',').map(x=>x.trim()).filter(Boolean)); enrich(); renderAll(); };
$('btnReset').onclick=()=>{ S.stepT=CFG.step; S.jobT=CFG.job; S.key=new Set(CFG.key); $('stepT').value=S.stepT; $('jobT').value=S.jobT; $('keyC').value=CFG.key.join(', '); enrich(); renderAll(); };
$('btnP').onclick=()=>window.print();

document.querySelectorAll('[data-panel]').forEach(c=>{ c.checked=!S.hidden[c.dataset.panel]; });
document.querySelectorAll('[data-col]').forEach(c=>{ c.checked=!!S.cols[c.dataset.col]; });

function exportData(){
  const list=sortedJobs();
  const maxSteps=Math.max.apply(null,[0].concat(list.map(j=>j.steps.length)));
  const hdr=['JobID','Filename','Status','Total_Sec','Sender','Receiver','Created','Long_Job','Queue_Wait_Steps','Slow_Process_Steps','Error_Steps','Analysis'];
  for(let i=1;i<=maxSteps;i++) hdr.push('Step_'+i);
  const jobs=list.map(j=>{
    const o={JobID:j.id,Filename:j.file,Status:j.status,Total_Sec:Math.round(j.total*100)/100,Sender:j.sender,Receiver:j.receiver,Created:j.created,Long_Job:j.longJob?'Y':'',Queue_Wait_Steps:j.nq,Slow_Process_Steps:j.np,Error_Steps:j.ne,Analysis:analysisText(j)};
    j.steps.forEach((s,i)=>{ o['Step_'+(i+1)]=s.n+' | process '+f2(s.pr)+'s | wait '+f2(s.w)+'s'+(s.err?' | ERROR: '+s.e:''); });
    return o;
  });
  const steps=[];
  list.forEach(j=>j.steps.forEach((s,i)=>steps.push({JobID:j.id,Filename:j.file,Step:i+1,Component:s.n,Wait_Sec:s.w,Process_Sec:s.pr,Error:s.e,Last_Message:s.lm})));
  return {hdr:hdr,jobs:jobs,steps:steps};
}
$('btnX').onclick=()=>{
  const d=exportData(), name=DATA.meta.excel_name;
  if(window.XLSX){
    const wb=XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb,XLSX.utils.json_to_sheet(d.jobs,{header:d.hdr}),'Jobs');
    XLSX.utils.book_append_sheet(wb,XLSX.utils.json_to_sheet(d.steps),'Steps');
    XLSX.writeFile(wb,name+'.xlsx');
  } else {
    const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
    const csv=[d.hdr.map(q).join(',')].concat(d.jobs.map(r=>d.hdr.map(h=>q(r[h])).join(','))).join('\r\n');
    const a=document.createElement('a');
    a.href=URL.createObjectURL(new Blob(['\uFEFF'+csv],{type:'text/csv'})); a.download=name+'.csv';
    document.body.appendChild(a); a.click(); a.remove();
    alert('Excel library could not be loaded (offline?). Downloaded CSV instead.');
  }
};

$('sub').textContent='Generated '+DATA.meta.generated+' \u00B7 '+DATA.jobs.length+' jobs from '+DATA.meta.source;
if(DATA.fetch_errors.length){
  $('banner').innerHTML='<div class="banner"><b>'+DATA.fetch_errors.length+' job(s) could not be retrieved</b> and are not counted below.<details><summary>Show details</summary>'
    +DATA.fetch_errors.map(e=>'<div style="margin-top:4px;word-break:break-all"><code>'+esc(e.id)+'</code><br><span class="red">'+esc(e.reason)+'</span></div>').join('')+'</details></div>';
}

enrich();
renderAll();
})();
</script>
</body>
</html>
'''


if __name__ == '__main__':
    sys.exit(main(sys.argv))
