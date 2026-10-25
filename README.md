# sftp_load

One script, one `config.json`, one `.env`. Generates uniquely named copies of your sample files and delivers them over SFTP (`upload_path`) or HTTP multipart POST (`http_url`), then writes an HTML report, a CSV manifest and a log.

## Setup (Windows)

```
pip install -r requirements.txt
copy .env.example .env      (then edit SFTP_HOST, SFTP_USER, SFTP_PASSWORD or SFTP_KEY_FILE)
```

Folder layout (all relative to config.json, changeable in the config):

```
config.json  .env  sftp_load.py
project\samples\<TYPE>\<serial> - <description>\<one sample file>
project\output_files   project\reports   project\logs   project\state
```

`<TYPE>` folders can be written `SFTP - EMAIL` on disk and `SFTP-EMAIL` in the config; spaces are ignored. The serial is matched as a whole number, so `29` never matches `290` or `129`.

## Command cheat sheet (start here)

Use the current `sftp_load.py` from the zip (rename it if you like, for example `sftp-upload-v2.py`). Type `--total 5` with a space; `--total5` is not recognised. An older copy fails with `Invalid schedule block` even for `--total`.

Every command starts with `python sftp_load.py`. Add `--dry-run` to any `run` to generate files only (nothing is uploaded).

**Smoke tests (do these first)**

| Goal | Command |
|---|---|
| Look at the plan, send nothing | `check` |
| Generate exactly 5 files, no upload | `run --dry-run --total 5` |
| Send exactly 5 real files | `run --total 5 --mode performance` |
| First 5 minutes of the schedule, generate only, in seconds | `run --dry-run --minutes 5` |
| Same, but really waiting out the 5 minutes | `run --dry-run --minutes 5 --paced` |
| Real 5-minute run | `run --minutes 5 --mode performance` |

`--total N` ignores the schedule and sends exactly N files. `--minutes N` runs only the first N minutes of the schedule; the file counts are scaled to match (per-type shares stay in proportion).

**Choosing what to run** (`--type` can be repeated; slots come after a colon)

| Goal | Command |
|---|---|
| Everything in the config | `run` |
| One type, all slots | `run --type SFTP-EMAIL` |
| One type, one slot | `run --type SFTP-EMAIL:5` |
| One type, several slots | `run --type SFTP-EMAIL:1,3,5` |
| Several types, all slots | `run --type SFTP-HTTP,SFTP-EMAIL` |
| Several types, same slot in each | `run --type SFTP-HTTP,SFTP-EMAIL --slot 2` |
| Several types, different slots | `run --type "SFTP-HTTP:1;SFTP-SFTP:1,2;SFTP-EMAIL:all"` |
| The same, written as separate options | `run --type SFTP-HTTP:1 --type SFTP-SFTP:1,2` |

Quote the value when it contains `;`. Type names ignore case and spaces. With a schedule and `type_totals`, only the selected types are run and their files are spread over the full hour (their share of the totals is kept); `min_per_minute` is lowered automatically if the smaller total could not reach it. Anything given on the command line wins over `run_filter` in the config. Add `--total` or `--minutes` to any of the above.

**Performance vs functional**

| | Command | What it does |
|---|---|---|
| Functional | `run --mode functional` (the default) | 1 worker, checks every uploaded file on the server (size, time, permissions), report has the `ls -l` evidence. For small runs. |
| Performance | `run --mode performance` | 4 workers, no per-file check, throughput chart. For the long scheduled runs. |

Examples: `run --mode functional --type SFTP-EMAIL:5 --total 20` (check 20 files of one recipe properly), `run --mode performance` (the full scheduled run).

**After a run** open `project\reports\report_<mode>_<id>.html` (summary, every file, chart) and `manifest_<mode>_<id>.csv` (every file). Logs are in `project\logs`.

## Quick start: one hour, 25,000 files

```
python sftp_load.py scan                          once, and again whenever you add sample folders
python sftp_load.py check                         look at the plan, send nothing
python sftp_load.py run --dry-run                 generate all 25,000 files into project\\output_files, upload nothing (takes seconds)
python sftp_load.py run --mode performance        the real one-hour run (type YES at the prompt, or add --yes)
```

`type_totals` names must match your `<TYPE>` folder names (`SFTP-HTTP`, `SFTP-SFTP`, `SFTP-EMAIL`; spaces ignored). `check` warns when a type has no ready integration.

### Generate only (no upload)

`--dry-run` (or `"output_mode": "generate_only"` in the config) writes the files to `project\\output_files\\_run_<id>\\<TYPE>\\<serial - description>\\` and sends nothing. With a schedule it generates at full speed instead of waiting out the hour; add `--paced` to keep the per-minute timing. A dry run never deletes earlier `_run_*` folders. A dry run does not need `upload_path` or `http_url`, so freshly scanned slots work too; a real run skips any slot that has neither. Narrow it with `--type SFTP-EMAIL --slot 5 --total 200`. The CSV manifest lists every generated file with its path.

## Commands

```
python sftp_load.py scan                      add every sample folder missing from config.json (backup kept as config.json.bak)
python sftp_load.py check                     resolve folders and files, show example renamed files, send nothing
python sftp_load.py run --mode functional     full validation, files on disk, every upload checked on the server
python sftp_load.py run --mode performance --total 20000 --yes
python sftp_load.py run --type SFTP-SFTP --slot 5 --total 200
python sftp_load.py run --dry-run             generate files only, upload nothing
```

Options: `--total N`, `--copies N`, `--workers N`, `--type T --slot N|all`, `--yes` (skip the prefix prompt).

## File naming (replace_last_n digits, default 10)

