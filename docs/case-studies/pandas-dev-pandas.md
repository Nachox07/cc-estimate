# Case study: pandas-dev/pandas

A run of `cc-estimate` on a large public repository, to see what the backtest says on history the
author did not write. Aggregate output only; no individual is named or measured.

## Repo and window

- Repo: [pandas-dev/pandas](https://github.com/pandas-dev/pandas) (Python, very active).
- Window: merged 2026-04-01 to 2026-10-01 (six months). Run on 2026-10-28.
- Source: `github` (pull requests and the issues they close). No fallback to `git-tasks` was needed.
- Timezone: `UTC`. pandas has contributors in many timezones, so the 09:00-18:00 Mon-Fri working
  day is an approximation (see caveats).
- Counts: 1,671 merged PRs fetched; 1,566 kept after dropping bots and automation; 1,550 tasks
  (14 tasks hold more than one PR). 659 of the 1,550 tasks (43%) close at least one issue. The
  rest are PRs that close nothing, each its own task. So "most PRs close an issue" did not hold
  here; the issue links matter for a minority of tasks.
- Backtest input: 1,288 tasks. 262 tasks that started after 2026-09-10 were dropped as
  right-censored. Split: first 200 history only, 200-1,069 tune (870), 1,070-1,287 locked (218),
  locked slice starting 2026-08-19.
- GitHub cost: GraphQL about 700 points of 5,000 (fetch 99, links 598), plus one retried HTTP 502.

## Commands

```bash
python3 -m venv venv && venv/bin/pip install -e <worktree>
cc-estimate init     # then set repo=pandas-dev/pandas, timezone=UTC, remove placeholder bots and absence
cc-estimate fetch --repo pandas-dev/pandas --since 2026-04-01 --until 2026-10-01 --config cc-estimate.json --out data/pandas.jsonl
cc-estimate links --repo pandas-dev/pandas --since 2026-04-01 --until 2026-10-01 --config cc-estimate.json --out data/pandas.links.jsonl
cc-estimate tasks --prs data/pandas.jsonl --links data/pandas.links.jsonl --config cc-estimate.json --out data/pandas.tasks.jsonl
cc-estimate backtest --in data/pandas.tasks.jsonl --config cc-estimate.json
cc-estimate estimate --size S --config cc-estimate.json --tasks data/pandas.tasks.jsonl
cc-estimate estimate --size M --config cc-estimate.json --tasks data/pandas.tasks.jsonl
cc-estimate estimate --size L --config cc-estimate.json --tasks data/pandas.tasks.jsonl
```

No spans CSV, no `classify`, no absence days (so "tasks with blocked or absence time removed: 0").

## Backtest output

```text
dropped 262 tasks starting after 2026-09-10 (right-censoring)
tasks with blocked or absence time removed: 0
tasks: 1288  tune: [200,1070)  locked: [1070,1288)  level=0.8  locked from 2026-08-19
  XS n= 693  business hours p10=   0.0 p50=   9.0 p90= 132.4
  S  n= 480  business hours p10=   0.0 p50=  27.0 p90= 407.5
  M  n=  95  business hours p10=   0.4 p50=  63.0 p90= 793.6
  L  n=   7  business hours p10=   0.0 p50=  96.0 p90= 590.2
  XL n=  13  business hours p10=   0.0 p50=   9.0 p90=  90.0

== TUNE · all tasks ==
  method                         n  cover  width_h medAE_h  winkler fallbk
  baseline: repo               870   0.84    233.1    12.5    469.1   0.00
  baseline: trailing 30d       870   0.82    366.5    20.3    557.2   0.00
  baseline: same author        870   0.83    152.4    10.7    456.1   0.23
  kind+epic                    870   0.83    198.0    11.1    457.9   0.01
  size                         870   0.83    144.0    22.4    471.6   0.02
  size+kind                    870   0.84    144.0    18.0    469.0   0.06
  size+kind+epic               870   0.84    144.0    18.0    469.0   0.06
  size+kind x pace             870   0.83    134.4    12.3    434.5   0.06
  size+team load               870   0.73     90.0    21.4    607.7   0.08

== TUNE · planned M+ ==
  method                         n  cover  width_h medAE_h  winkler fallbk
  baseline: repo                65   0.75    269.8    34.2    721.0   0.00
  baseline: trailing 30d        65   0.75    410.6    33.0    829.6   0.00
  baseline: same author         65   0.69    148.3    37.6    826.7   0.17
  kind+epic                     65   0.74    251.4    45.0    793.4   0.02
  size                          65   0.72    538.6    81.0    850.7   0.26
  size+kind                     65   0.74    413.3    63.0    840.7   0.48
  size+kind+epic                65   0.74    413.3    63.0    840.7   0.48
  size+kind x pace              65   0.72    292.8    46.9    782.7   0.48
  size+team load                65   0.72    524.9   117.0    915.4   0.63

== TUNE · planned L+ ==
  method                         n  cover  width_h medAE_h  winkler fallbk
  baseline: repo                11   0.82    307.2    16.3    460.7   0.00
  baseline: trailing 30d        11   0.73    261.0    26.1    575.1   0.00
  baseline: same author         11   0.82     84.6     7.7    556.2   0.27
  kind+epic                     11   0.73     90.0     6.3    540.9   0.09
  size                          11   0.73    261.0    26.1    575.1   1.00
  size+kind                     11   0.73    261.0    26.1    575.1   1.00
  size+kind+epic                11   0.73    261.0    26.1    575.1   1.00
  size+kind x pace              11   0.82    198.6    15.9    478.9   1.00
  size+team load                11   0.73    261.0    26.1    575.1   1.00

== LOCKED · all tasks ==
  method                         n  cover  width_h medAE_h  winkler fallbk
  baseline: repo               218   1.00    279.0    10.2    286.1   0.00
  baseline: trailing 30d       218   1.00    220.4     9.0    211.3   0.00
  baseline: same author        218   0.96    212.6     9.0    200.3   0.15
  kind+epic                    218   0.98    255.4     9.0    255.6   0.00
  size                         218   0.98    414.0    23.3    317.2   0.00
  size+kind                    218   0.95    381.5    17.2    299.0   0.03
  size+kind+epic               218   0.95    381.5    17.2    299.0   0.03
  size+kind x pace             218   0.93    135.9     8.5    177.7   0.03
  size+team load               218   1.00    316.3     9.0    408.8   0.02

== LOCKED · planned M+ ==
  method                         n  cover  width_h medAE_h  winkler fallbk
  baseline: repo                21   1.00    297.0    11.3    290.7   0.00
  baseline: trailing 30d        21   1.00    219.8     9.0    208.2   0.00
  baseline: same author         21   0.90    206.1     9.0    235.0   0.10
  kind+epic                     21   0.95    260.6     9.9    270.6   0.00
  size                          21   0.86    736.3    45.0    623.1   0.05
  size+kind                     21   0.90    725.9    34.5    592.3   0.29
  size+kind+epic                21   0.90    725.9    34.5    592.3   0.29
  size+kind x pace              21   0.90    285.2    13.7    332.4   0.29
  size+team load                21   1.00    869.2    25.6    766.3   0.19

== LOCKED · planned L+ ==
  method                         n  cover  width_h medAE_h  winkler fallbk
  fewer than 10 tasks here: nothing to score
```

## Estimate examples

```text
$ cc-estimate estimate --size S
Size S: 480 finished tasks in history (data up to 2026-10-01).
80% range: 0.0-407.5 work hours (0.0-45.3 working days), median 27.0 h (3.0 d).
Without a size: 0.0-279.0 h.

$ cc-estimate estimate --size M
Size M: 95 finished tasks in history (data up to 2026-10-01).
80% range: 0.4-793.6 work hours (0.0-88.2 working days), median 63.0 h (7.0 d).
Without a size: 0.0-279.0 h.

$ cc-estimate estimate --size L
Size L: 7 finished tasks in history (data up to 2026-10-01).
80% range: 0.0-590.2 work hours (0.0-65.6 working days), median 96.0 h (10.7 d).
Without a size: 0.0-279.0 h.
Fewer than 20 tasks of this size: treat the range as rough.
```

## Reading it

**Does size beat the baselines on the locked slice? No.** On all 218 locked tasks, `size` has a
Winkler score of 317.2 against 286.1 (repo), 211.3 (trailing 30d) and 200.3 (same author). Lower
is better, so size is worse than all three. Its intervals are also the widest of the lot (414 h
against 212-279 h), and its median error is the largest (23.3 h against 9.0-10.2 h). The tune
slice agrees: size scores 471.6 against 469.1 (repo) and 456.1 (same author), a tie at best. On
the planned M+ subset (21 locked tasks) size is again worse (623.1 against 208.2-290.7), and
there are too few L+ tasks to score.

Other rows, as measured: `size+kind x pace` has the lowest locked Winkler score (177.7) with
width 135.9 h and 0.93 coverage, and also did best on tune among the size rows (434.5). It is a
tuned variant, so its tune-slice edge is not evidence by itself; the locked result is one read of
218 tasks on one repo.

**Coverage against the 80% target.** On the tune slice, coverage sits near the target (0.82-0.84
for the main methods). On the locked slice every method is above it: baselines 0.96-1.00, size
0.98. Coverage that high with wide intervals means the ranges are too generous, not well
calibrated. The locked slice is the most recent six weeks and its tasks are quicker than the
history before it, so ranges built from older tasks cover almost everything.

**Why size struggles here.** Median cycle time does rise with size (XS 9 h, S 27 h, M 63 h), but
the spread inside each size is huge: the S p90 is 407 h and the M p90 is 794 h. A minority of
long-lived PRs (open for weeks) dominate the upper end, so the 80% range for a size is mostly
tail. L and XL have 7 and 13 tasks, and the XL median (9 h) is lower than M, which suggests large
mechanical changes that merge quickly. Size is a weak signal for how long a PR waits.

## Caveats

- Cycle time here is mostly waiting (review, maintainer availability, CI), not effort. Many tasks
  have a 0.0 h lower bound: a PR opened and merged within one working-hour window.
- Single-timezone assumption. The working day is fixed at 09:00-18:00 UTC, Mon-Fri, for a project
  with contributors worldwide. Time outside it is not counted, whoever was working.
- The tasks are GitHub-sourced, so the end is the real merge. A git-only run would end at the last
  commit and miss the review wait.
- The `size` row is a best case: it uses the final changed-line count, which you would not know
  at the start. It still lost, so a real call on size would do no better.
- No absence or blocked time was removed. pandas labels were not mapped to `excluded_labels`.
- One repo, one six-month window, one locked slice of 218 tasks. Do not read this as a verdict on
  size-based estimates in general; it says that on this repo, in this window, size did not help.
