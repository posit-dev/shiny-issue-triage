# Decision attribution: `decided_by` + `reason`

**Issue:** posit-dev/shiny-issue-triage#40
**Date:** 2026-07-27
**Status:** Approved

## Problem

Every human review decision written by `src/triage_verse/decisions.py` records the
proposal, verdict, and timestamp — but **not who decided it, or why**:

```python
{ "id", "proposal_id", "repo", "issue", "action", "params",
  "verdict", "confidence", "decided_at" }   # no actor, no reason
```

Today that is tolerable because a single human (the maintainer) reviews
everything. But the program's trajectory is toward more automation and possibly
more reviewers. Months from now, when someone asks "why was this issue closed as
a duplicate?", the decision log should answer with **who** decided and a **short
justification** — not just `verdict: rejected`. Capturing actor attribution now
is a small, append-only schema addition; retrofitting it after months of
undocumented decisions is an archaeology problem with no source to recover from.

### What already exists

`decided_by` is already a de-facto field, half-wired:

- `execute --auto` writes `decided_by: "autonomy"` on synthetic auto-approvals
  (`src/triage_verse/executor.py:368`).
- The audit-reject path writes `decided_by: "human"`
  (`src/triage_verse/review_app/app.py:268`).
- `src/triage_verse/autonomy.py` already reads the field: it measures human
  precision over decisions where `decided_by != "autonomy"`
  (`autonomy.py:20,37`).

The gap is that the two **main** human review paths never stamp it:
`on_decide` and `approve_visible`, both routed through `decisions.record()`
(`app.py:566,792`). Those produce records with no actor and no reason. There is
also no `reason` field anywhere.

### What "reject" means (context for the reason field)

The review verdicts are approve / edit / reject / skip. Reject is defined by:

- **Terminal** — `review_queue.py:87` puts `rejected` in `TERMINAL_VERDICTS`, so a
  rejected proposal leaves the queue permanently and never bounces back.
  (Contrast with `skip`/"not now", which is deferred and stays in the queue.)
- **Not executable** — `executor.py:22` lists only `{approved, edited,
  auto-approved}` in `EXECUTABLE_VERDICTS`, so `execute` never applies a rejected
  proposal.
- **Counts against autonomy** — `autonomy.py:6-7` treats `rejected` as a
  precision `_FAILURE` for that proposal's category, which can block or demote
  autonomy graduation.

So a reject is the "the agent got this one wrong" signal, and it is exactly where
the "why" is least recoverable later — which is why the reason field targets it.

## Goal

Record, on every human review decision, **who** decided (`decided_by`) and,
where a human states it, **why** (`reason`). Keep the fast keyboard-review
workflow fast, and require no data migration.

## Non-goals

- **Evidence links** (commit SHAs, à la `kata close --commit`) — not meaningful
  until the apply stage writes back to GitHub. Out of scope; revisit then.
- **Changing the automated actor scheme.** `execute --auto` keeps writing
  `"autonomy"`; `autonomy.py`'s precision filter and historical decision data are
  untouched. This issue is about the human review paths, which are the actual gap.
- **Any change to the decision JSONL storage mechanism** (`jsonl_log.append_weekly`)
  or to how the review queue collapses decisions to a latest verdict.

## Design

### 1. Record schema — `decisions.record()`

New signature:

```python
def record(
    proposal: dict,
    verdict: str,
    *,
    params: dict | None = None,
    decided_by: str,
    reason: str | None = None,
) -> dict:
```

- `decided_by` is a **required keyword argument** — every call site must supply
  an actor, so a new call site fails loudly rather than silently dropping
  attribution. It is always written to the record.
- `reason` is written **only when it is a non-empty string**. Empty/None reasons
  are omitted entirely, keeping records lean; all readers already access these
  fields defensively (`dict.get`), so absence is valid.
- No migration. Old records missing `decided_by`/`reason` remain valid; readers
  tolerate their absence (already true today).

The `decided_by` value scheme:

- **Human review paths** → the resolved bare GitHub login (e.g. `barret`).
- **Automated `execute --auto`** → `"autonomy"` (unchanged).

