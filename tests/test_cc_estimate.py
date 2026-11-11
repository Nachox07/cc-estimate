"""Stdlib tests on synthetic data. Run: python3 -m unittest discover -s tests"""
import contextlib, io, json, random, re, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import os, subprocess

from cc_estimate import advise, backtest as bt, estimate, gitsource, links, simulate, spans, tasks

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


def synthetic(n=400, seed=7, size_matters=True):
    """PR records + links for a fake repo: size drives duration, with noise. With `size_matters`
    off, duration comes from an independent draw, so size carries no signal."""
    rnd = random.Random(seed)
    prs, lks = [], []
    start = datetime(2026, 6, 1, 9, tzinfo=UTC)
    for i in range(1, n + 1):
        lines = int(rnd.choice([20, 120, 600, 2500, 8000]) * rnd.uniform(0.6, 1.4))
        created = start + timedelta(hours=i * 7)
        drives = lines if size_matters else int(rnd.choice([20, 120, 600, 2500, 8000]) * rnd.uniform(0.6, 1.4))
        merged = created + timedelta(hours=drives / 40 * rnd.uniform(0.5, 2.0) + rnd.uniform(0, 30))
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
                        end=(s + timedelta(hours=4 + 3 * (i % 5))).isoformat(), lines=lines, code_lines=lines,
                        test_lines=0, doc_lines=0, excluded={}))
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

    def test_a_task_ends_when_its_last_pr_merges(self):
        prs = [dict(number=n, title="feat: a", author="alice", labels=[], base="main", created_at="2026-10-05T09:00:00Z",
                    merged_at=m, first_commit_at=None, size={"lines": {"code": 10, "tests": 0, "docs": 0}}, review={"rounds": 0})
               for n, m in [(1, "2026-10-05T12:00:00Z"), (2, "2026-10-07T15:00:00Z")]]
        lks = [dict(number=n, base="main", closing=[{"number": 50, "excluded": {}}], excluded={}) for n in (1, 2)]
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, prs, lks, {})
            row = json.loads((d / "tasks.jsonl").read_text())
        self.assertEqual(row["end"], "2026-10-07T15:00:00Z")

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


class SizeByCode(unittest.TestCase):
    def load(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            write_tasks(Path(tmp, "t.jsonl"), rows)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                out = bt.load(str(Path(tmp, "t.jsonl")), UTC)
        return out, err.getvalue()

    def row(self, code, tests=0, docs=0, **kw):
        return {**task_rows(1)[0], "lines": code + tests + docs, "code_lines": code, "test_lines": tests, "doc_lines": docs, **kw}

    def test_size_comes_from_code_lines_not_the_total(self):
        rows, _ = self.load([self.row(100, tests=4000, docs=900)])
        self.assertEqual(rows[0]["size"], "S")

    def test_a_task_with_five_times_its_code_in_tests_keeps_its_code_size_and_is_flagged(self):
        rows, _ = self.load([self.row(1000, tests=5000), self.row(1000, tests=900, id=2), self.row(1000, tests=3000, id=3)])
        self.assertEqual([r["size"] for r in rows], ["M", "M", "M"])
        self.assertEqual([r["test_heavy"] for r in rows], [True, False, False])  # exactly 3x is not heavy

    def test_a_file_without_the_new_fields_loads_by_total_lines_and_says_so_once(self):
        strip = lambda r: {k: v for k, v in r.items() if k not in ("code_lines", "test_lines", "doc_lines")}
        old = [strip(r) for r in task_rows(2, lines=700)]
        rows, err = self.load(old)
        self.assertEqual([r["size"] for r in rows], ["M", "M"])
        self.assertFalse(any(r["test_heavy"] for r in rows))
        self.assertEqual(err.count("code_lines"), 1)

    def test_a_current_file_prints_no_note(self):
        self.assertEqual(self.load([self.row(100)])[1], "")

    def test_tasks_carry_the_pr_split_and_keep_lines_as_the_total(self):
        prs = [dict(number=n, title="feat: a", author="alice", labels=[], base="main", created_at="2026-10-05T09:00:00Z",
                    merged_at="2026-10-05T12:00:00Z", first_commit_at=None,
                    size={"lines": {"code": c, "tests": t, "docs": d, "ignored": 999}}, review={"rounds": 0})
               for n, (c, t, d) in [(1, (10, 20, 3)), (2, (5, 1, 0))]]
        lks = [dict(number=n, base="main", closing=[{"number": 50, "excluded": {}}], excluded={}) for n in (1, 2)]
        with tempfile.TemporaryDirectory() as tmp:
            row = json.loads((write(tmp, prs, lks, {}) / "tasks.jsonl").read_text())
        self.assertEqual((row["code_lines"], row["test_lines"], row["doc_lines"], row["lines"]), (15, 21, 3, 39))


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
        self.assertLess(size["winkler"], 0.85 * repo["winkler"])

    def scores(self, seed, size_matters):
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, *synthetic(seed=seed, size_matters=size_matters), {"timezone": "Europe/Madrid"})
            rows = bt.load(str(d / "tasks.jsonl"), MAD, {})
        idx = list(range(200, len(rows)))
        return (bt.score(bt.run(rows, idx, "size", 10, 0.1, 0.9)),
                bt.score(bt.run(rows, idx, "baseline: repo", 10, 0.1, 0.9)))

    def test_size_wins_clearly_and_covers_about_80_percent_across_seeds(self):
        for seed in range(7, 12):
            with self.subTest(seed=seed):
                size, repo = self.scores(seed, size_matters=True)
                self.assertLess(size["winkler"], 0.85 * repo["winkler"])
                self.assertTrue(0.75 <= size["coverage"] <= 0.95, size["coverage"])

    def test_size_does_not_win_when_size_has_no_effect(self):
        for seed in range(7, 12):
            with self.subTest(seed=seed):
                size, repo = self.scores(seed, size_matters=False)
                self.assertGreater(size["winkler"], 0.95 * repo["winkler"])


