"""Stdlib tests on synthetic data. Run: python3 -m unittest discover -s tests"""
import contextlib, io, json, random, re, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import os, subprocess

from cc_estimate import backtest as bt, estimate, gitsource, links, spans, tasks

MAD = ZoneInfo("Europe/Madrid")
UTC = timezone.utc
LOGINS = ["alice", "bob", "carol"]


def t(s):
    return datetime.fromisoformat(s).replace(tzinfo=MAD)


class WorkHours(unittest.TestCase):
    def test_one_weekday_counts_nine_hours(self):
        self.assertAlmostEqual(bt.biz_hours(t("2026-10-05 08:00"), t("2026-10-05 20:00"), MAD), 9)

    def test_overnight_counts_only_office_hours(self):
        self.assertAlmostEqual(bt.biz_hours(t("2026-10-05 17:00"), t("2026-10-06 10:00"), MAD), 2)

    def test_weekend_counts_nothing(self):
        self.assertEqual(bt.biz_hours(t("2026-10-10 09:00"), t("2026-10-11 18:00"), MAD), 0)

    def test_daylight_saving_day_still_nine_hours(self):
        self.assertAlmostEqual(bt.biz_hours(t("2026-10-26 00:00"), t("2026-10-26 23:00"), MAD), 9)

    def test_absence_days_are_removed(self):
        off = bt.expand_dates(["2026-10-06..2026-10-07"])
        self.assertAlmostEqual(bt.biz_hours(t("2026-10-05 09:00"), t("2026-10-08 18:00"), MAD, off), 18)

    def test_blocked_spans_are_removed_once_even_if_they_overlap(self):
        spans = [[t("2026-10-05 10:00"), t("2026-10-05 12:00")], [t("2026-10-05 11:00"), t("2026-10-05 13:00")]]
        self.assertAlmostEqual(bt.biz_hours(t("2026-10-05 09:00"), t("2026-10-05 18:00"), MAD, blocked=spans), 6)


class LabelSpans(unittest.TestCase):
    def ev(self, kind, at, name="blocked"):
        return {"__typename": kind, "createdAt": at, "label": {"name": name}}

    def test_open_span_closes_at_merge(self):
        events = {"nodes": [self.ev("LabeledEvent", "2026-10-05T10:00:00Z")]}
        self.assertEqual(links.label_spans(events, "2026-10-06T10:00:00Z", {"blocked"}),
                         [["2026-10-05T10:00:00Z", "2026-10-06T10:00:00Z"]])

    def test_two_labels_in_one_group_make_one_span(self):
        events = {"nodes": [self.ev("LabeledEvent", "2026-10-05T10:00:00Z", "a"),
                            self.ev("LabeledEvent", "2026-10-05T11:00:00Z", "b"),
                            self.ev("UnlabeledEvent", "2026-10-05T12:00:00Z", "a"),
                            self.ev("UnlabeledEvent", "2026-10-05T13:00:00Z", "b")]}
        self.assertEqual(links.label_spans(events, None, {"a", "b"}),
                         [["2026-10-05T10:00:00Z", "2026-10-05T13:00:00Z"]])

    def test_other_labels_are_ignored(self):
        events = {"nodes": [self.ev("LabeledEvent", "2026-10-05T10:00:00Z", "docs")]}
        self.assertEqual(links.label_spans(events, "2026-10-06T10:00:00Z", {"blocked"}), [])


