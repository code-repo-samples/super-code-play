# config.py - settings for sftp_load.py. Plain Python: use # for comments, True / False / None.
# Run:  python sftp_load.py check      (preview, sends nothing)
#       python sftp_load.py run --dry-run --total 5      (generate 5 files, upload nothing)
# Command line options (--type, --total, --minutes, --mode, --dry-run ...) always win over this file.
# Integrations (type + slot + target) are in integrations.py. This is the only settings file
# you need to operate: use the QUICK CONTROLS below, then adjust individual integrations only
# when you need to change their target, slot, type, or file-specific behaviour.

from integrations import INTEGRATIONS


# ==================================================================================================
# QUICK CONTROLS - most runs only require changing these values
# ==================================================================================================
# Which workload shape to use:
#   "off"                  = simple batch mode; use total / defaults.copies below
#   "one_hour_25k"         = one-hour schedule, 25k total across configured types
#   "five_hours_15k_each"  = 15k per hour for five hours
#   "five_hours_varied"    = different totals each hour
#   "flat_15k_per_hour"    = constant rate, 15k per hour
#   "repeatable_pattern"   = repeatable random pattern (fixed seed)
ACTIVE_SCHEDULE = "off"

# Which integrations take part: "everything", "one_type", "one_type_one_slot",
# "one_type_many_slots", "many_types_all_slots", or "many_types_many_slots".
ACTIVE_FILTER = "everything"

# The CONFIG values below are the normal controls:
#   total = exact file count for a smoke test (e.g. 5); None means use copies or schedule.
#   dry_run = True generates files but does not upload; set False for real delivery.
#   minutes = limits an active schedule to its first N minutes (e.g. 5); None means full duration.
# Command-line flags override these file settings.

# --- SCHEDULES: how many files, over how many hours (random files per minute, exact hourly totals) ---
# profile "random": each hour is split into 60 random minute counts between min_per_minute and
# max_per_minute that add up EXACTLY to that hour's total; "flat": same count every minute.
# Files are spread across each minute; a shortfall is carried into the next minute.
SCHEDULES = {
    "off": None,
    # your schedule, converted from the old JSON file
    "from_json": {
        "enabled": True,
        "hours": 1,
        "per_hour": None,
        "type_totals": { "SFTP-HTTP": 10000, "SFTP-SFTP": 10000, "SFTP-EMAIL": 5000 },
        "profile": "random",
        "min_per_minute": 100,
        "max_per_minute": 2000,
        "seed": None,
        "max_backlog": None,
    },
    # ---- examples: copy one, or change the numbers --------------------------------------------------
    # type_totals = files per TYPE for the whole run; they are mixed evenly through every hour.
    "one_hour_25k": {
        "enabled": True, "hours": 1, "per_hour": None,
        "type_totals": { "SFTP-HTTP": 10000, "SFTP-SFTP": 10000, "SFTP-EMAIL": 5000 },
        "profile": "random", "min_per_minute": 100, "max_per_minute": 2000, "seed": None, "max_backlog": None,
    },
    # same total every hour, no per-type split (all selected integrations share it round robin)
    "five_hours_15k_each": {
        "enabled": True, "hours": 5, "per_hour": 15000,
        "profile": "random", "min_per_minute": 100, "max_per_minute": 2000, "seed": None,
    },
    # a different total for every hour (repeat a number for hours that should match)
    "five_hours_varied": {
        "enabled": True, "per_hour": [15000, 20000, 15000, 15000, 12000],
        "profile": "random", "min_per_minute": 100, "max_per_minute": 2000, "seed": None,
    },
    # exactly the same count every minute (250 x 60 = 15000 per hour)
    "flat_15k_per_hour": {
        "enabled": True, "hours": 1, "per_hour": 15000, "profile": "flat",
    },
    # repeat an exact random pattern: put a fixed number in "seed" (the report prints the seed that was used)
    "repeatable_pattern": {
        "enabled": True, "hours": 1, "per_hour": 15000,
        "profile": "random", "min_per_minute": 100, "max_per_minute": 2000, "seed": 12345,
    },
}

# --- FILTERS: which types / slots take part (command line --type does the same for one run) ---
FILTERS = {
    "everything": [],
    # ---- examples: "slot" can be "all", one number, or a list of numbers ------------------------------
    "one_type": [ { "type": "SFTP-HTTP", "slot": "all" } ],
    "one_type_one_slot": [ { "type": "SFTP-HTTP", "slot": [1] } ],
    "one_type_many_slots": [ { "type": "SFTP-HTTP", "slot": [1, 2, 3] } ],
    "many_types_all_slots": [ { "type": "SFTP-HTTP", "slot": "all" }, { "type": "SFTP-SFTP", "slot": "all" } ],
    "many_types_many_slots": [
        { "type": "SFTP-HTTP", "slot": [1, 2] },
        { "type": "SFTP-SFTP", "slot": [13, 14, 15] },
        { "type": "SFTP-EMAIL", "slot": "all" },
    ],
}


