#!/usr/bin/env python3
"""Estimate a task from this repo's history, log the estimate, and score logged estimates.

  cc-estimate estimate --size M [--issue N | --text "..."] [--paths a/ b.py] [--queue N] [--log] [--off 2026-10-14..2026-10-16]
  cc-estimate attach <estimate-id> --pr N        # link a logged estimate to the PR that delivered it
  cc-estimate score                              # fill actuals from GitHub, print the running score

The range is the p10-p90 of work hours (minus absence and blocked time) for finished tasks of the same size bucket
(product-code lines; tests and docs are not counted)
(the only signal that held up in backtest.py). Each logged estimate also stores the
size-blind repo range, so `score` can say whether the planned size helped.
Only the current gh user's estimates are logged or scored.

When the history carries ready_at, the total is split in two: coding (by size) and waiting for review.
Waiting does not follow size; it follows how many PRs are already waiting on the same reviewers.
`--queue N` is that count today. The waiting range is Kaplan-Meier over finished tasks plus the PRs
still open (from the queue file), so slow waits that have not ended yet are not dropped.
"""
import argparse, json, os, re, subprocess, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import backtest as bt
from . import classify as cl
from . import gitsource as gs
from . import history
from .links import LABEL_EVENTS, blocked_spans
from .files import open_out

LEVEL = 0.8
LO_P, HI_P = (1 - LEVEL) / 2, 1 - (1 - LEVEL) / 2
HOURS_PER_DAY = bt.DAY1 - bt.DAY0
CENSOR_DAYS = 21
MIN_N = 5


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"gh {' '.join(args[:3])}: {r.stderr.strip()[:300]}")
    return r.stdout


def me(cfg=None):
    """Who is estimating: the git email for the git-only source, else the GitHub login."""
    if (cfg or {}).get("source") == "git":
        return subprocess.run(["git", "config", "user.email"], capture_output=True, text=True).stdout.strip()
    return gh("api", "user", "--jq", ".login").strip()


def task_key(issue, source):
    """The key a logged estimate is scored by. GitHub scores issue numbers; git scores any key,
    reading a bare number as `#N`."""
    s = str(issue).strip()
    if source == "git":
        return f"#{s}" if s.isdigit() else s
    n = s.lstrip("#")
    if not n.isdigit():
        sys.exit(f"--issue {s}: the GitHub source scores issue numbers. "
                 "Set \"source\": \"git\" in the config to use tracker keys.")
    return int(n)


def ranges(tasks_path, tz, size, cfg):
    p = Path(tasks_path)
    if not p.exists():
        sys.exit(f"no history at {p}: build it first (README, Quick start).")
    rows = bt.load(tasks_path, tz, cfg)
    if not rows:
        sys.exit(f"{p} has no tasks.")
    newest = max(r["end"] for r in rows)
    cut = newest - timedelta(days=CENSOR_DAYS)
    rows = [r for r in rows if r["start"] <= cut]  # recent starts: only the fast ones have finished
    if len(rows) < MIN_N:
        sys.exit(f"Only {len(rows)} finished tasks started before {cut:%Y-%m-%d}: not enough history yet.")
    same = [r["y"] for r in rows if r["size"] == size]
    rng = lambda ys: [round(bt.q(ys, LO_P), 1), round(bt.q(ys, 0.5), 1), round(bt.q(ys, HI_P), 1)]
    return (rng(same) if len(same) >= MIN_N else None), len(same), rng([r["y"] for r in rows]), newest


def days(h):
    return f"{h / HOURS_PER_DAY:.1f}"


def span(vals):
    """'lo-hi h (a-b d), median m h (c d)' from three (hours, reached) quantiles; a quantile the
    data never reaches is a floor and carries a '+'."""
    (lo, _), (med, med_ok), (hi, hi_ok) = vals
    plus = lambda ok: "" if ok else "+"
    return (f"{lo:.1f}-{hi:.1f}{plus(hi_ok)} work hours ({days(lo)}-{days(hi)}{plus(hi_ok)} working days), "
            f"median {med:.1f}{plus(med_ok)} h ({days(med)}{plus(med_ok)} d)")


