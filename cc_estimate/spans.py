"""Add non-working time from any tracker to a tasks file.

Export the periods when a work item was not being worked on (a Jira "Blocked" status, a Linear
"On hold" state, a waiting-for-review column) as CSV with the header `key,group,start,end`
(ISO-8601 times), then merge them in. They are subtracted from the task's working time.
"""
import argparse, csv, json, sys
from pathlib import Path
from collections import defaultdict


def merge(tasks, rows):
    by_key = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_key[r["key"]][r["group"]].append([r["start"], r["end"]])
    hit = 0
    for t in tasks:
        extra = by_key.get(str(t["id"]))
        if extra:
            hit += 1
            ex = t.setdefault("excluded", {})
            for g, spans in extra.items():
                ex[g] = ex.get(g, []) + spans
    return hit


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cc-estimate spans", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    tasks = [json.loads(l) for l in Path(a.tasks).read_text().splitlines()]
    with open(a.csv, newline="") as f:
        hit = merge(tasks, list(csv.DictReader(f)))
    with open(a.out, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")
    print(f"spans added to {hit} of {len(tasks)} tasks", file=sys.stderr)


if __name__ == "__main__":
    main()
