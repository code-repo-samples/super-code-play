const cfg = require('./config');

const TS_END = /(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})(?:\s*([AP]M))?\s*$/i;
const TS_RAW = '(\\d{4}-\\d{2}-\\d{2}\\s+\\d{1,2}:\\d{2}:\\d{2}(?:\\s*[AP]M)?)';

function toSeconds(ts) {
  const m = ts.match(TS_END);
  if (!m) return null;
  let h = +m[4];
  const ap = (m[7] || '').toUpperCase();
  if (ap === 'PM' && h < 12) h += 12;
  if (ap === 'AM' && h === 12) h = 0;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], h, +m[5], +m[6]) / 1000;
}

// Log times have no AM/PM (12:00:07 -> 2:47:04). When the newer time looks "earlier",
// the difference is taken modulo 12h so the gap stays correct.
function gapSeconds(newer, older) {
  const a = toSeconds(newer), b = toSeconds(older);
  if (a === null || b === null) return null;
  let gap = a - b;
  if (gap < 0 && !/[AP]M\s*$/i.test(newer)) gap += 12 * 3600;
  return gap;
}

// The Job Log shows newest first: component | message | time
function parseEntries(section) {
  const lines = section.split(/\r?\n/).map(s => s.trim()).filter(Boolean)
    .filter(l => !/^(Job Log|Show debug logs|Hide debug logs)$/i.test(l));
  const entries = [];
  let buf = [];
  for (const line of lines) {
    const m = line.match(TS_END);
    if (m) {
      const rest = line.slice(0, m.index).trim();
      if (rest) buf.push(rest);
      const parts = buf.flatMap(x => x.split(/\t+/)).map(x => x.trim()).filter(Boolean);
      const isComp = /^[A-Z][A-Z0-9_]+$/.test(parts[0] || '');
      entries.push({
        component: isComp ? parts[0] : '',
        message: (isComp ? parts.slice(1) : parts).join(' '),
        time: m[0].trim(),
      });
      buf = [];
    } else buf.push(line);
  }
  return entries;
}

function matchErrors(text) {
  const found = [];
  for (const p of cfg.failurePatterns) {
    if (p instanceof RegExp) { const m = text.match(p); if (m) found.push(m[0]); }
    else if (text.includes(p)) found.push(p);
  }
  return found;
}

function parseJobLog(text) {
  const split = text.search(/^\s*Job Log\s*$/m);
  const head = split >= 0 ? text.slice(0, split) : text;
  const section = split >= 0 ? text.slice(split) : text;
  const g = (re) => (head.match(re)?.[1] ?? '').trim();

  const entries = parseEntries(section);
  const errors = matchErrors(text);
  const isFail = e => e.component === 'UNKNOWN' || matchErrors(`${e.component} ${e.message}`).length > 0;

  let at = errors.length ? entries.findIndex(isFail) : 0;
  if (at < 0) at = 0;
  const step = entries[at] || null;
  const prev = entries[at + 1] || null;

  return {
    docNum: g(/DOCNUM[\s\r\n]+([A-Z0-9]+)/i) || 'NOT FOUND',
    jobId: g(/Job ID\s*(data_source:\S+)/i) || g(/Job ID\s*([\s\S]*?)Transaction Time/i),
    status: g(/Status\s*([\s\S]*?)\s*Creation Time/i),
    transactionTime: g(/Transaction Time\s*([\d.,]+\s*[A-Za-z]+)/i),
    payloadSize: g(/Payload Size\s*([\d.,]+\s*[A-Za-z]+)/i),
    creationTime: g(new RegExp('Creation Time\\s*' + TS_RAW, 'i')),
    deliveryTime: g(new RegExp('Delivery Time\\s*' + TS_RAW, 'i')),
    baseName: g(/baseName\s*([^\r\n]+)/i),
    incomingFileSize: g(/incomingFileSize\s*(\d+)/i),
    outgoingFileSize: g(/outgoingFileSize\s*(\d+)/i),
    partnerIdentifier: g(/partnerIdentifier\s*([^\r\n]+)/i),
    uploadTime: g(/uploadTime\s*([^\r\n]+)/i),
    errorFound: errors.length > 0,
    errors,
    step,   // failed step when there is an error, otherwise the last step
    prev,   // the step just before it
    gapSeconds: step && prev ? gapSeconds(step.time, prev.time) : null,
  };
}

module.exports = { parseJobLog };
