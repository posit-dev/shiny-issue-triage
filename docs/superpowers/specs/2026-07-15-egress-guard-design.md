# Credential egress guard for GitHub writes — design

**Date:** 2026-07-15
**Issue:** #41 — "P3 apply stage: credential egress guard for GitHub writes"

## Problem

triage-verse runs LLM-generated proposals with `gh` credentials, and the apply
stage (`executor.py`) writes to GitHub: it adds/removes labels, posts comments,
and closes issues. Today nothing *structurally* prevents a misfired prompt, a
bug, or a compromised dependency from turning that credential against the wrong
repository, the wrong endpoint, or the wrong verb. Every GitHub call already
funnels through one wrapper — `run_gh(args)` in `gh.py` — but that wrapper does
no authorization: it forwards whatever argument vector it is handed to the `gh`
CLI. The blast radius of "the prompt misfired" should be a single logged
refusal, not an incident.

The goal is a thin guard layer that all GitHub **write** calls pass through,
enforced at that single choke point so new call sites cannot bypass it by
accident, that **fails closed**: when the guard cannot confidently prove a call
is a safe read or an explicitly allowed write, it refuses the call before the
credential leaves the process.

## Prior art and the lesson we take from it

kata (katatracker.com) wraps its GitHub HTTP transport in an origin guard:
credentials are attached only to requests whose URL matches the bound repo's API
paths, and everything else is refused before the request leaves the process.
When kata hit a bug in that guard, it failed *closed* — it blocked a legitimate
pagination request rather than ever risking sending the token elsewhere. That is
the correct failure mode for this class of tool, and it is the failure mode this
design adopts.

kata's specific bug — GitHub pagination `Link` headers use numeric
`/repositories/{id}/...` URLs that did not match the guard's `owner/name`
patterns — is a reminder to **canonicalize identifiers before matching**. This
design carries that lesson into its repo check even though our transport differs
(see "Repo scoping").

## Why our transport differs from kata's

kata guards raw HTTP requests, so it can match a request URL against allowed
repo paths. Our choke point is different: `run_gh(args)` receives a **`gh` CLI
argument vector**, not a URL, and those vectors come in several unrelated
shapes — porcelain subcommands (`issue edit`, `issue close`), REST calls
(`api repos/{repo}/...`), and GraphQL calls (`api graphql` with an opaque query
body). A guard that tried to URL-match each shape would be brittle, and one of
those shapes — GraphQL — has no repo in the URL at all: every mutation POSTs to
the same `/graphql` endpoint.

Rather than teach the guard every shape, this design **unifies all
issue-mutating writes onto a single transport (GraphQL)** and guards that one
shape, while the classifier refuses any *other* write shape by default.

## Design overview

Three parts:

1. **Transport unification** — every issue-mutating write becomes a GraphQL
   mutation dispatched through one helper. Porcelain and REST write shapes
   disappear from the codebase's write paths.
2. **The guard in `run_gh`** — a fail-closed classifier that runs before every
   subprocess launch, categorizing each call as an allowed read, an allowed
   (guarded) mutation, an allowed trusted-infra write, or a refusal.
3. **Two allowlists** — the set of permitted GraphQL mutation fields (the
   "verb" allowlist) and the set of active repositories from
   `config/repos.yaml` (the "repo" allowlist).

### Transport unification

A new helper in `gh.py`:

```
gh_mutation(operation: str, query: str, variables: dict, *, repos: list[str]) -> dict
```

builds the GraphQL payload and calls `run_gh(["api", "graphql", ...])`, passing
the caller-declared `operation` name and target `repos` through to the guard
(see "How the declared operation and repos reach the guard"). `operation` is our
own readable operation name — it is the policy unit the guard checks, and it
appears in log lines; `repos` is the list of repositories the mutation targets.

**Operation names vs GraphQL fields.** We name operations *ourselves*, at the
issue/PR granularity we want to reason about, and each operation compiles to a
GitHub GraphQL mutation field. This matters for labels: GitHub's schema calls
the label mutations `addLabelsToLabelable` / `removeLabelsFromLabelable` —
generic fields shared by *both* issues and pull requests (a "Labelable" is
anything labelable). So `addLabelsToIssue` and `addLabelsToPR` both compile to
the same `addLabelsToLabelable` field on the wire; the issue-vs-PR distinction
lives only in our operation name, not in the query. Keeping our own operation
layer lets the guard allow issue-labeling while independently denying
PR-labeling even though the underlying field is identical.

`executor.py`'s `_apply_mutation` and `_apply_reverse` are rewritten from their
current porcelain + REST forms to these operations:

