# Peer pools, time off, handovers and status signals — design

Status: draft for review, 2026-10-28. Builds on the v0.2 measure in
`2026-10-25-hardening-measure-advisor-design.md`.

## Why

A range drawn from the whole repository mixes very different work: a backend change by someone who
owns that code and a first change in an unfamiliar area. A task's clock also keeps running through
time that says nothing about the work: a holiday, lost access, a sick day, a task changing hands.
Both make ranges wider than they need to be, and the second makes delivery dates wrong.

## What the tool protects, and what it does not

Anyone who can run `fetch` can read the same PR and issue timestamps from the GitHub API. The tool
cannot hide public repository data from them, and does not try. What it guarantees:

1. No command prints a per-person table, ranking or another person's login.
2. A range built from other people's work is released only under the rule in §1.
3. Time off is private data the team supplies. No output reveals any amount of it, for anyone,
   including totals ("hours removed") that a user could difference.
4. No reason is stored with time off. "Sick" is health data and the tool has no use for it.

## 1. Peer pools ("people in the same role as me")

Config, one pool per role, team or area, chosen by the team:

```json
"pools": {"backend": ["login-a", "login-b", "login-c", "login-d"]},
"identities": {"login-a": ["a@example.com", "a@users.noreply.github.com"]}
```

`identities` maps each login to the git emails it commits with, so the git-only source can match
pool members. An email with no mapping belongs to no pool. The current user is the GitHub login, or
for the git source the login whose `identities` contain `git config user.email`.

**Release rule.** A pool range for a size is shown only when, among finished tasks of that size in
the pool, excluding the user's own:

- at least three people have at least five tasks each, and
- no one person has more than 40% of the tasks.

Otherwise `estimate` uses the whole repository and says the pool is too small. Task counts are
printed rounded down to the nearest 10 ("40+ tasks"). The user's own tasks are left out of their pool range, so the
range describes the others and the user is compared with it, not with a mix that includes themself.

- **`estimate`** prints the pool range when released, otherwise the repository range, and always the
  size-blind range.
- **`score`** compares only the user's own logged estimates with their own actuals, as today. After
  15 or more finished estimates it adds one aggregate line: "Your actuals: 9 of 15 inside your
  pool's range, 2 faster, 4 slower." Never one line per estimate, so no single comparison exposes
  the edge of a range. This answers "how do I compare with people in the same role" with an
  aggregate of at least three people, not a person-to-person comparison. It replaces the earlier
  spec's blanket "no cross-person reporting" for this one aggregate only.
- **`backtest`** adds `size+pool` (each task estimated from its author's pool, by the same release
  rule, using only tasks finished before it started).

## 2. Time off and lost access

What exists: `absence` in the config (per login, and `*` for public holidays) as inclusive dates,
`--off` on `estimate`, and `excluded_labels` turned into excluded spans.

**The file.** `time_off_file` in the config points to a local, git-ignored CSV:

```csv
login,start,end
login-a,2026-11-03,2026-11-05
login-a,2026-11-10T14:00,2026-11-10T18:00
```

- A date is a whole working day; `end` is inclusive. A date-time is a span in the config
  `timezone`; `end` is exclusive.
- Overlapping rows are merged. A row whose login is not known, whose end is before its start, or
  that does not parse stops the run with the line number.
- `cc-estimate off add 2026-11-03..2026-11-05` appends rows for the current user only.

**What it changes.** Time off is a calendar fact from an independent record, so it is removed from
the target, the same way `absence` is today: the target is working hours minus time off.

**Delivery dates.** The range stays in working hours. `estimate` also converts it into calendar
dates from today, skipping weekends, public holidays and the user's own future time off in the file
plus `--off`: "likely done between 12 Nov and 19 Nov". `score` checks the actual against the same
working-hours range, so the forecast and the score use one definition.

**Output.** `backtest` keeps its count of tasks with any time removed, and prints nothing that
measures how much.

## 3. Handovers

