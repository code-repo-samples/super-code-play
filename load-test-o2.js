/*
  SIMPLE GRAPHQL LOAD TEST
  ------------------------
  Run:    k6 run -e BASE_URL=https://host/graphql -e TOKEN=xxxx load-test.js
  Debug:  add  -e PRINT=all   (print every response)   or   -e PRINT=fail   (print only failed ones)
  Output: a table on the console + report.html (change name with -e REPORT=my.html)

  1 TPS here = 1 "user journey" per second. One journey sends every request in REQUESTS below
  plus ONE search chosen from SEARCHES (by weight). With 5 requests + 1 search, 4 TPS = 24 requests per second.

  Ramp-up and ramp-down traffic IS sent, but it is left out of every number in the report.
  Only the time spent at full TPS ("seconds" in PHASES) is measured.
*/
import http from 'k6/http';
import exec from 'k6/execution';
import { group, check } from 'k6';
import { Counter } from 'k6/metrics';
import { SharedArray } from 'k6/data';

// ===================== 1. YOUR SETTINGS =====================
const URL = __ENV.BASE_URL;
const TOKEN = __ENV.TOKEN;
const PRINT = __ENV.PRINT || '';                       // '' = off, 'all', or 'fail'
const REPORT_FILE = __ENV.REPORT || 'report.html';
const TEST_NAME = __ENV.TEST_NAME || 'GraphQL Load Test';
if (!URL || !TOKEN) throw new Error('Run with: -e BASE_URL=https://host/graphql -e TOKEN=xxxx');

// Phases run one after the other. Add or remove lines as you like.
//   tps      = journeys per second at full load
//   seconds  = how long to hold full TPS (ONLY this part is measured and reported)
//   rampUp   = seconds to climb from 0 to full TPS first (optional, not measured)
//   rampDown = seconds to come back down to 0 at the end (optional, not measured)
const PHASES = [
  { name: 'Phase 1', tps: 4, seconds: 120, rampUp: 15, rampDown: 0 },
  { name: 'Phase 2', tps: 8, seconds: 120, rampUp: 15, rampDown: 15 },
];

// Test data from the CSV (first row = column names). Values must not contain commas.
// Blank cells are fine: a row may fill only some of the columns.
const rows = new SharedArray('csv', function () {
  const lines = open(__ENV.CSV || './testdata.csv').split('\n').map(function (l) { return l.trim(); }).filter(function (l) { return l; });
  const head = lines[0].split(',').map(function (h) { return h.trim(); });
  return lines.slice(1).map(function (line) {
    const cells = line.split(',');
    const row = {};
    head.forEach(function (h, i) { row[h] = (cells[i] || '').trim(); });
    return row;
  });
});

// How the data is used: every column becomes its own list of values (blank cells are skipped).
// Each journey picks one random value per column, so a column can have 10 values and another 500.
// The only exception: fromDate and toDate always stay together as a pair, from a row that fills both.
const COLUMNS = Object.keys(rows.length ? rows[0] : {});
const POOL = {};
COLUMNS.forEach(function (c) {
  POOL[c] = [];
  for (let i = 0; i < rows.length; i++) { if (rows[i][c]) POOL[c].push(rows[i][c]); }
});
const DATE_ROWS = [];
for (let i = 0; i < rows.length; i++) { if (rows[i].fromDate && rows[i].toDate) DATE_ROWS.push(rows[i]); }
const pick = function (list) { return list[Math.floor(Math.random() * list.length)]; };

function pickRow() {
  const row = {};
  COLUMNS.forEach(function (c) { row[c] = POOL[c].length ? pick(POOL[c]) : ''; });
  if (DATE_ROWS.length) { const d = pick(DATE_ROWS); row.fromDate = d.fromDate; row.toDate = d.toDate; }
  return row;
}

// Your GraphQL queries: paste the real ones from Postman (keep the $variable names used in "vars" below).
const Q_USER = `query { currentUser { id email role } }`;
const Q_JOBS = `query getJobs($count: Int, $filterConfig: JobFilterConfigInput) {
  jobs(first: $count, filterConfig: $filterConfig) {
    edges { cursor node { id status creationTimestamp sender { id name } receiver { id name } dataType { id name } } } } }`;
