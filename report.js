const fs = require('fs');
const path = require('path');

function newRun(cfg, planned) {
  const run = { cfg, planned, startedAt: new Date(), scenarios: [], jobs: [], perf: [] };
  run.addScenario = s => {
    const o = { startedAt: new Date(), finishedAt: null, jobsProcessed: 0, notes: [], ...s };
    run.scenarios.push(o);
    return o;
  };
  return run;
}

// FAILED-<docnum>-<timestamp>.txt and .png in reports/failed-job-logs
async function saveFailure(cfg, rec, logText, page) {
  const dir = path.join(process.cwd(), cfg.outDir, 'failed-job-logs');
  fs.mkdirSync(dir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const base = `FAILED-${rec.docNum}-${stamp}`;
  const report = [
    `Timestamp: ${new Date().toISOString()}`,
    `DOCNUM: ${rec.docNum}`,
    `Job ID: ${rec.jobId}`,
    '',
    '================ MATCHED ERRORS ================',
    rec.errors.join('\n'),
    '',
    '================ FAILED STEP ================',
    rec.step ? `${rec.step.component} @ ${rec.step.time}\n${rec.step.message}` : '',
    '',
    '================ PREVIOUS STEP ================',
    rec.prev ? `${rec.prev.component} @ ${rec.prev.time}\n${rec.prev.message}` : '',
    '',
    '================ JOB LOG CONTENT ================',
    logText,
  ].join('\n');
  fs.writeFileSync(path.join(dir, `${base}.txt`), report, 'utf8');
  await page.screenshot({ path: path.join(dir, `${base}.png`), fullPage: true });
  return base;
}

function chartTag() {
  const local = path.join(process.cwd(), 'node_modules', 'chart.js', 'dist', 'chart.umd.js');
  if (fs.existsSync(local)) return `<script>${fs.readFileSync(local, 'utf8')}</script>`; // works offline
  return '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>';
}

function writeReport(run) {
  const { cfg } = run;
  const data = {
    meta: {
      title: 'Jobs Portal - Test & Performance Report',
      env: cfg.baseUrl,
      startedAt: run.startedAt,
      finishedAt: new Date(),
      planned: run.planned,
      perfEnabled: cfg.perf.enabled,
      limitMs: cfg.perf.limitMs,
    },
    scenarios: run.scenarios,
    jobs: run.jobs,
    perf: cfg.perf.enabled ? run.perf : [],
  };
  const dir = path.join(process.cwd(), cfg.outDir);
  fs.mkdirSync(dir, { recursive: true });

  const json = JSON.stringify(data).replace(/</g, '\\u003c');
  const html = fs.readFileSync(path.join(__dirname, '..', 'report', 'template.html'), 'utf8')
    .replace('<!--CHARTJS-->', () => chartTag())
    .replace('/*DATA*/null', () => json);

  fs.writeFileSync(path.join(dir, 'managed-report.html'), html, 'utf8');
  fs.writeFileSync(path.join(dir, 'run-data.json'), JSON.stringify(data, null, 2), 'utf8');
  console.log(`\nReport written: ${path.join(dir, 'managed-report.html')}`);
}

module.exports = { newRun, saveFailure, writeReport };
