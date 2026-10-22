// Run:  k6 run -e PROFILE=smoke  loadtest.js
//       k6 run -e PROFILE=load   loadtest.js
//       k6 run -e PROFILE=stress loadtest.js
// Output: console error logs + report.html
//
// Request bodies are inside the OPS table below.
// Each request has its own CSV in data/. {{columnName}} inside a body is filled from a random row of that CSV.

import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import { Trend, Counter } from 'k6/metrics';

// ---------- EDIT THESE ----------
const URL = 'https://qa.nonprod.int.coke.com/graphql';
const HEADERS = {
  'content-type': 'application/json',
  authorization: 'Bearer PASTE_TOKEN_HERE',
  cookie: 'PASTE_COOKIE_HERE',
  origin: 'https://qa.nonprod.int.coke.com',
  referer: 'https://qa.nonprod.int.coke.com/jobs',
};

// Each request: weight = share of total traffic (0 = skip), p95 = threshold in ms,
// csv = that request's data file (omit if the body has no {{placeholders}}),
// body = paste the raw Postman body. {{columnName}} is filled from a row of the csv.
const OPS = {
  GetJobs: { weight: 40, p95: 2000, csv: 'data/getJobs.csv', body: String.raw`{
  "operationName": "GetJobs",
  "query": "query GetJobs($count: Int, $cursor: String, $filterConfig: JobFilterConfigInput) { jobs(first: $count, after: $cursor, filterConfig: $filterConfig) { pageInfo { hasNextPage } edges { cursor node { id name status } } } }",
  "variables": {
    "count": 10,
    "filterConfig": { "status": "{{status}}", "fromDate": {{fromDate}}, "toDate": {{toDate}} }
  }
}` },

  GetPendingBatchJobs: { weight: 10, p95: 5000, csv: 'data/getPendingBatchJobs.csv', body: String.raw`{
  "operationName": "GetPendingBatchJobs",
  "query": "query GetPendingBatchJobs($senderId: String) { __typename }",
  "variables": { "senderId": "{{senderId}}" }
}` },

  CurrentUser: { weight: 10, p95: 500, body: String.raw`{
  "operationName": "CurrentUser",
  "query": "query CurrentUser { __typename }",
  "variables": {}
}` },

  DataSystemsSearch: { weight: 10, p95: 2000, csv: 'data/dataSystemsSearch.csv', body: String.raw`{
  "operationName": "DataSystemsSearch",
  "query": "query DataSystemsSearch($count: Int, $cursor: String, $filterConfig: DataSystemFilterConfigInput) { dataSystems(first: $count, after: $cursor, filterConfig: $filterConfig) { pageInfo { hasNextPage } edges { cursor node { id name } } } }",
  "variables": {
    "count": 10,
    "filterConfig": { "searchText": "{{searchText}}" }
  }
}` },

  GetJobDetails: { weight: 15, p95: 2000, csv: 'data/getJobDetails.csv', body: String.raw`{
  "operationName": "GetJobDetails",
  "query": "query GetJobDetails($jobId: String!) { __typename }",
  "variables": { "jobId": "{{jobId}}" }
}` },

  GetSenders: { weight: 10, p95: 2000, csv: 'data/getSenders.csv', body: String.raw`{
  "operationName": "GetSenders",
  "query": "query GetSenders($senderId: String, $searchText: String) { __typename }",
  "variables": { "senderId": "{{senderId}}", "searchText": "{{searchText}}" }
}` },

  GetJobStatusCounts: { weight: 5, p95: 3000, csv: 'data/getJobStatusCounts.csv', body: String.raw`{
  "operationName": "GetJobStatusCounts",
  "query": "query GetJobStatusCounts($fromDate: Int, $toDate: Int) { __typename }",
  "variables": { "fromDate": {{fromDate}}, "toDate": {{toDate}} }
}` },
};

