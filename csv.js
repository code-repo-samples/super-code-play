const fs = require('fs');

function parse(text) {
  const rows = [];
  let row = [], cur = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; }
      else cur += c;
    } else if (c === '"') q = true;
    else if (c === ',') { row.push(cur); cur = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cur); rows.push(row); row = []; cur = '';
    } else cur += c;
  }
  if (cur !== '' || row.length) { row.push(cur); rows.push(row); }
  return rows.filter(r => r.some(x => x.trim() !== ''));
}

const list = v => (v || '').split(',').map(s => s.trim()).filter(Boolean);

// Columns: sender,receiver,dataType,status,fromDate,toDate,docnums,basefilenames,jobsToProcess
function readJobsCsv(file) {
  const [head, ...body] = parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, ''));
  const keys = head.map(h => h.trim());
  return body.map(r => {
    const o = {};
    keys.forEach((k, i) => (o[k] = (r[i] ?? '').trim()));
    return {
      sender: o.sender, receiver: o.receiver, dataType: o.dataType, status: o.status,
      fromDate: o.fromDate, toDate: o.toDate,
      docnums: list(o.docnums), baseNames: list(o.basefilenames),
      jobsToProcess: parseInt(o.jobsToProcess || '0', 10) || 0,
    };
  });
}

module.exports = { readJobsCsv };
