#!/usr/bin/env python3
"""run.py - runs the APIs on AWS and/or GCP, compares them, writes an HTML report, an Excel file and raw JSON.

python run.py                                     both clouds, everything
python run.py --cloud aws --group "Program"       one cloud, one group (checks results against expected status)
python run.py --service CreateLot,GetProgram      specific APIs
python run.py --type negative --tag promo         only negative cases tagged promo
python run.py --mode reuse                        use fixed IDs from config, skip create and delete steps
python run.py --cloud gcp --baseline runs/x.json  run GCP only, compare with AWS results saved in an earlier run
python run.py --rerun-failed runs/x.json          re-run only the tests that failed in an earlier run
python run.py --dry-run                           print the curl commands without sending anything
"""
import argparse, datetime, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
import requests
import config as C
import apis

F = C.FEATURES
PH = re.compile(r"\{(\w+)\}")
COLON = re.compile(r":([A-Za-z_]\w*)")
IGN = {x.lower() for x in C.IGNORE_FIELDS}
TYPE_LABEL = {"basic": "Basic", "bearer": "Bearer", "header": "API key header", "headers": "Headers", "cookie": "Cookie"}
JTYPE = {"int": "number", "float": "number", "str": "string", "bool": "boolean", "NoneType": "null"}
COLS = [("cat", "Category"), ("grp", "Group"), ("op", "Operation"), ("mp", "Method"), ("cs", "Test case"),
        ("ty", "Type"), ("aa", "AWS auth"), ("ca", "GCP auth"), ("ex", "Expected"), ("a", "AWS status"),
        ("c", "GCP status"), ("am", "AWS ms"), ("cm", "GCP ms"), ("td", "Time difference"),
        ("df", "Difference"), ("ot", "Headers, size, types"), ("res", "Result"),
        ("cuA", "AWS request sent (curl)"), ("cuC", "GCP request sent (curl)"),
        ("ar", "AWS response"), ("cr", "GCP response")]


# ------------------------------------------------------------------ building requests
def fill(s, v):
    return PH.sub(lambda m: str(v.get(m.group(1), m.group(0))), s)


def case_vars(ctx, case):
    v = dict(ctx)
    for k, x in case["vars"].items():
        v[k] = ctx.get(x[1:], "{%s}" % x[1:]) if isinstance(x, str) and x.startswith("@") else x
    return v


def apply_auth(profile, cloud, v, mode, masked):
    h, cookies, basic = {}, {}, None
    if mode == "missing":
        return h, cookies, basic
    spec = C.AUTH[profile][cloud]

    def val(s):
        def sub(m):
            k = m.group(1)
            x = v.get(k, m.group(0))
            if k in C.SECRET_PLACEHOLDER and (masked or mode == "invalid"):
                p = C.SECRET_PLACEHOLDER[k]
                p = p[cloud] if isinstance(p, dict) else p
                if masked:
                    w = p.split(" ")
                    if mode == "invalid":
                        w[-1] = "INVALID_" + w[-1]
                    x = " ".join(w)
                else:
                    x = "invalid-" + str(x)
            return str(x)
        return PH.sub(sub, s)

    t = spec["type"]
    if t == "basic":
        basic = (val(spec["user"]), val(spec["password"]))
    elif t == "bearer":
        h["Authorization"] = "Bearer " + val(spec["token"])
    elif t == "header":
        h[spec["name"]] = val(spec["value"])
    elif t == "headers":
        h.update({k: val(x) for k, x in spec["headers"].items()})
    elif t == "cookie":
        cookies = {k: val(x) for k, x in spec["cookies"].items()}
    return h, cookies, basic


def build(t, cloud, ctx, masked=False):
    o, c = t["op"], t["case"]
    ov = o["overrides"].get(cloud, {})
    v = case_vars(ctx, c)
    path = fill(COLON.sub(r"{\1}", c.get("path") or ov.get("path", o["path"])), v)
    body = c["body"] if c["body"] is not None else ov.get("body", o["body"])
    if callable(body):
        body = body(cloud, v)
    data = None
    if body is not None:
        data = fill(body if isinstance(body, str) else json.dumps(body), v)
    h = {"Accept": "application/xml" if o["fmt"] == "xml" else "application/json"}
    if data:
        h["Content-Type"] = "application/xml" if data.lstrip().startswith("<") else "application/json"
    ah, ck, basic = apply_auth(ov.get("auth", o["auth"]), cloud, v, c["auth_mode"], masked)
    h.update(ah)
    return dict(method=o["method"], url=C.BASE_URL[cloud][t["host"]] + path, headers=h,
                cookies=ck, basic=basic, data=data)


def curl(r):
    p = ["curl -i -X %s '%s'" % (r["method"], r["url"])]
    p += ["-H '%s: %s'" % (k, x) for k, x in r["headers"].items()]
    if r["basic"]:
        p.append("-u '%s:%s'" % r["basic"])
    if r["cookies"]:
        p.append("-b '%s'" % "; ".join("%s=%s" % kv for kv in r["cookies"].items()))
    if r["data"]:
        p.append("-d '%s'" % r["data"])
    return " ".join(p)


