# GitHub Agentic Workflows comparison

This branch adds `.github/workflows/team-issue-triage-gh-aw.md`, a GitHub Agentic Workflows version of the Shiny issue triage workflow. The compiled workflow is `.github/workflows/team-issue-triage-gh-aw.lock.yml`.

## What is different

- The existing workflow uses `anthropics/claude-code-action` with `CLAUDE_CODE_OAUTH_TOKEN` and checked-out Git branch state (`triage-state` branch).
- The gh-aw workflow uses `engine: claude` with the `ANTHROPIC_API_KEY` Actions secret, and persists triage cursors and duplicate candidates state in a pinned issue comment using the `comment-memory` tool.
- The agent job is dry-run for GitHub labels. The custom `summarize_triage_dry_run` safe-output job validates proposed label actions and publishes a GitHub Actions summary, but does not apply labels.
- The same triage config, label taxonomy, rubric, and label allowlist validator are reused.
- The gh-aw workflow is manual-dispatch only, requires `confirm_dry_run=true`, and is named `Team Issue Triage (gh-aw Claude Manual Dry Run)` so the team can compare it without creating a second scheduled triage run.

## Required secrets

- `ANTHROPIC_API_KEY`: Anthropic API key used by gh-aw's Claude engine.

`CLAUDE_CODE_OAUTH_TOKEN` is intentionally not used by gh-aw.

## How to compare

1. Push this branch and open the Actions tab.
2. Run `Team Issue Triage (gh-aw Claude Manual Dry Run)` manually with `confirm_dry_run=true`, a narrow `scan_since` value, a low `max_issues_total`, and specify the issue number to use for `triage_state_issue` (e.g. `1` or a test issue).
3. Run the existing `Team Issue Triage` workflow with the same inputs.
4. Compare the gh-aw dry-run summary with the existing workflow's applied labels, issue coverage, runtime, and Claude/gh-aw logs.

The gh-aw workflow should not create labels or apply labels to issues, but it will update the state comment (cursors and duplicates) on the specified issue.

Local maintenance commands:

```bash
gh aw validate team-issue-triage-gh-aw --strict --no-check-update
gh aw compile team-issue-triage-gh-aw --strict --validate --approve --no-check-update
```

If `.github/workflows/team-issue-triage-gh-aw.md` changes, regenerate and commit the `.lock.yml` file.