def scored_row(start, end, y, size="S", author="alice"):
    return dict(id=start, start=start, end=end, y=y, author=author, size=size, kind="feat", epic=None,
                approvals=None, e2e=None, spread=None, approval_set=frozenset())


class ScoringMath(unittest.TestCase):
    """The instruments themselves, checked against numbers worked out by hand."""
    T0 = datetime(2026, 6, 1, tzinfo=UTC)

    def history(self, ys, size="S"):
        return [scored_row(self.T0 + timedelta(days=i), self.T0 + timedelta(days=i, hours=1), y, size)
                for i, y in enumerate(ys)]

    def test_coverage_width_and_winkler_match_a_hand_calculation(self):
        h = self.history(range(1, 11))
        late = scored_row(self.T0 + timedelta(days=30), self.T0 + timedelta(days=31), 20)
        [o] = bt.run(h + [late], [10], "baseline: repo", 5, 0.1, 0.9)
        # 10th and 90th percentiles of 1..10 are 1.9 and 9.1; the actual (20) misses by 10.9;
        # an 80% interval's miss penalty is 2 / 0.2 = 10 per hour missed.
        self.assertFalse(o["inside"])
        self.assertAlmostEqual(o["width"], 7.2)
        self.assertAlmostEqual(o["winkler"], 7.2 + 10 * 10.9)
        inside = scored_row(self.T0 + timedelta(days=30), self.T0 + timedelta(days=31), 5)
        [o] = bt.run(h + [inside], [10], "baseline: repo", 5, 0.1, 0.9)
        self.assertTrue(o["inside"])
        self.assertAlmostEqual(o["winkler"], 7.2)

    def test_a_task_still_open_at_the_start_is_not_history(self):
        done = self.history([1] * 10)
        still_open = [scored_row(self.T0 + timedelta(days=20, hours=i), self.T0 + timedelta(days=90), 500)
                      for i in range(10)]
        t = scored_row(self.T0 + timedelta(days=21), self.T0 + timedelta(days=22), 1)
        rows = done + still_open + [t]
        [o] = bt.run(rows, [len(rows) - 1], "baseline: repo", 5, 0.1, 0.9)
        self.assertEqual(o["width"], 0)

    def test_trailing_baseline_uses_only_the_last_thirty_days(self):
        old = self.history([100] * 10)
        recent = [scored_row(self.T0 + timedelta(days=60 + i), self.T0 + timedelta(days=60 + i, hours=1), 2)
                  for i in range(10)]
        t = scored_row(self.T0 + timedelta(days=75), self.T0 + timedelta(days=76), 2)
        self.assertEqual(set(bt.trailing(old + recent, t)), {2})

    def test_size_buckets_split_at_50_300_1500_and_5000_lines(self):
        self.assertEqual([bt.bucket(n) for n in (0, 49, 50, 299, 300, 1499, 1500, 4999, 5000)],
                         ["XS", "XS", "S", "S", "M", "M", "L", "L", "XL"])

    def test_too_few_matches_falls_back_and_says_so(self):
        h = self.history([1] * 20, size="S")
        t = scored_row(self.T0 + timedelta(days=30), self.T0 + timedelta(days=31), 1, size="XL")
        *_, fell = bt.estimate(h, t, "size", 10, 0.1, 0.9)
        self.assertTrue(fell)
        *_, fell = bt.estimate(h, {**t, "size": "S"}, "size", 10, 0.1, 0.9)
        self.assertFalse(fell)

    def test_pace_moves_little_on_a_few_tasks_and_more_on_many(self):
        others = [scored_row(self.T0 + timedelta(hours=i), self.T0 + timedelta(hours=i, minutes=30), 4, author="bob")
                  for i in range(60)]
        mine = lambda n: [scored_row(self.T0 + timedelta(days=5, hours=i), self.T0 + timedelta(days=5, hours=i, minutes=30),
                                     8, author="alice") for i in range(n)]
        t = scored_row(self.T0 + timedelta(days=9), self.T0 + timedelta(days=10), 1)
        few, many = bt.pace(others + mine(2), t), bt.pace(others + mine(30), t)
        self.assertLess(few, 1.2)   # two slow tasks barely move it off 1.0
        self.assertGreater(many, 1.4)


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

    def test_git_tasks_split_lines_by_path_class_and_leave_out_ignored_files(self):
        with tempfile.TemporaryDirectory() as d:
            git = self.repo(d)
            for path, n in [("src/app.py", 4), ("tests/test_app.py", 9), ("docs/guide.md", 2), ("uv.lock", 50)]:
                Path(d, path).parent.mkdir(exist_ok=True)
                Path(d, path).write_text("x\n" * n)
            git("add", "."); git("commit", "-qm", "ABC-1 work", when="2026-10-05T09:00:00+00:00")
            rows, _ = gitsource.build(gitsource.commits(d, "main", "2020-01-01"), gitsource.key_patterns(None))
        self.assertEqual((rows[0]["code_lines"], rows[0]["test_lines"], rows[0]["doc_lines"], rows[0]["lines"]), (4, 9, 2, 15))

    def test_a_renamed_file_is_classified_by_its_new_path(self):
        self.assertEqual(gitsource.new_path("src/{old => tests}/a.py"), "src/tests/a.py")
        self.assertEqual(gitsource.new_path("a.py => tests/a.py"), "tests/a.py")

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

    def test_the_range_is_the_10th_to_90th_percentile(self):
        t0 = datetime(2026, 1, 5, tzinfo=UTC)
        rows = [scored_row(t0, t0 + timedelta(days=1), y) for y in range(81)]
        newest = [scored_row(t0 + timedelta(days=200), t0 + timedelta(days=200), 0)]  # moves the censor cut-off
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(estimate.bt, "load", return_value=rows + newest):
            sized, n, base, _ = estimate.ranges("t.jsonl", UTC, "S", {})
        self.assertEqual(sized, [8.0, 40.0, 72.0])

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

    def test_a_size_with_too_few_tasks_prints_no_durations(self):
        rows = task_rows(80)
        rows[10] = {**rows[10], "lines": 9000}
        line = next(l for l in self.run_backtest(rows).splitlines() if l.strip().startswith("XL"))
        self.assertNotIn("p50", line)

    def test_a_size_done_by_one_person_prints_no_durations(self):
        rows = task_rows(80)
        for i in range(10, 16):
            rows[i] = {**rows[i], "lines": 9000, "author": "alice"}
        line = next(l for l in self.run_backtest(rows).splitlines() if l.strip().startswith("XL"))
        self.assertNotIn("p50", line)

    def test_split_scales_down_and_keeps_a_locked_slice(self):
        self.assertEqual(bt.split(100, 200), (50, 90))
        self.assertEqual(bt.split(1000, 200), (200, 840))
        self.assertEqual(bt.split(40, 200), (20, 30))
        self.assertIsNone(bt.split(39, 200))

    def test_fifty_finished_tasks_are_enough_to_run(self):
        out = self.run_backtest(task_rows(71))  # the last 21 days are censored, leaving 50
        locked = out.split("== LOCKED · all tasks ==")[1].split("==")[0]
        self.assertIn("baseline: repo", locked)

    def test_methods_needing_classify_output_are_left_out_without_it(self):
        out = self.run_backtest(task_rows(80))
        self.assertNotIn("profile", out)
        self.assertNotIn("approvals+size", out)
        self.assertIn("size+team load", out)

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


