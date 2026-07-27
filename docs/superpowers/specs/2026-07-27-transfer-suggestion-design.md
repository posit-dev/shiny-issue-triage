# Implementing `cross_repo_option`: link, and suggest-transfer

**Date:** 2026-07-27
**Status:** Approved

## Summary

The dedup stage already asks the model, for every duplicate pair that spans two
repositories, what should happen to it:
`cross_repo_option: "close-and-link" | "transfer" | "keep-both-link" | null`.
The value is stored, threaded into proposal params, and shown to reviewers — but
no code has ever branched on it. Every cross-repo duplicate gets the same
treatment.

This change makes the field mean something:

- **`close-and-link`** keeps today's behavior — comment, then close as
  `not planned`.
- **`keep-both-link`** posts a comment linking the pair and closes nothing.
- **`transfer`** flags the issue for a human to move, and routes it to a
  worklist in the review app.

The system **never performs a transfer**. The egress guard's allowlists are not
modified, so a `transferIssue` mutation still fails closed. The only new
GitHub-facing capability is a comment that doesn't close, which the pipeline can
already express.

## The problem being fixed

Three things are wrong with the field as it stands, and all three are addressed
here.

**The model is never told what the values mean.** The only instruction sent with
a dedup request is "Decide whether A and B are duplicate, related, or distinct."
The three option names appear in no prompt, rubric, or taxonomy — the sole other
occurrence of the word "transfer" anywhere in the repository is the safety rubric
line forbidding auto-transfer. A model choosing from an undefined enum produces
noise, so **defining the options in the prompt is a prerequisite**, not a polish
step. Branching on today's values without doing that would mean acting on
guesses.

**One action name describes three outcomes.** The value rides on a
`close-duplicate` proposal. `keep-both-link` means *do not close*, which
contradicts the action it decorates, and `transfer` means *do not close either*.
A reviewer approving something labelled `close-duplicate` should be able to
trust that it closes.

**Nothing surfaces the human follow-up.** A `transfer` outcome requires a person
to act on GitHub. Nothing in the review app tells them to, or tracks whether they
did.

## Design

### Defining the options to the model

The dedup request gains explicit definitions of the three options and states that
the field applies only to pairs spanning two repositories, with `null` for
same-repo pairs. The definitions describe the *situation* each option fits:

- `close-and-link` — the duplicate adds nothing the canonical issue lacks; the
  discussion should consolidate there.
- `keep-both-link` — both issues have standing in their own repositories (for
  example, the same defect needs tracking separately in an R and a Python
  package), and neither should be closed.
- `transfer` — the issue is in the wrong repository, and its content belongs in
  the canonical issue's repository rather than being discarded.

### Mapping the option to distinct actions

`proposals.build` maps the option to a **different action per outcome**, so an
action name always tells the truth about what approving it does:

| `cross_repo_option` | action emitted | executor plans |
|---|---|---|
| `close-and-link`, or `null` | `close-duplicate` | comment from the cross-repo template, then close as `not planned` — unchanged from today |
| `keep-both-link` | `link-duplicate` | comment linking the canonical issue; **no close** |
| `transfer` | `suggest-transfer` | add the `wrong location` label; **no close, no transfer** |

Same-repo pairs are untouched: they continue to emit `close-duplicate` and close
with GitHub's native duplicate linkage. The option is ignored for them.

Because the option is model output, it is validated at proposal-build time. A
value of `transfer` or `keep-both-link` on a same-repo pair, or any unrecognized
string, degrades to `close-and-link`.

That fallback closes an issue, which is not the least destructive of the three
outcomes — `keep-both-link` is. It is chosen anyway because it preserves exactly
today's behavior for exactly today's inputs, so this change cannot alter what
happens to a pair whose option is absent or malformed. The risk is bounded by
review: `close-duplicate` is high-stakes, so a human must work through the
evidence before anything closes.

### Stakes and autonomy

Only `close-duplicate` remains in `HIGH_STAKES_ACTIONS`, since it is the only one
that closes an issue. `link-duplicate` (a comment) and `suggest-transfer` (a
label) are ordinary proposals, reviewable with quick-approve and bulk-approve.

Neither new action is added to `autonomy.ELIGIBLE`, so neither can graduate to
auto-approval, with a test asserting their absence. For `suggest-transfer` this
is belt-and-braces rather than the real safeguard: even auto-approved, it could
only apply a label, because the executor has no transfer capability at all.

### Destination for a transfer

No new field is required. For a cross-repo pair the destination *is* the
canonical issue's repository, which is already present in the proposal's
`canonical` param.

### Templates