def km3(obs):
    return [bt.km_quantile(obs, p) for p in (LO_P, 0.5, HI_P)]


def data_end(rows, pool, queue_file):
    """When the data was taken: the queue file's fetch time, else its newest event."""
    meta = Path(queue_file).with_suffix(".meta.json")
    if meta.is_file():
        return bt.parse(json.loads(meta.read_text())["fetched_at"])
    return max([r["end"] for r in rows] + [t for p in pool for t in (p["ready"], p["closed"]) if t])


def split_ranges(tasks_path, tz, size, cfg, queue_n=None, queue_file=None):
    """Coding and waiting ranges: {"lines": [...], "coding": [lo, med, hi] or None, "wait": ... or None}.
    Nothing if the history has no ready_at.

    Waiting is the PR-level Kaplan-Meier of `bt.wait_obs` at the data's end, the same function the
    backtest's `km wait` rows score at each task's start. Queue terciles are fitted on all history
    here (the backtest fits them on its tuning slice so the locked slice stays unseen; an estimate has
    no unseen slice to protect). With no queue file there is no PR-level data: waiting is then the
    finished tasks' waits, without the most recent ones, and no queue conditioning is possible."""
    queue_file = queue_file or bt.queue_path(tasks_path)
    pool = bt.load_queue(queue_file) if queue_file and Path(queue_file).is_file() else None
    holidays = bt.holiday_dates(cfg)
    rows = [r for r in bt.load(tasks_path, tz, cfg, None, pool) if r["wait_y"] is not None]
    got = dict(lines=[], coding=None, wait=None)
    if not rows:
        return got
    newest = max(r["end"] for r in rows)
    recent = [r for r in rows if r["start"] <= newest - timedelta(days=CENSOR_DAYS)]
    same = [r["coding_y"] for r in recent if r["size"] == size]
    pick, label = (same, f"size {size}") if len(same) >= MIN_N else ([r["coding_y"] for r in recent], "any size")
    if len(pick) >= MIN_N:
        got["coding"] = [bt.q(pick, p) for p in (LO_P, 0.5, HI_P)]
        got["lines"].append(f"Coding, {label}: {span([(v, True) for v in got['coding']])}; {len(pick)} tasks.")
    cuts = bt.tercile_cuts([r["queue"] for r in rows if r["queue"] is not None])
    if pool is not None:
        now = data_end(rows, pool, queue_file)
        pop = bt.wait_population(pool, tz, holidays, min(r["start"] for r in rows))
        count = lambda bucket=None: bt.wait_obs(pop, now, tz, holidays, bucket, cuts)
    else:
        count = lambda bucket=None: [(r["wait_y"], True) for r in recent]
    note = ""
    if queue_n is not None and cuts and pool is not None:
        bucket = bt.tercile(queue_n, cuts)
        obs = count(bucket)
        if sum(f for _, f in obs) >= MIN_N:
            head = (f"Waiting for review, with {queue_n} PRs already waiting on your reviewers "
                    f"({bucket} queue on this repo)")
        else:
            note = f"Not enough history for a {bucket} reviewer queue; waiting for review at any queue:"
            obs = None
    elif queue_n is not None:
        note = "No queue file, so the waiting range cannot follow the queue. Waiting for review at any queue:"
        obs = None
    else:
        obs = None
    if obs is None:
        obs = count()
        head = "Waiting for review, any queue"
        if note:
            got["lines"].append(note)
    vals = km3(obs)
    got["wait"] = [v for v, _ in vals]
    open_ = sum(not f for _, f in obs)
    got["lines"].append(f"{head}: {span(vals)}; {len(obs)} waits, {open_} still open.")
    if queue_n is None:
        got["lines"].append("  Pass --queue N (open PRs waiting on your planned reviewers) to fit it to today's queue.")
    return got


def split_lines(tasks_path, tz, size, cfg, queue_n=None, queue_file=None):
    return split_ranges(tasks_path, tz, size, cfg, queue_n, queue_file)["lines"]


