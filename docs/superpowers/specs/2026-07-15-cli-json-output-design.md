# Design: `--json` output mode on every CLI command

**Date:** 2026-07-15
**Issue:** #42 — CLI: `--json` output mode on every command
**Status:** Approved (pending spec review)

## Problem

The `triage-verse` CLI is human-first. Every command prints prose or tables to
stdout and returns an exit code; structured interchange happens through files
(`.data/proposals/`, decision JSONL, `analytics.json`). That works for the
interactive workflow but makes the CLI awkward to compose — for shell scripts,
for dashboards, and especially for coding agents, which are increasingly the
thing driving it. Consuming any command's outcome today means screen-scraping
prose or reading a side-file.

Prior art: kata (katatracker.com) treats agents as the primary consumer and
puts a global `--json` flag on every command, so any command's outcome is
machine-readable directly from stdout.

## Goal

Add a global `--json` flag to the `triage-verse` CLI. When set, every command
emits a single structured JSON document on stdout and nothing else; human
progress/log output moves to stderr; errors are structured; exit codes are
preserved. Applies to **all** subcommands in one pass so the CLI's behavior is
uniform (no half-migrated surface).

## Decisions

These were settled during brainstorming and drive the design:

1. **Scope:** every subcommand, now — not an incremental subset.
2. **Human/log stream in JSON mode:** redirected to **stderr**. stdout carries
   only the JSON document. The codebase's logging-visibility convention
   (stage markers, heartbeats) is preserved — it just moves to stderr under
   `--json`. It is not suppressed.
3. **Flag position:** accepted **either before or after** the subcommand —
   `triage-verse --json sync` and `triage-verse sync --json` are equivalent,
   including for nested subcommands (`analytics export --json`).
4. **Output shape:** a **consistent envelope**, the same object for every
   command. No per-command top-level shape, no JSON-vs-JSONL branching — list
   results live inside the envelope's `data` field as an array.
5. **`ok` semantics:** `ok` answers "did the command run to completion?", not
   "was the answer positive?". A command that ran fine but reports a negative
   domain result (a count mismatch, an execution with errors) is
   `{"ok": true, "exit_code": 1, "data": {...}}`. Only bad input and unexpected
   exceptions produce `{"ok": false, "error": ...}`. The numeric `exit_code` is
   always in the envelope so shell callers can still branch on it.

## The envelope

Every command, in JSON mode, emits exactly one JSON object as a single line on
stdout:

Success (including negative domain results):

```json
{"command": "verify-counts", "ok": true, "exit_code": 1, "data": {"reconciled": false, "...": "..."}}
```

Failure (bad input or unexpected exception):

```json
{"command": "sync", "ok": false, "exit_code": 1, "error": "rstudio/nonexistent is not in config/repos.yaml"}
```

- `command` — the full command path, including any subcommand, e.g. `"sync"`,
  `"analytics export"`, `"state push"`.
- `ok` — `true` if the command ran to completion, `false` on bad input or an
  unexpected exception.
- `exit_code` — the process exit code, always present.
- `data` — present when `ok` is `true`; the command-specific payload (object or
  array).
- `error` — present when `ok` is `false`; a human-readable message string.

## Mechanism

### Flag parsing (both positions)

A shared parent parser makes `--json` available after any subcommand; a
top-level definition makes it available before the subcommand:

- `common = argparse.ArgumentParser(add_help=False)` with
  `common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)`.
- Every subparser is created with `parents=[common]` — including the nested
  ones: `analytics export`, `snapshot publish`, `snapshot bootstrap`,
  `state pull`, `state push`, `autonomy status`.
- The top-level parser also defines `--json` with a regular `default=False`.

`argparse.SUPPRESS` on the parent copy is the key: when the post-subcommand
flag is absent, argparse does **not** write the attribute, so it never clobbers
a value set by the pre-subcommand flag. The result is a single `args.json`
attribute that is correct in either position. (Nested subparsers get the flag
by adding `parents=[common]` at each level they are created.)

### Output routing — an `Output` helper in `cli.py`

Constructed once in `main()` from the resolved command name and `args.json`:

- `out.log(msg)` — JSON mode: writes to **stderr**; human mode: writes to
  stdout. This is the callable passed as `log=` into `sync_all`, `analyze`,
  `tier1`, `state.*`, etc., so all existing heartbeat/stage logging is
  preserved and simply moves to stderr under `--json`.
- `out.emit(data, human, exit_code=0)` — JSON mode: prints one line,
  `{"command", "ok": true, "exit_code", "data"}`; human mode: prints the
  `human` string. Returns `exit_code`.
