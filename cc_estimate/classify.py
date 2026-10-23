#!/usr/bin/env python3
"""Give each task a workload profile: what the change has to go through, decided by the
repo's own rules from the paths it touches.

Paths come from the PR records (`cc-estimate history fetch` stores them). For records without paths,
it falls back to local git: the squash commit of each PR on the default branch.
Config "classifier":
  repo_dir          local clone
  default_branch    e.g. "origin/main"
  area_depth        {prefix: depth}: how many path segments name an area under a prefix
  approval_manifest file of [group:<name>] headers + regexes (! negates); a hit = that group must approve
  tiers_cmd         argv that takes --files and prints {"required": {tier: {"gating": ...}}}
  heavy_tiers       tiers that count as slow (real-stack, e2e, evals)
"""
import argparse, json, re, subprocess, sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SUBJECT_PR = re.compile(r"\(#(\d+)\)\s*$")


def pr_paths(repo_dir, branch, since):
    out = subprocess.run(["git", "-C", repo_dir, "log", branch, f"--since={since}", "--format=%x00%s", "--name-only"],
                         capture_output=True, text=True, check=True).stdout
    paths = {}
    for block in out.split("\x00")[1:]:
        lines = [l for l in block.splitlines() if l.strip()]
        m = SUBJECT_PR.search(lines[0]) if lines else None
        if m:
            paths[int(m.group(1))] = lines[1:]
    return paths


def load_manifest(path):
    groups, cur = defaultdict(lambda: ([], [])), None
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"\[group:([\w-]+)\]", line)
        if m:
            cur = m.group(1); continue
        if cur:
            (groups[cur][1] if line.startswith("!") else groups[cur][0]).append(re.compile(line.lstrip("!")))
    return groups


def approvals(paths, manifest):
    return sorted(g for g, (inc, exc) in manifest.items()
                  if any(any(r.search(p) for r in inc) and not any(r.search(p) for r in exc) for p in paths))


def area(path, depth_map):
    for prefix, depth in sorted(depth_map.items(), key=lambda kv: -len(kv[0])):
        if path.startswith(prefix):
            rest = path[len(prefix):].split("/")
            return prefix + "/".join(rest[:depth]) if len(rest) > depth else prefix.rstrip("/")
    return path.split("/")[0] if "/" in path else "(root)"


def tiers(paths, cmd, cwd):
    if not paths:
        return []
    r = subprocess.run(cmd + ["--files", *paths], capture_output=True, text=True, cwd=cwd)
    try:
        return sorted(json.loads(r.stdout).get("required", {}))
    except json.JSONDecodeError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prs", help="PR records from `cc-estimate history fetch`; their paths are used when present")
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text())
    c = cfg["classifier"]
    tasks = [json.loads(l) for l in Path(a.tasks).read_text().splitlines()]
    since = min(t["start"] for t in tasks)[:10]
    recs = [json.loads(l) for l in Path(a.prs).read_text().splitlines()] if a.prs else []
    paths = {r["number"]: r["paths"] for r in recs if "paths" in r}
    if c.get("repo_dir") and (len(paths) < len(recs) or not recs):
        paths = {**pr_paths(c["repo_dir"], c.get("default_branch", "origin/main"), since), **paths}
    manifest = load_manifest(Path(c.get("repo_dir", ".")) / c["approval_manifest"]) if c.get("approval_manifest") else {}
    heavy = set(c.get("heavy_tiers", []))

    def one(t):
        ps = sorted({p for n in t["prs"] for p in paths.get(n, [])})
        tr = tiers(ps, c["tiers_cmd"], c.get("repo_dir")) if c.get("tiers_cmd") else []
        return dict(id=t["id"], n_paths=len(ps), found=bool(ps),
                    areas=sorted({area(p, c.get("area_depth", {})) for p in ps}),
                    approvals=approvals(ps, manifest), tiers=tr,
                    heavy_tiers=sorted(set(tr or []) & heavy))

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(one, tasks))
    with open(a.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(rows)} tasks; paths found for {sum(r['found'] for r in rows)}; "
          f"tier lookup failed for {sum(r['tiers'] is None for r in rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
