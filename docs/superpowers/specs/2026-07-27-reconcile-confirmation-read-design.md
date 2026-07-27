# Confirming each retirement candidate against GitHub

**Date:** 2026-07-27
**Status:** Approved

## Summary

Sync reconciliation deletes mirrored issues that a full walk did not return. It
currently protects that deletion with a whole-repo veto: if GitHub reports more
issues than the walk saw, nothing is retired. That veto misfires on exactly the
repositories reconciliation matters for, and it cannot detect the failure it was
written to catch.

This replaces the veto with a per-candidate confirmation read. Before deleting a
row, the reconciler asks GitHub about that specific issue and deletes only what
GitHub confirms is gone.

## Why the current guard fails

The walk pages an `UPDATED_AT DESC` connection. An issue that receives a comment
mid-walk, while sitting on a page not yet fetched, jumps ahead of the cursor and
is never returned — so it is absent from the walk while being perfectly alive.
The guard notices the shortfall and refuses to retire anything.

For a small repository that is a rare coincidence. For a large one it is close to
routine: `rstudio/shiny` holds roughly 2,780 issues, about 56 pages, over minutes
of wall time. A single comment landing in that window trips the veto. The
practical result is that reconciliation would rarely, perhaps never, run on the
repositories where transferred issues and their ghost rows actually accumulate.

The direction of failure is safe — nothing is deleted, and the refusal is logged
loudly — but a safety mechanism that disables the feature it guards is not doing
its job.

The veto is also blind to the case it most needs to catch. A *partially
truncated* response — pages returned, then a premature end — leaves the walk's
count consistent with a smaller repository, so the comparison passes and every
issue beyond the truncation point is deleted. The guard only ever catches the
shortfall it can see.

## The mechanism

GitHub redirects a transferred issue's old number to its new home, and the REST
response names that home. This was verified against live repositories rather
than assumed:

- `rstudio/thematic#145` returns 200 resolving to `rstudio/shiny#3902`.
  Independently corroborated: that issue's timeline carries a transfer event
  naming `rstudio/thematic` as its origin.
- `rstudio/thematic#44` returns 200 resolving to `rstudio/bslib#83`.
- `rstudio/thematic#133` through `#137` return 404.

So a single read per candidate distinguishes three outcomes:

| Response | Meaning | Action |
|---|---|---|
| 404 | the issue is genuinely gone | retire |
| 200, and the response's repository differs from the queried one | transferred away | retire |
| 200, and the repository matches | still live — the walk simply missed it | keep |
| the read fails for any other reason | unknown | keep |

The last row matters as much as the others. A network error, a rate limit, or an
unparseable response must never be read as absence, so anything other than a
confident 404 or a confident cross-repo redirect leaves the row alone.

This is strictly stronger than the count comparison. Every deletion is now
justified by a direct statement from GitHub about that specific issue, which also
closes the partial-truncation hole: a truncated page yields candidates that are
still live, and each one is individually refused.

## Cost

Confirmation reads scale with candidates, not with repository size, and
candidates are rare. `rstudio/thematic` has 165 issue and PR numbers across its
whole history and 7 gaps in that sequence — so a full sync there would spend
about 7 extra reads. A healthy repository with no candidates spends none.

For comparison, the active four-repo pilot is a roughly 30-request full sync, and
the full 41-repo fleet about 767. A handful of confirmation reads is noise
against either.

## Refusing implausible candidate sets

A per-candidate read makes every individual deletion safe, but it does not by
itself bound how much work a badly broken walk can generate. If some systemic
problem caused a walk to return only part of a large repository, the reconciler
would dutifully confirm hundreds of candidates one at a time, correctly refusing
each — a very slow, very loud sync that achieves nothing.

So a threshold stays: when candidates exceed **10% of the mirrored non-PR rows
for that repository**, the reconciler retires nothing and logs the refusal. A
genuine batch of transfers is a handful of issues; losing more than a tenth of a
repository at once is a signal about the walk, not about the issues.

This is deliberately a whole-repo veto, the same shape as the mechanism being
removed. The difference is where it sits: the old veto fired on a *shortfall in
the walk*, which is routine on a busy repository, whereas this one fires on
*implausible mass absence*, which is not. Reconciliation on a big active repo now
proceeds normally and confirms its handful of candidates.

The existing empty-response refusal is kept as a cheap special case — a walk that
returned nothing at all while the mirror holds rows needs no candidate
enumeration to be recognised as broken.

## What happens to the count

`totalCount` stays in the query and stays in the log, but stops being a veto.
When the walk saw fewer issues than GitHub reported, that is worth recording,
because it explains *why* candidates exist and is the first thing an operator
would want when investigating an unexpected retirement. It is diagnostic context,
not a decision.

## Logging

Reconciliation already announces itself on every run, including when it retires
nothing. The new distinctions are worth surfacing at the same level:

- how many candidates were considered, and how many were confirmed gone;
- for each retirement, whether it was a 404 or a transfer, and for a transfer,
  where the issue went;
- every candidate kept, with the reason it was kept — a live issue the walk
  missed, or a read that failed.

A retirement is the one destructive thing sync does, so an operator reading the
log alone should be able to say exactly what was deleted and on what evidence.

## Testing

- A candidate GitHub reports as 404 is retired.
- A candidate that redirects to a different repository is retired, and the
  destination appears in the log.
- A candidate that returns 200 in its own repository is kept, even though the
  walk did not return it. This is the live-issue-missed-by-pagination case, and
  it is the reason the whole change exists.
- A candidate whose confirmation read raises is kept.
- Candidates exceeding 10% of the repository's mirrored rows cause a whole-repo
  refusal with nothing retired.
- A short walk no longer blocks retirement, but does log the shortfall.
- An empty response with mirrored rows present still refuses.
- Incremental syncs still never reconcile.

## Explicitly out of scope

- **Recording where a transferred issue went.** The confirmation read reveals the
  destination, and persisting it would let the pipeline skip re-analyzing a
  transferred issue in its new repository, and let the review app's transfer
  worklist resolve itself. That is a feature with its own design questions —
  where the mapping lives, whether classifications should follow, how transfer
  chains resolve — and is tracked separately. Here the destination is used only
  to justify a retirement and to make the log intelligible.
- **Reconciling on incremental syncs.** Unchanged: an incremental walk stops at
  its cursor, so absence from it carries no information.
- **Reconciling pull requests.** Unchanged; issues only.
