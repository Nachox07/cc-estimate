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
            rows, skipped = gitsource.build(gitsource.commits(d, "HEAD", "2020-01-01"), re.compile(gitsource.DEFAULT_KEY))
        by = {r["id"]: r for r in rows}
        self.assertEqual(skipped, 1)
        self.assertEqual(by["ABC-1"]["commits"], 2)
        self.assertEqual(by["ABC-1"]["lines"], 1 + 3)
        self.assertEqual(by["ABC-1"]["start"][:10], "2026-10-05")
        self.assertEqual(by["ABC-1"]["end"][:10], "2026-10-06")


class Spans(unittest.TestCase):
    def test_tracker_spans_are_subtracted_by_key(self):
        task = {"id": "ABC-1", "excluded": {}}
        spans.merge([task], [{"key": "ABC-1", "group": "blocked", "start": "2026-10-05T10:00:00+02:00",
                              "end": "2026-10-05T12:00:00+02:00"}, {"key": "ABC-9", "group": "blocked",
                              "start": "x", "end": "y"}])
        blocked = [[bt.parse(a), bt.parse(b)] for a, b in task["excluded"]["blocked"]]
        self.assertAlmostEqual(bt.biz_hours(t("2026-10-05 09:00"), t("2026-10-05 18:00"), MAD, blocked=blocked), 7)


if __name__ == "__main__":
    unittest.main()
