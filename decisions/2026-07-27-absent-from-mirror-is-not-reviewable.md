# A proposal whose issue is absent from the mirror is not reviewable

**Date:** 2026-07-27
**Status:** Decided
**Related:** `docs/superpowers/specs/2026-07-27-transfer-suggestion-design.md` (the transfer-suggestion work that introduced sync reconciliation); `docs/superpowers/specs/2026-07-01-plan-3-review-queue-design.md`, whose opposite rule this decision reverses

## Context

The review app shows a human a queue of triage proposals, each naming one issue
by repository and number. Before a proposal is offered, the queue loader
(`review_queue.load_undecided`) consults the local SQLite mirror to check the
issue is still worth a decision.

When the review queue was first designed, the mirror had exactly one way for an
issue's row to be missing: it had never been synced, or the sync was behind. The
mirror was append-and-update only — rows were upserted and never deleted — so a
missing row carried no information about GitHub at all. The queue design
therefore ruled that a proposal whose issue was absent from the mirror should
**still be shown**, on the grounds that absence isn't evidence the issue is
closed; only a mirrored row saying `state != OPEN` was treated as a reason to
drop a proposal.

That premise no longer holds. The transfer-suggestion work added a
reconciliation step to sync: after an **exhaustive** walk of a repository's
issues (`sync --full`, which pages the whole connection rather than stopping at
the stored cursor), any mirrored issue GitHub did not list is deleted from the
mirror along with its comments and its embedding. It exists because a
transferred issue leaves its source repository entirely — GitHub reports no
state change on the old number, it simply stops being there — and a ghost row
keeps pairing with the transferred copy as a duplicate candidate forever.

Reconciliation only deletes when the walk is trustworthy. It refuses if the walk
saw fewer issues than GitHub itself reported for the connection (unstable
`UPDATED_AT DESC` pagination can skip a live issue that receives a comment
mid-walk), and it refuses if the walk returned no issues at all while the mirror
holds some (far more likely a permissions or API problem than a repository that
genuinely lost every issue).

So the meaning of a missing row has changed: it is now either "never synced" —
as before — or "GitHub no longer lists this issue in this repository". The queue
has to pick a reading.

## Options considered

### A. Treat absence as non-reviewable and drop the proposal — chosen

**Pros**

- Matches what absence now most often means. In a mirror that is kept current
  (the steady-state loop syncs before it analyzes), a row is missing because
  reconciliation retired it, which means GitHub no longer has that issue at
  that number. A proposal about it is moot: its labels cannot be applied to it,
  and any comment would land nowhere or, worse, on an unrelated issue if the
  number were ever reused.
- Protects the reviewer from decisions that cannot execute. The executor
  freshness-checks each issue before mutating and would fail the apply anyway;
  dropping the proposal spends no human attention on a decision destined to
  error out.
- Symmetric with the closed-issue rule that already existed: both are "the
  mirror says this issue is not in a state a proposal can act on".

**Cons**

- **A stale or partial mirror silently shortens the queue.** If someone reviews
  without syncing, or a sync was interrupted before a repository was walked,
  real reviewable proposals vanish from the queue rather than appearing. The
  failure is quiet by nature: an empty queue looks like success.
- Conflates two causes ("never synced" and "retired") that the mirror cannot
  distinguish from a missing row alone. Distinguishing them would need a
  tombstone — a new table or column recording retirements — which is more
  schema and more state to keep honest.

### B. Keep showing the proposal (the original review-queue rule)

**Pros**

- Fails toward showing work rather than hiding it: no reviewable proposal is
  ever suppressed by a mirror problem.
- Needs no change at all; it is the behaviour already specified.

**Cons**

- Offers decisions that provably cannot be applied. Post-reconciliation, the
  common case for a missing row is a transferred or deleted issue, and the
  review app can show the human almost nothing about it — no title, no body, no
  comments, only "(not found in mirror)" — which is precisely the condition
  under which a human decision is least informed.
- For `close-duplicate` and `link-duplicate` it is actively unsafe: approving
  one posts a public comment naming an issue that no longer exists.

### C. Record tombstones and distinguish "retired" from "never synced"

**Pros**

- Exactly correct: retired issues drop out of the queue, never-synced ones stay
  in it and surface as a mirror-freshness problem instead.

**Cons**

- Needs a schema change to carry retirements, and every consumer of the mirror
  then has three states to reason about instead of two. The transfer-suggestion
  work was explicitly constrained to add no column and no schema version, and
  nothing observed so far justifies paying that cost.

## Decision

**Absence from the mirror is treated as non-reviewable (Option A).** The
deciding factor: reconciliation deletes a row *only* for an issue GitHub
declined to list in an exhaustive, count-verified walk, so absence has stopped
being an unknown and become a positive signal. Acting on it is what keeps
retired issues out of the reviewer's queue, out of duplicate candidacy, and out
of the executor's write path.

The chosen option's real cost — a stale mirror quietly shortening the queue — is
mitigated rather than eliminated. `load_undecided` counts the proposals it drops
for this reason and, if any, logs one aggregate warning that names the count and
the remedy: run `triage-verse sync --full`, and anything still missing afterward
refers to an issue GitHub no longer has. That converts a silently short queue
into a visible, actionable message, which is the part that actually bites.

## Consequences

- `review_queue.load_undecided` drops proposals whose issue has no mirror row,
  and emits the aggregate warning described above. A reviewer who sees it should
  sync before trusting the queue's length.
- The review-queue design document that specified the opposite rule stays as
  written; it records what was decided when the mirror could not delete rows.
  This record supersedes that rule, not that document.
- Revisit trigger: if a short queue caused by a stale mirror is ever mistaken
  for a drained queue in practice, or if operators need to tell "retired" from
  "never synced" per proposal, evaluate Option C (tombstones) as a new decision
  record rather than an edit to this one.
