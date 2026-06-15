# GitHub Agentic Workflows comparison

This branch adds `.github/workflows/team-issue-triage-gh-aw.md`, a GitHub Agentic Workflows version of the Shiny issue triage workflow. The compiled workflow is `.github/workflows/team-issue-triage-gh-aw.lock.yml`.

## What is different

- The existing workflow uses `anthropics/claude-code-action` with `CLAUDE_CODE_OAUTH_TOKEN`.
- The gh-aw workflow uses `engine: claude` with the `ANTHROPIC_API_KEY` Actions secret.
- The agent job stays read-only. The custom `summarize_triage_dry_run` safe-output job validates proposed label actions and publishes a GitHub Actions summary, but does not apply labels.
- The same triage config, label taxonomy, rubric, label allowlist validator, and read-only `triage-state` context are reused.
- The gh-aw workflow is manual-dispatch only so the team can compare it without creating a second scheduled triage run.

## Required secrets

- `ANTHROPIC_API_KEY`: Anthropic API key used by gh-aw's Claude engine.

`CLAUDE_CODE_OAUTH_TOKEN` is intentionally not used by gh-aw.

## How to compare

1. Push this branch and open the Actions tab.
2. Run `Team Issue Triage (gh-aw Claude)` manually with a narrow `scan_since` value and a low `max_issues_total`.
3. Run the existing `Team Issue Triage` workflow with the same inputs.
4. Compare the gh-aw dry-run summary with the existing workflow's applied labels, `triage-state/triage-results/*`, issue coverage, runtime, and Claude/gh-aw logs.

The gh-aw workflow should not create labels, apply labels to issues, or push updates to `triage-state`.

Local maintenance commands:

```bash
gh aw validate team-issue-triage-gh-aw --strict --no-check-update
gh aw compile team-issue-triage-gh-aw --strict --validate --approve --no-check-update
```

If `.github/workflows/team-issue-triage-gh-aw.md` changes, regenerate and commit the `.lock.yml` file.
