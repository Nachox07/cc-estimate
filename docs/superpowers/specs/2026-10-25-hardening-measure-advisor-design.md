# v0.1.1 hardening, v0.2 measure and sizing advisor, Jira source — design

Status: approved in conversation 2026-10-25. Approach: fix the measure first, then build the advisor on it.

## Why

Two kinds of teams using coding agents report opposite problems. One finishes tickets far faster
than planned, so old estimates feel meaningless. The other produces very large pull requests.
The tool must give current ranges for the first, and a reason and a way to cut work for the second.
Today size counts test lines, so most agent-written changes land in XL; an advisor on that number
would tell every change to split. The measure is fixed first.

## Release v0.1.1 — make it safe to publish

1. `summary` prints no other person's login or per-person numbers. A test covers every command's
   output, not just `estimate` and `backtest`.
2. `backtest` works on any history size: the tuning/locked split scales down, and below a minimum
   (default 40 finished tasks) it says the history is too short instead of crashing.
3. The default branch is configurable (`default_branch`, default `main`) in `tasks` and in GitHub scoring.
4. `git-tasks` matches `#123` as well as `ABC-123` by default.
5. An empty size bucket returns "not enough history for this size" plus the size-blind range.
6. Output directories are created when missing.
7. The git-only source reports **commit span** (first to last keyed commit). The README says it
   excludes review and merge waiting unless the history includes merge commits on the target branch,
   in which case the merge time is the end.
8. Packaging: `.claude-plugin/marketplace.json` at the root, the plugin under `plugin/cc-estimate/`
   with `.claude-plugin/plugin.json` and `skills/estimate/SKILL.md` as the single copy of the skill.
   The CLI installs with `pip`. The README documents both, and a fresh-install test runs in CI.
9. The README claims only what the code shows: the size backtest is a best case that assumes the
   final size is known; logged estimates measure the real case.
10. Tracker keys are scored per source: GitHub accepts issue numbers; git accepts any key.

## Release v0.2 — the measure

1. **Size** buckets use product-code lines. Tests and docs are reported beside them. A change is
   **test-heavy** when test lines exceed 3× code lines; the estimate says so.
2. **Two parts of cycle time**: *coding* (first commit to ready-for-review, or PR open when there is
   no draft stage) and *waiting* (ready to merge). Printed separately as ranges; never summed into
   the main range. The git-only source has no ready signal and prints the total only.
3. **Trailing window**: ranges come from tasks finished in the last N days (default 90). `backtest`
   compares windows (30/60/90/all) and reports the best on the locked slice.
4. Tasks carry `code_lines`, `test_lines`, `doc_lines`, `ready_at` (when known) and `areas`. The
   `tasks.jsonl` contract in the README is updated; old files without the fields still load.

## Release v0.2 — sizing advisor, ticket mode

`cc-estimate advise --size <S> --paths <planned paths>` (or `--plan <file>` listing several planned
tasks with paths and size).

- **Split** when the task is above the history's *break point*: the smallest size bucket or area
  count at which the 80% range more than doubles in width compared with the bucket below. Slices
  follow area boundaries in dependency order (shared code, then the services using it, then UI,
  then docs), each with its own estimate.
- **Merge** when two planned tasks are both XS or S and touch the same area.
- **Keep** otherwise.
- **No advice** when history has fewer than 20 tasks on either side of the break point, or when the
  backtest shows split tasks were not faster in total than single tasks of the same total size.
- The backtest of the advisor: for past tasks above the break point, compare the total cycle time of
  sets of tasks closing one parent issue with single tasks of the same total size.

## Release v0.2.x — sizing advisor, diff mode

`cc-estimate advise --diff <base>...<head>`: group changed files by area, order the groups by import
or path dependency where detectable, and propose stacked pull requests, each under the break point.
Proposals only; nothing is rewritten.

## Jira source

- **Agent path**: the skill tells an agent with a connected Jira MCP to search issues by JQL, read
  each issue's status history, and write `tasks.jsonl` rows plus a `key,group,start,end` CSV of
  non-working statuses (configurable list, default "Blocked", "On Hold"). The first task verifies
  that the Atlassian MCP exposes status history; if not, the agent path calls the headless command.
