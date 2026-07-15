# Prune invalid-module-id proposals

**Date:** 2026-07-15 · **Issue:** posit-dev/shiny-issue-triage#30 · **Status:** approved design

The review app renders each proposal as a Shiny dynamic module whose namespace is
the proposal's `id`. Shiny's `validate_id` only accepts ids matching `^\.?\w+$`
(letters, numbers, underscore, optional leading dot) and raises `ValueError` at
render otherwise. Because the whole queue renders in one output, a single bad id
blanks every row. The queue loader (`review_queue.load_undecided`) already skips
such proposals with a warning so the queue degrades gracefully — but a skipped
proposal is then invisible and unreviewable, with no built-in way to fix it.

Real proposal ids are `uuid.uuid4().hex` (`proposals.py`), always namespace-safe,
so an invalid id only arises from a hand-authored or hand-edited
`.data/proposals/*.jsonl` record. This feature adds an explicit way to remove such
records so the operator can regenerate clean ones.

## Recovery model

`.data/` is derived; GitHub is the source of truth. The intended repair is to
**delete the broken proposal record and re-run `analyze`**, which mints a fresh
`uuid4().hex` id. This command performs only the deletion step and then points the
operator at `analyze`; it never rewrites an id in place (that would require
rewriting every `proposal_id` reference in `.data/decisions/` and `.data/results/`
to keep the join intact — fragile, and unnecessary given regeneration).

## CLI surface

```
triage-verse proposals prune <TARGET> [--apply]
```

`<TARGET>` is **either a proposal id or a path to a proposals `.jsonl` file**,
disambiguated by `pathlib.Path(target).is_file()`:

- **id mode** (target is not an existing file): remove the record(s) whose `id`
  equals `<TARGET>`.
- **file mode** (target is an existing file): remove every record *in that file*
  whose `id` fails the module-id rule.

**Safety rail — only invalid ids are ever removed.** In id mode, if `<TARGET>` is
itself a valid module id, the command changes nothing, prints
`'<id>' is a valid module id; nothing to prune`, and exits non-zero. The tool
therefore cannot delete a well-formed proposal; its blast radius is exactly the
broken records. The empty-/missing-id case (which can't be typed as an id
argument) is handled via file mode, whose sweep matches `None`/`""` ids too.

**Dry-run is the default.** A bare invocation prints one line per match —
`file:lineno  repo#issue  id=<value>` — plus a total count, and changes nothing.
`--apply` performs the rewrite. This matches `execute`/`undo`, which are also
dry-run by default.

**Line-level rewrite.** Affected files are rewritten by dropping only the matched
lines and preserving every other line **verbatim** (the raw text is kept; records
are not re-serialized). Blank lines and any unrelated malformed-JSON lines are
left untouched. A file is only rewritten if at least one line was dropped.

**Recommend `analyze`.** After both dry-run and apply, the command prints:

> GitHub is the source of truth. Re-run `triage-verse analyze` to regenerate valid
> proposals for these issues.

Directory follows the existing env-var convention: `TRIAGE_VERSE_PROPOSALS`
(default `.data/proposals`).

## Warning-message update

`review_queue.load_undecided`'s existing skip warning gains an actionable hint,
staying id-only (the loader does not track which file a record came from):

> `skipping proposal '<id>' (<repo>#<n>): invalid Shiny module id. Remove it with`
> `'triage-verse proposals prune <id>' (or pass its .jsonl file), then re-run`
> `'triage-verse analyze'.`

## Code layout

- **`proposals.py`** — pure logic:
  `prune_proposals(proposals_dir, target, *, apply=False) -> list[dict]`. Returns
  the records that were removed (dry-run: would be removed) for the caller to
  report. Uses `review_queue.valid_module_id`. Raises a `ValueError` for the
  "valid id, nothing to prune" refusal so the CLI can map it to a non-zero exit
  with the message. No `shiny` import (keeps the engine UI-free).
- **`cli.py`** — a new `proposals` subcommand group (mirrors the existing
  `snapshot` / `state` / `autonomy` grouping) with a `prune` subcommand and a thin
  `_cmd_proposals_prune` that calls `prune_proposals`, prints the match lines and
  the `analyze` recommendation, and returns the exit code.

## Testing

- `prune_proposals` unit tests: id mode removes the matching record and leaves
  others verbatim; file mode removes all invalid-id records in the given file
  (including a `None`/missing-id record) and no others; a valid-id target raises
  and rewrites nothing; dry-run (`apply=False`) returns the matches but leaves
  files byte-for-byte unchanged; unrelated malformed-JSON and blank lines survive
  a rewrite; a nonexistent proposals dir yields an empty result.
- CLI test: `proposals prune <bad-id>` (dry-run) prints the match and exits 0
  without mutating; `--apply` rewrites; a valid-id target exits non-zero.
