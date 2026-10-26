#!/usr/bin/env python3
"""history.py - measure how long merged PRs really took, and what drove it.

Generic: works on any GitHub repo the `gh` CLI can read. Repo- and team-specific
settings live in a JSON config file (see config.example.json). Stdlib only.

  python3 history.py fetch   --repo OWNER/NAME [--days 120] [--config cfg.json] [--out prs.jsonl]
  python3 history.py ci      --repo OWNER/NAME --prs 12,34 [--in prs.jsonl]   # expensive CI detail
  python3 history.py summary --in prs.jsonl [--focus 1234] [--out summary.md]

Design borrowed from prior art (ideas, not code paths): a sha256(query+vars) on-disk
cache, node(id:) continuation paging for connections past 100 items, and date bisection
when a search window exceeds GitHub's ~1000-result cap, with a loud shortfall warning.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from .files import open_out

HERE = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    # Path classification. Regexes (searched, case-insensitive). Docs win over tests.
    "docs_patterns": [r"\.(md|mdx|rst|adoc)$", r"(^|/)docs?/", r"(^|/)(README|CHANGELOG|LICENSE)[^/]*$"],
    "tests_patterns": [r"(^|/)(tests?|spec|specs|__tests__|e2e|__screenshots__)/",
                       r"(^|/)(test_[^/]+|[^/]+_test\.[a-z]+|conftest\.py)$",
                       r"\.(test|spec)\.[a-z]+$"],
    # Files excluded from every line count (lockfiles, generated code). Globs.
    "ignore_globs": ["*package-lock.json", "*yarn.lock", "*pnpm-lock.yaml", "*poetry.lock", "*uv.lock", "*.snap"],
    # Who is a bot. A GraphQL actor of type Bot is always a bot; these add logins.
    "bot_login_patterns": [r"\[bot\]$", r"^github-actions$"],
    "bot_logins": [],
    # Bots that do code review (subset of bots, or human-looking service accounts).
    "ai_reviewer_patterns": [r"copilot", r"codex", r"coderabbit", r"claude", r"cursor", r"sourcery"],
    # Co-authored-by trailer / commit author naming an AI.
    "ai_coauthor_patterns": [r"claude", r"copilot", r"codex", r"cursor", r"noreply@anthropic\.com", r"openai"],
    # Commits closer than this many minutes are treated as one push (push time is not exposed).
    "push_cluster_minutes": 15,
    # Count bot reviews as the "first non-author review" that starts review rounds.
    "rounds_start_on_bot_review": False,
    # Day boundaries: hours offset from UTC, default and per author login.
    "utc_offset_hours": 0,
    "author_utc_offset_hours": {},
    # Non-working dates: {"*": ["2026-12-25", "2026-08-01..2026-08-15"], "login": [...]}
    "absence": {},
    "absence_file": None,
    # Optional external risk scorer: argv list; changed file paths are appended.
    # The first regex group of risk_score_regex in its stdout is recorded.
    "risk_score_cmd": None,
    "risk_score_cwd": None,
    "risk_score_regex": r"Tier:\s*(\w+)",
    "search_page_size": 20,
}

# ----------------------------------------------------------------------------- config


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        p = Path(path)
        user = json.loads(p.read_text())
        cfg.update(user)
        if cfg.get("absence_file"):
            af = Path(cfg["absence_file"])
            if not af.is_absolute():
                af = p.parent / af
            extra = json.loads(af.read_text())
            merged = dict(cfg.get("absence") or {})
            for k, v in extra.items():
                merged[k] = list(merged.get(k, [])) + list(v)
            cfg["absence"] = merged
    cfg["_absence_sets"] = {k: expand_dates(v) for k, v in (cfg.get("absence") or {}).items()}
    return cfg


def expand_dates(items: list[str]) -> set[date]:
    out: set[date] = set()
    for it in items:
        if ".." in it:
            a, b = (date.fromisoformat(x.strip()) for x in it.split(".."))
            d = a
            while d <= b:
                out.add(d)
                d += timedelta(days=1)
        else:
            out.add(date.fromisoformat(it.strip()))
    return out


# ----------------------------------------------------------------------------- gh + cache


class GhError(RuntimeError):
    pass


CACHE_DIR = Path(os.environ.get("CC_ESTIMATE_CACHE", ".cc-estimate-cache"))
STATS = {"calls": 0, "cache_hits": 0, "points": 0}


def _cache_path(name: str, parts: list[str]) -> Path:
    h = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:24]
    return CACHE_DIR / f"{name}_{h}.json"


def _transient(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in ("rate limit", "secondary", "abuse", "timeout", "timed out", "502", "503",
                                "504", "500", "stream error", "connection reset", "eof", "something went wrong",
                                "temporarily"))


def gh_graphql(query: str, variables: dict, name: str, ttl_s: float | None = None) -> dict:
    """GraphQL via `gh api graphql`, cached on disk; retries transient errors and
    sleeps until reset when the rate-limit budget runs low."""
    path = _cache_path(name, [query, json.dumps(variables, sort_keys=True)])
    if path.exists() and (ttl_s is None or time.time() - path.stat().st_mtime < ttl_s):
        STATS["cache_hits"] += 1
        return json.loads(path.read_text())
    body = json.dumps({"query": query, "variables": variables})
    delay = 5.0
    for attempt in range(1, 9):
        r = subprocess.run(["gh", "api", "graphql", "--input", "-"], input=body, capture_output=True, text=True)
        STATS["calls"] += 1
        out = r.stdout or ""
        try:
            data = json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            data = None
        errs = (data or {}).get("errors") if isinstance(data, dict) else None
        msg = (r.stderr or "") + (json.dumps(errs) if errs else "")
        if r.returncode == 0 and data and not errs:
            rl = (data.get("data") or {}).get("rateLimit")
            if rl:
                STATS["points"] += rl.get("cost", 0)
                if rl.get("remaining", 9999) < 100:
                    reset = datetime.fromisoformat(rl["resetAt"].replace("Z", "+00:00"))
                    wait = max(5, (reset - datetime.now(timezone.utc)).total_seconds() + 5)
                    print(f"  rate budget low ({rl['remaining']}); sleeping {wait:.0f}s", file=sys.stderr)
                    time.sleep(wait)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
            return data
        if "resource limits" in msg.lower():
            raise GhError(f"{name}: resource limits exceeded")
        if "rate limit" in msg.lower() and "secondary" not in msg.lower():
            wait = rate_reset_wait()
            print(f"  primary rate limit hit; sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if _transient(msg) and attempt < 8:
            print(f"  transient gh error (attempt {attempt}): {msg[:160]!r}; retry in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue
        raise GhError(f"{name}: {msg[:800]}")
    raise GhError(f"{name}: retries exhausted")


def rate_reset_wait() -> float:
    r = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, text=True)
    try:
        reset = json.loads(r.stdout)["resources"]["graphql"]["reset"]
        return max(10.0, reset - time.time() + 5)
    except Exception:
        return 300.0


# ----------------------------------------------------------------------------- queries

COMMIT_FIELDS = ("commit { oid authoredDate committedDate message "
                 "author { name email user { login } } statusCheckRollup { state } }")
CONN = {
    "files": "files(first: 100{after}) { totalCount pageInfo { hasNextPage endCursor } nodes { path additions deletions } }",
    "commits": "commits(first: 100{after}) { totalCount pageInfo { hasNextPage endCursor } nodes { " + COMMIT_FIELDS + " } }",
    "reviews": ("reviews(first: 100{after}) { totalCount pageInfo { hasNextPage endCursor } nodes { "
                "author { __typename login } state submittedAt commit { oid } comments { totalCount } } }"),
    "comments": ("comments(first: 100{after}) { totalCount pageInfo { hasNextPage endCursor } nodes { "
                 "author { __typename login } createdAt } }"),
    "timelineItems": ("timelineItems(first: 100{after}, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, "
                      "HEAD_REF_FORCE_PUSHED_EVENT]) { pageInfo { hasNextPage endCursor } nodes { __typename "
                      "... on ReadyForReviewEvent { createdAt } ... on ConvertToDraftEvent { createdAt } "
                      "... on HeadRefForcePushedEvent { createdAt } } }"),
}
LABELS = "labels(first: 50) { nodes { name } }"
PR_FIELDS = ("id number title url author { __typename login } baseRefName createdAt mergedAt isDraft "
             "additions deletions changedFiles " + LABELS + " "
             + " ".join(c.replace("{after}", "") for c in CONN.values()))

SEARCH_Q = """query($q: String!, $n: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  search(query: $q, type: ISSUE, first: $n, after: $cursor) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { %s } }
  }
}""" % PR_FIELDS

COUNT_Q = """query($q: String!) { rateLimit { cost remaining resetAt } search(query: $q, type: ISSUE, first: 1) { issueCount } }"""

ONE_PR_Q = """query($owner: String!, $name: String!, $num: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) { pullRequest(number: $num) { %s } }
}""" % PR_FIELDS


def continuation_q(conn: str) -> str:
    return ("query($id: ID!, $cursor: String) { rateLimit { cost remaining resetAt } node(id: $id) { ... on PullRequest { "
            + CONN[conn].replace("{after}", ", after: $cursor") + " } } }")


def complete_connections(node: dict) -> dict:
    for conn in CONN:
        c = node.get(conn) or {}
        nodes = list(c.get("nodes") or [])
        pi = c.get("pageInfo") or {}
        pages = 0
        while pi.get("hasNextPage") and pages < 50:
            d = gh_graphql(continuation_q(conn), {"id": node["id"], "cursor": pi["endCursor"]}, f"cont_{conn}")
            cc = d["data"]["node"][conn]
            nodes.extend(cc["nodes"])
            pi = cc["pageInfo"]
            pages += 1
        if pi.get("hasNextPage"):
            print(f"::warning:: #{node['number']} {conn} truncated after {len(nodes)} items", file=sys.stderr)
        node[conn] = {"totalCount": c.get("totalCount"), "nodes": nodes}
    return node


def search_window(repo: str, since: date, until: date, page: int, ttl: float | None) -> tuple[dict, list]:
    """All PRs merged in [since, until] (dates inclusive), bisecting over the ~1000 cap."""
    q = f"repo:{repo} is:pr is:merged merged:{since.isoformat()}..{until.isoformat()}"
    count = gh_graphql(COUNT_Q, {"q": q}, "count", ttl)["data"]["search"]["issueCount"]
    if count > 900 and since < until:
        mid = since + (until - since) // 2
        a, sa = search_window(repo, since, mid, page, ttl)
        b, sb = search_window(repo, mid + timedelta(days=1), until, page, ttl)
        a.update(b)
        return a, sa + sb
    out: dict[int, dict] = {}
    cursor = None
    while True:
        d = gh_graphql(SEARCH_Q, {"q": q, "n": page, "cursor": cursor}, "search", ttl)
        s = d["data"]["search"]
        for n in s["nodes"]:
            if n and n.get("number"):
                out[n["number"]] = n
        if not s["pageInfo"]["hasNextPage"]:
            break
        cursor = s["pageInfo"]["endCursor"]
    short = []
    if len(out) < count:
        print(f"::warning:: {q}: API reported {count}, fetched {len(out)}", file=sys.stderr)
        short.append({"since": str(since), "until": str(until), "reported": count, "fetched": len(out)})
    print(f"  {since}..{until}: {len(out)} PRs", file=sys.stderr)
    return out, short


# ----------------------------------------------------------------------------- time helpers


def ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def offset_for(cfg: dict, login: str | None) -> timedelta:
    h = (cfg.get("author_utc_offset_hours") or {}).get(login or "", cfg.get("utc_offset_hours", 0))
    return timedelta(hours=h)


def off_days(cfg: dict, login: str | None) -> set[date]:
    s = cfg["_absence_sets"]
    return set(s.get("*", set())) | set(s.get(login or "", set()))


def is_workday(d: date, off: set[date]) -> bool:
    return d.weekday() < 5 and d not in off


def workdays_elapsed(a: datetime | None, b: datetime | None, cfg: dict, login: str | None) -> float | None:
    """Fractional working days between two instants: wall time that falls on Mon-Fri
    (author-local calendar), minus absence dates. 24h of a weekday = 1.0."""
    if a is None or b is None:
        return None
    if b <= a:
        return 0.0
    off = off_days(cfg, login)
    tz = offset_for(cfg, login)
    la, lb = (a + tz).replace(tzinfo=None), (b + tz).replace(tzinfo=None)
    total = 0.0
    d = la.date()
    while d <= lb.date():
        if is_workday(d, off):
            lo = max(la, datetime.combine(d, datetime.min.time()))
            hi = min(lb, datetime.combine(d + timedelta(days=1), datetime.min.time()))
            if hi > lo:
                total += (hi - lo).total_seconds() / 86400
        d += timedelta(days=1)
    return round(total, 3)


def workdays_span(a: datetime | None, b: datetime | None, cfg: dict, login: str | None) -> int | None:
    """Count of working dates touched from a's date to b's date, inclusive."""
    if a is None or b is None:
        return None
    off = off_days(cfg, login)
    tz = offset_for(cfg, login)
    d, end = (a + tz).date(), (b + tz).date()
    n = 0
    while d <= end:
        n += is_workday(d, off)
        d += timedelta(days=1)
    return n


def local_date(t: datetime, cfg: dict, login: str | None) -> date:
    return (t + offset_for(cfg, login)).date()


# ----------------------------------------------------------------------------- classification


def compile_cfg(cfg: dict) -> dict:
    c = {k: [re.compile(p, re.I) for p in cfg[k]] for k in
         ("docs_patterns", "tests_patterns", "bot_login_patterns", "ai_reviewer_patterns", "ai_coauthor_patterns")}
    c["bot_logins"] = {x.lower() for x in cfg.get("bot_logins") or []}
    return c


def classify_path(path: str, cc: dict, cfg: dict) -> str:
    if any(fnmatch.fnmatch(path, g) for g in cfg.get("ignore_globs") or []):
        return "ignored"
    if any(r.search(path) for r in cc["docs_patterns"]):
        return "docs"
    if any(r.search(path) for r in cc["tests_patterns"]):
        return "tests"
    return "code"


def is_bot(actor: dict | None, cc: dict) -> bool:
    if not actor:
        return True  # deleted account / ghost: not a human reviewer signal
    login = (actor.get("login") or "")
    if actor.get("__typename") == "Bot" or login.lower() in cc["bot_logins"]:
        return True
    return any(r.search(login) for r in cc["bot_login_patterns"])


COAUTHOR_RE = re.compile(r"^co-authored-by:\s*(.+)$", re.I | re.M)


def commit_ai(commit: dict, cc: dict) -> bool:
    trailers = COAUTHOR_RE.findall(commit.get("message") or "")
    a = commit.get("author") or {}
    who = " ".join(trailers) + " " + (a.get("name") or "") + " " + (a.get("email") or "")
    return any(r.search(who) for r in cc["ai_coauthor_patterns"])


def cluster_count(times: list[datetime], minutes: int) -> int:
    times = sorted(times)
    n, last = 0, None
    for t in times:
        if last is None or (t - last) > timedelta(minutes=minutes):
            n += 1
        last = t
    return n


# ----------------------------------------------------------------------------- per-PR record


def build_record(n: dict, cfg: dict, cc: dict) -> dict:
    author = (n.get("author") or {}).get("login")
    created, merged = ts(n["createdAt"]), ts(n["mergedAt"])
    commits = [x["commit"] for x in n["commits"]["nodes"]]
    reviews = n["reviews"]["nodes"]
    comments = n["comments"]["nodes"]
    tl = n["timelineItems"]["nodes"]

    # size
    split = {"code": 0, "tests": 0, "docs": 0, "ignored": 0}
    files_by_class = {"code": 0, "tests": 0, "docs": 0, "ignored": 0}
    for f in n["files"]["nodes"]:
        k = classify_path(f["path"], cc, cfg)
        split[k] += f["additions"] + f["deletions"]
        files_by_class[k] += 1

    # commits / working time
    authored = [ts(c["authoredDate"]) for c in commits]
    first_commit = min(authored) if authored else None
    last_commit = max(authored) if authored else None
    start = min(first_commit, created) if first_commit else created

    def by_author(c):
        u = ((c.get("author") or {}).get("user") or {}).get("login")
        return u == author
    own = [c for c in commits if by_author(c)]
    off = off_days(cfg, author)
    own_days = {local_date(ts(c["authoredDate"]), cfg, author) for c in own}
    # also count days the author pushed commits (committer-date) - rebases distort it, so separate field
    active_days = sorted(d for d in own_days if is_workday(d, off))

    # ready-for-review instant (coding phase end)
    ready_events = sorted(ts(e["createdAt"]) for e in tl if e.get("__typename") == "ReadyForReviewEvent")
    ready = ready_events[0] if ready_events else created

    # reviews
    rev = sorted((r for r in reviews if r.get("submittedAt")), key=lambda r: r["submittedAt"])
    non_author = [r for r in rev if (r.get("author") or {}).get("login") != author]
    human = [r for r in non_author if not is_bot(r.get("author"), cc)]
    botr = [r for r in non_author if is_bot(r.get("author"), cc)]
    first_human = ts(human[0]["submittedAt"]) if human else None
    first_any = ts(non_author[0]["submittedAt"]) if non_author else None
    approvals = [ts(r["submittedAt"]) for r in human if r["state"] == "APPROVED"]
    changes_req = sum(1 for r in human if r["state"] == "CHANGES_REQUESTED")

    push_times = [ts(c["committedDate"]) for c in commits] + \
                 [ts(e["createdAt"]) for e in tl if e.get("__typename") == "HeadRefForcePushedEvent"]
    mins = cfg.get("push_cluster_minutes", 15)

    def pushes_after(t):
        if t is None:
            return 0
        return cluster_count([p for p in push_times if p > t and p <= merged], mins)
    start_t = first_any if cfg.get("rounds_start_on_bot_review") else first_human
    pushes_after_review = pushes_after(start_t)
    review_rounds = pushes_after_review + changes_req

    # round counting by reviewed head (one round = one distinct commit reviewed)
    def distinct_heads(rs):
        return len({(r.get("commit") or {}).get("oid") or r["submittedAt"] for r in rs})

    # comments: review submissions with body/inline comments + issue comments
    def tally(actors_items):
        h = b = 0
        for actor, cnt in actors_items:
            if (actor or {}).get("login") == author:
                continue
            if is_bot(actor, cc):
                b += cnt
            else:
                h += cnt
        return h, b
    inline = [(r.get("author"), (r.get("comments") or {}).get("totalCount", 0)) for r in reviews]
    convo = [(c.get("author"), 1) for c in comments]
    inline_h, inline_b = tally(inline)
    convo_h, convo_b = tally(convo)

    ai_bots = sorted({(x.get("author") or {}).get("login") for x in reviews + comments
                      if x.get("author") and any(r.search(x["author"].get("login") or "") for r in cc["ai_reviewer_patterns"])})

    ci_failed_commits = sum(1 for c in commits if (c.get("statusCheckRollup") or {}).get("state") in ("FAILURE", "ERROR"))
    ci_known = sum(1 for c in commits if c.get("statusCheckRollup"))

    last_approval = max(approvals) if approvals else None
    return {
        "number": n["number"], "title": n["title"], "url": n.get("url"), "author": author,
        "labels": [l["name"] for l in n["labels"]["nodes"]], "base": n["baseRefName"],
        "created_at": n["createdAt"], "merged_at": n["mergedAt"],
        "first_commit_at": first_commit.isoformat() if first_commit else None,
        "last_commit_at": last_commit.isoformat() if last_commit else None,
        "size": {"additions": n["additions"], "deletions": n["deletions"], "changed_files": n["changedFiles"],
                 "lines": split, "files": files_by_class,
                 "files_listed": len(n["files"]["nodes"])},
        "paths": [f["path"] for f in n["files"]["nodes"]],
        "commits": len(commits), "commits_by_author": len(own),
        "time": {
            "calendar_days": round((merged - start).total_seconds() / 86400, 2),
            "weekdays_span": workdays_span(start, merged, cfg, author),
            "weekdays_elapsed": workdays_elapsed(start, merged, cfg, author),
            "active_days": len(active_days),
            "active_days_incl_weekends": len(own_days),
            "active_dates": [d.isoformat() for d in active_days],
            "first_commit_predates_pr_days": round((created - first_commit).total_seconds() / 86400, 2) if first_commit else None,
        },
        "phases_weekdays": {
            "coding": workdays_elapsed(start, max(ready, start), cfg, author),
            "wait_first_human_review": workdays_elapsed(ready, first_human, cfg, author) if first_human and first_human >= ready else (0.0 if first_human else None),
            "first_human_review_to_merge": workdays_elapsed(first_human, merged, cfg, author),
            "last_approval_to_merge": workdays_elapsed(last_approval, merged, cfg, author),
            "ci_wall": None,
        },
        "review": {
            "rounds": review_rounds,
            "pushes_after_first_review": pushes_after_review,
            "changes_requested": changes_req,
            "human_reviewers": sorted({r["author"]["login"] for r in human}),
            "human_reviews": len(human), "bot_reviews": len(botr),
            "human_review_heads": distinct_heads(human), "bot_review_heads": distinct_heads(botr),
            "pushes_after_first_bot_review": pushes_after(ts(botr[0]["submittedAt"]) if botr else None),
            "comments_human": inline_h + convo_h + sum(1 for r in human if r["state"] in ("COMMENTED", "CHANGES_REQUESTED")),
            "comments_bot": inline_b + convo_b + len(botr),
            "inline_comments_human": inline_h, "inline_comments_bot": inline_b,
            "first_human_review_at": first_human.isoformat() if first_human else None,
            "approvals": len(approvals),
        },
        "ai": {
            "ai_coauthored_share": round(sum(commit_ai(c, cc) for c in commits) / len(commits), 3) if commits else None,
            "ai_review_bots": ai_bots,
            "ai_review_present": bool(ai_bots),
        },
        "ci": {"failed_commits": ci_failed_commits, "commits_with_status": ci_known,
               "distinct_failing_checks": None, "note": "distinct_failing_checks needs `history.py ci` (expensive)"},
        "risk_tier": None,
    }


# ----------------------------------------------------------------------------- risk scorer


def risk_tier(paths: list[str], cfg: dict) -> str | None:
    cmd = cfg.get("risk_score_cmd")
    if not cmd or not paths:
        return None
    key = _cache_path("risk", [json.dumps(cmd), "\n".join(sorted(paths))])
    if key.exists():
        return json.loads(key.read_text())
    try:
        r = subprocess.run(list(cmd) + paths, capture_output=True, text=True, cwd=cfg.get("risk_score_cwd"), timeout=600)
        m = re.search(cfg["risk_score_regex"], r.stdout)
        val = m.group(1) if m else None
    except (OSError, subprocess.TimeoutExpired):
        val = None
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text(json.dumps(val))
    return val


# ----------------------------------------------------------------------------- commands


def cmd_fetch(a):
    cfg = load_config(a.config)
    cc = compile_cfg(cfg)
    owner, name = a.repo.split("/")
    until = date.fromisoformat(a.until) if a.until else datetime.now(timezone.utc).date()
    since = date.fromisoformat(a.since) if a.since else until - timedelta(days=a.days)
    # completed windows are immutable once merged; only re-query the last 2 days
    nodes: dict[int, dict] = {}
    shortfalls = []
    cut = until - timedelta(days=2)
    if since <= cut:
        got, sf = search_window(a.repo, since, cut, cfg["search_page_size"], None)
        nodes.update(got); shortfalls += sf
    got, sf = search_window(a.repo, max(since, cut + timedelta(days=1)), until, cfg["search_page_size"], 3600)
    nodes.update(got); shortfalls += sf
    for num in a.include or []:
        if num not in nodes:
            d = gh_graphql(ONE_PR_Q, {"owner": owner, "name": name, "num": num}, "one_pr")
            nodes[num] = d["data"]["repository"]["pullRequest"]
    print(f"fetched {len(nodes)} PR nodes; completing connections...", file=sys.stderr)
    recs = []
    for i, num in enumerate(sorted(nodes)):
        node = complete_connections(nodes[num])
        recs.append((build_record(node, cfg, cc), [f["path"] for f in node["files"]["nodes"]]))
        if i % 250 == 0:
            print(f"  built {i}/{len(nodes)}", file=sys.stderr)
    if cfg.get("risk_score_cmd"):
        print("scoring risk tiers...", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            tiers = list(ex.map(lambda rp: risk_tier(rp[1], cfg), recs))
        for (rec, _), t in zip(recs, tiers):
            rec["risk_tier"] = t
    out = open_out(a.out) if a.out else sys.stdout
    for rec, _ in sorted(recs, key=lambda x: x[0]["merged_at"]):
        out.write(json.dumps(rec) + "\n")
    if a.out:
        out.close()
    meta = {"repo": a.repo, "since": str(since), "until": str(until), "prs": len(recs), "shortfalls": shortfalls,
            "gh_calls": STATS["calls"], "cache_hits": STATS["cache_hits"], "graphql_points": STATS["points"]}
    print(json.dumps(meta), file=sys.stderr)
    if a.out:
        Path(a.out).with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))


CI_Q = """query($id: ID!, $cursor: String, $n: Int!) {
  rateLimit { cost remaining resetAt }
  node(id: $id) { ... on PullRequest { commits(first: $n, after: $cursor) { pageInfo { hasNextPage endCursor }
    nodes { commit { oid checkSuites(first: 60) { nodes { createdAt updatedAt conclusion app { slug }
      checkRuns(first: 80) { nodes { name conclusion startedAt completedAt } } } } } } } } }
}"""
FAIL = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}


def cmd_ci(a):
    """Expensive (~10-30 GraphQL points per PR): distinct failing check runs and CI wall time."""
    owner, name = a.repo.split("/")
    recs = [json.loads(l) for l in Path(a.inp).read_text().splitlines()] if a.inp and Path(a.inp).exists() else []
    want = {int(x) for x in a.prs.split(",")}
    res = {}
    for num in sorted(want):
        node = gh_graphql(ONE_PR_Q, {"owner": owner, "name": name, "num": num}, "one_pr")["data"]["repository"]["pullRequest"]
        failing, walls, cursor = set(), [], None
        while True:
            d = None
            for n_commits in (5, 2, 1):  # large check fan-outs trip GitHub's per-query resource limit
                try:
                    d = gh_graphql(CI_Q, {"id": node["id"], "cursor": cursor, "n": n_commits}, "ci")
                    break
                except GhError as e:
                    if "resource limits" not in str(e).lower():
                        raise
            if d is None:
                failing = None
                break
            cs = d["data"]["node"]["commits"]
            for c in cs["nodes"]:
                suites = c["commit"]["checkSuites"]["nodes"]
                runs = [r for s in suites for r in s["checkRuns"]["nodes"]]
                for r in runs:
                    if r["conclusion"] in FAIL:
                        failing.add(r["name"])
                st = [ts(r["startedAt"]) for r in runs if r.get("startedAt")]
                en = [ts(r["completedAt"]) for r in runs if r.get("completedAt")]
                if st and en:
                    walls.append((min(st), max(en)))
            if not cs["pageInfo"]["hasNextPage"]:
                break
            cursor = cs["pageInfo"]["endCursor"]
        if failing is None:
            res[num] = {"distinct_failing_checks": None, "failing_check_names": None, "ci_wall_hours": None,
                        "error": "GitHub resource limit even at 1 commit per query"}
            continue
        # CI wall = union of per-commit [first start, last finish] spans, in hours
        walls.sort()
        tot, cur = 0.0, None
        for s, e in walls:
            if cur and s <= cur[1]:
                cur = (cur[0], max(cur[1], e))
            else:
                if cur:
                    tot += (cur[1] - cur[0]).total_seconds()
                cur = (s, e)
        if cur:
            tot += (cur[1] - cur[0]).total_seconds()
        res[num] = {"distinct_failing_checks": len(failing), "failing_check_names": sorted(failing),
                    "ci_wall_hours": round(tot / 3600, 2)}
    if recs:
        for r in recs:
            if r["number"] in res:
                r["ci"].update(res[r["number"]])
                r["ci"]["note"] = "from `history.py ci`"
                r["phases_weekdays"]["ci_wall"] = round(res[r["number"]]["ci_wall_hours"] / 24, 3)
        with open(a.inp, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
    print(json.dumps(res, indent=2))
    print(json.dumps({"gh_calls": STATS["calls"], "graphql_points": STATS["points"]}), file=sys.stderr)


# ----------------------------------------------------------------------------- summary


def pct(vals, p):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def fmt(x):
    return "-" if x is None else (f"{x:.1f}" if isinstance(x, float) else str(x))


def dist_row(label, rs):
    a = [r["time"]["active_days"] for r in rs]
    w = [r["time"]["weekdays_span"] for r in rs]
    return (f"| {label} | {len(rs)} | {fmt(pct(a,50))} / {fmt(pct(a,75))} / {fmt(pct(a,90))} | "
            f"{fmt(pct(w,50))} / {fmt(pct(w,75))} / {fmt(pct(w,90))} |")


SIZE_BUCKETS = [(0, 300, "<300"), (300, 1500, "300-1500"), (1500, 5000, "1500-5000"), (5000, 10**12, ">5000")]
ROUND_BUCKETS = [(0, 1, "0"), (1, 2, "1"), (2, 4, "2-3"), (4, 8, "4-7"), (8, 10**9, "8+")]


def cmd_summary(a):
    recs = [json.loads(l) for l in Path(a.inp).read_text().splitlines()]
    meta_p = Path(a.inp).with_suffix(".meta.json")
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    L = []
    L.append(f"# PR history summary: {meta.get('repo', '')}\n")
    L.append(f"{len(recs)} merged PRs, merged {meta.get('since', '?')} to {meta.get('until', '?')}. "
             "Active days = distinct weekdays with at least one commit authored by the PR author "
             "(author dates, UTC unless configured). Weekdays = weekday dates from first commit (or PR open, if earlier) to merge, inclusive. "
             "Cells are median / p75 / p90.\n")
    excluded = [r for r in recs if r["commits_by_author"] == 0]
    recs_all, recs = recs, [r for r in recs if r["commits_by_author"] > 0]
    L.append(f"Distributions exclude {len(excluded)} PRs with no commit authored by the PR author "
             "(bot-opened PRs, cherry-picks of someone else's commits); their active days are 0 by construction.\n")
    head = "| Bucket | PRs | Active days | Weekdays |\n|---|---|---|---|"
    L.append("## By changed code lines (additions + deletions, excluding tests and docs)\n")
    L.append(head)
    for lo, hi, lab in SIZE_BUCKETS:
        L.append(dist_row(lab, [r for r in recs if lo <= r["size"]["lines"]["code"] < hi]))
    L.append("\n## By review rounds (pushes after first human review + changes-requested reviews)\n")
    L.append(head)
    for lo, hi, lab in ROUND_BUCKETS:
        L.append(dist_row(lab, [r for r in recs if lo <= r["review"]["rounds"] < hi]))
    L.append(dist_row("all", recs))
    L.append("\n## Phase medians by size bucket (fractional weekdays)\n")
    L.append("| Bucket | Coding (first commit to ready) | Wait for first human review | First human review to merge | PRs with no human review |\n|---|---|---|---|---|")
    for lo, hi, lab in SIZE_BUCKETS:
        rs = [r for r in recs if lo <= r["size"]["lines"]["code"] < hi]
        ph = lambda k: fmt(pct([r["phases_weekdays"][k] for r in rs], 50))
        L.append(f"| {lab} | {ph('coding')} | {ph('wait_first_human_review')} | {ph('first_human_review_to_merge')} | "
                 f"{sum(1 for r in rs if not r['review']['first_human_review_at'])} |")
    L.append("\n## Ten largest PRs by changed code lines\n")
    L.append("| PR | Code lines | Tests | Docs | Files | Active days | Weekdays | Rounds |\n|---|---|---|---|---|---|---|---|")
    for r in sorted(recs_all, key=lambda r: -r["size"]["lines"]["code"])[:10]:
        s = r["size"]
        L.append(f"| #{r['number']} | {s['lines']['code']} | {s['lines']['tests']} | {s['lines']['docs']} | "
                 f"{s['changed_files']} | {r['time']['active_days']} | {r['time']['weekdays_span']} | {r['review']['rounds']} |")
    if a.focus:
        L.append("")
        for num in a.focus:
            r = next((x for x in recs_all if x["number"] == num), None)
            if not r:
                L.append(f"## PR #{num}\n\nNot in the data set.")
                continue
            L.append(f"## PR #{num}: {r['title']}\n")
            L.append("```json\n" + json.dumps({k: r[k] for k in ("created_at", "merged_at", "first_commit_at",
                     "last_commit_at", "size", "commits", "commits_by_author", "time", "phases_weekdays", "review", "ai", "ci", "risk_tier")},
                     indent=1) + "\n```")
    if a.notes and Path(a.notes).exists():
        L.append("\n" + Path(a.notes).read_text())
    text = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text)
    else:
        print(text)


def main(argv=None):
    global CACHE_DIR
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cache-dir", default=str(CACHE_DIR))
    sp = p.add_subparsers(dest="cmd", required=True)
    f = sp.add_parser("fetch")
    f.add_argument("--repo", required=True)
    f.add_argument("--days", type=int, default=120)
    f.add_argument("--since"); f.add_argument("--until")
    f.add_argument("--config")
    f.add_argument("--out")
    f.add_argument("--include", type=int, nargs="*", help="extra PR numbers to fetch even if outside the window")
    f.add_argument("--workers", type=int, default=6)
    c = sp.add_parser("ci")
    c.add_argument("--repo", required=True); c.add_argument("--prs", required=True)
    c.add_argument("--in", dest="inp")
    s = sp.add_parser("summary")
    s.add_argument("--in", dest="inp", required=True)
    s.add_argument("--focus", type=int, nargs="*")
    s.add_argument("--notes", help="markdown file appended verbatim")
    s.add_argument("--out")
    a = p.parse_args(argv)
    CACHE_DIR = Path(a.cache_dir)
    {"fetch": cmd_fetch, "ci": cmd_ci, "summary": cmd_summary}[a.cmd](a)


if __name__ == "__main__":
    main()
