#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sftp_load.py - integration load / functional test tool.

Commands
    python sftp_load.py run   [--config config.py] [--mode functional|performance] [options]
    python sftp_load.py scan  [--config config.py]       add integrations found in the samples folder
    python sftp_load.py check [--config config.py]       resolve folders/files only, send nothing
    python sftp_load.py convert --config config.json     turn an old JSON config into config.py + integrations.py

The config is config.py (commented Python, preferred) or a .json file; without --config, config.py is used if it
exists, otherwise config.json.

Delivery is chosen per integration: http_url -> HTTP multipart POST, upload_path -> SFTP.
SFTP login lives in .env (SFTP_HOST, SFTP_PORT, SFTP_USER, SFTP_PASSWORD or SFTP_KEY_FILE).
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import html as htmlmod
import io
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import signal
import stat as statmod
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.1"
COMMANDS = ("run", "scan", "check", "convert")

PRESETS = {
    "functional": {"storage": "disk", "workers": 1, "verify_remote": True, "max_files_per_hour": None},
    "performance": {"storage": "memory", "workers": 4, "verify_remote": False, "max_files_per_hour": None},
}
SERIAL_RE = re.compile(r"^\s*0*(\d+)\s*-\s*(.*)$")
IGNORED_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}


# --------------------------------------------------------------------------- helpers
def utc(ts=None, fmt="%Y-%m-%d %H:%M:%S"):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime(fmt)


def norm_key(s):
    return re.sub(r"\s+", "", str(s)).upper()


def to_path(base: Path, value) -> Path:
    s = str(value)
    p = Path(s) if os.name == "nt" else Path(s.replace("\\", "/"))
    return p if p.is_absolute() else base / p


def opt(cfg, key, default=None):
    v = cfg.get(key)
    return default if v is None else v


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


def digits(num: int, n: int) -> str:
    return str(num)[-n:].rjust(n, "0")