Human logins are still `!= "autonomy"`, so `autonomy.py`'s human-precision filter
behaves identically to today (where human records simply lacked the field).

### 2. Actor resolution — `current_actor()` in `decisions.py`

A new module-level helper resolves the local reviewer identity once per process:

```python
@functools.cache
def current_actor() -> str:
    # 1. gh api user --jq .login   (via gh.run_gh; a plain REST GET read —
    #    passes the egress guard, confirmed against gh.classify_gh_call)
    # 2. os.environ["USER"]  fallback
    # 3. "unknown"           final fallback
```

- Resolution is cached (`functools.cache`) so the network call happens at most
  once per process, not per decision.
- Any failure of the `gh` call (non-zero exit, empty login, `EgressRefused`,
  network error) falls through to `$USER`, then `"unknown"`. `current_actor()`
  never raises.

### 3. Call-site wiring

- `on_decide` (`app.py:566`) and `approve_visible` (`app.py:792`) pass
  `decided_by=current_actor()`, plus `reason=` where the UI provides one
  (see §4).
- `app_audit_reject` (`app.py:268`) replaces its hardcoded `"human"` literal with
  `current_actor()`, so all human-originated decisions attribute uniformly.
- `execute --auto` (`executor.py:368`) is untouched.

### 4. Review-app UI — reason capture + verdict-aware seeding

The reviewing agent's own reasoning is already available on the proposal as
`rationale` (`proposals.py` `_rec`; populated for `close` from the close-candidate
rationale, empty string for plain label/priority proposals). Since the decision
handlers receive the full proposal, this is available client-side to seed the
reason input.

Approve and reject want *opposite* content in the reason field, so seeding is
**verdict-aware**:

- The agent's `rationale` is shown as **read-only context** next to the decision
  controls, so the reviewer always sees what the agent claimed.
- **Reject** (both the main-Queue row action and the drawer): reveals an inline
  text field plus a confirm control. `reason` is **optional** — an empty confirm
  works in one extra keystroke, so keyboard triage stays fast. The field starts
  **blank** with placeholder "why was this wrong?" (Seeding it with the agent's
  pro-rationale would be backwards — the reviewer is rejecting that argument.)
- **Approve / edit** (drawer): the `reason` field is **pre-filled with the
  proposal's `rationale`**, editable. Approving affirms the agent's reasoning, so
  its rationale *is* the "why."
- **Bulk "approve all visible"** (`approve_visible`): no per-item reason input;
  it leaves `reason` empty. It is a bulk affirmation, and prompting per item would
  defeat its purpose.

Keyboard flow for reject stays fast: the reject key reveals the (focused) reason
field; Enter confirms (empty allowed), Esc cancels. Exact key handling is pinned
down in the implementation plan.

### 5. Testing

- `decisions.record()`:
  - stamps `decided_by` on every record;
  - omits `reason` when it is empty/None; includes it verbatim when non-empty;
  - `decided_by` is required (calling without it is a `TypeError`).
- `decisions.current_actor()`:
  - returns the login from a mocked `gh.run_gh`;
  - falls back to `$USER` when the `gh` call fails or returns empty;
  - falls back to `"unknown"` when `$USER` is unset;
  - result is cached (one `gh.run_gh` call across repeated invocations).
- Review app:
  - reject records a `reason` when supplied and omits it when blank;
  - approve/edit seeds `reason` from the proposal `rationale`;
  - `decided_by` is present on records from `on_decide` / `approve_visible` /
    `app_audit_reject`.
- Regression: `autonomy.py` precision metrics are unchanged when human decisions
  now carry a login string (still `!= "autonomy"`).

## Files touched

- `src/triage_verse/decisions.py` — `record()` signature + `current_actor()`.
- `src/triage_verse/review_app/app.py` — wire `decided_by`/`reason` into
  `on_decide`, `approve_visible`, `app_audit_reject`; reason UI + rationale
  context; verdict-aware seeding.
- `tests/triage_verse/` — new/updated tests for the above.
- `execute --auto` (`executor.py`) — no change (documented here as deliberate).
