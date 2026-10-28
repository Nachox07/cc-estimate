#!/usr/bin/env python3
"""Write a made-up task history with known truth, to test the tool against.

  cc-estimate simulate --scenario size-matters --seed 7 --out dir

Writes dir/tasks.jsonl (same contract as real task files) and dir/truth.json (the model the
durations were drawn from); advisor scenarios also write dir/plan.jsonl for `advise --plan`. Same scenario and seed give the same files.

Durations are drawn in working hours, then placed on the clock by walking Monday-Friday
09:00-18:00 UTC, at non-round minutes. A task therefore measures exactly the hours drawn, and
durations do not pile up at 0 or 9 hours the way clock-time offsets from round start times do.

A scenario is a small function `(rnd, n) -> (specs, model)`. A spec is
{"code", "tests", "docs", "hours"}; the model is the plain-language truth. Add one to SCENARIOS.
"""
import argparse, json, random, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAY_H, WEEK_H = 9, 45
BASE = datetime(2023, 1, 2, 9, tzinfo=timezone.utc)  # a Monday, far enough back that no history ends in the future
LOGINS = ["user-a", "user-b", "user-c"]
MIN_HOURS = 0.5  # keeps every duration clear of zero


def clock(w):
    """The clock time `w` working hours after BASE."""
    weeks, rem = divmod(w, WEEK_H)
    days, hour = divmod(rem, DAY_H)
    return BASE + timedelta(weeks=int(weeks), days=int(days), hours=hour)


def lognormal(rnd, median, sigma):
    return median * rnd.lognormvariate(0, sigma)


def size_driven(rnd, code):
    return MIN_HOURS + lognormal(rnd, 2 + code / 50, 0.5)


def pick_code(rnd):
    return max(1, int(rnd.choice([20, 150, 700, 2500, 7000]) * rnd.uniform(0.6, 1.4)))


def side_lines(rnd, code, heavy=False):
    """Tests and docs. Tests stay under 3x the product code unless `heavy` allows 5x."""
    ratios = [0.2, 0.5, 0.5, 1.0, 1.0] + ([5.0] if heavy else [])
    return int(code * rnd.choice(ratios) * rnd.uniform(0.6, 1.4)), \
        int(rnd.choice([0, 5, 40]) * rnd.uniform(0.6, 1.4))


def size_matters(rnd, n):
    """800 tasks: a larger locked slice keeps coverage within 70-90% by more than chance.
    Some tasks are test-heavy; tests never add time, so they must not change the size."""
    specs = []
    for _ in range(n):
        code = pick_code(rnd)
        tests, docs = side_lines(rnd, code, heavy=True)
        specs.append(dict(code=code, tests=tests, docs=docs, hours=size_driven(rnd, code)))
    return specs, dict(
        model="hours = 0.5 + (2 + code_lines / 50) * lognormal(sigma 0.5)",
        params=dict(sigma=0.5, base_hours=2, hours_per_code_line=0.02))


def null(rnd, n):
    """800 tasks: a larger locked slice keeps chance gaps between size and baseline inside 5%."""
    specs = []
    for _ in range(n):
        code = pick_code(rnd)
        tests, docs = side_lines(rnd, code)
        specs.append(dict(code=code, tests=tests, docs=docs, hours=MIN_HOURS + lognormal(rnd, 12, 0.9)))
    return specs, dict(
        model="hours = 0.5 + 12 * lognormal(sigma 0.9), drawn without looking at size",
        params=dict(median_hours=12, sigma=0.9))


def test_heavy(rnd, n):
    """A third of tasks are M by product code and carry 5x that in tests: XL if tests counted."""
    specs, heavy = [], []
    for i in range(n):
        if i % 3 == 0:
            code = rnd.randint(1100, 1450)
            specs.append(dict(code=code, tests=5 * code, docs=0, hours=size_driven(rnd, code)))
            heavy.append(i)
        else:
            code = pick_code(rnd)
            tests, docs = side_lines(rnd, code)
            specs.append(dict(code=code, tests=tests, docs=docs, hours=size_driven(rnd, code)))
    return specs, dict(
        model="hours = 0.5 + (2 + code_lines / 50) * lognormal(sigma 0.5); tests never add time",
        params=dict(sigma=0.5, test_heavy_indexes=heavy, test_heavy_code=[1100, 1450], test_to_code=5))


def thin(rnd, n):
    """30 tasks: 27 S and 3 L by product code, the L ones early so they outlive the recency cut."""
    n = 30
    large = {3, 12, 20}
    specs = []
    for i in range(n):
        code = rnd.randint(1600, 4900) if i in large else rnd.randint(60, 290)
        tests, docs = side_lines(rnd, code)
        specs.append(dict(code=code, tests=tests, docs=docs, hours=size_driven(rnd, code)))
    return specs, dict(
        model="hours = 0.5 + (2 + code_lines / 50) * lognormal(sigma 0.5); 27 S and 3 L tasks",
        params=dict(sigma=0.5, large_indexes=sorted(large)))


