// All the things you may need to tweak live here.
const num = (v, d) => (v !== undefined && v !== '' && !isNaN(Number(v)) ? Number(v) : d);

module.exports = {
  baseUrl: 'https://qa.nonprod.int.coke.com/jobs',
  storageState: 'playwright/.auth/user.json',
  csvFile: 'test-data/jobs.csv',
  outDir: 'reports',

  defaultJobsToProcess: 20, // used when jobsToProcess is 0/blank in the CSV (list mode)
  maxJobsPerSearch: 10,     // cap for DOCNUM / baseName searches

  // ---- Performance testing (flag driven) ----
  // PowerShell:  $env:PERF="true"; npx playwright test tests/flow.spec.js --headed
  perf: {
    enabled: /^(1|true|yes|on)$/i.test(process.env.PERF || ''),
    limitMs: num(process.env.PERF_LIMIT_MS, 2000), // flag actions slower than this (full load)
    gqlTimeoutMs: 15000,    // how long to wait for a GraphQL call after an action
    settleTimeoutMs: 60000, // how long to wait for the page to finish loading
    stableMs: 300,          // page must be quiet (no spinner, no GraphQL) this long
  },

  // ---- Selectors / indexes (taken from your recordings) ----
  selectors: {
    list: '.scrollable-item-list',
    // Anything that spins or shows "loading". Replace with the real class from DevTools.
    spinners: ['.spinner', '.loading', '.loader', '.loading-spinner', '[role="progressbar"]', '.fa-spin'],
  },
  buttons: { advanced: 1, refresh: 2, addMetadata: 3 }, // page.getByRole('button').nth(n)

  // ---- Failure detection (string = contains, RegExp = pattern) ----
  failurePatterns: [
    'No more retries. Marking job as FAILED.',
    'HTTP_DELIVERY failed',
    'Expected status code in 200 range. Received 401',
    'Error while streaming:',
    'Failed to load job log id',
    'RestError',
    /Received [45]\d\d/,
  ],
};
