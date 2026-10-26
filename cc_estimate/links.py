#!/usr/bin/env python3
"""Fetch the PR -> issue links that history.py does not: closing references, body
references, base branch, when each linked issue was first assigned, and the spans the PR or
issue carried a label whose time is not work time (config `excluded_labels`, e.g. blocked, or
waiting for a review gate that no longer exists). Stdlib + gh."""
import argparse, json, re, sys
from pathlib import Path
from datetime import date, timedelta

from . import history as h
from .files import open_out

LABEL_EVENTS = """labelEvents: timelineItems(first: 50, itemTypes: [LABELED_EVENT, UNLABELED_EVENT]) { nodes {
  __typename ... on LabeledEvent { createdAt label { name } } ... on UnlabeledEvent { createdAt label { name } } } }"""
FIELDS = """number baseRefName headRefName title body createdAt mergedAt author { login } %s
closingIssuesReferences(first: 10) { nodes { number createdAt title closedAt
  labels(first: 30) { nodes { name } } %s
  timelineItems(first: 10, itemTypes: [ASSIGNED_EVENT]) { nodes { ... on AssignedEvent { createdAt } } } } }""" % (LABEL_EVENTS, LABEL_EVENTS)
DEFAULT_EXCLUDED = {"blocked": ["blocked"]}

Q = """query($q: String!, $n: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  search(query: $q, type: ISSUE, first: $n, after: $cursor) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { %s } } } }""" % FIELDS

REF = re.compile(r"(?i)\b(?:closes|fixes|resolves|refs|part of|relates to|tracking)\s*:?\s*#(\d+)")
PICK = re.compile(r"(?i)(?:cherry[- ]?pick(?:ed)?(?: of| from)?|carry(?:ing)?|backport(?: of)?)\D{0,20}#(\d+)")
TITLE_PR = re.compile(r"\(#(\d+)\)\s*$")


def window(repo, since, until, ttl):
    q = f"repo:{repo} is:pr is:merged merged:{since}..{until}"
    count = h.gh_graphql(h.COUNT_Q, {"q": q}, "count", ttl)["data"]["search"]["issueCount"]
    if count > 900 and since < until:
        mid = since + (until - since) // 2
        return window(repo, since, mid, ttl) + window(repo, mid + timedelta(days=1), until, ttl)
    out, cursor = [], None
    while True:
        s = h.gh_graphql(Q, {"q": q, "n": 50, "cursor": cursor}, "links", ttl)["data"]["search"]
        out += [n for n in s["nodes"] if n and n.get("number")]
        if not s["pageInfo"]["hasNextPage"]:
            break
        cursor = s["pageInfo"]["endCursor"]
    if len(out) < count:
        print(f"::warning:: {q}: reported {count}, fetched {len(out)}", file=sys.stderr)
    print(f"  {since}..{until}: {len(out)}", file=sys.stderr)
    return out


def label_spans(events, close_at, names):
    """[start, end] spans while any label in `names` was on; an open span ends at close_at."""
    spans, on, active = [], None, set()
    for e in sorted((e for e in (events or {}).get("nodes") or [] if e and e["label"]["name"] in names),
                    key=lambda e: e["createdAt"]):
        if e["__typename"] == "LabeledEvent":
            if not active:
                on = e["createdAt"]
            active.add(e["label"]["name"])
        else:
            active.discard(e["label"]["name"])
            if not active and on is not None:
                spans.append([on, e["createdAt"]]); on = None
    if on is not None and close_at:
        spans.append([on, close_at])
    return spans


def excluded_spans(events, close_at, groups):
    return {g: label_spans(events, close_at, set(names)) for g, names in groups.items()}


def blocked_spans(events, close_at):
    return label_spans(events, close_at, set(DEFAULT_EXCLUDED["blocked"]))


def record(n, groups):
    body = n.get("body") or ""
    closing = []
    for i in (n.get("closingIssuesReferences") or {}).get("nodes") or []:
        assigned = [t["createdAt"] for t in (i.get("timelineItems") or {}).get("nodes") or [] if t]
        closing.append(dict(number=i["number"], created_at=i["createdAt"], title=i["title"],
                            labels=[l["name"] for l in i["labels"]["nodes"]],
                            first_assigned_at=min(assigned) if assigned else None,
                            excluded=excluded_spans(i.get("labelEvents"), i.get("closedAt") or n["mergedAt"], groups)))
    picked = PICK.findall(body) + PICK.findall(n["title"])
    m = TITLE_PR.search(n["title"])
    return dict(number=n["number"], base=n["baseRefName"], head=n["headRefName"],
                excluded=excluded_spans(n.get("labelEvents"), n["mergedAt"], groups),
                closing=closing, body_refs=sorted({int(x) for x in REF.findall(body)}),
                cherry_pick_of=int(picked[0]) if picked else (int(m.group(1)) if m and n["baseRefName"] != "main" else None))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--since", required=True); ap.add_argument("--until", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    groups = (json.loads(Path(a.config).read_text()).get("excluded_labels") if a.config else None) or DEFAULT_EXCLUDED
    since, until = date.fromisoformat(a.since), date.fromisoformat(a.until)
    rows, d = [], since
    while d <= until:  # month-sized windows keep each search under the 1000-result cap
        e = min(d + timedelta(days=30), until)
        rows += window(a.repo, d, e, None)
        d = e + timedelta(days=1)
    seen = {}
    for n in rows:
        seen[n["number"]] = record(n, groups)
    with open_out(a.out) as f:
        for r in sorted(seen.values(), key=lambda r: r["number"]):
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(seen)} rows to {a.out}; gh calls {h.STATS['calls']} cache {h.STATS['cache_hits']} points {h.STATS['points']}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
