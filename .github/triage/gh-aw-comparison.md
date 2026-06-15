# GitHub Agentic Workflows comparison

This branch adds `.github/workflows/team-issue-triage-gh-aw.md`, a GitHub Agentic Workflows version of the Shiny issue triage workflow. The compiled workflow is `.github/workflows/team-issue-triage-gh-aw.lock.yml`.

## What is different

- The existing workflow uses `anthropics/claude-code-action` with `CLAUDE_CODE_OAUTH_TOKEN`.
- The gh-aw workflow uses `engine: claude` with the `ANTHROPIC_API_KEY` Actions secret.
- The agent job stays read-only. Label writes and state persistence run through the custom `apply_triage_actions` safe-output job.
- The same triage config, label taxonomy, rubric, GitHub App token-map script, label allowlist validator, and `triage-state` branch are reused.
- The gh-aw workflow is manual-dispatch only so the team can compare it without creating a second scheduled triage run.

## Required secrets

- `ANTHROPIC_API_KEY`: Anthropic API key used by gh-aw's Claude engine.
- `POSIT_SHINY_AUTOMATION_CLIENT_ID`: existing GitHub App client ID.
- `POSIT_SHINY_AUTOMATION_PEM`: existing GitHub App private key.

`CLAUDE_CODE_OAUTH_TOKEN` is intentionally not used by gh-aw.

## How to compare

1. Push this branch and open the Actions tab.
2. Run `Team Issue Triage (gh-aw Claude)` manually with a narrow `scan_since` value and a low `max_issues_total`.
3. Run the existing `Team Issue Triage` workflow with the same inputs.
4. Compare the Actions summaries, labels applied, `triage-state/triage-results/*`, issue coverage, runtime, and Claude/gh-aw logs.

Local maintenance commands:

```bash
gh aw validate team-issue-triage-gh-aw --strict --no-check-update
gh aw compile team-issue-triage-gh-aw --strict --validate --approve --no-check-update
```

If `.github/workflows/team-issue-triage-gh-aw.md` changes, regenerate and commit the `.lock.yml` file.
