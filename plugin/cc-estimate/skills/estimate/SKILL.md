---
name: estimate
description: Estimate how long a task will take from the team's own delivery history — an 80% cycle-time interval in working days by size, logged and later scored against what actually happened. Use when asked "how long will this take", "estimate this ticket", "size this task", "how accurate are my estimates", "/estimate". Only the user's own estimates are logged or scored; never compare people.
---

# cc-estimate

`cc-estimate` returns the 10th–90th percentile of cycle time, in working hours, for finished tasks
of the same size. Working hours exclude weekends, nights, absence and non-working states such as
blocked. Cycle time is mostly waiting, so intervals are wide. Say so. Never narrow an interval by
hand, and never add a point estimate the tool did not print.

The skill runs the `cc-estimate` command. If it is not installed: `pipx install git+https://github.com/Nachox07/cc-estimate`.

## Steps

1. **Size.** Ask for XS/S/M/L/XL, or propose one and confirm. Buckets are changed lines of product
   code, tests and docs not counted: XS <50, S <300, M <1,500, L <5,000, XL ≥5,000. Agent-written work
   often carries several times its product lines in tests; leave those out of the size.
2. **Estimate and log:** `cc-estimate estimate --size <S> --issue <KEY> --log`.
   - Add `--paths` with the files or directories the change will touch, to list the approvals and
     test suites it triggers.
   - Add `--off <dates>` for days the user won't work on it.
   - With no ticket yet, use `--text`, then `cc-estimate attach <id> --pr <N>` once the PR exists.
3. **Report** the interval in working days, the median, the sample size and the data date, exactly
   as printed.
4. **Score on request:** `cc-estimate score`. Below 15 finished estimates, say it is too early to
   judge.

If the data date is more than a month old, rebuild the history (README, "Quick start") and rerun
`cc-estimate backtest` to confirm size still beats the baselines.

## Never

- Print or compare other people's numbers.
- Commit or share the history data or the cache.