`close-duplicate-cross-repo.md` is unchanged. A new `link-duplicate.md` notes
that the two issues track the same underlying problem in different repositories,
links the sibling, and states that both are staying open deliberately so neither
reporter thinks their report was dismissed.

Like every other action, `link-duplicate` comments only on the proposal's own
issue, not on the sibling. A proposal targets one issue, and the pipeline holds
no mandate to write to the canonical issue's repository on the strength of a
verdict about this one. If the pair warrants a note on both sides, that is two
proposals, and the dedup stage does not currently emit the reciprocal one.

`suggest-transfer` posts no comment at all — see below.

### Why `suggest-transfer` posts no comment

The destination is information for the maintainer, and it lives in the review app
where they act on it. A public "we think this belongs elsewhere" comment would go
stale the moment someone performed the transfer, with nothing to retract it.
Approving a transfer suggestion therefore produces no public-facing write beyond
a label.

## User interface

Because the system never performs the transfer, the UI is what makes the
`transfer` outcome real. A captured suggestion nobody can act on is the same dead
end as the field being unread.

### Reading proposals

`_row_label` renders params through `str()`, which would show a raw Python dict.
The two new actions get readable renderings — a linked sibling reference, and a
`→ owner/name` destination — mirroring the special-casing that already exists for
`close-duplicate` params.

Queue rows for `suggest-transfer` carry a badge showing the destination, reusing
the pill styling already used for the `stale` and `not now` markers, so
suggestions stay scannable in a long queue.

The drawer gains a **Suggested destination** block for `suggest-transfer`: the
target repository, a link to it, and explicit wording that the transfer must be
done manually and that approving applies only the `wrong location` label. The
drawer's existing "Open on GitHub ↗" link is the route to doing it. GitHub exposes
no deep link for the transfer dialog — it lives in the issue sidebar — so the
issue link is the closest available target.

### The Transfers panel

Approving a `suggest-transfer` applies a label; it does not move the issue. Under
the existing queue model an approved proposal leaves the queue, so without
further work every approved suggestion would vanish with nobody having
transferred anything — recording the judgment and then losing it, which is the
failure this change exists to fix.

The review app therefore gains a **Transfers** nav panel, following the
established pattern of the Skipped panel: a worklist of approved
`suggest-transfer` proposals, each showing the source issue, the destination
repository, and a link to the issue on GitHub, with a **Mark transferred**
control that records completion so the entry leaves the list.

Completion is recorded explicitly rather than inferred. Detecting a transfer from
the mirror is unreliable — a transferred issue's original number redirects rather
than disappearing, and it reappears in the destination repository under a new
number, so no clean signal attributes the move to a given suggestion. Explicit
marking is honest about what is actually known. Inferring completion during sync
is a reasonable later refinement, not a prerequisite.

## Testing

- The dedup prompt states all three option definitions.
- `proposals.build` maps each option to its action, and degrades an unrecognized
  value, a same-repo `transfer`, and a same-repo `keep-both-link` to
  `close-duplicate`.
- Same-repo duplicate execution is unchanged: comment plus native duplicate
  close.
- `close-and-link` execution is unchanged: comment plus `not planned` close.
- `link-duplicate` plans a comment and **no** close mutation.
- `suggest-transfer` plans a lone `add-label` and never a transfer mutation.
- `close-duplicate` is high-stakes; `link-duplicate` and `suggest-transfer` are
  not.
- Neither new action appears in `autonomy.ELIGIBLE`.
- Regression: `gh_mutation("transferIssue", …)` is refused by the egress guard.
- The Transfers panel lists approved suggestions and drops an entry once marked
  transferred.

## Explicitly out of scope

- **Performing transfers.** Rejected. A transfer assigns a new issue number in
  the destination repository, and transferring back yields a third number rather
  than restoring the original — content-reversible, but not
  identity-reversible. It would also change both halves of the mirror's
  `(repo, number)` primary key, orphaning every proposal, decision, dedup verdict,
  and result row referencing the issue, including the proposal that authorized
  the move. It would further require new egress-guard allowlist entries and a new
  undo verb.
- **Suggesting a transfer for an issue that duplicates nothing.** Coverage here
  is limited to issues the dedup stage paired across repositories, because
  `cross_repo_option` only exists on a pair verdict. A plainly misfiled issue
  that duplicates nothing gets no suggestion. Catching those needs a per-issue
  signal from the classification stage — a reasonable follow-up, and a separate
  design.
- **Amending the safety rubric.** Its prohibition on auto-transfer remains
  accurate, and this design complies with it.
- **Transfers or duplicate handling for pull requests.** Issues only.