const Q_JOB = `query JobDetails($id: String!) {
  job(id: $id) { id status creationTimestamp deliveryTimestamp fileSize sender { id name } receiver { id name } dataType { id name } } }`;
const Q_BATCH = `query GetPendingBatchJobs($first: Int, $offset: Int) {
  pendingBatchJobs(first: $first, offset: $offset) { hasMore items { job { id creationTimestamp } nextActionTime } } }`;
const Q_SYSTEMS = `query Query($count: Int, $filterConfig: DataSystemFilterConfigInput) {
  dataSystems(first: $count, filterConfig: $filterConfig) { edges { cursor node { id name } } } }`;

// The requests sent in every journey, in order (the page load and the job page).
//   name   = label used in the report (letters/numbers/underscore only, no spaces)
//   expect = text that must appear in the response, otherwise the request counts as failed
//   vars   = the GraphQL variables, built from the current CSV row
const REQUESTS = [
  { name: 'CurrentUser', query: Q_USER, expect: 'currentUser',
    vars: function (row) { return {}; } },
  { name: 'GetJobs', query: Q_JOBS, expect: '"edges"',                     // the default list shown when the page opens
    vars: function (row) { return { count: 5, filterConfig: { showHidden: true } }; } },
  { name: 'PendingBatchJobs', query: Q_BATCH, expect: 'pendingBatchJobs',
    vars: function (row) { return { first: 50, offset: 0 }; } },
  { name: 'SearchSystems', query: Q_SYSTEMS, expect: 'dataSystems',
    vars: function (row) { return { count: 10, filterConfig: { like: row.searchText } }; } },
  { name: 'JobDetails', query: Q_JOB, expect: '"job"',
    vars: function (row) { return { id: row.jobId }; } },
];

// ---------- SEARCHES: the mix of filter combinations your users run ----------
// Each journey runs ONE of these, picked by weight. Each has its own line in the report.
//   name        = label in the report
//   weight      = how often it is picked (any numbers; only the proportions matter)
//   use         = which CSV columns go into the filter
//   mustContain = (optional) the response must also contain this CSV column's value, e.g. a job ID search must return that job
const SEARCHES = [
  { name: 'Search_Date',          weight: 30, use: ['fromDate', 'toDate'] },
  { name: 'Search_Date_Status',   weight: 20, use: ['fromDate', 'toDate', 'status'] },
  { name: 'Search_Date_FileName', weight: 15, use: ['fromDate', 'toDate', 'fileName'] },
  { name: 'Search_FileName',      weight: 10, use: ['fileName'] },
  { name: 'Search_Sender',        weight: 10, use: ['senderId'] },
  { name: 'Search_JobId',         weight: 10, use: ['jobId'], mustContain: 'jobId' },
  { name: 'Search_BaseFileName',  weight: 5,  use: ['baseFileName'] },
];

// CSV column -> field name in the API's filterConfig. Rename here if the API uses different names.
//   number: true   = send as a number (Unix dates)
//   under: 'name'  = put the field inside a nested object, e.g. under: 'metadata' gives filterConfig.metadata.jobId
const FIELDS = {
  jobId:        { field: 'jobId' },
  baseFileName: { field: 'baseFileName' },
  fileName:     { field: 'fileName' },
  senderId:     { field: 'senderId' },
  status:       { field: 'status' },
  fromDate:     { field: 'fromDate', number: true },
  toDate:       { field: 'toDate', number: true },
};
const BASE_FILTER = { showHidden: true };   // sent with every search; remove if your UI does not send it

function buildFilter(row, use) {
  const filter = Object.assign({}, BASE_FILTER);
  use.forEach(function (col) {
    const f = FIELDS[col];
    const target = f.under ? (filter[f.under] = filter[f.under] || {}) : filter;
    target[f.field] = f.number ? Number(row[col]) : row[col];
  });
  return filter;
}