| Sample name | Result |
|---|---|
| `test.file.whereamigoing.txt.6a73h738.edi` | `test.file.whereamigoing.txt.4829301175.edi` (last dot piece replaced) |
| `sample.txt.x.edi` | `sample.txt.4829301176.edi` |
| `sample.txt` | `sample.4829301177.txt` (number inserted) |
| `INV_EINSTEIN_20261001.csv` | `INV_EINSTEIN_4829301178.csv` (last `_` piece replaced) |
| `..._F8626FC19F7C44F789DC7B11653EDL47.dat` | last 10 characters of that piece replaced |

Per integration you can force a rule with `"name_mode": "token" | "tail" | "insert"`. `append_suffix` goes after the number, before the extension.

Numbers come from `project\state\counter.json` and only ever go up, so names never repeat across runs, days or hours. Do not delete that file between runs.

## Replace tags

`"replace_tags": ["DocNum", "TransactionNumber"]` replaces the text of every `<DocNum>...</DocNum>` (namespace prefixes and attributes are fine, case ignored). All occurrences in one file get the same new value. A tag not present in the sample is skipped and listed once in the warnings. Special formats: `{"tag": "DocNum", "prefix": "XYZ", "length": 10}` gives `XYZ` plus 10 digits.

## Modes

| | functional | performance |
|---|---|---|
| Files | written to `output_files\_run_<id>` | memory only (`"storage": "disk"` to also write) |
| Workers | 1 | 4 |
| After each upload | stat on the same connection: size, time, permissions | none (response only) |
| Report | every file with remote size, time, permissions, `ls -l` image per integration | every file, throughput chart, per-worker counts, retries |

Each worker keeps one SFTP connection open and reconnects automatically. Each type runs its own batch stream (`type_run_settings`). `max_files_per_hour` throttles the whole run.

A file that is already gone when it is checked (the poller took it) is shown as Unverified, not Failed.

## Stopping and failures

Ctrl+C stops handing out new files, finishes the ones in flight and writes the report for what was done. A second Ctrl+C quits immediately; the manifest is written as the run goes, so nothing is lost. A failed file is retried (`retries`, default 2), then recorded and the run continues. Missing folders, missing sample files and paths outside `required_prefix` are skipped with a warning.

## Scheduled runs (random files per minute, hourly totals, multi-hour)

Add a `schedule` block to `config.json` (it replaces batch size / pause / `total` / `max_files_per_hour` while enabled):

```json
"schedule": { "enabled": true, "hours": 5, "per_hour": 15000, "profile": "random",
              "min_per_minute": 100, "max_per_minute": 2000, "seed": null }
```

- Each hour is split into 60 random minute counts (for example 1,000, 2,000, 500, 130 ...) that add up to **exactly** that hour's total. `per_hour` is a number (same every hour) or a list, one number per hour: `[15000, 20000, 15000, 15000, 15000]` (repeat a value for hours that should match).
- **Files per type:** `"type_totals": {"SFTP-HTTP": 15000, "SFTP-SFTP": 12000, "SFTP-EMAIL": 5000}` with `"per_hour": null` spreads those totals evenly over `hours`. The types are mixed evenly through every hour, and integrations within a type share round robin. If you also set `per_hour`, the two must add up to the same number (otherwise the run refuses to start and says so).
- `profile: "flat"` sends the same count every minute. `seed: null` gives a new pattern every run; the seed used is printed and written in the report, so you can paste it back to repeat a pattern.
- Files are spread across the whole minute, never fired at once, and all selected integrations share every minute round robin.
- If the server cannot keep up, the shortfall is carried into the next minute (sending is held back once `max_backlog` files are waiting), so the hour still finishes on its total. Minutes that ran behind plan are listed in the report.
- A checkpoint report (`report_..._checkpoint.html`) is rewritten at the end of every hour; the CSV manifest is flushed with it. After a crash in hour 4 you still have hours 1 to 3.
- `python sftp_load.py check` prints the plan (hourly totals, min/avg/max per minute, peak files/second and the upload time your workers must beat). Use `--mode performance` for long runs: functional mode verifies every file and is too slow for thousands per minute.
- The manifest `batch` column is the minute number of the run (1 to 300 for 5 hours).
- Large runs: the HTML report keeps at most `report_html_max_rows` rows (failed rows always kept); the CSV has everything.

### Running several types at the same time

Start one terminal per type, all from the same folder and credentials:

```
python sftp_load.py run --mode performance --type SFTP-SFTP --yes
python sftp_load.py run --mode performance --type SFTP-EMAIL --yes
```

Each run has its own schedule, report, manifest and log (run ids contain the process id). The number counter is locked across processes, so file names never repeat between them, and a run's cleanup never deletes the files of a run that is still going. Their loads add up on the shared account, so size `max_per_minute` for the total.

### When uploads fail

Failed files are retried, recorded, and the run continues. `failure_policy` adds a time window: if **every** file fails continuously for `warn_after_seconds` an ERROR appears in the log and report; after `abort_after_seconds` (and at least `min_failures` files) the run stops, writes the report, and exits with code 3. A single success resets the timer. Set `abort_after_seconds` to 0 to never stop automatically. Note the window looks at all files together: if only one type is down while others succeed, the run continues and that type's failures show in the report.

Keep the PC awake and the terminal open for the whole run. There is no resume after a stop.

## Report

`project\reports\report_<mode>_<id>.html`: summary cards, type table with Pass/Fail, accordion per integration with every file, evidences, warnings. Buttons: Export Excel (opens in Excel, may show a one-time format prompt), Print / Save PDF (summary or all files). `manifest_<mode>_<id>.csv` has every file row.

## Try it without touching real systems

```
python mock_servers.py        (SFTP on 127.0.0.1:2222 user test / password test, HTTP on 127.0.0.1:8080)
```
Point `.env` at it and use `http://127.0.0.1:8080/x` as an `http_url`.