- **Headless path**: `cc-estimate jira-tasks --jql "<query>"` with `JIRA_URL`, `JIRA_EMAIL`,
  `JIRA_API_TOKEN`. Stdlib HTTP against the Jira Cloud REST API (search with changelog). Start is
  the first transition into an in-progress category, end is resolution. Non-working statuses become
  `excluded` spans.
- Lines and paths come from git by matching the issue key in commit messages, as `git-tasks` does.

## Evals — is the answer right?

Unit tests show the code does what it says. Evals show the numbers are worth trusting. Three layers.

**1. Simulated history with known truth (v0.2, runs in CI).** `cc-estimate simulate --scenario <name>
--seed <n> --out <dir>` writes a `tasks.jsonl` and, for the git source, a real git repository built
from the same tasks. Durations are drawn from a stated model, so the right answer is known. Fixed
seeds; each scenario asserts a band, not an exact number. All comparisons are on the same locked
slice. The scenarios work in pairs, so a method cannot pass by doing nothing: always falling back to
the baseline passes `null` but fails `size-matters`, and calling every task XS fails `test-heavy`. Each
scenario also runs `estimate` on fresh tasks drawn from the same world and checks that it uses what
the backtest supports: the size range in `size-matters`, the size-blind range in `null`, the 90-day
window in `drift`, and the product-code bucket in `test-heavy`.

| Scenario | World | Must hold |
|---|---|---|
| size-matters | duration grows with size | size rows: coverage 70–90% and a Winkler score at least 15% below the baseline's |
| null | size has no effect | size rows: coverage 70–90%, Winkler score within 5% of the baseline's; `backtest` does not recommend size |
| drift | tasks get 2× faster halfway | `backtest` picks the 90-day window, its Winkler score at least 15% below "all", and `estimate` uses it |
| steady | no change over time, wide spread | `backtest` picks "all" or the longest window, and `estimate` uses it |
| test-heavy | a third of tasks are M by product code and carry 5× that in tests, enough to make them XL if tests counted | each lands in the bucket of its product-code size and is flagged test-heavy; no other task is flagged |
| break-point | width doubles above L; 60 tasks per bucket | the advisor names L as the break and splits no task at or below it; the slices together cover every planned file, each is below the break, and the replay of those slices finishes sooner in total than the single task |
| no-break | width grows smoothly; 60 tasks per bucket | the advisor gives no advice |
| slow-splits | a break above L, but past split tasks took longer in total | the advisor gives no advice |
| thin-advisor | a break above L, 15 tasks above it | the advisor gives no advice and says history is too thin |
| merge-keep | a plan of two XS tasks in one area, two XS tasks in different areas, and one M task | the advisor merges only the same-area pair and keeps the rest |
| thin | 30 tasks, 27 of them S and 3 of them L | `backtest` refuses; `estimate --size L` says the size has too little history and gives the size-blind range |

The same generator gives new users a sample history to try the commands on.

**2. Self-check on the user's own history (v0.2).** `backtest` adds per-bucket coverage and one line
per feature: "trust it here", "too little data" or "does not help here". For the advisor it replays
past split and single tasks of the same total size. This is what tells a user whether the tool works
on their project; the simulated layer only shows the method can work.

**3. Skill evals (before each release, by hand).** `plugin/cc-estimate/evals/` holds cases for
`claude plugin eval`, run against a simulated repository, with a no-plugin baseline arm
(`--ablation with-without`). Cases:

- asks for a size when none is given;
- reports the range, N and data date as the command printed them, with no made-up single number;
- logs the estimate;
- declines to compare two people;
- recommends a split for a large multi-area task (v0.2);
- says there is not enough history when there is not.

Not in CI: each run costs model calls. The release checklist records the pass rate and cost.

## Out of scope

Linear (same two paths later), similar-task retrieval, GitLab and Azure DevOps sources, any
cross-person reporting.

## Testing

- Every behaviour above has a unit test on synthetic data; no real repository data in the repo.
- Privacy: one parametrised test runs every CLI command on a fixture with three logins and asserts
  that only the current user's login appears.
- Leak test: an estimate does not change when outcomes after its start change.
- Jira: the REST client is tested against recorded, synthetic JSON responses.
- CI runs the suite on Python 3.10, 3.12 and 3.13, plus a fresh `pip install` and plugin manifest check.