// Turn each search into a request (you do not need to edit this part).
const SEARCH_REQUESTS = SEARCHES.map(function (s) {
  return { name: s.name, weight: s.weight, use: s.use, mustContain: s.mustContain, query: Q_JOBS, expect: '"edges"',
    vars: function (row) { return { count: 5, filterConfig: buildFilter(row, s.use) }; } };
});
const TOTAL_WEIGHT = SEARCH_REQUESTS.reduce(function (t, s) { return t + s.weight; }, 0);
const ALL = REQUESTS.concat(SEARCH_REQUESTS);   // everything the report lists
const JOURNEY_SIZE = REQUESTS.length + 1;       // requests per journey

// Safety check before the test starts: every column a request needs must have at least one value in the CSV.
// Columns used by the fixed REQUESTS above (SearchSystems uses searchText, JobDetails uses jobId):
const FIXED_NEEDS = ['searchText', 'jobId'];
(function checkData() {
  const problems = [];
  const need = function (who, col) {
    if (col === 'fromDate' || col === 'toDate') { if (!DATE_ROWS.length) problems.push(who + ' needs a row with BOTH fromDate and toDate filled'); }
    else if (!POOL[col] || !POOL[col].length) problems.push(who + ' needs column "' + col + '" but it has no values in the CSV');
  };
  FIXED_NEEDS.forEach(function (c) { need('Fixed requests', c); });
  SEARCHES.forEach(function (s) {
    s.use.concat(s.mustContain ? [s.mustContain] : []).forEach(function (c) { need(s.name, c); });
  });
  if (problems.length) throw new Error('CSV problem:\n - ' + problems.filter(function (p, i) { return problems.indexOf(p) === i; }).join('\n - '));
})();

// ===================== 2. HOW THE TEST RUNS =====================
const total = new Counter('txn_total');    // every request counts once here
const failed = new Counter('txn_failed');  // and once here only if it failed (pass = total - failed, so nothing is double counted)

// Phases become k6 scenarios that start one after another.
const scenarios = {};
let startAt = 0;
PHASES.forEach(function (p, i) {
  const up = p.rampUp || 0, down = p.rampDown || 0;
  const stages = [];
  if (up) stages.push({ duration: up + 's', target: p.tps });
  stages.push({ duration: p.seconds + 's', target: p.tps });
  if (down) stages.push({ duration: down + 's', target: 0 });
  scenarios['phase' + (i + 1)] = {
    executor: 'ramping-arrival-rate', startRate: up ? 0 : p.tps, timeUnit: '1s', stages: stages, startTime: startAt + 's',
    preAllocatedVUs: p.tps * 5, maxVUs: p.tps * 25,
  };
  startAt += up + p.seconds + down;
});

// These always-true thresholds are a k6 trick: they make k6 keep the per-phase, per-request numbers for the report.
const thresholds = { 'http_req_duration{counted:yes}': ['max>=0'] };
PHASES.forEach(function (p, i) {
  const ph = 'phase' + (i + 1);
  thresholds['http_req_duration{phase:' + ph + '}'] = ['max>=0'];
  thresholds['iterations{scenario:' + ph + '}'] = ['count>=0'];
  thresholds['dropped_iterations{scenario:' + ph + '}'] = ['count>=0'];
  ALL.forEach(function (r) {
    const t = '{phase:' + ph + ',name:' + r.name + '}';
    thresholds['http_req_duration' + t] = ['max>=0'];
    thresholds['txn_total' + t] = ['count>=0'];
    thresholds['txn_failed' + t] = ['count>=0'];
  });
});

export const options = {
  scenarios: scenarios,
  thresholds: thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

const headers = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN };

// One journey: pick the values and send every request.
export default function () {
  // 1. choose which kind of search this journey does (by weight)
  let dice = Math.random() * TOTAL_WEIGHT;
  let search = SEARCH_REQUESTS[0];
  for (let i = 0; i < SEARCH_REQUESTS.length; i++) {
    dice -= SEARCH_REQUESTS[i].weight;
    if (dice <= 0) { search = SEARCH_REQUESTS[i]; break; }
  }
  // 2. pick this journey's values: one random value per column, only from cells that are filled in
  const row = pickRow();
  // 3. is this journey inside the measured part of its phase?
  const p = PHASES[parseInt(exec.scenario.name.replace('phase', ''), 10) - 1];
  const up = (p.rampUp || 0) * 1000;
  const elapsed = Date.now() - exec.scenario.startTime;
  const counted = elapsed >= up && elapsed < up + p.seconds * 1000;
  REQUESTS.forEach(function (r) { send(r, row, counted); });
  send(search, row, counted);
}