def synthetic(n=400, seed=7):
    """PR records + links for a fake repo: size drives duration, with noise."""
    rnd = random.Random(seed)
    prs, lks = [], []
    start = datetime(2026, 6, 1, 9, tzinfo=UTC)
    for i in range(1, n + 1):
        lines = int(rnd.choice([20, 120, 600, 2500, 8000]) * rnd.uniform(0.6, 1.4))
        created = start + timedelta(hours=i * 7)
        merged = created + timedelta(hours=lines / 40 * rnd.uniform(0.5, 2.0) + rnd.uniform(0, 30))
        author = LOGINS[i % 3] if i % 50 else "release-bot[bot]"
        base = "main" if i % 40 else "release/1.0"
        prs.append(dict(number=i, title=rnd.choice(["feat: x", "fix: y", "chore: z"]), author=author, labels=[],
                        base=base, created_at=created.isoformat(), merged_at=merged.isoformat(),
                        first_commit_at=created.isoformat(), size={"lines": {"code": lines, "tests": 0, "docs": 0}},
                        review={"rounds": 1}))
        issue = 1000 + (i // 2 if i % 10 == 0 else i)  # every 10th PR shares an issue with the one before
        lks.append(dict(number=i, base=base, closing=[{"number": issue, "excluded": {}}], excluded={}))
    return prs, lks


def write(tmp, prs, lks, cfg):
    d = Path(tmp)
    (d / "prs.jsonl").write_text("".join(json.dumps(p) + "\n" for p in prs))
    (d / "links.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lks))
    (d / "cfg.json").write_text(json.dumps(cfg))
    with contextlib.redirect_stdout(io.StringIO()):
        tasks.main(["--prs", str(d / "prs.jsonl"), "--links", str(d / "links.jsonl"),
                    "--config", str(d / "cfg.json"), "--out", str(d / "tasks.jsonl")])
    return d


def task_rows(n, lines=100, every_h=24, start=datetime(2026, 6, 1, 9, tzinfo=UTC)):
    """Finished tasks of one size, one every `every_h` hours, lasting 4-16 hours."""
    out = []
    for i in range(n):
        s = start + timedelta(hours=i * every_h)
        out.append(dict(id=i + 1, title="feat: x", author=LOGINS[i % 3], labels=[], start=s.isoformat(),
                        end=(s + timedelta(hours=4 + 3 * (i % 5))).isoformat(), lines=lines, excluded={}))
    return out


def write_tasks(path, rows):
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))


def history_records(n=60):
    """PR records shaped like `fetch` output, authored by the three fixture logins."""
    out = []
    for i in range(1, n + 1):
        out.append(dict(number=i, title=f"feat: change {i}, thanks @{LOGINS[(i + 1) % 3]}", author=LOGINS[i % 3], commits_by_author=1,
                        created_at="2026-10-05T09:00:00Z", merged_at="2026-10-06T09:00:00Z",
                        first_commit_at="2026-10-05T08:00:00Z", last_commit_at="2026-10-06T08:00:00Z", commits=2,
                        size={"lines": {"code": 40 * i, "tests": 10, "docs": 0}, "changed_files": 3},
                        time={"active_days": 1 + i % 3, "weekdays_span": 2 + i % 4},
                        phases_weekdays={"coding": 1.0, "wait_first_human_review": 0.5, "first_human_review_to_merge": 0.5},
                        review={"rounds": i % 5, "first_human_review_at": "2026-10-05T15:00:00Z",
                                "human_reviewers": [LOGINS[(i + 1) % 3]], "ai_review_bots": ["reviewbot"]},
                        ai={}, ci={}, risk_tier="low"))
    return out


class Tasks(unittest.TestCase):
    def test_grouping_drops_bots_and_release_branches_and_joins_shared_issues(self):
        prs = [dict(number=n, title="feat: a", author=a, labels=[], base=b, created_at="2026-10-05T09:00:00Z",
                    merged_at="2026-10-05T12:00:00Z", first_commit_at=None,
                    size={"lines": {"code": 10, "tests": 0, "docs": 0}}, review={"rounds": 0})
               for n, a, b in [(1, "alice", "main"), (2, "alice", "main"), (3, "dep[bot]", "main"), (4, "bob", "release/1")]]
        lks = [dict(number=n, base="main", closing=[{"number": 50, "excluded": {}}] if n < 3 else [], excluded={})
               for n in (1, 2, 3, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, prs, lks, {})
            rows = [json.loads(l) for l in (d / "tasks.jsonl").read_text().splitlines()]
        self.assertEqual([r["prs"] for r in rows], [[1, 2]])

    def test_grouping_keeps_prs_into_the_configured_default_branch(self):
        prs, lks = synthetic(60)
        for p in prs:
            p["base"] = "trunk" if p["base"] == "main" else p["base"]
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, prs, lks, {"default_branch": "trunk"})
            rows = [json.loads(l) for l in (d / "tasks.jsonl").read_text().splitlines()]
        kept = {n for r in rows for n in r["prs"]}
        self.assertTrue(kept)
        self.assertTrue(all(prs[n - 1]["base"] == "trunk" for n in kept))


class Links(unittest.TestCase):
    def test_squash_suffix_is_a_cherry_pick_only_off_the_default_branch(self):
        n = dict(number=9, baseRefName="trunk", headRefName="x", title="fix: y (#3)", body="", mergedAt="2026-10-06T16:00:00Z")
        self.assertIsNone(links.record(n, {}, "trunk")["cherry_pick_of"])
        self.assertEqual(links.record({**n, "baseRefName": "release/1"}, {}, "trunk")["cherry_pick_of"], 3)