- `out.fail(message, exit_code=1)` — JSON mode: prints
  `{"command", "ok": false, "exit_code", "error"}` to stdout; human mode:
  prints `error: <message>` to stderr. Returns `exit_code`.

Each `_cmd_*` handler is refactored to build a `data` dict and a `human`
string, then end with `return out.emit(data, human, exit_code=...)` or
`return out.fail(...)` in place of today's `print(...); return N`. The `Output`
instance is passed to handlers (via `args`, e.g. `args._out`, or as an
argument) so they need no knowledge of the mode themselves.

### Error handling

Three cases, matching decision 5:

- **Bad input / expected failure** (unknown `--repo`, unparseable issue ref):
  the handler calls `out.fail(msg, exit_code=1)`. Envelope:
  `{"ok": false, "error", "exit_code": 1}`.
- **Unexpected exception** (e.g. `sync_all` raising mid-run): wrapped in
  `main()`. After parsing, `try: rc = args.func(args)`. In **JSON mode**, catch
  `Exception`, emit `{"command", "ok": false, "exit_code": 1, "error": str(exc)}`
  to stdout, return 1. In **human mode**, re-raise — today's traceback behavior
  is unchanged. The `command` label comes from the parsed args, available even
  when the handler raised.
- **Domain-negative but successful** (`verify-counts` mismatch, `execute` /
  `undo` with `counts.error > 0`): `out.emit(data, human, exit_code=1)` —
  `{"ok": true, "exit_code": 1, "data": {...}}`.

## Per-command `data` payloads

Most commands pass their module's existing return dict straight through.

| Command | `data` | non-zero `exit_code` when |
|---|---|---|
| `sync` | `{repos, issues, prs, comments}` | — |
| `verify-counts` | `{reconciled: bool, tolerance, repos: [{repo, mirror, github, diff, ok}]}` | any repo mismatch → 1 |
| `analyze` | `{classified, rechecked, pairs, halted_on_budget}` | — |
| `analyze-status` | `{open_batches: [...], today_spend_usd}` | — |
| `embed` | `{embedded}` | — |
| `execute` | `{batch_id, counts}` | `counts.error > 0` → 1 |
| `undo` | `{batch_id, counts}` | `counts.error > 0` → 1 |
| `analytics export` | the analytics document (see note) | — |
| `snapshot publish` | `{tag, latest_tag}` | — |
| `snapshot bootstrap` | `{db, tag}` | — |
| `state pull` | the `state.pull` result dict | — |
| `state push` | the `state.push` result dict | — |
| `tier1` | `{sessions, proposals, halted_on_budget}` | — |
| `tier2` | `{repo, number, label, workflow_hint}` | — |
| `autonomy status` | `{categories: {action: {reviewed, precision, audit_failures, promote}}, wrote: path\|null}` | — |
| `steady-state` | dry-run: `{stages: [...], dry_run: true}`; real: the `steady_state.run` result | real run with `failed` → 1 |

**Note on `analytics export`:** `analytics_mod.export(con, out_path)` currently
returns `None` and builds its `payload` dict inline before writing it to disk.
To emit that document as `data`, `export` will be refactored to **return** the
payload it writes. No behavior change for the file path — the file is still
written to `--out` as before; JSON mode additionally emits the returned payload
in the envelope. This satisfies the issue's "analytics export should support
stdout" without adding a separate flag.

## Testing

- **Envelope shape** for representative commands (`sync`, `verify-counts`,
  `analyze-status`, `execute`): stdout parses as exactly one JSON object with
  `command`, `ok`, `exit_code`, `data`; captured stderr (not stdout) carries the
  log lines.
- **Both flag positions** produce identical parsed results
  (`sync --json` ≡ `--json sync`), including a nested command
  (`analytics export --json` ≡ `--json analytics export`).
- **Domain-negative:** `verify-counts` with a mismatch →
  `{"ok": true, "exit_code": 1, "data": {"reconciled": false, ...}}` and process
  return code 1.
- **Errors:** unknown `--repo` → `{"ok": false, "error", "exit_code": 1}`, rc 1;
  an injected exception under `--json` → an `ok: false` envelope on stdout (not a
  traceback), rc 1.
- **Human mode unchanged:** existing `tests/triage_verse/test_cli.py` and the
  other `test_cli_*.py` assertions still pass — the refactor must not alter
  non-JSON output.

## Out of scope

- No `--agent` terser mode (kata has one; not requested here).
- No change to the file artifacts (`.data/proposals/`, decision JSONL,
  results JSONL) — those remain the durable interchange.
- No streaming/incremental JSON — one document per invocation.