function send(r, row, counted) {
  const phase = counted ? exec.scenario.name : 'ramp';   // ramp traffic is tagged 'ramp' and never reported
  const tags = { phase: phase, name: r.name, counted: counted ? 'yes' : 'no' };
  group(r.name, function () {
    const variables = r.vars(row);
    const res = http.post(URL, JSON.stringify({ query: r.query, variables: variables }), { headers: headers, tags: tags, timeout: '30s' });

    // ONE decision per request: passed only if HTTP status is 200 AND no GraphQL error AND the expected text is present.
    let message = '';
    try { const b = res.json(); if (b.errors && b.errors.length) message = String(b.errors[0].message); } catch (e) { /* not JSON */ }
    let problem = '';
    if (res.status !== 200) problem = message || res.error || String(res.body || '').slice(0, 80) || 'No response body';
    else if (message) problem = 'GraphQL error: ' + message;
    else if (res.body.indexOf(r.expect) === -1) problem = 'Expected text not found: ' + r.expect;
    else if (r.mustContain && res.body.indexOf(row[r.mustContain]) === -1) problem = 'Result does not contain the searched ' + r.mustContain;

    if (counted) {
      total.add(1, tags);
      failed.add(problem ? 1 : 0, tags);
    }
    if (counted && problem) {
      // The error text goes into a check name: k6 counts identical names for us across all users, and the report reads them.
      check(res, { [phase + ' | ' + r.name + ' | ' + res.status + ' | ' + problem.replace(/\d{4,}/g, '#').slice(0, 100)]: function () { return false; } });
    }
    if (PRINT === 'all' || (PRINT === 'fail' && problem)) {
      console.log('[' + phase + '] ' + r.name + ' -> HTTP ' + res.status + ' in ' + Math.round(res.timings.duration) + ' ms '
        + (problem ? 'FAILED: ' + problem : 'OK') + '\nvariables: ' + JSON.stringify(variables).slice(0, 300) + '\n' + String(res.body).slice(0, 1000));
    }
  });
}

// ===================== 3. REPORT (no need to edit below) =====================
const esc = function (s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); };
const list = function (x) { return Array.isArray(x) ? x : Object.keys(x || {}).map(function (k) { return x[k]; }); };
const fmt = function (ms) { return ms === undefined || isNaN(ms) ? '-' : ms >= 1000 ? (ms / 1000).toFixed(2) + ' s' : Math.round(ms) + ' ms'; };
const when = function (d) { return d.toISOString().replace('T', ' ').slice(0, 19) + ' UTC'; };
const hms = function (ms) { const s = Math.round(ms / 1000); return Math.floor(s / 60) + 'm ' + (s % 60) + 's'; };
const pct = function (a, b) { return b ? (a / b * 100).toFixed(1) + '%' : '-'; };

// Finds a tagged sub-metric (for example http_req_duration for phase1 + GetJobs), whatever order k6 wrote the tags in.
function find(data, metric, tags) {
  const want = Object.keys(tags).map(function (k) { return k + ':' + tags[k]; }).sort().join(',');
  const keys = Object.keys(data.metrics);
  for (let i = 0; i < keys.length; i++) {
    const b = keys[i].indexOf('{');
    if (b < 0 || keys[i].slice(0, b) !== metric) continue;
    const have = keys[i].slice(b + 1, -1).split(',').map(function (s) { return s.trim(); }).sort().join(',');
    if (have === want) return data.metrics[keys[i]].values;
  }
  return {};
}