| Write | Operation name | GraphQL mutation field |
| --- | --- | --- |
| add-label | `addLabelsToIssue` | `addLabelsToLabelable` |
| remove-label | `removeLabelsFromIssue` | `removeLabelsFromLabelable` |
| comment | `addComment` | `addComment` |
| close (completed / not-planned) | `closeIssue` | `closeIssue` (`stateReason: COMPLETED` / `NOT_PLANNED`) |
| close-duplicate | `closeIssue` | `closeIssue` (`stateReason: DUPLICATE`, `duplicateIssueId:`) |
| reopen | `reopenIssue` | `reopenIssue` |
| delete-comment (undo) | `deleteIssueComment` | `deleteIssueComment` |

`tier2.request_fix` and `reprex.request_reprex` — which today add a label via
`issue edit --add-label` — are migrated to the same helper (operation
`addLabelsToIssue`). After this change there is exactly one write shape in the
codebase.

**Reserved for later — PR labeling.** Nothing in the codebase labels pull
requests today, so no PR operation is wired up yet. When the apply stage begins
touching PRs, it will add operations `addLabelsToPR` / `removeLabelsFromPR` to
the operation allowlist; both compile to the same `addLabelsToLabelable` /
`removeLabelsFromLabelable` fields (differing only in the labelable node ID they
target). They are named now so the operation allowlist has room for them without
a redesign.

**Node IDs.** GraphQL mutations operate on node IDs, not `owner/repo/number`
tuples:

- **Issue node IDs** are already available: `executor._fetch_issue` reads the
  issue over REST and the response includes `node_id`. No new call is needed.
- **Label node IDs** are *not* carried by name in GraphQL label mutations, so a
  small REST read — `GET repos/{repo}/labels/{name}` — resolves name → node ID
  before the mutation. This is a read and passes the guard unchanged. (REST
  label writes take names directly, which GraphQL cannot; this resolution step
  is the modest cost of unifying on GraphQL, accepted deliberately.)

### The guard in `run_gh`

Before launching the subprocess, `run_gh` classifies the call. The classifier is
**fail-closed**: it *allows* only calls it can positively recognize, and refuses
everything else.

**Allowed — safe read.** Any of:

- `api <path>` with no HTTP-method flag (`-X`/`--method`) and no body flag
  (`-f`/`-F`/`--field`/`--raw-field`/`--input`) — a REST `GET`.
- `api graphql` whose query body contains **no** `mutation` operation — a
  GraphQL read (this is what `sync.py` uses).
- Read-only porcelain the codebase actually uses (e.g. `repo view`,
  `release view`, `release list`).

**Allowed — guarded mutation.** `api graphql` whose body contains a `mutation`.
Two independent checks, both must pass (defense in depth):

- **Operation-name check (policy unit).** The caller-declared `operation` must
  be in `ALLOWED_OPERATIONS` — our readable names (`addLabelsToIssue`,
  `removeLabelsFromIssue`, `addComment`, `closeIssue`, `reopenIssue`,
  `deleteIssueComment`; PR variants reserved). This is where issue-vs-PR
  granularity is enforced. A missing or unrecognized operation → refuse.
- **Wire-field check (fail-closed backstop).** Parse **every** top-level
  mutation field from the query string (the query is one we construct, so the
  field names are reliably present as literal text) and require **every** field
  to be in `ALLOWED_MUTATION_FIELDS` — the real GraphQL field names
  (`addLabelsToLabelable`, `removeLabelsFromLabelable`, `addComment`,
  `closeIssue`, `reopenIssue`, `deleteIssueComment`). Any unrecognized field →
  refuse. This catches a mutation whose declared operation lies about its body.
- **Repo check.** The caller-declared `repos` must be non-empty and every entry
  must be in the active-repo allowlist (see "Repo scoping"). Otherwise → refuse.

**Allowed — trusted infra.** `release create` / `release upload` /
`release delete` are the state-bus snapshot mechanism (`snapshot.py`): they
write the SQLite mirror as a Release asset on the **hub repo**, are not
LLM-driven, and cannot touch a target issue. The guard recognizes `release`
subcommands as a bounded, logged trusted-infra category, restricted to the hub
repository. Every other write shape stays refused.

**Refused — everything else.** Including, explicitly:

- Porcelain writes: `issue edit`, `issue close`, `issue reopen`,
  `issue comment`, `issue create`, `issue delete`, and any other mutating
  porcelain subcommand.
- REST writes: `api` with `-X POST/PATCH/PUT/DELETE` or any body flag on a
  non-`graphql` path.
- Any `gh` invocation shape the classifier does not positively recognize.

A refusal raises `EgressRefused` (a subclass of the existing `GhError`, so
current `except GhError` handling still catches it) with a message naming the
refused operation and, where known, the repo. Every refusal is logged. This is
the mechanism that makes a *new* write call site fail closed: unless it goes
through `gh_mutation`, its shape is unrecognized and the guard refuses it.

### Repo scoping