def leftovers(r, cx=None):
    s = [r["url"], r["data"] or ""] + list(r["headers"].values()) + list(r["cookies"].values()) + list(r["basic"] or [])
    j = " ".join(s)
    unfilled = {k for k, x in (cx or {}).items() if isinstance(x, str) and x.startswith("FILL_") and x in j}
    return sorted({n for x in s for n in PH.findall(x)} | unfilled)


def auth_label(t, cloud):
    c = t["case"]
    if c["auth_mode"] == "missing":
        return "None: Missing credentials"
    spec = C.AUTH[t["op"]["overrides"].get(cloud, {}).get("auth", t["op"]["auth"])][cloud]
    parts = []
    for n in dict.fromkeys(PH.findall(json.dumps(spec))):
        if n in C.SECRET_PLACEHOLDER:
            p = C.SECRET_PLACEHOLDER[n]
            parts.append("%s (curl shows %s)" % (n, p[cloud] if isinstance(p, dict) else p))
        else:
            parts.append("%s %s" % (n, C.VARS[cloud].get(n, "?")))
    s = "%s: %s" % (TYPE_LABEL[spec["type"]], " + ".join(parts))
    return s + (" [invalid credentials]" if c["auth_mode"] == "invalid" else "")


# ------------------------------------------------------------------ sending
def send(r):
    t0 = time.perf_counter()
    try:
        x = requests.request(r["method"], r["url"], headers=r["headers"], cookies=r["cookies"], auth=r["basic"],
                             data=r["data"], timeout=C.TIMEOUT, verify=C.VERIFY_SSL)
        return {"status": x.status_code, "body": x.text, "headers": {k.lower(): v for k, v in x.headers.items()},
                "ms": int((time.perf_counter() - t0) * 1000), "error": None}
    except requests.RequestException as e:
        return {"status": None, "body": "", "headers": {}, "ms": 0, "error": str(e)}


def run_cloud(t, cloud, ctx):
    req = build(t, cloud, ctx)
    rt = F["response_time"]
    runs = rt["runs_per_api"] if rt["enabled"] and t["op"]["method"] == "GET" else 1
    if runs > 1 and rt["warm_up_call"]:
        send(req)
    times, res = [], None
    for _ in range(runs):
        res = send(req)
        times.append(res["ms"])
    res.update(ms=round(sum(times) / len(times)), min=min(times), max=max(times), runs=runs,
               curl=curl(build(t, cloud, ctx, masked=True)))
    return res


def call_all(t, clouds, ctx):
    if F["parallel"]["enabled"] and len(clouds) > 1:
        with ThreadPoolExecutor(len(clouds)) as ex:
            futs = {c: ex.submit(run_cloud, t, c, ctx[c]) for c in clouds}
            return {c: f.result() for c, f in futs.items()}
    return {c: run_cloud(t, c, ctx[c]) for c in clouds}


# ------------------------------------------------------------------ comparing
def flat(body):
    m, t = {}, {}
    b = (body or "").strip()
    if not b:
        return m, t

    def wj(v, p):
        if isinstance(v, dict):
            for k, x in v.items():
                wj(x, p + "/" + k)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                wj(x, p + "/" + str(i))
        else:
            m[p] = "null" if v is None else (str(v).lower() if isinstance(v, bool) else str(v))
            t[p] = JTYPE.get(type(v).__name__, type(v).__name__)

    def wx(n, p):
        kids = list(n)
        if not kids:
            m[p] = n.text or ""
            t[p] = "text"
        for k in kids:
            wx(k, p + "/" + k.tag.split("}")[-1])
    try:
        if b[0] in "{[":
            wj(json.loads(b), "")
        elif b[0] == "<":
            r = ET.fromstring(b)
            wx(r, r.tag.split("}")[-1])
        else:
            m["_text"], t["_text"] = b, "text"
    except Exception:
        m["_text"], t["_text"] = b, "text"
    return m, t


def is_var(a, g):
    return bool(a) and any(C.VARS["aws"].get(k) == a and C.VARS["gcp"].get(k) == g for k in C.VARS["aws"])


def fdiff(ab, gb):
    (a, at), (g, gt) = flat(ab), flat(gb)
    d = []
    for p, x in a.items():
        leaf = p.split("/")[-1]
        if p not in g:
            d.append(("missing", leaf, x, None, at[p], None))
        elif at[p] != gt[p] and F["type_checks"]["enabled"]:
            d.append(("type", leaf, x, g[p], at[p], gt[p]))
        elif x != g[p] and leaf.lower() not in IGN and not is_var(x, g[p]):
            d.append(("value", leaf, x, g[p], at[p], gt[p]))
    for p, x in g.items():
        if p not in a:
            d.append(("extra", p.split("/")[-1], None, x, None, gt[p]))
    return d


