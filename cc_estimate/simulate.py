#!/usr/bin/env python3
"""Write a made-up task history with known truth, to test the tool against.

  cc-estimate simulate --scenario size-matters --seed 7 --out dir

Writes dir/tasks.jsonl (same contract as real task files) and dir/truth.json (the model the
durations were drawn from), plus dir/queue.jsonl when the scenario has review timing. Same scenario and seed give the same files.

Durations are drawn in working hours, then placed on the clock by walking Monday-Friday
09:00-18:00 UTC, at non-round minutes. A task therefore measures exactly the hours drawn, and
durations do not pile up at 0 or 9 hours the way clock-time offsets from round start times do.

A scenario is a small function `(rnd, n) -> (specs, model)`. A spec is
{"code", "tests", "docs", "hours"}; the model is the plain-language truth. Add one to SCENARIOS.
A scenario that places its own tasks in time adds `start` (working hours from BASE) to each spec,
and for review timing `ready`, `first_review`, `reviewers` [(who, at)], `state` and `task`; those
also write dir/queue.jsonl, the all-PRs file that measures the reviewer queue.
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


TEAMS = {"team-a": ["rev-a1", "rev-a2", "rev-a3"], "team-b": ["rev-b1", "rev-b2", "rev-b3"]}
CODING_BASE, CODING_LINES, CODING_SIGMA = 0.3, 2000, 0.4  # median coding hours = base + code_lines / lines
WAIT_BASE, HOURS_PER_QUEUED_PR, WAIT_SIGMA = 1.0, 2.4, 0.3  # median wait hours = base + per_pr * queued
GAP = (0.5, 3.2)  # working hours between one PR opening and the next
LATE_REQUEST = 0.25  # share of PRs whose reviewers are named only after the PR is ready
ABANDONED = 0.06  # share of PRs closed without merging


def wait_dominates(rnd, n):
    """Coding grows with size; waiting grows with the reviewer queue and ignores size. PRs are
    opened every 0.5-3.2 working hours, so many are in flight. Each asks team-a (50%), team-b (40%)
    or both (10%), plus one person from each team. Waiting is drawn in order of ready time, from
    the PRs already ready and unmerged on the same team(s), so the queue is built, not assumed.
    A quarter of PRs name their reviewers only after they are ready, and 6% are closed unmerged. Of the
    rest, two in five have their reviewers requested a few seconds after ready, as CODEOWNERS does
    (picked by index, not drawn, so the other draws do not move). The first review ends that
    reviewer's request and every team's (any human's review ends a team request, as the loader reads
    it); a second person's request stays until the merge.
    The data ends when the last PR is opened; PRs not done by then are open.
    Measured over seeds 0-30 with the real loader: 31 of 31 meet the bars; the closest is seed 10, WAIT
    queue at 0.84 of the baseline's Winkler against the 0.85 bar (seed 19: 0.83)."""
    specs, w = [], 0.0
    for i in range(n):
        w += rnd.uniform(*GAP)
        code = pick_code(rnd)
        tests, docs = side_lines(rnd, code)
        coding = MIN_HOURS + lognormal(rnd, CODING_BASE + code / CODING_LINES, CODING_SIGMA)
        teams = rnd.choices([("team-a",), ("team-b",), ("team-a", "team-b")], [5, 4, 1])[0]
        specs.append(dict(code=code, tests=tests, docs=docs, start=w, ready=w + coding, teams=teams, idx=i,
                          late=rnd.random() < LATE_REQUEST, task=rnd.random() >= ABANDONED))
    cutoff, open_ = w, []
    for s in sorted(specs, key=lambda s: s["ready"]):
        depth = sum(1 for p in specs if "end" in p and p["end"] > s["ready"] and set(p["teams"]) & set(s["teams"]))
        wait = MIN_HOURS + lognormal(rnd, WAIT_BASE + HOURS_PER_QUEUED_PR * depth, WAIT_SIGMA)
        s["end"], s["hours"] = s["ready"] + wait, s["ready"] + wait - s["start"]
        at = s["ready"] + wait * rnd.uniform(0.3, 0.8) if s["late"] else s["ready"] - rnd.uniform(0, 0.3)
        if not s["late"] and s["idx"] % 5 < 2:
            at = s["ready"] + (1 + s["idx"] % 3) / 3600
        users = [f"user:{rnd.choice(TEAMS[t])}" for t in s["teams"]]
        s["first_review"] = s["ready"] + wait * rnd.uniform(0.85, 0.98)
        s["reviewers"] = [(x, at, s["first_review"] if x.startswith("team:") or x == users[0] else None)
                          for x in [f"team:acme/{t}" for t in s["teams"]] + users]
        s["state"] = "OPEN" if s["end"] > cutoff else "MERGED" if s["task"] else "CLOSED"
    return specs, dict(
        model=f"coding = 0.5 + ({CODING_BASE} + code_lines / {CODING_LINES}) * lognormal(sigma {CODING_SIGMA}); "
              f"wait = 0.5 + ({WAIT_BASE} + {HOURS_PER_QUEUED_PR} * queued) * lognormal(sigma {WAIT_SIGMA}); "
              "queued = PRs ready and unmerged, asking a team this one asks, when it becomes ready",
        params=dict(sigma_coding=CODING_SIGMA, sigma_wait=WAIT_SIGMA, hours_per_queued_pr=HOURS_PER_QUEUED_PR,
                    gap_hours=list(GAP), late_request_share=LATE_REQUEST, abandoned_share=ABANDONED,
                    teams=sorted(TEAMS)))