GraphQL gives us no repo in the URL, so the guard cannot verify the target repo
from the wire. Instead, the write path **declares** its target repos
(`gh_mutation(..., repos=[...])`) and the guard checks each declared repo
against the active repositories in `config/repos.yaml`. This is an honest
weakening of kata's URL-matching model: it trusts the caller's declaration. Two
things bound that trust:

- The single choke point — `gh_mutation` is the only way to emit a mutation the
  guard will allow, and it *requires* a non-empty `repos` argument.
- The wire-field allowlist — even a mis-declared call can only invoke one of the
  known issue-mutation fields, never an org-, user-, or repo-scoped destructive
  operation.

Applying kata's canonicalization lesson: both the declared repos and the
`repos.yaml` entries are canonicalized (trimmed, case-folded `owner/name`)
before comparison, so a casing or whitespace difference cannot cause a
false match *or* a false refusal.

### How the declared operation and repos reach the guard

`run_gh` gains optional keyword arguments carrying the declared `operation` name
and target repos (populated only by `gh_mutation`). The active-repo allowlist is
loaded via `config.load_repos("config/repos.yaml")` and cached in the guard.
`ALLOWED_OPERATIONS`, `ALLOWED_MUTATION_FIELDS`, and the hub-repo identity are
constants/config in `gh.py`. No call site other than `gh_mutation` sets the
declared-operation/declared-repos arguments, so no call site can talk its way
past the mutation branch of the classifier.

## Components and boundaries

- **`gh.py`** — owns the guard (classifier + allowlists + `EgressRefused`) and
  the `gh_mutation` helper. This is the trust boundary; it depends on
  `config.load_repos`.
- **`executor.py`** — constructs GraphQL mutations for each decision/undo and
  dispatches them via `gh_mutation`. No longer builds porcelain/REST writes.
- **`tier2.py` / `reprex.py`** — add a label via `gh_mutation` with operation
  `addLabelsToIssue`.
- **`snapshot.py`** — unchanged in behavior; its `release` writes are recognized
  by the guard's trusted-infra category.
- **`sync.py`, `verify.py`, other readers** — unchanged; their reads pass the
  classifier's read branch.

## Error handling and failure modes

- **Unrecognized shape → refuse** (fail closed). A read shape the codebase adds
  later that the classifier does not recognize will be refused; the fix is to
  teach the classifier that read shape, not to loosen the default.
- **Mutation with an unknown field → refuse**, naming the field.
- **Mutation with a repo not in `repos.yaml` → refuse**, naming the repo.
- **Mutation with empty declared repos → refuse.**
- Refusals are not retried (they are not in `RETRYABLE_MARKERS`) and surface as
  `EgressRefused`; the executor records them as an `error` result like any other
  failed mutation, so a single misfire is one logged, recorded refusal.

## Testing

Unit tests (pytest), driven at the `gh.py` seam:

- **Classifier** — one case per shape: REST GET read → allow; GraphQL query
  read → allow; `repo view` / `release view` → allow; `release create/upload/delete`
  → allow (trusted infra); porcelain `issue edit`/`close`/`reopen` → refuse;
  `api -X POST` / body-flag on non-graphql path → refuse; unknown shape → refuse.
- **Operation-name check** — declared operation in `ALLOWED_OPERATIONS` → allow;
  missing or unknown operation → refuse; a reserved-but-unwired PR operation is
  handled per whether it has been added to the allowlist.
- **Wire-field parser** — single field allowed; multiple fields all allowed; a
  field not in `ALLOWED_MUTATION_FIELDS` → refuse; an aliased/unknown field
  mixed with an allowed one → refuse (all fields must pass).
- **Repo check** — declared repo in `repos.yaml` → allow; not in `repos.yaml` →
  refuse; empty declared repos → refuse; casing/whitespace canonicalization
  matches correctly.
- **Bypass guard** — a raw porcelain write submitted to `run_gh` is refused
  (asserts the choke point cannot be sidestepped).
- **Executor** — existing executor tests updated to assert the new GraphQL
  command shapes for each action and its undo, with `gh_mutation` exercised
  through a fake `run_gh`.

`make py-check` (ruff format + lint, pyright, pytest) is the gate.

## Logging

Following the codebase's logging-verbosity convention:

- Every refusal logs a clear line naming the refused operation and repo.
- `gh_mutation` logs the operation name and target repos before dispatch, so a
  tailed log shows exactly which repo each write touched.

## Out of scope

- **Read-side egress guarding.** Reads (`sync`) can adopt a repo check later;
  this change only guards writes, matching the issue's framing.
- **Changing which actions the apply stage supports.** The mutation-field
  allowlist mirrors exactly the operations the executor performs today; no new
  actions are added or removed.
- **Migrating `snapshot.py` off releases.** Its `release` writes are recognized,
  not reworked.
