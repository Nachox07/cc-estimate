#!/usr/bin/env python3
"""Chronological backtest of task-duration estimates. Stdlib only.

Target: work hours (Mon-Fri, 09-18 local, minus absence days and excluded-label spans:
blocked, and waiting for a review gate that has since been removed)
from task start to its last merge.
Each task is estimated at its start using only tasks already finished by then.
As-of inputs: title kind, epic label, author. `size` rows assume a perfect planner who
names the final size bucket in advance, so they are an upper bound for what a planned size buys.
"""
import argparse, json, sys, statistics as st
from bisect import bisect_right
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .files import read_since

DAY0, DAY1 = 9, 18
BUCKETS = ((50, "XS"), (300, "S"), (1500, "M"), (5000, "L"), (10 ** 12, "XL"))


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def merge_spans(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def biz_hours(a, b, tz, off=frozenset(), blocked=()):
    """Mon-Fri 09-18 hours in [a, b], skipping `off` dates and time inside `blocked` spans."""
    a, b = a.astimezone(tz), b.astimezone(tz)
    if b <= a:
        return 0.0
    spans = merge_spans([[max(x, a), min(y, b)] for x, y in blocked if y > a and x < b])
    total, d = 0.0, a.replace(hour=0, minute=0, second=0, microsecond=0)
    while d <= b:
        if d.weekday() < 5 and d.date() not in off:
            lo = max(a, d.replace(hour=DAY0)); hi = min(b, d.replace(hour=DAY1))
            if hi > lo:
                total += (hi - lo).total_seconds()
                for x, y in spans:
                    ov = min(hi, y) - max(lo, x)
                    if ov.total_seconds() > 0:
                        total -= ov.total_seconds()
        d += timedelta(days=1)
    return total / 3600


def title_kind(t):
    t = t.lower()
    for k in ("feat", "fix", "chore", "ci", "docs", "test", "refactor", "perf", "revert"):
        if t.startswith(k):
            return k
    return "other"


def epic(labels):
    return next((l for l in labels if l.startswith("epic:")), None)


TEST_HEAVY = 3  # tests beyond this multiple of product code mark a task test-heavy


def bucket(lines):
    return next(name for limit, name in BUCKETS if lines < limit)


def off_dates(cfg, login):
    """Absence from config: {"absence": {"*": [...], "<login>": ["2026-10-14..2026-10-16"]}}."""
    ab = cfg.get("absence") or {}
    return frozenset(expand_dates(ab.get("*", []) + ab.get(login, [])))


def expand_dates(items):
    out = set()
    for it in items:
        a, _, b = it.partition("..")
        d, end = date.fromisoformat(a.strip()), date.fromisoformat((b or a).strip())
        while d <= end:
            out.add(d); d += timedelta(days=1)
    return out


def profile_fields(p):
    """Coarse workload profile: who must approve, whether slow test tiers run, how spread the change is."""
    if not p:
        return dict(approvals="?", e2e="?", spread="?", approval_set=frozenset())
    ap = p["approvals"]
    return dict(approvals="+".join(ap) if len(ap) <= 1 else "multi", e2e="e2e" if p["heavy_tiers"] else "-",
                spread=str(min(len(p["areas"]), 3)), approval_set=frozenset(ap))


def load(path, tz, cfg=None, profiles=None):
    profiles = profiles or {}
    rows, old = [], 0
    for r in map(json.loads, Path(path).read_text().splitlines()):
        old += "code_lines" not in r
        start, end = parse(r["start"]), parse(r["end"])
        blocked = [[parse(x), parse(y)] for spans in (r.get("excluded") or {}).values() for x, y in spans]
        off = off_dates(cfg or {}, r["author"])
        rows.append(dict(id=r["id"], start=start, end=end, end_kind=r.get("end_kind"), y=biz_hours(start, end, tz, off, blocked),
                         y_raw=biz_hours(start, end, tz), author=r["author"],
                         kind=title_kind(r["title"]), epic=epic(r["labels"]), size=bucket(r.get("code_lines", r["lines"])),
                         test_heavy=r.get("test_lines", 0) > TEST_HEAVY * r.get("code_lines", r["lines"]),
                         **profile_fields(profiles.get(r["id"]))))
    if old:
        print(f"{old} tasks have no code_lines; sized by total lines. Rebuild the tasks file to size by product code.",
              file=sys.stderr)
    rows.sort(key=lambda x: x["start"])
    add_load(rows)
    return rows


MIN_CENSOR_DAYS = 21
MAX_ITER = 50
MIN_ABOVE = 3      # observed tasks above the p90 needed to trust the weights
HEAVY_CAP_SHARE = 0.5  # H/cap of 0.5 is the window = 4x p90 limit; above it H reads low
HEAVY_SHARE = 0.05  # weight share of tasks longer than half the window that triggers a warning


def horizon(rows, w0, data_end):
    """Fixed point H >= MIN_CENSOR_DAYS: H is the p90 calendar duration of tasks starting by data_end - H.
    Such a task longer than H is seen only if it started early enough, so it is weighted by 1 / F(data_end - d),
    F being the start-time CDF of the tasks not longer than H (all of them seen); unweighted, the fixed point
    sits below the true p90. Valid while the window is at least ~4x the true p90: a task longer than the whole
    window is never fetched. H is capped at max(21, half the window). Returns (H, notes); the first note is
    always the summary line."""
    span = (data_end - w0).total_seconds() / 86400
    pts = sorted(((r["start"] - w0).total_seconds() / 86400, (r["end"] - r["start"]).total_seconds() / 86400) for r in rows)
    cap = max(MIN_CENSOR_DAYS, span / 2)

    def sample(h, weighted):
        mine = [(s, d) for s, d in pts if s <= span - h]
        short = [s for s, d in mine if d <= h]
        out = []
        for s, d in mine:
            w = 1.0
            if weighted and d > h:
                f = bisect_right(short, span - d) / len(short) if short else 0
                if f <= 0:
                    continue
                w = 1 / f
            out.append((d, w))
        return out

    def step(weighted):
        def go(h):
            pairs = sample(h, weighted)
            return round(min(cap, max(MIN_CENSOR_DAYS, wq(pairs, 0.9))), 2) if pairs else MIN_CENSOR_DAYS
        return go

    notes = []
    wh, ok = settle(step(True))
    wpairs = sample(wh, True)
    h, pairs = wh, wpairs
    if sum(d > h for d, _ in pairs) < MIN_ABOVE:
        h, ok = settle(step(False))
        pairs = sample(h, False)
        notes.append(f"horizon: fewer than {MIN_ABOVE} observed tasks above the weighted p90, so the unweighted p90 is used")
    notes.insert(0, f"horizon: {h:.1f} days (cap {cap:.1f}); {sum(d > h for d, _ in pairs)} observed tasks above it")
    if not ok:
        notes.append(f"horizon: the iteration did not settle; using {h:.1f} days")
    if wh >= cap - 0.01:
        notes.append(f"horizon: the weighted horizon hit its cap of {cap:.1f} days")
    short = ("the window is too short for this history, so the horizon is probably too low")
    total = sum(w for _, w in wpairs)
    if wh >= HEAVY_CAP_SHARE * cap:
        notes.append(f"horizon: the weighted horizon {wh:.1f} days is at least {HEAVY_CAP_SHARE:.0%} of its cap; {short}")
    elif total and sum(w for d, w in wpairs if d > span / 2) / total > HEAVY_SHARE:
        notes.append(f"horizon: tasks longer than half the window carry much of the weight; {short}")
    return h, notes


def settle(step, h=MIN_CENSOR_DAYS):
    """Iterate h -> step(h) to a fixed point. On a cycle, return its smallest value and False."""
    seen = []
    for _ in range(MAX_ITER):
        nxt = step(h)
        if abs(nxt - h) < 0.01:
            return nxt, True
        if nxt in seen:
            return min(seen[seen.index(nxt):]), False
        seen.append(nxt)
        h = nxt
    return h, False


def wq(pairs, p):
    """Weighted quantile with q()'s interpolation (equal weights give the same value as q)."""
    pairs = sorted(pairs)
    if len(pairs) == 1:
        return pairs[0][0]
    tot, before = sum(w for _, w in pairs) - pairs[-1][1], 0.0
    pos = []
    for d, w in pairs:
        pos.append((before / tot, d)); before += w
    for (p0, d0), (p1, d1) in zip(pos, pos[1:]):
        if p <= p1:
            return d0 + (d1 - d0) * (p - p0) / (p1 - p0) if p1 > p0 else d1
    return pos[-1][1]


def window(rows, since=None, censor_days=None):
    """Cut both edges of a merged-in-window fetch. Before the window only long-lived tasks are
    present; near its end only fast ones are. Returns (kept, info) with the window start, cut, horizon and drop counts."""
    if not rows:
        return [], None
    data_end = max(r["end"] for r in rows)
    w0 = since or min(r["end"] for r in rows)  # a merged-in-window fetch has no end before its start
    inside = [r for r in rows if r["start"] >= w0]
    notes = []
    if censor_days is None:
        merge_ended = [r for r in inside if r.get("end_kind") != "commit"]  # commit-ended tasks are cut short, not absent
        censor_days, notes = horizon(merge_ended or inside, w0, data_end)
        for n in notes:
            print(n, file=sys.stderr)
    cut = data_end - timedelta(days=censor_days)
    kept = [r for r in inside if r["start"] <= cut]
    return kept, dict(start=w0, cut=cut, horizon=censor_days, early=len(rows) - len(inside), late=len(inside) - len(kept), notes=notes)


def add_load(rows):
    """Work already in flight when a task starts: tasks open at that moment, overall and among those
    needing an approval group this task also needs. Both are knowable at the start."""
    for t in rows:
        open_ = [x for x in rows if x is not t and x["start"] <= t["start"] < x["end"]]
        t["load_all"] = len(open_)
        t["load_group"] = sum(1 for x in open_ if x["approval_set"] & t["approval_set"]) if t["approval_set"] \
            else sum(1 for x in open_ if not x["approval_set"])


def bucket_load(rows, fit_rows):
    """Low / mid / high by terciles fitted on `fit_rows` only (the tuning slice)."""
    for key in ("load_all", "load_group"):
        cuts = (q([r[key] for r in fit_rows], 1 / 3), q([r[key] for r in fit_rows], 2 / 3))
        for r in rows:
            r[key + "_b"] = "low" if r[key] <= cuts[0] else "mid" if r[key] <= cuts[1] else "high"


def q(xs, p):
    xs = sorted(xs)
    i = (len(xs) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def match(keys):
    return lambda h, t: [x["y"] for x in h if all(x[k] == t[k] for k in keys)]


def trailing(h, t):
    """Tasks that ended in the 30 days before the lagged cut (`ref`), which is t's start when there is no lag."""
    cut = t.get("ref", t["start"]) - timedelta(days=30)
    return [x["y"] for x in h if x["end"] >= cut]


METHODS = {
    "baseline: repo": lambda h, t: [x["y"] for x in h],
    "baseline: trailing 30d": trailing,
    "baseline: same author": match(["author"]),
    "kind+epic": match(["kind", "epic"]),
    "size": match(["size"]),
    "size+kind": match(["size", "kind"]),
    "size+kind+epic": match(["size", "kind", "epic"]),
    "size+kind x pace": match(["size", "kind"]),
    "profile": match(["approvals", "e2e", "spread"]),
    "profile+size": match(["approvals", "e2e", "spread", "size"]),
    "approvals+size": match(["approvals", "size"]),
    "e2e+size": match(["e2e", "size"]),
    "size+team load": match(["size", "load_all_b"]),
    "size+group load": match(["size", "load_group_b"]),
}
# On too few matches, fall back down this chain before the trailing-30-day baseline.
FALLBACK = {"profile+size": ["approvals+size", "size"], "approvals+size": ["size"], "e2e+size": ["size"],
            "size+team load": ["size"], "size+group load": ["size"]}
NEEDS_PROFILES = {"profile", "profile+size", "approvals+size", "e2e+size", "size+group load"}
PACE_PRIOR = 15
MIN_TASKS = 40   # below this a backtest says nothing useful
MIN_SCORED = 10  # fewest estimates a table row is printed for


def split(n, min_history):
    """Tuning starts after `min_history` tasks, or half the history if that is less; the last
    20% of the rest, and at least MIN_SCORED tasks, is the locked slice."""
    if n < MIN_TASKS:
        return None
    first = min(min_history, n // 2)
    rest = n - first
    return first, n - max(rest - int(rest * 0.8), MIN_SCORED)


def pace(h, t):
    """Shrunk median ratio of the author's past actuals to their size+kind group median."""
    mine = [x for x in h if x["author"] == t["author"]][-30:]
    ratios = []
    for x in mine:
        g = [y["y"] for y in h if y["size"] == x["size"] and y["kind"] == x["kind"] and y is not x]
        if len(g) >= 10 and st.median(g) > 0.25:
            ratios.append(max(x["y"], 0.25) / st.median(g))
    if not ratios:
        return 1.0
    w = len(ratios) / (len(ratios) + PACE_PRIOR)
    return w * st.median(ratios) + (1 - w)


def estimate(h, t, name, k_min, lo_p, hi_p):
    s = METHODS[name](h, t)
    fell = len(s) < k_min
    for alt in FALLBACK.get(name, []) if fell else []:
        s = METHODS[alt](h, t)
        if len(s) >= k_min:
            break
    if len(s) < k_min:
        s = trailing(h, t)
        if len(s) < k_min:
            s = [x["y"] for x in h]
    if len(s) < 5:
        return None
    f = pace(h, t) if name.endswith("pace") else 1.0
    return q(s, lo_p) * f, q(s, 0.5) * f, q(s, hi_p) * f, fell


def run(rows, idx, name, k_min, lo_p, hi_p, lag=0):
    """`lag` days: history leaves out tasks that started that recently, as the live estimate does."""
    out, alpha = [], 1 - (hi_p - lo_p)
    for i in idx:
        t = rows[i]
        h = [x for x in rows[:i] if x["end"] <= t["start"]]
        if lag:
            h = [x for x in h if x["start"] <= t["start"] - timedelta(days=lag)]
        e = estimate(h, dict(t, ref=t["start"] - timedelta(days=lag)), name, k_min, lo_p, hi_p)
        if not e:
            continue
        lo, med, hi, fell = e
        miss = max(lo - t["y"], 0) + max(t["y"] - hi, 0)
        out.append(dict(t=t, inside=lo <= t["y"] <= hi, width=hi - lo, ae=abs(t["y"] - med),
                        winkler=(hi - lo) + 2 / alpha * miss, fell=fell))
    return out


def score(o):
    return dict(n=len(o), coverage=sum(x["inside"] for x in o) / len(o), width=st.median(x["width"] for x in o),
                medae=st.median(x["ae"] for x in o), winkler=st.mean(x["winkler"] for x in o),
                fallback=sum(x["fell"] for x in o) / len(o))


def table(results, keep):
    print(f"  {'method':26} {'n':>5} {'cover':>6} {'width_h':>8} {'medAE_h':>7} {'winkler':>8} {'fallbk':>6}")
    shown = 0
    for name, o in results.items():
        o = [x for x in o if keep(x["t"])]
        if len(o) < MIN_SCORED:
            continue
        s = score(o)
        shown += 1
        print(f"  {name:26} {s['n']:5d} {s['coverage']:6.2f} {s['width']:8.1f} {s['medae']:7.1f} {s['winkler']:8.1f} {s['fallback']:6.2f}")
    if not shown:
        print(f"  fewer than {MIN_SCORED} tasks here: nothing to score")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="tasks jsonl from tasks.py")
    ap.add_argument("--tz", help="default: the config's timezone, else UTC")
    ap.add_argument("--min-history", type=int, default=200, help="tasks before tuning starts; capped at half the history")
    ap.add_argument("--k-min", type=int, default=10)
    ap.add_argument("--level", type=float, default=0.8)
    ap.add_argument("--config")
    ap.add_argument("--profiles", help="profiles jsonl from classify.py")
    ap.add_argument("--censor-days", type=float,
                    help="drop tasks starting this close to the data end: their slow siblings are still open "
                         "(default: learned from the data, at least 21)")
    ap.add_argument("--since", help="start of the fetched window, YYYY-MM-DD (default: the one git-tasks recorded, else the earliest task end)")
    a = ap.parse_args(argv)
    profiles = {p["id"]: p for p in map(json.loads, Path(a.profiles).read_text().splitlines())} if a.profiles else {}
    cfg = json.loads(Path(a.config).read_text()) if a.config else {}
    rows = load(a.inp, ZoneInfo(a.tz or cfg.get("timezone", "UTC")), cfg, profiles)
    since = datetime.fromisoformat(a.since).replace(tzinfo=ZoneInfo(a.tz or cfg.get("timezone", "UTC"))) if a.since else read_since(a.inp)
    rows, win = window(rows, since, a.censor_days)
    n = len(rows)
    s = split(n, a.min_history)
    if not s:
        print(f"{n} finished tasks: too few to backtest (needs {MIN_TASKS}).")
        return
    first, lock = s
    bucket_load(rows, rows[:lock])
    lo_p, hi_p = (1 - a.level) / 2, 1 - (1 - a.level) / 2
    print(f"dropped {win['early']} tasks starting before {win['start']:%Y-%m-%d} (left-truncation: only long-lived ones reach the window)")
    print(f"dropped {win['late']} tasks starting after {win['cut']:%Y-%m-%d} (right-censoring, horizon {win['horizon']:.1f} days)")
    nb = sum(r["y"] < r["y_raw"] - 0.01 for r in rows)
    print(f"tasks with blocked or absence time removed: {nb}")
    print(f"test-heavy tasks (tests over {TEST_HEAVY}x product code): {sum(r['test_heavy'] for r in rows)}")
    print(f"tasks: {n}  tune: [{first},{lock})  locked: [{lock},{n})  level={a.level}  locked from {rows[lock]['start']:%Y-%m-%d}")
    for size in ("XS", "S", "M", "L", "XL"):
        ys = [r["y"] for r in rows if r["size"] == size]
        authors = {r["author"] for r in rows if r["size"] == size}
        if len(ys) < 5 or len(authors) < 3:  # fewer and the durations describe one person's work
            print(f"  {size:2} n={len(ys):4d}")
            continue
        print(f"  {size:2} n={len(ys):4d}  business hours p10={q(ys,.1):6.1f} p50={q(ys,.5):6.1f} p90={q(ys,.9):6.1f}")
    strata = {"all tasks": lambda t: True, "planned M+": lambda t: t["size"] in ("M", "L", "XL"),
              "planned L+": lambda t: t["size"] in ("L", "XL")}
    for label, idx in (("TUNE", range(first, lock)), ("LOCKED", range(lock, n))):
        results = {name: run(rows, list(idx), name, a.k_min, lo_p, hi_p, win["horizon"]) for name in METHODS
                   if profiles or name not in NEEDS_PROFILES}  # without classify output they repeat other rows
        for sname, keep in strata.items():
            print(f"\n== {label} · {sname} ==")
            table(results, keep)


if __name__ == "__main__":
    main()