# name -> (function, default task count)
SCENARIOS = {"size-matters": (size_matters, 800), "null": (null, 800), "test-heavy": (test_heavy, 400),
             "thin": (thin, 30), "wait-dominates": (wait_dominates, 900)}


def instant(w):
    return clock(round(w * 3600) / 3600).isoformat()


def queue_row(i, s, cutoff):
    """The all-PRs record of one spec, as the data would look at `cutoff`: nothing after it is known."""
    seen = lambda w: w is not None and w <= cutoff
    return dict(number=i, state=s["state"], created_at=instant(s["start"]),
                ready_at=instant(s["ready"]) if seen(s["ready"]) else None,
                first_review_at=instant(s["first_review"]) if seen(s["first_review"]) else None,
                closed_at=instant(s["end"]) if s["state"] != "OPEN" else None,
                reviewers=[dict(who=x, at=instant(at), done_at=instant(done) if seen(done) else None)
                           for x, at, done in sorted(s["reviewers"], key=lambda r: r[1]) if seen(at)])


def generate_all(name, seed, n=None):
    """Task rows, the truth dict and the all-PRs rows (empty unless the scenario has review timing)."""
    if name not in SCENARIOS:
        sys.exit(f"cc-estimate simulate: unknown scenario {name!r}. Choose from: {', '.join(SCENARIOS)}.")
    fn, default_n = SCENARIOS[name]
    rnd = random.Random(f"{name}:{seed}")
    specs, model = fn(rnd, n or default_n)
    rows, queue, w = [], [], 0.0
    timed = any("ready" in s for s in specs)
    cutoff = max(s["start"] for s in specs) if timed else 0.0
    for i, s in enumerate(specs, 1):
        if "start" in s:
            start = clock(round(s["start"] * 3600) / 3600)
        else:
            w += rnd.uniform(2, 14) if i > 1 else rnd.uniform(0, 4)
            start = clock(round(w * 60 + rnd.uniform(0.1, 0.9)) / 60)  # starts at a random minute, not on round hours
        end = clock(round(((s["start"] if "start" in s else w) + s["hours"]) * 3600) / 3600)
        if timed:
            queue.append(queue_row(i, s, cutoff))
            if s["state"] != "MERGED":
                continue  # open or closed unmerged: in the queue file, not a finished task
        row = dict(id=f"SIM-{i}", title=rnd.choice(["feat: add", "fix: repair", "chore: tidy"]),
                   author=LOGINS[i % 3], labels=[], start=start.isoformat(), end=end.isoformat(),
                   lines=s["code"] + s["tests"] + s["docs"], code_lines=s["code"],
                   test_lines=s["tests"], doc_lines=s["docs"], excluded={})
        if timed:
            row.update(prs=[i], ready_at=queue[-1]["ready_at"], first_review_at=queue[-1]["first_review_at"],
                       reviewers=queue[-1]["reviewers"])
        rows.append(row)
    truth = dict(scenario=name, seed=seed, tasks=len(rows), timezone="UTC",
                 working_hours="Monday-Friday 09:00-18:00", **model)
    if "test_heavy_indexes" in truth["params"]:
        truth["params"]["test_heavy_ids"] = [rows[i]["id"] for i in truth["params"].pop("test_heavy_indexes")]
    return rows, truth, queue


def generate(name, seed, n=None):
    """Task rows and the truth dict for one scenario."""
    return generate_all(name, seed, n)[:2]


def write_rows(rows, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cc-estimate simulate", description=__doc__.split("\n")[0])
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True, help="directory for tasks.jsonl, truth.json (and queue.jsonl)")
    ap.add_argument("--tasks", type=int, help="number of tasks (default depends on the scenario)")
    a = ap.parse_args(argv)
    rows, truth, queue = generate_all(a.scenario, a.seed, a.tasks)
    out = Path(a.out)
    write_rows(rows, out / "tasks.jsonl")
    if queue:
        write_rows(queue, out / "queue.jsonl")
    (out / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    print(f"Wrote {len(rows)} tasks and truth.json to {out}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
