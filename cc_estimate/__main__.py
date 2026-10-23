"""cc-estimate <command> [options]   (each command has its own --help)

Build the history. Pick one source, then refresh monthly:
  git-tasks  from local git only; any tracker key in commit messages (ABC-123, #123)
  fetch      GitHub: merged PRs                   -> data/<repo>.jsonl
  links      GitHub: issue links, label time spans -> data/<repo>.links.jsonl
  tasks      GitHub: group PRs into tasks         -> data/<repo>.tasks.jsonl
Then, optionally:
  spans      add blocked/waiting periods from any tracker (CSV: key,group,start,end)
  classify   approvals and test suites per task   -> data/<repo>.profiles.jsonl
  backtest   check which method beats a plain guess on your history

Use it:
  estimate   a range for a new task; --log to record it
  attach     link a logged estimate to its PR
  score      compare your logged estimates with what happened
  summary    a markdown report of the PR history
"""
import sys

from . import backtest, classify, estimate, gitsource, history, links, spans, tasks


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in ("fetch", "summary", "ci"):
        return history.main([cmd, *rest])
    if cmd in ("estimate", "attach", "score"):
        return estimate.main([cmd, *rest])
    modules = {"links": links, "tasks": tasks, "classify": classify, "backtest": backtest,
               "git-tasks": gitsource, "spans": spans}
    if cmd not in modules:
        sys.exit(f"cc-estimate: unknown command {cmd!r}. Run `cc-estimate --help`.")
    return modules[cmd].main(rest)


if __name__ == "__main__":
    sys.exit(main())