function buildPhases(data) {
  return PHASES.map(function (p, i) {
    const ph = 'phase' + (i + 1);
    const rowsOut = ALL.map(function (r) {
      const t = { phase: ph, name: r.name };
      const n = find(data, 'txn_total', t).count || 0;
      const f = find(data, 'txn_failed', t).count || 0;
      const d = find(data, 'http_req_duration', t);
      return { name: r.name, count: n, pass: n - f, fail: f, avg: d.avg, max: d.max, p90: d['p(90)'], p95: d['p(95)'] };
    });
    const sum = function (k) { return rowsOut.reduce(function (s, r) { return s + r[k]; }, 0); };
    const all = find(data, 'http_req_duration', { phase: ph });
    return { name: p.name, tps: p.tps, seconds: p.seconds, rampUp: p.rampUp || 0, rampDown: p.rampDown || 0, rows: rowsOut, count: sum('count'), pass: sum('pass'), fail: sum('fail'),
      avg: all.avg, max: all.max, p90: all['p(90)'], p95: all['p(95)'],
      achieved: p.seconds ? Math.round(sum('count') / JOURNEY_SIZE) / p.seconds : 0, dropped: find(data, 'dropped_iterations', { scenario: ph }).count || 0 };
  });
}

function buildErrors(data) {
  const out = [];
  const walk = function (g) {
    list(g.checks).forEach(function (c) {
      if (!c.fails) return;
      const parts = c.name.split(' | ');
      const idx = parseInt(parts[0].replace('phase', ''), 10) - 1;
      out.push({ phase: PHASES[idx] ? PHASES[idx].name : parts[0], txn: parts[1], code: parts[2], desc: parts.slice(3).join(' | '), count: c.fails });
    });
    list(g.groups).forEach(walk);
  };
  walk(data.root_group || {});
  return out.sort(function (a, b) { return b.count - a.count; });
}

const css = `
:root{--bg:#f3f5fa;--card:#fff;--fg:#1b2433;--mut:#6a7588;--line:#e5e9f1;--ok:#168a4a;--okbg:#e4f5ea;--bad:#d23a3a;--badbg:#fdeaea;--warn:#a96b00;--warnbg:#fff3dc;--soft:rgba(110,125,150,.07)}
@media (prefers-color-scheme:dark){:root{--bg:#0d1320;--card:#151d2d;--fg:#e8edf6;--mut:#93a0b8;--line:#25304a;--ok:#4cd38a;--okbg:#10301f;--bad:#ff7b7b;--badbg:#3a1a1e;--warn:#f0b24a;--warnbg:#3a2d12;--soft:rgba(150,165,190,.08)}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
.hero{background:linear-gradient(135deg,#0c2340 0%,#1b5aa6 100%);color:#fff;padding:34px 24px 64px}
.hero .in{max-width:1080px;margin:0 auto}.hero h1{margin:0;font-size:26px;font-weight:650}.hero p{margin:6px 0 0;opacity:.8;font-size:13px;word-break:break-all}
.wrap{max-width:1080px;margin:-40px auto 48px;padding:0 20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:0 2px 8px rgba(15,30,60,.07)}
.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut)}.val{font-size:21px;font-weight:650;margin-top:3px}.small{font-size:15px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.slow{color:var(--warn);font-weight:700}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-top:20px;overflow:hidden;box-shadow:0 2px 8px rgba(15,30,60,.07)}
section>h2{margin:0;padding:15px 18px;font-size:17px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.pills{display:flex;gap:8px;flex-wrap:wrap}.pill{font-size:12px;font-weight:600;padding:3px 10px;border-radius:99px;background:var(--soft);color:var(--mut)}
.pill.ok{background:var(--okbg);color:var(--ok)}.pill.bad{background:var(--badbg);color:var(--bad)}.pill.warn{background:var(--warnbg);color:var(--warn)}
.bar{height:5px;background:var(--badbg);margin:0 18px 12px;border-radius:3px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ok)}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);text-align:right;padding:10px 16px;background:var(--soft);white-space:nowrap}
td{padding:10px 16px;text-align:right;border-top:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child,.left{text-align:left}td.wrapit{white-space:normal;min-width:220px}
tr.tot td{font-weight:650;background:var(--soft)}
.note{padding:14px 18px;color:var(--mut)}.foot{margin-top:22px;text-align:center;color:var(--mut);font-size:12px}
`;

