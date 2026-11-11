#!/usr/bin/env python3
"""Sizing advisor, ticket mode: should a planned task be split, merged with another, or kept?

  cc-estimate advise --size XL --paths src/lib/a.py src/api/b.py ui/c.tsx docs/d.md
  cc-estimate advise --plan plan.jsonl        # one JSON object per line: id, size, paths

Verdicts: Split, Merge, Keep, No advice. Each comes with one plain sentence saying why.

Break point. For each size bucket with at least 5 finished tasks (and 3 authors, or it is hidden),
take the spread of its 80% range relative to scale: log(p90 + 1) - log(p10 + 1) of work hours, so a
spread that is a constant fraction of the duration is the same at every size. The break point is the
smallest bucket whose spread is wider than the bucket directly below it by more than GROWTH (1.5x),
with the bootstrap 90% interval of the ratio (each bucket resampled, fixed seed) wholly above GROWTH.
The same is done by number of areas (1, 2, 3, 4+) when
`--profiles` (from `classify`) supplies areas; without them the advisor works on size only, and says so.

Split. A task at or above the break point is split, unless the history cannot support it. There is
no advice when:
  - fewer than 20 finished tasks lie on either side of the break point (by size; by area count for
    the area break), or
  - past splits were slower. A past split is a set of two or more tasks with the same `parent`
    (a sub-issue link; no source writes it yet, issue #22). Its total is the SUM of the slices'
    working hours, compared with single tasks (no parent) whose product-code size equals the set's
    summed size. Splitting counts as faster when, over the buckets at or above the break that have
    at least 3 sets and 3 single tasks, the set medians (weighted by set count) are below the single
    medians, with at least 5 sets overall. Without parent links, or without enough sets, the advice
    rests on the break rule alone and the report says past splits could not be checked.
Tasks below the size L get Keep when the break cannot be judged; L and XL get No advice.

Slices. Paths are grouped by layer. Docs and tests are recognised first (the config's
docs_patterns and tests_patterns), then the layers by pattern, first match wins. Slices run in
dependency order: shared code, services, other code (paths matching nothing), UI, tests, docs. The
layer names and patterns can be set in the config under "advise": {"layers": [{"name", "patterns"}]}. Each slice is sized from the history's median product
code for the task's size, shared across slices in proportion to their code files. The tool sees
file counts, not lines. Docs and tests carry no product code, so their slice is XS.

Merge. Two planned tasks that are both XS or S and touch the same area (first path segment, or the
classifier's `area_depth`). Merge needs no history.

Groups with fewer than 5 tasks or 3 authors print counts only, as in `backtest`.
"""
import argparse, json, math, os, random, re, statistics as st, sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import backtest as bt
from . import classify as cl
from . import estimate as est
from .history import MIN_AUTHORS, MIN_CELL

SIZES = [name for _, name in bt.BUCKETS]
GROWTH = 1.5         # a break: the bootstrap 90% interval of the spread ratio to the bucket below lies wholly above this
BOOT = 500
MIN_SIDE = 20        # finished tasks needed on each side of the break
MIN_SETS, MIN_PER_BUCKET = 5, 3
AREA_GROUPS = ("1", "2", "3", "4+")
DEFAULT_LAYERS = [
    ("shared code", [r"(^|/)(shared|common|core|lib|libs|utils?)/"]),
    ("services", [r"(^|/)(services?|api|server|backend|handlers?|routes?)/"]),
    ("other code", []),
    ("UI", [r"(^|/)(ui|web|frontend|client|components?|views?|pages?)/"]),
]
DEFAULT_DOCS = [r"\.(md|mdx|rst|adoc)$", r"(^|/)docs?/"]
DEFAULT_TESTS = [r"(^|/)(tests?|spec|__tests__|e2e)/", r"(^|/)(test_[^/]+|[^/]+_test\.[a-z]+)$", r"\.(test|spec)\.[a-z]+$"]


def layers(cfg):
    custom = (cfg.get("advise") or {}).get("layers")
    return [(l["name"], l["patterns"]) for l in custom] if custom else DEFAULT_LAYERS


def layer_of(path, cfg):
    """Docs and tests first, then the layers in order; a path that matches none goes to the layer with no patterns."""
    if any(re.search(p, path) for p in cfg.get("docs_patterns", DEFAULT_DOCS)):
        return "docs"
    if any(re.search(p, path) for p in cfg.get("tests_patterns", DEFAULT_TESTS)):
        return "tests"
    ls = layers(cfg)
    for name, pats in ls:
        if any(re.search(p, path) for p in pats):
            return name
    return next((n for n, p in ls if not p), ls[-1][0])


