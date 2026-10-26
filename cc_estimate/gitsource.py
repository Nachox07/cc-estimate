"""Build tasks from local git alone: no GitHub, works with any tracker whose keys appear in
commit messages (Jira `ABC-123`, Linear `ENG-42`, or `#123`).

A task is every commit on the branch whose message carries the same key. It starts at the first
commit. It ends at the merge commit that landed it when the history has one, else at the last
commit (a commit span, which leaves out review and merge waiting). Commits without a key are
skipped and counted.
"""
import argparse, json, re, subprocess, sys
from datetime import datetime
from collections import defaultdict
from .files import open_out

# Tried in order, so a tracker key beats the "(#123)" a squash merge appends.
DEFAULT_KEYS = [r"\b([A-Z][A-Z0-9]+-\d+)\b", r"(?<![\w&])(#\d+)\b"]
REC, UNIT = "\x1e", "\x1f"


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
        key = find_key(m["text"], patterns)
        if key:
            landed[key].append(m["at"])
    when = datetime.fromisoformat
    rows = []
    for key, cl in groups.items():
        cl.sort(key=lambda c: when(c["at"]))
        authors = defaultdict(int)
        for c in cl:
            authors[c["author"]] += 1
        last = cl[-1]["at"]
        after = [m for m in landed.get(key, []) if when(m) >= when(last)]
        end = max(after, key=when) if after else last
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
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows, skipped = build(commits(a.repo_dir, a.branch, a.since), key_patterns(a.key_regex),
                          re.compile(a.ignore_authors) if a.ignore_authors else None,
                          merged=merges(a.repo_dir, a.branch, a.since))
    with open_out(a.out) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(rows)} tasks; {skipped} commits had no key and were skipped", file=sys.stderr)


if __name__ == "__main__":
    main()