def known(svc, kind, leaf):
    today = datetime.date.today().isoformat()
    key = {"extra": "gcp_extra", "missing": "gcp_missing", "value": "value_diff", "type": "type_diff",
           "header": "headers"}[kind]
    for r in C.KNOWN_DIFFERENCES:
        if r.get("service") in (svc, "*") and r.get("expires", "9999") >= today and leaf in r.get(key, []):
            return r["reason"]


def diff_lines(ds):
    out = []
    miss = [d[1] for d in ds if d[0] == "missing"]
    ext = [d[1] for d in ds if d[0] == "extra"]
    if miss:
        out.append("Missing columns (in AWS, not in GCP): " + ", ".join(miss))
    if ext:
        out.append("New columns (in GCP, not in AWS): " + ", ".join(ext))
    for d in ds:
        if d[0] == "value":
            out.append("Value differs in %s: AWS '%s' vs GCP '%s'" % (d[1], d[2], d[3]))
        elif d[0] == "type":
            out.append("Type differs in %s: AWS %s (%s) vs GCP %s (%s)" % (d[1], d[4], d[2], d[5], d[3]))
    return out


def pretty(body):
    b = (body or "").strip()
    try:
        if b.startswith(("{", "[")):
            return json.dumps(json.loads(b), indent=2)
        if b.startswith("<"):
            r = ET.fromstring(b)
            ET.indent(r)
            return ET.tostring(r, encoding="unicode")
    except Exception:
        pass
    return body or ""


def resp_text(r):
    if r is None:
        return "Not run"
    if r["error"]:
        return "Request failed: " + r["error"]
    ct = r["headers"].get("content-type", "")
    return "HTTP/1.1 %s\ncontent-type: %s\n\n%s" % (r["status"], ct, pretty(r["body"]))


def err_msg(body):
    b = (body or "").strip()
    try:
        d = json.loads(b)
        if isinstance(d, dict):
            for k in ("message", "error", "errorMessage", "details", "title", "errorDesc"):
                if d.get(k):
                    return str(d[k]).strip()
    except Exception:
        pass
    return b[:300]


def msg_issues(t, name, r):
    c = t["case"]
    exp = c.get("expect_msg")
    if not exp or r["error"]:
        return []
    act, mode = err_msg(r["body"]), c.get("match", "contains")
    ok = {"exact": act == exp, "startswith": act.startswith(exp), "contains": exp in act}[mode]
    return [] if ok else ["Error message on %s: expected %s '%s', got '%s'" % (name, mode, exp, act[:120])]


def compare(t, A, G):
    svc, ex = t["op"]["name"], t["case"]["expect"]
    lines, other, notes, sa, sc = [], [], [], set(), set()
    for n, r in (("AWS", A), ("GCP", G)):
        if r["error"]:
            lines.append("Request failed on %s: %s" % (n, r["error"]))
    if lines:
        return lines, other, notes, sa, sc
    if A["status"] != G["status"]:
        lines.append("Status: AWS %s vs GCP %s" % (A["status"], G["status"]))
    elif A["status"] != ex:
        lines.append("Unexpected status: expected %s, both returned %s" % (ex, A["status"]))
    lines += msg_issues(t, "AWS", A) + msg_issues(t, "GCP", G)
    bad = []
    for d in fdiff(A["body"], G["body"]):
        why = known(svc, d[0], d[1])
        if why:
            notes.append("Accepted: %s (%s)" % (why, d[1]))
        else:
            bad.append(d)
            if d[0] in ("missing", "value", "type"):
                sa.add(d[1])
            if d[0] in ("extra", "value", "type"):
                sc.add(d[1])
    lines += diff_lines(bad)
    if F["compare_headers"]["enabled"]:
        for n in F["compare_headers"]["check"]:
            a, g = A["headers"].get(n), G["headers"].get(n)
            if a != g:
                why = known(svc, "header", n)
                if why:
                    notes.append("Accepted: %s (%s)" % (why, n))
                else:
                    other.append("Header %s: AWS %s vs GCP %s" % (n, a or "missing", g or "missing"))
    if F["response_size"]["enabled"] and A["body"]:
        la, lg = len(A["body"]), len(G["body"])
        if abs(lg - la) / la * 100 > F["response_size"]["tolerance_pct"]:
            other.append("Size: AWS %s B vs GCP %s B" % (la, lg))
    return lines, other, notes, sa, sc


