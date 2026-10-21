/*
  GraphQL load test. Settings come from params.json, queries from queries.js, token from env or token.txt.

  Smoke test (1 user, 30s):   k6 run -e BASE_URL=https://host/graphql -e TOKEN=xxx -e LOAD=1 -e HOLD=30s load-test.js
  50 users:                   k6 run -e BASE_URL=... -e TOKEN=xxx -e LOAD=50  --log-output=file=errors-vus-50.log  load-test.js
  200 users:                  k6 run -e BASE_URL=... -e TOKEN=xxx -e LOAD=200 --log-output=file=errors-vus-200.log load-test.js
  Per second instead of users: add  -e MODE=rate   (LOAD = new user journeys started per second)
  One activity only:          add  -e ONLY=scroll   (pageLoad scroll filter search jobDetails batchPanel dropdown)
  Typeahead without debounce: add  -e SEARCH_BURST=1
  Other options: -e PARAMS=other.json  -e TAG=myrun  -e HOLD=10m  -e RAMP_UP=2m  -e MAX_VUS=3000

  Outputs: console summary, report-<TAG>.html, summary-<TAG>.json, errors-<TAG>.txt
  Optional: node add-error-descriptions.js errors-<TAG>.log report-<TAG>.html   (adds actual error messages to the report)
*/
import http from 'k6/http';
import { sleep } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.0.2/index.js';
import { Q } from './queries.js';

// ---------- Settings ----------
const URL = __ENV.BASE_URL;
if (!URL) throw new Error('Set -e BASE_URL=https://host/graphql');
const TOKEN = __ENV.TOKEN || open('./token.txt').trim();
const LOAD = parseInt(__ENV.LOAD || '50');
const MODE = __ENV.MODE || 'vus'; // vus = concurrent users, rate = user journeys started per second
const ONLY = __ENV.ONLY || '';
const TAG = __ENV.TAG || `${MODE}-${LOAD}${ONLY ? '-' + ONLY : ''}`;
const P = new SharedArray('params', () => [JSON.parse(open(__ENV.PARAMS || './params.json'))])[0];
const D = {
  rampUp: __ENV.RAMP_UP || P.durations.rampUp,
  hold: __ENV.HOLD || P.durations.hold,
  rampDown: __ENV.RAMP_DOWN || P.durations.rampDown,
};
const TIMEOUT = `${P.timeoutSeconds}s`;

// ---------- Scenarios (one per activity, sized by the mix in params.json) ----------
const scenarios = {};
Object.keys(P.mix).forEach((name) => {
  if (ONLY && ONLY !== name) return;
  const target = Math.max(1, Math.round(ONLY ? LOAD : (LOAD * P.mix[name]) / 100));
  const stages = [
    { duration: D.rampUp, target },
    { duration: D.hold, target },
    { duration: D.rampDown, target: 0 },
  ];
  scenarios[name] = MODE === 'rate'
    ? { executor: 'ramping-arrival-rate', exec: name, startRate: 0, timeUnit: '1s', preAllocatedVUs: target * 5,
        maxVUs: parseInt(__ENV.MAX_VUS || String(target * 40)), stages }
    : { executor: 'ramping-vus', exec: name, startVUs: 0, stages, gracefulRampDown: '30s' };
});
if (!Object.keys(scenarios).length) throw new Error(`Unknown ONLY="${ONLY}". Use a key from "mix" in params.json`);

const thresholds = { http_req_failed: [`rate<${P.maxErrorRate}`], graphql_errors: [`rate<${P.maxErrorRate}`] };
Object.keys(P.thresholdsMs).forEach((op) => { thresholds[`dur_${op}`] = [`p(95)<${P.thresholdsMs[op]}`]; });

