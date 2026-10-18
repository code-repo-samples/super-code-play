const { expect } = require('@playwright/test');
const cfg = require('./config');

const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const isGql = req => req.url().includes('graphql');

// '2026-10-18' or '2026-10-18 16:00'  ->  '10/18/2026 4:00 PM'
function fmtDate(s, defaultTime) {
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{1,2}):(\d{2}))?/);
  if (!m) return s;
  let time = defaultTime;
  if (m[4]) {
    let h = +m[4];
    const ap = h >= 12 ? 'PM' : 'AM';
    time = `${h % 12 || 12}:${m[5]} ${ap}`;
  }
  return `${m[2]}/${m[3]}/${m[1]} ${time}`;
}

function createPortal(page, run, scenario) {
  const portal = { silent: false }; // silent = do not record perf (used when re-applying filters)
  const gql = { inflight: 0 };
  page.on('request', r => { if (isGql(r)) gql.inflight++; });
  const done = r => { if (isGql(r)) gql.inflight = Math.max(0, gql.inflight - 1); };
  page.on('requestfinished', done);
  page.on('requestfailed', done);

  const spinner = cfg.selectors.spinners.map(s => `${s}:visible`).join(', ');
  const btn = n => page.getByRole('button').nth(n);
  const scroller = () => page.locator(cfg.selectors.list).first();

  // Page is "loaded" when: no spinner, no GraphQL call in flight, for stableMs in a row.
  // Returns ms from t0 to the moment it became stable (the stable window is not counted).
  async function settle(t0 = Date.now()) {
    const deadline = Date.now() + cfg.perf.settleTimeoutMs;
    let since = null;
    while (Date.now() < deadline) {
      const spinning = spinner ? await page.locator(spinner).count() : 0;
      if (spinning === 0 && gql.inflight === 0) {
        since = since ?? Date.now();
        if (Date.now() - since >= cfg.perf.stableMs) return since - t0;
      } else since = null;
      await page.waitForTimeout(50);
    }
    return null; // never settled
  }

  // Runs `trigger` (the click that loads data) and times it:
  //  graphqlMs = click -> GraphQL response, fullMs = click -> fully loaded, no spinner.
  async function measure(action, value, trigger, { gqlTimeoutMs = cfg.perf.gqlTimeoutMs } = {}) {
    const t0 = Date.now();
    const gqlP = page
      .waitForResponse(r => isGql(r.request()), { timeout: gqlTimeoutMs })
      .then(r => ({ ms: Date.now() - t0, status: r.status() }))
      .catch(() => null);
    await trigger();
    const g = await gqlP;
    const fullMs = await settle(t0);
    const rec = {
      scenario, action, value: String(value ?? ''),
      graphqlMs: g ? g.ms : null, graphqlStatus: g ? g.status : null,
      fullMs, settled: fullMs !== null,
    };
    if (!portal.silent) run.perf.push(rec);
    return rec;
  }

  async function pickOption(value) {
    const options = page.locator('[id^="react-select-"][id*="-option-"]');
    await options.first().waitFor({ state: 'visible', timeout: 15000 });
    const exact = options.filter({ hasText: new RegExp(`^\\s*${esc(value)}\\s*$`, 'i') });
    return (await exact.count()) ? exact.first() : options.filter({ hasText: value }).first();
  }

  Object.assign(portal, {
    settle,
    measure,

    async open() {
      await measure('Initial page load', cfg.baseUrl, () => page.goto(cfg.baseUrl, { waitUntil: 'domcontentloaded' }));
      await expect(page.getByText('Jobs', { exact: true })).toBeVisible();
    },

    async selectDropdown(placeholder, value) {
      if (!value) return;
      await page.getByText(placeholder, { exact: true }).click();
      await page.keyboard.type(value, { delay: 20 });
      const option = await pickOption(value);
      await measure(placeholder, value, () => option.click());
    },

    async setDates(from, to) {
      if (!from && !to) return;
      if (from) await page.getByRole('textbox', { name: 'Start date' }).fill(fmtDate(from, '12:00 AM'));
      if (to) await page.getByRole('textbox', { name: 'End date' }).fill(fmtDate(to, '11:59 PM'));
      await page.locator('.date-time-range-filter').click();
      await measure('Date range', `${from || ''} -> ${to || ''}`, () => btn(cfg.buttons.refresh).click());
    },

    async advancedSearch(key, value) {
      await btn(cfg.buttons.advanced).click();
      await expect(page.getByText('Advanced Filters')).toBeVisible();
      await btn(cfg.buttons.addMetadata).click();
      await page.getByText('Key', { exact: true }).click();
      await page.keyboard.type(key, { delay: 20 });
      await (await pickOption(key)).click();
      await page.getByRole('textbox', { name: 'Value' }).fill(value);
      await measure(`Advanced filter: ${key}`, value, () => page.getByRole('button', { name: 'Apply' }).click());
    },

    async applyFilters(row) {
      await portal.selectDropdown('Filter by Sender', row.sender);
      await portal.selectDropdown('Filter by Receiver', row.receiver);
      await portal.selectDropdown('Filter by Data Type', row.dataType);
      await portal.selectDropdown('Filter by Status', row.status);
      await portal.setDates(row.fromDate, row.toDate);
    },

    jobLinks(row) {
      const list = page.locator(cfg.selectors.list).first().getByRole('link');
      return row.sender && row.dataType ? list.filter({ hasText: `${row.sender} > ${row.dataType}` }) : list;
    },

    // Scrolls the list until job #idx exists. Returns false when the list has no more jobs.
    async ensureVisible(links, idx) {
      for (let i = 0; i < 15 && (await links.count()) <= idx; i++) {
        const before = await links.count();
        await measure('Scroll: load more jobs', `${before} loaded`,
          () => scroller().evaluate(el => el.scrollTo({ top: el.scrollHeight })), { gqlTimeoutMs: 4000 });
        if ((await links.count()) <= before) break;
      }
      return (await links.count()) > idx;
    },

    async openJob(links, idx) {
      const link = links.nth(idx);
      await link.scrollIntoViewIfNeeded();
      await measure('Open job details', `job #${idx + 1}`, async () => {
        await link.click();
        await expect(page.getByText('Job Log', { exact: true })).toBeVisible({ timeout: 60000 });
      });
    },

    async readLog() {
      return page.locator('#root').innerText();
    },

    async backToJobs() {
      await page.getByText('Jobs', { exact: true }).click();
      await settle();
    },
  });

  return portal;
}

module.exports = { createPortal };
