"""Build tasks from Jira Cloud, headless: no MCP, no dependencies.

Reads JIRA_URL (https://yourco.atlassian.net), JIRA_EMAIL and JIRA_API_TOKEN from the environment.
Uses the token-paginated search (POST /rest/api/3/search/jql) with the changelog expanded; the older
offset-paginated /rest/api/3/search is being retired.

A task is one resolved issue. It starts at the first move into an "In Progress"-category status
(else at creation, marked start_kind "created") and ends at the resolution date. Time spent in
the blocked statuses becomes the excluded group "blocked". Unresolved issues are skipped and counted.
With --repo-dir, lines come from commits whose message carries the issue key.
"""
import argparse, base64, json, os, re, subprocess, sys, time
import urllib.error, urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from . import gitsource
from .files import open_out
from .history import classify_path, compile_cfg

FIELDS = ["summary", "status", "assignee", "labels", "created", "resolutiondate"]
DEFAULT_BLOCKED = ["Blocked", "On Hold"]
MAX_RETRIES, PAGE = 5, 100


class JiraError(Exception):
    pass


def _http(method, url, headers, body):
    """The only network call: returns (status, headers, bytes)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except urllib.error.URLError as e:
        raise JiraError(f"cannot reach Jira ({type(e.reason).__name__})") from None


def _sleep(s):
    time.sleep(s)


class Client:
    def __init__(self, base, email, token):
        self.base = base.rstrip("/")
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.headers = {"Authorization": f"Basic {auth}", "Accept": "application/json",
                        "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(MAX_RETRIES + 1):
            status, headers, raw = _http(method, self.base + path, self.headers, data)
            if status == 429 and attempt < MAX_RETRIES:
                h = {k.lower(): v for k, v in headers.items()}
                try:
                    wait = min(float(h.get("retry-after", "")), 120)
                except ValueError:
                    wait = 2 ** attempt
                _sleep(wait)
                continue
            if status >= 400:
                raise JiraError(f"Jira returned {status}: {_short(raw)}")
            return json.loads(raw or b"{}")


def _short(raw):
    try:
        d = json.loads(raw)
        msg = (d.get("errorMessages") or [""])[0] or json.dumps(d.get("errors", ""))
    except ValueError:
        msg = ""
    return msg[:160] or "no message"


def search(client, jql):
    token = None
    while True:
        body = {"jql": jql, "fields": FIELDS, "expand": "changelog", "maxResults": PAGE}
        if token:
            body["nextPageToken"] = token
        d = client.call("POST", "/rest/api/3/search/jql", body)
        yield from d.get("issues", [])
        token = d.get("nextPageToken")
        if not token or d.get("isLast"):
            return


def full_histories(client, issue):
    """The search expands only the first histories of an issue; fetch the rest when truncated."""
    cl = issue.get("changelog") or {}
    got = cl.get("histories") or []
    if cl.get("total", len(got)) <= len(got):
        return got
    out, start = [], 0
    while True:
        d = client.call("GET", f"/rest/api/3/issue/{issue['key']}/changelog?startAt={start}&maxResults={PAGE}")
        out += d.get("values", [])
        start += len(d.get("values", []))
        if d.get("isLast", True) or not d.get("values"):
            return out


def status_categories(client):
    return {s["name"].lower(): s["statusCategory"]["key"] for s in client.call("GET", "/rest/api/3/status")}


def when(s):
    """Jira writes +0100; Python 3.10 wants +01:00."""
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s)


def build_row(issue, histories, categories, blocked, users):
    f = issue["fields"]
    if not f.get("resolutiondate"):
        return None
    created, end = when(f["created"]), when(f["resolutiondate"])
    ev = sorted(((when(h["created"]), i) for h in histories for i in h.get("items", [])), key=lambda x: x[0])
    moves = [(at, (i.get("toString") or "")) for at, i in ev if i.get("field") == "status"]
    blocked = {b.lower() for b in blocked}
    start, kind = created, "created"
    for at, to in moves:
        if to.lower() not in blocked and categories.get(to.lower()) == "indeterminate":
            start, kind = at, "transition"
            break
    spans, open_at = [], None
    for at, to in moves:
        if open_at is not None:
            spans.append([open_at, at]); open_at = None
        if to.lower() in blocked:
            open_at = at
    if open_at is not None:
        spans.append([open_at, end])
    spans = [[a.isoformat(), min(b, end).isoformat()] for a, b in spans if a < end]
    who = (f.get("assignee") or {}).get("accountId")
    later = [i for at, i in ev if i.get("field") == "assignee" and at > end]
    if later:
        who = later[0].get("from")
    who = who or "unassigned"
    return dict(id=issue["key"], title=f.get("summary") or "", author=users.get(who, who),
                labels=list(f.get("labels") or []), start=start.isoformat(), start_kind=kind,
                end=end.isoformat(), end_kind="resolution", lines=0, code_lines=0, test_lines=0,
                doc_lines=0, excluded={"blocked": spans} if spans else {})


def line_counts(repo_dir, branch, since, patterns, cfg):
    """Per-key code/test/doc lines from commit messages, split by path class."""
    fmt = gitsource.UNIT.join(["%H", "%s", "%b"]) + gitsource.REC
    out = subprocess.run(["git", "-C", repo_dir, "log", branch, "--no-merges", f"--since={since}",
                          f"--format={fmt}", "--numstat"], capture_output=True, text=True, check=True).stdout
    cc, totals, key = compile_cfg(cfg), defaultdict(lambda: defaultdict(int)), None
    names = {"code": "code_lines", "tests": "test_lines", "docs": "doc_lines"}
    for chunk in out.split(gitsource.REC):
        lines = chunk.strip("\n").split("\n")
        n = 0
        while n < len(lines) and (m := re.match(r"^(\d+|-)\t(\d+|-)\t(.+)$", lines[n])):
            if key and m[1] != "-":  # numstat lines trail their commit record
                kind = classify_path(m[3], cc, cfg)
                if kind != "ignored":
                    totals[key][names[kind]] += int(m[1]) + int(m[2])
            n += 1
        rest = "\n".join(lines[n:])
        if rest.strip():
            _, subject, body = (rest.split(gitsource.UNIT) + ["", ""])[:3]
            key = gitsource.find_key(subject + "\n" + body, patterns)
    return totals


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cc-estimate jira-tasks", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jql", required=True)
    ap.add_argument("--config", help="cc-estimate.json; jira.users and jira.blocked_statuses are read from it")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo-dir", help="git clone to read lines from, matching issue keys in commit messages")
    ap.add_argument("--branch", default="HEAD")
    ap.add_argument("--since", default="400 days ago")
    a = ap.parse_args(argv)
    cfg = json.loads(Path(a.config).read_text()) if a.config else {}
    jcfg = cfg.get("jira", {})
    env = {k: os.environ.get(k, "") for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        sys.exit(f"cc-estimate: set {', '.join(missing)} in the environment.")
    if not env["JIRA_URL"].startswith("https://"):
        sys.exit("cc-estimate: JIRA_URL must start with https://")
    client = Client(env["JIRA_URL"], env["JIRA_EMAIL"], env["JIRA_API_TOKEN"])
    try:
        cats = status_categories(client)
        rows, unresolved = [], 0
        for issue in search(client, a.jql):
            row = build_row(issue, full_histories(client, issue) if issue["fields"].get("resolutiondate") else [],
                            cats, jcfg.get("blocked_statuses", DEFAULT_BLOCKED), jcfg.get("users", {}))
            if row:
                rows.append(row)
            else:
                unresolved += 1
    except JiraError as e:
        sys.exit(f"cc-estimate: {e}")
    if a.repo_dir:
        base = json.loads(Path(__file__).with_name("config.example.json").read_text())
        counts = line_counts(a.repo_dir, a.branch, a.since, gitsource.key_patterns(cfg.get("git", {}).get("key_regex")),
                             {"ai_reviewer_patterns": [], **base, **cfg})
        for r in rows:
            c = counts.get(r["id"], {})
            r.update(code_lines=c.get("code_lines", 0), test_lines=c.get("test_lines", 0), doc_lines=c.get("doc_lines", 0))
            r["lines"] = r["code_lines"] + r["test_lines"] + r["doc_lines"]
    rows.sort(key=lambda r: when(r["start"]))
    with open_out(a.out) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(rows)} tasks; {unresolved} unresolved issues skipped", file=sys.stderr)
    if not a.repo_dir:
        print("sizes are missing (no --repo-dir): lines are 0, so `estimate --size` cannot work from this file.", file=sys.stderr)


if __name__ == "__main__":
    main()
