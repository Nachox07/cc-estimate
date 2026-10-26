"""Build tasks from local git alone: no GitHub, works with any tracker whose keys appear in
commit messages (Jira `ABC-123`, Linear `ENG-42`, GitHub `#123`).

A task is every commit on the branch whose message carries the same key. It starts at the first
commit and ends at the last. Commits without a key are skipped and counted.
"""
import argparse, json, re, subprocess, sys
from collections import defaultdict
from .files import open_out

DEFAULT_KEY = r"\b([A-Z][A-Z0-9]+-\d+)\b"
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


def build(cs, key_re, ignore_re=None):
    groups, skipped = defaultdict(list), 0
    for c in cs:
        text = c["subject"] + "\n" + c["body"]
        if ignore_re and ignore_re.search(c["author"]):
            continue
        m = key_re.search(text)
        if not m:
            skipped += 1
            continue
        groups[m.group(1)].append(c)
    rows = []
    for key, cl in groups.items():
        cl.sort(key=lambda c: c["at"])
        authors = defaultdict(int)
        for c in cl:
            authors[c["author"]] += 1
        rows.append(dict(id=key, title=cl[0]["subject"], author=max(authors, key=authors.get), labels=[],
                         start=cl[0]["at"], end=cl[-1]["at"], lines=sum(c.get("lines", 0) for c in cl),
                         commits=len(cl), excluded={}))
    rows.sort(key=lambda r: r["start"])
    return rows, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cc-estimate git-tasks", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--branch", default="HEAD")
    ap.add_argument("--since", default="120 days ago")
    ap.add_argument("--key-regex", default=DEFAULT_KEY, help="first group is the task key")
    ap.add_argument("--ignore-authors", help="regex of bot author emails to skip")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows, skipped = build(commits(a.repo_dir, a.branch, a.since), re.compile(a.key_regex),
                          re.compile(a.ignore_authors) if a.ignore_authors else None)
    with open_out(a.out) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(rows)} tasks; {skipped} commits had no key and were skipped", file=sys.stderr)


if __name__ == "__main__":
    main()
