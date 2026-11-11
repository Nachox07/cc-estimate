"""How often the advisor names a break, over the shipped advisor scenarios.

    python3 tests/break_detection_rate.py [first_seed last_seed]     # default 0 99; a few minutes

Prints, per scenario, in how many seeds the break was named (and for the no-break scenarios, in how many
any break was named), and how many size buckets were not compared.
"""
import sys, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cc_estimate import advise, simulate

UTC = ZoneInfo("UTC")
CASES = [("break-point", "XL", "3x wider at XL, 60 tasks per size"), ("weak-break", "XL", "2x wider at XL, 60 tasks per size"),
         ("thin-advisor", "XL", "3x wider at XL, 15 XL tasks"), ("no-break", None, "same spread at every size"),
         ("small-tasks", None, "medians 0.5-2 h, same spread at every size"), ("size-matters", None, "800 tasks, same spread at every size")]


def run(first=0, last=99):
    lines = []
    for name, expect, what in CASES:
        hit = skipped = 0
        for seed in range(first, last + 1):
            rows, _ = simulate.generate(name, seed)
            with tempfile.TemporaryDirectory() as tmp:
                simulate.write_rows(rows, Path(tmp) / "t.jsonl")
                an = advise.analyse(advise.load_history(str(Path(tmp) / "t.jsonl"), UTC, {}), UTC)
            hit += an["size_break"] == expect if expect else an["size_break"] is not None
            skipped += sum(1 for d in an["size_detail"] if d[6])
        n = last - first + 1
        what_hit = f"named {expect}" if expect else "named a break"
        lines.append(f"{name:13} ({what}): {what_hit} in {hit} of {n} seeds; {skipped} buckets not compared over {n} seeds")
    return lines


if __name__ == "__main__":
    print("\n".join(run(*map(int, sys.argv[1:3])) if len(sys.argv) == 3 else run()))