// Phases run one after another. tps = TOTAL requests/sec across all operations
// (split by weight), secs = how long the phase lasts.
const PROFILES = {
  smoke: [{ tps: 1, secs: 30 }],
  load: [{ tps: 5, secs: 120 }, { tps: 20, secs: 300 }],
  stress: [{ tps: 5, secs: 120 }, { tps: 20, secs: 120 }, { tps: 30, secs: 120 }],
};
// --------------------------------

const PROFILE = __ENV.PROFILE || 'smoke';
const PHASES = PROFILES[PROFILE].map((p) => ({ ...p, name: p.tps + 'tps' }));
const NAMES = Object.keys(OPS).filter((n) => OPS[n].weight > 0);
const TOTAL_W = NAMES.reduce((s, n) => s + OPS[n].weight, 0);

// test data: one CSV per request, first row = column names.
// Rows with any blank cell are ignored so a blank value never goes into a request.
const DATA = {};
for (const n of NAMES) {
  DATA[n] = new SharedArray('csv_' + n, () => {
    if (!OPS[n].csv) return [{}];
    const lines = open('./' + OPS[n].csv).trim().split(/\r?\n/).filter((l) => l.trim());
    const head = lines[0].split(',').map((h) => h.trim());
    const rows = lines.slice(1).map((l) => {
      const c = l.split(',');
      const o = {};
      head.forEach((h, i) => (o[h] = (c[i] || '').trim()));
      return o;
    }).filter((o) => Object.values(o).every((v) => v !== ''));
    if (!rows.length) throw new Error(OPS[n].csv + ' has no complete rows');
    return rows;
  });
}

const dur = new Trend('dur', true);
const total = new Counter('total');
const failed = new Counter('failed');

// ---- scenarios: one per phase x operation (rate set by weight) ----
const scenarios = {};
const thresholds = {};
const ACTIVE = []; // phase x op combos that actually run
let start = 0;
for (const ph of PHASES) {
  for (const op of NAMES) {
    const perSec = (ph.tps * OPS[op].weight) / TOTAL_W;
    const rate = Math.round(perSec * 60); // per minute keeps small rates precise
    if (rate < 1) continue;
    scenarios[ph.name + '_' + op] = {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '60s',
      duration: ph.secs + 's',
      startTime: start + 's',
      preAllocatedVUs: Math.max(5, Math.ceil(perSec * 3)),
      maxVUs: Math.max(200, Math.ceil(perSec * 120)), // room for responses up to ~2 min without dropping any request
      gracefulStop: '10m', // let in-flight requests finish after the phase ends, never cut them
      exec: op,
      tags: { phase: ph.name, op },
    };
    ACTIVE.push({ ph, op, perSec });
    const tag = `{phase:${ph.name},op:${op}}`;
    thresholds['dur' + tag] = ['p(95)<' + OPS[op].p95];
    thresholds['total' + tag] = ['count>=0'];
    thresholds['failed' + tag] = ['count>=0'];
  }
  start += ph.secs;
}