export const options = {
  scenarios,
  thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

// ---------- Metrics: per operation requests, latency, size, errors by type; HTTP status counts ----------
const OPS = ['GetJobs', 'GetPendingBatchJobs', 'GetJobDetails', 'DataTypesDropdown', 'DataSystemsDropdown', 'DataSystemsSearch', 'CurrentUser'];
const ERR_TYPES = ['timeout_or_network', 'auth_401_403', 'throttled_429', 'server_5xx', 'other_http', 'graphql_error', 'empty_data'];
const STATUS = [0, 200, 400, 401, 403, 404, 408, 429, 500, 502, 503, 504];
const reqCount = {}, dur = {}, size = {}, errCount = {}, statusCount = {};
OPS.forEach((op) => {
  reqCount[op] = new Counter(`req_${op}`);
  dur[op] = new Trend(`dur_${op}`, true);
  size[op] = new Trend(`size_${op}`);
  ERR_TYPES.forEach((t) => (errCount[`${op}|${t}`] = new Counter(`err_${op}_${t}`)));
});
STATUS.concat(['other']).forEach((s) => (statusCount[s] = new Counter(`status_${s}`)));
const pageLoadAll = new Trend('dur_PageLoad_All', true);
const gqlErrors = new Rate('graphql_errors');

// ---------- Random helpers (use these in your own activities) ----------
const rand = (min, max) => min + Math.random() * (max - min);                       // decimal
const randInt = (min, max) => min + Math.floor(Math.random() * (max - min + 1));    // whole number, inclusive
const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];                  // random item
const chance = (p) => Math.random() < p;                                            // true with probability p (0 to 1)
const pickWeighted = (items) => {                                                   // items: [{ value, weight }]
  let r = Math.random() * items.reduce((s, i) => s + i.weight, 0);
  for (const i of items) { r -= i.weight; if (r <= 0) return i.value; }
  return items[items.length - 1].value;
};
const randomString = (n) => { let s = ''; for (let i = 0; i < n; i++) s += 'abcdefghijklmnopqrstuvwxyz'[randInt(0, 25)]; return s; };
const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => { const r = randInt(0, 15); return (c === 'x' ? r : (r & 3) | 8).toString(16); });
const randomDateRange = (days) => {                                                 // { from, to } as YYYY-MM-DD
  const to = new Date(Date.now() - randInt(0, 30) * 86400000);
  const from = new Date(to.getTime() - days * 86400000);
  return { from: from.toISOString().slice(0, 10), to: to.toISOString().slice(0, 10) };
};
const typedPrefixes = (word) => { const out = []; for (let i = 1; i <= word.length; i++) out.push(word.slice(0, i)); return out; };
const think = (r) => sleep(rand(r.min, r.max));

