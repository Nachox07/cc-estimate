#!/usr/bin/env python3
"""Estimate a task from this repo's history, log the estimate, and score logged estimates.

  cc-estimate estimate --size M [--issue N | --text "..."] [--paths a/ b.py] [--log] [--off 2026-10-14..2026-10-16]
  cc-estimate attach <estimate-id> --pr N        # link a logged estimate to the PR that delivered it
  cc-estimate score                              # fill actuals from GitHub, print the running score

The range is the p10-p90 of work hours (minus absence and blocked time) for finished tasks of the same size bucket
(the only signal that held up in backtest.py). Each logged estimate also stores the
size-blind repo range, so `score` can say whether the planned size helped.
Only the current gh user's estimates are logged or scored.
"""
import argparse, json, os, re, subprocess, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import backtest as bt
from . import classify as cl
from . import gitsource as gs
from .links import LABEL_EVENTS, blocked_spans
from .files import open_out

LEVEL = 0.8
LO_P, HI_P = (1 - LEVEL) / 2, 1 - (1 - LEVEL) / 2
HOURS_PER_DAY = bt.DAY1 - bt.DAY0
CENSOR_DAYS = 21


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
    rows = bt.load(tasks_path, tz, cfg)
    cut = max(r["end"] for r in rows) - timedelta(days=CENSOR_DAYS)
    rows = [r for r in rows if r["start"] <= cut]  # recent starts: only the fast ones have finished
    same = [r["y"] for r in rows if r["size"] == size]
    allr = [r["y"] for r in rows]
    newest = max(r["end"] for r in rows)
    rng = lambda ys: [round(bt.q(ys, LO_P), 1), round(bt.q(ys, 0.5), 1), round(bt.q(ys, HI_P), 1)]
    return rng(same), len(same), rng(allr), newest


def days(h):
    return f"{h / HOURS_PER_DAY:.1f}"


def print_needs(paths, cfg):
    """What the change has to go through, from the repo's own rules. Informational: in the
    backtest these did not narrow the range beyond size."""
    c = cfg.get("classifier")
    if not c:
        return
    # A directory stands for "some file under it", which is what the path rules match.
    files = [p + "_" if p.endswith("/") else p for p in paths]
    groups = cl.approvals(files, cl.load_manifest(Path(c.get("repo_dir", ".")) / c["approval_manifest"])) if c.get("approval_manifest") else []
    tiers = cl.tiers(files, c["tiers_cmd"], c.get("repo_dir")) if c.get("tiers_cmd") else []
    slow = sorted(set(tiers or []) & set(c.get("heavy_tiers", [])))
    print("This change will need:")
    print(f"  approvals: {', '.join(groups) + ' (security paths)' if groups else 'normal code review only'}")
    print(f"  test tiers: {', '.join(tiers) if tiers else 'none mapped'}" + (f"; slow ones: {', '.join(slow)}" if slow else ""))


def cmd_estimate(a, cfg):
    issue = task_key(a.issue, cfg.get("source")) if a.issue else None
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    (lo, med, hi), n, base, newest = ranges(a.tasks, tz, a.size, cfg)
    print(f"Size {a.size}: {n} finished tasks in history (data up to {newest:%Y-%m-%d}).")
    print(f"80% range: {lo}-{hi} work hours ({days(lo)}-{days(hi)} working days), median {med} h ({days(med)} d).")
    print(f"Without a size: {base[0]}-{base[2]} h.")
    if n < 20:
        print("Fewer than 20 tasks of this size: treat the range as rough.")
    if a.paths:
        print_needs(a.paths, cfg)
    if not a.log:
        return
    rec = dict(id=uuid.uuid4().hex[:8], at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               author=me(cfg), repo=cfg.get("repo", "local/repo"), size=a.size, issue=issue, text=a.text, pr=None,
               range_h=[lo, med, hi], baseline_h=base, n=n, off=a.off or [], actual=None)
    with open_out(a.log_file, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"Logged as {rec['id']}." + ("" if a.issue else f" No issue given: run `cc-estimate attach {rec['id']} --pr <N>` once the PR exists."))


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


PR_FIELDS = "number mergedAt createdAt baseRefName commits(first:1){nodes{commit{authoredDate}}} " + LABEL_EVENTS
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
    rows, _ = gs.build(gs.commits(g.get("repo_dir", "."), g.get("branch", "HEAD"), "365 days ago"),
                       gs.key_patterns(g.get("key_regex")))
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
                start=start.isoformat(), end=end.isoformat(), commits=row["commits"])


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
    return dict(hours=round(bt.biz_hours(start, end, tz, off, spans), 1),
                hours_raw=round(bt.biz_hours(start, end, tz), 1),
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
    e.add_argument("--size", required=True, choices=[name for _, name in bt.BUCKETS])
    e.add_argument("--issue", help="issue number or tracker key (ABC-123)"); e.add_argument("--text")
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