export const options = {
  scenarios,
  thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

// ---- the seven functions ----
export function GetJobs() { call('GetJobs'); }
export function GetPendingBatchJobs() { call('GetPendingBatchJobs'); }
export function CurrentUser() { call('CurrentUser'); }
export function DataSystemsSearch() { call('DataSystemsSearch'); }
export function GetJobDetails() { call('GetJobDetails'); }
export function GetSenders() { call('GetSenders'); }
export function GetJobStatusCounts() { call('GetJobStatusCounts'); }

function buildBody(op) {
  const rows = DATA[op];
  const row = rows[Math.floor(Math.random() * rows.length)];
  return OPS[op].body.replace(/\{\{(\w+)\}\}/g, (m, k) => (k in row ? row[k] : m));
}

const short = (x) => String(x == null ? '' : x).replace(/\s+/g, ' ').slice(0, 120);

// pull a readable message out of whatever the server sent back
function messageOf(res) {
  if (res.status === 0) return short(res.error || 'no response');
  try {
    const j = JSON.parse(res.body);
    if (j.errors && j.errors.length) {
      const e = j.errors[0];
      const code = e.extensions && e.extensions.code ? ' [' + e.extensions.code + ']' : '';
      return short(e.message) + code;
    }
    if (j.message) return short(j.message);
    if (j.error) return short(typeof j.error === 'string' ? j.error : JSON.stringify(j.error));
  } catch (e) {}
  return short(res.body);
}

function call(op) {
  const res = http.post(URL, buildBody(op), { headers: HEADERS, tags: { name: op }, timeout: '10m' });
  const ms = res.timings.duration;
  dur.add(ms);
  total.add(1);

  let status = null;
  if (res.status === 0 || res.status >= 400) {
    status = String(res.status);
  } else {
    try {
      if (JSON.parse(res.body).errors) status = '200 (GraphQL error)';
    } catch (e) {
      status = '200 (invalid JSON)';
    }
  }
  if (status) {
    failed.add(1);
    const msg = messageOf(res);
    // every distinct phase|op|status|message becomes its own counted entry in the report
    const phase = exec.scenario.name.split('_')[0];
    check(res, { ['ERR|' + phase + '|' + op + '|' + status + '|' + msg]: () => false });
    console.error(`[ERROR] phase=${phase} op=${op} status=${res.status} time=${Math.round(ms)}ms message=${msg}`);
    // 401 = token expired, nothing after this will work: stop the test, summary is still generated
    if (res.status === 401) {
      exec.test.abort('Token expired (HTTP 401). Test aborted, results up to this point are in report.html');
    }
  }
}

// ---- HTML report ----
const fmt = (ms) => (ms == null ? '-' : ms >= 1000 ? (ms / 1000).toFixed(2) + ' s' : Math.round(ms) + ' ms');
const val = (m, k) => (m && m.values ? m.values[k] : undefined);

export function handleSummary(data) {
  const M = data.metrics;
  const rows = [];
  let allTotal = 0, allFailed = 0;
  const opAgg = {};
  for (const { ph, op } of ACTIVE) {
    const tag = `{phase:${ph.name},op:${op}}`;
    const d = M['dur' + tag];
    const t = val(M['total' + tag], 'count') || 0;
    const f = val(M['failed' + tag], 'count') || 0;
    allTotal += t; allFailed += f;
    opAgg[op] = opAgg[op] || { t: 0, f: 0 };
    opAgg[op].t += t; opAgg[op].f += f;
    const pct = t ? (f / t) * 100 : 0;
    const w = ((OPS[op].weight / TOTAL_W) * 100).toFixed(0);
    rows.push(`<tr class="${pct >= 5 ? 'hot' : ''}"><td class="l">${ph.name}</td><td class="l">${op}</td><td>${w}%</td><td>${t}</td><td class="ok">${t - f}</td><td class="${f ? 'bad' : ''}">${f}</td><td>${pct.toFixed(1)}%</td><td>${fmt(val(d, 'avg'))}</td><td>${fmt(val(d, 'p(90)'))}</td><td>${fmt(val(d, 'p(95)'))}</td><td>${fmt(val(d, 'p(99)'))}</td><td>${fmt(val(d, 'max'))}</td></tr>`);
  }

  // errors: collected from the dynamic check names written in call()
  const found = [];
  const walk = (g) => {
    (g.checks || []).forEach((c) => c.name.startsWith('ERR|') && found.push(c));
    (g.groups || []).forEach(walk);
  };
  walk(data.root_group);
  found.sort((x, y) => y.fails - x.fails);
  const errRows = found.map((c) => {
    const p = c.name.split('|');
    const [phase, op, status] = [p[1], p[2], p[3]];
    const msg = p.slice(4).join('|').replace(/</g, '&lt;');
    return `<tr><td class="l">${phase}</td><td class="l">${op}</td><td class="l">${status}</td><td class="bad">${c.fails}</td><td class="l" style="white-space:normal;word-break:break-word">${msg}</td></tr>`;
  });

  const statusAgg = {};
  found.forEach((c) => { const st = c.name.split('|')[3]; statusAgg[st] = (statusAgg[st] || 0) + c.fails; });
  const aborted = statusAgg['401'] > 0;
  const durMs = (data.state && data.state.testRunDurationMs) || 0;
  const dropped = val(M['dropped_iterations'], 'count') || 0;
  const endAt = new Date();
  const startAt = new Date(endAt.getTime() - durMs);
  const stamp = (d) => d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
  const dsec = Math.round(durMs / 1000);
  const durTxt = `${Math.floor(dsec / 3600)}h ${Math.floor((dsec % 3600) / 60)}m ${dsec % 60}s`;

  const funcRows = NAMES.map((op) => {
    const a = opAgg[op] || { t: 0, f: 0 };
    return `<tr><td class="l">${op}</td><td>${((OPS[op].weight / TOTAL_W) * 100).toFixed(0)}%</td><td>${a.t}</td><td class="ok">${a.t - a.f}</td><td class="${a.f ? 'bad' : ''}">${a.f}</td></tr>`;
  });

  const bars = [['Success', allTotal - allFailed, 'okb']].concat(Object.keys(statusAgg).sort((x, y) => statusAgg[y] - statusAgg[x]).map((k) => [k, statusAgg[k], '']));
  const maxBar = Math.max(1, ...bars.map((b) => b[1]));
  const chartHtml = bars.map((b) => `<div class="barrow"><div class="barlbl">${b[0]}</div><div class="bar ${b[2]}" style="width:${Math.max(1, (b[1] / maxBar) * 100)}%"></div><div class="barcnt">${b[1]}</div></div>`).join('');

  const thRows = [];
  let thFail = 0;
  for (const k of Object.keys(M)) {
    const th = M[k].thresholds;
    if (!th || !k.startsWith('dur{')) continue;
    for (const expr of Object.keys(th)) {
      const ok = typeof th[expr] === 'object' ? th[expr].ok : !th[expr];
      if (!ok) thFail++;
      thRows.push(`<tr><td class="l"><b class="${ok ? 'ok' : 'bad'}">${ok ? 'PASS' : 'FAIL'}</b></td><td class="l">${k}</td><td class="l">${expr}</td></tr>`);
    }
  }

  const all = M['dur'];
  const rate = allTotal ? ((allFailed / allTotal) * 100).toFixed(1) : '0.0';
  const card = (l, v, c) => `<div class="card"><div class="lbl">${l}</div><div class="val ${c || ''}">${v}</div></div>`;
  const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Load test report ${PROFILE}</title>
<style>
:root{--bg:#fff;--fg:#1a1a19;--mut:#6b6a66;--line:#e1e0d9;--card:#f6f5f1;--ok:#2d7a2d;--bad:#c23a3a;--hot:#fdeeee}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--fg:#f0efec;--mut:#a3a29a;--line:#383835;--card:#262624;--ok:#5fc15f;--bad:#f07272;--hot:#3a2222}}
body{font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:20px;max-width:1150px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}.sub{color:var(--mut);font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:14px 0}
.card{background:var(--card);border-radius:8px;padding:10px}.lbl{font-size:12px;color:var(--mut)}.val{font-size:20px;font-weight:500}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{color:var(--mut);font-weight:400}
.l{text-align:left}.ok{color:var(--ok)}.bad{color:var(--bad)}tr.hot{background:var(--hot)}
.banner{display:inline-block;padding:3px 10px;border-radius:6px;font-size:12px;font-weight:500;background:var(--hot);color:var(--bad)}
.banner.pass{background:var(--card);color:var(--ok)}
.val.sm{font-size:13px}.barrow{display:flex;align-items:center;gap:8px;margin:5px 0}.barlbl{width:170px;font-size:12px;flex-shrink:0}.bar{height:14px;border-radius:3px;background:var(--bad)}.bar.okb{background:var(--ok)}.barcnt{font-size:12px;color:var(--mut)}
</style></head><body>
<h1>Load test report <span class="banner ${thFail ? '' : 'pass'}">${thFail ? thFail + ' threshold(s) failed' : 'all thresholds passed'}</span></h1>
${aborted ? '<div class="banner" style="margin:8px 0">TEST ABORTED: token expired (HTTP 401). Results below cover the test up to that point.</div>' : ''}
<div class="sub">profile: ${PROFILE} | phases: ${PHASES.map((p) => p.name + ' x ' + p.secs + 's').join(', ')} | ${URL}</div>
<h2>Executive summary</h2>
<div class="cards">${card('Started', stamp(startAt), 'sm')}${card('Ended', stamp(endAt), 'sm')}${card('Duration', durTxt)}${card('Transactions', allTotal)}${card('Pass', allTotal - allFailed)}${card('Fail', allFailed, allFailed ? 'bad' : '')}${card('Error rate', rate + '%', allFailed ? 'bad' : '')}</div>
<div class="cards">${card('Avg', fmt(val(all, 'avg')))}${card('p90', fmt(val(all, 'p(90)')))}${card('p95', fmt(val(all, 'p(95)')))}${card('p99', fmt(val(all, 'p(99)')))}${card('Max', fmt(val(all, 'max')))}${card('Dropped', dropped, dropped ? 'bad' : '')}</div>
${dropped ? '<div class="sub">Dropped = requests k6 could not send because all virtual users were busy (server too slow). Actual load was lower than planned.</div>' : ''}
<div class="wrap"><table><tr><th class="l">Function tested</th><th>Weight</th><th>Requests</th><th>Pass</th><th>Fail</th></tr>${funcRows.join('')}</table></div>
<h2>Per phase and operation</h2><div class="wrap"><table>
<tr><th class="l">Phase</th><th class="l">Operation</th><th>Weight</th><th>Requests</th><th>Pass</th><th>Fail</th><th>Fail %</th><th>Avg</th><th>p90</th><th>p95</th><th>p99</th><th>Max</th></tr>${rows.join('')}</table></div>
<h2>Errors</h2><div class="sub">Actual status and message from the response (status 0 = no response). Response time per error is in the console log.</div><div class="wrap"><table>
<tr><th class="l">Phase</th><th class="l">Operation</th><th class="l">Status</th><th>Count</th><th class="l">Message</th></tr>${errRows.join('') || '<tr><td class="l" colspan="5">No errors</td></tr>'}</table></div>
<h2>Thresholds</h2><div class="wrap"><table><tr><th class="l">Result</th><th class="l">Metric</th><th class="l">Condition</th></tr>${thRows.join('')}</table></div>
<h2>Status code distribution</h2>${chartHtml}
</body></html>`;

  const lines = [
    '',
    '================ TEST SUMMARY ================',
    aborted ? '*** ABORTED: token expired (HTTP 401). Test stopped early, results below are up to that point. ***' : '',
    `Started : ${stamp(startAt)}`,
    `Ended   : ${stamp(endAt)}`,
    `Duration: ${durTxt}`,
    `Requests: ${allTotal}   Pass: ${allTotal - allFailed}   Fail: ${allFailed} (${rate}%)`,
    `Dropped : ${dropped}${dropped ? '  (k6 ran out of virtual users, actual load was lower than planned)' : ''}`,
    `Latency : max ${fmt(val(all, 'max'))}  avg ${fmt(val(all, 'avg'))}  p90 ${fmt(val(all, 'p(90)'))}  p95 ${fmt(val(all, 'p(95)'))}  p99 ${fmt(val(all, 'p(99)'))}`,
    '',
    'Per function:',
    ...NAMES.map((op) => { const a = opAgg[op] || { t: 0, f: 0 }; return `  ${op.padEnd(22)} requests=${a.t} fail=${a.f}`; }),
    '',
    'Status codes (failures):',
    ...(Object.keys(statusAgg).length ? Object.keys(statusAgg).map((k) => `  ${k}: ${statusAgg[k]}`) : ['  none']),
    `Thresholds failed: ${thFail}`,
    'Report: report.html',
    '==============================================',
    '',
  ];
  return {
    'report.html': html,
    stdout: lines.join('\n'),
  };
}
