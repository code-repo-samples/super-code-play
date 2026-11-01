# AWS vs GCP Comparison Tool: Report Enhancement Requirements

**Project:** `postman-collections/simplified-python-api-tests`
**Files in scope:** `main.py`, `runner.py`, `reporter.py`, `routes.py`, `config.py`
**Outputs:** CSV, XLSX, HTML (`aws_gcp_compare.*`)

---

## 1. Goal

The current tool works and captures status codes, responses, structures, requests and schema differences. We want to make the report more useful for **migration sign-off** by adding a clear verdict, root cause, latency comparison and a few safety checks.

---

## 2. Ground Rules (Must Follow)

| # | Rule |
|---|------|
| 1 | **Do NOT change, rename or remove** any existing function, column, CSS class, CLI argument or output. |
| 2 | **Additive only.** New logic goes in new functions. New columns are appended at the **end** of existing columns. New sections go **below or beside** the existing tiles. |
| 3 | Every new feature is controlled by a **new optional CLI flag, default OFF**, so default behaviour is unchanged. |
| 4 | Reuse the existing HTML table and tile styling. |
| 5 | Make changes **one step at a time**, show a diff, and wait for approval before the next step. |
| 6 | Do not touch `builders.py`, `auth.py` or `api.py` unless a step cannot be done otherwise. State the reason first. |

**Acceptance check for every step:** run the old and new versions with `--limit 10` and confirm all **original columns and values are identical**.

---

## 3. Current Gaps (Why This Is Needed)

| # | Gap | Impact |
|---|-----|--------|
| 1 | Only one "Response Time" column, so it is unclear whether it is AWS or GCP | No latency comparison |
| 2 | Single run only (`--iterations` and `--count` are accepted but unused) | No p50/p95, cannot detect flaky results |
| 3 | Response headers are not compared | CORS, content-type and cache differences are missed |
| 4 | Remarks show structure differences but not value differences | Same fields with different values go unnoticed |
| 5 | No ignore list for dynamic fields (`transactionId`, timestamps) | False mismatches |
| 6 | AWS and GCP use different static IDs (`PROGRAM_ID`, `RELEASE_ID`, `LOT_ID`, codes) | Data differences look like migration bugs |
| 7 | `TEST_AUDIT_MGMT_SERVICE = False` | Audit-Mgmt may not be tested |
| 8 | `GCP_ENV` has `localhost` URLs (`codes-mgmt` 8091, `config-mgmt` 8085, `audit-mgmt` 8092, `CODE_VALIDATE_SERVICE_URL`) | Results are not from real GCP endpoints |
| 9 | No run metadata (time, base URLs, environment) | Hard to trace a report to a run |
| 10 | No verdict, severity or root cause | Reader must interpret every row |
| 11 | Authorization header is visible in the request view | Credential exposure when sharing |
| 12 | Some negative tests expect `200` (`Get User Invalid UUID Format`, `Get Owner Group Invalid Query`) | Test expectations are probably wrong |

---

## 4. Requirements (Step by Step)

### Step 1: `classify()` in `reporter.py` (new function)

For each result row, compute and **append** these fields.

| Field | Values |
|-------|--------|
| `verdict` | `PASS`, `WARN`, `FAIL` |
| `severity` | `Blocker`, `Major`, `Minor`, `None` |
| `root_cause` | `Routing/config`, `Auth`, `Breaking schema`, `Extra fields`, `Latency`, `Test expectation`, `Error text only`, `None` |

**Verdict rules**

| Verdict | Condition |
|---------|-----------|
| **FAIL** | Status code differs, **or** a field is missing in GCP, **or** a field type differs |
| **WARN** | Only extra fields in GCP, **or** GCP latency is over +50% **and** over +500 ms vs AWS |
| **PASS** | Everything else |
| Negative tests | PASS if the status code matches. Ignore error message text. |

**Severity rules**

| Severity | Condition |
|----------|-----------|
| Blocker | Routing/config failure (404, connection refused, 403 vs 200) |
| Major | Type change, missing field, latency regression |
| Minor | Extra fields only, wrong test expectation |

### Step 2: Ignore list