class SummaryBuckets(unittest.TestCase):
    def test_a_bucket_too_small_to_hide_one_pr_prints_no_numbers(self):
        from cc_estimate import history
        recs = history_records()
        recs[0] = {**recs[0], "size": {**recs[0]["size"], "lines": {"code": 9000, "tests": 0, "docs": 0}},
                   "time": {"active_days": 17, "weekdays_span": 23}}
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "h.jsonl"
            f.write_text("".join(json.dumps(r) + "\n" for r in recs))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                history.main(["summary", "--in", str(f)])
        row = next(l for l in buf.getvalue().splitlines() if l.startswith("| >5000 | 1 "))
        self.assertNotRegex(row, r"\d+\.\d")
        recs = [{**r, "author": "alice"} if r["size"]["lines"]["code"] < 300 else r for r in history_records()]
        f2 = Path(tempfile.mkdtemp()) / "h.jsonl"
        f2.write_text("".join(json.dumps(r) + "\n" for r in recs))
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            history.main(["summary", "--in", str(f2)])
        one_author = next(l for l in buf2.getvalue().splitlines() if l.startswith("| <300 |"))
        self.assertEqual(one_author.replace(" ", ""), "|<300|7|-|-|")
        phase_row = [l for l in buf.getvalue().splitlines() if l.startswith("| >5000 |")][-1]
        self.assertEqual(phase_row.replace(" ", ""), "|>5000|-|-|-|-|")


