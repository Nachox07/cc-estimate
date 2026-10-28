"""Mutation probe: plant one known bug at a time and check the test suite fails.

    python3 tests/mutation_probe.py        # from the repo root; about a minute

Each line is a bug the tests must catch (KILLED). The last line changes nothing and must SURVIVE,
which shows the harness itself is not failing every run.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
M = [
 ("backtest.py", 'h = [x for x in rows[:i] if x["end"] <= t["start"]]', 'h = [x for x in rows if x is not t]', "future leak: history includes tasks that finish later"),
 ("backtest.py", 'h = [x for x in rows[:i] if x["end"] <= t["start"]]', 'h = [x for x in rows[:i] if x["start"] <= t["start"]]', "partial leak: unfinished tasks count as history"),
 ("backtest.py", 'winkler=(hi - lo) + 2 / alpha * miss', 'winkler=(hi - lo) + miss', "Winkler miss penalty dropped"),
 ("backtest.py", 'inside=lo <= t["y"] <= hi', 'inside=True', "coverage always 100%"),
 ("backtest.py", 'if d.weekday() < 5 and', 'if d.weekday() < 7 and', "weekends count as working time"),
 ("backtest.py", 'ov = min(hi, y) - max(lo, x)', 'ov = min(hi, y) - min(hi, y)', "blocked time not subtracted"),
 ("backtest.py", 'and d.date() not in off:', ':', "absence days not subtracted"),
 ("backtest.py", 'return first, n - max(rest - int(rest * 0.8), MIN_SCORED)', 'return first, n - (rest - int(rest * 0.8))', "locked slice can shrink below 10"),
 ("backtest.py", 'return first, n - max(rest - int(rest * 0.8), MIN_SCORED)', 'return first, first', "locked slice = everything after warm-up (tuning overlaps scoring)"),
 ("backtest.py", 'cut = t["start"] - timedelta(days=30)', 'cut = t["start"] - timedelta(days=3650)', "trailing-30d baseline uses all history"),
 ("backtest.py", '(50, "XS"), (300, "S")', '(50, "XS"), (3000, "S")', "size bucket boundary moved"),
 ("backtest.py", 'w = len(ratios) / (len(ratios) + PACE_PRIOR)', 'w = 1.0', "pace factor not shrunk"),
 ("backtest.py", 'size=bucket(r.get("code_lines", r["lines"]))', 'size=bucket(r["lines"])', "size buckets on total lines, tests and docs included"),
 ("backtest.py", 'r.get("test_lines", 0) > TEST_HEAVY *', 'r.get("test_lines", 0) >= TEST_HEAVY *', "test-heavy at exactly 3x"),
 ("gitsource.py", 'if a != "-" and kind != "ignored":', 'if a != "-":', "git source counts ignored files"),
 ("tasks.py", 'code_lines=code, test_lines=tests', 'code_lines=code + tests, test_lines=tests', "task code lines include tests"),
 ("backtest.py", 'fell = len(s) < k_min', 'fell = False', "fallback not reported"),
 ("estimate.py", 'rows = [r for r in rows if r["start"] <= cut]', 'rows = rows', "estimate keeps right-censored recent tasks"),
 ("estimate.py", 'same = [r["y"] for r in rows if r["size"] == size]', 'same = [r["y"] for r in rows]', "estimate ignores the size"),
 ("estimate.py", 'LO_P, HI_P = (1 - LEVEL) / 2, 1 - (1 - LEVEL) / 2', 'LO_P, HI_P = 0.25, 0.75', "estimate prints a 50% range, not 80%"),
 ("gitsource.py", 'end = min(after, key=when) if after else last', 'end = max(after, key=when) if after else last', "git task ends at last merge, not first"),
 ("gitsource.py", 'end = min(after, key=when) if after else last', 'end = last', "git task ignores merge commits"),
 ("gitsource.py", 'key = find_key(MERGE_PR.sub("", m["text"]), patterns)', 'key = find_key(m["text"], patterns)', "merge-PR number taken as a key"),
 ("tasks.py", 'if r["base"] == branch and', 'if', "release-branch PRs kept as tasks"),
 ("tasks.py", 'and not any(b.search(r["author"] or "") for b in bot_re)]', ']', "bot PRs kept as tasks"),
 ("tasks.py", 'end=max(prs[n]["merged_at"] for n in ns)', 'end=min(prs[n]["merged_at"] for n in ns)', "task ends at first PR merge, not last"),
 ("tasks.py", 'groups[("issue", closing[0]) if closing else ("pr", n)].append(n)', 'groups[("pr", n)].append(n)', "PRs closing one issue not grouped"),
 ("history.py", 'return len(rs) >= MIN_CELL and len({r.get("author") for r in rs}) >= MIN_AUTHORS', 'return True', "summary shows small groups"),
 ("files.py", 'def open_out', '# control\ndef open_out', "control: no behaviour change (must survive)"),
]
rows = []
for f, old, new, desc in M:
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "r"; shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "build", "*.egg-info"))
        p = dst / "cc_estimate" / f; s = p.read_text()
        if old not in s:
            rows.append(dict(file=f, desc=desc, result="NOT APPLIED")); continue
        p.write_text(s.replace(old, new, 1))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests"], cwd=dst, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(dst), "PYTHONDONTWRITEBYTECODE": "1"})
        failed = [l.split("::")[-1].split(" ")[0] for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))]
        rows.append(dict(file=f, desc=desc, result="KILLED" if r.returncode else "SURVIVED", by=failed[:1]))
for x in rows:
    print(f"{x['result']:9} {x['file']:13} {x['desc']}" + (f"  <- {x['by'][0]}" if x.get("by") else ""))
bad = [x for x in rows[:-1] if x["result"] != "KILLED"] + [x for x in rows[-1:] if x["result"] != "SURVIVED"]
sys.exit(1 if bad else 0)