def layer_order(cfg):
    """Dependency order: the configured layers (shared code first), then tests, then docs."""
    return [n for n, _ in layers(cfg)] + ["tests", "docs"]


def shown(rs):
    return len(rs) >= MIN_CELL and len({r["author"] for r in rs}) >= MIN_AUTHORS


def spread(ys):
    """Width of the 80% range in log hours, so a spread that is a constant fraction of the duration is the same at every size."""
    return math.log(bt.q(ys, est.HI_P) + 1) - math.log(bt.q(ys, est.LO_P) + 1)


def jump(prev, cur):
    """Bootstrap 90% interval of spread(cur) / spread(prev): resample each bucket with a fixed seed."""
    rnd = random.Random(0)
    ratios = sorted(spread(rnd.choices(cur, k=len(cur))) / max(spread(rnd.choices(prev, k=len(prev))), 1e-9)
                    for _ in range(BOOT))
    return ratios[int(BOOT * 0.05)], ratios[int(BOOT * 0.95)]


def find_break(groups, members):
    """The first group whose spread is wider than the one below by more than GROWTH, with the bootstrap
    interval of the ratio above GROWTH. Groups under 5 tasks or hidden for privacy cannot be compared, nor can the one above.
    detail: (group, n, spread, shown, ratio, interval)"""
    prev = None
    detail = []
    for g in groups:
        rs = members[g]
        ok = len(rs) >= est.MIN_N and shown(rs)
        ys = [r["y"] for r in rs]
        w = spread(ys) if ok else None
        ratio = ci = None
        if prev is not None and w is not None and prev[1] is not None and prev[1] > 0:
            ratio, ci = w / prev[1], jump(prev[2], ys)
        detail.append((g, len(rs), w, shown(rs), ratio, ci))
        if ci and ci[0] > GROWTH:
            return g, detail
        prev = (g, w, ys) if w is not None else (g, None, None)
    return None, detail


def area_group(n):
    return AREA_GROUPS[min(n, 4) - 1] if n else AREA_GROUPS[0]


def load_history(tasks_path, tz, cfg, profiles_path=None):
    if not tasks_path or not Path(tasks_path).exists():
        return None
    profiles = {p["id"]: p for p in map(json.loads, Path(profiles_path).read_text().splitlines())} \
        if profiles_path and Path(profiles_path).exists() else {}
    rows = bt.load(tasks_path, tz, cfg, profiles)
    if not rows:
        return None
    cut = max(r["end"] for r in rows) - timedelta(days=est.CENSOR_DAYS)
    return dict(rows=[r for r in rows if r["start"] <= cut], newest=max(r["end"] for r in rows),
                has_areas=any(r["areas"] for r in rows), tz=tz)


def split_evidence(rows, break_size):
    """Did past splits at or above the break take less work in total than single tasks of the same total size?
    A split is the tasks sharing a `parent` (sub-issue) link; its total is the SUM of their working hours.
    Returns (verdict, text) with verdict True, False or None (cannot check)."""
    if not any(r["parent"] is not None for r in rows):
        return None, "this history has no parent links between tasks, so past splits cannot be found"
    by_parent = {}
    for r in rows:
        if r["parent"] is not None:
            by_parent.setdefault(r["parent"], []).append(r)
    sets = [(bt.bucket(sum(r["code_lines"] for r in rs)), sum(r["y_raw"] for r in rs)) for rs in by_parent.values() if len(rs) > 1]
    singles = [r for r in rows if r["parent"] is None]
    cmp = []
    for size in SIZES[SIZES.index(break_size):]:
        s = [t for b, t in sets if b == size]
        one = [r["y_raw"] for r in singles if r["size"] == size]
        if len(s) >= MIN_PER_BUCKET and len(one) >= MIN_PER_BUCKET:
            cmp.append((size, len(s), st.median(s), st.median(one)))
    n_sets = sum(c[1] for c in cmp)
    if n_sets < MIN_SETS:
        return None, f"only {n_sets} past splits at or above {break_size} can be compared (need {MIN_SETS})"
    split_m = sum(c[1] * c[2] for c in cmp) / n_sets
    one_m = sum(c[1] * c[3] for c in cmp) / n_sets
    text = (f"{n_sets} past splits at or above {break_size} took a median of {split_m:.1f} work hours in total, "
            f"against {one_m:.1f} for single tasks of the same size")
    return split_m < one_m, text


