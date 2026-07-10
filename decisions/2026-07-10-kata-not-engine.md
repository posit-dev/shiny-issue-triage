# Keep triage-verse's own SQLite engine; do not adopt kata as the internal engine

**Date:** 2026-07-10
**Status:** Decided
**Related:** GitHub issues #40 (decision actor attribution), #41 (credential egress guard), #42 (`--json` CLI output) — the design lessons we *did* take from kata; upstream bug report kenn-io/kata#168 / fix PR kenn-io/kata#169

## Context

kata (<https://www.katatracker.com/>, source at `kenn-io/kata`) is a local-first
issue tracker built for coding agents: a single Go binary storing issue state
in SQLite, with one-way GitHub import, stable short IDs, priorities,
blocked-by/parent relationships, an audit trail on closes, JSON output on
every command, and a `kata next` work-dispatch primitive.

While setting kata up locally for three shinyverse repos (reactlog,
shinytest2, promises), it became clear that kata and triage-verse
independently converged on the same foundation: a local SQLite store treated
as derived data, one-way sync with GitHub as the source of truth, resumable
cursors, and idempotent upserts. That convergence raised the natural
question: rather than maintaining our own mirror-and-pipeline engine, should
triage-verse adopt kata as its internal engine and build the triage pipeline
on top of it?

Constraints that matter for the answer:

- The triage program must drain roughly 4,200 issues across 42 repos, with
  cost metering (a daily spend cap) and human approval gates.
- The pipeline's real substance is analytical state layered on the mirror:
  embeddings, dedup verdicts, classifications, batch orchestration, spend
  records, proposals, and review decisions — all currently tables in one
  SQLite database, one JOIN away from the issues they describe.
- Pull requests are part of the mirror (linked fixes and in-flight work
  inform triage verdicts).
- A future apply stage (P3) will write labels/comments/closes back to GitHub.

## Options considered

### A. Keep triage-verse's own engine — chosen

**Pros**

- Already built, validated at pilot scale, and shaped to the fleet: batch
  backfill with resumable cursors across all configured repos, count
  reconciliation against GitHub search (`verify-counts`), and snapshot
  publish/bootstrap so a fresh machine pulls the mirror instead of
  re-crawling GitHub.
- Mirrors PRs as well as issues and comments. kata's GitHub sync imports
  issues and comments only — no pull requests, no timeline events, and only
  the first assignee.
- One database holds both the mirror and every pipeline artifact
  (embeddings, classifications, dedup verdicts, spend, decisions), so
  pipeline state is transactional with the data it describes and queries
  need no cross-store joins.
- No new external dependency in the critical path of a program with cost
  and approval constraints.

**Cons**

- We keep maintaining sync code that overlaps with what kata offers, and we
  forgo kata's ready-made features (short IDs, relationship graph, audit
  closes, `kata next`, TUI, per-command JSON) instead of getting them free.
- Our engine has no work-dispatch or dependency-ordering primitives; if we
  ever need them, we build them or bolt kata on later.

### B. Adopt kata as the internal engine

**Pros**

- Ready-made issue store with exactly the agent ergonomics we admire:
  stable short IDs, priorities, blocked-by relationships, evidence-carrying
  closes with actor attribution, JSON output everywhere, and `kata next`
  for "give me the highest-priority ready item."
- Someone else maintains the GitHub sync.
- Architectural fit at the foundation level is real, not superficial —
  same local-SQLite / one-way-sync / GitHub-as-truth shape.

**Cons**

- **Covers the already-solved 20%, not the hard 80%.** kata replaces our
  mirror but has no schema for embeddings, classifications, dedup
  verdicts, batches, spend, or proposals. We would keep a second database
  keyed to kata's IDs and synchronize the two — strictly worse than one
  database.
- **Loses data we need.** No PR import, no timeline events, first assignee
  only. Rebuilding PR mirroring alongside kata means running most of our
  sync code anyway.
- **Wrong sync model for a fleet.** kata polls per-repo bindings via a
  daemon (default every 5 minutes) — fine for three repos, clumsy for 42;
  our batch sync with cursors, count verification, and snapshots is built
  for the fleet case. kata also has no equivalent of snapshot
  publish/bootstrap.
- **Maturity risk.** We were evidently the first users to sync a repo with
  more than 100 issues: v0.9.0's credential egress guard rejects GitHub's
  numeric-form pagination URLs, aborting sync at page 2 (reported as
  kenn-io/kata#168, fix submitted as kenn-io/kata#169; we run a locally
  patched binary meanwhile). A stable external API contract is itself
  still an open upstream issue. Acceptable for a sidecar; not for the
  system of record.
- **Overwrite hazard in its data model.** kata mutates one issue record
  with "GitHub-owned" fields that a later sync can silently clobber, and
  encodes provenance as a `[GitHub #123]` title prefix. Our split —
  mirror tables distinct from local classification/decision tables, keyed
  by repo and issue number — avoids that class of bug entirely.

### C. Hybrid: triage-verse as engine, kata as an agent-facing work queue on top

**Pros**

- Uses each tool for what it is: triage-verse decides *what should happen*
  to 4,200 issues (analytical pipeline); kata tracks *who does what next*
  (work ledger). `kata next` plus blocked-by ordering is a genuinely good
  dispatch primitive we lack, and kata's priority field is local-only —
  GitHub re-syncs never overwrite it — so our classifications could set
  priorities safely.

**Cons**

- Two systems to keep consistent, a daemon to run, and a patched binary to
  manage — standing cost with no proven need yet: today one human reviews
  proposals through the review app, and nothing dispatches per-issue work
  to agents.

## Decision

**Keep triage-verse's own engine (Option A).** The deciding factor: kata
would replace the part of the system that is already built and working —
the mirror — while providing nothing for the part that carries the
program's value — the analysis pipeline and its audit/cost state — and it
would do the mirror's job with less data (no PRs), a sync model that
doesn't fit 42 repos, and v0.9.x maturity risk.

kata's design still pays us, in two ways. First, its best ideas are adopted
as issues on this repo: actor attribution and reasons on review decisions
(#40), a fail-closed credential egress guard for GitHub writes (#41), and
`--json` output on every CLI command (#42). Second, the hybrid (Option C)
stays open as a cheap later experiment — it layers on top of our engine
without displacing anything — if per-issue work is ever dispatched to
agents.

## Consequences

- Triage-verse continues to own its sync, mirror schema, and pipeline
  tables; no kata dependency enters the pipeline.
- Issues #40, #41, and #42 carry the kata-inspired improvements; they stand
  on their own merits and none requires kata.
- The local kata installation (three-repo GitHub sync on Barret's machine)
  remains a personal sidecar and evaluation vehicle, not program
  infrastructure. It currently runs a locally patched binary; once
  kenn-io/kata ships the pagination fix, `kata update` restores a stock
  binary.
- Revisit trigger: if the program starts dispatching per-issue work to
  multiple agents or reviewers and needs ready-work selection with
  dependency ordering, evaluate Option C (kata as work queue) against
  building a small `next` primitive natively — a new decision record, not
  an edit to this one.