SIZE_LINES = {"XS": (10, 49), "S": (50, 299), "M": (300, 1499), "L": (1500, 4999), "XL": (5000, 9000)}
SIZE_NAMES = list(SIZE_LINES)
# Median hours per size. The 80% range is proportional to the median here, so these set the widths.
BREAK_MEDIANS = dict(zip(SIZE_NAMES, (4.0, 4.6, 5.3, 6.1, 18.3)))   # widths x1.15 a step, then x3
# 15 XL tasks sample the width noisily, so that world makes the jump x4 to keep the break visible.
THIN_MEDIANS = {**BREAK_MEDIANS, "XL": 24.4}
SMOOTH_MEDIANS = dict(zip(SIZE_NAMES, (4.0, 4.8, 5.8, 6.9, 8.3)))   # widths x1.2 a step
SLICES, SETS = 3, 12
ADVISOR_SIGMA = 0.3  # a narrow spread keeps sampling noise in the widths well under the x2 threshold


def sized_singles(rnd, medians, per_bucket):
    """`per_bucket[size]` single tasks of each size, shuffled so sizes interleave over time."""
    specs = []
    for size in SIZE_NAMES:
        for _ in range(per_bucket[size]):
            code = rnd.randint(*SIZE_LINES[size])
            tests, docs = side_lines(rnd, code)
            specs.append(dict(code=code, tests=tests, docs=docs, hours=MIN_HOURS + lognormal(rnd, medians[size], ADVISOR_SIGMA)))
    rnd.shuffle(specs)
    return specs


def split_set(rnd, medians, sequential):
    """One XL change cut into SLICES L tasks that close a shared parent issue. Parallel slices
    start together; sequential ones wait for the one before and then a hand-off gap."""
    total = rnd.randint(*SIZE_LINES["XL"])
    set_specs = []
    for k in range(SLICES):
        code = total // SLICES
        tests, docs = side_lines(rnd, code)
        hours = MIN_HOURS + lognormal(rnd, medians["L"], ADVISOR_SIGMA)
        spec = dict(code=code, tests=tests, docs=docs, hours=hours, set=True)
        if k:
            prev = set_specs[-1]
            spec["after"] = (1, prev["hours"] + rnd.uniform(6, 10)) if sequential else (k, 0.2)
        set_specs.append(spec)
    return set_specs


def advisor_world(rnd, medians, xl=60, sequential=False, with_splits=True):
    """60 single tasks per size (`xl` of XL), plus SETS split changes interleaved among them."""
    specs = sized_singles(rnd, medians, {s: (xl if s == "XL" else 60) for s in SIZE_NAMES})
    if with_splits:
        step = len(specs) // (SETS + 1)
        for k in range(SETS, 0, -1):
            specs[k * step:k * step] = split_set(rnd, medians, sequential)
    return specs


def number_issues(specs):
    """Each single task closes its own issue; a split's slices close their own and the parent, numbered after them."""
    n, i = 1000, 0
    while i < len(specs):
        if specs[i].get("set"):
            kids = [specs[i + j] for j in range(SLICES)]
            parent = n + SLICES
            for j, k in enumerate(kids):
                k["issues"] = [n + j, parent]
            n, i = parent + 1, i + SLICES
        else:
            specs[i]["issues"] = [n]
            n, i = n + 1, i + 1


def break_point(rnd, n):
    """60 tasks per size; the 80% range grows 3x from L to XL; splits finish sooner."""
    specs = advisor_world(rnd, BREAK_MEDIANS)
    number_issues(specs)
    return specs, dict(
        model="hours = 0.5 + median[size] * lognormal(sigma 0.3); range widths grow x1.15 a size, then x3 at XL; "
              "12 XL changes were cut into 3 parallel L tasks sharing a parent issue",
        params=dict(medians=BREAK_MEDIANS, sigma=ADVISOR_SIGMA, break_size="XL", splits_faster=True))


def no_break(rnd, n):
    """60 tasks per size; widths grow x1.2 a size; splits finish sooner, so only the missing break stops advice."""
    specs = advisor_world(rnd, SMOOTH_MEDIANS)
    number_issues(specs)
    return specs, dict(
        model="hours = 0.5 + median[size] * lognormal(sigma 0.3); range widths grow x1.2 a size, no jump",
        params=dict(medians=SMOOTH_MEDIANS, sigma=ADVISOR_SIGMA, break_size=None, splits_faster=True))


def slow_splits(rnd, n):
    """A break at XL, but each split's slices ran one after another with hand-off gaps."""
    specs = advisor_world(rnd, BREAK_MEDIANS, sequential=True)
    number_issues(specs)
    return specs, dict(
        model="as break-point, but the 3 slices of each split ran in sequence with 6-10 hour gaps",
        params=dict(medians=BREAK_MEDIANS, sigma=ADVISOR_SIGMA, break_size="XL", splits_faster=False))