def analyse(h):
    """Everything the verdicts need from the history, as a dict."""
    rows = h["rows"]
    members = {s: [r for r in rows if r["size"] == s] for s in SIZES}
    size_break, size_detail = find_break(SIZES, members)
    out = dict(n=len(rows), newest=h["newest"], size_detail=size_detail, size_break=size_break,
               area_break=None, area_detail=None, has_areas=h["has_areas"], blocker=None, evidence=None)
    if h["has_areas"]:
        amem = {g: [r for r in rows if r["areas"] and area_group(len(r["areas"])) == g] for g in AREA_GROUPS}
        out["area_break"], out["area_detail"] = find_break(AREA_GROUPS, amem)
    if not size_break:
        out["blocker"] = ("no size bucket has an 80% range clearly wider, relative to its duration, than the bucket below, "
                          "so there is no size at which splitting helps")
        return out
    lower = sum(1 for r in rows if SIZES.index(r["size"]) < SIZES.index(size_break))
    upper = len(rows) - lower
    out["sides"] = (lower, upper)
    if min(lower, upper) < MIN_SIDE:
        out["blocker"] = (f"history is too thin around the break point {size_break}: {lower} finished tasks below it "
                          f"and {upper} at or above it, and each side needs {MIN_SIDE}")
        return out
    ok, text = split_evidence(rows, size_break)
    out["evidence"] = text
    if ok is False:
        out["blocker"] = f"past splits were not faster in total than single tasks of the same size: {text}"
    return out


def slices(paths, size, rows, cfg):
    """Group paths into dependency-ordered slices, each with a size from history."""
    code_layers = {n for n, _ in layers(cfg)}
    groups = {}
    for p in paths:
        groups.setdefault(layer_of(p, cfg), []).append(p)
    out = []
    pool = [r["code_lines"] for r in rows if r["size"] == size]
    total = st.median(pool) if pool else dict(zip(SIZES, (25, 150, 700, 3000, 7000)))[size]
    weight = sum(len(v) for k, v in groups.items() if k in code_layers)
    for name in layer_order(cfg):
        if name not in groups:
            continue
        ps = groups[name]
        lines = total * len(ps) / weight if name in code_layers and weight else 0
        out.append(dict(layer=name, paths=ps, size=bt.bucket(lines) if lines else "XS", lines=lines))
    return out


def size_range(rows, size):
    rs = [r for r in rows if r["size"] == size]
    if len(rs) < est.MIN_N:
        return None, len(rs)
    if not shown(rs):
        return "hidden", len(rs)
    ys = [r["y"] for r in rs]
    return [bt.q(ys, est.LO_P), bt.q(ys, 0.5), bt.q(ys, est.HI_P)], len(rs)


def areas_of(paths, cfg):
    depth = (cfg.get("classifier") or {}).get("area_depth", {})
    return {cl.area(p, depth) for p in paths}


def merge_pairs(plan, cfg):
    """task id -> partner id, for the first same-area partner among XS/S tasks."""
    small = [t for t in plan if t["size"] in ("XS", "S")]
    pair = {}
    for i, a in enumerate(small):
        for b in small[i + 1:]:
            if a["id"] in pair or b["id"] in pair:
                continue
            if areas_of(a["paths"], cfg) & areas_of(b["paths"], cfg):
                pair[a["id"]], pair[b["id"]] = b["id"], a["id"]
    return pair


def verdict(task, plan, an, merge, hist, cfg):
    """(verdict, reason, slices)"""
    if task["id"] in merge:
        other = next(t for t in plan if t["id"] == merge[task["id"]])
        shared = sorted(areas_of(task["paths"], cfg) & areas_of(other["paths"], cfg))
        return "Merge", (f"it is {task['size']}, {other['id']} is {other['size']}, and both touch "
                         f"{', '.join(shared)}: one task saves a hand-off"), None
    large = task["size"] in ("L", "XL")
    if an is None:
        return ("No advice", "there is no finished-task history to read", None) if large else \
            ("Keep", "nothing in the plan says to change it", None)
    if an["blocker"]:
        if large:
            return "No advice", an["blocker"], None
        return "Keep", f"{task['size']} is small enough that splitting is not on the table", None
    over_size = SIZES.index(task["size"]) >= SIZES.index(an["size_break"])
    over_area = False
    if an["area_break"] and task["paths"]:
        over_area = AREA_GROUPS.index(area_group(len(areas_of(task["paths"], cfg)))) >= AREA_GROUPS.index(an["area_break"])
    if not (over_size or over_area):
        return "Keep", f"{task['size']} is below the break point {an['size_break']}", None
    why = (f"{task['size']} reaches the size break point ({an['size_break']}), where the 80% range gets much wider relative to the duration" if over_size
           else f"it touches {len(areas_of(task['paths'], cfg))} areas, at or above the area break point {an['area_break']}")
    if not task["paths"]:
        return "Split", why + "; add --paths to see the slices", []
    sl = slices(task["paths"], task["size"], hist["rows"], cfg)
    if len(sl) < 2:
        return "Keep", why + ", but the paths sit in one layer, so there is nothing to split along", None
    return "Split", why, sl