**Source.** On GitHub, the closing issues' `AssignedEvent` and `UnassignedEvent`, with assignee
login and time, fetched with pagination (today's query reads ten events and only their times). On
Jira, the changelog's assignee field. The git-only source has no handover signal.

**Rules.**

- The owner at any moment is the issue's assignee. Several assignees at once are collaborators,
  not a handover. A task with several closing issues uses the issue assigned first.
- A handover is an assignee leaving while another person stays or arrives, after the task started.
- Different PR authors on one task are collaboration, not a handover.

**Use.** Tasks gain `handover: true` and `owners: [[login, from, to], ...]`. Each owner's time off is
removed only from their own segment. For prediction, the owner is the one known when the task
started (its first owner); that is who `size+pool` and pace use in `backtest`, so nothing after the
start leaks in. `backtest` reports coverage for handover tasks as a separate row.

## 4. Status signals

These are discretionary: a person decides to mark something blocked. So unlike time off, they are
**never removed from the target by default**. `estimate` reports them beside the range ("blocked
time on similar tasks: 0–6 h") and `backtest` prints that range. A team can opt in to removing a
signal from the target with `exclude_from_target`; `backtest` then prints the scores against the
original target as well, so the effect of the choice is visible.

- **Labels**: `excluded_labels`, as today. Existing configs keep today's behaviour (removed from the
  target) until changed.
- **GitHub Projects status**: the issue timeline item `PROJECT_V2_ITEM_STATUS_CHANGED_EVENT`
  returns `createdAt`, `previousStatus` and `status` (checked on a public repository on 2026-10-28).
  Config names the project number and status values (`"project_status": {"project": 2,
  "blocked": ["Blocked", "On hold"]}`). Events are fetched with pagination; a span runs from moving
  into a listed status to the next event or the issue's close. An issue with no events for that
  project contributes nothing. A recorded, synthetic GraphQL response is the test fixture.
- **Jira statuses**: as in the Jira design.
- **Comment markers**: issue comments only (not PR comments), first line exactly `/blocked` or
  `/unblocked`. A span runs from `/blocked` to the next `/unblocked` or the issue's close. A second
  `/blocked` while open is ignored; an `/unblocked` with no open span is ignored. Deleted comments are
  gone from the API and so do not count; edits are read as the current text. Free text is never
  parsed.

## How signals are judged

All methods are scored against one target: working hours minus time off. Signals, pools and
thresholds are compared and chosen on the **tune** slice. The chosen setup is then scored once on
the **locked** slice, which is never used for choosing. A signal stays on by default only if, on
the tune slice, it lowers the Winkler score with coverage between 70% and 90%, and the locked-slice
check agrees. Logged estimates are the evidence for real use.

## Evals

Simulator scenarios, added to the v0.2 table:

| Scenario | World | Must hold |
|---|---|---|
| pools | two pools of 4 people, one 2× slower | `size+pool` beats `size` on tune and locked; `estimate` uses the user's pool |
| tiny-pool | a pool of 3 where one person has 60% of tasks | `estimate` withholds the pool range and says why |
| time-off | 20% of tasks carry a holiday | ranges narrow against the shared target; delivery dates skip the user's planned days |
| handover | 15% of tasks change assignee midway | flagged; each owner's time off applies only to their segment; prediction uses the first owner |
| markers | `/blocked` … `/unblocked` on issue comments | blocked-time range reported; target unchanged unless opted in |

Privacy tests:

- The every-command output test runs with pools, a time-off file and handovers. No other login,
  email or time-off amount appears.
- The release rule: pools that break it (too few people, too few tasks each, one person over 40%)
  are withheld, and printed counts are rounded down to tens.

A user who edits the pool config and compares outputs can learn something about one member's
durations. Those durations come from public PR timestamps that the same user can already read
from the API, so the rule aims to stop accidental per-person reporting, not a determined reader of
public data. Time off is not public, which is why no output measures it at all.

## Out of scope

Guessing pools from data, parsing free text, storing reasons for time off, and any output of another
person's time off.