class Scoring(unittest.TestCase):
    def test_scoring_uses_the_configured_default_branch(self):
        pr = {"number": 5, "mergedAt": "2026-10-06T16:00:00Z", "createdAt": "2026-10-05T09:00:00Z",
              "baseRefName": "trunk", "commits": {"nodes": []}, "labelEvents": {"nodes": []}}
        rec = {"repo": "acme/widgets", "pr": 5, "author": "alice"}
        with mock.patch.object(estimate, "gql", return_value={"pullRequest": pr}):
            got = estimate.actual_for(rec, MAD, {"default_branch": "trunk"})
        self.assertEqual(got["prs"], [5])

    def test_github_source_takes_issue_numbers_only(self):
        self.assertEqual(estimate.task_key("#123", "github"), 123)
        self.assertEqual(estimate.task_key(123, None), 123)
        with self.assertRaises(SystemExit):
            estimate.task_key("ABC-1", "github")

    def test_git_source_takes_any_key_and_reads_a_bare_number_as_hash(self):
        self.assertEqual(estimate.task_key("123", "git"), "#123")
        self.assertEqual(estimate.task_key("ABC-1", "git"), "ABC-1")

    def test_score_skips_an_old_github_row_with_a_tracker_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "cfg.json").write_text(json.dumps({"repo": "acme/widgets", "data_dir": "."}))
            (d / "estimates.jsonl").write_text(json.dumps(dict(
                id="x1", author="alice", repo="acme/widgets", size="M", issue="ABC-1", pr=None,
                range_h=[1, 5, 20], baseline_h=[0, 3, 30], actual=None)) + "\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(estimate, "me", return_value="alice"):
                estimate.main(["score", "--config", str(d / "cfg.json")])
        self.assertIn("1 logged, 0 finished", buf.getvalue())


class Backtest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = write(self.tmp.name, *synthetic(), {"timezone": "Europe/Madrid"})
        self.rows = bt.load(str(d / "tasks.jsonl"), MAD, {})
        self.d = d

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_estimate_never_sees_the_future(self):
        i = 250
        before = bt.run(self.rows, [i], "size", 10, 0.1, 0.9)[0]
        for r in self.rows:
            if r is not self.rows[i] and r["end"] > self.rows[i]["start"]:
                r["y"] = 1e6  # outcomes not yet known at the start must not move the estimate
        after = bt.run(self.rows, [i], "size", 10, 0.1, 0.9)[0]
        self.assertEqual((before["width"], before["inside"]), (after["width"], after["inside"]))

    def test_size_beats_the_plain_baseline_when_size_drives_duration(self):
        idx = list(range(200, len(self.rows)))
        size = bt.score(bt.run(self.rows, idx, "size", 10, 0.1, 0.9))
        repo = bt.score(bt.run(self.rows, idx, "baseline: repo", 10, 0.1, 0.9))
        self.assertLess(size["winkler"], repo["winkler"])


class Privacy(unittest.TestCase):
    """No command may print another person's login or numbers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = {"repo": "acme/widgets", "timezone": "Europe/Madrid"}
        self.d = write(self.tmp.name, *synthetic(), cfg)
        (self.d / "cfg.json").write_text(json.dumps({**cfg, "data_dir": "."}))
        (self.d / "widgets.tasks.jsonl").write_text((self.d / "tasks.jsonl").read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            estimate.main([*args[:1], "--config", str(self.d / "cfg.json"), *args[1:]])
        return buf.getvalue()

    def assert_only(self, out, me):
        for login in LOGINS:
            if login != me:
                self.assertNotIn(login, out)

    def test_estimate_prints_no_logins(self):
        self.assert_only(self.run_cli("estimate", "--size", "M"), me=None)

    def test_backtest_prints_no_logins(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bt.main(["--in", str(self.d / "tasks.jsonl"), "--min-history", "100"])
        self.assert_only(buf.getvalue(), me=None)

    def test_score_shows_only_my_own_estimates(self):
        log = self.d / "estimates.jsonl"
        recs = [dict(id=f"id{who}", author=who, repo="acme/widgets", size="M", issue=7, pr=None,
                     range_h=[1, 5, 20], baseline_h=[0, 3, 30], actual={"hours": 4.0}) for who in ("alice", "bob")]
        log.write_text("".join(json.dumps(r) + "\n" for r in recs))
        with mock.patch.object(estimate, "me", return_value="alice"):
            out = self.run_cli("score")
        self.assertIn("1 logged", out)
        self.assertNotIn("idbob", out)
        self.assert_only(out, me="alice")


class GitSource(unittest.TestCase):
    def test_commits_with_the_same_key_make_one_task(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com",
                   "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
            git = lambda *args, **kw: subprocess.run(["git", "-C", d, *args], check=True, capture_output=True, env={**env, **kw})
            git("init", "-q")
            for i, (msg, when) in enumerate([("ABC-1 start", "2026-10-05T09:00:00+02:00"), ("no key here", "2026-10-05T10:00:00+02:00"),
                                             ("fix tests\n\nRefs ABC-1", "2026-10-06T11:00:00+02:00"), ("ABC-2 other", "2026-10-07T09:00:00+02:00")]):
                Path(d, f"f{i}.txt").write_text("x\n" * (i + 1))
                git("add", ".")
                git("commit", "-qm", msg.replace("\\n", "\n"), GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
            rows, skipped = gitsource.build(gitsource.commits(d, "HEAD", "2020-01-01"), gitsource.key_patterns(None))
        by = {r["id"]: r for r in rows}
        self.assertEqual(skipped, 1)
        self.assertEqual(by["ABC-1"]["commits"], 2)
        self.assertEqual(by["ABC-1"]["lines"], 1 + 3)
        self.assertEqual(by["ABC-1"]["start"][:10], "2026-10-05")
        self.assertEqual(by["ABC-1"]["end"][:10], "2026-10-06")

    def test_tracker_keys_win_over_hash_numbers_and_hash_numbers_work_alone(self):
        pats = gitsource.key_patterns(None)
        self.assertEqual(gitsource.find_key("fix login (#12)", pats), "#12")
        self.assertEqual(gitsource.find_key("ABC-1 fix login (#13)", pats), "ABC-1")
        self.assertEqual(gitsource.find_key("fix (#14)\n\nRefs ABC-2", pats), "ABC-2")
        self.assertIsNone(gitsource.find_key("quote &#39; in text", pats))

    def test_a_single_string_key_regex_still_works(self):
        pats = gitsource.key_patterns(r"\b(X-\d+)\b")
        self.assertEqual(gitsource.find_key("X-4 and ABC-1", pats), "X-4")

    def repo(self, d):
        env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com",
               "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
        def git(*args, when=None):
            e = {**env, **({"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else {})}
            subprocess.run(["git", "-C", d, *args], check=True, capture_output=True, env=e)
        git("init", "-q", "-b", "main")
        return git

    def test_a_merged_branch_ends_at_its_merge_commit(self):
        with tempfile.TemporaryDirectory() as d:
            git = self.repo(d)
            Path(d, "a").write_text("a\n"); git("add", "."); git("commit", "-qm", "init", when="2026-10-01T09:00:00+00:00")
            git("checkout", "-qb", "ABC-1-login")
            Path(d, "b").write_text("b\n"); git("add", "."); git("commit", "-qm", "ABC-1 work", when="2026-10-05T09:00:00+00:00")
            git("checkout", "-q", "main")
            git("merge", "-q", "--no-ff", "ABC-1-login", "-m", "Merge branch ABC-1-login", when="2026-10-07T15:00:00+00:00")
            pats = gitsource.key_patterns(None)
            rows, _ = gitsource.build(gitsource.commits(d, "main", "2020-01-01"), pats,
                                      merged=gitsource.merges(d, "main", "2020-01-01"))
        row = next(r for r in rows if r["id"] == "ABC-1")
        self.assertEqual((row["end"][:10], row["end_kind"]), ("2026-10-07", "merge"))

    def test_without_a_merge_commit_the_task_is_a_commit_span(self):
        with tempfile.TemporaryDirectory() as d:
            git = self.repo(d)
            Path(d, "a").write_text("a\n"); git("add", "."); git("commit", "-qm", "ABC-1 work", when="2026-10-05T09:00:00+00:00")
            rows, _ = gitsource.build(gitsource.commits(d, "main", "2020-01-01"), gitsource.key_patterns(None),
                                      merged=gitsource.merges(d, "main", "2020-01-01"))
        self.assertEqual(rows[0]["end_kind"], "commit")

    def merged_end(self, commit_msg, merges):
        cs = [dict(sha="1", author="a", at="2026-01-05T09:00:00+00:00", subject=commit_msg, body="", lines=1)]
        rows, _ = gitsource.build(cs, gitsource.key_patterns(None), merged=[dict(at=at, text=t) for at, t in merges])
        return rows[0]["end"][:10], rows[0]["end_kind"]

    def test_a_task_ends_at_the_first_merge_after_its_last_commit(self):
        self.assertEqual(self.merged_end("ABC-1 work", [("2026-01-06T09:00:00+00:00", "Merge branch ABC-1-login"),
                                                        ("2026-06-01T09:00:00+00:00", "Merge branch ABC-1-followup")]),
                         ("2026-01-06", "merge"))

    def test_a_merge_commit_pr_number_is_not_a_task_key(self):
        self.assertEqual(self.merged_end("Refs #12", [("2026-05-01T09:00:00+00:00", "Merge pull request #12 from o/other\n")]),
                         ("2026-01-05", "commit"))
        self.assertEqual(self.merged_end("#5 work", [("2026-01-07T09:00:00+00:00", "Merge pull request #40 from o/b\nFixes #5")]),
                         ("2026-01-07", "merge"))

    def test_git_tasks_reads_key_regex_from_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            git = self.repo(d)
            Path(d, "a").write_text("a\n"); git("add", "."); git("commit", "-qm", "X-4 and ABC-1", when="2026-10-05T09:00:00+00:00")
            Path(d, "cfg.json").write_text(json.dumps({"git": {"key_regex": r"\b(X-\d+)\b"}}))
            with contextlib.redirect_stderr(io.StringIO()):
                gitsource.main(["--repo-dir", d, "--since", "2020-01-01", "--config", str(Path(d, "cfg.json")),
                                "--out", str(Path(d, "t.jsonl"))])
            self.assertEqual(json.loads(Path(d, "t.jsonl").read_text())["id"], "X-4")

    def test_commits_order_by_time_across_timezone_offsets(self):
        cs = [dict(sha="1", author="a", at="2026-10-05T10:00:00+02:00", subject="ABC-1 b", body="", lines=1),
              dict(sha="2", author="a", at="2026-10-05T09:30:00+00:00", subject="ABC-1 a", body="", lines=1)]
        rows, _ = gitsource.build(cs, gitsource.key_patterns(None))
        self.assertEqual((rows[0]["title"], rows[0]["end"]), ("ABC-1 b", "2026-10-05T09:30:00+00:00"))


class Spans(unittest.TestCase):
    def test_tracker_spans_are_subtracted_by_key(self):
        task = {"id": "ABC-1", "excluded": {}}
        spans.merge([task], [{"key": "ABC-1", "group": "blocked", "start": "2026-10-05T10:00:00+02:00",
                              "end": "2026-10-05T12:00:00+02:00"}, {"key": "ABC-9", "group": "blocked",
                              "start": "x", "end": "y"}])
        blocked = [[bt.parse(a), bt.parse(b)] for a, b in task["excluded"]["blocked"]]
        self.assertAlmostEqual(bt.biz_hours(t("2026-10-05 09:00"), t("2026-10-05 18:00"), MAD, blocked=blocked), 7)


class OutputFiles(unittest.TestCase):
    def test_tasks_creates_a_missing_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, *synthetic(30), {})
            out = d / "new" / "dir" / "tasks.jsonl"
            with contextlib.redirect_stdout(io.StringIO()):
                tasks.main(["--prs", str(d / "prs.jsonl"), "--links", str(d / "links.jsonl"),
                            "--config", str(d / "cfg.json"), "--out", str(out)])
            self.assertTrue(out.exists())

    def test_logging_an_estimate_creates_the_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, *synthetic(), {})
            (d / "cfg.json").write_text(json.dumps({"repo": "acme/widgets", "data_dir": "not/yet"}))
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(estimate, "me", return_value="alice"):
                estimate.main(["estimate", "--config", str(d / "cfg.json"), "--tasks", str(d / "tasks.jsonl"),
                               "--size", "M", "--log"])
            self.assertTrue((d / "not" / "yet" / "estimates.jsonl").exists())


class ThinHistory(unittest.TestCase):
    def run_estimate(self, rows, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "cfg.json").write_text(json.dumps({"repo": "acme/widgets", "data_dir": "."}))
            if rows is not None:
                write_tasks(d / "widgets.tasks.jsonl", rows)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                estimate.main(["estimate", "--config", str(d / "cfg.json"), *extra])
            return buf.getvalue()

    def test_a_size_with_no_history_falls_back_to_the_size_blind_range(self):
        out = self.run_estimate(task_rows(80, lines=10), "--size", "XL")
        self.assertIn("Not enough history for size XL", out)
        self.assertIn("Without a size:", out)

    def test_missing_history_file_names_the_file(self):
        with self.assertRaises(SystemExit) as e:
            self.run_estimate(None, "--size", "M")
        self.assertIn("no history at", str(e.exception))

    def test_empty_history_file_says_so(self):
        with self.assertRaises(SystemExit) as e:
            self.run_estimate([], "--size", "M")
        self.assertIn("has no tasks", str(e.exception))

    def test_history_younger_than_the_censor_window_says_so(self):
        with self.assertRaises(SystemExit) as e:
            self.run_estimate(task_rows(10), "--size", "XS")
        self.assertIn("not enough history yet", str(e.exception))


class SmallHistory(unittest.TestCase):
    def run_backtest(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            write_tasks(Path(tmp) / "t.jsonl", rows)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bt.main(["--in", str(Path(tmp) / "t.jsonl")])
            return buf.getvalue()

    def test_split_scales_down_and_keeps_a_locked_slice(self):
        self.assertEqual(bt.split(100, 200), (50, 90))
        self.assertEqual(bt.split(1000, 200), (200, 840))
        self.assertEqual(bt.split(40, 200), (20, 30))
        self.assertIsNone(bt.split(39, 200))

    def test_fifty_finished_tasks_are_enough_to_run(self):
        out = self.run_backtest(task_rows(71))  # the last 21 days are censored, leaving 50
        locked = out.split("== LOCKED · all tasks ==")[1].split("==")[0]
        self.assertIn("baseline: repo", locked)

    def test_too_few_tasks_says_so(self):
        self.assertIn("too few to backtest", self.run_backtest(task_rows(50)))

    def test_empty_or_all_recent_history_says_so(self):
        self.assertIn("too few to backtest", self.run_backtest([]))
        self.assertIn("too few to backtest", self.run_backtest(task_rows(10)))


class Init(unittest.TestCase):
    def test_writes_a_starter_config_and_never_overwrites(self):
        from cc_estimate.__main__ import main as cli
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cc-estimate.json"
            with contextlib.redirect_stdout(io.StringIO()):
                cli(["init", "--out", str(out)])
            self.assertEqual(json.loads(out.read_text())["default_branch"], "main")
            out.write_text("{}")
            with self.assertRaises(SystemExit):
                cli(["init", "--out", str(out)])
            self.assertEqual(out.read_text(), "{}")


class EveryCommandPrivacy(unittest.TestCase):
    """Every command that runs without network, on a three-person fixture, prints no other login."""

    def test_no_command_prints_another_login(self):
        from cc_estimate.__main__ import main as cli
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, *synthetic(), {"repo": "acme/widgets", "timezone": "Europe/Madrid", "data_dir": "."})
            (d / "widgets.tasks.jsonl").write_text((d / "tasks.jsonl").read_text())
            (d / "h.jsonl").write_text("".join(json.dumps(r) + "\n" for r in history_records()))
            (d / "s.csv").write_text("key,group,start,end\n1,blocked,2026-06-01T10:00:00Z,2026-06-01T12:00:00Z\n")
            cfg = str(d / "cfg.json")
            commands = {
                "tasks": ["tasks", "--prs", str(d / "prs.jsonl"), "--links", str(d / "links.jsonl"), "--config", cfg, "--out", str(d / "t2.jsonl")],
                "spans": ["spans", "--tasks", str(d / "tasks.jsonl"), "--csv", str(d / "s.csv"), "--out", str(d / "t3.jsonl")],
                "backtest": ["backtest", "--in", str(d / "tasks.jsonl"), "--config", cfg],
                "estimate": ["estimate", "--size", "M", "--config", cfg],
                "summary": ["summary", "--in", str(d / "h.jsonl"), "--focus", "3"],
                "git-tasks": ["git-tasks", "--repo-dir", str(Path(__file__).resolve().parents[1]), "--since", "10 years ago",
                              "--out", str(d / "g.jsonl")],
            }
            for name, args in commands.items():
                with self.subTest(command=name):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                        cli(args)
                    for login in LOGINS:
                        self.assertNotIn(login, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