function phaseHtml(p) {
  const achieved = p.achieved.toFixed(2);
  const ramps = (p.rampUp ? 'Ramp-up ' + p.rampUp + ' s' : '') + (p.rampUp && p.rampDown ? ' and ' : '') + (p.rampDown ? 'ramp-down ' + p.rampDown + ' s' : '');
  let slowAt = -1, slowest = -1;   // the request with the highest p95 in this phase is highlighted
  p.rows.forEach(function (r, i) { if (r.count && r.p95 > slowest) { slowest = r.p95; slowAt = i; } });
  const body = p.rows.concat([{ name: 'Total', count: p.count, pass: p.pass, fail: p.fail, avg: p.avg, max: p.max, p90: p.p90, p95: p.p95, total: true }]).map(function (r, i) {
    return '<tr' + (r.total ? ' class="tot"' : '') + '><td>' + esc(r.name) + '</td><td>' + r.count + '</td><td class="ok">' + r.pass + '</td><td class="' + (r.fail ? 'bad' : '') + '">' + r.fail
      + '</td><td>' + fmt(r.avg) + '</td><td>' + fmt(r.max) + '</td><td>' + fmt(r.p90) + '</td><td class="' + (i === slowAt ? 'slow' : '') + '">' + fmt(r.p95) + '</td></tr>';
  }).join('');
  return '<section><h2><span>' + esc(p.name) + ' &middot; ' + p.tps + ' TPS</span><span class="pills">'
    + '<span class="pill">Target ' + p.tps + ' TPS</span><span class="pill ' + (p.dropped ? 'warn' : '') + '">Achieved ' + achieved + ' TPS</span><span class="pill">Measured ' + p.seconds + ' s</span>' + (ramps ? '<span class="pill">' + ramps + ' excluded</span>' : '')
    + (p.dropped ? '<span class="pill warn">' + p.dropped + ' journeys not started (load generator could not keep up)</span>' : '')
    + '<span class="pill ' + (p.fail ? 'bad' : 'ok') + '">' + (p.fail ? p.fail + ' failed' : 'All passed') + '</span></span></h2>'
    + '<div class="bar"><i style="width:' + (p.count ? (p.pass / p.count * 100) : 0) + '%"></i></div>'
    + '<div class="scroll"><table><tr><th>Transaction</th><th>Count</th><th>Pass</th><th>Fail</th><th>Avg</th><th>Max</th><th>p90</th><th>p95</th></tr>' + body + '</table></div></section>';
}

function errorsHtml(errors) {
  if (!errors.length) return '<section><h2>Errors</h2><div class="note">No errors recorded. Every request passed.</div></section>';
  const byCode = {};
  errors.forEach(function (e) { byCode[e.code] = (byCode[e.code] || 0) + e.count; });
  const codeRows = Object.keys(byCode).sort(function (a, b) { return byCode[b] - byCode[a]; }).map(function (c) {
    return '<tr><td>' + esc(c === '0' ? '0 (no response / timeout)' : c) + '</td><td class="bad">' + byCode[c] + '</td></tr>'; }).join('');
  const detailRows = errors.map(function (e) {
    return '<tr><td>' + esc(e.code) + '</td><td class="left wrapit">' + esc(e.desc) + '</td><td class="left">' + esc(e.txn) + '</td><td class="left">' + esc(e.phase) + '</td><td class="bad">' + e.count + '</td></tr>'; }).join('');
  return '<section><h2>Errors</h2><div class="scroll"><table><tr><th>Status code</th><th>Count</th></tr>' + codeRows + '</table></div>'
    + '<div class="scroll" style="border-top:1px solid var(--line)"><table><tr><th>Status code</th><th class="left">Description</th><th class="left">Transaction</th><th class="left">Phase</th><th>Count</th></tr>' + detailRows + '</table></div></section>';
}

