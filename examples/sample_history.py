"""Write a made-up history to try the commands on: three people, 300 tasks, duration driven by product-code size.

    python3 examples/sample_history.py data/sample.tasks.jsonl

A thin wrapper over `cc-estimate simulate --scenario size-matters`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cc_estimate import simulate

rows, _ = simulate.generate("size-matters", seed=1, n=300)
dest = Path(sys.argv[1] if len(sys.argv) > 1 else "data/sample.tasks.jsonl")
simulate.write_rows(rows, dest)
print(f"Wrote {len(rows)} tasks to {dest}.")