def fmt_hours(r):
    d = est.HOURS_PER_DAY
    return f"{r[0]:.1f}-{r[2]:.1f} work hours ({r[0] / d:.1f}-{r[2] / d:.1f} working days)"


def report(plan, hist, an, cfg, out=print):
    if an is None:
        out("No history: build it first (README, Quick start). Only the merge rule can run.")
    else:
        out(f"History: {an['n']} finished tasks (data up to {an['newest']:%Y-%m-%d}).")
        out("Spread of the 80% range by size, in log work hours (equal at every size when the range is a fixed share of the duration):")
        for g, n, w, vis, ratio, ci in an["size_detail"]:
            if w is None or not vis:
                out(f"  {g:2} n={n}" + ("  (too few tasks or people to show)" if n else ""))
            else:
                jmp = f"  x{ratio:.1f} vs the bucket below (90% interval {ci[0]:.1f}-{ci[1]:.1f})" if ratio else ""
                out(f"  {g:2} n={n}  spread {w:.2f}{jmp}")
        out(f"Break point by size: {an['size_break'] or 'none'}.")
        if an["has_areas"]:
            out(f"Break point by number of areas: {an['area_break'] or 'none'}.")
        else:
            out("No area data in this history (pass --profiles from classify): the advice uses size only.")
        if an["evidence"]:
            out(f"Past splits: {an['evidence']}." if an["blocker"] or "took a median" in an["evidence"]
                else f"Past splits could not be checked: {an['evidence']}.")
    merge = merge_pairs(plan, cfg)
    for t in plan:
        v, why, sl = verdict(t, plan, an, merge, hist, cfg)
        out(f"\n{t['id']} ({t['size']}): {v}. {why[0].upper() + why[1:]}.")
        if sl:
            out("  Slices, in dependency order. Ordered by the layer rule (matching path patterns): "
                + ", then ".join(n for n in layer_order(cfg) if n in {s["layer"] for s in sl}) + ".")
            for i, s in enumerate(sl, 1):
                rng, n = size_range(hist["rows"], s["size"])
                if rng == "hidden":
                    est_txt = f"n={n}, range hidden (too few people)"
                elif rng:
                    est_txt = f"{fmt_hours(rng)}, median {rng[1]:.1f} h, n={n}"
                else:
                    est_txt = f"too little {s['size']} history (n={n}) for a range"
                out(f"  {i}. {s['layer']}: {len(s['paths'])} file(s), size {s['size']}: {est_txt}")
            out("  Slice sizes assume product-code lines in proportion to code files; the tool cannot see lines per file.")


def read_plan(a):
    if a.plan:
        rows = [json.loads(l) for l in Path(a.plan).read_text().splitlines() if l.strip()]
    else:
        rows = [dict(id="task", size=a.size, paths=a.paths or [])]
    for r in rows:
        if r.get("size") not in SIZES:
            sys.exit(f"cc-estimate advise: {r.get('id')}: size must be one of {', '.join(SIZES)}.")
        r["paths"] = r.get("paths") or []
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cc-estimate advise", description="Split, merge or keep planned tasks.")
    ap.add_argument("--size", choices=SIZES, help="size of the planned task (XS <50 ... XL >=5,000 product-code lines)")
    ap.add_argument("--paths", nargs="*", help="files or directories you expect to change")
    ap.add_argument("--plan", help="JSON lines: id, size, paths")
    ap.add_argument("--config", default=os.environ.get("CC_ESTIMATE_CONFIG", "cc-estimate.json"))
    ap.add_argument("--tasks")
    ap.add_argument("--profiles", help="profiles jsonl from classify, for the break by number of areas")
    a = ap.parse_args(argv)
    if bool(a.plan) == bool(a.size):
        ap.error("give either --size (with --paths) or --plan")
    cfg = json.loads(Path(a.config).read_text()) if Path(a.config).exists() else {}
    data = Path(a.config).resolve().parent / cfg.get("data_dir", "data")
    tasks = a.tasks or str(data / f"{cfg.get('repo', 'local/repo').split('/')[-1]}.tasks.jsonl")
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    hist = load_history(tasks, tz, cfg, a.profiles)
    report(read_plan(a), hist, analyse(hist) if hist else None, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