export function handleSummary(data) {
  const end = new Date();
  const planned = PHASES.reduce(function (s, p) { return s + (p.rampUp || 0) + p.seconds + (p.rampDown || 0); }, 0) * 1000;
  const measured = PHASES.reduce(function (s, p) { return s + p.seconds; }, 0) * 1000;
  const ran = data.state && data.state.testRunDurationMs ? data.state.testRunDurationMs : planned;
  const start = new Date(end.getTime() - ran);
  const phases = buildPhases(data);
  const errors = buildErrors(data);
  const n = phases.reduce(function (s, p) { return s + p.count; }, 0);
  const f = phases.reduce(function (s, p) { return s + p.fail; }, 0);
  const all = find(data, 'http_req_duration', { counted: 'yes' });  // measured time only, ramps excluded
  const card = function (l, v, cls) { return '<div class="card"><div class="lbl">' + l + '</div><div class="val ' + (cls || '') + '">' + v + '</div></div>'; };

  const html = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(TEST_NAME) + '</title><style>' + css + '</style></head><body>'
    + '<div class="hero"><div class="in"><h1>' + esc(TEST_NAME) + '</h1><p>' + esc(URL) + '</p></div></div><div class="wrap">'
    + '<div class="cards">' + card('Started', when(start), 'small') + card('Ended', when(end), 'small') + card('Total duration', hms(ran)) + card('Measured time', hms(measured))
    + card('Total transactions', n) + card('Passed', n - f, 'ok') + card('Failed', f, f ? 'bad' : '') + card('Pass rate', pct(n - f, n), f ? 'bad' : 'ok') + '</div>'
    + '<div class="cards" style="margin-top:12px">' + card('Avg response', fmt(all.avg)) + card('p90', fmt(all['p(90)'])) + card('p95', fmt(all['p(95)'])) + card('Max', fmt(all.max)) + '</div>'
    + phases.map(phaseHtml).join('') + errorsHtml(errors)
    + '<div class="foot">Ramp-up and ramp-down traffic is sent but excluded from every number above. Total duration includes ramps and shutdown; measured time is the time held at full TPS. Orange p95 = slowest request in the phase. 1 TPS = 1 user journey per second (the fixed requests plus one search). A transaction is one request, counted once as pass or fail. Pass = HTTP 200, no GraphQL error and the expected text found.</div></div></body></html>';

  // ---- console summary ----
  const pad = function (x, w) { return String(x).padEnd(w); };
  const L = ['', '=============== ' + TEST_NAME + ' ===============', 'Started: ' + when(start) + '   Ended: ' + when(end) + '   Total duration: ' + hms(ran) + '   Measured (ramps excluded): ' + hms(measured),
    'Total transactions: ' + n + '   Passed: ' + (n - f) + '   Failed: ' + f + '   Pass rate: ' + pct(n - f, n),
    'Avg ' + fmt(all.avg) + '   p90 ' + fmt(all['p(90)']) + '   p95 ' + fmt(all['p(95)']) + '   Max ' + fmt(all.max)];
  phases.forEach(function (p) {
    L.push('', p.name + ' - ' + p.tps + ' TPS target (achieved ' + p.achieved.toFixed(2) + ' TPS, measured ' + p.seconds + 's' + (p.rampUp || p.rampDown ? '; ' + [p.rampUp ? 'ramp-up ' + p.rampUp + 's' : '', p.rampDown ? 'ramp-down ' + p.rampDown + 's' : ''].filter(Boolean).join(' + ') + ' excluded' : '') + ')' + (p.dropped ? '   WARNING: ' + p.dropped + ' journeys not started' : ''),
      pad('Transaction', 24) + pad('Count', 8) + pad('Pass', 8) + pad('Fail', 7) + pad('Avg', 10) + pad('Max', 10) + pad('p90', 10) + 'p95', '-'.repeat(84));
    p.rows.concat([{ name: 'TOTAL', count: p.count, pass: p.pass, fail: p.fail, avg: p.avg, max: p.max, p90: p.p90, p95: p.p95 }]).forEach(function (r) {
      L.push(pad(r.name, 24) + pad(r.count, 8) + pad(r.pass, 8) + pad(r.fail, 7) + pad(fmt(r.avg), 10) + pad(fmt(r.max), 10) + pad(fmt(r.p90), 10) + fmt(r.p95)); });
  });
  L.push('', errors.length ? 'ERRORS' : 'ERRORS: none');
  errors.forEach(function (e) { L.push(pad(e.count + 'x', 7) + pad('HTTP ' + e.code, 10) + pad(e.phase, 9) + pad(e.txn, 22) + e.desc); });
  L.push('', 'Report: ' + REPORT_FILE, '');

  const out = { stdout: L.join('\n') };
  out[REPORT_FILE] = html;
  return out;
}
