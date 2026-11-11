"""Stdlib tests on synthetic data. Run: python3 -m unittest discover -s tests"""
import contextlib, io, json, random, re, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import os, subprocess

from cc_estimate import backtest as bt, estimate, gitsource, history, links, simulate, spans, tasks

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
        asked = (created + timedelta(hours=1)).isoformat()
        prs.append(dict(number=i, title=rnd.choice(["feat: x", "fix: y", "chore: z"]), author=author, labels=[],
                        base=base, created_at=created.isoformat(), merged_at=merged.isoformat(),
                        first_commit_at=created.isoformat(), size={"lines": {"code": lines, "tests": 0, "docs": 0}},
                        review={"rounds": 1}, ready_at=(created + timedelta(hours=2)).isoformat(),
                        first_review_at=(created + timedelta(hours=5)).isoformat(),
                        reviewers=[dict(who=f"user:{LOGINS[(i + 1) % 3]}", at=asked), dict(who="team:acme/core", at=asked)]))
        issue = 1000 + (i // 2 if i % 10 == 0 else i)  # every 10th PR shares an issue with the one before
        lks.append(dict(number=i, base=base, closing=[{"number": issue, "excluded": {}}], excluded={}))
    return prs, lks


def synthetic_queue(prs):
    """The all-PRs file for `synthetic`: every PR as merged, plus a few still open."""
    rows = [dict(number=p["number"], state="MERGED", created_at=p["created_at"], ready_at=p["ready_at"],
                 first_review_at=p["first_review_at"], closed_at=p["merged_at"], reviewers=p["reviewers"]) for p in prs]
    late = prs[-1]["created_at"]
    rows += [dict(number=9000 + k, state="OPEN", created_at=late, ready_at=late, first_review_at=None, closed_at=None,
                  reviewers=[dict(who=f"user:{LOGINS[k % 3]}", at=late)]) for k in range(6)]
    return rows


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
              "baseRefName": "trunk", "commits": {"nodes": []}, "labelEvents": {"nodes": []},
              "isDraft": False, "timelineItems": {"nodes": []}}
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
            simulate.write_rows(synthetic_queue(synthetic()[0]), d / "widgets.queue.jsonl")
            (d / "h.jsonl").write_text("".join(json.dumps(r) + "\n" for r in history_records()))
            (d / "s.csv").write_text("key,group,start,end\n1,blocked,2026-06-01T10:00:00Z,2026-06-01T12:00:00Z\n")
            cfg = str(d / "cfg.json")
            commands = {
                "tasks": ["tasks", "--prs", str(d / "prs.jsonl"), "--links", str(d / "links.jsonl"), "--config", cfg, "--out", str(d / "t2.jsonl")],
                "spans": ["spans", "--tasks", str(d / "tasks.jsonl"), "--csv", str(d / "s.csv"), "--out", str(d / "t3.jsonl")],
                "backtest": ["backtest", "--in", str(d / "tasks.jsonl"), "--config", cfg,
                             "--queue", str(d / "widgets.queue.jsonl")],
                "estimate": ["estimate", "--size", "M", "--config", cfg],
                "estimate with queue": ["estimate", "--size", "M", "--queue", "4", "--config", cfg],
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
                    # the reviewer data was really read, not skipped
                    if name == "backtest":
                        self.assertIn("LOCKED · WAIT", buf.getvalue())
                    if name.startswith("estimate"):
                        self.assertIn("Waiting for review", buf.getvalue())
                    self.assertNotIn("team:", buf.getvalue())
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


def pr_node(**kw):
    """A GraphQL PR node as `fetch` receives it, with empty connections."""
    n = dict(id="PR_x", number=1, title="feat: a", url="u", author={"__typename": "User", "login": "alice"},
             baseRefName="main", createdAt="2026-10-05T09:00:00Z", mergedAt="2026-10-07T09:00:00Z", closedAt=None,
             state="MERGED", isDraft=False, additions=1, deletions=1, changedFiles=1, labels={"nodes": []},
             files={"nodes": []}, commits={"nodes": []}, reviews={"nodes": []}, comments={"nodes": []},
             timelineItems={"nodes": []})
    n.update(kw)
    return n


def requested(at, kind, name, org="acme"):
    who = {"__typename": "User", "login": name} if kind == "user" else \
        {"__typename": "Team", "slug": name, "organization": {"login": org}}
    return {"__typename": "ReviewRequestedEvent", "createdAt": at, "requestedReviewer": who}


def review(who, at, state="COMMENTED", typename="User"):
    return {"author": {"__typename": typename, "login": who}, "state": state, "submittedAt": at,
            "commit": {"oid": "c1"}, "comments": {"totalCount": 0}}


class ReviewTiming(unittest.TestCase):
    """What `fetch` and `fetch-queue` keep about review timing, and what `tasks` carries on."""

    def setUp(self):
        self.cfg = history.load_config(None)
        self.cc = history.compile_cfg(self.cfg)

    def timed_node(self, **kw):
        tl = [{"__typename": "ReadyForReviewEvent", "createdAt": "2026-10-05T12:00:00Z"},
              requested("2026-10-05T13:00:00Z", "user", "bob"), requested("2026-10-05T14:00:00Z", "team", "core"),
              requested("2026-10-05T15:00:00Z", "user", "ci[bot]"), requested("2026-10-05T15:30:00Z", "user", "alice"),
              requested("2026-10-05T16:00:00Z", "user", "bob")]
        rv = [review("bob", "2026-10-06T09:00:00Z"), review("carol", "2026-10-06T10:00:00Z", "APPROVED"),
              review("reviewbot", "2026-10-05T12:30:00Z", typename="Bot")]
        return pr_node(timelineItems={"nodes": tl}, reviews={"nodes": rv}, **kw)

    def test_a_pr_record_keeps_the_instants_and_who_was_asked(self):
        rec = history.build_record(self.timed_node(), self.cfg, self.cc)
        self.assertEqual((rec["ready_at"], rec["first_review_at"], rec["merged_at"]),
                         ("2026-10-05T12:00:00+00:00", "2026-10-06T09:00:00+00:00", "2026-10-07T09:00:00Z"))
        # earliest request wins; a team is kept whole; a bot, the author and a bot review are not reviewers;
        # carol reviewed unasked, so her first review is when she became involved
        self.assertEqual([(r["who"], r["at"]) for r in rec["reviewers"]],
                         [("user:bob", "2026-10-05T13:00:00+00:00"), ("team:acme/core", "2026-10-05T14:00:00+00:00"),
                          ("user:carol", "2026-10-06T10:00:00+00:00")])

    def test_each_request_ends_when_that_reviewer_answers_and_a_team_when_any_human_does(self):
        rec = history.build_record(self.timed_node(), self.cfg, self.cc)
        done = {r["who"]: r["done_at"] for r in rec["reviewers"]}
        self.assertEqual(done["user:bob"], "2026-10-06T09:00:00+00:00")
        self.assertEqual(done["team:acme/core"], "2026-10-06T09:00:00+00:00")
        self.assertEqual(done["user:carol"], "2026-10-06T10:00:00+00:00")  # unasked: starts and ends at the review

    def test_a_request_after_a_review_starts_a_new_entry_and_one_while_open_adds_nothing(self):
        tl = [requested("2026-10-05T10:00:00Z", "user", "bob"), requested("2026-10-05T10:30:00Z", "user", "bob"),
              requested("2026-10-06T10:00:00Z", "user", "bob")]
        node = pr_node(timelineItems={"nodes": tl}, reviews={"nodes": [review("bob", "2026-10-05T12:00:00Z")]})
        rec = history.build_record(node, self.cfg, self.cc)
        self.assertEqual([(r["at"], r["done_at"]) for r in rec["reviewers"]],
                         [("2026-10-05T10:00:00+00:00", "2026-10-05T12:00:00+00:00"), ("2026-10-06T10:00:00+00:00", None)])

    def test_a_pr_opened_ready_and_later_turned_into_a_draft_was_ready_when_opened(self):
        drafted = {"__typename": "ConvertToDraftEvent", "createdAt": "2026-10-05T11:00:00Z"}
        ready = {"__typename": "ReadyForReviewEvent", "createdAt": "2026-10-05T13:00:00Z"}
        for tl in ([drafted], [drafted, ready]):
            node = pr_node(state="OPEN", isDraft=True, mergedAt=None, timelineItems={"nodes": tl})
            self.assertEqual(history.build_queue_record(node, self.cfg, self.cc)["ready_at"], "2026-10-05T09:00:00+00:00")
        node = pr_node(state="OPEN", isDraft=True, mergedAt=None, timelineItems={"nodes": [ready]})
        self.assertEqual(history.build_queue_record(node, self.cfg, self.cc)["ready_at"], "2026-10-05T13:00:00+00:00")

    def test_fetch_queue_completes_connections_on_a_pool_of_workers_and_writes_every_pr(self):
        nodes = {n: pr_node(number=n, state="OPEN", mergedAt=None) for n in (3, 1, 2)}
        a = mock.Mock(repo="o/r", days=10, since="2026-10-01", until="2026-10-09", config=None, workers=3)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(history, "search_window", return_value=(nodes, [])), \
                mock.patch.object(history, "complete_connections", side_effect=lambda n, *_: n) as cc:
            a.out = f"{tmp}/q.jsonl"
            history.cmd_fetch_queue(a)
            got = [json.loads(l)["number"] for l in Path(a.out).read_text().splitlines()]
        self.assertEqual(got, [1, 2, 3])
        self.assertEqual(cc.call_count, 3)

    def test_a_pr_never_marked_ready_is_ready_when_opened(self):
        rec = history.build_record(pr_node(), self.cfg, self.cc)
        self.assertEqual((rec["ready_at"], rec["first_review_at"], rec["reviewers"]), ("2026-10-05T09:00:00+00:00", None, []))

    def test_review_requests_are_read_from_the_timeline_and_paged_with_it(self):
        self.assertIn("REVIEW_REQUESTED_EVENT", history.CONN["timelineItems"])
        self.assertIn("ReviewRequestedEvent", history.continuation_q("timelineItems"))

    def test_the_queue_record_covers_open_closed_and_draft_prs_and_keeps_no_author(self):
        draft = pr_node(state="OPEN", isDraft=True, mergedAt=None)
        open_ = pr_node(state="OPEN", mergedAt=None, timelineItems={"nodes": [requested("2026-10-05T10:00:00Z", "team", "core")]})
        closed = pr_node(state="CLOSED", mergedAt=None, closedAt="2026-10-06T09:00:00Z")
        recs = [history.build_queue_record(n, self.cfg, self.cc) for n in (draft, open_, closed)]
        self.assertEqual([r["ready_at"] for r in recs], [None, "2026-10-05T09:00:00+00:00", "2026-10-05T09:00:00+00:00"])
        self.assertEqual([(r["state"], r["closed_at"]) for r in recs],
                         [("OPEN", None), ("OPEN", None), ("CLOSED", "2026-10-06T09:00:00Z")])
        self.assertEqual(recs[1]["reviewers"][0]["who"], "team:acme/core")
        self.assertNotIn("alice", json.dumps(recs))

    def test_a_bot_opened_pr_is_not_in_the_queue_file(self):
        bot = pr_node(author={"__typename": "Bot", "login": "dep[bot]"})
        self.assertIsNone(history.build_queue_record(bot, self.cfg, self.cc))

    def test_the_queue_search_is_by_created_date_and_not_only_merged(self):
        calls = []

        def fake(query, variables, name, ttl_s=None):
            calls.append(variables["q"])
            if name == "count":
                return {"data": {"search": {"issueCount": 1}}}
            return {"data": {"search": {"nodes": [pr_node(number=7)], "pageInfo": {"hasNextPage": False}}}}
        with mock.patch.object(history, "gh_graphql", fake), contextlib.redirect_stderr(io.StringIO()):
            nodes, _ = history.search_window("o/r", datetime(2026, 10, 1).date(), datetime(2026, 10, 9).date(), 20, 3600, queue=True)
        self.assertEqual(set(nodes), {7})
        self.assertTrue(all("created:2026-10-01..2026-10-09" in q and "is:merged" not in q for q in calls))

    def test_tasks_carry_the_earliest_ready_instant_and_every_reviewer_once(self):
        prs = [dict(number=n, title="feat: a", author="alice", labels=[], base="main", created_at="2026-10-05T09:00:00Z",
                    merged_at="2026-10-08T12:00:00Z", first_commit_at=None, size={"lines": {"code": 1, "tests": 0, "docs": 0}},
                    review={"rounds": 0}, ready_at=ready, first_review_at=first,
                    reviewers=[dict(who="user:bob", at=at), dict(who=f"user:only{n}", at=at)])
               for n, ready, first, at in [(1, "2026-10-06T10:00:00+00:00", "2026-10-07T10:00:00+00:00", "2026-10-06T11:00:00+00:00"),
                                           (2, "2026-10-05T14:00:00Z", "2026-10-06T10:00:00+00:00", "2026-10-05T15:00:00Z")]]
        lks = [dict(number=n, base="main", closing=[{"number": 50, "excluded": {}}], excluded={}) for n in (1, 2)]
        with tempfile.TemporaryDirectory() as tmp:
            row = json.loads((write(tmp, prs, lks, {}) / "tasks.jsonl").read_text())
        self.assertEqual((row["ready_at"], row["first_review_at"]), ("2026-10-05T14:00:00Z", "2026-10-06T10:00:00+00:00"))
        self.assertEqual({(r["who"], r["at"]) for r in row["reviewers"]},
                         {("user:bob", "2026-10-05T15:00:00Z"), ("user:only1", "2026-10-06T11:00:00+00:00"),
                          ("user:only2", "2026-10-05T15:00:00Z")})

    def test_records_from_before_review_timing_still_make_tasks_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs, lks = synthetic(30)
            for p in prs:
                for k in ("ready_at", "first_review_at", "reviewers"):
                    p.pop(k)
            d = write(tmp, prs, lks, {})
            row = json.loads((d / "tasks.jsonl").read_text().splitlines()[0])
            loaded = bt.load(str(d / "tasks.jsonl"), UTC, {})
        self.assertEqual((row["ready_at"], row["reviewers"]), (None, []))
        self.assertTrue(all(r["coding_y"] is None and r["wait_y"] is None and r["queue"] is None for r in loaded))


def at(hhmm, day="2026-10-05"):
    return f"{day}T{hhmm if hhmm.count(':') == 2 else hhmm + ':00'}+00:00"


def req(who, hhmm):
    return dict(who=who, at=at(hhmm))


def queue_pr(number, ready, closed=None, reviewers=(), state=None):
    return dict(number=number, state=state or ("OPEN" if closed is None else "MERGED"),
                ready_at=at(ready) if ready else None, closed_at=at(closed) if closed else None,
                first_review_at=None, reviewers=list(reviewers))


class SplitTimes(unittest.TestCase):
    def load_one(self, **kw):
        row = dict(id=1, title="feat: a", author="alice", labels=[], start=at("09:00"), end=at("12:00", "2026-10-06"),
                   lines=10, code_lines=10, test_lines=0, doc_lines=0, excluded={}, ready_at=at("15:00"))
        row.update(kw)
        with tempfile.TemporaryDirectory() as tmp:
            write_tasks(Path(tmp) / "t.jsonl", [row])
            return bt.load(str(Path(tmp) / "t.jsonl"), UTC, {})[0]

    def test_working_hours_split_at_ready(self):
        r = self.load_one()
        self.assertAlmostEqual(r["coding_y"], 6)       # Monday 09:00-15:00
        self.assertAlmostEqual(r["wait_y"], 6)         # Monday 15:00-18:00, Tuesday 09:00-12:00
        self.assertAlmostEqual(r["coding_y"] + r["wait_y"], r["y"])

    def test_a_blocked_span_comes_off_coding_only(self):
        r = self.load_one(excluded={"blocked": [[at("14:00"), at("16:00")]]})
        self.assertAlmostEqual(r["coding_y"], 5)       # 14:00-15:00 is blocked
        self.assertAlmostEqual(r["wait_y"], 6)         # 15:00-16:00 blocked, but waiting ignores blocked spans
        self.assertAlmostEqual(r["y"], 10)

    def test_the_authors_absence_over_the_wait_leaves_wait_unchanged_and_shortens_coding(self):
        cfg = {"absence": {"alice": ["2026-10-06", "2026-10-05"]}}
        plain = self.load_one()
        with tempfile.TemporaryDirectory() as tmp:
            write_tasks(Path(tmp) / "t.jsonl", [dict(id=1, title="feat: a", author="alice", labels=[], start=at("09:00"),
                        end=at("12:00", "2026-10-06"), lines=1, code_lines=1, test_lines=0, doc_lines=0, excluded={},
                        ready_at=at("15:00"))])
            r = bt.load(str(Path(tmp) / "t.jsonl"), UTC, cfg)[0]
        self.assertAlmostEqual(r["wait_y"], plain["wait_y"])   # Tuesday is alice's day off, not the reviewers'
        self.assertEqual(r["coding_y"], 0)                      # Monday was too
        self.assertEqual(r["y"], 0)

    def test_a_public_holiday_comes_off_the_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tasks(Path(tmp) / "t.jsonl", [dict(id=1, title="feat: a", author="alice", labels=[], start=at("09:00"),
                        end=at("12:00", "2026-10-06"), lines=1, code_lines=1, test_lines=0, doc_lines=0, excluded={},
                        ready_at=at("15:00"))])
            r = bt.load(str(Path(tmp) / "t.jsonl"), UTC, {"absence": {"*": ["2026-10-06"]}})[0]
        self.assertAlmostEqual(r["wait_y"], 3)

    def test_a_ready_instant_outside_the_task_is_pulled_inside(self):
        late = self.load_one(ready_at=at("09:00", "2026-10-09"))
        self.assertEqual((late["wait_y"], round(late["coding_y"], 6)), (0, round(late["y"], 6)))
        early = self.load_one(ready_at=at("08:00", "2026-10-02"))
        self.assertEqual((early["coding_y"], round(early["wait_y"], 6)), (0, round(early["y"], 6)))

    def test_a_task_without_ready_at_has_no_split(self):
        r = self.load_one(ready_at=None)
        self.assertEqual((r["coding_y"], r["wait_y"]), (None, None))


class ReviewerQueue(unittest.TestCase):
    """The queue a task faced when it became ready, using only what was known at that instant.
    Ready at 12:00 on Monday; the task asks team X and user u1 (and, at 15:00, user u9)."""

    def task(self):
        return dict(id=1, title="feat: a", author="alice", labels=[], start=at("09:00"), end=at("12:00", "2026-10-06"),
                    lines=10, code_lines=10, test_lines=0, doc_lines=0, excluded={}, prs=[100], ready_at=at("12:00"),
                    first_review_at=at("16:00"),
                    reviewers=[req("team:x", "11:00"), req("user:u1", "11:00"), req("user:u9", "15:00")])

    def pool(self):
        return [
            queue_pr(100, "12:00", reviewers=[req("team:x", "11:00")]),             # the task's own PR
            queue_pr(1, "10:00", reviewers=[req("team:x", "10:30")]),               # waits on X: counts
            queue_pr(2, "10:00", closed="11:59", reviewers=[req("team:x", "10:00")]),  # merged before: no
            queue_pr(3, "10:00", closed="13:00", reviewers=[req("user:u1", "10:00")]),  # still waiting at 12:00: counts
            queue_pr(4, "10:00", reviewers=[req("team:y", "10:00")]),               # other team: repo only
            queue_pr(5, "12:30", reviewers=[req("team:x", "12:00")]),               # not ready yet: no
            queue_pr(6, "09:00", reviewers=[req("team:x", "12:30")]),               # asked X only after 12:00: repo only
            queue_pr(7, None, reviewers=[req("team:x", "10:00")]),                  # draft: no
            queue_pr(8, "12:00", reviewers=[req("team:x", "11:59")]),               # ready exactly now: counts
            queue_pr(9, "08:00", closed="12:00", reviewers=[req("team:x", "08:00")]),  # closed exactly now: no
            queue_pr(10, "10:00", reviewers=[req("user:u9", "10:00")]),             # u9 was asked on the task later: repo only
        ]

    def queue(self, task, pool):
        with tempfile.TemporaryDirectory() as tmp:
            write_tasks(Path(tmp) / "t.jsonl", [task])
            simulate.write_rows(pool, Path(tmp) / "q.jsonl")
            r = bt.load(str(Path(tmp) / "t.jsonl"), UTC, {}, None, bt.load_queue(str(Path(tmp) / "q.jsonl")))[0]
        return r["queue"], r["queue_repo"]

    def test_counts_match_a_hand_count(self):
        self.assertEqual(self.queue(self.task(), self.pool()), (3, 6))  # {1, 3, 8} on X or u1; {1, 3, 4, 6, 8, 10} anywhere

    def test_with_no_reviewer_known_by_ready_the_repo_wide_count_stands_in(self):
        t = self.task()
        t["reviewers"] = [req("user:u9", "15:00")]
        self.assertEqual(self.queue(t, self.pool()), (6, 6))

    def test_nothing_after_ready_moves_the_queue(self):
        before = self.queue(self.task(), self.pool())
        t, pool = self.task(), self.pool()
        t["first_review_at"] = at("23:00")
        t["end"] = at("12:00", "2026-10-09")
        t["reviewers"] += [req("user:u10", "16:00"), req("team:z", "13:00")]  # asked after ready
        by_number = {p["number"]: p for p in pool}
        by_number[4]["reviewers"].append(req("team:x", "14:00"))              # a request made after ready
        by_number[4]["reviewers"].append(req("user:u1", "12:06"))
        by_number[10]["reviewers"].append(req("user:u1", "16:00"))
        by_number[6]["reviewers"] = [req("team:x", "23:00")]                  # a later request moves later still
        by_number[3]["closed_at"] = at("20:00")                               # merges after ready, at another time
        by_number[1]["closed_at"] = at("12:01")
        by_number[1]["state"] = "MERGED"
        by_number[8]["first_review_at"] = at("12:30")                         # reviews after ready
        by_number[1]["reviewers"][0]["done_at"] = at("13:00")                 # X answers after ready: still waiting at ready
        pool += [queue_pr(20, "12:01", reviewers=[req("team:x", "11:00")]),   # opened and readied after ready
                 queue_pr(21, "13:00", closed="14:00", reviewers=[req("user:u1", "12:00")])]
        self.assertEqual(self.queue(t, pool), before)

    def test_the_test_would_notice_a_change_made_before_ready(self):
        pool = self.pool()
        pool[4]["reviewers"].append(req("team:x", "11:59"))
        self.assertEqual(self.queue(self.task(), pool), (4, 6))

    def test_requests_a_second_after_ready_count_as_made_at_ready_on_both_sides(self):
        t = self.task()
        t["reviewers"] = [req("team:x", "12:00:01")]       # CODEOWNERS asks about a second after the PR opens
        pool = [queue_pr(1, "10:00", reviewers=[req("team:x", "12:00:01")]), queue_pr(2, "10:00", reviewers=[req("team:y", "10:00")])]
        self.assertEqual(self.queue(t, pool), (1, 2))      # known reviewer, and PR 1 is on it; not the repo-wide fallback
        t["reviewers"] = [req("team:x", "12:06")]          # more than five minutes later is not "at ready"
        self.assertEqual(self.queue(t, pool), (2, 2))
        pool[0]["reviewers"] = [req("team:x", "12:06")]
        t["reviewers"] = [req("team:x", "11:00")]
        self.assertEqual(self.queue(t, pool), (0, 2))

    def test_the_queue_is_githubs_review_requested_and_matches_the_skills_search(self):
        def entry(who, a, done=None):
            return dict(who=who, at=at(a), done_at=at(done) if done else None)
        pool = [
            queue_pr(1, "10:00", reviewers=[entry("user:x", "10:00", "11:00")]),                    # X already reviewed
            queue_pr(2, "10:00", reviewers=[entry("user:x", "10:00")]),                              # waiting on X
            queue_pr(3, "10:00", closed="11:30", reviewers=[entry("user:x", "10:00")]),             # merged
            queue_pr(4, None, reviewers=[entry("user:x", "10:00")]),                                # draft
            queue_pr(5, "10:00", reviewers=[entry("team:t", "10:00")]),                              # waiting on the team
            queue_pr(6, "10:00", reviewers=[entry("team:t", "09:00", "11:00")]),                    # a member answered
            queue_pr(7, "10:00", reviewers=[entry("user:x", "10:00", "11:00"), entry("user:x", "11:30")]),  # asked again
            queue_pr(8, "10:00", reviewers=[entry("user:x", "10:00"), entry("team:t", "10:00")]),   # both
        ]

        def gh_search(reviewer, now):
            """`gh search prs --state open --draft=false --review-requested <reviewer>` at `now`, from the raw rows."""
            t = lambda x: datetime.fromisoformat(x)
            return {p["number"] for p in pool if p["ready_at"] and t(p["ready_at"]) <= now
                    and not (p["closed_at"] and t(p["closed_at"]) <= now)
                    and any(e["who"] == reviewer and t(e["at"]) <= now and not (e["done_at"] and t(e["done_at"]) <= now)
                            for e in p["reviewers"])}
        now = datetime.fromisoformat(at("12:00"))
        self.assertEqual(gh_search("user:x", now), {2, 7, 8})
        self.assertEqual(gh_search("team:t", now), {5, 8})
        for asked, expect in ((["user:x"], {2, 7, 8}), (["team:t"], {5, 8}), (["user:x", "team:t"], {2, 5, 7, 8})):
            t = self.task()
            t["reviewers"] = [req(w, "11:00") for w in asked]
            union = set().union(*(gh_search(w, now) for w in asked))
            self.assertEqual(union, expect)
            self.assertEqual(self.queue(t, pool)[0], len(expect))

    def test_a_task_does_not_count_its_own_pr(self):
        pool = [queue_pr(100, "12:00", reviewers=[req("team:x", "11:00")])]
        self.assertEqual(self.queue(self.task(), pool), (0, 0))

    def test_terciles_are_fitted_on_the_tuning_slice_only(self):
        rows = [dict(queue=v) for v in (1, 2, 3, 4, 5, 6)] + [dict(queue=100)] * 6 + [dict(queue=None)]
        for r in rows:
            r.update(load_all=0, load_group=0)
        bt.bucket_load(rows, rows[:6])
        self.assertEqual([r["queue_b"] for r in rows[:6]], ["low", "low", "mid", "mid", "high", "high"])
        self.assertEqual((rows[6]["queue_b"], rows[-1]["queue_b"]), ("high", None))


class KaplanMeier(unittest.TestCase):
    # Waits 1, 2, 3, 4 h finished; one PR still open after 2.5 h.
    OBS = [(1, True), (2, True), (2.5, False), (3, True), (4, True)]

    def test_quantiles_match_a_hand_calculation(self):
        # Survival: 5 at risk, one ends at 1 -> 4/5 = 0.8; 4 at risk, one ends at 2 -> 0.8 * 3/4 = 0.6;
        # the open PR leaves at 2.5, leaving 2 at risk; one ends at 3 -> 0.6 * 1/2 = 0.3; the last ends at 4 -> 0.
        self.assertEqual(bt.km_quantile(self.OBS, 0.1), (1, True))   # first time survival <= 0.9
        self.assertEqual(bt.km_quantile(self.OBS, 0.5), (3, True))   # first time survival <= 0.5
        self.assertEqual(bt.km_quantile(self.OBS, 0.9), (4, True))   # first time survival <= 0.1

    def test_dropping_the_open_pr_gives_a_shorter_wait(self):
        finished = [o for o in self.OBS if o[1]]
        self.assertEqual(bt.km_quantile(finished, 0.5), (2, True))   # 0.75, then 0.5 at 2

    def test_a_curve_that_never_falls_far_enough_reports_a_floor(self):
        self.assertEqual(bt.km_quantile([(1, True), (5, False)], 0.9), (5, False))

    def test_a_finish_and_an_open_pr_at_the_same_time_count_the_finish_first(self):
        # 3 at risk at t=2, one finishes: 0.667, so the median is 3. Counting the open PR out first would give 2 at risk and 0.5.
        self.assertEqual(bt.km_quantile([(2, False), (2, True), (3, True)], 0.5), (3, True))


class WaitingRanges(unittest.TestCase):
    """`estimate` splits the range into coding and waiting once the history carries ready_at."""

    def weekdays(self, n):
        d, out = datetime(2026, 3, 2, tzinfo=UTC), []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def history(self, tmp, waits, open_ready=(), abandoned=0, old_open=0):
        """One task per weekday, coding 1 h then waiting `waits[i]` h, each on its own day; `open_ready`
        are PRs still open that became ready on those weekday indexes."""
        days = self.weekdays(len(waits) + 40)
        rows, pool = [], []
        for i, w in enumerate(waits):
            s = days[i].replace(hour=9)
            ready, end = s + timedelta(hours=1), s + timedelta(hours=1 + w)
            rows.append(dict(id=i + 1, title="feat: x", author=LOGINS[i % 3], labels=[], start=s.isoformat(),
                             end=end.isoformat(), lines=100, code_lines=100, test_lines=0, doc_lines=0, excluded={},
                             prs=[i + 1], ready_at=ready.isoformat(), reviewers=[]))
            pool.append(dict(number=i + 1, state="MERGED", ready_at=ready.isoformat(), closed_at=end.isoformat(),
                             first_review_at=None, reviewers=[]))
        for k, d in enumerate(open_ready):
            pool.append(dict(number=1000 + k, state="OPEN", ready_at=days[d].replace(hour=10).isoformat(),
                             closed_at=None, first_review_at=None, reviewers=[]))
        for k in range(abandoned):  # closed unmerged after weeks: the slow ones
            pool.append(dict(number=2000 + k, state="CLOSED", ready_at=days[3].replace(hour=10).isoformat(),
                             closed_at=days[30].replace(hour=10).isoformat(), first_review_at=None, reviewers=[]))
        for k in range(old_open):  # still open, but ready before the tasks file begins
            pool.append(dict(number=3000 + k, state="OPEN", ready_at="2026-02-16T10:00:00+00:00", closed_at=None,
                             first_review_at=None, reviewers=[]))
        write_tasks(Path(tmp) / "r.tasks.jsonl", rows)
        simulate.write_rows(pool, Path(tmp) / "r.queue.jsonl")
        return str(Path(tmp) / "r.tasks.jsonl")

    def lines(self, tasks, **kw):
        return estimate.split_lines(tasks, UTC, "S", {}, **kw)

    def test_prs_still_open_stretch_the_wait_range(self):
        waits = [1 + i % 4 for i in range(40)]  # 1, 2, 3, 4 hours, ten of each
        with tempfile.TemporaryDirectory() as tmp:
            closed_only = self.lines(self.history(tmp, waits))[-2]
            with_open = self.lines(self.history(tmp, waits, open_ready=range(5, 15)))[-2]
        # 40 finished: survival is 0.75, 0.5, 0.25, 0 at 1-4 h, so the median is 2 h and the top is 4 h.
        self.assertIn("median 2.0 h", closed_only)
        self.assertNotIn("+", closed_only)
        # ten PRs open far longer than 4 h stay in the count: 0.8, 0.6, 0.4, 0.2 at 1-4 h, then it never reaches 0.1.
        self.assertIn("median 3.0 h", with_open)
        self.assertIn("+ work hours", with_open)

    def test_prs_closed_unmerged_are_left_out_even_when_they_are_the_slow_ones(self):
        waits = [1 + i % 4 for i in range(40)]
        with tempfile.TemporaryDirectory() as tmp:
            line = self.lines(self.history(tmp, waits, abandoned=10))[-2]
        self.assertIn("median 2.0 h", line)
        self.assertNotIn("+", line)
        self.assertIn("40 waits, 0 still open", line)

    def test_prs_open_since_before_the_tasks_file_begins_do_not_stretch_the_wait(self):
        waits = [1 + i % 4 for i in range(40)]
        with tempfile.TemporaryDirectory() as tmp:
            line = self.lines(self.history(tmp, waits, old_open=10))[-2]
        self.assertIn("40 waits, 0 still open", line)
        self.assertIn("median 2.0 h", line)

    def test_a_logged_estimate_carries_the_coding_and_wait_ranges_and_score_reports_their_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_path = self.history(tmp, [1 + i % 4 for i in range(40)])
            (Path(tmp) / "cfg.json").write_text(json.dumps({"repo": "acme/r", "data_dir": "."}))
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(estimate, "me", return_value="alice"):
                estimate.main(["estimate", "--config", f"{tmp}/cfg.json", "--tasks", tasks_path, "--size", "S",
                               "--queue", "2", "--text", "x", "--log"])
            [rec] = [json.loads(l) for l in (Path(tmp) / "estimates.jsonl").read_text().splitlines()]
            self.assertEqual(len(rec["coding_h"]), 3)
            self.assertEqual(len(rec["wait_h"]), 3)
            self.assertEqual(rec["queue"], 2)
            rec.update(actual={"hours": 3.0, "coding_hours": rec["coding_h"][1], "wait_hours": rec["wait_h"][2] + 50},
                       author="alice", range_h=[1, 2, 9], baseline_h=[1, 2, 9], issue=7)
            (Path(tmp) / "estimates.jsonl").write_text(json.dumps(rec) + "\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), mock.patch.object(estimate, "me", return_value="alice"):
                estimate.main(["score", "--config", f"{tmp}/cfg.json"])
        self.assertIn("Coding inside its range: 1/1", buf.getvalue())
        self.assertIn("Waiting inside its range: 0/1", buf.getvalue())

    def test_the_actual_coding_and_wait_come_from_the_ready_instant(self):
        pr = {"number": 5, "mergedAt": "2026-10-06T12:00:00Z", "createdAt": "2026-10-05T09:00:00Z", "isDraft": False,
              "baseRefName": "main", "commits": {"nodes": []}, "labelEvents": {"nodes": []},
              "timelineItems": {"nodes": [{"__typename": "ConvertToDraftEvent", "createdAt": "2026-10-05T11:00:00Z"},
                                          {"__typename": "ReadyForReviewEvent", "createdAt": "2026-10-05T15:00:00Z"}]}}
        # opened ready, turned into a draft, readied again: ready from the start, so no coding time
        with mock.patch.object(estimate, "gql", return_value={"pullRequest": pr}):
            got = estimate.actual_for({"repo": "a/b", "pr": 5, "author": "alice"}, UTC, {})
        self.assertEqual((got["coding_hours"], got["wait_hours"], got["hours"]), (0.0, 12.0, 12.0))

    def test_no_queue_flag_prints_the_unconditioned_range_and_a_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.lines(self.history(tmp, [2] * 40))
        self.assertTrue(out[0].startswith("Coding, size S:") or out[0].startswith("Coding, any size:"))
        self.assertTrue(out[1].startswith("Waiting for review, any queue:"))
        self.assertIn("--queue N", out[2])

    def test_a_history_without_ready_at_prints_no_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tasks(Path(tmp) / "x.tasks.jsonl", task_rows(80))
            self.assertEqual(self.lines(str(Path(tmp) / "x.tasks.jsonl")), [])

    def test_queue_flag_without_a_queue_file_says_so_and_still_gives_a_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks_path = self.history(tmp, [2] * 40)
            (Path(tmp) / "r.queue.jsonl").unlink()
            out = self.lines(tasks_path, queue_n=5)
        self.assertIn("No queue file", out[1])
        self.assertTrue(out[2].startswith("Waiting for review, any queue:"))


class SimulatedWait(unittest.TestCase):
    """The wait-dominates world: coding follows size, waiting follows the reviewer queue, and the total
    is mostly waiting. Scored on the locked slice with the real loader and queue code."""
    SEEDS = range(7, 12)

    def tables(self, seed):
        rows, _, queue = simulate.generate_all("wait-dominates", seed)
        with tempfile.TemporaryDirectory() as tmp:
            simulate.write_rows(rows, Path(tmp) / "t.jsonl")
            simulate.write_rows(queue, Path(tmp) / "q.jsonl")
            rows = bt.load(str(Path(tmp) / "t.jsonl"), UTC, {}, None, bt.load_queue(str(Path(tmp) / "q.jsonl")))
        cut = max(r["end"] for r in rows) - timedelta(days=21)
        rows = [r for r in rows if r["start"] <= cut]
        _, lock = bt.split(len(rows), 200)
        bt.bucket_load(rows, rows[:lock])
        idx = list(range(lock, len(rows)))
        out = {}
        for title, key, names in (("TOTAL", "y", ("size", "baseline: repo")), ("CODING", "coding_y", ("size", "baseline: repo")),
                                  ("WAIT", "wait_y", ("size", "queue", "size+queue", "baseline: repo"))):
            view = [dict(r, y=r[key]) for r in rows]
            out[title] = {n: bt.score(bt.run(view, idx, n, 10, 0.1, 0.9)) for n in names}
        return out

    def test_coding_follows_size(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                size, repo = (self.tables(seed)["CODING"][k] for k in ("size", "baseline: repo"))
                self.assertLess(size["winkler"], 0.85 * repo["winkler"])
                self.assertTrue(0.70 <= size["coverage"] <= 0.90, size["coverage"])

    def test_waiting_follows_the_queue_and_not_size(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                w = self.tables(seed)["WAIT"]
                self.assertLess(w["queue"]["winkler"], 0.85 * w["baseline: repo"]["winkler"])
                self.assertGreater(w["size"]["winkler"], 0.95 * w["baseline: repo"]["winkler"])

    def test_total_cycle_time_is_mostly_waiting_so_size_barely_helps(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                t = self.tables(seed)["TOTAL"]
                self.assertGreater(t["size"]["winkler"], 0.85 * t["baseline: repo"]["winkler"])

    def test_backtest_prints_the_coding_and_wait_tables(self):
        rows, _, queue = simulate.generate_all("wait-dominates", 7, 400)
        with tempfile.TemporaryDirectory() as tmp:
            simulate.write_rows(rows, Path(tmp) / "t.jsonl")
            simulate.write_rows(queue, Path(tmp) / "q.jsonl")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bt.main(["--in", str(Path(tmp) / "t.jsonl"), "--queue", str(Path(tmp) / "q.jsonl")])
            no_queue = io.StringIO()
            with contextlib.redirect_stdout(no_queue):
                bt.main(["--in", str(Path(tmp) / "t.jsonl")])
        out = buf.getvalue()
        coding = out.split("== LOCKED · CODING ==")[1].split("==")[0]
        wait = out.split("== LOCKED · WAIT ==")[1].split("==")[0]
        self.assertNotIn("queue", coding)
        for method in ("baseline: repo", "baseline: same author", "size", "queue", "size+queue"):
            self.assertIn(method, wait)
        self.assertIn("== LOCKED · all tasks ==", out)  # the total tables stay
        self.assertNotIn("size+queue", no_queue.getvalue())
        self.assertIn("== LOCKED · WAIT ==", no_queue.getvalue())

    def test_the_km_wait_at_a_start_knows_nothing_after_it(self):
        _, _, queue = simulate.generate_all("wait-dominates", 7, 300)
        with tempfile.TemporaryDirectory() as tmp:
            simulate.write_rows(queue, Path(tmp) / "q.jsonl")
            pool = bt.load_queue(str(Path(tmp) / "q.jsonl"))
        pop = bt.wait_population(pool, UTC, frozenset(), min(p["ready"] for p in pool if p["ready"]))
        now = sorted(p["ready"] for p in pop)[len(pop) // 2]
        before = bt.wait_obs(pop, now, UTC, frozenset())
        later = [dict(p) for p in pop]
        for p in later:
            if p["closed"] and p["closed"] > now:
                p["closed"] += timedelta(days=30)                         # closes later than it did
                p["merged"], p["wait"] = (not p["merged"]), 99.0          # or ends another way
        self.assertEqual(sorted(bt.wait_obs(later, now, UTC, frozenset())), sorted(before))
        self.assertTrue(any(not f for _, f in before) and any(f for _, f in before))

    def test_the_backtest_scores_the_km_wait_that_estimate_prints(self):
        rows, _, queue = simulate.generate_all("wait-dominates", 7, 400)
        with tempfile.TemporaryDirectory() as tmp:
            simulate.write_rows(rows, Path(tmp) / "tasks.jsonl")
            simulate.write_rows(queue, Path(tmp) / "queue.jsonl")      # found beside tasks.jsonl without --queue
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                bt.main(["--in", str(Path(tmp) / "tasks.jsonl")])
        wait = buf.getvalue().split("== LOCKED · WAIT ==")[1]
        self.assertIn("km wait ", wait)
        self.assertIn("km wait+queue", wait)
        self.assertNotIn("km wait", buf.getvalue().split("== LOCKED · CODING ==")[1].split("== LOCKED · WAIT ==")[0])

    def test_km_wait_with_the_queue_beats_the_unconditioned_one_and_the_baseline(self):
        for seed in self.SEEDS:
            with self.subTest(seed=seed):
                w = self.wait_tables(seed)
                self.assertLess(w["km wait+queue"]["winkler"], 0.85 * w["km wait"]["winkler"])
                self.assertLess(w["km wait+queue"]["winkler"], 0.85 * w["baseline: repo"]["winkler"])

    def wait_tables(self, seed):
        rows, _, queue = simulate.generate_all("wait-dominates", seed)
        with tempfile.TemporaryDirectory() as tmp:
            simulate.write_rows(rows, Path(tmp) / "t.jsonl")
            simulate.write_rows(queue, Path(tmp) / "q.jsonl")
            pool = bt.load_queue(str(Path(tmp) / "q.jsonl"))
            rows = bt.load(str(Path(tmp) / "t.jsonl"), UTC, {}, None, pool)
        since = min(r["start"] for r in rows)
        rows = [r for r in rows if r["start"] <= max(r["end"] for r in rows) - timedelta(days=21)]
        _, lock = bt.split(len(rows), 200)
        bt.bucket_load(rows, rows[:lock])
        ctx = dict(tz=UTC, holidays=frozenset(), pop=bt.wait_population(pool, UTC, frozenset(), since),
                   cuts=bt.tercile_cuts([r["queue"] for r in rows[:lock] if r["queue"] is not None]))
        view = [dict(r, y=r["wait_y"]) for r in rows]
        return {n: bt.score(bt.run(view, list(range(lock, len(rows))), n, 10, 0.1, 0.9, ctx))
                for n in ("km wait", "km wait+queue", "baseline: repo")}

    def test_queue_files_are_found_beside_tasks_files_under_either_naming(self):
        self.assertEqual(bt.queue_path("d/tasks.jsonl"), Path("d/queue.jsonl"))
        self.assertEqual(bt.queue_path("d/repo.tasks.jsonl"), Path("d/repo.queue.jsonl"))
        self.assertIsNone(bt.queue_path("d/other.jsonl"))

    def test_the_estimate_conditions_waiting_on_the_queue(self):
        rows, _, queue = simulate.generate_all("wait-dominates", 7, 500)
        with tempfile.TemporaryDirectory() as tmp:
            simulate.write_rows(rows, Path(tmp) / "r.tasks.jsonl")
            simulate.write_rows(queue, Path(tmp) / "r.queue.jsonl")
            medians = {}
            for depth in (0, 20):
                [line] = [l for l in estimate.split_lines(str(Path(tmp) / "r.tasks.jsonl"), UTC, "M", {}, queue_n=depth)
                          if l.startswith("Waiting")]
                self.assertIn(f"with {depth} PRs already waiting", line)
                medians[depth] = float(re.search(r"median ([\d.]+)", line).group(1))
        self.assertLess(medians[0], medians[20])

    def test_same_seed_gives_the_same_queue_file_and_it_knows_nothing_after_the_data_ends(self):
        a, b = (simulate.generate_all("wait-dominates", 7, 200)[2] for _ in range(2))
        self.assertEqual(a, b)
        self.assertEqual({r["state"] for r in a}, {"MERGED", "CLOSED", "OPEN"})
        opens = [r for r in a if r["state"] == "OPEN"]
        self.assertTrue(all(r["closed_at"] is None for r in opens))
        newest = max(t for r in a for t in (r["created_at"], r["ready_at"], r["closed_at"], r["first_review_at"],
                                            *[x["at"] for x in r["reviewers"]]) if t)
        self.assertEqual(newest, max(r["created_at"] for r in a))  # the last PR opened ends the data

    def test_reviewers_come_from_two_teams(self):
        rows, _, queue = simulate.generate_all("wait-dominates", 7, 200)
        teams = {x["who"] for r in queue for x in r["reviewers"] if x["who"].startswith("team:")}
        self.assertEqual(teams, {"team:acme/team-a", "team:acme/team-b"})
        self.assertTrue(all(r["reviewers"] and r["ready_at"] and r["prs"] for r in rows))
