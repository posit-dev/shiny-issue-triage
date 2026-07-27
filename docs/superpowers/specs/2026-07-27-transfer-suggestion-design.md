# Transfer suggestion, and retiring `cross_repo_option`

**Date:** 2026-07-27
**Status:** Approved

## Summary

Two related changes to how the pipeline handles issues that belong in a
different repository:

1. **Retire `cross_repo_option`** from the dedup schema, proposal params, and
   review-app rendering. The field is model output that the model was never told
   how to produce, and no code has ever branched on it.
2. **Add a transfer *suggestion*** — a new `suggest-transfer` proposal that
   names the repository an issue should live in. The system never performs the
   transfer. A human does it on GitHub, and the review app gives them a worklist
   for doing so.

The pipeline gains **no new ability to mutate GitHub**. The egress guard's
allowlists are unchanged, so an actual `transferIssue` mutation still fails
closed.

## Part 1 — Why `cross_repo_option` is being retired

The dedup stage's output schema asks the model for
`cross_repo_option: "close-and-link" | "transfer" | "keep-both-link" | null`.
It is stored in `dedup_verdicts.cross_repo_option`, threaded into
`close-duplicate` proposal params, and displayed to the reviewer. No code
branches on the value.

It is being removed rather than implemented, for four reasons:

**The model is never told what the values mean.** The only text sent with the
dedup request is "Decide whether A and B are duplicate, related, or distinct."
The three option names appear nowhere in any prompt, rubric, or taxonomy — the
sole other occurrence of the word "transfer" in the repository is the safety
rubric line forbidding auto-transfer. The model is choosing from an undefined
enum, so the stored values carry no reliable signal. Branching on them would
mean acting on noise.

**`keep-both-link` contradicts its own action.** The value only ever rides on a
`close-duplicate` proposal. "Keep both" means *do not close* — the option and
the action it decorates are incoherent.

**The current cross-repo behavior is a deliberate decision, not an oversight.**
The Plan 4 executor design states that cross-repo pairs fall back to a
`not planned` close plus the cross-repo comment template, because GitHub's
duplicate-close linkage is same-repo only. The executor implements exactly that.
There is no missing branch, only an unused field.

**"Wrong repository" is the wrong shape for dedup.** Belonging in another
repository is a property of a single issue, not of a duplicate pair. A pair
verdict is the wrong place to express it, which is part of why the option was
never actionable.

The `dedup_verdicts.cross_repo_option` **column stays**. This codebase retains
decision history deliberately, dropping a SQLite column requires a full table
rebuild in the migration path, and a column holding historical values costs
nothing. New rows will write `NULL`.

## Part 2 — Transfer suggestion

### Where it lives

The classification stage, not dedup. The label allowlist already contains
`wrong location`, so the classifier can already flag a misfiled issue — it just
cannot say where the issue belongs. This change completes that existing signal
with a destination.

### Schema and prompt

`CLASSIFY_SCHEMA` gains `suggested_repo`, a nullable string, added to the
`required` list to match the schema's existing style of requiring every field.

Unlike `cross_repo_option`, this field is **defined to the model**. The prompt
states what `suggested_repo` means and supplies the active repository list from
`config/repos.yaml`, so the model selects from a closed set rather than
inventing a name. Null means "this issue is in the right place", which is the
expected answer for the overwhelming majority of issues.

### Validation

Storage validates the value the way label output is already validated, reducing
it to `NULL` unless it both appears in the active-repos allowlist and differs
from the issue's own repository. A hallucinated or self-referential destination
therefore becomes "no suggestion" rather than a bad proposal.

This needs a `classifications.suggested_repo` column and a schema-version bump.

### Proposal and execution

A `suggest-transfer` proposal is emitted only when a validated non-null
`suggested_repo` survives, with params `{"suggested_repo": "owner/name"}`.

The executor plans exactly one mutation for it: **add the `wrong location`
label**. Nothing else. Specifically:

- **No `transferIssue` mutation.** The egress guard's operation and wire-field
  allowlists are not modified. A transfer attempt still fails closed, which is
  the property that makes this design safe to ship.
