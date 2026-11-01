# UI Automation Execution Report — Coke Codes Admin Portal

Run #52 · Sunday, 1 November 2026 · Triggered by nightly schedule · Branch `main`

SAMPLE DATA — illustrative values to show report layout

## Executive summary

## Result distribution

## Coverage by functional area

## Failures & potential defects

## Stability — last 8 runs (flagged tests)

## Test case inventory

## Execution environment

| Application URL | ui-gcp.alpha.codes.coke.com |
| --- | --- |
| Auth | SSO / IAP (credentials from .env) |
| Browser | Chromium 141 (headless) |
| Framework | Playwright Test 1.63 · Node 22 |
| Workers / retries | 1 (serial) · 1 retry |

**Test design**

- Page Object Model (15 page objects) + reusable `flows/`
- Tag-driven selection: `@program`, `@release`, `@campaign` …
- Self-healing prerequisites; parameter file for org / promotion IDs
- Traces, screenshots and video kept on failure

## Findings & recommendations

- **Validation gaps (3):** end-before-start dates are accepted for Releases and Promotions, and duplicate organization names are not rejected. Recommend raising defects.
- **Code generation timing:** batch generation exceeded the 30 s poll window. Raise the timeout or poll the Queued Requests page; run `@validate` 5–10 minutes after `@batch`.
- **Selector hygiene:** replace remaining `// VERIFY` locators with recorded ones from codegen to reduce flakiness.
- **Next:** add negative cases for Edit flows and Download Codes; wire the report into CI with history retention.

Generated from Playwright JSON reporter output by `scripts/generate-report.js`. Trace, screenshot and video links appear per failure in the live report.