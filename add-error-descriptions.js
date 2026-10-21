// Usage: node add-error-descriptions.js errors.log report.html
// Groups the actual error messages from errors.log and adds them to report.html (and error-descriptions.txt)
const fs = require('fs');
const logFile = process.argv[2] || 'errors.log';
const htmlFile = process.argv[3] || 'report.html';

const esc = (x) => String(x).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// Handles k6 log formats: raw JSON lines, --log-format json, and the default logfmt (msg="...")
function parseLine(line) {
  line = line.trim();
  if (!line) return null;
  try {
    const o = JSON.parse(line);
    if (o && o.operation) return o;
    if (o && typeof o.msg === 'string') return JSON.parse(o.msg);
  } catch (e) { /* fall through */ }
  const m = line.match(/msg="(.*)"(?:\s+source=\S+)?\s*$/);
  if (m) {
    try { return JSON.parse(JSON.parse('"' + m[1] + '"')); } catch (e) { /* ignore */ }
  }
  return null;
}

// Collapse ids and long numbers so the same error groups together
const normalize = (s) => String(s)
  .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi, '<id>')
  .replace(/\d{4,}/g, '<n>')
  .slice(0, 300);

const groups = {};
let total = 0;
fs.readFileSync(logFile, 'utf8').split('\n').forEach((line) => {
  const e = parseLine(line);
  if (!e) return;
  total++;
  const msg = normalize(e.gql_message || e.body_snippet || e.error || `HTTP ${e.status}`);
  const key = [e.operation, e.type, msg].join('|');
  const g = groups[key] || (groups[key] = { op: e.operation, type: e.type, msg, status: e.status, n: 0, ms: 0, reqId: e.request_id, vars: e.variables });
  g.n++;
  g.ms += e.duration_ms || 0;
});

const list = Object.values(groups).sort((a, b) => b.n - a.n).slice(0, 25);

const txt = ['ERROR DESCRIPTIONS (from ' + total + ' logged failures)', '']
  .concat(list.map((g) => `${g.n}x  ${g.op} | ${g.type} | status ${g.status} | avg ${Math.round(g.ms / g.n)} ms\n    ${g.msg}\n    example request_id: ${g.reqId || '-'}  variables: ${g.vars || '-'}`))
  .join('\n');
fs.writeFileSync(logFile.replace(/\.[^.]*$/, '') + '-descriptions.txt', txt + '\n');
console.log(txt);

if (fs.existsSync(htmlFile)) {
  const rows = list.map((g) => `<tr><td>${g.n}</td><td class="l">${esc(g.op)}</td><td class="l">${esc(g.type)}</td><td>${esc(g.status)}</td><td>${Math.round(g.ms / g.n)} ms</td><td class="l" style="word-break:break-word">${esc(g.msg)}</td><td class="l" style="word-break:break-all">${esc(g.reqId || '-')}</td></tr>`).join('');
  const section = `<h2>Error descriptions <span class="sub">(from ${total} logged failures; the log keeps the first N per VU, so counts here are samples)</span></h2><div class="wrap"><table><tr><th>Count</th><th class="l">Operation</th><th class="l">Type</th><th>Status</th><th>Avg time</th><th class="l">Message</th><th class="l">Example request id</th></tr>${rows || '<tr><td class="l" colspan="7">No failures logged</td></tr>'}</table></div>`;
  fs.writeFileSync(htmlFile, fs.readFileSync(htmlFile, 'utf8').replace('<!--ERROR_DESCRIPTIONS-->', section));
  console.log('\nUpdated ' + htmlFile);
}