- **No public comment.** The destination is information for the maintainer, and
  it lives in the review app where they will act on it. A public "we think this
  belongs elsewhere" comment would go stale the instant someone transferred the
  issue, with nothing to retract it. Approving a suggestion produces no
  public-facing write beyond a label.
- **Undo keeps working.** A label add is already in undo's vocabulary, so the
  undo guarantee needs no carve-out.

Adding a label the issue already carries is a no-op on GitHub, so the proposal
is safe to apply alongside the classifier's normal label output.

### Autonomy

`suggest-transfer` is deliberately **not** added to `autonomy.ELIGIBLE`, so it
can never graduate to auto-approval. A test asserts its absence, so a later edit
cannot quietly promote it.

Note that this is belt-and-braces rather than the primary safeguard: even a
fully auto-approved `suggest-transfer` could only add a label, because the
executor has no transfer capability at all.

## Part 3 — User interface

Because the system never performs the transfer, the UI *is* the feature. A
captured suggestion that no human can act on is the same dead end as the field
being retired.

### Reading the suggestion

`_row_label` currently renders params via `str()`, which for this action would
show a raw Python dict. It gains a `suggest-transfer` case rendering the
destination in words (`→ posit-dev/py-shiny`), mirroring the special-casing that
already exists for `close-duplicate` params.

Queue rows for the action carry a distinct badge showing the destination, reusing
the pill styling already used for the `stale` and `not now` markers, so
suggestions are scannable in a long queue.

The drawer gains a **Suggested destination** block: the target repository, a link
to it, and explicit wording that the transfer must be performed manually on
GitHub and that approving applies only the `wrong location` label. The drawer's
existing "Open on GitHub ↗" link is the path to actually doing it. GitHub exposes
no deep link for the transfer dialog itself — it lives in the issue sidebar — so
the issue link is the closest available target.

### The workflow gap, and the Transfers panel

Approving a `suggest-transfer` applies a label; it does **not** move the issue.
Under the existing queue model an approved proposal leaves the queue, so without
further work every approved suggestion would disappear with nobody having
transferred anything. The suggestion would be recorded and then lost — the exact
failure mode this change exists to fix.

So the review app gains a **Transfers** nav panel: a worklist of approved
`suggest-transfer` proposals, following the established pattern of the existing
Skipped panel. Each entry shows the source issue, the suggested destination, and
a link to the issue on GitHub. A **Mark transferred** control records completion
so the entry leaves the list.

Completion is recorded explicitly rather than inferred. Detecting a transfer from
the mirror is unreliable: a transferred issue's original number redirects rather
than vanishing, and the issue reappears in the destination repository under a new
number, so there is no clean signal that a given suggestion was the cause.
Explicit marking is honest about what is known. Inferring completion from sync is
a reasonable later refinement, not a prerequisite.

## Testing

- `DEDUP_SCHEMA` no longer exposes `cross_repo_option`, and `close-duplicate`
  proposal params no longer carry it.
- Cross-repo duplicate execution is unchanged — still the comment plus
  `not planned` close.
- Classification storage nulls a `suggested_repo` that is unknown or equal to the
  issue's own repository, and preserves a valid one.
- `proposals.build` emits `suggest-transfer` only when a validated destination is
  present.
- The executor plans a lone `add-label` for `suggest-transfer` and never emits a
  transfer mutation.
- `suggest-transfer` is absent from `autonomy.ELIGIBLE`.
- Regression: `gh_mutation("transferIssue", …)` is refused by the egress guard.
- The Transfers panel lists approved suggestions and drops an entry once marked
  transferred.

## Explicitly out of scope

- **Performing transfers.** Rejected. A transfer assigns a new issue number in
  the destination repository, and transferring back yields a third number rather
  than restoring the original — so it is content-reversible but not
  identity-reversible. It would also change both halves of the mirror's
  `(repo, number)` primary key, orphaning every proposal, decision, dedup verdict,
  and result row that references the issue, including the proposal that
  authorized the move. Enabling it would additionally require new egress-guard
  allowlist entries and a new undo verb.
- **Amending the safety rubric.** Its prohibition on auto-transfer remains
  accurate and this design complies with it.
- **Dropping the `cross_repo_option` column.**
- **Suggesting transfers for pull requests.** Issues only.