# ------------------------------------------------------------------ one row of the report
def evaluate(t, res, skip=None):
    A, G = res.get("aws"), res.get("gcp")
    ex = t["case"]["expect"]
    o = t["op"]
    row = dict(key=t["key"], cat=t["cat"], grp=t["grp"], op=o["name"], mp=o["method"] + " " + o["path"],
               cs=t["case"]["name"], ty=t["case"]["type"], au=o["auth"], ex=ex,
               aa=auth_label(t, "aws"), ca=auth_label(t, "gcp"), a=None, c=None, am=None, cm=None,
               at="", ct="", td="-", slow=False, df="None", ot="-", sa=[], sc=[],
               cuA=(A or {}).get("curl", ""), cuC=(G or {}).get("curl", ""),
               ar=resp_text(A), cr=resp_text(G))
    if skip:
        row.update(res="SKIPPED", df=skip)
        return row
    for k, r in (("a", A), ("c", G)):
        if r:
            row[k] = r["status"]
            row[k + "m"] = r["ms"]
            row[k + "t"] = "avg of %s run(s); min %s, max %s" % (r["runs"], r["min"], r["max"])
    if A and G:
        lines, other, notes, sa, sc = compare(t, A, G)
        issues = lines + other
        row.update(res="FAIL" if issues else ("ACCEPTED" if notes else "PASS"), sa=sorted(sa), sc=sorted(sc),
                   df="\n".join(lines + notes) or "None", ot="\n".join(other) or "-")
        if A["ms"] and not A["error"] and not G["error"]:
            dp = round((G["ms"] - A["ms"]) / A["ms"] * 100)
            rt = F["response_time"]
            row["td"] = "%+d ms (%+d%%)" % (G["ms"] - A["ms"], dp)
            row["slow"] = rt["enabled"] and (dp > rt["slow_pct"] or G["ms"] > rt["slow_ms"])
    else:
        r = A or G
        probs = msg_issues(t, "AWS" if A else "GCP", r)
        ok = r["status"] == ex and not r["error"] and not probs
        why = ["Request failed: " + r["error"]] if r["error"] else (
            [] if r["status"] == ex else ["Unexpected status: expected %s, got %s" % (ex, r["status"])])
        row.update(res="PASS" if ok else "FAIL", df="None" if ok else "\n".join(why + probs))
    return row


# ------------------------------------------------------------------ test selection
def all_tests():
    for g in apis.GROUPS:
        for o in g["ops"]:
            pos = dict(name="Valid request", expect=o["expect"], vars={}, body=None, auth_mode=None,
                       tags=[], type="Positive", path=None, expect_msg=None, match="contains", static=False)
            for c in ([] if o["neg_only"] else [pos]) + o["cases"]:
                yield dict(cat=g["category"], grp=g["name"], host=o["host"] or g["host"], op=o, case=c,
                           key="%s|%s|%s" % (g["name"], o["name"], c["name"]))


def want(val, arg):
    return not arg or val.lower() in [x.strip().lower() for x in arg.split(",")]


def select(args):
    failed = None
    if args.rerun_failed:
        failed = {r["key"] for r in json.load(open(args.rerun_failed))["rows"] if r["res"] == "FAIL"}
    out = []
    for t in all_tests():
        c, o = t["case"], t["op"]
        if not (want(t["cat"], args.category) and want(t["grp"], args.group) and want(o["name"], args.service)
                and want(c["name"], args.case)):
            continue
        if args.type != "all" and c["type"].lower() != args.type:
            continue
        if args.tag and not set(x.lower() for x in args.tag.split(",")) & {x.lower() for x in c["tags"] + o["tags"]}:
            continue
        if failed is not None and t["key"] not in failed:
            continue
        out.append(t)
    return out


def check_prereqs(clouds):
    missing = {c: set() for c in clouds}
    for p in C.PREREQUISITES:
        for cl in clouds:
            t = dict(host=p["host"], op=dict(method="GET", path=p["path"], auth=p["auth"], fmt="json",
                                              overrides={}, body=None),
                     case=dict(vars={}, body=None, auth_mode=None))
            r = send(build(t, cl, C.VARS[cl]))
            if r["status"] != 200:
                missing[cl].add(p["name"])
                print("  prerequisite '%s' not found on %s (status %s)" % (p["name"], cl.upper(), r["status"]))
    return missing


def extract(body, key):
    m, _ = flat(body)
    for want_key in (key, "id"):
        for p, x in m.items():
            if p.split("/")[-1] == want_key:
                return x