def thin_advisor(rnd, n):
    """A break at XL, but only 15 XL tasks."""
    specs = advisor_world(rnd, THIN_MEDIANS, xl=15)
    number_issues(specs)
    return specs, dict(
        model="as break-point with 15 single XL tasks instead of 60, and the XL width x4 instead of x3",
        params=dict(medians=THIN_MEDIANS, sigma=ADVISOR_SIGMA, break_size="XL", splits_faster=True, xl_tasks=15))


def merge_keep(rnd, n):
    """The break-point history, with a plan (MERGE_KEEP_PLAN) to merge or keep."""
    specs, model = break_point(rnd, n)
    model["model"] = "history as in break-point; the plan has two XS tasks in one area, two XS in different areas, one M"
    return specs, model


# Plans (`simulate` writes plan.jsonl when a scenario has one): id, size, paths.
SPLIT_PLAN = [
    dict(id="big", size="XL", paths=["src/lib/money.py", "src/lib/dates.py", "src/api/orders.py", "src/api/refunds.py",
                                     "ui/orders/list.tsx", "ui/orders/detail.tsx", "tests/test_orders.py", "docs/orders.md"]),
    dict(id="mid", size="L", paths=["src/api/orders.py", "ui/orders/list.tsx"]),
]
MERGE_KEEP_PLAN = [
    dict(id="a", size="XS", paths=["src/api/orders.py"]),
    dict(id="b", size="XS", paths=["src/api/refunds.py"]),
    dict(id="c", size="XS", paths=["ui/header.tsx"]),
    dict(id="d", size="XS", paths=["scripts/release.sh"]),
    dict(id="e", size="M", paths=["src/lib/money.py", "src/lib/dates.py"]),
]
PLANS = {"break-point": SPLIT_PLAN, "no-break": SPLIT_PLAN, "slow-splits": SPLIT_PLAN,
         "thin-advisor": SPLIT_PLAN, "merge-keep": MERGE_KEEP_PLAN}

# name -> (function, default task count)
SCENARIOS = {"size-matters": (size_matters, 800), "null": (null, 800), "test-heavy": (test_heavy, 400),
             "thin": (thin, 30), "break-point": (break_point, 0), "no-break": (no_break, 0),
             "slow-splits": (slow_splits, 0), "thin-advisor": (thin_advisor, 0), "merge-keep": (merge_keep, 0)}


def generate(name, seed, n=None):
    """Task rows and the truth dict for one scenario."""
    if name not in SCENARIOS:
        sys.exit(f"cc-estimate simulate: unknown scenario {name!r}. Choose from: {', '.join(SCENARIOS)}.")
    fn, default_n = SCENARIOS[name]
    rnd = random.Random(f"{name}:{seed}")
    specs, model = fn(rnd, n or default_n)
    rows, w, starts = [], 0.0, []
    for i, s in enumerate(specs, 1):
        if "after" in s:  # starts relative to an earlier spec: (how many specs back, hours after its start)
            back, delay = s["after"]
            sw = starts[len(starts) - back] + delay
        else:
            w += rnd.uniform(2, 14) if i > 1 else rnd.uniform(0, 4)
            sw = w
        starts.append(sw)
        start = clock(round(sw * 60 + rnd.uniform(0.1, 0.9)) / 60)  # starts at a random minute, not on round hours
        end = clock(round((sw + s["hours"]) * 3600) / 3600)
        rows.append(dict(id=f"SIM-{i}", title=rnd.choice(["feat: add", "fix: repair", "chore: tidy"]),
                         author=LOGINS[i % 3], labels=[], start=start.isoformat(), end=end.isoformat(),
                         lines=s["code"] + s["tests"] + s["docs"], code_lines=s["code"],
                         test_lines=s["tests"], doc_lines=s["docs"], excluded={},
                         **({"issues": s["issues"]} if "issues" in s else {})))
    truth = dict(scenario=name, seed=seed, tasks=len(rows), timezone="UTC",
                 working_hours="Monday-Friday 09:00-18:00", **model)
    if "test_heavy_indexes" in truth["params"]:
        truth["params"]["test_heavy_ids"] = [rows[i]["id"] for i in truth["params"].pop("test_heavy_indexes")]
    return rows, truth


def write_rows(rows, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cc-estimate simulate", description=__doc__.split("\n")[0])
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True, help="directory for tasks.jsonl and truth.json")
    ap.add_argument("--tasks", type=int, help="number of tasks (default depends on the scenario)")
    a = ap.parse_args(argv)
    rows, truth = generate(a.scenario, a.seed, a.tasks)
    out = Path(a.out)
    write_rows(rows, out / "tasks.jsonl")
    (out / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    if a.scenario in PLANS:
        write_rows(PLANS[a.scenario], out / "plan.jsonl")
    print(f"Wrote {len(rows)} tasks and truth.json to {out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