def print_needs(paths, cfg):
    """What the change has to go through, from the repo's own rules. Informational: in the
    backtest these did not narrow the range beyond size."""
    c = cfg.get("classifier")
    if not c:
        return
    # A directory stands for "some file under it", which is what the path rules match.
    files = [p + "_" if p.endswith("/") else p for p in paths]
    print("This change will need:")
    manifest = Path(c.get("repo_dir", ".")) / c["approval_manifest"] if c.get("approval_manifest") else None
    if manifest and not manifest.is_file():
        print(f"  approvals: not found ({manifest}); set classifier in the config")
    else:
        groups = cl.approvals(files, cl.load_manifest(manifest)) if manifest else []
        print(f"  approvals: {', '.join(groups) + ' (protected paths)' if groups else 'normal code review only'}")
    try:
        tiers = cl.tiers(files, c["tiers_cmd"], c.get("repo_dir")) if c.get("tiers_cmd") else []
    except OSError as e:
        print(f"  test tiers: not found ({e.strerror}); set classifier in the config")
        return
    slow = sorted(set(tiers or []) & set(c.get("heavy_tiers", [])))
    print(f"  test tiers: {', '.join(tiers) if tiers else 'none mapped'}" + (f"; slow ones: {', '.join(slow)}" if slow else ""))


def cmd_estimate(a, cfg):
    issue = task_key(a.issue, cfg.get("source")) if a.issue else None
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    sized, n, base, newest = ranges(a.tasks, tz, a.size, cfg)
    print(f"Size {a.size}: {n} finished tasks in history (data up to {newest:%Y-%m-%d}).")
    if sized:
        lo, med, hi = sized
        print(f"80% range: {lo}-{hi} work hours ({days(lo)}-{days(hi)} working days), median {med} h ({days(med)} d).")
        print(f"Without a size: {base[0]}-{base[2]} h.")
        if n < 20:
            print("Fewer than 20 tasks of this size: treat the range as rough.")
    else:
        lo, med, hi = base
        print(f"Not enough history for size {a.size}. Without a size: {lo}-{hi} work hours "
              f"({days(lo)}-{days(hi)} working days), median {med} h ({days(med)} d).")
    parts = split_ranges(a.tasks, tz, a.size, cfg, a.queue, a.queue_file)
    for line in parts["lines"]:
        print(line)
    if a.paths:
        print_needs(a.paths, cfg)
    if not a.log:
        return
    rec = dict(id=uuid.uuid4().hex[:8], at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               author=me(cfg), repo=cfg.get("repo", "local/repo"), size=a.size, issue=issue, text=a.text, pr=None,
               range_h=[lo, med, hi], baseline_h=base, n=n, sized=bool(sized), off=a.off or [], actual=None,
               coding_h=rounded(parts["coding"]), wait_h=rounded(parts["wait"]), queue=a.queue)
    with open_out(a.log_file, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"Logged as {rec['id']}." + ("" if a.issue else f" No issue given: run `cc-estimate attach {rec['id']} --pr <N>` once the PR exists."))


def rounded(vals):
    return [round(v, 1) for v in vals] if vals else None


def load_log(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def save_log(path, recs):
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in recs))


def cmd_attach(a, cfg):
    recs = load_log(a.log_file)
    hit = [r for r in recs if r["id"] == a.id]
    if not hit:
        sys.exit(f"no estimate {a.id}")
    hit[0]["pr"] = a.pr
    save_log(a.log_file, recs)
    print(f"{a.id} -> PR #{a.pr}")


READY_EVENTS = ("timelineItems(first:20,itemTypes:[READY_FOR_REVIEW_EVENT,CONVERT_TO_DRAFT_EVENT]){nodes{__typename "
                "... on ReadyForReviewEvent{createdAt} ... on ConvertToDraftEvent{createdAt}}}")
PR_FIELDS = ("number mergedAt createdAt isDraft baseRefName commits(first:1){nodes{commit{authoredDate}}} "
             + READY_EVENTS + " " + LABEL_EVENTS)
