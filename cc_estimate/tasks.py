#!/usr/bin/env python3
"""Group merged PRs into tasks. A task is the set of default-branch PRs sharing an issue.

Grouping key: the PR's lowest-numbered closing issue; a PR closing nothing is its own task.
Body references ("Refs #N") are not used: most cite umbrella issues or rules.
Release-branch PRs are carries of work already counted on main and are dropped.
Bot and automation authors are dropped.
"""
import argparse, json, re
from pathlib import Path
from collections import defaultdict
from .files import open_out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--prs", required=True)
    ap.add_argument("--links", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text())
    bots = set(cfg.get("bot_logins", [])) | set(cfg.get("automation_logins", []))
    bot_re = [re.compile(p) for p in cfg.get("bot_login_patterns", [r"\[bot\]$"])]
    branch = cfg.get("default_branch", "main")
    prs = {r["number"]: r for r in map(json.loads, Path(a.prs).read_text().splitlines())}
    links = {r["number"]: r for r in map(json.loads, Path(a.links).read_text().splitlines())}

    keep = [n for n, r in prs.items()
            if r["base"] == branch and r.get("merged_at") and r["author"] not in bots
            and not any(b.search(r["author"] or "") for b in bot_re)]

    # One key per PR, no transitive chaining: chaining through shared references merged
    # unrelated PRs months apart into one "task".
    groups = defaultdict(list)
    for n in keep:
        closing = sorted(i["number"] for i in links.get(n, {}).get("closing", []))
        groups[("issue", closing[0]) if closing else ("pr", n)].append(n)

    with open_out(a.out) as f:
        for root, ns in groups.items():
            ns.sort(key=lambda n: prs[n]["created_at"])
            first = prs[ns[0]]
            starts = [prs[n]["created_at"] for n in ns] + [prs[n]["first_commit_at"] for n in ns if prs[n].get("first_commit_at")]
            issues = sorted({i["number"] for n in ns for i in links.get(n, {}).get("closing", [])})
            assigned = [i["first_assigned_at"] for n in ns for i in links.get(n, {}).get("closing", []) if i.get("first_assigned_at")]
            sz = [prs[n]["size"]["lines"] for n in ns]
            excluded = defaultdict(list)  # label group -> spans, from the PRs and their closing issues
            for n in ns:
                l = links.get(n, {})
                for src in [l] + l.get("closing", []):
                    for g, spans in (src.get("excluded") or {}).items():
                        excluded[g] += spans
            f.write(json.dumps(dict(
                id=ns[0], prs=ns, issues=issues, author=first["author"], title=first["title"],
                labels=first["labels"], start=min(starts, key=lambda s: s.replace("Z", "+00:00")),
                end=max(prs[n]["merged_at"] for n in ns), issue_first_assigned_at=min(assigned) if assigned else None,
                lines=sum(s["code"] + s["tests"] + s["docs"] for s in sz),
                excluded=excluded,
                review_rounds=sum(prs[n]["review"]["rounds"] for n in ns),
            )) + "\n")
    multi = sum(len(v) > 1 for v in groups.values())
    print(f"{len(keep)} PRs -> {len(groups)} tasks ({multi} with >1 PR)")


if __name__ == "__main__":
    main()