class StarterConfig(unittest.TestCase):
    def test_estimate_with_paths_works_on_the_unedited_starter_config(self):
        cfg = json.loads((Path(__file__).resolve().parents[1] / "cc_estimate" / "config.example.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "cfg.json").write_text(json.dumps(cfg))
            write_tasks(d / "t.jsonl", task_rows(80))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                estimate.main(["estimate", "--config", str(d / "cfg.json"), "--tasks", str(d / "t.jsonl"),
                               "--size", "M", "--paths", "src/api/"])
            self.assertIn("approvals: not found", buf.getvalue())
            self.assertIn("test tiers: not found", buf.getvalue())


class EveryCommandPrivacy(unittest.TestCase):
    """Every command that runs without network, on a three-person fixture, prints no other login."""

    def test_no_command_prints_another_login(self):
        from cc_estimate.__main__ import main as cli
        with tempfile.TemporaryDirectory() as tmp:
            d = write(tmp, *synthetic(), {"repo": "acme/widgets", "timezone": "Europe/Madrid", "data_dir": "."})
            (d / "widgets.tasks.jsonl").write_text((d / "tasks.jsonl").read_text())
            (d / "h.jsonl").write_text("".join(json.dumps(r) + "\n" for r in history_records()))
            (d / "plan.jsonl").write_text("".join(json.dumps(t) + "\n" for t in
                [dict(id="a", size="XS", paths=["src/x.py"]), dict(id="b", size="S", paths=["src/y.py"]), dict(id="c", size="XL", paths=["src/lib/z.py", "ui/w.tsx"])]))
            (d / "s.csv").write_text("key,group,start,end\n1,blocked,2026-06-01T10:00:00Z,2026-06-01T12:00:00Z\n")
            cfg = str(d / "cfg.json")
            commands = {
                "tasks": ["tasks", "--prs", str(d / "prs.jsonl"), "--links", str(d / "links.jsonl"), "--config", cfg, "--out", str(d / "t2.jsonl")],
                "spans": ["spans", "--tasks", str(d / "tasks.jsonl"), "--csv", str(d / "s.csv"), "--out", str(d / "t3.jsonl")],
                "backtest": ["backtest", "--in", str(d / "tasks.jsonl"), "--config", cfg],
                "estimate": ["estimate", "--size", "M", "--config", cfg],
                "advise": ["advise", "--size", "M", "--paths", "src/a.py", "--config", cfg, "--tasks", str(d / "tasks.jsonl")],
                "advise plan": ["advise", "--plan", str(d / "plan.jsonl"), "--config", cfg, "--tasks", str(d / "tasks.jsonl")],
                "summary": ["summary", "--in", str(d / "h.jsonl")],
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
                    if name == "summary":  # a PR number leads straight to its author
                        self.assertNotRegex(buf.getvalue(), r"#\d")


if __name__ == "__main__":
    unittest.main()


class SimulatedScenarios(unittest.TestCase):
    """Histories drawn from a stated model, scored on the locked slice (the last tasks, read once)."""
    SEEDS = range(7, 12)

    def rows(self, name, seed):
        generated, truth = simulate.generate(name, seed)
        with tempfile.TemporaryDirectory() as tmp:
            simulate.write_rows(generated, Path(tmp) / "t.jsonl")
            return bt.load(str(Path(tmp) / "t.jsonl"), UTC, {}), truth

    def locked_scores(self, name, seed):
        rows, _ = self.rows(name, seed)
        cut = max(r["end"] for r in rows) - timedelta(days=21)
        rows = [r for r in rows if r["start"] <= cut]
        _, lock = bt.split(len(rows), 200)
        idx = list(range(lock, len(rows)))
        return (bt.score(bt.run(rows, idx, "size", 10, 0.1, 0.9)),
                bt.score(bt.run(rows, idx, "baseline: repo", 10, 0.1, 0.9)))

    def test_same_scenario_and_seed_give_identical_files(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b, \
                contextlib.redirect_stdout(io.StringIO()):
            for d in (a, b):
                simulate.main(["--scenario", "size-matters", "--seed", "3", "--out", d])
            for f in ("tasks.jsonl", "truth.json"):
                self.assertEqual((Path(a) / f).read_text(), (Path(b) / f).read_text())

    def test_truth_states_the_model_and_logins_are_generic(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            simulate.main(["--scenario", "null", "--seed", "7", "--out", tmp])
            truth = json.loads((Path(tmp) / "truth.json").read_text())
            logins = {json.loads(l)["author"] for l in (Path(tmp) / "tasks.jsonl").read_text().splitlines()}
        self.assertEqual((truth["scenario"], truth["seed"]), ("null", 7))
        self.assertIn("model", truth)
        self.assertEqual(logins, set(simulate.LOGINS))

    def test_durations_are_continuous_not_piled_up_at_zero_or_a_full_day(self):
        for name in simulate.SCENARIOS:
            with self.subTest(scenario=name):
                rows, _ = self.rows(name, 7)
                ys = [r["y"] for r in rows]
                self.assertEqual(sum(y < 0.01 for y in ys), 0)
                self.assertGreater(len({round(y, 3) for y in ys}), 0.9 * len(ys))

    def test_size_matters_covers_70_to_90_percent_and_beats_the_baseline_clearly(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                size, repo = self.locked_scores("size-matters", seed)
                self.assertTrue(0.70 <= size["coverage"] <= 0.90, size["coverage"])
                self.assertLess(size["winkler"], 0.85 * repo["winkler"])

    def test_null_world_size_stays_within_5_percent_of_the_baseline(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                size, repo = self.locked_scores("null", seed)
                self.assertLessEqual(abs(size["winkler"] / repo["winkler"] - 1), 0.05)

    def test_test_heavy_tasks_are_sized_by_product_code_and_flagged_alone(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                rows, truth = self.rows("test-heavy", seed)
                heavy = set(truth["params"]["test_heavy_ids"])
                self.assertEqual({r["id"] for r in rows if r["test_heavy"]}, heavy)
                self.assertTrue(all(r["size"] == "M" for r in rows if r["id"] in heavy))
                generated, _ = simulate.generate("test-heavy", seed)
                self.assertTrue(all(bt.bucket(g["lines"]) == "XL" for g in generated if g["id"] in heavy))

    def thin_history(self, seed):
        generated, _ = simulate.generate("thin", seed)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "tasks.jsonl"
        simulate.write_rows(generated, path)
        (Path(tmp.name) / "cfg.json").write_text(json.dumps({"repo": "acme/widgets"}))
        return tmp.name, path

    def run_main(self, fn, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(argv)
        return buf.getvalue()

    def test_thin_history_has_27_small_and_3_large_tasks(self):
        rows, _ = self.rows("thin", 7)
        sizes = [r["size"] for r in rows]
        self.assertEqual((sizes.count("S"), sizes.count("L"), len(sizes)), (27, 3, 30))

    def test_thin_history_is_refused_by_backtest_and_estimated_by_size_only_where_it_has_data(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                tmp, path = self.thin_history(seed)
                self.assertIn("too few to backtest", self.run_main(bt.main, ["--in", str(path)]))
                args = ["estimate", "--config", f"{tmp}/cfg.json", "--tasks", str(path)]
                large = self.run_main(estimate.main, [*args, "--size", "L"])
                self.assertIn("Not enough history for size L", large)
                self.assertIn("Without a size:", large)
                small = self.run_main(estimate.main, [*args, "--size", "S"])
                self.assertIn("80% range:", small)
                self.assertNotIn("Not enough history", small)


T0 = datetime(2024, 1, 1, 9, tzinfo=timezone.utc)


class SizingAdvisor(unittest.TestCase):
    """The advisor on histories with a known break point, on the same seeds as the other scenarios."""
    SEEDS = range(7, 12)

    def world(self, name, seed, parents=None):
        generated, _ = simulate.generate(name, seed)
        if parents:
            generated = parents(generated)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "tasks.jsonl"
        simulate.write_rows(generated, path)
        hist = advise.load_history(str(path), UTC, {})
        return hist, advise.analyse(hist, UTC), simulate.PLANS.get(name, [])

    def verdicts(self, name, seed, plan=None, history=True, **kw):
        hist, an, default_plan = self.world(name, seed, **kw)
        plan = [dict(t) for t in (plan or default_plan)]
        merge = advise.merge_pairs(plan, {})
        return {t["id"]: advise.verdict(t, plan, an if history else None, merge, hist, {}) for t in plan}

    def test_break_point_world_names_xl_and_splits_only_the_xl_task(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                hist, an, plan = self.world("break-point", seed)
                self.assertEqual(an["size_break"], "XL")
                v = self.verdicts("break-point", seed)
                self.assertEqual(v["big"][0], "Split")
                self.assertEqual(v["mid"][0], "Keep")

    def test_slices_cover_every_planned_file_and_each_is_below_the_break(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                _, _, plan = self.world("break-point", seed)
                slices = self.verdicts("break-point", seed)["big"][2]
                self.assertEqual(sorted(p for s in slices for p in s["paths"]), sorted(plan[0]["paths"]))
                self.assertTrue(all(advise.SIZES.index(s["size"]) < advise.SIZES.index("XL") for s in slices))

    def test_slices_follow_dependency_order(self):
        slices = self.verdicts("break-point", 7)["big"][2]
        self.assertEqual([s["layer"] for s in slices], ["shared code", "services", "UI", "tests", "docs"])

    def test_same_relative_spread_at_every_size_shows_no_break(self):
        # size-matters draws one lognormal sigma at every size, so absolute widths grow with the median
        # but nothing breaks.
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                self.assertIsNone(self.world("size-matters", seed)[1]["size_break"])

    def test_small_quick_tasks_with_a_wide_relative_spread_show_no_break(self):
        # medians 0.5-2 h, sigma 1.2 at every size: the shape of a library of quick fixes
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                self.assertIsNone(self.world("small-tasks", seed)[1]["size_break"])

    def test_a_two_times_jump_is_named_at_xl_or_not_at_all(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                self.assertIn(self.world("weak-break", seed)[1]["size_break"], ("XL", None))

    def test_smooth_widths_give_no_advice_for_large_tasks(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                _, an, _ = self.world("no-break", seed)
                self.assertIsNone(an["size_break"])
                for verdict, why, _ in self.verdicts("no-break", seed).values():
                    self.assertEqual(verdict, "No advice")
                    self.assertIn("no size bucket", why)

    def test_thin_history_gives_no_advice_and_says_so(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                # 15 XL tasks find the break only most of the time; the answer is No advice either way
                found = self.world("thin-advisor", seed)[1]["size_break"]
                verdict, why, _ = self.verdicts("thin-advisor", seed)["big"]
                self.assertEqual(verdict, "No advice")
                self.assertIn("too thin" if found else "no size bucket", why)

    def test_only_the_same_area_pair_of_small_tasks_merges(self):
        for seed in self.SEEDS:
            for history in (True, False):
                with self.subTest(seed=seed, history=history):
                    v = self.verdicts("merge-keep", seed, history=history)
                    self.assertEqual({k: x[0] for k, x in v.items()},
                                     {"a": "Merge", "b": "Merge", "c": "Keep", "d": "Keep", "e": "Keep"})

    @staticmethod
    def rows(sigma, scale=10.0, n=60, authors=3, zeros=0.0):
        """Hours with a fixed lognormal shape: the 80% range is the same fraction of `scale` for a given sigma.
        The first `zeros` share of the tasks took no time."""
        import math
        z = [-2 + 4 * i / (n - 1) for i in range(n)]
        return [dict(y=0.0 if i < zeros * n else scale * math.exp(sigma * zi), author=LOGINS[i % authors])
                for i, zi in enumerate(z)]

    def test_a_break_is_a_wider_relative_spread_not_a_longer_task(self):
        for scale, sigma, expect in ((100, 0.3, None), (1000, 0.3, None), (10, 0.9, "S")):
            with self.subTest(scale=scale, sigma=sigma):
                members = {"XS": self.rows(0.3), "S": self.rows(sigma, scale)}
                self.assertEqual(advise.find_break(["XS", "S"], members)[0], expect)

    def test_tiny_durations_and_zero_hour_tasks_do_not_fake_a_break(self):
        # the same sigma at 0.5 h and 5 h, with 30% of tasks at zero hours: the 10th percentile is under
        # the floor in both, and the pair is not compared
        for sigma in (0.5, 1.2):
            with self.subTest(sigma=sigma):
                members = {"XS": self.rows(sigma, 0.5, zeros=0.3), "S": self.rows(sigma, 5.0, zeros=0.3)}
                brk, detail = advise.find_break(["XS", "S"], members)
                self.assertIsNone(brk)
                self.assertTrue(detail[0][6] and detail[1][6])

    def test_the_same_sigma_at_half_an_hour_and_five_hours_is_not_a_break(self):
        members = {"XS": self.rows(0.8, 0.5, zeros=0.3), "S": self.rows(0.8, 5.0, zeros=0.3)}
        self.assertIsNone(advise.find_break(["XS", "S"], members)[0])
        members = {"XS": self.rows(0.5, 0.6), "S": self.rows(0.5, 6.0)}   # p10 just above the floor
        self.assertIsNone(advise.find_break(["XS", "S"], members)[0])

    def test_a_bucket_is_floored_only_when_its_10th_percentile_is_under_the_floor(self):
        # rows(0.5, scale): p10 is about 0.45 x scale
        for scale, expect in ((0.4, True), (0.667, False)):
            with self.subTest(scale=scale):
                ys = [r["y"] for r in self.rows(0.5, scale)]
                self.assertEqual(advise.bt.q(ys, advise.est.LO_P) < advise.FLOOR_H, expect)
                members = {"XS": self.rows(0.5, 5.0), "S": self.rows(0.5, scale)}
                self.assertEqual(bool(advise.find_break(["XS", "S"], members)[1][1][6]), expect)

    def test_a_bucket_with_no_spread_is_not_compared_and_does_not_crash(self):
        flat = [dict(y=1.0, author=LOGINS[i % 3]) for i in range(60)]
        members = {"XS": flat, "S": self.rows(0.8, 5.0), "M": self.rows(0.8, 50.0)}
        brk, detail = advise.find_break(["XS", "S", "M"], members)
        self.assertIsNone(brk)
        self.assertEqual([d[6] for d in detail], ["flat", None, None])
        self.assertIsNone(detail[1][4])
        self.assertIsNotNone(detail[2][4])

    def history(self, groups):
        rows = [dict(r, size=g, areas=None) for g, rs in groups.items() for r in rs]
        return dict(rows=rows, newest=T0, has_areas=False)

    def test_when_no_size_can_be_compared_the_advisor_says_so_instead_of_reporting_no_break(self):
        h = self.history({g: self.rows(0.8, 0.5, zeros=0.3) for g in advise.SIZES})
        an = advise.analyse(h, UTC)
        self.assertEqual(an["compared"], (0, 4))
        self.assertIn("no size could be compared", an["blocker"])
        self.assertNotIn("no size bucket has", an["blocker"])

    def test_a_no_break_verdict_counts_the_comparisons_it_could_not_make(self):
        groups = {g: self.rows(0.8, 5.0) for g in advise.SIZES}
        groups["S"] = self.rows(0.8, 0.5, zeros=0.3)
        an = advise.analyse(self.history(groups), UTC)
        self.assertEqual(an["compared"], (2, 4))
        self.assertIn("(2 of 4 size comparisons could not be made)", an["blocker"])
        an = advise.analyse(self.history({g: self.rows(0.8, 5.0) for g in advise.SIZES}), UTC)
        self.assertNotIn("could not be made", an["blocker"])

    def test_a_floored_bucket_is_marked_in_the_report(self):
        hist = self.history({g: self.rows(0.8, 0.5, zeros=0.3) for g in ("XS", "S")})
        out = []
        advise.report([], hist, advise.analyse(hist, UTC), {}, out.append)
        self.assertIn("floor: not compared", "\n".join(out))

    def test_a_jump_inside_the_bootstrap_interval_is_not_a_break(self):
        # 8 tasks per bucket: a x1.8 spread is plausible noise at that size
        members = {"XS": self.rows(0.3, n=8), "S": self.rows(0.54, n=8)}
        self.assertIsNone(advise.find_break(["XS", "S"], members)[0])
        members = {"XS": self.rows(0.3, n=60), "S": self.rows(0.54, n=60)}
        self.assertEqual(advise.find_break(["XS", "S"], members)[0], "S")

    def test_a_bucket_hidden_for_privacy_is_not_used_to_find_a_break(self):
        members = {"XS": self.rows(0.3), "S": self.rows(0.9, authors=2)}
        self.assertIsNone(advise.find_break(["XS", "S"], members)[0])
        self.assertFalse(advise.find_break(["XS", "S"], members)[1][1][3])

    def test_a_break_by_number_of_areas_is_found_when_areas_are_present(self):
        members = {"1": self.rows(0.3), "2": self.rows(0.35), "3": self.rows(0.9), "4+": self.rows(0.9)}
        self.assertEqual(advise.find_break(advise.AREA_GROUPS, members)[0], "3")

    def test_without_parent_links_the_break_alone_decides_and_the_report_says_splits_were_not_checked(self):
        hist, an, plan = self.world("break-point", 7)
        self.assertEqual(self.verdicts("break-point", 7)["big"][0], "Split")
        out = []
        advise.report([dict(t) for t in plan], hist, an, {}, out.append)
        self.assertIn("Past splits could not be checked: this history has no parent links", "\n".join(out))

    @staticmethod
    def split_rows(hours_each, singles=20.0, lone_parents=0):
        """5 splits of 3 L tasks that ran in parallel (so the wall-clock span is one task's hours), and 6 single XL tasks."""
        def row(code, y, size, parent, h):
            return dict(code_lines=code, y_raw=y, size=size, parent=parent, start=T0, end=T0 + timedelta(hours=h))
        rows = [row(2000, hours_each, "L", k // 3, hours_each) for k in range(15)]
        rows += [row(6000, singles, "XL", None, singles) for _ in range(6)]
        return rows + [row(6000, 100.0, "XL", 900 + k, 100.0) for k in range(lone_parents)]

    def test_past_splits_are_judged_on_the_summed_hours_not_the_span(self):
        # 3 x 9 h of work that ran side by side spans only 9 h, which would beat a 20 h single task
        for hours_each, state in ((5.0, True), (9.0, False)):
            with self.subTest(hours_each=hours_each):
                ok, text = advise.split_evidence(self.split_rows(hours_each), "XL", UTC)
                self.assertIs(ok, state)
                self.assertIn(f"took a median of {3 * hours_each:.1f} work hours in total (wall-clock span {hours_each:.1f})", text)

    def test_splits_must_beat_single_tasks_by_a_fifth(self):
        for hours_each, state in ((5.2, True), (5.5, False)):   # 15.6 h is 22% under 20 h, 16.5 h is 17.5% under
            with self.subTest(hours_each=hours_each):
                self.assertIs(advise.split_evidence(self.split_rows(hours_each), "XL", UTC)[0], state)

    def test_a_parent_with_one_child_counts_as_a_single_task(self):
        ok, text = advise.split_evidence(self.split_rows(5.0, lone_parents=7), "XL", UTC)
        self.assertIs(ok, True)
        self.assertIn("against 100.0 for single", text)

    def test_with_no_parent_links_the_state_is_unchecked(self):
        rows = [dict(r, parent=None) for r in self.split_rows(5.0)]
        self.assertIsNone(advise.split_evidence(rows, "XL", UTC)[0])

    def test_output_names_the_break_the_ordering_rule_and_hides_small_groups(self):
        hist, an, plan = self.world("break-point", 7)
        out = []
        advise.report([dict(t) for t in plan], hist, an, {}, out.append)
        text = "\n".join(out)
        self.assertIn("Break point by size: XL.", text)
        self.assertIn("Ordered by the layer rule", text)
        self.assertIn("No area data", text)
        two = dict(hist, rows=[dict(r, author=LOGINS[i % 2]) for i, r in enumerate(hist["rows"])])
        out = []
        advise.report([dict(t) for t in plan], two, advise.analyse(two, UTC), {}, out.append)
        self.assertIn("too few tasks or people to show", "\n".join(out))
        self.assertNotRegex("\n".join(out), r"median [\d.]+ h, n=")

    def test_advise_cli_reads_a_plan_file_and_prints_a_verdict_per_task(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as buf:
            simulate.main(["--scenario", "break-point", "--seed", "7", "--out", tmp])
            advise.main(["--plan", f"{tmp}/plan.jsonl", "--tasks", f"{tmp}/tasks.jsonl", "--config", f"{tmp}/none.json"])
        self.assertIn("big (XL): Split.", buf.getvalue())
        self.assertIn("mid (L): Keep.", buf.getvalue())


def st_median(xs):
    import statistics
    return statistics.median(xs)