ISSUE_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){state closedAt %s
  closedByPullRequestsReferences(first:10,includeClosedPrs:false){nodes{%s}}}}}""" % (LABEL_EVENTS, PR_FIELDS)
PR_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){%s}}}""" % PR_FIELDS


def gql(q, **v):
    args = ["api", "graphql", "-f", f"query={q}"]
    for k, val in v.items():
        args += ["-F" if isinstance(val, int) else "-f", f"{k}={val}"]
    return json.loads(gh(*args))["data"]["repository"]


def actual_for_git(rec, tz, cfg):
    """Git-only source: the task is the commits carrying its key. It counts as finished once the
    key has had no commit for `quiet_days` (default 5) days."""
    g = cfg.get("git", {})
    repo_dir, branch = g.get("repo_dir", "."), g.get("branch", "HEAD")
    rows, _ = gs.build(gs.commits(repo_dir, branch, "365 days ago"), gs.key_patterns(g.get("key_regex")),
                       merged=gs.merges(repo_dir, branch, "365 days ago"))
    key = task_key(rec.get("issue"), "git")
    row = next((r for r in rows if r["id"] == key), None)
    if not row:
        return None
    end = bt.parse(row["end"])
    if datetime.now(timezone.utc) - end < timedelta(days=g.get("quiet_days", 5)):
        return None
    start = bt.parse(row["start"])
    off = bt.off_dates(cfg, rec["author"]) | bt.expand_dates(rec.get("off", []))
    return dict(hours=round(bt.biz_hours(start, end, tz, off), 1), hours_raw=round(bt.biz_hours(start, end, tz), 1),
                start=start.isoformat(), end=end.isoformat(), commits=row["commits"],
                end_kind=row["end_kind"])


def actual_for(rec, tz, cfg):
    """Work hours from the first commit or PR open (whichever is earlier) to the last merge,
    over the default-branch PRs that delivered the task, minus absence days and blocked spans.
    None while any is unmerged."""
    if cfg.get("source") == "git":
        return actual_for_git(rec, tz, cfg)
    owner, name = rec["repo"].split("/")
    blocked = []
    if rec.get("pr"):
        prs = [gql(PR_Q, o=owner, r=name, n=rec["pr"])["pullRequest"]]
    elif rec.get("issue"):
        issue = gql(ISSUE_Q, o=owner, r=name, n=task_key(rec["issue"], "github"))["issue"]
        prs = issue["closedByPullRequestsReferences"]["nodes"]
        blocked += blocked_spans(issue.get("labelEvents"), issue.get("closedAt"))
    else:
        return None
    prs = [p for p in prs if p["baseRefName"] == cfg.get("default_branch", "main")]
    if not prs or any(not p["mergedAt"] for p in prs):
        return None
    starts = [p["createdAt"] for p in prs] + [c["commit"]["authoredDate"] for p in prs for c in p["commits"]["nodes"]]
    start = min(bt.parse(s) for s in starts)
    end = max(bt.parse(p["mergedAt"]) for p in prs)
    blocked += [sp for p in prs for sp in blocked_spans(p.get("labelEvents"), p["mergedAt"])]
    spans = [[bt.parse(x), bt.parse(y)] for x, y in blocked]
    off = bt.off_dates(cfg, rec["author"]) | bt.expand_dates(rec.get("off", []))
    readies = [history.ready_instant(p, p["timelineItems"]["nodes"]) for p in prs]
    ready = min((r for r in readies if r), default=None)
    ready = min(max(ready, start), end) if ready else None
    return dict(hours=round(bt.biz_hours(start, end, tz, off, spans), 1),
                hours_raw=round(bt.biz_hours(start, end, tz), 1),
                coding_hours=round(bt.biz_hours(start, ready, tz, off, spans), 1) if ready else None,
                wait_hours=round(bt.biz_hours(ready, end, tz, bt.holiday_dates(cfg)), 1) if ready else None,
                start=start.isoformat(), end=end.isoformat(), prs=[p["number"] for p in prs])


def winkler(lo, hi, y):
    alpha = 1 - LEVEL
    return (hi - lo) + 2 / alpha * (max(lo - y, 0) + max(y - hi, 0))


