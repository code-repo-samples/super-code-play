# GraphQL load test (k6)

One script, seven GraphQL POST requests, weighted traffic, CSV-driven data, phased TPS, HTML report.

## Files

```
loadtest.js                  the script (everything is configured at the top)
data/getJobs.csv             test data, one CSV per request
data/getPendingBatchJobs.csv
data/dataSystemsSearch.csv
data/getJobDetails.csv
data/getSenders.csv
data/getJobStatusCounts.csv
report.html                  created after each run, next to the script
```

CurrentUser has no placeholders, so it has no CSV.

## One-time setup

1. Install k6 (a recent version): https://grafana.com/docs/k6/latest/set-up/install-k6/
   Windows: `winget install k6 --source winget` or `choco install k6`
2. Open `loadtest.js` and set, in the EDIT THESE section:
   - `URL` - the GraphQL endpoint
   - `HEADERS` - paste your current bearer token and cookie

## Changing the requests

Everything is in the `OPS` table at the top of `loadtest.js`. Each request has:

| Field    | Meaning |
|----------|---------|
| `weight` | Share of total traffic. Any numbers, they are normalised. `0` = skip the request. |
| `p95`    | Threshold in ms. The report marks FAIL if p95 is above it. |
| `csv`    | That request's data file in `data/`. Leave out if the body has no placeholders. |
| `body`   | The raw Postman body, pasted between the backticks. |

In a body, `{{columnName}}` is replaced with a value from a random row of that request's CSV.
Values from the same row are always used together. Numbers can be left unquoted
(`"fromDate": {{fromDate}}`).

## CSV rules

- First row is the column names, then one row per combination.
- Every cell must be filled. Rows with any blank cell are ignored.
- If a CSV has no complete rows the test refuses to start and names the file.
- More rows = better spread of data (rows are picked at random and can repeat).

## Profiles and phases

Set in `PROFILES` at the top. Phases run one after another.

```js
stress: [{ tps: 5, secs: 120 }, { tps: 20, secs: 120 }, { tps: 30, secs: 120 }]
```

`tps` is the TOTAL requests per second across all requests, split by weight.
Example: 30 TPS with GetJobs at 40% = 12 GetJobs requests per second.

## Commands

```
k6 version                                   check k6 is installed

k6 run -e PROFILE=smoke  loadtest.js         quick check, 1 TPS for 30s
k6 run -e PROFILE=load   loadtest.js         5 TPS then 20 TPS
k6 run -e PROFILE=stress loadtest.js         5, 20, then 30 TPS

k6 run -e PROFILE=stress loadtest.js > run.log 2>&1
                                             save everything printed to a file
                                             (use this to keep the error lines)

k6 run -e PROFILE=load --out json=raw.json loadtest.js
                                             also save every raw data point

k6 run -e PROFILE=smoke --http-debug loadtest.js
                                             print full request/response (debugging
                                             only - very noisy, use with smoke)

k6 run -e PROFILE=smoke --insecure-skip-tls-verify loadtest.js
                                             if the internal certificate is not trusted
```

If `PROFILE` is not given it runs `smoke`.

## What you get

- **Console:** every failed request is printed as it happens
  (`[ERROR] phase=.. op=.. status=.. time=..ms message=..`), and a summary at the end.
- **report.html:**
  - Executive summary: start, end, duration, transactions, pass, fail, error rate,
    latency (avg, p90, p95, p99, max), dropped count, and each function tested with
    its weight and counts
  - Per phase and operation table (requests, pass, fail, latency percentiles, max)
  - Errors table: exact status code and message from the response, with counts
  - Thresholds: PASS/FAIL per request and phase
  - Status code distribution chart

## What counts as a failure

- HTTP status 400 or above
- No response (status 0, for example connection error)
- HTTP 200 whose body contains GraphQL `errors`
- HTTP 200 with a body that is not valid JSON

Status codes and messages are reported exactly as received, nothing is grouped.

## Token expiry

The first HTTP 401 aborts the test. The console says the token expired and the test was
aborted, and `report.html` is still written with a red banner. Results cover the test up
to that point. Refresh the token in `HEADERS` and run again.

## Slow responses

Nothing is cut short, so response times are the real ones:

- Request timeout is 10 minutes (k6 default is 60 seconds).
- When a phase ends, in-flight requests are given up to 10 minutes to finish.
- The virtual-user limit is high so k6 does not skip requests. If it ever does, the report
  shows a Dropped count.

A gateway or load balancer in front of the API can still end a long request. That shows
up as a real error (for example 504) with its status code and time.