# ------------------------------------------------------------------ main loop
def run(args):
    clouds = ["aws", "gcp"] if args.cloud == "both" else [args.cloud]
    if set(C.VARS["aws"]) != set(C.VARS["gcp"]):
        sys.exit("config.VARS: AWS and GCP must define the same variable names. Differences: %s" %
                 sorted(set(C.VARS["aws"]) ^ set(C.VARS["gcp"])))
    for cl in clouds:
        used = {n for p in C.AUTH.values() for n in PH.findall(json.dumps(p[cl]))}
        empty = [k for k in C.SECRET_PLACEHOLDER if k in used and not C.VARS[cl].get(k)]
        if empty and not args.dry_run:
            print("  warning: empty secret(s) for %s: %s (set the environment variables)" % (cl.upper(), ", ".join(empty)))
    tests = select(args)
    print("Selected %d test(s) on %s" % (len(tests), " + ".join(c.upper() for c in clouds)))
    base = json.load(open(args.baseline))["raw"] if args.baseline else {}
    pre = {c: set() for c in clouds} if args.dry_run else check_prereqs(clouds)
    ctx = {c: dict(C.VARS[c]) for c in clouds}      # fixed values, overwritten by ids created in this run
    fixed = {c: dict(C.VARS[c]) for c in clouds}    # fixed values only (used by static=True tests)
    rows, raw = [], {}
    for t in tests:
        o, c = t["op"], t["case"]
        positive = c["type"] == "Positive"
        use = fixed if (c.get("static") or o.get("static")) else ctx
        cx = {cl: {k: (x() if callable(x) else x) for k, x in use[cl].items()} for cl in clouds}
        skip = None
        if args.mode == "reuse" and positive and o["capture"]:
            skip = "Create step skipped in reuse mode (fixed IDs from config are used)"
        elif args.mode == "reuse" and positive and o["method"] == "DELETE" and not args.allow_destructive:
            skip = "Delete skipped in reuse mode so fixed records survive (use --allow-destructive)"
        for n in o["needs"]:
            for cl in clouds:
                if n in pre[cl]:
                    skip = "Prerequisite '%s' not found on %s" % (n, cl.upper())
        if not skip:
            for cl in clouds:
                left = leftovers(build(t, cl, cx[cl]), cx[cl])
                if left:
                    skip = "Missing or unfilled variable: %s (%s)" % (", ".join(left), cl.upper())
        if args.dry_run:
            print("\n[%s / %s / %s]" % (t["grp"], o["name"], c["name"]) + ("  SKIPPED: " + skip if skip else ""))
            for cl in clouds:
                print("  %s: %s" % (cl.upper(), curl(build(t, cl, cx[cl], masked=True))))
            continue
        if skip:
            row = evaluate(t, {}, skip)
        else:
            def fetch():
                r = call_all(t, clouds, cx)
                for other in ("aws", "gcp"):          # fill the cloud that was not run from the baseline file
                    if other not in r and base.get(t["key"], {}).get(other):
                        r[other] = base[t["key"]][other]
                return r
            res = fetch()
            row = evaluate(t, res)
            if row["res"] == "FAIL" and F["retry_failed"]["enabled"] and o["method"] == "GET" and len(res) == 2:
                res = fetch()
                row = evaluate(t, res)
            raw[t["key"]] = res
            if positive and o["capture"]:
                for cl in clouds:
                    for var, key in o["capture"].items():
                        val = extract(res[cl]["body"], key) if 200 <= (res[cl]["status"] or 0) < 300 else None
                        if val:
                            ctx[cl][var] = val
                        else:
                            ctx[cl].pop(var, None)
        rows.append(row)
        print("  %-8s %s / %s / %s" % (row["res"], t["grp"], o["name"], c["name"]))
    return rows, raw, clouds


# ------------------------------------------------------------------ summary + outputs
def summary(rows):
    ex = [r for r in rows if r["res"] != "SKIPPED"]
    ok = lambda rs: sum(r["res"] in ("PASS", "ACCEPTED") for r in rs)
    pct = lambda rs: "%d%%" % round(ok(rs) / len(rs) * 100) if rs else "-"
    pos = [r for r in ex if r["ty"] == "Positive"]
    neg = [r for r in ex if r["ty"] == "Negative"]
    return [("Executed", len(ex)), ("Pass rate", pct(ex)), ("Passed", sum(r["res"] == "PASS" for r in rows)),
            ("Failed", sum(r["res"] == "FAIL" for r in rows)), ("Accepted differences", sum(r["res"] == "ACCEPTED" for r in rows)),
            ("Skipped", sum(r["res"] == "SKIPPED" for r in rows)), ("Positive pass rate", "%s (%d/%d)" % (pct(pos), ok(pos), len(pos))),
            ("Negative pass rate", "%s (%d/%d)" % (pct(neg), ok(neg), len(neg))), ("Slow calls", sum(bool(r["slow"]) for r in rows))]


def write_excel(rows, path):
    try:
        from openpyxl import Workbook
    except ImportError:
        print("  (install openpyxl to also get an .xlsx file: pip install openpyxl)")
        return
    wb = Workbook()
    ws = wb.active
    ws.title = "Parity results"
    ws.append([h for _, h in COLS])
    for r in rows:
        ws.append([str("" if r.get(k) is None else r[k])[:32000] for k, _ in COLS])
    s = wb.create_sheet("Summary")
    s.append(["Metric", "Value"])
    for k, v in summary(rows):
        s.append([k, v])
    wb.save(path)