def cmd_score(a, cfg):
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    login = me(cfg)
    recs = load_log(a.log_file)
    for r in recs:
        if r["author"] == login and not r.get("actual"):
            try:
                r["actual"] = actual_for(r, tz, cfg)
            except SystemExit as e:  # one unreadable issue or PR must not stop the rest
                print(f"  {r['id']}: skipped ({e})", file=sys.stderr)
    save_log(a.log_file, recs)
    mine = [r for r in recs if r["author"] == login]
    done = [r for r in mine if r.get("actual")]
    print(f"Your estimates: {len(mine)} logged, {len(done)} finished.")
    if not done:
        return
    for r in done[-10:]:
        lo, _, hi = r["range_h"]
        y = r["actual"]["hours"]
        mark = "inside" if lo <= y <= hi else ("faster" if y < lo else "slower")
        label = str(r["issue"]) if r.get("issue") else f"PR #{r['pr']}"
        raw = r["actual"].get("hours_raw", y)
        excl = f" ({raw - y:.1f} h off/blocked excluded)" if raw - y > 0.05 else ""
        print(f"  {r['id']} {label:9} size {r['size']:2} range {lo}-{hi} h  actual {y} h{excl}  {mark}")
    inside = sum(r["range_h"][0] <= r["actual"]["hours"] <= r["range_h"][2] for r in done)
    w = sum(winkler(r["range_h"][0], r["range_h"][2], r["actual"]["hours"]) for r in done) / len(done)
    wb = sum(winkler(r["baseline_h"][0], r["baseline_h"][2], r["actual"]["hours"]) for r in done) / len(done)
    print(f"Inside the range: {inside}/{len(done)} (target 80%).")
    print(f"Winkler (lower is better): with your size {w:.1f}, without a size {wb:.1f}.")
    for key, field, what in (("coding_h", "coding_hours", "Coding"), ("wait_h", "wait_hours", "Waiting")):
        got = [r for r in done if r.get(key) and r["actual"].get(field) is not None]
        if got:
            hit = sum(r[key][0] <= r["actual"][field] <= r[key][2] for r in got)
            print(f"{what} inside its range: {hit}/{len(got)} (target 80%).")
    if len(done) < 15:
        print("Fewer than 15 finished: too few to judge yet.")


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=os.environ.get("CC_ESTIMATE_CONFIG", "cc-estimate.json"))
    common.add_argument("--tasks", default=None)
    common.add_argument("--log-file")
    ap = argparse.ArgumentParser(prog="cc-estimate")
    sp = ap.add_subparsers(dest="cmd", required=True)
    e = sp.add_parser("estimate", parents=[common])
    e.add_argument("--size", required=True, choices=[name for _, name in bt.BUCKETS],
                   help="product-code lines (tests and docs not counted): XS <50, S <300, M <1,500, L <5,000, XL")
    e.add_argument("--issue", help="issue number or tracker key (ABC-123)"); e.add_argument("--text")
    e.add_argument("--queue", type=int, help="open PRs waiting on the reviewers you plan to ask (or their team)")
    e.add_argument("--queue-file", help="all-PRs file from fetch-queue; default: <repo>.queue.jsonl beside the tasks file")
    e.add_argument("--log", action="store_true")
    e.add_argument("--paths", nargs="*", help="files or directories you expect to change")
    e.add_argument("--off", nargs="*", help="days you will not work on it: 2026-10-14 or 2026-10-14..2026-10-16")
    t = sp.add_parser("attach", parents=[common]); t.add_argument("id"); t.add_argument("--pr", type=int, required=True)
    sp.add_parser("score", parents=[common])
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text())
    data = Path(a.config).resolve().parent / cfg.get("data_dir", "data")
    a.tasks = a.tasks or str(data / f"{cfg.get('repo', 'local/repo').split('/')[-1]}.tasks.jsonl")
    a.log_file = a.log_file or str(data / "estimates.jsonl")
    {"estimate": cmd_estimate, "attach": cmd_attach, "score": cmd_score}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