def fmt_dur(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# --------------------------------------------------------------------------- naming + tags
def split_ext(name):
    return os.path.splitext(name)


def pick_mode(stem, n, requested):
    """token = replace last dot-piece, tail = replace last n chars of the last piece, insert = add .<digits>"""
    if requested in ("token", "tail", "insert"):
        if requested == "token" and "." not in stem:
            return "insert"
        return requested
    if "." in stem:
        last = stem.rsplit(".", 1)[1]
        if re.fullmatch(r"[A-Za-z0-9]{1,12}", last):
            return "token"
    if re.search(r"\d", re.split(r"[_\-. ]", stem)[-1]):
        return "tail"
    return "insert"


def build_name(template_name, number, n, suffix, mode):
    stem, ext = split_ext(template_name)
    mode = pick_mode(stem, n, mode)
    if mode == "token":
        last = stem.rsplit(".", 1)[1]
        new = stem[: len(stem) - len(last)] + number
    elif mode == "tail":
        # work inside the last _ - . or space separated piece so earlier words are never damaged
        seg = re.split(r"[_\-. ]", stem)[-1]
        keep = len(seg) - n if len(seg) > n else 0
        new = stem[: len(stem) - len(seg) + keep] + number
    else:
        new = stem + "." + number
    return new + suffix + ext


class TagSpec:
    def __init__(self, raw, default_len):
        if isinstance(raw, str):
            raw = {"tag": raw}
        self.tag = str(raw["tag"])
        self.prefix = str(raw.get("prefix", ""))
        self.suffix = str(raw.get("suffix", ""))
        self.length = int(raw.get("length", default_len))
        t = re.escape(self.tag).encode()
        self.rx = re.compile(
            rb"(<(?:[\w.\-]+:)?" + t + rb"(?:\s[^>]*)?>)([^<]*)(</(?:[\w.\-]+:)?" + t + rb"\s*>)", re.I)


def apply_tags(data: bytes, specs, first_num: int):
    changed = []
    for k, sp in enumerate(specs):
        val = (sp.prefix + digits(first_num + k, sp.length) + sp.suffix).encode()
        data, c = sp.rx.subn(lambda m, v=val: m.group(1) + v + m.group(3), data)
        if c:
            changed.append(sp.tag)
    return data, changed


# --------------------------------------------------------------------------- schedule (random per-minute plan)
def plan_hour(total, lo, hi, profile, rng, minutes=60):
    """Return `minutes` counts that add up to exactly `total`, each within [lo, hi] (random profile)."""
    if profile == "flat":
        return [(total * (i + 1)) // minutes - (total * i) // minutes for i in range(minutes)]
    if not (lo * minutes <= total <= hi * minutes):
        fix = (f"raise max_per_minute to at least {-(-total // minutes)} (more if you want bursts)" if total > hi * minutes
               else f"lower min_per_minute to {total // minutes} or less")
        raise ValueError(f"{total:,} files/hour cannot be split over {minutes} minutes within "
                         f"{lo}..{hi} per minute (allowed hourly total: {lo * minutes:,}..{hi * minutes:,}). To fix: {fix}, "
                         f"or change the hourly total / type_totals")
    cap = hi - lo
    extra = [0] * minutes
    remaining = total - lo * minutes
    weights = [rng.lognormvariate(0, 1.4) for _ in range(minutes)]   # heavy tail: a few big minutes, many small
    open_ = list(range(minutes))
    while remaining > 0 and open_:
        wsum = sum(weights[i] for i in open_)
        given = 0
        for i in open_:
            a = min(int(remaining * weights[i] / wsum), cap - extra[i])
            extra[i] += a
            given += a
        remaining -= given
        open_ = [i for i in open_ if extra[i] < cap]
        if given == 0 and open_:                                        # hand out the last few one by one
            for i in rng.sample(open_, min(len(open_), remaining)):
                extra[i] += 1
                remaining -= 1
            open_ = [i for i in open_ if extra[i] < cap]
    assert remaining == 0 and sum(extra) == total - lo * minutes
    return [lo + e for e in extra]


def build_filter(cfg, args):
    """What to run: --type values (repeatable) like 'SFTP-HTTP', 'SFTP-HTTP:1,3', 'A;B:2' or 'A,B'; else config run_filter."""
    flt = []
    for val in (getattr(args, "type", None) or []):
        for part in str(val).split(";"):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                t, sl = part.split(":", 1)
                flt.append({"type": t.strip(), "slot": [x.strip() for x in sl.split(",") if x.strip()] or "all"})
            else:
                dflt = [x.strip() for x in str(args.slot).split(",")] if getattr(args, "slot", None) else "all"
                flt.extend({"type": t.strip(), "slot": dflt} for t in part.split(",") if t.strip())
    if not flt:
        flt = list((cfg.get("run_filter") or {}).get("slots") or [])
    return flt


def mix_order(quotas):
    """Order files so every integration (and so every type) is spread evenly through the whole run."""
    keys = []
    for i, q in enumerate(quotas):
        keys.extend(((k + 0.5) / q, i) for k in range(q))
    keys.sort()
    return [i for _, i in keys]


def parse_schedule(raw, keep_types=None, only_minutes=None):
    """Validate the `schedule` block and build the full minute-by-minute plan. Returns None when not enabled."""
    if not raw or raw.get("enabled", True) is False:
        return None
    try:
        type_totals = {str(k): int(v) for k, v in (raw.get("type_totals") or {}).items() if not str(k).startswith("_")}
        if any(v < 1 for v in type_totals.values()):
            raise ValueError("every number in type_totals must be at least 1")
        per_hour = raw.get("per_hour")
        if isinstance(per_hour, list):
            totals = [int(x) for x in per_hour]
            hours = int(raw.get("hours") or len(totals))
            if len(totals) != hours:
                raise ValueError(f"per_hour has {len(totals)} entries but hours is {hours}")
        else:
            hours = int(raw.get("hours", 1))
            if per_hour is None and type_totals:                    # spread the type totals evenly over the hours
                tt = sum(type_totals.values())
                totals = [(tt * (h + 1)) // hours - (tt * h) // hours for h in range(hours)]
            else:
                totals = [int(15000 if per_hour is None else per_hour)] * hours
        scaled_note = ""
        if type_totals and sum(totals) == sum(type_totals.values()) and keep_types is not None:
            kept = {k: v for k, v in type_totals.items() if norm_key(k) in keep_types}
            if not kept:
                raise ValueError("none of the selected types appear in type_totals (selected: "
                                 + ", ".join(sorted(keep_types)) + ")")
            if kept != type_totals:                                  # a subset was selected: re-spread over the hours
                old, new = sum(type_totals.values()), sum(kept.values())
                acc, scaled = 0, []
                for t in totals:
                    acc += t * new
                    scaled.append(acc // old - sum(scaled))
                totals, type_totals = scaled, kept
                scaled_note = (f"Only {', '.join(kept)} selected: {new:,} files, hourly totals "
                               + ", ".join(f"{x:,}" for x in totals))
        if type_totals and sum(totals) != sum(type_totals.values()):
            raise ValueError(f"type_totals add up to {sum(type_totals.values()):,} but the hourly totals add up to "
                             f"{sum(totals):,}. Set per_hour to null to spread type_totals evenly over the hours, "
                             f"or make the two match")
        profile = str(raw.get("profile", "random")).lower()
        if profile not in ("random", "flat"):
            raise ValueError("profile must be 'random' or 'flat'")
        lo, hi = int(raw.get("min_per_minute", 0)), int(raw.get("max_per_minute", 10 ** 9))
        if scaled_note and lo * 60 > min(totals):
            lo = max(min(totals) // 60, 0)
            scaled_note += f"; min_per_minute lowered to {lo}"
        if hours < 1 or lo < 0 or hi < max(lo, 1) or min(totals) < 1:
            raise ValueError("hours must be >= 1, per_hour >= 1 and 0 <= min_per_minute <= max_per_minute")
        seed = raw.get("seed")
        seed_used = int(seed) if seed is not None else random.SystemRandom().randrange(2 ** 32)
        rng = random.Random(seed_used)
        minutes = []
        for t in totals:
            minutes.extend(plan_hour(t, lo, hi, profile, rng))
    except (ValueError, TypeError) as ex:
        raise SystemExit(f"Invalid schedule in the config: {ex}")
    if only_minutes and only_minutes < len(minutes):                 # quick test: just the first N minutes of the plan
        minutes = minutes[:only_minutes]
        new, old = sum(minutes), sum(totals)
        if type_totals:
            acc, out, done = 0, {}, 0
            for k, v in type_totals.items():
                acc += v * new
                out[k] = acc // old - done
                done += out[k]
            out[k] += new - done
            type_totals = {k: v for k, v in out.items() if v > 0}
        hours = (len(minutes) + 59) // 60
        totals = [sum(minutes[h * 60:(h + 1) * 60]) for h in range(hours)]
        scaled_note = (scaled_note + "; " if scaled_note else "") + \
            f"First {only_minutes} minute(s) of the plan only: {new:,} files"
    peak = max(minutes)
    backlog = int(raw.get("max_backlog") or max(peak * 2, 200))
    msec = float(raw.get("minute_seconds", 60))      # test only: shrink a 'minute' to run a long schedule quickly
    return dict(note=scaled_note, hours=hours, totals=totals, type_totals=type_totals, profile=profile, lo=lo, hi=hi, minutes=minutes, minute_seconds=msec,
                seed=seed, seed_used=seed_used, max_backlog=backlog, total=sum(totals))


class _FileLock:
    """Cross-process lock, so two sftp_load runs started at the same time never share a number."""

    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def __enter__(self):
        self.fh = open(self.path, "a+b")
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


class UniqueCounter:
    """Persistent, strictly increasing counter so numbers never repeat across runs (or parallel runs)."""

    def __init__(self, path: Path, warn):
        self.path, self.lock, self.last = path, threading.Lock(), 0
        self.warn = warn
        path.parent.mkdir(parents=True, exist_ok=True)
        self.flock = _FileLock(path.with_suffix(".lock"))
        self.last = self._read(first=True)

    def _read(self, first=False):
        if not self.path.exists():
            return 0
        try:
            return int(json.loads(self.path.read_text())["last"])
        except Exception:
            if first:
                self.warn(f"Counter file {self.path} unreadable; re-seeding from the clock")
            return 0

    def reserve(self, count):
        with self.lock, self.flock:
            start = max(self.last + 1, self._read() + 1, int(time.time()))   # re-read: another run may have moved it
            self.last = start + count - 1
            tmp = self.path.with_suffix(".tmp%d" % os.getpid())
            tmp.write_text(json.dumps({"last": self.last, "saved": utc()}))
            os.replace(tmp, self.path)
            return start


class RateLimiter:
    def __init__(self, per_hour):
        self.interval, self.next, self.lock = 3600.0 / per_hour, time.monotonic(), threading.Lock()

    def acquire(self, stop):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.interval
        if t - now > 0:
            stop.wait(t - now)


# --------------------------------------------------------------------------- transports
class SftpConn:
    def __init__(self):
        self.client = self.sftp = None
        self.root = os.environ.get("SFTP_LOCAL_ROOT")  # test hook: pretend SFTP with a local folder

    def open(self):
        if self.root:
            return
        import paramiko
        e = os.environ
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        timeout = float(e.get("SFTP_TIMEOUT", "30"))
        kw = dict(hostname=e["SFTP_HOST"], port=int(e.get("SFTP_PORT", "22")), username=e["SFTP_USER"],
                  timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                  allow_agent=False, look_for_keys=False)
        if e.get("SFTP_KEY_FILE"):
            kw["key_filename"] = e["SFTP_KEY_FILE"]
            if e.get("SFTP_KEY_PASSPHRASE"):
                kw["passphrase"] = e["SFTP_KEY_PASSPHRASE"]
        if e.get("SFTP_PASSWORD"):
            kw["password"] = e["SFTP_PASSWORD"]
        c.connect(**kw)
        c.get_transport().set_keepalive(30)
        self.sftp = c.open_sftp()
        self.sftp.get_channel().settimeout(timeout)
        self.client = c

    def put(self, remote, data: bytes):
        if self.root:
            p = Path(self.root) / remote.lstrip("/")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return
        self.sftp.putfo(io.BytesIO(data), remote, confirm=False)

    def stat(self, remote):
        if self.root:
            p = Path(self.root) / remote.lstrip("/")
            if not p.exists():
                return None
            s = p.stat()
            return dict(size=s.st_size, mtime=s.st_mtime, mode=s.st_mode, uid=s.st_uid, gid=s.st_gid)
        try:
            a = self.sftp.stat(remote)
        except FileNotFoundError:
            return None
        except IOError as ex:
            if getattr(ex, "errno", None) == 2:
                return None
            raise
        return dict(size=a.st_size, mtime=a.st_mtime, mode=a.st_mode, uid=a.st_uid, gid=a.st_gid)

    def listdir(self, path):
        if self.root:
            p = Path(self.root) / path.lstrip("/")
            return [x.name for x in p.iterdir()] if p.exists() else []
        try:
            return self.sftp.listdir(path)
        except IOError:
            return []

    def close(self):
        for o in (self.sftp, self.client):
            try:
                if o:
                    o.close()
            except Exception:
                pass
        self.sftp = self.client = None


# --------------------------------------------------------------------------- model
class Integration:
    def __init__(self, raw, defaults, base_tag_len):
        self.raw = raw
        self.type = str(raw.get("type", "")).strip()
        self.slot = str(raw.get("slot", raw.get("sr_no", ""))).strip()
        self.serial = raw.get("serial", self.slot)
        self.label = raw.get("label") or ""
        self.integration = raw.get("integration") or self.label or f"{self.type} #{self.slot}"
        self.sender, self.receiver = raw.get("sender", ""), raw.get("receiver", "")
        self.data_type = raw.get("data_type", "")
        self.upload_path = (raw.get("upload_path") or "").strip() or None
        self.http_url = (raw.get("http_url") or "").strip() or None
        self.form_field = raw.get("http_form_field", defaults.get("http_form_field", ""))
        self.headers = raw.get("http_headers") or {}
        self.copies = raw.get("copies")
        self.max_copies = raw.get("max_copies")
        self.weight = max(1, int(raw.get("weight", 1)))
        self.name_mode = raw.get("name_mode") or defaults.get("name_mode")
        self.n = int(raw.get("replace_last_n", defaults.get("replace_last_n", 10)))
        self.suffix = raw.get("append_suffix", defaults.get("append_suffix", "")) or ""
        self.tags = [TagSpec(t, int(raw.get("tag_value_length", defaults.get("tag_value_length", base_tag_len))))
                     for t in (raw.get("replace_tags") or [])]
        self.delivery = "http" if self.http_url else ("sftp" if self.upload_path else None)
        # resolved later
        self.ok, self.skip_reason = False, ""
        self.notes = []
        self.folder = self.template_name = self.template = None
        self.mode_used = ""
        self.tag_hits = {}
        self.end_check = None

    @property
    def key(self):
        return f"{self.type} / slot {self.slot}"

    @property
    def target(self):
        return self.http_url if self.delivery == "http" else (self.upload_path or "")


def index_samples(samples_dir: Path):
    idx = {}
    for t in sorted(p for p in samples_dir.iterdir() if p.is_dir()):
        serials = {}
        for f in sorted(q for q in t.iterdir() if q.is_dir()):
            m = SERIAL_RE.match(f.name)
            if m:
                serials.setdefault(int(m.group(1)), []).append(f)
        idx[norm_key(t.name)] = (t, serials)
    return idx


def interleave(quotas, weights):
    rem = list(quotas)
    while any(r > 0 for r in rem):
        for i, w in enumerate(weights):
            for _ in range(w):
                if rem[i] > 0:
                    rem[i] -= 1
                    yield i


# --------------------------------------------------------------------------- the runner
class Runner:
    def __init__(self, cfg, cfg_path: Path, args):
        self.cfg, self.cfg_path, self.args = cfg, cfg_path, args
        self.base = cfg_path.parent
        self.mode = (args.mode or cfg.get("mode") or "functional").lower()
        if self.mode not in PRESETS:
            raise SystemExit(f"mode must be functional or performance, not '{self.mode}'")
        pre = PRESETS[self.mode]
        self.workers = int(args.workers or opt(cfg, "workers", pre["workers"]))
        self.storage = opt(cfg, "storage", pre["storage"])
        self.verify = bool(opt(cfg, "verify_remote", pre["verify_remote"]))
        self.max_per_hour = opt(cfg, "max_files_per_hour", pre["max_files_per_hour"])
        self.target_per_hour = opt(cfg, "target_files_per_hour", 20000)
        self.retries = int(opt(cfg, "retries", 2))
        self.end_check = bool(opt(cfg, "end_check", False))
        self.abort_after = int(opt(cfg, "abort_after_consecutive_failures", 0))
        fp = cfg.get("failure_policy") or {}
        self.fp_abort_s = float(fp.get("abort_after_seconds") or 0)
        self.fp_min = int(fp.get("min_failures", 10))
        self.fp_warn_s = float(fp.get("warn_after_seconds") or 0)
        self.fail_since, self.fail_n, self.fail_warned = None, 0, False
        self.abort_reason = ""
        out_mode = cfg.get("output_mode", "generate_and_upload")
        self.dry = bool(args.dry_run or cfg.get("dry_run") or out_mode == "generate_only")
        if self.dry and self.storage != "disk":
            self.storage = "disk"
        self.defaults = cfg.get("defaults", {})
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_p{os.getpid()}"   # pid: parallel runs never clash
        d = lambda k, dflt: to_path(self.base, cfg.get(k) or dflt)
        self.samples_dir = d("samples_directory", "project/samples")
        self.output_dir = d("output_directory", "project/output_files")
        self.reports_dir = d("reports_directory", "project/reports")
        self.logs_dir = d("logs_directory", "project/logs")
        self.state_dir = d("state_directory", "project/state")
        for p in (self.reports_dir, self.logs_dir, self.state_dir):
            p.mkdir(parents=True, exist_ok=True)
        self.warnings = []          # (level, message)
        self.lock = threading.RLock()
        self.log = self._make_logger()
        self.stop = threading.Event()
        self.local = threading.local()
        self.conns, self.sessions = [], []
        self.rows = []
        self.ints = []              # selected integrations (resolved or not)
        self.counter = None
        self.limiter = None
        self.active = set()
        self.consec_fail = 0
        self.reconnects = self.retried = 0
        self.per_worker, self.per_minute = Counter(), Counter()
        self.resp_kept = Counter()
        self.t_start = self.t_end = None
        self.status = "NOT STARTED"
        flt0 = build_filter(cfg, args)
        keep = {norm_key(e.get("type", "")) for e in flt0} if flt0 else None
        self.sched = None if getattr(args, "total", None) else parse_schedule(
            cfg.get("schedule"), keep, (getattr(args, "minutes", None) if getattr(args, "minutes", None) is not None else cfg.get("minutes")))
        if self.sched is None and cfg.get("schedule") and getattr(args, "total", None):
            self.log_early = f"--total {args.total} given: the schedule is not used, sending exactly {args.total} file(s)"
        self.minute_s = self.sched["minute_seconds"] if self.sched else 60.0
        self.sched_log, self.sched_state, self.last_checkpoint = [], {}, None
        if self.sched:
            self.target_per_hour = round(self.sched["total"] / (len(self.sched["minutes"]) / 60.0))
            if self.sched["note"]:
                self.log.info(self.sched["note"])
            if self.max_per_hour:
                self.warn("schedule is set, so max_files_per_hour is ignored")
                self.max_per_hour = None
            if cfg.get("type_run_settings") or cfg.get("batch_size"):
                self.log.info("schedule is set: batch_size, batch_pause_seconds and type_run_settings are not used")

    # ---- logging / warnings
    def _make_logger(self):
        lg = logging.getLogger(f"sftp_load_{self.run_id}")
        lg.setLevel(logging.INFO)
        h = logging.FileHandler(self.logs_dir / f"run_{self.run_id}.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
        return lg

    def warn(self, msg, level="WARNING"):
        with self.lock:
            self.warnings.append((level, msg))
        self.log.warning(msg) if level == "WARNING" else self.log.error(msg)

    # ---- selection
    def select(self):
        cfg, args = self.cfg, self.args
        allints = [Integration(r, self.defaults, int(self.defaults.get("tag_value_length", 10)))
                   for r in cfg.get("integrations", [])]
        flt = build_filter(cfg, args)
        chosen = []
        for it in allints:
            if not flt:
                chosen.append(it)
                continue
            for e in flt:
                if norm_key(e.get("type", "")) != norm_key(it.type):
                    continue
                s = e.get("slot", e.get("slots", "all"))
                slots = s if isinstance(s, list) else [s]
                if any(str(x).lower() == "all" or str(x) == it.slot for x in slots):
                    chosen.append(it)
                    break
        self.ints = chosen

    def resolve(self):
        if not self.samples_dir.is_dir():
            raise SystemExit(f"Samples folder not found: {self.samples_dir}")
        index = index_samples(self.samples_dir)
        policy = self.cfg.get("upload_path_policy") or {}
        prefix = (policy.get("required_prefix") or "").replace("\\", "/")
        for it in self.ints:
            def skip(reason):
                it.ok, it.skip_reason = False, reason
                self.warn(f"SKIPPED {it.key} [{it.integration}]: {reason}")
            if it.delivery is None and self.dry:
                it.delivery = "none"                              # generate-only: no destination needed
            if it.delivery is None:
                skip("no upload_path or http_url configured (set one, or use --dry-run to only generate files)")
                continue
            if it.delivery == "sftp" and prefix:
                up = it.upload_path.replace("\\", "/")
                if not (up == prefix.rstrip("/") or up.startswith(prefix.rstrip("/") + "/")):
                    skip(f"upload_path '{it.upload_path}' is outside required prefix '{prefix}'")
                    continue
            if it.delivery == "http" and not re.match(r"^https?://", it.http_url, re.I):
                skip("http_url must start with http:// or https://")
                continue
            fallback = str(self.cfg.get("sample_fallback", "off")).lower() == "dummy"
            folder, files, why = None, [], ""
            tinfo = index.get(norm_key(it.type))
            if not tinfo:
                why = f"no folder for type '{it.type}' under {self.samples_dir}"
            else:
                try:
                    serial = int(str(it.serial).strip())
                except ValueError:
                    serial, why = None, f"serial '{it.serial}' is not a number"
                if serial is not None:
                    folders = tinfo[1].get(serial, [])
                    if not folders:
                        why = f"no folder starting with '{serial} -' under {tinfo[0].name}"
                    elif len(folders) > 1:
                        why = f"{len(folders)} folders start with '{serial} -' under {tinfo[0].name}; ambiguous"
                    else:
                        folder = folders[0]
                        ok_file = lambda f: f.is_file() and f.name.lower() not in IGNORED_FILES and not f.name.startswith("~$")
                        files = sorted(f for f in folder.iterdir() if ok_file(f))
                        if not files:                          # nothing directly inside: accept files one folder level down
                            files = sorted(f for d in folder.iterdir() if d.is_dir() for f in d.iterdir() if ok_file(f))
                            if files:
                                it.notes.append(f"{it.key}: no file directly in '{folder.name}', using "
                                                f"'{files[0].relative_to(folder)}'")
                        if not files:
                            items = list(folder.iterdir())
                            why = (f"sample folder has no file. Looked in: {folder.resolve()}  ({len(items)} item(s) there, "
                                   f"{sum(1 for x in items if x.is_dir())} sub-folder(s)); is this the same folder you see in your editor?")
            if not files and fallback:
                dummy = self.find_dummy()
                if dummy:
                    files, folder = [dummy], dummy.parent
                    it.notes.append(f"{it.key}: no sample file ({why}); using dummy file '{dummy.name}' (sample_fallback = dummy)")
            if not files:
                skip(why)
                continue
            if len(files) > 1:
                it.notes.append(f"{it.key}: {len(files)} files in sample folder, using '{files[0].name}'")
            it.folder, it.template_name = folder, files[0].name
            it.template = files[0].read_bytes()
            stem, _ = split_ext(it.template_name)
            it.mode_used = pick_mode(stem, it.n, it.name_mode)
            for sp in it.tags:
                hits = len(sp.rx.findall(it.template))
                it.tag_hits[sp.tag] = hits
                if hits == 0:
                    it.notes.append(f"{it.key}: tag '{sp.tag}' not found in sample file (skipped)")
            it.ok = True

    def find_dummy(self):
        """File used when an integration has no sample of its own (sample_fallback = dummy)."""
        if getattr(self, "_dummy", None) is None:
            want = self.cfg.get("sample_fallback_file")
            root = self.samples_dir
            good = lambda f: f.is_file() and f.name.lower() not in IGNORED_FILES and not f.name.startswith("~$")
            pick = None
            if want:
                w = Path(want)
                cand = [w if w.is_absolute() else root / w]
                cand += sorted(f for f in root.rglob("*") if good(f) and f.name.lower() == w.name.lower())
                pick = next((c for c in cand if c.is_file()), None)
            if pick is None:
                pick = next(iter(sorted(f for f in root.iterdir() if good(f))), None) or \
                       next(iter(sorted(f for f in root.rglob("*") if good(f))), None)
            self._dummy = pick or False
        return self._dummy or None

    # ---- plan
    def build_plan(self):
        ok = [it for it in self.ints if it.ok]
        if getattr(self, "log_early", None):
            print(self.log_early)
        if self.sched:
            if self.args.total or self.cfg.get("total"):
                self.warn("schedule is set, so --total / total is ignored (the schedule decides the file count)")
            total = self.sched["total"]
        else:
            total = self.args.total if self.args.total else self.cfg.get("total")
        n = len(ok)
        quotas = [0] * n
        tt = self.sched["type_totals"] if self.sched else {}
        if tt:
            have = {norm_key(it.type) for it in ok}
            for tname, ttotal in tt.items():
                idxs = [i for i, it in enumerate(ok) if norm_key(it.type) == norm_key(tname)]
                if not idxs:
                    self.warn(f"type_totals: no ready integration for type '{tname}'; its {ttotal:,} files are dropped")
                    continue
                caps = [min(int(ok[i].max_copies), ttotal) if ok[i].max_copies else ttotal for i in idxs]
                for k, j in enumerate(interleave(caps, [ok[i].weight for i in idxs])):
                    if k >= ttotal:
                        break
                    quotas[idxs[j]] += 1
            for t in sorted({it.type for it in ok if norm_key(it.type) not in {norm_key(x) for x in tt}}):
                self.warn(f"type_totals has no entry for type '{t}', so it sends nothing in this run")
        elif total and total < len(ok):                          # fewer files than integrations: one each, spread over types
            seen_t, order = Counter(), []
            for i, it in enumerate(ok):
                seen_t[it.type] += 1
                order.append((seen_t[it.type], i))               # 1st of every type, then 2nd of every type, ...
            for _, i in sorted(order)[:total]:
                quotas[i] = 1
        elif total:
            caps = [min(int(it.max_copies), total) if it.max_copies else total for it in ok]
            for k, i in enumerate(interleave(caps, [it.weight for it in ok])):
                if k >= total:
                    break
                quotas[i] += 1
        else:
            dflt = int(self.args.copies or self.defaults.get("copies", 100))
            quotas = [int(it.copies if it.copies is not None else dflt) for it in ok]
        for it, q in zip(ok, quotas):
            it.planned = q
            it.counts = Counter()
            if q:
                for note in it.notes:                          # notices only for integrations that really run
                    self.warn(note)
        self.stride = 1 + max([len(it.tags) for it in ok] + [0])
        self.streams = {}
        if self.sched:
            seq, items = Counter(), []
            for gi in mix_order(quotas):                                # every type and integration spread through the run
                seq[gi] += 1
                items.append((gi, seq[gi]))
            self.streams["SCHEDULE"] = (items, 0, 0)
            self.clip_schedule(len(items))
        else:
            by_type = {}
            for i, it in enumerate(ok):
                by_type.setdefault(it.type, []).append(i)
            trs = self.cfg.get("type_run_settings") or {}
            for t, idxs in by_type.items():
                st = next((v for k, v in trs.items() if norm_key(k) == norm_key(t)), {})
                bs = int(st.get("batch_size") or self.cfg.get("batch_size") or 100)
                pause = float(st.get("batch_pause_seconds", self.cfg.get("batch_pause_seconds", 0)) or 0)
                local_q = [quotas[i] for i in idxs]
                seq = Counter()
                items = []
                for j in interleave(local_q, [ok[i].weight for i in idxs]):
                    gi = idxs[j]
                    seq[gi] += 1
                    items.append((gi, seq[gi]))
                self.streams[t] = (items, bs, pause)
        self.ok_ints = ok
        self.planned = sum(quotas)

    def clip_schedule(self, n):
        """Fewer files can be generated than planned (a type has no ready integration, max_copies caps):
        scale every minute down proportionally so the run keeps its full duration."""
        S = self.sched
        old = S["total"]
        if n >= old:
            return
        self.warn(f"Only {n:,} of the {old:,} scheduled files can be generated (a type without a ready integration, "
                  f"or max_copies caps); every minute is scaled down proportionally")
        acc, done, out = 0, 0, []
        for c in S["minutes"]:
            acc += c * n
            v = acc // old - done
            out.append(v)
            done += v
        S["minutes"], S["total"] = out, n
        S["totals"] = [sum(out[h * 60:(h + 1) * 60]) for h in range(S["hours"])]

    def describe_schedule(self):
        S = self.sched
        m = S["minutes"]
        peak_s = max(m) / 60.0
        lines = ([S["note"]] if S["note"] else []) + [f"Schedule: {S['hours']} hour(s), profile {S['profile']}, {S['lo']}-{S['hi']} files/min, "
                 f"seed {S['seed_used']}{'' if S['seed'] is not None else ' (random; put it in config to repeat this pattern)'}",
                 "  hourly totals: " + ", ".join(f"{t:,}" for t in S["totals"]) + f"  = {S['total']:,} files over "
                 f"{fmt_dur(len(m) * 60)}",
                 f"  per minute: min {min(m):,}, avg {sum(m) / len(m):,.0f}, max {max(m):,}; peak {peak_s:.1f} files/s "
                 f"(with {self.workers} workers each upload must finish within {self.workers / peak_s * 1000:,.0f} ms to keep up)",
                 "  first minutes: " + ", ".join(f"{x:,}" for x in m[:12]) + (" ..." if len(m) > 12 else "")]
        if S["type_totals"]:
            lines.append("  per type: " + ", ".join(f"{k} {v:,}" for k, v in S["type_totals"].items()))
        if self.fp_abort_s:
            lines.append(f"  failure policy: warn after {self.fp_warn_s:.0f}s, stop after {self.fp_abort_s:.0f}s of "
                         f"continuous failures (at least {self.fp_min} files)")
        if self.verify:
            lines.append("  NOTE: per-file verification is on (functional mode); it slows every upload. "
                         "For long scheduled runs use --mode performance.")
        return lines

    def schedule_stream(self, tname):
        """One dispatcher: sends the planned number of files each minute, spread over that minute.
        Anything not sent in time (workers busy) is carried into the next minute."""
        items = self.streams[tname][0]
        S, mp = self.sched, self.sched["minutes"]
        cum, c = [], 0
        for x in mp:
            c += x
            cum.append(c)
        t0, sent, hours, ms = self.t_start, 0, S["hours"], self.minute_s
        pace = not self.dry or getattr(self.args, "paced", False)     # generate-only runs skip the clock

        def until(t):
            if pace:
                self.stop.wait(max(0.0, t - time.time()))
        self.active.add(tname)
        try:
            for m, plan_n in enumerate(mp):
                if self.stop.is_set():
                    break
                m_start = t0 + m * ms
                until(m_start)
                self.sched_state = {"minute": m + 1, "plan": plan_n, "hour": m // 60 + 1, "of": hours}
                due = cum[m] - sent                                   # this minute's plan + any carried shortfall
                if due > 0:
                    base = self.counter.reserve(due * self.stride)
                    gap = ms / due
                    for j in range(due):
                        if self.stop.is_set():
                            break
                        while sent - len(self.rows) > S["max_backlog"] and not self.stop.is_set():
                            self.stop.wait(0.05)                      # workers are behind: hold back, do not pile up
                        until(m_start + j * gap)
                        if self.stop.is_set():
                            break
                        gi, seq = items[sent]
                        self.pool.submit(self.process, gi, base + j * self.stride, m + 1, seq)
                        sent += 1
                until(m_start + ms)                                    # close the minute
                done = len(self.rows)
                lag = cum[m] - done
                behind = pace and lag > max(2 * self.workers, int(0.05 * plan_n))
                self.sched_log.append(dict(m=m, plan=plan_n, sent=sent, done=done, lag=lag, due=cum[m], behind=behind))
                if behind:
                    self.log.warning("minute %d (hour %d) behind plan: planned %d, completed so far %d of %d due",
                                     m + 1, m // 60 + 1, plan_n, done, cum[m])
                if (m + 1) % 60 == 0 or m == len(mp) - 1:
                    self.checkpoint(m // 60 + 1, hours)
        except Exception as ex:
            self.warn(f"Scheduler crashed: {type(ex).__name__}: {ex}", "ERROR")
        finally:
            self.active.discard(tname)

    def checkpoint(self, hour_no, hours):
        """End-of-hour report written while the run continues, so a crash later still leaves everything so far."""
        try:
            with self.lock:
                snap = copy.copy(self)
                snap.rows = list(self.rows)
                snap.per_minute, snap.per_worker = Counter(self.per_minute), Counter(self.per_worker)
                try:
                    self.mf.flush()
                except Exception:
                    pass
            snap.t_end = time.time()
            snap.status = f"CHECKPOINT after hour {hour_no} of {hours} (run still in progress)"
            path = write_report(snap, suffix="_checkpoint")
            self.last_checkpoint = path
            self.log.info("checkpoint hour %d/%d: %d files recorded -> %s", hour_no, hours, len(snap.rows), path)
        except Exception as ex:
            self.warn(f"Checkpoint after hour {hour_no} failed: {type(ex).__name__}: {ex}")

    # ---- connections
    def tl_sftp(self):
        c = getattr(self.local, "sftp", None)
        if c is None:
            c = SftpConn()
            c.open()
            self.local.sftp = c
            with self.lock:
                self.conns.append(c)
        return c

    def drop_sftp(self):
        c = getattr(self.local, "sftp", None)
        if c:
            c.close()
            self.local.sftp = None
            with self.lock:
                self.reconnects += 1

    def tl_http(self):
        s = getattr(self.local, "http", None)
        if s is None:
            import requests
            s = requests.Session()
            ad = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2)
            s.mount("http://", ad)
            s.mount("https://", ad)
            self.local.http = s
            with self.lock:
                self.sessions.append(s)
        return s

    # ---- one file
    def process(self, gi, base_num, batch_no, seq):
        it = self.ok_ints[gi]
        if self.stop.is_set():
            return
        if self.limiter:
            self.limiter.acquire(self.stop)
            if self.stop.is_set():
                return
        wname = threading.current_thread().name.replace("ThreadPoolExecutor", "worker")
        row = dict(gi=gi, seq=seq, batch=batch_no, gen="", status="FAILED", http="", lsize="", rsize="",
                   verify="", mtime="", mtime_e=None, perms="", owner="", checked="", err="", att=0,
                   worker=wname, sha="", lpath="", tags="", resp="")
        try:
            number = digits(base_num, it.n)
            name = build_name(it.template_name, number, it.n, it.suffix, it.mode_used)
            data, changed = apply_tags(it.template, it.tags, base_num + 1)
            row.update(gen=name, lsize=len(data), tags=",".join(changed))
            if self.storage == "disk":
                p = self.run_out / it.folder.parent.name / it.folder.name / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                row["lpath"] = str(p)
                row["sha"] = hashlib.sha256(data).hexdigest()
            if self.dry:
                row["status"], row["verify"] = "GENERATED", "NOT_SENT"
            else:
                self.deliver(it, name, data, row)
        except Exception as ex:  # never let one file stop the run
            row["status"], row["err"] = "FAILED", f"{type(ex).__name__}: {ex}"[:300]
        row["t"] = time.time()
        self.record(it, row)

    def deliver(self, it, name, data, row):
        last_err = ""
        for attempt in range(1, self.retries + 2):
            row["att"] = attempt
            try:
                if it.delivery == "http":
                    self.send_http(it, name, data, row)
                else:
                    self.send_sftp(it, name, data, row)
                return
            except Exception as ex:
                last_err = f"{type(ex).__name__}: {ex}"[:300]
                if it.delivery == "sftp":
                    self.drop_sftp()
                if attempt <= self.retries and not self.stop.is_set():
                    with self.lock:
                        self.retried += 1
                    self.stop.wait(min(0.5 * 2 ** (attempt - 1), 5))
                    continue
        row["status"], row["err"] = "FAILED", last_err

    def send_sftp(self, it, name, data, row):
        conn = self.tl_sftp()
        remote = it.upload_path.rstrip("/\\") + "/" + name
        conn.put(remote, data)
        if not self.verify:
            row["status"], row["verify"] = "PASSED", "UPLOADED"
            return
        st = conn.stat(remote)
        row["checked"] = utc()
        if st is None:
            row["status"], row["verify"] = "UNVERIFIED", "NOT_FOUND_AFTER_UPLOAD (poller may have taken it)"
            return
        row.update(rsize=st["size"], mtime=utc(st["mtime"]), mtime_e=st["mtime"],
                   perms=statmod.filemode(st["mode"]), owner=f'{st["uid"]}:{st["gid"]}')
        if st["size"] == len(data):
            row["status"], row["verify"] = "PASSED", "VERIFIED"
        else:
            row["status"], row["verify"] = "FAILED", "SIZE_MISMATCH"
            row["err"] = f"remote size {st['size']} != sent {len(data)}"

    def send_http(self, it, name, data, row):
        s = self.tl_http()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        headers = dict(self.cfg.get("http_headers") or {})
        headers.update(it.headers)
        r = s.post(it.http_url, files={it.form_field: (name, data, ctype)}, headers=headers,
                   timeout=(10, 60), verify=bool(opt(self.cfg, "http_verify_tls", True)))
        row["http"] = r.status_code
        ok = 200 <= r.status_code < 300
        if not ok or self.resp_kept[row["gi"]] < 3:
            self.resp_kept[row["gi"]] += 1
            row["resp"] = (r.text or "")[:200].replace("\r", " ").replace("\n", " ")
        if ok:
            row["status"], row["verify"] = "PASSED", "HTTP_ACCEPTED"
        else:
            row["status"], row["verify"], row["err"] = "FAILED", "HTTP_REJECTED", f"HTTP {r.status_code}"

    def record(self, it, row):
        with self.lock:
            self.rows.append(row)
            it.counts["processed"] += 1
            it.counts[row["status"]] += 1
            self.per_worker[row["worker"]] += 1
            self.per_minute[int((row["t"] - self.t_start) // self.minute_s)] += 1
            if row["status"] == "FAILED":
                self.consec_fail += 1
                if self.fail_since is None:
                    self.fail_since, self.fail_n, self.fail_warned = row["t"], 0, False
                self.fail_n += 1
                span = row["t"] - self.fail_since
                if self.fp_warn_s and span >= self.fp_warn_s and not self.fail_warned:
                    self.fail_warned = True
                    self.warn(f"Every file has failed for {span:.0f}s ({self.fail_n} failures in a row, last error: "
                              f"{row['err'][:120]}). The run keeps going.", "ERROR")
                if (self.fp_abort_s and span >= self.fp_abort_s and self.fail_n >= self.fp_min
                        and not self.stop.is_set()):
                    self.abort_reason = f"every file failed for {span:.0f}s ({self.fail_n} in a row)"
                    self.stop.set()
                    self.warn(f"Stopped: {self.abort_reason} (failure_policy.abort_after_seconds)", "ERROR")
            else:
                self.consec_fail = 0
                self.fail_since = None
            self.write_manifest_row(it, row)
            if self.abort_after and self.consec_fail >= self.abort_after and not self.stop.is_set():
                self.abort_reason = f"{self.consec_fail} consecutive failures"
                self.stop.set()
                self.warn(f"Stopped: {self.consec_fail} consecutive failures (abort_after_consecutive_failures)", "ERROR")
        if row["status"] == "FAILED":
            self.log.error("%s | %s | %s", it.key, row["gen"], row["err"])

    # ---- manifest
    def open_manifest(self):
        self.manifest_path = self.reports_dir / f"manifest_{self.mode}_{self.run_id}.csv"
        self.mf = open(self.manifest_path, "w", newline="", encoding="utf-8")
        self.mw = csv.writer(self.mf)
        self.mw.writerow(["run_id", "type", "slot", "integration", "seq", "batch", "original_file", "generated_file",
                          "status", "verification", "http_status", "local_size", "remote_size", "destination",
                          "remote_modified_utc", "permissions", "owner", "checked_utc", "attempts", "worker",
                          "sha256", "local_path", "tags_replaced", "error"])
        self._mrows = 0

    def write_manifest_row(self, it, row):
        dest = it.http_url if it.delivery == "http" else (
            it.upload_path.rstrip("/") + "/" + row["gen"] if it.upload_path else "(generate only)")
        self.mw.writerow([self.run_id, it.type, it.slot, it.integration, row["seq"], row["batch"], it.template_name,
                          row["gen"], row["status"], row["verify"], row["http"], row["lsize"], row["rsize"], dest,
                          row["mtime"], row["perms"], row["owner"], row["checked"], row["att"], row["worker"],
                          row["sha"], row["lpath"], row["tags"], row["err"]])
        self._mrows += 1
        if self._mrows % 200 == 0:
            self.mf.flush()

    # ---- streams
    def stream(self, tname):
        items, bs, pause = self.streams[tname]
        self.active.add(tname)
        try:
            for b0 in range(0, len(items), bs):
                if self.stop.is_set():
                    break
                chunk = items[b0:b0 + bs]
                batch_no = b0 // bs + 1
                base = self.counter.reserve(len(chunk) * self.stride)
                futs = [self.pool.submit(self.process, gi, base + j * self.stride, batch_no, seq)
                        for j, (gi, seq) in enumerate(chunk)]
                pending = set(futs)
                while pending:
                    _, pending = wait(pending, timeout=0.5)
                if b0 + bs < len(items) and pause > 0:
                    self.stop.wait(pause)
        except Exception as ex:
            self.warn(f"Stream {tname} crashed: {type(ex).__name__}: {ex}", "ERROR")
        finally:
            self.active.discard(tname)

    # ---- progress
    def progress_line(self):
        done = len(self.rows)
        el = max(time.time() - self.t_start, 0.001)
        rate = done / el
        failed = sum(1 for r in self.rows if r["status"] == "FAILED") if done < 5000 else \
            sum(it.counts["FAILED"] for it in self.ok_ints)
        ok = sum(it.counts["PASSED"] + it.counts["GENERATED"] for it in self.ok_ints)
        eta = (self.planned - done) / rate if rate > 0 else 0
        pct = 100.0 * done / self.planned if self.planned else 100
        extra = ""
        if self.sched:
            eta = max(len(self.sched["minutes"]) * self.minute_s - el, 0)
            st = self.sched_state
            if st:
                extra = f" | h{st['hour']}/{st['of']} min {st['minute']} plan {st['plan']}/min"
        return (f"[{fmt_dur(el)}] {done:,}/{self.planned:,} ({pct:.1f}%) ok {ok:,} fail {failed:,} | "
                f"{rate:.1f} files/s = {rate * 3600:,.0f}/h | ETA {fmt_dur(eta)}{extra} | "
                f"active: {', '.join(sorted(self.active)) or '-'}")

    # ---- main
    def run(self):
        self.select()
        self.resolve()
        load_env(self.base / ".env")
        self.build_plan()
        skipped = [it for it in self.ints if not it.ok]
        print(f"\nsftp_load {VERSION} | mode={self.mode} storage={self.storage} workers={self.workers} "
              f"{'DRY-RUN ' if self.dry else ''}run_id={self.run_id}")
        print(f"Selected {len(self.ints)} integrations, {len(self.ok_ints)} ready, {len(skipped)} skipped, "
              f"{self.planned:,} files planned")
        if self.sched:
            for ln in self.describe_schedule():
                print(ln)
        else:
            for t, (items, bs, pause) in self.streams.items():
                print(f"  {t:<14} {len(items):>7,} files  batch {bs} / pause {pause}s")
        for it in skipped:
            print(f"  ! skipped {it.key}: {it.skip_reason}")
        if not self.ok_ints or not self.planned:
            print("Nothing to run.")
            return 2
        sftp_n = sum(1 for it in self.ok_ints if it.delivery == "sftp")
        if not self.dry:
            if sftp_n and not os.environ.get("SFTP_LOCAL_ROOT"):
                miss = [k for k in ("SFTP_HOST", "SFTP_USER") if not os.environ.get(k)]
                if miss or not (os.environ.get("SFTP_PASSWORD") or os.environ.get("SFTP_KEY_FILE")):
                    print(f"Missing in .env: {', '.join(miss) or 'SFTP_PASSWORD or SFTP_KEY_FILE'}")
                    return 2
            pol = self.cfg.get("upload_path_policy") or {}
            if pol.get("prompt_before_prefix") and not self.args.yes:
                print(f"\nUploads will go under: {pol.get('required_prefix')}  "
                      f"(SFTP integrations: {sftp_n}, HTTP: {len(self.ok_ints) - sftp_n})")
                if not sys.stdin.isatty():
                    print("Not interactive; re-run with --yes to confirm.")
                    return 2
                if input("Type YES to continue: ").strip().upper() != "YES":
                    print("Cancelled.")
                    return 2
        if self.cfg.get("cleanup_output_before_run", False) and self.output_dir.is_dir() and not self.dry:
            for d in self.output_dir.glob("_run_*"):
                mark = d / ".running"
                if mark.exists() and time.time() - mark.stat().st_mtime < 86400:
                    continue                                    # another run is still using this folder
                shutil.rmtree(d, ignore_errors=True)
        self.run_out = self.output_dir / f"_run_{self.run_id}"
        if self.storage == "disk":
            self.run_out.mkdir(parents=True, exist_ok=True)
            (self.run_out / ".running").write_text(str(os.getpid()))
        self.counter = UniqueCounter(self.state_dir / "counter.json", self.warn)
        if self.max_per_hour:
            self.limiter = RateLimiter(float(self.max_per_hour))
        self.t_start = time.time()
        self.open_manifest()
        self.pool = ThreadPoolExecutor(max_workers=self.workers)
        hits = {"n": 0}

        def on_sigint(sig, frm):
            hits["n"] += 1
            if hits["n"] == 1:
                self.stop.set()
                print("\nCtrl+C: finishing in-flight files, then writing the report (Ctrl+C again to quit now)...")
            else:
                print("\nSecond Ctrl+C: exiting immediately. manifest.csv has everything done so far.")
                try:
                    self.mf.flush()
                except Exception:
                    pass
                os._exit(130)
        signal.signal(signal.SIGINT, on_sigint)
        runner_fn = self.schedule_stream if self.sched else self.stream
        threads = [threading.Thread(target=runner_fn, args=(t,), daemon=True) for t in self.streams]
        tty = sys.stdout.isatty()
        last_perm = 0
        for th in threads:
            th.start()
        while any(th.is_alive() for th in threads):
            time.sleep(0.5)
            now = time.time()
            if tty:
                sys.stdout.write("\r" + self.progress_line()[:160].ljust(160))
                sys.stdout.flush()
            if now - last_perm >= (60 if tty else 10):
                last_perm = now
                line = self.progress_line()
                self.log.info(line)
                if tty:
                    sys.stdout.write("\n")
                else:
                    print(line, flush=True)
        self.pool.shutdown(wait=True)
        self.t_end = time.time()
        if tty:
            print()
        if self.end_check and not self.dry:
            self.do_end_check()
        for c in self.conns:
            c.close()
        for s in self.sessions:
            s.close()
        self.mf.close()
        if self.storage == "disk":
            try:
                (self.run_out / ".running").unlink()
            except OSError:
                pass
        failed = sum(1 for r in self.rows if r["status"] == "FAILED")
        self.status = (f"ABORTED: {self.abort_reason}" if self.abort_reason else
                       "INTERRUPTED (Ctrl+C)") if self.stop.is_set() and len(self.rows) < self.planned else \
            ("COMPLETED WITH ERRORS" if failed else "COMPLETED")
        from_report = write_report(self)
        done = len(self.rows)
        el = max(self.t_end - self.t_start, 0.001)
        print(f"\n{self.status}: {done:,}/{self.planned:,} files in {fmt_dur(el)} "
              f"({done / el:.1f}/s = {done / el * 3600:,.0f}/h), failed {failed:,}")
        print(f"Report:   {from_report}\nManifest: {self.manifest_path}\nLog:      {self.logs_dir / ('run_' + self.run_id + '.log')}")
        if self.storage == "disk":
            print(f"Files:    {self.run_out}")
        if self.sched:
            lagged = sum(1 for x in self.sched_log if x["behind"])
            print(f"Schedule: {len(self.sched_log)}/{len(self.sched['minutes'])} minutes run, "
                  f"{lagged} minute(s) behind plan (listed in the report)")
        if self.warnings:
            print(f"Warnings/errors: {len(self.warnings)} (see report)")
        if self.abort_reason:
            return 3
        return 130 if self.stop.is_set() and len(self.rows) < self.planned else (1 if failed else 0)

    def do_end_check(self):
        try:
            conn = SftpConn()
            conn.open()
            self.conns.append(conn)
            gens = {}
            for r in self.rows:
                it = self.ok_ints[r["gi"]]
                if it.delivery == "sftp" and r["status"] in ("PASSED", "UNVERIFIED"):
                    gens.setdefault(r["gi"], []).append(r["gen"])
            for gi, names in gens.items():
                it = self.ok_ints[gi]
                present = set(conn.listdir(it.upload_path))
                here = sum(1 for n in names if n in present)
                it.end_check = dict(sent=len(names), still_on_remote=here, picked_up=len(names) - here)
        except Exception as ex:
            self.warn(f"End-of-run directory check failed: {ex}")


# --------------------------------------------------------------------------- report
def ls_svg(it, rows, runner):
    lines = [f"$ ls -l {it.upload_path}"]
    good = [r for r in rows if r["mtime_e"] is not None][:25]
    lines.append(f"total {len(rows)}")
    for r in good:
        uid, gid = (r["owner"] or "0:0").split(":")
        mt = datetime.fromtimestamp(r["mtime_e"], timezone.utc).strftime("%b %d %H:%M")
        lines.append(f"{r['perms']} 1 {uid:<6} {gid:<6} {r['rsize']:>8} {mt} {r['gen']}")
    if len(rows) > len(good):
        lines.append(f"... {len(rows) - len(good)} more (see table)")
    w = int(max(len(x) for x in lines) * 7.4 + 28)
    h = len(lines) * 17 + 22
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" rx="8" fill="#0d1117"/>']
    for i, ln in enumerate(lines):
        col = "#7ee787" if i == 0 else "#c9d1d9"
        out.append(f'<text x="14" y="{24 + i * 17}" fill="{col}" font-family="Consolas,Menlo,monospace" '
                   f'font-size="12" xml:space="preserve">{htmlmod.escape(ln)}</text>')
    out.append("</svg>")
    return "".join(out)


def chart_svg(per_min, target_pm, planned=None):
    n = max(len(per_min), 1)
    W, H, pl, pb, pt = 900, 230, 56, 30, 14
    planned = (planned or [])[:n]
    mx = max(max(per_min or [0]), target_pm or 0, max(planned or [0]), 1)
    bw = (W - pl - 10) / n
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" style="max-width:900px">',
         f'<rect width="{W}" height="{H}" fill="#fff"/>']
    for i, v in enumerate(per_min):
        h = (H - pb - pt) * v / mx
        s.append(f'<rect x="{pl + i * bw + 1:.1f}" y="{H - pb - h:.1f}" width="{max(bw - 2, 1):.1f}" '
                 f'height="{h:.1f}" fill="#3b82f6"><title>minute {i + 1}: {v} files</title></rect>')
    if planned:
        pts = " ".join(f"{pl + i * bw + bw / 2:.1f},{H - pb - (H - pb - pt) * v / mx:.1f}" for i, v in enumerate(planned))
        s.append(f'<polyline points="{pts}" fill="none" stroke="#f59e0b" stroke-width="1.6"/>')
        s.append(f'<text x="{pl + 6}" y="{pt + 10}" font-size="11" fill="#b45309">orange line = planned per minute, '
                 f'blue bars = completed</text>')
    if target_pm:
        y = H - pb - (H - pb - pt) * target_pm / mx
        s.append(f'<line x1="{pl}" x2="{W - 8}" y1="{y:.1f}" y2="{y:.1f}" stroke="#dc2626" stroke-dasharray="6 4"/>')
        s.append(f'<text x="{W - 10}" y="{y - 4:.1f}" text-anchor="end" font-size="11" fill="#dc2626">'
                 f'target {target_pm:.0f}/min</text>')
    s.append(f'<line x1="{pl}" x2="{pl}" y1="{pt}" y2="{H - pb}" stroke="#94a3b8"/>')
    s.append(f'<line x1="{pl}" x2="{W - 8}" y1="{H - pb}" y2="{H - pb}" stroke="#94a3b8"/>')
    s.append(f'<text x="{pl - 6}" y="{pt + 8}" text-anchor="end" font-size="11" fill="#475569">{mx:.0f}</text>')
    s.append(f'<text x="{pl - 6}" y="{H - pb}" text-anchor="end" font-size="11" fill="#475569">0</text>')
    s.append(f'<text x="{(W + pl) / 2}" y="{H - 8}" text-anchor="middle" font-size="11" fill="#475569">'
             f'minute of run (files per minute)</text></svg>')
    return "".join(s)


def sched_block(rn, per_min):
    S, e = rn.sched, htmlmod.escape
    log = list(rn.sched_log)
    out = [f"<p><b>Schedule</b>: {S['hours']} hour(s), profile <b>{e(S['profile'])}</b>, {S['lo']}-{S['hi']} files/min, "
           f"seed <b>{S['seed_used']}</b> (put <code>\"seed\": {S['seed_used']}</code> in the config to repeat this exact pattern).</p>"]
    out.append('<table style="margin-top:8px"><tr><th>Hour</th><th class="num">Planned</th><th class="num">Dispatched</th>'
               '<th class="num">Completed in the hour</th><th class="num">Still behind at hour end</th></tr>')
    for h in range(S["hours"]):
        ent = [x for x in log if x["m"] // 60 == h]
        if not ent:
            out.append(f'<tr><td>{h + 1}</td><td class="num">{S["totals"][h]:,}</td><td colspan="3" class="m">not reached</td></tr>')
            continue
        prev = next((x for x in reversed(log) if x["m"] < h * 60), None)
        sent = ent[-1]["sent"] - (prev["sent"] if prev else 0)
        done = ent[-1]["done"] - (prev["done"] if prev else 0)
        part = "" if len(ent) == 60 or ent[-1]["m"] == len(S["minutes"]) - 1 else " (partial)"
        out.append(f'<tr><td>{h + 1}{part}</td><td class="num">{S["totals"][h]:,}</td><td class="num">{sent:,}</td>'
                   f'<td class="num">{done:,}</td><td class="num">{max(ent[-1]["lag"], 0):,}</td></tr>')
    out.append("</table>")
    bad = [x for x in log if x["behind"]]
    if bad:
        out.append(f'<p style="margin-top:10px"><b>{len(bad)} minute(s) ran behind plan</b> '
                   f'(the shortfall was carried into the next minute):</p><table><tr><th>Hour</th><th>Minute</th>'
                   f'<th class="num">Planned</th><th class="num">Completed so far</th><th class="num">Due so far</th>'
                   f'<th class="num">Behind by</th></tr>')
        for x in bad[:40]:
            out.append(f'<tr><td>{x["m"] // 60 + 1}</td><td>{x["m"] % 60 + 1}</td><td class="num">{x["plan"]:,}</td>'
                       f'<td class="num">{x["done"]:,}</td><td class="num">{x["due"]:,}</td><td class="num">{x["lag"]:,}</td></tr>')
        if len(bad) > 40:
            out.append(f'<tr><td colspan="6" class="m">... and {len(bad) - 40} more (see the run log)</td></tr>')
        out.append("</table>")
    else:
        out.append('<p style="margin-top:10px">No minute ran behind plan.</p>')
    return "".join(out)


def build_data(rn: Runner):
    el = max((rn.t_end or time.time()) - rn.t_start, 0.001)
    done = len(rn.rows)
    cnt = Counter(r["status"] for r in rn.rows)
    planned = rn.planned
    rate_h = done / el * 3600
    by_gi = {}
    for r in rn.rows:
        by_gi.setdefault(r["gi"], []).append(r)
    integrations, types, newidx = [], {}, {}
    for gi, it in enumerate(rn.ok_ints):
        if not it.planned and gi not in by_gi:
            continue                                   # selected in the config but got no files in this run: not reported
        newidx[gi] = len(integrations)
        rows = by_gi.get(gi, [])
        c = Counter(r["status"] for r in rows)
        proc = len(rows)
        failed, unv = c["FAILED"], c["UNVERIFIED"]
        status = ("FAIL" if failed else "SKIPPED" if proc == 0 else
                  "INCOMPLETE" if proc < it.planned else "PASS")
        first_ok = next((r for r in rows if r["status"] in ("PASSED", "UNVERIFIED", "GENERATED")), None)
        first_bad = next((r for r in rows if r["status"] == "FAILED"), None)
        kv = [["Delivery", {"http": "HTTP multipart POST", "none": "Generate only (no destination)"}.get(it.delivery, "SFTP upload")],
              ["Target", it.target], ["Sample folder", it.folder.name], ["Sample file", it.template_name],
              ["Naming rule used", f"{it.mode_used} (last {it.n} digits/chars, suffix '{it.suffix}')"]]
        if first_ok:
            kv.append(["Example", f"{it.template_name}  ->  {first_ok['gen']}"])
        if it.delivery == "http":
            kv.append(["Form field name", repr(it.form_field)])
            if first_ok and first_ok["http"] != "":
                kv.append(["First response", f"HTTP {first_ok['http']} {first_ok['resp']}"])
        if it.tags:
            kv.append(["Replace tags (occurrences in sample)",
                       ", ".join(f"{t}={n}" for t, n in it.tag_hits.items())])
        kv.append(["Attempted / passed / failed / unverified", f"{proc} / {c['PASSED']} / {failed} / {unv}"])
        if first_bad:
            kv.append(["First failure", f"{first_bad['gen']}: {first_bad['err']} {first_bad['resp']}"])
        if it.end_check:
            e = it.end_check
            kv.append(["End-of-run directory check",
                       f"sent {e['sent']}, still on remote {e['still_on_remote']}, picked up since {e['picked_up']}"])
        svg = ls_svg(it, rows, rn) if (rn.mode == "functional" and it.delivery == "sftp" and not rn.dry
                                       and any(r["mtime_e"] is not None for r in rows)) else ""
        integrations.append(dict(
            type=it.type, slot=it.slot, name=it.integration, sender=it.sender, receiver=it.receiver,
            data_type=it.data_type, delivery=it.delivery, target=it.target, template=it.template_name,
            planned=it.planned, processed=proc, passed=c["PASSED"] + c["GENERATED"], failed=failed, unverified=unv,
            not_attempted=max(0, it.planned - proc), status=status, ev=dict(kv=kv, svg=svg)))
        t = types.setdefault(it.type, dict(type=it.type, integrations=0, planned=0, processed=0, passed=0,
                                           failed=0, unverified=0, not_attempted=0, skipped=0))
        t["ready"] = t.get("ready", 0) + 1
        t["integrations"] += 1 if proc else 0
        t["planned"] += it.planned
        t["processed"] += proc
        t["passed"] += c["PASSED"] + c["GENERATED"]
        t["failed"] += failed
        t["unverified"] += unv
        t["not_attempted"] += max(0, it.planned - proc)
    for it in rn.ints:
        if not it.ok and it.type in types:
            types[it.type]["skipped"] += 1
    for t in types.values():
        t["status"] = ("FAIL" if t["failed"] else
                       "NO FILES ASSIGNED" if t["processed"] == 0 and t.get("ready") and not t["planned"] else
                       "SKIPPED" if t["processed"] == 0 else
                       "INCOMPLETE" if t["not_attempted"] else "PASS")
    files = []
    cap_total = int(opt(rn.cfg, "report_html_max_rows", 20000))
    per_int_cap = max(50, cap_total // max(len(rn.ok_ints), 1))
    shown, truncated = Counter(), 0
    for r in rn.rows:
        if len(rn.rows) > cap_total and r["status"] in ("PASSED", "GENERATED") and shown[r["gi"]] >= per_int_cap:
            truncated += 1
            continue
        shown[r["gi"]] += 1
        files.append([newidx.get(r["gi"], r["gi"]), r["seq"], r["batch"], r["gen"], r["status"], r["http"], r["lsize"], r["rsize"],
                      r["verify"], r["mtime"], r["perms"], r["owner"], r["checked"], r["err"], r["att"],
                      r["worker"].replace("worker_", "w"), r["sha"], r["lpath"], r["tags"], r["resp"]])
    n_min = (max(rn.per_minute) + 1) if rn.per_minute else 0
    per_min = [rn.per_minute.get(i, 0) for i in range(n_min)]
    tgt = rn.target_per_hour
    meaningless = rn.dry or el < 30 or done < 50          # too short (or nothing really sent) to quote a rate
    summary = dict(planned=planned, processed=done, passed=cnt["PASSED"] + cnt["GENERATED"], failed=cnt["FAILED"],
                   unverified=cnt["UNVERIFIED"], not_attempted=max(0, planned - done),
                   success_pct=round(100.0 * (cnt["PASSED"] + cnt["GENERATED"]) / done, 2) if done else 0.0,
                   error_pct=round(100.0 * cnt["FAILED"] / done, 2) if done else 0.0,
                   rate_per_sec=None if meaningless else round(done / el, 2),
                   rate_per_hour=None if meaningless else round(rate_h), target_per_hour=None if meaningless else tgt,
                   rate_note="generate only" if rn.dry else "run too short for a rate",
                   target_met=None if meaningless else ((rate_h >= tgt) if tgt else None),
                   selected=len(integrations), tested=sum(1 for i in integrations if i["processed"]),
                   skipped=sum(1 for it in rn.ints if not it.ok and it.type in types))
    meta = dict(title="Integration execution report", mode=rn.mode, status=rn.status, run_id=rn.run_id,
                started=utc(rn.t_start) + " UTC", finished=utc(rn.t_end) + " UTC", duration=fmt_dur(el),
                storage=rn.storage, workers=rn.workers, dry=rn.dry, version=VERSION,
                host=os.environ.get("SFTP_HOST", "(local test)" if os.environ.get("SFTP_LOCAL_ROOT") else ""),
                output=str(getattr(rn, "run_out", "")) if rn.storage == "disk" else "(memory only)",
                manifest=str(rn.manifest_path))
    planned_pm = list(rn.sched["minutes"]) if rn.sched else None
    perf = dict(per_minute=per_min, svg=chart_svg(per_min, (tgt / 60.0) if tgt else None, planned_pm),
                per_worker=dict(sorted(rn.per_worker.items())), retries=rn.retried, reconnects=rn.reconnects)
    if rn.sched:
        perf["sched"] = True
        perf["sched_html"] = sched_block(rn, per_min)
    warns = [list(w) for w in rn.warnings]
    if truncated:
        warns.append(["WARNING", f"This HTML shows {len(files):,} of {len(rn.rows):,} file rows ({truncated:,} passed rows "
                                 f"left out to keep the page small; every failed row is kept). The CSV manifest has all rows. "
                                 f"Change report_html_max_rows in the config to show more."])
    skipped = [dict(key=it.key, name=it.integration, reason=it.skip_reason) for it in rn.ints
               if not it.ok and it.type in types]
    return dict(meta=meta, summary=summary, types=list(types.values()), integrations=integrations, files=files,
                warnings=warns, perf=perf, skipped=skipped)


def write_report(rn: Runner, suffix="") -> Path:
    data = build_data(rn)
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    page = REPORT_HTML.replace("__DATA__", blob)
    path = rn.reports_dir / f"report_{rn.mode}_{rn.run_id}{suffix}.html"
    path.write_text(page, encoding="utf-8")
    return path


REPORT_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Integration execution report</title>
<style>
:root{--bg:#f4f6fa;--card:#fff;--ink:#0f172a;--mut:#64748b;--line:#e2e8f0;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--blue:#1e3a8a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1280px;margin:0 auto;padding:20px}
.banner{background:linear-gradient(135deg,#1e3a8a,#1d4ed8);color:#fff;border-radius:14px;padding:22px 26px}
.banner h1{margin:0 0 6px;font-size:24px}.banner .sub{opacity:.9;font-size:13px}
.pill{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600;margin-right:6px;background:rgba(255,255,255,.18)}
.tools{display:flex;gap:8px;justify-content:flex-end;margin:14px 0}
button{border:1px solid #cbd5e1;background:#fff;border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer}
button:hover{background:#f1f5f9}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:6px 0 18px}
.card{background:var(--card);border-radius:12px;padding:14px 16px;border:1px solid var(--line)}
.card b{display:block;font-size:26px}.card span{color:var(--mut);font-size:12px}
.card.ok b{color:var(--ok)}.card.bad b{color:var(--bad)}.card.warn b{color:var(--warn)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:0 0 18px}
h2{margin:0 0 10px;font-size:17px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.03em;background:#f8fafc}
.num{text-align:right;font-variant-numeric:tabular-nums}
.b{display:inline-block;border-radius:6px;padding:1px 8px;font-size:12px;font-weight:700}
.b.pass,.b.passed,.b.verified,.b.generated{background:#dcfce7;color:var(--ok)}
.b.fail,.b.failed{background:#fee2e2;color:var(--bad)}
.b.unverified,.b.incomplete,.b.skipped{background:#fef3c7;color:var(--warn)}
details{border:1px solid var(--line);border-radius:10px;margin:8px 0;background:#fff}
details>summary{cursor:pointer;padding:10px 12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details>summary .n{font-weight:600;flex:1;min-width:220px}details>summary .m{color:var(--mut);font-size:12px}
.inner{padding:0 12px 12px;overflow:auto}
.files td,.files th{font-size:12px;white-space:nowrap}.files td.w{white-space:normal;word-break:break-all;min-width:260px}
tr.bad td{background:#fff5f5}
.warnrow td:first-child{font-weight:700}.lvl-ERROR{color:var(--bad)}.lvl-WARNING{color:var(--warn)}
.kv td:first-child{color:var(--mut);width:240px}.kv td{border:0;padding:3px 8px;word-break:break-all}
.flt{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
input[type=search]{padding:7px 10px;border:1px solid #cbd5e1;border-radius:8px;min-width:240px}
.red{color:var(--bad)}
@media print{.tools,.flt,button{display:none!important}body{background:#fff}.banner{-webkit-print-color-adjust:exact;print-color-adjust:exact}
section,details{break-inside:avoid-page}.summary-only .files-wrap{display:none}}
</style></head><body><div class="wrap" id="app"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const F=D.files,I=D.integrations,M=D.meta,S=D.summary,FUNC=M.mode==='functional';
const X={ii:0,seq:1,batch:2,gen:3,st:4,http:5,ls:6,rs:7,ver:8,mt:9,perm:10,own:11,chk:12,err:13,att:14,wk:15,sha:16,lp:17,tags:18,resp:19};
const byInt=I.map(()=>[]);F.forEach((r,k)=>byInt[r[0]].push(k));
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>Number(n).toLocaleString();
const badge=s=>`<span class="b ${esc(String(s).toLowerCase())}">${esc(s)}</span>`;
const join=(a,b)=>a.replace(/[\\/]+$/,'')+'/'+b;
const dest=(it,r)=>it.delivery==='http'?it.target:join(it.target,r[X.gen]);
let failedOnly=false,q='',PAGE=500;
function head(){return FUNC
 ?['#','Batch','Original file','Generated file','Result','HTTP','Local size','Remote size','Verification','Remote modified (UTC)','Perms','Owner','Checked (UTC)','Destination','Error']
 :['#','Batch','Original file','Generated file','Result','HTTP','Size','Destination','Error'];}
function rowHtml(it,r){const o=it.template,bad=r[X.st]==='FAILED';
 const err=esc(r[X.err]||'')+(r[X.resp]?' '+esc(r[X.resp]):'');
 const c=FUNC?[r[X.seq],r[X.batch],esc(o),esc(r[X.gen]),badge(r[X.st]),r[X.http],r[X.ls],r[X.rs],esc(r[X.ver]),esc(r[X.mt]),esc(r[X.perm]),esc(r[X.own]),esc(r[X.chk]),esc(dest(it,r)),err]
 :[r[X.seq],r[X.batch],esc(o),esc(r[X.gen]),badge(r[X.st]),r[X.http],r[X.ls],esc(dest(it,r)),err];
 return `<tr class="${bad?'bad':''}">`+c.map((v,i)=>`<td class="${(i==3||i==2)?'w':''}">${v}</td>`).join('')+'</tr>';}
function renderFiles(ii,box,limit){
 const it=I[ii];let ids=byInt[ii];if(failedOnly)ids=ids.filter(k=>F[k][X.st]==='FAILED');
 const shown=ids.slice(0,limit);
 box.innerHTML=`<div class="files-wrap"><table class="files"><thead><tr>${head().map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${shown.map(k=>rowHtml(it,F[k])).join('')}</tbody></table>`
 +(ids.length>shown.length?`<p>Showing ${fmt(shown.length)} of ${fmt(ids.length)} files. <button data-more>Show next ${PAGE}</button> <button data-all>Show all</button></p>`:`<p class="m">${fmt(ids.length)} file(s).</p>`)+'</div>';
 const m=box.querySelector('[data-more]');if(m)m.onclick=()=>renderFiles(ii,box,limit+PAGE);
 const a=box.querySelector('[data-all]');if(a)a.onclick=()=>renderFiles(ii,box,1e9);}
function intMatches(it){if(failedOnly&&!it.failed)return false;if(!q)return true;
 return (it.type+' '+it.name+' '+it.sender+' '+it.receiver+' '+it.data_type+' '+it.target+' '+it.template).toLowerCase().includes(q);}
function build(){
 const ok=S.target_met===null?'':(S.target_met?'ok':'warn');
 let h=`<div class="banner"><h1>${esc(M.title)}</h1><div><span class="pill">${esc(M.status)}</span><span class="pill">${esc(M.mode.toUpperCase())}</span>${M.dry?'<span class="pill">DRY RUN</span>':''}<span class="pill">storage: ${esc(M.storage)}</span><span class="pill">workers: ${M.workers}</span></div>
 <div class="sub" style="margin-top:8px">Started ${esc(M.started)} &nbsp;|&nbsp; Finished ${esc(M.finished)} &nbsp;|&nbsp; Duration ${esc(M.duration)} &nbsp;|&nbsp; Run ${esc(M.run_id)}${M.host?' &nbsp;|&nbsp; Server '+esc(M.host):''}</div></div>
 <div class="tools"><button onclick="exportExcel()">Export Excel</button><button onclick="printSummary()">Print / Save PDF (summary)</button><button onclick="printAll()">Print / Save PDF (all files)</button></div>
 <div class="cards">
 <div class="card"><b>${fmt(S.planned)}</b><span>Planned</span></div>
 <div class="card"><b>${fmt(S.processed)}</b><span>Processed</span></div>
 <div class="card ok"><b>${fmt(S.passed)}</b><span>Passed (${S.success_pct}%)</span></div>
 <div class="card ${S.failed?'bad':''}"><b>${fmt(S.failed)}</b><span>Failed (${S.error_pct}%)</span></div>
 <div class="card ${S.unverified?'warn':''}"><b>${fmt(S.unverified)}</b><span>Unverified</span></div>
 <div class="card ${S.not_attempted?'warn':''}"><b>${fmt(S.not_attempted)}</b><span>Not attempted</span></div>
 <div class="card ${ok}"><b>${S.rate_per_hour==null?'n/a':fmt(S.rate_per_hour)}</b><span>${S.rate_per_hour==null?'Files/hour ('+S.rate_note+')':'Files/hour ('+S.rate_per_sec+'/s)'}${S.target_per_hour?' &middot; target '+fmt(S.target_per_hour)+(S.target_met?' met':' not met'):''}</span></div>
 <div class="card"><b>${S.tested}</b><span>Integrations tested</span></div>
 ${S.skipped?`<div class="card bad"><b>${S.skipped}</b><span>Integrations skipped</span></div>`:''}
 <div class="card ${D.warnings.length?'warn':''}"><b>${D.warnings.length}</b><span>Warnings / errors</span></div></div>`;
 if(D.skipped.length)h+=`<section><h2 class="red">Skipped integrations (not run)</h2><table><tr><th>Integration</th><th>Reason</th></tr>${D.skipped.map(s=>`<tr><td>${esc(s.key)}<br><span class="m">${esc(s.name)}</span></td><td>${esc(s.reason)}</td></tr>`).join('')}</table></section>`;
 h+=`<section><h2>Summary by type</h2><table><tr><th>Type</th><th class="num">Integrations tested</th><th class="num">Planned</th><th class="num">Files placed</th><th class="num">Passed</th><th class="num">Failed</th><th class="num">Unverified</th><th class="num">Not attempted</th><th>Status</th></tr>
 ${D.types.map(t=>`<tr><td>${esc(t.type)}</td><td class="num">${t.integrations}</td><td class="num">${fmt(t.planned)}</td><td class="num">${fmt(t.processed)}</td><td class="num">${fmt(t.passed)}</td><td class="num">${fmt(t.failed)}</td><td class="num">${fmt(t.unverified)}</td><td class="num">${fmt(t.not_attempted)}</td><td>${badge(t.status)}</td></tr>`).join('')}</table>
 <p class="m" style="color:var(--mut)">Passed = ${FUNC?'file found on the SFTP server with the same size, or HTTP 2xx':'upload accepted (SFTP) or HTTP 2xx'}. A file counts as Unverified when it was gone from the server by the time it was checked (a poller may have taken it); that is not a failure.</p></section>`;
 h+=`<section><h2>Integrations and files</h2><div class="flt"><input type="search" id="q" placeholder="Search integration, sender, receiver, target..."><label><input type="checkbox" id="fo"> Failed only</label></div><div id="ilist"></div></section>`;
 h+=`<section><h2>Evidences</h2><div id="evid"></div></section>`;
 h+=`<section><details><summary><span class="n">Warnings and errors (${D.warnings.length})</span></summary><div class="inner">${D.warnings.length?`<table><tr><th>Level</th><th>Message</th></tr>${D.warnings.map(w=>`<tr class="warnrow"><td class="lvl-${esc(w[0])}">${esc(w[0])}</td><td>${esc(w[1])}</td></tr>`).join('')}</table>`:'None.'}</div></details></section>`;
 document.getElementById('app').innerHTML=h;
 document.getElementById('q').oninput=e=>{q=e.target.value.toLowerCase();drawInts();};
 document.getElementById('fo').onchange=e=>{failedOnly=e.target.checked;drawInts();};
 drawInts();drawEvid();}
function drawInts(){const box=document.getElementById('ilist');box.innerHTML='';
 const types=[...new Set(I.map(i=>i.type))];
 types.forEach(t=>{const list=I.map((it,ii)=>[it,ii]).filter(([it])=>it.type===t&&intMatches(it));if(!list.length)return;
  const d=document.createElement('details');d.innerHTML=`<summary><span class="n">${esc(t)}</span><span class="m">${list.length} integration(s)</span></summary><div class="inner"></div>`;
  const inner=d.querySelector('.inner');
  list.forEach(([it,ii])=>{const e=document.createElement('details');e.className='int';e.dataset.ii=ii;
   e.innerHTML=`<summary><span class="n">${esc(it.slot)} &middot; ${esc(it.name)}<br><span class="m">${esc(it.sender)} &rarr; ${esc(it.receiver)} &middot; ${esc(it.data_type)} &middot; ${esc(it.target)}</span></span><span class="m">${fmt(it.processed)}/${fmt(it.planned)} files &middot; ${fmt(it.failed)} failed</span>${badge(it.status)}</summary><div class="inner"></div>`;
   e.addEventListener('toggle',()=>{if(e.open&&!e.dataset.done){e.dataset.done=1;renderFiles(ii,e.querySelector('.inner'),PAGE);}});
   inner.appendChild(e);});
  box.appendChild(d);});
 if(!box.children.length)box.innerHTML='<p class="m">No integrations match.</p>';}
function drawEvid(){const box=document.getElementById('evid');let h='';
 if(!FUNC||D.perf.sched){h+=`<details><summary><span class="n">Throughput</span></summary><div class="inner">${D.perf.svg}${D.perf.sched_html||''}<table style="margin-top:10px"><tr><th>Worker</th><th class="num">Files</th></tr>${Object.entries(D.perf.per_worker).map(([k,v])=>`<tr><td>${esc(k)}</td><td class="num">${fmt(v)}</td></tr>`).join('')}</table><p>Retries: ${D.perf.retries} &middot; Reconnects: ${D.perf.reconnects}</p></div></details>`;}
 if(FUNC){h+=`<p class="m" style="color:var(--mut)">Generated files: ${esc(M.output)}<br>Manifest: ${esc(M.manifest)}</p>`;}
 I.forEach((it,ii)=>{h+=`<details><summary><span class="n">${esc(it.type)} &middot; ${esc(it.name)}</span>${badge(it.status)}</summary><div class="inner"><table class="kv">${it.ev.kv.map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join('')}</table>${it.ev.svg?`<div style="margin-top:10px;overflow:auto">${it.ev.svg}</div>`:''}</div></details>`;});
 box.innerHTML=h;}
function xe(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g,'');}
function cell(v){return (typeof v==='number'&&isFinite(v))?`<Cell><Data ss:Type="Number">${v}</Data></Cell>`:`<Cell><Data ss:Type="String">${xe(v)}</Data></Cell>`;}
function ws(name,rows){return `<Worksheet ss:Name="${xe(name)}"><Table>${rows.map(r=>'<Row>'+r.map(cell).join('')+'</Row>').join('')}</Table></Worksheet>`;}
function exportExcel(){
 const sum=[['Item','Value'],['Mode',M.mode],['Status',M.status],['Started',M.started],['Finished',M.finished],['Duration',M.duration],['Run id',M.run_id]].concat(Object.entries(S).map(([k,v])=>[k,v===null?'':v]));
 const types=[['Type','Integrations tested','Planned','Files placed','Passed','Failed','Unverified','Not attempted','Status']].concat(D.types.map(t=>[t.type,t.integrations,t.planned,t.processed,t.passed,t.failed,t.unverified,t.not_attempted,t.status]));
 const ints=[['Type','Slot','Integration','Sender','Receiver','Data type','Target','Sample file','Planned','Processed','Passed','Failed','Unverified','Status']].concat(I.map(i=>[i.type,i.slot,i.name,i.sender,i.receiver,i.data_type,i.target,i.template,i.planned,i.processed,i.passed,i.failed,i.unverified,i.status]));
 const files=[['Type','Slot','Integration','Seq','Batch','Original file','Generated file','Result','Verification','HTTP','Local size','Remote size','Destination','Remote modified (UTC)','Perms','Owner','Checked (UTC)','Attempts','Worker','SHA256','Local path','Tags replaced','Error']];
 F.forEach(r=>{const it=I[r[0]];files.push([it.type,it.slot,it.name,r[X.seq],r[X.batch],it.template,r[X.gen],r[X.st],r[X.ver],r[X.http],r[X.ls],r[X.rs],dest(it,r),r[X.mt],r[X.perm],r[X.own],r[X.chk],r[X.att],r[X.wk],r[X.sha],r[X.lp],r[X.tags],(r[X.err]||'')+(r[X.resp]?' '+r[X.resp]:'')]);});
 const warns=[['Level','Message']].concat(D.warnings);
 const xml='<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'+ws('Summary',sum)+ws('Types',types)+ws('Integrations',ints)+ws('Files',files)+ws('Warnings',warns)+'</Workbook>';
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([xml],{type:'application/vnd.ms-excel'}));a.download='report_'+M.mode+'_'+M.run_id+'.xls';document.body.appendChild(a);a.click();a.remove();}
function printSummary(){document.body.classList.add('summary-only');window.print();}
function printAll(){if(F.length>5000&&!confirm('This renders '+F.length+' rows; the PDF may be hundreds of pages. Continue?'))return;
 failedOnly=false;document.querySelectorAll('details').forEach(d=>d.open=true);
 document.querySelectorAll('details.int').forEach(e=>{e.dataset.done=1;renderFiles(+e.dataset.ii,e.querySelector('.inner'),1e9);});
 setTimeout(()=>window.print(),300);}
window.addEventListener('afterprint',()=>document.body.classList.remove('summary-only'));
build();
</script></body></html>
"""


# --------------------------------------------------------------------------- scan / check
def split_parts(desc):
    return [p.strip() for p in re.split(r"\s{2,}|\s+_\s+", desc.strip()) if p.strip()]


def describe_folder(desc):
    parts = split_parts(desc)
    p0 = parts[0] if parts else desc
    m = re.match(r"^.*\bdelivery\b.*?\bfor\s+(.+)$", p0, re.I)
    sender = m.group(1).strip() if m else p0
    data_type = parts[1] if len(parts) > 1 else ""
    receiver = parts[2] if len(parts) > 2 else (parts[-1] if len(parts) == 2 else "")
    integration = " > ".join(parts) if parts else desc
    label = re.sub(r"\s+", "", f"{sender}_{receiver}_{data_type}")
    return sender, receiver, data_type, integration, label


def cmd_scan(cfg, cfg_path, args):
    base = cfg_path.parent
    samples = to_path(base, cfg.get("samples_directory") or "project/samples")
    if not samples.is_dir():
        raise SystemExit(f"Samples folder not found: {samples}")
    index = index_samples(samples)
    ints = cfg.setdefault("integrations", [])
    n_before = len(ints)
    have = {(norm_key(i.get("type", "")), str(i.get("slot", ""))) for i in ints}
    seen = set()
    added = 0
    for ntype, (tpath, serials) in index.items():
        tname = re.sub(r"\s*-\s*", "-", tpath.name.strip()).upper()
        sr = max([int(i.get("sr_no", 0)) for i in ints if norm_key(i.get("type", "")) == ntype] + [0])
        for serial in sorted(serials):
            folder = serials[serial][0]
            seen.add((ntype, str(serial)))
            if (ntype, str(serial)) in have:
                continue
            desc = SERIAL_RE.match(folder.name).group(2)
            sender, receiver, dtype, integ, label = describe_folder(desc)
            sr += 1
            ints.append({"sr_no": sr, "slot": str(serial), "type": tname, "label": label, "integration": integ,
                         "sender": sender, "receiver": receiver, "data_type": dtype, "upload_path": None,
                         "http_url": None, "http_form_field": "", "replace_tags": [],
                         "_comment": "NEW FROM SCAN - set upload_path (SFTP) or http_url (HTTP)"})
            added += 1
    missing = [f"{i.get('type')} slot {i.get('slot')}" for i in ints
               if (norm_key(i.get("type", "")), str(i.get("slot", ""))) not in seen]
    if cfg_path.suffix.lower() == ".py":
        target = cfg_path.parent / "integrations.py"
        if added:
            if target.exists():
                shutil.copy(target, target.with_suffix(".py.bak"))
            append_integrations_py(target, ints[n_before:])
        print(f"Scan done: {added} integration(s) added to {target.name}"
              f"{' (backup: ' + target.name + '.bak)' if added and target.with_suffix('.py.bak').exists() else ''}.")
    else:
        if added:
            shutil.copy(cfg_path, cfg_path.with_suffix(".json.bak"))
            cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Scan done: {added} integration(s) added to {cfg_path.name}"
              f"{' (backup: ' + cfg_path.name + '.bak)' if added else ''}.")
    print(f"Sample folders found: {len(seen)}.")
    if missing:
        print(f"In config but no sample folder ({len(missing)}):")
        for m in missing[:50]:
            print("  -", m)
    return 0


def cmd_check(rn: Runner):
    rn.select()
    rn.resolve()
    print(f"Samples folder in use: {rn.samples_dir.resolve()}\n")
    ok = [it for it in rn.ints if it.ok]
    print(f"Selected {len(rn.ints)} | ready {len(ok)} | skipped {len(rn.ints) - len(ok)}\n")
    for it in rn.ints:
        if it.ok:
            ex = build_name(it.template_name, "0123456789"[: it.n].rjust(it.n, "0"), it.n, it.suffix, it.mode_used)
            print(f"OK    {it.key:<26} {it.delivery:<4} {it.template_name}\n      -> {ex}   [{it.mode_used}] tags: "
                  f"{', '.join(f'{k}={v}' for k, v in it.tag_hits.items()) or '-'}")
            for note in it.notes:
                print(f"      note: {note}")
        else:
            print(f"SKIP  {it.key:<26} {it.skip_reason}")
    if rn.sched:
        print()
        for ln in rn.describe_schedule():
            print(ln)
    return 0 if len(ok) == len(rn.ints) else 1


# --------------------------------------------------------------------------- python config (config.py + integrations.py)
def load_config(path: Path):
    """config.py (a CONFIG dict, may import integrations.py) or a legacy .json file."""
    if path.suffix.lower() != ".py":
        return json.loads(path.read_text(encoding="utf-8-sig"))
    import importlib.util
    folder, old_flag = str(path.parent), sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, folder)
    sys.modules.pop("integrations", None)                  # always read the current file, never a cached copy
    try:
        spec = importlib.util.spec_from_file_location("sftp_config", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except SyntaxError as ex:
        raise SystemExit(f"{Path(ex.filename or path).name}, line {ex.lineno}: {ex.msg}\n  {(ex.text or '').strip()}")
    except Exception as ex:
        raise SystemExit(f"Error while reading {path.name}: {type(ex).__name__}: {ex}")
    finally:
        sys.path.remove(folder)
        sys.dont_write_bytecode = old_flag
    cfg = getattr(mod, "CONFIG", None)
    if not isinstance(cfg, dict):
        raise SystemExit(f"{path.name} must define a dict called CONFIG")
    return copy.deepcopy(cfg)


def py_lit(v, ind=0, width=100):
    """Python source for a JSON-like value: double-quoted strings, short dicts on one line."""
    pad = " " * ind
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        if not v:
            return "{}"
        simple = all(not isinstance(x, dict) and (not isinstance(x, list) or all(not isinstance(i, (dict, list)) for i in x))
                     for x in v.values())
        flat = "{ " + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)}: {py_lit(x)}" for k, x in v.items()) + " }"
        if simple and len(flat) + ind <= width:
            return flat
        inner = "".join(f"{pad}    {json.dumps(str(k), ensure_ascii=False)}: {py_lit(x, ind + 4, width)},\n" for k, x in v.items())
        return "{\n" + inner + pad + "}"
    if isinstance(v, list):
        if not v:
            return "[]"
        if all(not isinstance(i, (dict, list)) for i in v):
            return "[" + ", ".join(py_lit(i) for i in v) + "]"
        return "[\n" + "".join(f"{pad}    {py_lit(i, ind + 4, width)},\n" for i in v) + pad + "]"
    return repr(v)


def render_integration(d):
    lines = ["    {"] + [f"        {json.dumps(k, ensure_ascii=False)}: {py_lit(v, 8)}," for k, v in d.items()] + ["    },"]
    return "\n".join(lines)


INTEGRATIONS_HEADER = """# integrations.py - one entry per integration (type + slot). Imported by config.py.
# Edit freely: put a # in front of any line (or a whole { ... } block) to switch it off.
# `python sftp_load.py scan` appends new entries for new sample folders at the end of the list.
#
# Fields
#   "type"            folder name under project/samples, e.g. "SFTP-HTTP", "SFTP-SFTP", "SFTP-EMAIL"
#   "slot"            the number the sample folder starts with ("12 - ..." -> "12")
#   "upload_path"     SFTP target folder (must start with upload_path_policy.required_prefix) - or None
#   "http_url"        HTTP target URL (use this OR upload_path) - or None
#   "http_form_field" name of the multipart form part ("" = empty name, like curl -F '=@file')
#   "replace_tags"    XML tags whose text gets a new unique value in every file, e.g. ["DocNum"]
#   "copies"          files for this integration when no total / schedule is set (default: defaults["copies"])
#   "max_copies"      upper limit for this integration whatever the total is
#   "weight"          bigger = gets more files than the others in a round robin (default 1)
#   "name_mode"       force the file-name rule: "token" | "tail" | "insert" (default: automatic)
#   "replace_last_n" / "append_suffix" / "tag_value_length"   per-integration overrides of defaults
#   "http_headers"    extra headers for this integration only
#   "label", "integration", "sender", "receiver", "data_type", "sr_no"   descriptive only (shown in reports)

INTEGRATIONS = [
"""


CONFIG_SPEC = [
    ("mode", "functional", ["functional = 1 worker, files written to disk, every upload checked on the server (small, careful runs).",
                            "performance = 4 workers, files kept in memory, no per-file check (long scheduled runs).",
                            "The command line --mode overrides this."]),
    ("dry_run", False, ["True = generate files only, upload nothing (same as --dry-run on the command line)."]),
    ("output_mode", "generate_and_upload", ['"generate_and_upload" | "generate_only" (generate_only = same as dry_run).']),
    ("samples_directory", "project\\samples", ["Where the sample files live: <samples>/<TYPE>/<slot> - <description>/<sample file>"]),
    ("output_directory", "project\\output_files", ["Generated files are written here (functional mode, dry runs, or storage = disk)."]),
    ("reports_directory", "project\\reports", ["HTML report and CSV manifest of every run."]),
    ("logs_directory", "project\\logs", ["One log file per run."]),
    ("state_directory", "project\\state", ["counter.json lives here. NEVER delete it: it keeps file numbers unique across runs."]),
    ("total", None, ["A number = send exactly that many files, dealt over the selected integrations.",
                     "Ignored while a schedule is active. None = use `copies` per integration."]),
    ("defaults", {"copies": 100, "replace_last_n": 10, "append_suffix": "", "tag_value_length": 10, "http_form_field": ""},
     ["copies           files per integration when no total/schedule is set",
      "replace_last_n   how many digits of the unique number go into each file name",
      "append_suffix    text added after the number, before the extension",
      "tag_value_length digits written into replace_tags values",
      'http_form_field  default multipart field name ("" = empty name, like curl -F \'=@file\')']),
    ("batch_size", 100, ["BATCH MODE (only used when ACTIVE_SCHEDULE = \"off\"): send batch_size files, wait, repeat."]),
    ("batch_pause_seconds", 5, ["Pause between batches (batch mode only)."]),
    ("type_run_settings", {}, ["Batch size / pause per type (batch mode only), e.g.",
                               '{ "SFTP-HTTP": { "batch_size": 100, "batch_pause_seconds": 5 } }']),
    ("sample_fallback", "off", ['"off" = an integration without a sample file is skipped.',
                                '"dummy" = it sends the file named in sample_fallback_file instead (each use is listed as a warning).']),
    ("sample_fallback_file", None, ['Name of the placeholder file, e.g. "Folder structure latest.xlsx" (searched under samples_directory).',
                                    "None = the first file directly inside samples_directory."]),
    ("failure_policy", {"warn_after_seconds": 30, "abort_after_seconds": 120, "min_failures": 10},
     ["Failed files never stop a run by themselves. If EVERY file fails continuously for warn_after_seconds an ERROR is",
      "logged; after abort_after_seconds (and at least min_failures files) the run stops, writes the report, exit code 3.",
      "abort_after_seconds 0 = never stop automatically."]),
    ("abort_after_consecutive_failures", 0, ["Also stop after this many failures in a row (0 = off)."]),
    ("report_html_max_rows", 20000, ["Above this many files the HTML report lists only the first rows per integration",
                                     "(failed files are always kept). The CSV manifest always has every row."]),
    ("workers", None, ["None = use the mode preset (functional 1, performance 4). Or a number, e.g. 8."]),
    ("storage", None, ['None = mode preset. "disk" = also write generated files to output_directory, "memory" = do not.']),
    ("verify_remote", None, ["None = mode preset. True = check each uploaded file on the SFTP server, False = trust the upload."]),
    ("max_files_per_hour", None, ["Hard throttle for the whole run (batch mode only; a schedule paces itself)."]),
    ("target_files_per_hour", 20000, ["Used only for the 'target met / not met' line in the report of long runs."]),
    ("retries", 2, ["Extra attempts for a failed upload before it is recorded as FAILED."]),
    ("end_check", False, ["True = at the end of a performance run list each remote folder once (a poller may have taken files)."]),
    ("http_headers", {}, ["Headers added to every HTTP upload, e.g. { \"Authorization\": \"Bearer ...\" }."]),
    ("http_verify_tls", True, ["False = do not verify HTTPS certificates (test servers only)."]),
    ("cleanup_output_before_run", False, ["True = delete older _run_* folders in output_directory before a real run",
                                           "(never during a dry run, never a folder another run is still using)."]),
    ("report_skipped_integrations", True, ["Kept for compatibility."]),
    ("upload_path_policy", {"required_prefix": "/spice/qa-final", "prompt_before_prefix": True},
     ["Safety net: an SFTP upload_path outside required_prefix is skipped.",
      "prompt_before_prefix True = you must type YES before a real run (--yes skips the prompt)."]),
]

SCHEDULE_EXAMPLES = '''    # ---- examples: copy one, or change the numbers --------------------------------------------------
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
'''

FILTER_EXAMPLES = '''    # ---- examples: "slot" can be "all", one number, or a list of numbers ------------------------------
    "one_type": [ { "type": "SFTP-HTTP", "slot": "all" } ],
    "one_type_one_slot": [ { "type": "SFTP-HTTP", "slot": [1] } ],
    "one_type_many_slots": [ { "type": "SFTP-HTTP", "slot": [1, 2, 3] } ],
    "many_types_all_slots": [ { "type": "SFTP-HTTP", "slot": "all" }, { "type": "SFTP-SFTP", "slot": "all" } ],
    "many_types_many_slots": [
        { "type": "SFTP-HTTP", "slot": [1, 2] },
        { "type": "SFTP-SFTP", "slot": [13, 14, 15] },
        { "type": "SFTP-EMAIL", "slot": "all" },
    ],
'''


def build_config_py(cfg):
    """Turn a (JSON) config dict into a commented config.py."""
    out = []
    w = out.append
    w("# config.py - settings for sftp_load.py. Plain Python: use # for comments, True / False / None.")
    w("# Run:  python sftp_load.py check      (preview, sends nothing)")
    w("#       python sftp_load.py run --dry-run --total 5      (generate 5 files, upload nothing)")
    w("# Command line options (--type, --total, --minutes, --mode, --dry-run ...) always win over this file.")
    w("# Integrations (type + slot + target) are in integrations.py.")
    w("")
    w("from integrations import INTEGRATIONS")
    w("")
    w("")
    w("# " + "=" * 98)
    w("# 1. WHAT TO RUN - change these two names to switch (everything else can stay as it is)")
    w("# " + "=" * 98)
    sched = cfg.get("schedule")
    rf = (cfg.get("run_filter") or {}).get("slots") or []
    w('ACTIVE_SCHEDULE = "%s"     # a name from SCHEDULES below; "off" = batch mode (batch_size / pause)' %
      ("from_json" if sched else "off"))
    w('ACTIVE_FILTER = "%s"     # a name from FILTERS below; "everything" = every integration in integrations.py' %
      ("from_json" if rf else "everything"))
    w("")
    w("# --- SCHEDULES: how many files, over how many hours (random files per minute, exact hourly totals) ---")
    w("# profile \"random\": each hour is split into 60 random minute counts between min_per_minute and")
    w("# max_per_minute that add up EXACTLY to that hour's total; \"flat\": same count every minute.")
    w("# Files are spread across each minute; a shortfall is carried into the next minute.")
    w("SCHEDULES = {")
    w('    "off": None,')
    if sched:
        w('    # your schedule, converted from the old JSON file')
        w('    "from_json": %s,' % py_lit(sched, 4))
    w(SCHEDULE_EXAMPLES.rstrip("\n"))
    w("}")
    w("")
    w("# --- FILTERS: which types / slots take part (command line --type does the same for one run) ---")
    w("FILTERS = {")
    w('    "everything": [],')
    if rf:
        w('    "from_json": %s,' % py_lit(rf, 4))
    w(FILTER_EXAMPLES.rstrip("\n"))
    w("}")
    w("")
    w("")
    w("# " + "=" * 98)
    w("# 2. SETTINGS")
    w("# " + "=" * 98)
    w("CONFIG = {")
    known = {"integrations", "schedule", "run_filter"}
    for key, default, comment in CONFIG_SPEC:
        known.add(key)
        val = cfg.get(key, default)
        w("")
        for c in comment:
            w("    # " + c)
        w(f"    {json.dumps(key)}: {py_lit(val, 4)},")
    extras = {k: v for k, v in cfg.items() if k not in known and not k.startswith("_")}
    if extras:
        w("")
        w("    # ---- other settings found in your old file ----")
        for k, v in extras.items():
            w(f"    {json.dumps(k)}: {py_lit(v, 4)},")
    w("")
    w("    # ---- set by the two names at the top; no need to edit these three lines ----")
    w('    "schedule": SCHEDULES[ACTIVE_SCHEDULE],')
    w('    "run_filter": { "slots": FILTERS[ACTIVE_FILTER] },')
    w('    "integrations": INTEGRATIONS,')
    w("}")
    return "\n".join(out) + "\n"


def cmd_convert(cfg, cfg_path, args):
    """config.json -> config.py + integrations.py (next to the JSON file)."""
    out_dir = cfg_path.parent
    targets = [out_dir / "config.py", out_dir / "integrations.py"]
    for t in targets:
        if t.exists():
            shutil.copy(t, t.with_suffix(".py.bak"))
            print(f"Existing {t.name} saved as {t.name}.bak")
    ints = cfg.get("integrations") or []
    body = INTEGRATIONS_HEADER + "\n".join(render_integration(i) for i in ints) + ("\n" if ints else "") + "]\n"
    targets[1].write_text(body, encoding="utf-8")
    targets[0].write_text(build_config_py(cfg), encoding="utf-8")
    print(f"Wrote {targets[0]} and {targets[1]} ({len(ints)} integration(s)).")
    print("Next: python sftp_load.py check")
    return 0


def append_integrations_py(path: Path, new_entries):
    """Add entries at the end of INTEGRATIONS in integrations.py without touching anything else in the file."""
    if not path.exists():
        path.write_text(INTEGRATIONS_HEADER + "]\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    cut = text.rstrip().rfind("]")
    if cut < 0:
        raise SystemExit(f"Cannot find the closing ] of INTEGRATIONS in {path.name}")
    block = "\n".join(render_integration(e) for e in new_entries) + "\n"
    head = text[:cut].rstrip("\n") + "\n"
    path.write_text(head + block + text[cut:], encoding="utf-8")


# --------------------------------------------------------------------------- main
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS + ("-h", "--help"):
        argv.insert(0, "run")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=COMMANDS)
    ap.add_argument("--config", default=None, help="config.py (default) or a .json file")
    ap.add_argument("--mode", choices=list(PRESETS))
    ap.add_argument("--type", action="append", metavar="TYPE[:SLOTS]",
                    help="run only this type, e.g. SFTP-EMAIL, SFTP-EMAIL:1,3 (slots), 'A,B' (several types), "
                         "'A:1;B:all' (per-type slots); repeat the option to add more")
    ap.add_argument("--slot", help="slots for --type entries without ':' (one number, a list 1,3,5, or 'all')")
    ap.add_argument("--total", type=int, help="total files, dealt round robin over the selected integrations")
    ap.add_argument("--copies", type=int, help="files per integration (when no total is set)")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--dry-run", action="store_true", help="generate files, upload nothing")
    ap.add_argument("--minutes", type=int, metavar="N",
                    help="quick test: run only the first N minutes of the schedule (files scaled to match)")
    ap.add_argument("--paced", action="store_true",
                    help="with --dry-run and a schedule: also wait out the per-minute timing (default: generate at full speed)")
    ap.add_argument("--yes", action="store_true", help="skip the upload-prefix confirmation prompt")
    args = ap.parse_args(argv)
    if args.config:
        cfg_path = Path(args.config).resolve()
    else:                                                    # config.py first, then config.json; here, then next to the script
        names = ("config.py", "config.json")
        cands = [Path.cwd() / n for n in names] + [Path(__file__).resolve().parent / n for n in names]
        cfg_path = next((c for c in cands if c.exists()), cands[0]).resolve()
    if not cfg_path.exists():
        raise SystemExit(f"Config not found: {cfg_path}")
    cfg = load_config(cfg_path)
    load_env(cfg_path.parent / ".env")
    if args.command == "convert":
        if cfg_path.suffix.lower() == ".py":
            raise SystemExit("convert needs a .json file: python sftp_load.py convert --config config.json")
        return cmd_convert(cfg, cfg_path, args)
    if args.command == "scan":
        return cmd_scan(cfg, cfg_path, args)
    rn = Runner(cfg, cfg_path, args)
    if args.command == "check":
        return cmd_check(rn)
    return rn.run()


if __name__ == "__main__":
    sys.exit(main())