// ---------- Request helpers with full error capture ----------
const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` };
let logged = 0; // per-VU count of logged failures

function classify(res, body) {
  if (res.status === 0) return 'timeout_or_network';
  if (res.status === 401 || res.status === 403) return 'auth_401_403';
  if (res.status === 429) return 'throttled_429';
  if (res.status >= 500) return 'server_5xx';
  if (res.status !== 200) return 'other_http';
  if (body && body.errors) return 'graphql_error'; // GraphQL returns 200 even when it fails
  if (!body || !body.data) return 'empty_data';
  return null;
}

function track(op, res, variables) {
  reqCount[op].add(1);
  dur[op].add(res.timings.duration);
  size[op].add(res.body ? res.body.length : 0);
  statusCount[STATUS.indexOf(res.status) >= 0 ? res.status : 'other'].add(1);
  let body = null;
  try { body = res.json(); } catch (e) { /* not JSON */ }
  const type = classify(res, body);
  gqlErrors.add(type !== null);
  if (type) {
    errCount[`${op}|${type}`].add(1);
    if (logged++ < P.maxErrorLogsPerVU) {
      const e0 = body && body.errors && body.errors[0];
      console.error(JSON.stringify({
        time: new Date().toISOString(), operation: op, type, status: res.status,
        error_code: res.error_code || undefined, error: res.error || undefined,
        duration_ms: Math.round(res.timings.duration), waiting_ms: Math.round(res.timings.waiting || 0),
        size_bytes: res.body ? res.body.length : 0,
        gql_message: e0 ? String(e0.message).slice(0, 300) : undefined,
        gql_code: e0 && e0.extensions ? e0.extensions.code : undefined,
        retry_after: res.headers['Retry-After'] || res.headers['X-Ms-Retry-After-Ms'] || undefined,
        request_id: res.headers['Request-Id'] || res.headers['X-Request-Id'] || res.headers['Apim-Request-Id'] || undefined,
        variables: variables ? JSON.stringify(variables).slice(0, 200) : undefined,
        body_snippet: !body && res.body ? String(res.body).slice(0, 200) : undefined,
      }));
    }
  }
  return body;
}

const call = (op, query, variables) =>
  track(op, http.post(URL, JSON.stringify({ query, variables }), { headers, tags: { operation: op }, timeout: TIMEOUT }), variables);
const req = (op, query, variables) =>
  ['POST', URL, JSON.stringify({ query, variables }), { headers, tags: { operation: op }, timeout: TIMEOUT }];

// ---------- Data helpers ----------
function jobFilter() {
  const f = Object.assign({}, pick(P.jobFilters));
  if (P.dateFields && P.dateFields.from && P.dateFields.to) { // set the real field names in params.json to enable
    const r = randomDateRange(pick(P.dateRangesDays));
    f[P.dateFields.from] = r.from;
    f[P.dateFields.to] = r.to;
  }
  return f;
}
const edgesOf = (b) => (b && b.data && b.data.jobs && b.data.jobs.edges) || [];
const nextCursor = (b) => {
  const e = edgesOf(b);
  const more = b && b.data && b.data.jobs && b.data.jobs.pageInfo && b.data.jobs.pageInfo.hasNextPage;
  return e.length && more ? e[e.length - 1].cursor : null;
};

// ---------- Activities (names must match "mix" in params.json) ----------
export function pageLoad() { // the calls the page fires together on load
  const t0 = Date.now();
  const defs = [
    ['GetJobs', Q.getJobs, { count: P.pageSize, filterConfig: pick(P.jobFilters) }],
    ['GetPendingBatchJobs', Q.pendingBatchJobs, { first: P.batchPageSize, offset: 0 }],
    ['DataTypesDropdown', Q.dataTypes, { count: 10 }],
    ['DataSystemsDropdown', Q.dataSystems, { count: 10 }],
    ['CurrentUser', Q.currentUser, {}],
  ];
  const rs = http.batch(defs.map((d) => req(d[0], d[1], d[2])));
  rs.forEach((r, i) => track(defs[i][0], r, defs[i][2]));
  pageLoadAll.add(Date.now() - t0);
  think(P.think);
}

export function scroll() { // infinite scroll with the cursor
  const f = jobFilter();
  let b = call('GetJobs', Q.getJobs, { count: P.pageSize, filterConfig: f });
  let cursor = nextCursor(b);
  const pages = randInt(P.scrollPages.min, P.scrollPages.max);
  for (let i = 0; i < pages && cursor; i++) {
    b = call('GetJobs', Q.getJobs, { count: P.pageSize, cursor, filterConfig: f });
    cursor = nextCursor(b);
    think(P.scrollThink);
  }
  think(P.think);
}

export function filter() {
  call('GetJobs', Q.getJobs, { count: P.pageSize, filterConfig: jobFilter() });
  think(P.think);
}

export function search() { // typeahead: filterConfig { like: "<typed text>" }
  const terms = typedPrefixes(pick(P.searchWords));
  if (__ENV.SEARCH_BURST) { // worst case: no debounce, every keystroke in flight together
    const rs = http.batch(terms.map((t) => req('DataSystemsSearch', Q.dataSystems, { count: 10, filterConfig: { like: t } })));
    rs.forEach((r, i) => track('DataSystemsSearch', r, { like: terms[i] }));
  } else {
    terms.forEach((t) => {
      call('DataSystemsSearch', Q.dataSystems, { count: 10, filterConfig: { like: t } });
      sleep(rand(0.2, 0.5));
    });
  }
  think(P.think);
}

export function jobDetails() { // open a job: real IDs from params.json, or taken from a fresh job list
  let id = P.jobIds && P.jobIds.length ? pick(P.jobIds) : null;
  if (!id) {
    const e = edgesOf(call('GetJobs', Q.getJobs, { count: 20, filterConfig: jobFilter() }));
    id = e.length ? pick(e).node.id : null;
    think({ min: 0.5, max: 1.5 });
  }
  if (id) call('GetJobDetails', Q.jobDetails, { id, logCount: pick(P.logCounts), asc: chance(0.5) });
  think(P.think);
}

export function batchPanel() {
  call('GetPendingBatchJobs', Q.pendingBatchJobs, { first: P.batchPageSize, offset: pick(P.batchOffsets) });
  think(P.think);
}

export function dropdown() {
  if (chance(0.5)) call('DataTypesDropdown', Q.dataTypes, { count: 10 });
  else call('DataSystemsDropdown', Q.dataSystems, { count: 10 });
  think(P.think);
}

// ---------- Reports ----------
const val = (d, n, k) => (d.metrics[n] && d.metrics[n].values[k] !== undefined ? d.metrics[n].values[k] : undefined);
const count = (d, n) => val(d, n, 'count') || 0;
const esc = (x) => String(x).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const fmt = (v) => (v === undefined || v === null || isNaN(v) ? '-' : v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms');
const kb = (v) => (v === undefined || isNaN(v) ? '-' : (v / 1024).toFixed(1) + ' KB');

const ERR_HELP = {
  timeout_or_network: 'No response within the timeout, or connection failed (backend/gateway too slow, or network issue)',
  auth_401_403: 'Token rejected: expired token or missing permissions',
  throttled_429: 'Throttled: Cosmos DB RU limit exceeded or gateway rate limit hit',
  server_5xx: 'Server error: crash, or gateway 502/503/504 (backend unavailable or timed out)',
  other_http: 'Unexpected HTTP status, e.g. 400 (bad query) or 404',
  graphql_error: 'HTTP 200 but the response has GraphQL errors (resolver failure or validation error)',
  empty_data: 'HTTP 200 but no data (empty or non-JSON response)',
};

function opStats(d, op) {
  const reqs = count(d, `req_${op}`);
  const errs = ERR_TYPES.map((t) => [t, count(d, `err_${op}_${t}`)]).filter((e) => e[1] > 0);
  const fail = errs.reduce((s, e) => s + e[1], 0);
  return { op, reqs, fail, pass: reqs - fail, errs, avg: val(d, `dur_${op}`, 'avg'), p90: val(d, `dur_${op}`, 'p(90)'),
    p95: val(d, `dur_${op}`, 'p(95)'), p99: val(d, `dur_${op}`, 'p(99)'), max: val(d, `dur_${op}`, 'max'), kb: val(d, `size_${op}`, 'avg') };
}

function thresholdRows(d) {
  const rows = [];
  Object.keys(d.metrics).forEach((name) => {
    const th = d.metrics[name].thresholds;
    if (!th) return;
    Object.keys(th).forEach((expr) => { const v = th[expr]; rows.push({ name, expr, ok: v && typeof v === 'object' ? v.ok : !v }); });
  });
  return rows;
}

const statusLine = (d) => STATUS.concat(['other']).map((s) => [s, count(d, `status_${s}`)]).filter((e) => e[1] > 0).map((e) => `${e[0]}=${e[1]}`).join('  ');
const runInfo = () => `${MODE === 'rate' ? LOAD + ' journeys/s' : LOAD + ' users'} | ramp ${D.rampUp}, hold ${D.hold}, down ${D.rampDown}${ONLY ? ' | only: ' + ONLY : ''}`;

function consoleTable(d) {
  const pad = (x, n) => String(x).padEnd(n);
  const lines = [
    `RUN: ${runInfo()}`, '',
    pad('Operation', 21) + pad('Reqs', 8) + pad('Pass', 8) + pad('Fail', 7) + pad('Fail%', 7) + pad('Avg', 10) + pad('p90', 10) + pad('p95', 10) + pad('p99', 10) + pad('Max', 10) + pad('Size', 9) + 'Errors',
    '-'.repeat(130),
  ];
  OPS.forEach((op) => {
    const r = opStats(d, op);
    if (!r.reqs) return;
    lines.push(pad(op, 21) + pad(r.reqs, 8) + pad(r.pass, 8) + pad(r.fail, 7) + pad(((r.fail / r.reqs) * 100).toFixed(1) + '%', 7)
      + pad(fmt(r.avg), 10) + pad(fmt(r.p90), 10) + pad(fmt(r.p95), 10) + pad(fmt(r.p99), 10) + pad(fmt(r.max), 10) + pad(kb(r.kb), 9)
      + (r.errs.map((e) => `${e[0]}=${e[1]}`).join(', ') || 'none'));
  });
  lines.push('', `HTTP status counts: ${statusLine(d) || '-'}`, `Page load (slowest of parallel calls) avg ${fmt(val(d, 'dur_PageLoad_All', 'avg'))}, p95 ${fmt(val(d, 'dur_PageLoad_All', 'p(95)'))}`);
  const ths = thresholdRows(d);
  lines.push(`Thresholds: ${ths.filter((t) => t.ok).length} passed, ${ths.filter((t) => !t.ok).length} failed`);
  ths.filter((t) => !t.ok).forEach((t) => lines.push(`  FAILED ${t.name} ${t.expr}`));
  lines.push('', `Failure details: errors log (use request_id to find the request in App Insights).`);
  return lines.join('\n');
}

function htmlPage(d) {
  const rows = OPS.map((op) => opStats(d, op)).filter((r) => r.reqs > 0);
  const totReq = rows.reduce((s, r) => s + r.reqs, 0);
  const totFail = rows.reduce((s, r) => s + r.fail, 0);
  const ths = thresholdRows(d);
  const thFailed = ths.filter((t) => !t.ok).length;
  const card = (label, value, bad) => `<div class="card"><div class="lbl">${label}</div><div class="val${bad ? ' bad' : ''}">${value}</div></div>`;
  const opRows = rows.map((r) => `<tr class="${r.fail / r.reqs > 0.05 ? 'hot' : ''}"><td class="l">${esc(r.op)}</td><td>${r.reqs}</td><td class="ok">${r.pass}</td><td class="${r.fail ? 'bad' : ''}">${r.fail}</td><td>${((r.fail / r.reqs) * 100).toFixed(1)}%</td><td>${fmt(r.avg)}</td><td>${fmt(r.p90)}</td><td>${fmt(r.p95)}</td><td>${fmt(r.p99)}</td><td>${fmt(r.max)}</td><td>${kb(r.kb)}</td></tr>`).join('');
  const typeRows = rows.reduce((acc, r) => acc.concat(r.errs.map((e) => `<tr><td class="l">${esc(r.op)}</td><td class="l">${esc(e[0])}</td><td class="bad">${e[1]}</td><td class="l">${esc(ERR_HELP[e[0]])}</td></tr>`)), []).join('') || '<tr><td class="l" colspan="4">No errors recorded</td></tr>';
  const thRows = ths.map((t) => `<tr><td class="l">${t.ok ? '<b class="ok">PASS</b>' : '<b class="bad">FAIL</b>'}</td><td class="l">${esc(t.name)}</td><td class="l">${esc(t.expr)}</td></tr>`).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Load test report ${esc(TAG)}</title>
<style>
:root{--bg:#fff;--fg:#1a1a19;--mut:#6b6a66;--line:#e1e0d9;--card:#f6f5f1;--ok:#2d7a2d;--bad:#c23a3a;--hot:#fdeeee}
@media (prefers-color-scheme:dark){:root{--bg:#1a1a19;--fg:#f0efec;--mut:#a3a29a;--line:#383835;--card:#262624;--ok:#5fc15f;--bad:#f07272;--hot:#3a2222}}
body{font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:20px;max-width:1150px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}.sub{color:var(--mut);font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:14px 0}
.card{background:var(--card);border-radius:8px;padding:10px}.lbl{font-size:12px;color:var(--mut)}.val{font-size:20px;font-weight:500}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{color:var(--mut);font-weight:400}
.l{text-align:left;white-space:normal}.ok{color:var(--ok)}.bad{color:var(--bad)}tr.hot{background:var(--hot)}
.banner{display:inline-block;padding:3px 10px;border-radius:6px;font-size:12px;font-weight:500;background:var(--hot);color:var(--bad)}
.banner.pass{background:var(--card);color:var(--ok)}
</style></head><body>
<h1>Load test report <span class="banner${thFailed ? '' : ' pass'}">${thFailed ? thFailed + ' threshold(s) failed' : 'All thresholds passed'}</span></h1>
<div class="sub">${new Date().toISOString()} | ${esc(runInfo())} | ${esc(URL)}</div>
<div class="cards">
${card('Requests', totReq)}${card('Requests/s', (val(d, 'http_reqs', 'rate') || 0).toFixed(1))}${card('Pass', totReq - totFail)}${card('Fail', totFail, totFail > 0)}${card('Error rate', totReq ? ((totFail / totReq) * 100).toFixed(1) + '%' : '-', totFail > 0)}
${card('Avg', fmt(val(d, 'http_req_duration', 'avg')))}${card('p90', fmt(val(d, 'http_req_duration', 'p(90)')))}${card('p95', fmt(val(d, 'http_req_duration', 'p(95)')))}${card('p99', fmt(val(d, 'http_req_duration', 'p(99)')))}${card('Page load p95', fmt(val(d, 'dur_PageLoad_All', 'p(95)')))}
</div>
<h2>Per operation</h2><div class="wrap"><table>
<tr><th class="l">Operation</th><th>Requests</th><th>Pass</th><th>Fail</th><th>Fail %</th><th>Avg</th><th>p90</th><th>p95</th><th>p99</th><th>Max</th><th>Avg size</th></tr>${opRows}</table></div>
<h2>HTTP status counts</h2><div class="sub">${esc(statusLine(d) || '-')}</div>
<h2>Errors by type</h2><div class="wrap"><table>
<tr><th class="l">Operation</th><th class="l">Type</th><th>Count</th><th class="l">What it means</th></tr>${typeRows}</table></div>
<!--ERROR_DESCRIPTIONS-->
<h2>Thresholds</h2><div class="wrap"><table><tr><th class="l">Result</th><th class="l">Metric</th><th class="l">Condition</th></tr>${thRows}</table></div>
</body></html>`;
}

export function handleSummary(data) {
  const table = consoleTable(data);
  const out = { stdout: textSummary(data, { indent: ' ', enableColors: true }) + '\n\n' + table + '\n' };
  out[`report-${TAG}.html`] = htmlPage(data);
  out[`summary-${TAG}.json`] = JSON.stringify(data, null, 2);
  out[`errors-${TAG}.txt`] = table;
  return out;
}
