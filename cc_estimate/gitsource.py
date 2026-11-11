"""Build tasks from local git alone: no GitHub, works with any tracker whose keys appear in
commit messages (Jira `ABC-123`, Linear `ENG-42`, or `#123`).

A task is every commit on the branch whose message carries the same key. It starts at the first
commit. It ends at the merge commit that landed it when the history has one, else at the last
commit (a commit span, which leaves out review and merge waiting). Commits without a key are
skipped and counted.
"""
import argparse, json, re, subprocess, sys
from datetime import datetime, timezone
from collections import defaultdict
from .files import open_out, write_since

# Tried in order, so a tracker key beats the "(#123)" a squash merge appends.
DEFAULT_KEYS = [r"\b([A-Z][A-Z0-9]+-\d+)\b", r"(?<![\w&])(#\d+)\b"]
REC, UNIT = "\x1e", "\x1f"
MERGE_PR = re.compile(r"^Merge pull request #\d+")  # the PR's own number, not the task key


def commits(repo_dir, branch, since):
    fmt = UNIT.join(["%H", "%ae", "%aI", "%s", "%b"]) + REC
    out = subprocess.run(["git", "-C", repo_dir, "log", branch, "--no-merges", f"--since={since}",
                          f"--format={fmt}", "--numstat"], capture_output=True, text=True, check=True).stdout
    # numstat lines follow each record, so they sit at the start of the next chunk
    chunks = out.split(REC)
    parsed, stats = [], []
    for i, chunk in enumerate(chunks):
        lines = chunk.strip("\n").split("\n")
        if i > 0:  # leading numstat lines belong to the previous commit
            head = [l for l in lines if re.match(r"^(\d+|-)\t(\d+|-)\t", l)]
            stats.append(sum(int(a) + int(b) for a, b, _ in (l.split("\t", 2) for l in head) if a != "-"))
            lines = [l for l in lines if l not in head]
        rest = "\n".join(lines).strip("\n")
        if rest:
            sha, email, date, subject, body = (rest.split(UNIT) + [""] * 5)[:5]
            parsed.append(dict(sha=sha, author=email, at=date, subject=subject, body=body))
    for c, n in zip(parsed, stats):
        c["lines"] = n
    return parsed


def merges(repo_dir, branch, since):
    """Merge commits on the branch's first-parent line, with the time each one landed."""
    fmt = UNIT.join(["%cI", "%s", "%b"]) + REC
    out = subprocess.run(["git", "-C", repo_dir, "log", branch, "--merges", "--first-parent", f"--since={since}",
                          f"--format={fmt}"], capture_output=True, text=True, check=True).stdout
    rows = []
    for chunk in out.split(REC):
        chunk = chunk.strip("\n")
        if chunk:
            at, subject, body = (chunk.split(UNIT) + [""] * 3)[:3]
            rows.append(dict(at=at, text=subject + "\n" + body))
    return rows


def since_epoch(repo_dir, since):
    """The instant git reads `since` as, so every query and the sidecar agree on one left edge."""
    out = subprocess.run(["git", "-C", repo_dir, "rev-parse", f"--since={since}"], capture_output=True, text=True).stdout
    m = re.search(r"--max-age=(\d+)", out)
    if not m:
        sys.exit(f"git-tasks: cannot read --since {since!r} as a date")
    return int(m.group(1))


def older_keys(repo_dir, branch, since, patterns, ignore_re=None, wanted=None):
    """Task keys with a commit before `since`: those tasks began outside the window, so their
    in-window commits would give a late start and a short duration. Streams the log and stops once
    every key in `wanted` is found. A key reused from long ago (an old "refs #123") drops today's task."""
    fmt = UNIT.join(["%ae", "%s", "%b"]) + REC
    proc = subprocess.Popen(["git", "-C", repo_dir, "log", branch, "--no-merges", f"--until={since}", f"--format={fmt}"],
                            stdout=subprocess.PIPE, text=True)
    keys, buf = set(), ""
    try:
        for part in iter(lambda: proc.stdout.read(65536), ""):
            *chunks, buf = (buf + part).split(REC)
            for chunk in chunks:
                email, subject, body = (chunk.strip("\n").split(UNIT) + [""] * 3)[:3]
                if not (ignore_re and ignore_re.search(email)):
                    keys.add(find_key(subject + "\n" + body, patterns))
            if wanted is not None and wanted <= keys:
                break
    finally:
        proc.kill(); proc.wait(); proc.stdout.close()
    return keys - {None}


def key_patterns(spec):
    """Key regexes from config or the CLI: one string or a list; the first group is the task key."""
    spec = spec or DEFAULT_KEYS
    return [re.compile(p) for p in ([spec] if isinstance(spec, str) else spec)]


def find_key(text, patterns):
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1)
    return None


def build(cs, patterns, ignore_re=None, merged=()):
    groups, skipped = defaultdict(list), 0
    for c in cs:
        if ignore_re and ignore_re.search(c["author"]):
            continue
        key = find_key(c["subject"] + "\n" + c["body"], patterns)
        if not key:
            skipped += 1
            continue
        groups[key].append(c)
    landed = defaultdict(list)
    for m in merged:
        key = find_key(MERGE_PR.sub("", m["text"]), patterns)
        if key:
            landed[key].append(m["at"])
    when = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # 3.10 rejects "Z"
    rows = []
    for key, cl in groups.items():
        cl.sort(key=lambda c: when(c["at"]))
        authors = defaultdict(int)
        for c in cl:
            authors[c["author"]] += 1
        last = cl[-1]["at"]
        after = [m for m in landed.get(key, []) if when(m) >= when(last)]
        end = min(after, key=when) if after else last
        rows.append(dict(id=key, title=cl[0]["subject"], author=max(authors, key=authors.get), labels=[],
                         start=cl[0]["at"], end=end, end_kind="merge" if after else "commit",
                         lines=sum(c.get("lines", 0) for c in cl), commits=len(cl), excluded={}))
    rows.sort(key=lambda r: when(r["start"]))
    return rows, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cc-estimate git-tasks", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--branch", default="HEAD")
    ap.add_argument("--since", default="120 days ago")
    ap.add_argument("--key-regex", action="append", help="repeatable; tried in order; first group is the task key")
    ap.add_argument("--ignore-authors", help="regex of bot author emails to skip")
    ap.add_argument("--config", help="cc-estimate.json; its git.key_regex is used when --key-regex is not given")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    cfg = json.loads(open(a.config).read()) if a.config else {}
    since = f"@{since_epoch(a.repo_dir, a.since)}"
    pats = key_patterns(a.key_regex or cfg.get("git", {}).get("key_regex"))
    ignore = re.compile(a.ignore_authors) if a.ignore_authors else None
    rows, skipped = build(commits(a.repo_dir, a.branch, since), pats, ignore, merged=merges(a.repo_dir, a.branch, since))
    older = older_keys(a.repo_dir, a.branch, since, pats, ignore, {r["id"] for r in rows})
    kept = [r for r in rows if r["id"] not in older]
    left, rows = len(rows) - len(kept), kept
    with open_out(a.out) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    write_since(a.out, datetime.fromtimestamp(int(since[1:]), timezone.utc))
    print(f"{len(rows)} tasks; {skipped} commits had no key and were skipped; "
          f"{left} dropped on the left (first commit before --since)", file=sys.stderr)


if __name__ == "__main__":
    main()
