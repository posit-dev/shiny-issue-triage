# shiny-issue-triage

Automation for triaging issues in Shiny team repositories.

The workflow in `.github/workflows/team-issue-triage.yml` runs weekly and on
manual dispatch. Its allowlist, label taxonomy, safety rubric, and helper
scripts live in `.github/triage/`.

See `.github/triage/README.md` for configuration, required secrets, state
branch behavior, and validation commands.
