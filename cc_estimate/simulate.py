#!/usr/bin/env python3
"""Write a made-up task history with known truth, to test the tool against.

  cc-estimate simulate --scenario size-matters --seed 7 --out dir

Writes dir/tasks.jsonl (same contract as real task files) and dir/truth.json (the model the
durations were drawn from). Same scenario and seed give the same files.

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


# name -> (function, default task count)
SCENARIOS = {"size-matters": (size_matters, 800), "null": (null, 800), "test-heavy": (test_heavy, 400),
             "thin": (thin, 30)}


def generate(name, seed, n=None):
    """Task rows and the truth dict for one scenario."""
    if name not in SCENARIOS:
        sys.exit(f"cc-estimate simulate: unknown scenario {name!r}. Choose from: {', '.join(SCENARIOS)}.")
    fn, default_n = SCENARIOS[name]
    rnd = random.Random(f"{name}:{seed}")
    specs, model = fn(rnd, n or default_n)
    rows, w = [], 0.0
    for i, s in enumerate(specs, 1):
        w += rnd.uniform(2, 14) if i > 1 else rnd.uniform(0, 4)
        start = clock(round(w * 60 + rnd.uniform(0.1, 0.9)) / 60)  # whole-minute starts, never on the hour by design
        end = clock(round((w + s["hours"]) * 3600) / 3600)
        rows.append(dict(id=f"SIM-{i}", title=rnd.choice(["feat: add", "fix: repair", "chore: tidy"]),
                         author=LOGINS[i % 3], labels=[], start=start.isoformat(), end=end.isoformat(),
                         lines=s["code"] + s["tests"] + s["docs"], code_lines=s["code"],
                         test_lines=s["tests"], doc_lines=s["docs"], excluded={}))
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
    print(f"Wrote {len(rows)} tasks and truth.json to {out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
