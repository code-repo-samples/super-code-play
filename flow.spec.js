const { test } = require('@playwright/test');
const cfg = require('../helpers/config');
const { readJobsCsv } = require('../helpers/csv');
const { createPortal } = require('../helpers/portal');
const { parseJobLog } = require('../helpers/logParser');
const { newRun, saveFailure, writeReport } = require('../helpers/report');

test.use({ storageState: cfg.storageState });
test.describe.configure({ mode: 'serial' });

const rows = readJobsCsv(cfg.csvFile);
const run = newRun(cfg, rows.length);

const describeFilters = r =>
  [r.sender, r.receiver, r.dataType, r.status && `Status ${r.status}`, (r.fromDate || r.toDate) && `${r.fromDate || ''} to ${r.toDate || ''}`]
    .filter(Boolean).join(' > ');

test.describe('Jobs portal - functional + performance', () => {
  rows.forEach((row, i) => {
    test(`Scenario ${i + 1}`, async ({ page }) => {
      test.setTimeout(30 * 60 * 1000);

      const searches = [
        ...row.docnums.map(value => ({ key: 'DOCNUM', value })),
        ...row.baseNames.map(value => ({ key: 'baseName', value })),
      ];
      const listMode = searches.length === 0;
      if (listMode) searches.push({ key: null, value: null });

      const label = listMode
        ? `Latest ${row.jobsToProcess || cfg.defaultJobsToProcess} jobs`
        : `By ${row.docnums.length ? 'DOCNUM' : 'baseName'}`;
      const sc = run.addScenario({
        id: `S${i + 1}`,
        name: `S${i + 1} - ${label}`,
        filters: describeFilters(row),
        searchBy: listMode ? '-' : searches.map(s => `${s.key} ${s.value}`).join(', '),
      });

      const portal = createPortal(page, run, sc.id);

      for (const s of searches) {
        const limit = listMode
          ? row.jobsToProcess || cfg.defaultJobsToProcess
          : Math.min(row.jobsToProcess || cfg.maxJobsPerSearch, cfg.maxJobsPerSearch);

        const runSearch = async () => {
          await portal.open();
          await portal.applyFilters(row);
          if (s.key) await portal.advancedSearch(s.key, s.value);
        };

        await runSearch();
        const links = portal.jobLinks(row);

        for (let idx = 0; idx < limit; idx++) {
          if (!(await portal.ensureVisible(links, idx))) {
            if (idx === 0) sc.notes.push(`No jobs returned${s.key ? ` for ${s.key} ${s.value}` : ''}`);
            break;
          }

          console.log(`\n========== ${sc.id} ${s.key ? `${s.key} ${s.value} ` : ''}job ${idx + 1} of ${limit} ==========`);
          await portal.openJob(links, idx);

          const logText = await portal.readLog();
          const rec = {
            scenario: sc.id,
            searchedBy: s.key ? `${s.key} ${s.value}` : `Job #${idx + 1} in list`,
            ...parseJobLog(logText),
          };

          if (rec.errorFound) {
            console.log(`FAILED: ${rec.docNum} -> ${rec.errors.join(' | ')}`);
            rec.files = await saveFailure(cfg, rec, logText, page);
          } else {
            console.log(`OK: ${rec.docNum}`);
          }
          run.jobs.push(rec);
          sc.jobsProcessed++;

          await portal.backToJobs();
          // If the portal forgot the filters when we went back, silently re-apply them.
          if ((await links.count()) === 0) {
            portal.silent = true;
            await runSearch();
            portal.silent = false;
          }
        }
      }
      sc.finishedAt = new Date();
    });
  });

  test.afterAll(() => writeReport(run));
});