- Add `IGNORE_FIELDS` in `config.py`, for example `transactionId`, `timestamp`, `requestId` and generated codes.
- Exclude these fields from schema and value comparison.
- List them in Remarks as `Ignored: field1, field2`.

### Step 3: Latency

- Record **AWS ms** and **GCP ms** separately. Keep the existing "Response Time" column unchanged.
- Add a **Delta %** column: `(GCP - AWS) / AWS * 100`.
- Add **p50** and **p95** tiles (AWS and GCP) after the existing tiles.
- Use the existing `--iterations` argument. When it is greater than 1, run each GET route that many times and use the median for the row.

### Step 4: HTML additions in `render_comparison_html`

**New columns (appended at the end of the table)**
- Verdict
- Severity
- Root Cause
- Delta %
- Field Diff (tags: `+ extra`, `- missing`, `type changed`)

**New tiles**
- PASS count, WARN count, FAIL count
- Latency p50 and p95 (AWS to GCP)
- Overall banner: `READY`, `READY WITH WARNINGS` or `NOT READY` with the blocker count

**New sections and controls**
- Root-cause summary bar chart, placed **below** the domain tiles
- Per-domain PASS / WARN / FAIL counts on the domain tiles
- Root Cause dropdown, placed next to the existing filters
- "Failures only" checkbox, placed next to the existing checkboxes
- "Export CSV (filtered)" button

**Security**
- Mask the Authorization header as `*******` in the request view.

### Step 5: CSV and XLSX

- Append the same new columns **at the end only**: `AWS ms`, `GCP ms`, `Delta %`, `Verdict`, `Severity`, `Root Cause`, `Field Diff`.
- Do not change existing column order or names.

### Step 6: Startup validation (print only, no behaviour change)

At startup, print a `WARNING` if:
- any `GCP_ENV` URL contains `localhost`
- `TEST_AUDIT_MGMT_SERVICE` is `False`
- `USER_ID` in `AWS_ENV` is blank

Also print the base URLs and the run time, and add them to the HTML report header as run metadata.

### Step 7 (optional): Extra checks

- Compare response headers (status, content-type, CORS) and add a "Header Diff" column.
- Compare values on stable fields (after the ignore list) and report value mismatches in Field Diff.
- Fix the test expectations in `routes.py` listed in Gap 12, after confirming the correct status with the API owners.

---

## 5. New CLI Flags (all default OFF)

| Flag | Purpose |
|------|---------|
| `--classify` | Enable verdict, severity and root cause (Steps 1, 2, 4, 5) |
| `--latency` | Enable AWS/GCP ms, Delta %, p50/p95 (Step 3) |
| `--validate-config` | Print startup warnings (Step 6) |
| `--compare-headers` | Enable header comparison (Step 7) |

Running without any new flag must produce the **same output as today**.

---

## 6. Acceptance Criteria

- [ ] Running without new flags gives output identical to the current version.
- [ ] Original columns, tiles, filters and checkboxes are unchanged.
- [ ] New columns appear only at the end of the table, CSV and XLSX.
- [ ] Every row has a verdict, severity and root cause when `--classify` is used.
- [ ] Ignored fields do not cause mismatches and are listed in Remarks.
- [ ] AWS ms, GCP ms and Delta % are shown per row when `--latency` is used. p50 and p95 tiles are shown.
- [ ] The Authorization header is masked in the HTML report.
- [ ] Startup warnings appear for localhost URLs and disabled audit tests.
- [ ] Negative tests are judged on status code only, not error text.

---

## 7. Prompt to Give Copilot

```
Read REQUIREMENTS_report_enhancements.md in the project root.
Follow section 2 (Ground Rules) strictly. Implement Step 1 only.
Show the diff, do not change any existing behaviour, and wait for my
approval before Step 2.
```

Attach `#file:reporter.py #file:runner.py #file:main.py #file:config.py` in Copilot Chat so it sees the real code.

---

## 8. Before You Start

1. Commit the current working code to git.
2. Run the current tool with `--limit 10` and keep the output as the baseline.
3. After each step, run again with `--limit 10` and compare against the baseline.