# ==================================================================================================
# 2. SETTINGS
# ==================================================================================================
CONFIG = {

    # functional = 1 worker, files written to disk, every upload checked on the server (small, careful runs).
    # performance = 4 workers, files kept in memory, no per-file check (long scheduled runs).
    # The command line --mode overrides this.
    "mode": "functional",

    # True = generate files only, upload nothing (same as --dry-run on the command line).
    "dry_run": True,

    # "generate_and_upload" | "generate_only" (generate_only = same as dry_run).
    "output_mode": "generate_and_upload",

    # Where the sample files live: <samples>/<TYPE>/<slot> - <description>/<sample file>
    "samples_directory": "project\\samples",

    # Generated files are written here (functional mode, dry runs, or storage = disk).
    "output_directory": "project\\output_files",

    # HTML report and CSV manifest of every run.
    "reports_directory": "project\\reports",

    # One log file per run.
    "logs_directory": "project\\logs",

    # counter.json lives here. NEVER delete it: it keeps file numbers unique across runs.
    "state_directory": "project\\state",

    # A number = send exactly that many files, dealt over the selected integrations.
    # Ignored while a schedule is active. None = use `copies` per integration.
    "total": 5,

    # copies           files per integration when no total/schedule is set
    # replace_last_n   how many digits of the unique number go into each file name
    # append_suffix    text added after the number, before the extension
    # tag_value_length digits written into replace_tags values
    # http_form_field  default multipart field name ("" = empty name, like curl -F '=@file')
    "defaults": {
        "copies": 100,
        "replace_last_n": 10,
        "append_suffix": "",
        "tag_value_length": 10,
        "http_form_field": "",
    },

    # BATCH MODE (only used when ACTIVE_SCHEDULE = "off"): send batch_size files, wait, repeat.
    "batch_size": 100,

    # Pause between batches (batch mode only).
    "batch_pause_seconds": 5,

    # Batch size / pause per type (batch mode only), e.g.
    # { "SFTP-HTTP": { "batch_size": 100, "batch_pause_seconds": 5 } }
    "type_run_settings": {
        "HTTP-SFTP": { "batch_size": 100, "batch_pause_seconds": 1 },
        "SFTP-EMAIL": { "batch_size": 200, "batch_pause_seconds": 1 },
        "SFTP-FTP": { "batch_size": 100, "batch_pause_seconds": 5 },
        "SFTP-HTTP": { "batch_size": 100, "batch_pause_seconds": 1 },
        "SFTP-SFTP": { "batch_size": 300, "batch_pause_seconds": 0.1 },
    },

    # "off" = an integration without a sample file is skipped.
    # "dummy" = it sends the file named in sample_fallback_file instead (each use is listed as a warning).
    "sample_fallback": "dummy",

    # Name of the placeholder file, e.g. "Folder structure latest.xlsx" (searched under samples_directory).
    # None = the first file directly inside samples_directory.
    "sample_fallback_file": "Folder structure latest.xlsx",

    # Failed files never stop a run by themselves. If EVERY file fails continuously for warn_after_seconds an ERROR is
    # logged; after abort_after_seconds (and at least min_failures files) the run stops, writes the report, exit code 3.
    # abort_after_seconds 0 = never stop automatically.
    "failure_policy": { "warn_after_seconds": 30, "abort_after_seconds": 120, "min_failures": 10 },

    # Also stop after this many failures in a row (0 = off).
    "abort_after_consecutive_failures": 0,

    # Above this many files the HTML report lists only the first rows per integration
    # (failed files are always kept). The CSV manifest always has every row.
    "report_html_max_rows": 20000,

    # None = use the mode preset (functional 1, performance 4). Or a number, e.g. 8.
    "workers": None,

    # None = mode preset. "disk" = also write generated files to output_directory, "memory" = do not.
    "storage": None,

    # None = mode preset. True = check each uploaded file on the SFTP server, False = trust the upload.
    "verify_remote": None,

    # Hard throttle for the whole run (batch mode only; a schedule paces itself).
    "max_files_per_hour": None,

    # Used only for the 'target met / not met' line in the report of long runs.
    "target_files_per_hour": 20000,

    # Extra attempts for a failed upload before it is recorded as FAILED.
    "retries": 2,

    # True = at the end of a performance run list each remote folder once (a poller may have taken files).
    "end_check": False,

    # Headers added to every HTTP upload, e.g. { "Authorization": "Bearer ..." }.
    "http_headers": {},

    # False = do not verify HTTPS certificates (test servers only).
    "http_verify_tls": True,

    # True = delete older _run_* folders in output_directory before a real run
    # (never during a dry run, never a folder another run is still using).
    "cleanup_output_before_run": True,

    # Kept for compatibility.
    "report_skipped_integrations": True,

    # Safety net: an SFTP upload_path outside required_prefix is skipped.
    # prompt_before_prefix True = you must type YES before a real run (--yes skips the prompt).
    "upload_path_policy": { "required_prefix": "/spice/qa-final", "prompt_before_prefix": True },

    # ---- set by the two names at the top; no need to edit these three lines ----
    "schedule": SCHEDULES[ACTIVE_SCHEDULE],
    "run_filter": { "slots": FILTERS[ACTIVE_FILTER] },
    "integrations": INTEGRATIONS,
}
