# Team issue triage

`.github/workflows/team-issue-triage.yml` runs Claude Code every Thursday at 08:00 UTC. You can also start it with workflow dispatch. The workflow reads `.github/triage/team-issue-triage.yaml` for scope, and the first rollout is limited to `rstudio/reactlog`.

Authentication comes from a Claude Code OAuth token created with `claude setup-token`.

## Files in this directory

- `team-issue-triage.yaml` defines the repo allowlist, report repo, scan limits, provider guardrails, and state paths.
- `labels.yaml` defines the label taxonomy and the `allowed_safe_output_labels` list used by the post-processing validator.
- `issue-triage-rubric.md` is the rubric passed to Claude.
- `scripts/` contains the repository resolver, label spec resolver, GitHub App token map generator, and the `gh` token router.

The workflow keeps its state on the long-lived `triage-state` branch in `cursors.json`, `issues/*.jsonl`, `triage-results/*.jsonl`, and `duplicates/candidates.jsonl`.

## Configuration

Required secrets and variables:

- Org or repo secrets inherited by this workflow repo: `POSIT_SHINY_AUTOMATION_CLIENT_ID`, `POSIT_SHINY_AUTOMATION_PEM`.
- Repo secret: `CLAUDE_CODE_OAUTH_TOKEN`, created with `claude setup-token`.
- Optional repository variable: `ANTHROPIC_MODEL`.

Do not set `ANTHROPIC_API_KEY` or `AWS_BEDROCK_ROLE_TO_ASSUME` for this workflow. Claude Code gets its credentials from `CLAUDE_CODE_OAUTH_TOKEN`.

The post-processing step applies validated labels, report issues, and approved comments. It also writes progress and audit records to `triage-results/*.jsonl` on the `triage-state` branch.

## Adding repositories

1. Install the `posit-shiny-automation` GitHub App on the repo with issue write access.
2. Add `owner/repo` to `repositories:` in `team-issue-triage.yaml`.
3. The workflow will create or update the managed labels from `labels.yaml` on every allowlisted repo before triage runs.

The allowlist can include repositories from different owners. For each repo, the workflow looks up the matching GitHub App installation, groups repos by installation, and writes temporary read and write token maps for the run. Each `gh --repo owner/repo` call uses the token for that installation, so keep each command pointed at one repo.

Example:

```yaml
repositories:
  - rstudio/repo-a
  - posit-dev/repo-b
  - another-org/repo-c
report_repo: rstudio/repo-a
```

## Issue and commit style

Report issues opened by the workflow use a conventional-commit-style title: `triage(<scope>): <imperative summary>`. Keep the title under 72 characters and do not end it with a period. Use the short repo name for `<scope>`, or `cross-repo` when the report spans repositories.

Report bodies must include these sections in this order: `## Summary`, `## Affected repositories`, `## Evidence`, `## Recommended next action`, and `## Confidence`. The workflow adds a standard footer with the workflow run link, model, timestamp, and applied labels.

Every report issue should carry `ai-triage:report` and `ai-generated-issue`. The workflow synchronizes those labels into the report repo before triage actions are processed.

Updates to the `triage-state` branch are committed as `chore(triage): update team issue triage state`, with a body that references the workflow run.