def write_html(rows, clouds, path):
    meta = {"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "clouds": clouds}
    data = json.dumps(rows).replace("</", "<\\/")
    html = TEMPLATE.replace("__ROWS__", data).replace("__META__", json.dumps(meta))
    open(path, "w", encoding="utf-8").write(html)


def main():
    p = argparse.ArgumentParser(description="AWS to GCP API parity runner")
    p.add_argument("--cloud", choices=["aws", "gcp", "both"], default="both")
    p.add_argument("--category"); p.add_argument("--group"); p.add_argument("--service")
    p.add_argument("--case"); p.add_argument("--tag")
    p.add_argument("--type", choices=["positive", "negative", "all"], default="all")
    p.add_argument("--mode", choices=["create", "reuse"], default="create")
    p.add_argument("--allow-destructive", action="store_true")
    p.add_argument("--baseline"); p.add_argument("--rerun-failed")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    rows, raw, clouds = run(args)
    if args.dry_run:
        return
    os.makedirs(C.OUTPUT_DIR, exist_ok=True)
    stem = os.path.join(C.OUTPUT_DIR, "parity_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    write_html(rows, clouds, stem + ".html")
    write_excel(rows, stem + ".xlsx")
    json.dump({"rows": rows, "raw": raw}, open(stem + ".json", "w"))
    print("\n" + "\n".join("  %-22s %s" % kv for kv in summary(rows)))
    print("\nReport: %s.html  (also .xlsx and .json)" % stem)


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AWS vs GCP API parity report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
<style>
:root{--bg:#f5f7f9;--sf:#fff;--tx:#1b2430;--mu:#66727f;--bd:#d9e0e6;--ok:#14794a;--okb:#dff3e8;--bad:#b3261e;--badb:#fbe3e1;--ac:#1f5fa8;--wn:#8a5a00;--wnb:#fdf0d2}
@media (prefers-color-scheme:dark){:root{--bg:#12171d;--sf:#1a2129;--tx:#e6ebf0;--mu:#93a0ad;--bd:#2c3641;--ok:#5fd596;--okb:#143524;--bad:#ff8a80;--badb:#3d1c1a;--ac:#7db4f0;--wn:#f0c25f;--wnb:#3a2e0f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1400px;margin:0 auto;padding:20px 16px 40px}h1{font-size:22px;margin:0 0 2px}.sub{color:var(--mu);margin:0 0 12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:10px 12px;text-align:left;cursor:pointer;font:inherit;color:inherit}
.card b{display:block;font-size:26px;line-height:1.1}.card span{color:var(--mu);font-size:12.5px;display:block}.card.on{outline:2px solid var(--ac)}
.card.ok b{color:var(--ok)}.card.bad b{color:var(--bad)}.card.wn b{color:var(--wn)}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
.bar input,.bar select,.bar button{font:inherit;color:var(--tx);background:var(--sf);border:1px solid var(--bd);border-radius:6px;padding:7px 9px}
.bar input[type=search]{flex:1 1 220px}.bar button{cursor:pointer}.bar button.pri{background:var(--ac);color:#fff;border-color:var(--ac);font-weight:600}
.bar label{display:flex;align-items:center;gap:5px;color:var(--mu)}.count{color:var(--mu);margin:0 0 8px}
.wrap{overflow-x:auto;background:var(--sf);border:1px solid var(--bd);border-radius:8px;margin-bottom:14px}
table{border-collapse:collapse;width:100%}#tbl{min-width:3000px}#tbl.compact{min-width:1800px}#tbl.compact .dt{display:none}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--bd);vertical-align:top}
th{position:sticky;top:0;background:var(--sf);cursor:pointer;white-space:nowrap;font-weight:600}.gt th{position:static;cursor:default}
.gr{cursor:pointer}.gr:hover{background:var(--bg)}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-weight:600;font-size:12px}
.p-ok{background:var(--okb);color:var(--ok)}.p-bad{background:var(--badb);color:var(--bad)}.p-wn{background:var(--wnb);color:var(--wn)}.p-mu{background:var(--bg);color:var(--mu);border:1px solid var(--bd)}
.m,.full{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px}
.full{width:340px;margin:0;white-space:pre-wrap;word-break:break-all}.ln{display:block;min-height:1em}
.dl{background:var(--badb);color:var(--bad)}.dx{background:var(--okb);color:var(--ok)}
.df{min-width:260px;max-width:340px;white-space:pre-wrap}.mis{color:var(--bad);font-weight:600}.empty{padding:24px;text-align:center;color:var(--mu)}
</style></head><body><main>
<h1>AWS vs GCP API parity report</h1>
<p class="sub" id="meta"></p>
<div class="cards" id="cards"></div><p class="sub" id="lat"></p>
<div class="wrap"><table class="gt" id="gt"></table></div>
<div class="bar"><input id="q" type="search" placeholder="Search anything: group, operation, field, status, error text, curl...">
<select id="fc"></select><select id="fg"></select><select id="fs"></select><select id="ft"></select><select id="fa"></select><select id="fr"></select>
<label><input type="checkbox" id="cp"> Hide curl and responses</label><button id="rs">Reset</button><button id="ex" class="pri">Export to Excel</button></div>
<p class="count" id="cnt"></p>
<div class="wrap"><table id="tbl"><thead><tr id="hd"></tr></thead><tbody id="tb"></tbody></table></div>
</main>
<script>
var R=__ROWS__,META=__META__;
var $=function(i){return document.getElementById(i)};
function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")}
var isPass=function(r){return r.res==="PASS"||r.res==="ACCEPTED"},execd=function(r){return r.res!=="SKIPPED"};
var pct=function(p,n){return n?Math.round(p/n*100)+"%":"-"};
$("meta").textContent="Generated "+META.generated+" - clouds run: "+META.clouds.join(", ").toUpperCase()+". A test passes when status, fields, types, headers and size match. Accepted differences count as passed; skipped tests are left out of the pass rate. Secrets in curl commands are plain placeholders such as PROGRAM_SECRET: put your own value in before running.";
function ratio(f){var e=R.filter(function(r){return execd(r)&&f(r)}),p=e.filter(isPass).length;return[p+" of "+e.length+" passed",pct(p,e.length)]}
var CARDS=[["rate","Pass rate","ok",execd,function(){var e=R.filter(execd),p=e.filter(isPass).length;return[pct(p,e.length),p+" of "+e.length+" executed"]}],
["pass","Passed","ok",function(r){return r.res==="PASS"}],["fail","Failed","bad",function(r){return r.res==="FAIL"}],
["acc","Accepted differences","wn",function(r){return r.res==="ACCEPTED"}],["skip","Skipped","wn",function(r){return r.res==="SKIPPED"}],
["pos","Positive pass rate","",function(r){return r.ty==="Positive"&&execd(r)},function(){var q=ratio(function(r){return r.ty==="Positive"});return[q[1],q[0]]}],
["neg","Negative pass rate","",function(r){return r.ty==="Negative"&&execd(r)},function(){var q=ratio(function(r){return r.ty==="Negative"});return[q[1],q[0]]}],
["slow","Slow calls","wn",function(r){return r.slow}]];
var F={q:"",c:"",g:"",s:"",t:"",a:"",r:"",card:""},sk=null,sd=1;
function drawCards(){$("cards").innerHTML=CARDS.map(function(c){var v=c[4]?c[4]():[R.filter(c[3]).length,""];
return '<button class="card '+c[2]+(F.card===c[0]?' on':'')+'" data-c="'+c[0]+'"><b>'+v[0]+'</b><span>'+c[1]+'</span><span>'+v[1]+'</span></button>'}).join("")}
function drawTop(){var e=R.filter(function(r){return r.am!=null&&r.cm!=null&&r.am>0});
if(e.length){var av=function(k){return Math.round(e.reduce(function(s,r){return s+r[k]},0)/e.length)},a=av("am"),c=av("cm"),sl=e.slice().sort(function(x,y){return y.cm-x.cm})[0];
$("lat").textContent="Average response time: AWS "+a+" ms, GCP "+c+" ms ("+(c>=a?"+":"")+Math.round((c-a)/a*100)+"% for GCP). Slowest GCP call: "+sl.op+" at "+sl.cm+" ms."}
var gs=[];R.forEach(function(r){if(gs.indexOf(r.grp)<0)gs.push(r.grp)});
$("gt").innerHTML='<thead><tr><th>Category</th><th>Group</th><th>Executed</th><th>Passed</th><th>Failed</th><th>Skipped</th><th>Pass rate</th></tr></thead><tbody>'+gs.map(function(g){var rs=R.filter(function(r){return r.grp===g}),e2=rs.filter(execd),p=e2.filter(isPass).length;
return '<tr class="gr" data-g="'+esc(g)+'"><td>'+rs[0].cat+'</td><td>'+esc(g)+'</td><td>'+e2.length+'</td><td>'+p+'</td><td>'+e2.filter(function(r){return r.res==="FAIL"}).length+'</td><td>'+(rs.length-e2.length)+'</td><td>'+pct(p,e2.length)+'</td></tr>'}).join("")+'</tbody>'}
function opts(id,label,k){var v=R.map(function(r){return r[k]}).filter(function(x,i,a){return a.indexOf(x)===i});$(id).innerHTML='<option value="">'+label+'</option>'+v.map(function(x){return '<option>'+esc(x)+'</option>'}).join("")}
opts("fc","All categories","cat");opts("fg","All groups","grp");opts("fs","All operations","op");opts("ft","Positive and negative","ty");opts("fa","All auth profiles","au");opts("fr","All results","res");
var H=[["cat","Category"],["grp","Group"],["op","Operation"],["mp","Method"],["cs","Test case"],["ty","Type"],["aa","AWS auth"],["ca","GCP auth"],["ex","Expected"],["a","AWS status"],["c","GCP status"],["am","AWS ms"],["cm","GCP ms"],["td","Time difference"],["df","Difference"],["ot","Headers, size, types"],["res","Result"],["cuA","AWS request sent (curl)",1],["cuC","GCP request sent (curl)",1],["ar","AWS response",1],["cr","GCP response",1]];
$("hd").innerHTML=H.map(function(h){return '<th data-k="'+h[0]+'"'+(h[2]?' class="dt"':'')+'>'+h[1]+'</th>'}).join("");
function vis(){var q=F.q.toLowerCase(),cd=F.card?CARDS.filter(function(c){return c[0]===F.card})[0][3]:null;
var o=R.filter(function(r){
if(F.c&&r.cat!==F.c||F.g&&r.grp!==F.g||F.s&&r.op!==F.s||F.t&&r.ty!==F.t||F.a&&r.au!==F.a||F.r&&r.res!==F.r)return false;
if(cd&&!cd(r))return false;
if(q&&H.map(function(h){return r[h[0]]}).join(" ").toLowerCase().indexOf(q)<0)return false;return true});
if(sk)o.sort(function(a,b){return (a[sk]>b[sk]?1:a[sk]<b[sk]?-1:0)*sd});return o}
function hl(t,set,cls){return t.split("\n").map(function(l){var m=/^\s*(?:<([\w:.-]+)>|"([\w.-]+)":)/.exec(l),k=m&&(m[1]||m[2]);return '<span class="ln'+(k&&set.indexOf(k)>=0?' '+cls:'')+'">'+esc(l)+'</span>'}).join("")}
function pill(r){var c={PASS:"p-ok",FAIL:"p-bad",ACCEPTED:"p-wn",SKIPPED:"p-mu"}[r.res];return '<span class="pill '+c+'">'+r.res+'</span>'}
function st(r,v){return v==null?"-":(v!==r.ex?'<span class="mis">'+v+'</span>':v)}
function tm(v,t){return v==null?"-":'<span title="'+esc(t)+'">'+v+'</span>'}
function draw(){drawCards();var o=vis();$("cnt").textContent="Showing "+o.length+" of "+R.length+" tests";
if(!o.length){$("tb").innerHTML='<tr><td colspan="21" class="empty">No tests match these filters. Reset filters to see all results.</td></tr>';return}
$("tb").innerHTML=o.map(function(r){
return '<tr><td>'+r.cat+'</td><td>'+esc(r.grp)+'</td><td>'+esc(r.op)+'</td><td class="m">'+esc(r.mp)+'</td><td>'+esc(r.cs)+'</td><td><span class="pill '+(r.ty==="Negative"?'p-wn':'p-ok')+'">'+r.ty+'</span></td><td>'+esc(r.aa)+'</td><td>'+esc(r.ca)+'</td><td>'+r.ex+'</td><td>'+st(r,r.a)+'</td><td>'+st(r,r.c)+'</td><td>'+tm(r.am,r.at)+'</td><td>'+tm(r.cm,r.ct)+'</td><td>'+esc(r.td)+(r.slow?' <span class="pill p-wn">SLOW</span>':"")+'</td><td class="df'+(r.df==="None"?'':' mis')+'">'+esc(r.df)+'</td><td class="df'+(r.ot==="-"?'':' mis')+'">'+esc(r.ot)+'</td><td>'+pill(r)+'</td><td class="dt"><pre class="full">'+esc(r.cuA)+'</pre></td><td class="dt"><pre class="full">'+esc(r.cuC)+'</pre></td><td class="dt"><pre class="full">'+hl(r.ar,r.sa,"dl")+'</pre></td><td class="dt"><pre class="full">'+hl(r.cr,r.sc,"dx")+'</pre></td></tr>'}).join("")}
$("cards").onclick=function(e){var b=e.target.closest(".card");if(b){F.card=F.card===b.dataset.c?"":b.dataset.c;draw()}};
$("gt").onclick=function(e){var t=e.target.closest(".gr");if(t){F.g=t.dataset.g;$("fg").value=F.g;draw()}};
$("hd").onclick=function(e){var t=e.target.closest("th");if(!t)return;var k=t.dataset.k;sd=sk===k?-sd:1;sk=k;draw()};
$("q").oninput=function(){F.q=this.value;draw()};
$("cp").onchange=function(){$("tbl").className=this.checked?"compact":""};
[["fc","c"],["fg","g"],["fs","s"],["ft","t"],["fa","a"],["fr","r"]].forEach(function(p){$(p[0]).onchange=function(){F[p[1]]=this.value;draw()}});
$("rs").onclick=function(){F={q:"",c:"",g:"",s:"",t:"",a:"",r:"",card:""};$("q").value="";["fc","fg","fs","ft","fa","fr"].forEach(function(i){$(i).value=""});draw()};
$("ex").onclick=function(){var rows=vis().map(function(r){var o={};H.forEach(function(h){o[h[1]]=String(r[h[0]]==null?"":r[h[0]]).slice(0,32000)});return o});
if(!rows.length){$("cnt").textContent="Nothing to export: no rows match the current filters.";return}
if(window.XLSX){var ws=XLSX.utils.json_to_sheet(rows),wb=XLSX.utils.book_new();XLSX.utils.book_append_sheet(wb,ws,"Parity results");
XLSX.utils.book_append_sheet(wb,XLSX.utils.aoa_to_sheet([["Metric","Value","Detail"]].concat(CARDS.map(function(c){var v=c[4]?c[4]():[R.filter(c[3]).length,""];return[c[1],v[0],v[1]]}))),"Summary");
XLSX.writeFile(wb,"aws_gcp_parity_report.xlsx")}
else{var ks=Object.keys(rows[0]),csv=[ks.join(",")].concat(rows.map(function(r){return ks.map(function(k){return '"'+String(r[k]).replace(/"/g,'""')+'"'}).join(",")})).join("\n"),a=document.createElement("a");
a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"}));a.download="aws_gcp_parity_report.csv";a.click()}};
drawTop();draw();
</script></body></html>
"""

if __name__ == "__main__":
    main()
