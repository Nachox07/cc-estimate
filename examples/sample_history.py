"""Write a made-up history to try the commands on: three people, 300 tasks, duration driven by size.

    python3 examples/sample_history.py data/sample.tasks.jsonl
"""
import json, random, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

rnd = random.Random(1)
out, t = [], datetime(2026, 1, 5, 9, tzinfo=timezone.utc)
for i in range(1, 301):
    t += timedelta(hours=rnd.choice([3, 5, 8, 20]))
    if t.weekday() >= 5:
        t += timedelta(days=7 - t.weekday())
    lines = int(rnd.choice([20, 150, 700, 2500, 7000]) * rnd.uniform(0.6, 1.4))
    hours = lines / 60 * rnd.uniform(0.5, 2.0) + rnd.uniform(1, 24)
    out.append(dict(id=f"DEMO-{i}", title=rnd.choice(["feat: add", "fix: repair", "chore: tidy"]),
                    author=["alice", "bob", "carol"][i % 3], labels=[], start=t.isoformat(),
                    end=(t + timedelta(hours=hours * 2.4)).isoformat(), lines=lines, excluded={}))
dest = Path(sys.argv[1] if len(sys.argv) > 1 else "data/sample.tasks.jsonl")
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text("".join(json.dumps(r) + "\n" for r in out))
print(f"Wrote {len(out)} tasks to {dest}.")
