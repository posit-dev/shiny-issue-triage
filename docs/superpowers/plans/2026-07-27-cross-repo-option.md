# Cross-Repo Option Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the dedup stage's `cross_repo_option` field real behavior — link-without-closing, and flag-for-human-transfer — instead of being computed, stored, displayed, and ignored.

**Architecture:** `proposals.build` maps each `cross_repo_option` value to a *distinct action* (`close-duplicate` / `link-duplicate` / `suggest-transfer`), so an action name always describes what approving it does. The executor gains two branches: a comment with no close, and a label. The review app gains a Transfers worklist, because approving a transfer suggestion labels the issue but does not move it. The system never transfers an issue; a human does.

**Tech Stack:** Python 3.12, uv, pytest, Shiny for Python, SQLite. GitHub access via the `gh` CLI behind the egress guard in `src/triage_verse/gh.py`.

## Global Constraints

- **No schema migration.** `dedup_verdicts.cross_repo_option` already exists. Do not add columns and do not bump `_SCHEMA_VERSION` — Task 10 deletes rows from existing tables rather than adding state.
- **Do not modify `gh.py`.** `ALLOWED_OPERATIONS` and `ALLOWED_MUTATION_FIELDS` must not gain `transferIssue` or any other entry. A transfer mutation must keep failing closed.
- **The pipeline never transfers an issue.** No task may emit a `transferIssue` mutation or a new mutation `kind`. Only the existing kinds `add-label`, `remove-label`, `comment`, `close`, `close-duplicate` may appear.
- **Primary gate:** `make py-check` (ruff format + lint, pyright, pytest) must pass before any task is considered done.
- **Exact strings:** the transfer label is `wrong location` (already in `.github/triage/labels.yaml` under `allowed_safe_output_labels`). The completion verdict is `transferred`. New action names are exactly `link-duplicate` and `suggest-transfer`.
- **Fallback rule:** an unrecognized `cross_repo_option`, or `transfer`/`keep-both-link` on a same-repo pair, degrades to `close-duplicate` (today's behavior).

---

### Task 1: Define the three options in the dedup prompt

The model currently receives no definition of `close-and-link`, `transfer`, or `keep-both-link` — the words appear in no prompt, rubric, or taxonomy. Branching on the current values would mean acting on undefined output, so this task is a prerequisite for every task after it.

**Files:**
- Modify: `src/triage_verse/dedup.py:47-78` (add a module constant above `build_requests`, and append it to the instruction text)
- Test: `tests/triage_verse/test_dedup.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `dedup.CROSS_REPO_GUIDANCE: str` — the option definitions, included in every dedup request's user content.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_dedup.py`:

```python
def test_build_requests_defines_cross_repo_options(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    for repo, num in (("r/a", 1), ("r/b", 2)):
        con.execute(
            "INSERT INTO issues (repo, number, title, body, state, created_at,"
            " updated_at, is_pr) VALUES (?, ?, 'T', 'B', 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 0)",
            (repo, num),
        )
    con.commit()
    reqs = dedup.build_requests(
        con, _stage(), [{"type": "text", "text": "RUBRIC"}], [_pair()]
    )
    content = reqs[0].params["messages"][0]["content"]
    for option in ("close-and-link", "keep-both-link", "transfer"):
        assert option in content
    assert "null" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_dedup.py::test_build_requests_defines_cross_repo_options -v`
Expected: FAIL — `assert 'close-and-link' in content`.

- [ ] **Step 3: Write minimal implementation**

In `src/triage_verse/dedup.py`, add above `build_requests`:

```python
CROSS_REPO_GUIDANCE = """\
When A and B are duplicates that live in DIFFERENT repositories, set
cross_repo_option to whichever of these fits the situation:
- "close-and-link": the duplicate adds nothing the canonical issue lacks, so the
  discussion should consolidate on the canonical issue.
- "keep-both-link": both issues have standing in their own repositories -- for
  example, the same defect must be tracked separately in an R package and its
  Python counterpart. Neither issue should be closed.
- "transfer": the issue is filed in the wrong repository, and its content belongs
  in the canonical issue's repository rather than being discarded.
Set cross_repo_option to null when A and B live in the SAME repository."""
```

Then change the instruction element of the `content` list in `build_requests` from:

```python
                "Decide whether A and B are duplicate, related, or distinct. "
                "Respond with JSON matching the schema.",
```

to:

```python
                "Decide whether A and B are duplicate, related, or distinct.",
                CROSS_REPO_GUIDANCE,
                "Respond with JSON matching the schema.",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_dedup.py -v`
Expected: PASS (5 tests — the new one plus the 4 existing).

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/dedup.py tests/triage_verse/test_dedup.py
git commit -m "feat(dedup): define cross_repo_option values in the prompt"
```

---

### Task 2: Map `cross_repo_option` to a distinct action

**Files:**
- Modify: `src/triage_verse/proposals.py:44-77` (add a helper above `build`, use it in the dedup loop)
- Test: `tests/triage_verse/test_proposals.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `proposals.CROSS_REPO_ACTIONS: dict[str, str]` and `proposals.cross_repo_action(option: object, repo_a: str, repo_b: str) -> str`. Proposal records whose `action` is `"link-duplicate"` or `"suggest-transfer"`, with the same `params` shape as `close-duplicate`: `{"canonical": str | None, "cross_repo_option": str | None}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_proposals.py`:

```python
import pytest


def _seed_dup(con, option, repo_b="r/b"):
    """One duplicate verdict between r/a#1 and repo_b#2 with `option`."""
    for repo, num in (("r/a", 1), (repo_b, 2)):
        con.execute(
            "INSERT OR IGNORE INTO issues (repo, number, title, state, created_at,"
            " updated_at, is_pr) VALUES (?, ?, 'T', 'OPEN',"
            " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z', 0)",
            (repo, num),
        )
    db.upsert_dedup_verdict(
        con,
        {
            "repo_a": "r/a",
            "number_a": 1,
            "repo_b": repo_b,
            "number_b": 2,
            "hash_a": "ha",
            "hash_b": "hb",
            "verdict": "duplicate",
            "canonical_json": json.dumps(f"{repo_b}#2"),
            "cross_repo_option": option,
            "confidence": 0.9,
            "rationale": "same bug",
            "model": "claude-sonnet-5",
            "run_id": "run1",
            "at": "2026-06-29T00:00:00Z",
        },
    )
    con.commit()


@pytest.mark.parametrize(
    "option,expected",
    [
        ("close-and-link", "close-duplicate"),
        (None, "close-duplicate"),
        ("nonsense", "close-duplicate"),
        ("keep-both-link", "link-duplicate"),
        ("transfer", "suggest-transfer"),
    ],
)
def test_build_maps_cross_repo_option_to_action(tmp_path, option, expected):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_dup(con, option)
    recs = [r for r in proposals.build(con, "run1") if r["repo"] == "r/a"]
    assert [r["action"] for r in recs] == [expected]
    assert recs[0]["params"]["canonical"] == "r/b#2"
    assert recs[0]["params"]["cross_repo_option"] == option


@pytest.mark.parametrize("option", ["transfer", "keep-both-link"])
def test_same_repo_pair_ignores_cross_repo_option(tmp_path, option):
    con = db.connect(tmp_path / "m.sqlite")
    _seed_dup(con, option, repo_b="r/a")
    recs = [r for r in proposals.build(con, "run1") if r["repo"] == "r/a"]
    assert [r["action"] for r in recs] == ["close-duplicate"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_proposals.py -k cross_repo -v`
Expected: FAIL — actions come back as `close-duplicate` for the `keep-both-link` and `transfer` cases.

- [ ] **Step 3: Write minimal implementation**

In `src/triage_verse/proposals.py`, add above `build`:

```python
# cross_repo_option -> the action that honestly describes the outcome. Anything
# unrecognized, and any same-repo pair, degrades to close-duplicate: that is
# exactly today's behavior, so a missing or malformed option cannot change what
# happens to a pair.
CROSS_REPO_ACTIONS = {
    "close-and-link": "close-duplicate",
    "keep-both-link": "link-duplicate",
    "transfer": "suggest-transfer",
}


def cross_repo_action(option: object, repo_a: str, repo_b: str) -> str:
    """Action for a duplicate verdict, given its cross-repo option."""
    if repo_a == repo_b:
        return "close-duplicate"
    if not isinstance(option, str):
        return "close-duplicate"
    return CROSS_REPO_ACTIONS.get(option, "close-duplicate")
```

Then in the dedup loop of `build`, replace the literal `"close-duplicate"` argument to `_rec` with the mapped action. The loop body becomes:

```python
    for d in dup_rows:
        repo, num = d["repo_a"], d["number_a"]
        base = {
            "repo": repo,
            "issue": num,
            "issue_updated_at": d["issue_updated_at"],
            "run_id": run_id,
            "model": d["model"],
            "confidence": d["confidence"],
            "evidence": [
                f"https://github.com/{d['repo_a']}/issues/{d['number_a']}",
                f"https://github.com/{d['repo_b']}/issues/{d['number_b']}",
            ],
        }
        records.append(
            _rec(
                base,
                cross_repo_action(d["cross_repo_option"], d["repo_a"], d["repo_b"]),
                {
                    "canonical": json.loads(d["canonical_json"])
                    if d["canonical_json"]
                    else None,
                    "cross_repo_option": d["cross_repo_option"],
                },
                d["rationale"],
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_proposals.py -v`
Expected: PASS (all tests, new and pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/proposals.py tests/triage_verse/test_proposals.py
git commit -m "feat(proposals): map cross_repo_option to link-duplicate / suggest-transfer"
```

---

### Task 3: Add the `link-duplicate` comment template

**Files:**
- Create: `config/templates/link-duplicate.md`
- Modify: `src/triage_verse/templates.py:8-13` (add the name to `TEMPLATE_NAMES`)
- Test: `tests/triage_verse/test_templates.py`

**Interfaces:**
- Consumes: nothing.
- Produces: template key `"link-duplicate"` in the dict returned by `templates.load(...)`, renderable with the single placeholder `canonical_url`.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_templates.py`:

```python
def test_link_duplicate_template_loads_and_renders():
    import pathlib

    from triage_verse import templates

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    loaded = templates.load(repo_root / "config" / "templates")
    body = templates.render(
        loaded, "link-duplicate", canonical_url="https://github.com/o/r/issues/3"
    )
    assert "https://github.com/o/r/issues/3" in body
    assert "open" in body.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_templates.py::test_link_duplicate_template_loads_and_renders -v`
Expected: FAIL — `KeyError: 'link-duplicate'`.

- [ ] **Step 3: Write minimal implementation**

Create `config/templates/link-duplicate.md`:

```markdown
This tracks the same underlying problem as {canonical_url}, which lives in a different repository. Both issues are staying open on purpose: each one needs its own fix in its own package, and this was reviewed and approved by a maintainer as part of a triage of the backlog.

We're linking them so the two discussions can reference each other.
```

In `src/triage_verse/templates.py`, add `"link-duplicate"` to `TEMPLATE_NAMES`:

```python
TEMPLATE_NAMES = (
    "close-completed",
    "close-not-planned",
    "close-duplicate",
    "close-duplicate-cross-repo",
    "link-duplicate",
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_templates.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config/templates/link-duplicate.md src/triage_verse/templates.py tests/triage_verse/test_templates.py
git commit -m "feat(templates): add link-duplicate comment template"
```

---

### Task 4: Plan mutations for `link-duplicate` and `suggest-transfer`

`link-duplicate` comments and closes nothing. `suggest-transfer` applies one label and closes nothing. Neither emits a transfer mutation — the executor has no such capability and this task does not add one.

**Files:**
- Modify: `src/triage_verse/executor.py:165-189` (add two branches before the final `return`)
- Test: `tests/triage_verse/test_executor_plan.py`

**Interfaces:**
- Consumes: `templates.load` key `"link-duplicate"` from Task 3; action names from Task 2.
- Produces: `executor.TRANSFER_LABEL: str` (value `"wrong location"`). `plan_decision` handles actions `"link-duplicate"` and `"suggest-transfer"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_executor_plan.py`. Note `ALLOWED` at the top of that file does not include `wrong location`; these tests pass an extended set so the label check is exercised both ways.

```python
ALLOWED_WITH_TRANSFER = ALLOWED | {"wrong location"}


def test_link_duplicate_comments_without_closing():
    muts, err = executor.plan_decision(
        _decision(
            "link-duplicate",
            {"canonical": "other/repo#3", "cross_repo_option": "keep-both-link"},
        ),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert err is None
    assert len(muts) == 1
    assert muts[0]["kind"] == "comment"
    assert "https://github.com/other/repo/issues/3" in muts[0]["body"]


@pytest.mark.parametrize(
    "params",
    [
        {"canonical": None, "cross_repo_option": "keep-both-link"},
        {"canonical": "nonsense", "cross_repo_option": "keep-both-link"},
        {"canonical": "o/r#7", "cross_repo_option": "keep-both-link"},
    ],
)
def test_link_duplicate_bad_canonical_is_an_error(params):
    muts, err = executor.plan_decision(
        _decision("link-duplicate", params), _issue(), allowed=ALLOWED, tmpl=TMPL
    )
    assert muts == [] and err is not None


def test_suggest_transfer_only_labels():
    muts, err = executor.plan_decision(
        _decision(
            "suggest-transfer",
            {"canonical": "other/repo#3", "cross_repo_option": "transfer"},
        ),
        _issue(),
        allowed=ALLOWED_WITH_TRANSFER,
        tmpl=TMPL,
    )
    assert err is None
    assert muts == [{"kind": "add-label", "label": "wrong location"}]


def test_suggest_transfer_never_emits_a_transfer_mutation():
    muts, _ = executor.plan_decision(
        _decision(
            "suggest-transfer",
            {"canonical": "other/repo#3", "cross_repo_option": "transfer"},
        ),
        _issue(),
        allowed=ALLOWED_WITH_TRANSFER,
        tmpl=TMPL,
    )
    assert all(m["kind"] != "transfer" for m in muts)
    assert not any("transfer" in str(m.get("kind", "")) for m in muts)


def test_suggest_transfer_rejects_same_repo_target():
    muts, err = executor.plan_decision(
        _decision(
            "suggest-transfer",
            {"canonical": "o/r#3", "cross_repo_option": "transfer"},
        ),
        _issue(),
        allowed=ALLOWED_WITH_TRANSFER,
        tmpl=TMPL,
    )
    assert muts == [] and "own repo" in err


def test_suggest_transfer_requires_the_label_to_be_allowlisted():
    muts, err = executor.plan_decision(
        _decision(
            "suggest-transfer",
            {"canonical": "other/repo#3", "cross_repo_option": "transfer"},
        ),
        _issue(),
        allowed=ALLOWED,
        tmpl=TMPL,
    )
    assert muts == [] and "allowlist" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_executor_plan.py -k "link_duplicate or suggest_transfer" -v`
Expected: FAIL — `plan_decision` falls through to `action not allowlisted`.

- [ ] **Step 3: Write minimal implementation**

In `src/triage_verse/executor.py`, add near `PRIORITY_VALUES` / `CLOSE_REASON_MAP`:

```python
TRANSFER_LABEL = "wrong location"
```

Then in `plan_decision`, insert both branches immediately after the existing `close-duplicate` branch and before the final `return [], f"action not allowlisted: {action!r}"`:

```python
    if action == "link-duplicate":
        canonical = params.get("canonical")
        if not canonical:
            return [], "link-duplicate requires a canonical target"
        ref = parse_issue_ref(str(canonical), decision["repo"])
        if ref is None:
            return [], f"cannot parse canonical issue ref: {canonical!r}"
        if ref == (decision["repo"], decision["issue"]):
            return [], "canonical target is the issue itself"
        body = templates_mod.render(
            tmpl, "link-duplicate", canonical_url=_issue_url(*ref)
        )
        return [{"kind": "comment", "body": body}], None

    if action == "suggest-transfer":
        canonical = params.get("canonical")
        if not canonical:
            return [], "suggest-transfer requires a canonical target"
        ref = parse_issue_ref(str(canonical), decision["repo"])
        if ref is None:
            return [], f"cannot parse canonical issue ref: {canonical!r}"
        if ref[0] == decision["repo"]:
            return [], "suggest-transfer target is the issue's own repo"
        if TRANSFER_LABEL not in allowed:
            return [], f"label not in allowlist: {TRANSFER_LABEL!r}"
        # Labels the issue for a human to move. The executor has no transfer
        # capability and never gains one; see the Transfers tab in the review app.
        return [{"kind": "add-label", "label": TRANSFER_LABEL}], None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_executor_plan.py -v`
Expected: PASS — the new tests plus every pre-existing one (the unchanged `close-duplicate` cases must still pass).

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/executor.py tests/triage_verse/test_executor_plan.py
git commit -m "feat(executor): plan link-duplicate comment and suggest-transfer label"
```

---

### Task 5: Queue support, transfer destination, and the pending-transfer list

**Files:**
- Modify: `src/triage_verse/review_queue.py:15-18` (`SUPPORTED_ACTIONS`), `:87` (`TERMINAL_VERDICTS`), and append two functions near `duplicate_sibling`
- Test: `tests/triage_verse/test_review_queue.py`

**Interfaces:**
- Consumes: action names from Task 2.
- Produces:
  - `review_queue.SUPPORTED_ACTIONS` includes `"link-duplicate"` and `"suggest-transfer"`; `HIGH_STAKES_ACTIONS` is unchanged.
  - `review_queue.TERMINAL_VERDICTS` includes `"transferred"`.
  - `review_queue.TRANSFER_DONE_VERDICT: str` (value `"transferred"`).
  - `review_queue.transfer_destination(record: dict) -> str | None` — destination repo from `record["params"]["canonical"]`; works for both proposal and decision records.
  - `review_queue.pending_transfers(decisions_dir) -> list[dict]` — approved-and-not-yet-transferred `suggest-transfer` decisions, newest first.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_review_queue.py`:

```python
def test_transfer_destination_from_canonical_ref():
    rec = {"params": {"canonical": "posit-dev/py-shiny#12"}}
    assert review_queue.transfer_destination(rec) == "posit-dev/py-shiny"


def test_transfer_destination_from_canonical_url():
    rec = {"params": {"canonical": "https://github.com/posit-dev/py-shiny/issues/12"}}
    assert review_queue.transfer_destination(rec) == "posit-dev/py-shiny"


@pytest.mark.parametrize(
    "params", [{}, {"canonical": None}, {"canonical": "nonsense"}, {"canonical": 7}]
)
def test_transfer_destination_none_when_unparseable(params):
    assert review_queue.transfer_destination({"params": params}) is None


def _tdec(pid, verdict, at, action="suggest-transfer"):
    return {
        "id": f"d-{pid}-{at}",
        "proposal_id": pid,
        "repo": "r/a",
        "issue": 1,
        "action": action,
        "params": {"canonical": "r/b#2", "cross_repo_option": "transfer"},
        "verdict": verdict,
        "confidence": 0.9,
        "decided_at": at,
    }


def test_pending_transfers_lists_approved_only(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _tdec("p1", "approved", "2026-07-01T00:00:00Z"),
                _tdec("p2", "rejected", "2026-07-02T00:00:00Z"),
                _tdec("p3", "approved", "2026-07-03T00:00:00Z"),
                _tdec("p4", "approved", "2026-07-04T00:00:00Z", action="add-label"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rows = review_queue.pending_transfers(d)
    assert [r["proposal_id"] for r in rows] == ["p3", "p1"]


def test_pending_transfers_drops_marked_transferred(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                _tdec("p1", "approved", "2026-07-01T00:00:00Z"),
                _tdec("p1", "transferred", "2026-07-05T00:00:00Z"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert review_queue.pending_transfers(d) == []


def test_transferred_is_terminal_so_it_does_not_return_to_the_queue():
    assert "transferred" in review_queue.TERMINAL_VERDICTS


def test_new_actions_are_supported_but_not_high_stakes():
    assert {"link-duplicate", "suggest-transfer"} <= review_queue.SUPPORTED_ACTIONS
    assert not ({"link-duplicate", "suggest-transfer"} & review_queue.HIGH_STAKES_ACTIONS)
```

Confirm `json` and `pytest` are imported at the top of the file; add whichever is missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -k "transfer or new_actions" -v`
Expected: FAIL — `AttributeError: module 'triage_verse.review_queue' has no attribute 'transfer_destination'`.

- [ ] **Step 3: Write minimal implementation**

In `src/triage_verse/review_queue.py`:

Extend `SUPPORTED_ACTIONS` (leave `HIGH_STAKES_ACTIONS` alone — a comment and a label do not warrant the full-evidence gate):

```python
SUPPORTED_ACTIONS = frozenset(
    {
        "add-label",
        "set-priority",
        "close",
        "close-duplicate",
        "link-duplicate",
        "suggest-transfer",
    }
)
```

Add the verdict constant and extend `TERMINAL_VERDICTS`:

```python
# Recorded when a human confirms they moved an issue a suggest-transfer proposal
# pointed at. Terminal so the proposal cannot bounce back into the main queue.
TRANSFER_DONE_VERDICT = "transferred"
TERMINAL_VERDICTS = frozenset({"approved", "edited", "rejected", TRANSFER_DONE_VERDICT})
```

Append near `duplicate_sibling` (this module cannot import `executor` — `executor` imports *this* module — so the ref patterns are local):

```python
_CANONICAL_REF = re.compile(
    r"^(?:([\w.-]+/[\w.-]+)#\d+|https://github\.com/([\w.-]+/[\w.-]+)/issues/\d+)$"
)


def transfer_destination(record: dict) -> str | None:
    """Repo a suggest-transfer points at, from `params.canonical`.

    Works for proposal and decision records alike: decisions carry `params` but
    not `evidence`, so the canonical ref is the only shared source.
    """
    canonical = (record.get("params") or {}).get("canonical")
    if not isinstance(canonical, str):
        return None
    m = _CANONICAL_REF.match(canonical.strip())
    if m is None:
        return None
    return m.group(1) or m.group(2)


def pending_transfers(decisions_dir: str | pathlib.Path) -> list[dict]:
    """Approved suggest-transfer decisions not yet marked transferred, newest first."""
    latest: dict[str, dict] = {}
    for r in iter_jsonl_records(decisions_dir):
        pid = r.get("proposal_id")
        if pid is None or r.get("action") != "suggest-transfer":
            continue
        cur = latest.get(pid)
        if cur is None or r.get("decided_at", "") >= cur.get("decided_at", ""):
            latest[pid] = r
    return sorted(
        (d for d in latest.values() if d.get("verdict") in ("approved", "edited")),
        key=lambda d: d.get("decided_at", ""),
        reverse=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_review_queue.py -v`
Expected: PASS — new tests plus all pre-existing queue tests.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_queue.py tests/triage_verse/test_review_queue.py
git commit -m "feat(review-queue): support new actions, transfer destination, pending list"
```

---

### Task 6: Lock in the safety properties

These tests assert things that must *stay* true. They protect the design's core claim: the pipeline gains no power to transfer an issue, and transfer suggestions can never auto-apply.

**Files:**
- Test: `tests/triage_verse/test_autonomy.py`, `tests/triage_verse/test_gh_guard.py`, `tests/triage_verse/test_executor_auto.py`

**Interfaces:**
- Consumes: `executor.TRANSFER_LABEL` from Task 4; action names from Task 2.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the tests (these should pass immediately — they are regression locks)**

Add to `tests/triage_verse/test_autonomy.py`:

```python
def test_new_actions_can_never_graduate_to_autonomy():
    from triage_verse import autonomy

    assert "suggest-transfer" not in autonomy.ELIGIBLE
    assert "link-duplicate" not in autonomy.ELIGIBLE
```

Add to `tests/triage_verse/test_executor_auto.py`:

```python
def test_new_actions_are_not_auto_eligible():
    from triage_verse import executor

    assert "suggest-transfer" not in executor.AUTO_ELIGIBLE
    assert "link-duplicate" not in executor.AUTO_ELIGIBLE


def test_select_auto_never_picks_a_suggest_transfer():
    from triage_verse import executor

    proposals = [
        {"id": "p1", "action": "suggest-transfer", "confidence": 1.0},
        {"id": "p2", "action": "link-duplicate", "confidence": 1.0},
    ]
    promoted = {
        "suggest-transfer": {"confidence_floor": 0.0},
        "link-duplicate": {"confidence_floor": 0.0},
    }
    assert executor.select_auto(proposals, set(), promoted, audit_rate=0.0) == []
```

Add to `tests/triage_verse/test_gh_guard.py`:

```python
def test_transfer_issue_operation_is_refused():
    import pytest

    from triage_verse import gh

    assert "transferIssue" not in gh.ALLOWED_OPERATIONS
    assert "transferIssue" not in gh.ALLOWED_MUTATION_FIELDS
    with pytest.raises(gh.EgressRefused):
        gh.gh_mutation(
            "transferIssue",
            "mutation($id: ID!, $repo: ID!) { transferIssue(input: {issueId: $id,"
            " repositoryId: $repo}) { issue { id } } }",
            {"id": "NID", "repo": "RID"},
            repos=["o/r"],
        )


def test_transfer_issue_wire_field_is_refused_even_under_an_allowed_operation():
    import pytest

    from triage_verse import gh

    with pytest.raises(gh.EgressRefused):
        gh.classify_gh_call(
            ["api", "graphql"],
            input=json.dumps(
                {
                    "query": "mutation($id: ID!, $repo: ID!) { transferIssue("
                    "input: {issueId: $id, repositoryId: $repo}) { issue { id } } }",
                    "variables": {},
                }
            ),
            operation="addComment",
            repos=["o/r"],
            resolve_allowed=lambda: frozenset({"o/r"}),
        )
```

Confirm `json` is imported at the top of `test_gh_guard.py`; add it if missing.

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/triage_verse/test_autonomy.py tests/triage_verse/test_executor_auto.py tests/triage_verse/test_gh_guard.py -v`
Expected: PASS. If any fails, the design has been violated — stop and report rather than relaxing the test.

- [ ] **Step 3: Commit**

```bash
git add tests/triage_verse/test_autonomy.py tests/triage_verse/test_executor_auto.py tests/triage_verse/test_gh_guard.py
git commit -m "test: lock in no-auto-transfer and egress-guard refusal"
```

---

### Task 7: Record a completed transfer

**Files:**
- Modify: `src/triage_verse/decisions.py` (append one function)
- Test: `tests/triage_verse/test_decisions.py`

**Interfaces:**
- Consumes: `review_queue.TRANSFER_DONE_VERDICT` from Task 5.
- Produces: `decisions.record_transferred(decision: dict) -> dict` — a decision record with `verdict == "transferred"` and the same `proposal_id` as the approved decision it closes out.

- [ ] **Step 1: Write the failing test**

Add to `tests/triage_verse/test_decisions.py`:

```python
def test_record_transferred_reuses_the_proposal_id():
    approved = {
        "id": "d1",
        "proposal_id": "p1",
        "repo": "r/a",
        "issue": 1,
        "action": "suggest-transfer",
        "params": {"canonical": "r/b#2", "cross_repo_option": "transfer"},
        "verdict": "approved",
        "confidence": 0.9,
        "decided_at": "2026-07-01T00:00:00Z",
    }
    rec = decisions.record_transferred(approved)
    assert rec["proposal_id"] == "p1"
    assert rec["verdict"] == "transferred"
    assert rec["action"] == "suggest-transfer"
    assert rec["params"] == {"canonical": "r/b#2", "cross_repo_option": "transfer"}
    assert rec["repo"] == "r/a" and rec["issue"] == 1
    assert rec["id"] != "d1"
    assert "proposed_params" not in rec
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/triage_verse/test_decisions.py::test_record_transferred_reuses_the_proposal_id -v`
Expected: FAIL — `AttributeError: module 'triage_verse.decisions' has no attribute 'record_transferred'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/triage_verse/decisions.py`:

```python
def record_transferred(decision: dict) -> dict:
    """Close out an approved suggest-transfer once a human has moved the issue.

    Takes the *approved decision* rather than a proposal, since that is what the
    Transfers worklist holds. `record` reads `proposal["id"]`, so the decision's
    `proposal_id` is mapped onto `id` to keep both records pointing at the same
    proposal.
    """
    from . import review_queue

    return record(
        {**decision, "id": decision["proposal_id"]},
        review_queue.TRANSFER_DONE_VERDICT,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_decisions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/decisions.py tests/triage_verse/test_decisions.py
git commit -m "feat(decisions): record a completed transfer"
```

---

### Task 8: Render the new actions readably

Today `_row_label` interpolates the raw params dict, so a reviewer would see `{'canonical': 'r/b#2', 'cross_repo_option': 'transfer'}`. This task gives both new actions readable renderings, a destination badge, and a drawer block that says plainly that approving does not move the issue.

**Files:**
- Modify: `src/triage_verse/review_app/app.py:77-78` (`_row_label`), `:88-126` (`row_ui` header badges), `:354-359` (params rendering), `:387-391` (`_drawer_proposal`), `:474-480` (`_drawer_panel`)
- Test: `tests/triage_verse/test_review_app_transfer.py` (create)

**Interfaces:**
- Consumes: `review_queue.transfer_destination` from Task 5.
- Produces: `app._params_line(proposal: dict) -> str` and `app._drawer_transfer(proposal: dict) -> list`.

- [ ] **Step 1: Write the failing test**

Create `tests/triage_verse/test_review_app_transfer.py`:

```python
"""Rendering of link-duplicate and suggest-transfer in the review app."""

from triage_verse.review_app import app


def _proposal(action, canonical="posit-dev/py-shiny#12"):
    return {
        "id": "p1",
        "repo": "rstudio/shiny",
        "issue": 7,
        "action": action,
        "params": {"canonical": canonical, "cross_repo_option": "transfer"},
        "confidence": 0.9,
        "rationale": "belongs in the python package",
        "evidence": [],
    }


def test_params_line_for_suggest_transfer_shows_destination():
    line = app._params_line(_proposal("suggest-transfer"))
    assert "posit-dev/py-shiny" in line
    assert "canonical" not in line


def test_params_line_for_link_duplicate_reads_as_words():
    line = app._params_line(_proposal("link-duplicate"))
    assert "posit-dev/py-shiny#12" in line
    assert not line.startswith("{")


def test_params_line_falls_back_to_params_for_other_actions():
    p = {"action": "add-label", "params": {"label": "regression"}}
    assert "regression" in app._params_line(p)


def test_row_label_uses_the_readable_params_line():
    label = app._row_label(_proposal("suggest-transfer"))
    assert label.startswith("rstudio/shiny#7 — suggest-transfer:")
    assert "posit-dev/py-shiny" in label
    assert "cross_repo_option" not in label


def test_drawer_transfer_states_that_approving_does_not_move_the_issue():
    parts = app._drawer_transfer(_proposal("suggest-transfer"))
    text = " ".join(str(p) for p in parts)
    assert "posit-dev/py-shiny" in text
    assert "wrong location" in text
    assert "Transfers" in text


def test_drawer_transfer_handles_a_missing_destination():
    parts = app._drawer_transfer(_proposal("suggest-transfer", canonical=None))
    text = " ".join(str(p) for p in parts)
    assert "not identified" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_app_transfer.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_params_line'`.

- [ ] **Step 3: Write minimal implementation**

In `src/triage_verse/review_app/app.py`:

Add after `_close_duplicate_params` (which stays as-is, still rendering `cross_repo_option` for audit):

```python
def _params_line(proposal: dict) -> str:
    """Proposal params as words, per action."""
    action = proposal["action"]
    params = proposal.get("params") or {}
    if action in ("close-duplicate", "link-duplicate"):
        return _close_duplicate_params(params)
    if action == "suggest-transfer":
        dest = review_queue.transfer_destination(proposal)
        return f"→ {dest}" if dest else "→ (destination not identified)"
    return str(params)
```

Change `_row_label` to use it:

```python
def _row_label(proposal: dict) -> str:
    return f"{proposal['repo']}#{proposal['issue']} — {proposal['action']}: {_params_line(proposal)}"
```

In `_drawer_proposal`, replace the opening `if`/`else` that computes `params_line` with a single call:

```python
def _drawer_proposal(proposal: dict) -> list:
    params_line = _params_line(proposal)
```

Add the drawer block after `_drawer_sibling`:

```python
def _drawer_transfer(proposal: dict) -> list:
    dest = review_queue.transfer_destination(proposal)
    parts: list = [ui.h4("Suggested destination")]
    if dest is None:
        parts.append(ui.p("(destination not identified from the canonical ref)"))
        return parts
    parts.append(
        ui.p(ui.a(dest, href=f"https://github.com/{dest}", target="_blank"))
    )
    parts.append(
        ui.p(
            "Approving applies the 'wrong location' label only — it does not move "
            "the issue. Transfer it by hand on GitHub (Transfer issue, in the "
            "issue sidebar). It stays on the Transfers tab until you mark it done.",
            class_="text-muted",
        )
    )
    return parts
```

In `row_ui`, add a destination badge. Insert this immediately before the existing `if proposal.get("stale"):` block, so badge order reads destination → stale → not-now:

```python
    if proposal["action"] == "suggest-transfer":
        dest = review_queue.transfer_destination(proposal)
        header.insert(
            0,
            ui.span(
                f"→ {dest}" if dest else "transfer",
                style=(
                    "background-color: #00695c; color: white; border-radius: 999px; "
                    "padding: 0 0.5rem; margin-right: 0.5rem; font-size: 0.8rem;"
                ),
                title="Suggested transfer; a maintainer must move this on GitHub.",
            ),
        )
```

In `_drawer_panel`, render the block for the relevant action. Change:

```python
    parts += _drawer_proposal(state["proposal"])
```

to:

```python
    parts += _drawer_proposal(state["proposal"])
    if state["proposal"]["action"] == "suggest-transfer":
        parts += _drawer_transfer(state["proposal"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_review_app_transfer.py tests/triage_verse/test_review_app_audit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/triage_verse/review_app/app.py tests/triage_verse/test_review_app_transfer.py
git commit -m "feat(review-app): readable rendering and drawer block for transfers"
```

---

### Task 9: The Transfers worklist panel

Approving a `suggest-transfer` applies a label and then the proposal leaves the queue — so without this panel every approved suggestion would vanish with nobody having moved anything. This is the task that makes the `transfer` outcome real.

**Files:**
- Modify: `src/triage_verse/review_app/app.py` (add a row module beside `row_ui`/`row_server`, a `transfers_panel` beside `skipped_panel`, register it in `app_ui`, and wire `transfers_ui` in `server`)
- Test: `tests/triage_verse/test_review_app_transfer.py`

**Interfaces:**
- Consumes: `review_queue.pending_transfers` and `review_queue.transfer_destination` (Task 5); `decisions.record_transferred` (Task 7).
- Produces: `app.app_mark_transferred(decision: dict, *, decisions_dir=DECISIONS_DIR) -> str` — writes the completion record and returns the `proposal_id` it closed out. Kept as a module-level function (mirroring the existing `app_audit_reject`) so it is testable without a running Shiny session.

- [ ] **Step 1: Write the failing test**

Append to `tests/triage_verse/test_review_app_transfer.py`:

```python
import json

from triage_verse import review_queue


def _approved(pid="p1"):
    return {
        "id": f"d-{pid}",
        "proposal_id": pid,
        "repo": "rstudio/shiny",
        "issue": 7,
        "action": "suggest-transfer",
        "params": {"canonical": "posit-dev/py-shiny#12", "cross_repo_option": "transfer"},
        "verdict": "approved",
        "confidence": 0.9,
        "decided_at": "2026-07-01T00:00:00Z",
    }


def test_mark_transferred_removes_it_from_pending(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(_approved()) + "\n", encoding="utf-8")
    assert [r["proposal_id"] for r in review_queue.pending_transfers(d)] == ["p1"]

    pid = app.app_mark_transferred(_approved(), decisions_dir=d)
    assert pid == "p1"
    assert review_queue.pending_transfers(d) == []


def test_mark_transferred_appends_rather_than_rewriting(tmp_path):
    d = tmp_path / "decisions"
    d.mkdir()
    (d / "a.jsonl").write_text(json.dumps(_approved()) + "\n", encoding="utf-8")
    app.app_mark_transferred(_approved(), decisions_dir=d)

    records = review_queue.iter_jsonl_records(d)
    verdicts = sorted(r["verdict"] for r in records)
    assert verdicts == ["approved", "transferred"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_review_app_transfer.py -k mark_transferred -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'app_mark_transferred'`.

- [ ] **Step 3: Write minimal implementation**

In `src/triage_verse/review_app/app.py`:

Add the write helper next to the existing `app_audit_reject`:

```python
def app_mark_transferred(decision: dict, *, decisions_dir=DECISIONS_DIR) -> str:
    """Record that a human moved the issue this suggest-transfer pointed at."""
    decisions.write([decisions.record_transferred(decision)], decisions_dir)
    return decision["proposal_id"]
```

Add a row module after `row_server`:

```python
@module.ui
def transfer_row_ui(decision: dict, dest: str):
    url = f"https://github.com/{decision['repo']}/issues/{decision['issue']}"
    return ui.card(
        ui.card_header(f"{decision['repo']}#{decision['issue']} → {dest}"),
        ui.p(f"decided {decision.get('decided_at', '(unknown)')}"),
        ui.p(ui.a("Open on GitHub ↗", href=url, target="_blank")),
        ui.input_action_button(
            "mark_done",
            "Mark transferred",
            style="background-color: #00695c; color: white;",
        ),
    )


@module.server
def transfer_row_server(
    input: Inputs,
    output: Outputs,
    session: Session,
    decision: dict,
    on_done: Callable[[dict], None],
):
    @reactive.effect
    @reactive.event(input.mark_done)
    def _mark_done():
        on_done(decision)
```

Add the panel next to `skipped_panel`:

```python
transfers_panel = ui.nav_panel(
    "Transfers",
    ui.h4("Transfer worklist"),
    ui.p(
        "Approved transfer suggestions. Approving applied the 'wrong location' "
        "label; moving the issue is manual. Open it on GitHub, use Transfer "
        "issue in the sidebar, then mark it done here.",
        class_="text-muted",
    ),
    ui.output_ui("transfers_ui"),
)
```

Register it in `app_ui` immediately after `skipped_panel`:

```python
    skipped_panel,
    transfers_panel,
    dashboard_panel,
    audit_panel,
```

In `server`, add near the other `reactive.value` declarations:

```python
    transfers_tick = reactive.value(0)
    transfers_wired: set[str] = set()
```

and add the renderer next to `skipped_ui`:

```python
    def _mark_transferred(decision: dict) -> None:
        app_mark_transferred(decision)
        transfers_tick.set(transfers_tick.get() + 1)

    @render.ui
    def transfers_ui():
        transfers_tick.get()
        rows = review_queue.pending_transfers(DECISIONS_DIR)
        if not rows:
            return ui.p("No pending transfers.", class_="text-muted")
        cards = []
        for d in rows:
            pid = d["proposal_id"]
            if not review_queue.valid_module_id(pid):
                continue
            dest = review_queue.transfer_destination(d) or "(unknown)"
            if pid not in transfers_wired:
                transfer_row_server(pid, decision=d, on_done=_mark_transferred)
                transfers_wired.add(pid)
            cards.append(transfer_row_ui(pid, d, dest))
        return ui.div(*cards)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_review_app_transfer.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full gate**

Run: `make py-check`
Expected: ruff format clean, ruff lint clean, pyright clean, all pytest passing.

- [ ] **Step 6: Verify the app actually starts and the tab renders**

Run: `shiny run src/triage_verse/review_app/app.py`
Expected: the app boots and a **Transfers** tab appears beside Queue / Skipped / Dashboard / Audit. With no approved `suggest-transfer` decisions it reads "No pending transfers." Stop the server afterwards.

- [ ] **Step 7: Commit**

```bash
git add src/triage_verse/review_app/app.py tests/triage_verse/test_review_app_transfer.py
git commit -m "feat(review-app): Transfers worklist for approved transfer suggestions"
```

---

### Task 10: Reconcile issues GitHub no longer has

**Why this task exists.** When an issue is transferred from repo A to repo B, GitHub assigns a new number in B and the issue leaves A entirely. `sync` is upsert-only and never deletes, so the source row is frozen at `state = 'OPEN'` forever — no future sync will ever touch it again, because it no longer appears in repo A's issue connection.

That ghost row breaks duplicate detection in a way this feature makes actively dangerous. `candidate_pairs` sources from `i.state='OPEN'`, so the ghost qualifies; `db.knn` has **no state filter**, so the ghost's vector is also returned as a neighbour. The transferred issue B#N has byte-identical title and body, so cosine is ≈1.0 — far above the 0.80 threshold. The pair is cross-repo with no cached verdict, so it goes to the model, which sees two identical issues in two repos and answers `close-and-link` (proposing to close the **live** issue as a duplicate of one that no longer exists) or `transfer` (proposing to move it back). Before this plan `transfer` was inert; afterwards it is actionable, so the pipeline could recommend a transfer loop.

**Why deletion rather than marking.** Marking the ghost `CLOSED` is not stable: `embed_repo` selects `WHERE repo=? AND is_pr=0` with no state filter, so the next `embed` run re-creates any vector that was deleted while the `issues` row survives. Adding a state filter to `embed_repo` is not an option — the index intentionally covers closed issues, which duplicate detection needs. So the row must go.

**Why sync-side rather than at "Mark transferred".** Reconciliation is trigger-independent: it catches a maintainer transferring an issue on their own initiative, which the review app never hears about. It therefore subsumes any per-decision cleanup, so Task 9 needs no change.

**Files:**
- Modify: `src/triage_verse/db.py` (append `delete_issue`)
- Modify: `src/triage_verse/sync.py:97-110` (`sync_issues` collects seen numbers, reconciles on full) and `:240-259` (`sync_all` passes `log` through)
- Modify: `src/triage_verse/review_queue.py:79-81` (replace `_is_closed`) and `:146-151` (its call site)
- Test: `tests/triage_verse/test_sync_reconcile.py` (create), `tests/triage_verse/test_review_queue.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-9.
- Produces: `db.delete_issue(con, repo: str, number: int) -> None`; `sync.reconcile_repo(con, repo: str, seen: set[int]) -> list[int]` returning the deleted numbers sorted ascending; `review_queue._not_reviewable(con, repo: str, number: int) -> bool`.

- [ ] **Step 1: Write the failing tests**

Create `tests/triage_verse/test_sync_reconcile.py`:

```python
"""Reconciling mirrored issues that GitHub no longer lists (transferred/deleted)."""

from triage_verse import candidates, db, embed, sync


def _insert(con, repo, number, title="T", body="B", state="OPEN"):
    con.execute(
        "INSERT INTO issues (repo, number, title, body, state, created_at,"
        " updated_at, is_pr) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00Z',"
        " '2026-06-01T00:00:00Z', 0)",
        (repo, number, title, body, state),
    )
    con.commit()


def test_delete_issue_removes_row_comments_and_vector(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    db.upsert_comment(
        con,
        {
            "repo": "r/a",
            "issue_number": 1,
            "comment_id": 99,
            "author": "x",
            "body": "hi",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    db.upsert_vector(con, "r/a", 1, "h", embed.FakeEmbedder().embed(["T\nB"])[0])
    con.commit()

    db.delete_issue(con, "r/a", 1)

    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_embed_hash(con, "r/a", 1) is None
    assert (
        con.execute(
            "SELECT COUNT(*) FROM comments WHERE repo='r/a' AND issue_number=1"
        ).fetchone()[0]
        == 0
    )
    assert con.execute("SELECT COUNT(*) FROM vec_issues").fetchone()[0] == 0


def test_reconcile_repo_deletes_only_unseen_issues(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)
    _insert(con, "r/b", 3)

    gone = sync.reconcile_repo(con, "r/a", {2})

    assert gone == [1]
    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_issue(con, "r/a", 2) is not None
    assert db.get_issue(con, "r/b", 3) is not None


def _graphql_returning(numbers):
    def graphql(query, variables):
        return {
            "repository": {
                "issues": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": n,
                            "title": "T",
                            "body": "B",
                            "state": "OPEN",
                            "stateReason": None,
                            "author": {"login": "a"},
                            "labels": {"nodes": []},
                            "assignees": {"nodes": []},
                            "milestone": None,
                            "comments": {"totalCount": 0},
                            "reactions": {"totalCount": 0},
                            "createdAt": "2026-01-01T00:00:00Z",
                            "updatedAt": "2026-06-01T00:00:00Z",
                            "closedAt": None,
                        }
                        for n in numbers
                    ],
                }
            }
        }

    return graphql


def test_full_sync_retires_a_transferred_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([2]), full=True)

    assert db.get_issue(con, "r/a", 1) is None
    assert db.get_issue(con, "r/a", 2) is not None


def test_incremental_sync_never_retires_anything(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    _insert(con, "r/a", 2)

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([2]), full=False)

    assert db.get_issue(con, "r/a", 1) is not None


def test_retired_ghost_stops_pairing_with_its_transferred_copy(tmp_path):
    """The bug this task exists to prevent: A#1 transferred to r/b#9."""
    import types

    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1, title="crash on init", body="stack trace")
    _insert(con, "r/b", 9, title="crash on init", body="stack trace")
    embedder = embed.FakeEmbedder()
    embed.embed_repo(con, "r/a", embedder)
    embed.embed_repo(con, "r/b", embedder)
    cfg = types.SimpleNamespace(cosine_threshold=0.8, candidate_top_k=10)

    before = candidates.candidate_pairs(con, cfg)
    assert any(
        {(a[0], a[1]), (b[0], b[1])} == {("r/a", 1), ("r/b", 9)} for a, b in before
    )

    sync.sync_issues(con, "r/a", graphql=_graphql_returning([]), full=True)

    after = candidates.candidate_pairs(con, cfg)
    assert not any(("r/a", 1) in ((a[0], a[1]), (b[0], b[1])) for a, b in after)


def test_reembedding_does_not_resurrect_a_retired_issue(tmp_path):
    con = db.connect(tmp_path / "m.sqlite")
    _insert(con, "r/a", 1)
    embed.embed_repo(con, "r/a", embed.FakeEmbedder())
    sync.sync_issues(con, "r/a", graphql=_graphql_returning([]), full=True)

    embed.embed_repo(con, "r/a", embed.FakeEmbedder())

    assert db.get_embed_hash(con, "r/a", 1) is None
```

Add to `tests/triage_verse/test_review_queue.py`:

```python
def test_proposal_for_a_retired_issue_leaves_the_queue(tmp_path):
    """A missing mirror row means the issue was transferred or deleted."""
    con = db.connect(tmp_path / "m.sqlite")
    props = tmp_path / "proposals"
    props.mkdir()
    (props / "a.jsonl").write_text(
        json.dumps(
            {
                "id": "p1",
                "repo": "r/a",
                "issue": 1,
                "action": "add-label",
                "params": {"label": "regression"},
                "confidence": 0.9,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()

    # No issues row at all: the mirror has no such issue.
    assert review_queue.load_undecided(props, decisions_dir, con) == []
```

Confirm `db` is imported at the top of `test_review_queue.py`; add it if missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/triage_verse/test_sync_reconcile.py tests/triage_verse/test_review_queue.py::test_proposal_for_a_retired_issue_leaves_the_queue -v`
Expected: FAIL — `AttributeError: module 'triage_verse.db' has no attribute 'delete_issue'`, and the queue test returns one proposal instead of none.

- [ ] **Step 3: Write minimal implementation**

Append to `src/triage_verse/db.py`:

```python
def delete_issue(con: sqlite3.Connection, repo: str, number: int) -> None:
    """Remove an issue and its derived rows from the mirror.

    For issues GitHub no longer has in `repo` -- transferred away, or deleted.
    The embedding rows must go too, and the `issues` row must go rather than
    merely being marked closed: `embed_repo` has no state filter, so a surviving
    row would be re-embedded, and `knn` ignores state, so a surviving vector
    would keep surfacing as a duplicate candidate.

    Classification and dedup history keyed to (repo, number) is deliberately
    left in place, as are the append-only JSONL logs, so the audit trail
    survives the row.
    """
    row = con.execute(
        "SELECT id FROM issue_vectors WHERE repo=? AND number=?", (repo, number)
    ).fetchone()
    if row is not None:
        con.execute("DELETE FROM vec_issues WHERE rowid=?", (row["id"],))
        con.execute(
            "DELETE FROM issue_vectors WHERE repo=? AND number=?", (repo, number)
        )
    con.execute(
        "DELETE FROM comments WHERE repo=? AND issue_number=?", (repo, number)
    )
    con.execute(
        "DELETE FROM issues WHERE repo=? AND number=? AND is_pr=0", (repo, number)
    )
```

In `src/triage_verse/sync.py`, add above `sync_issues`:

```python
def reconcile_repo(con: sqlite3.Connection, repo: str, seen: set[int]) -> list[int]:
    """Delete mirrored issues of `repo` that GitHub no longer lists.

    Only sound after an exhaustive walk (`full=True`): an incremental sync stops
    at the stored cursor and legitimately never sees older issues, so absence
    there means nothing. A transferred issue leaves its source repo altogether,
    so absence from a full walk is the only signal available.
    """
    rows = con.execute(
        "SELECT number FROM issues WHERE repo=? AND is_pr=0", (repo,)
    ).fetchall()
    gone = sorted(r["number"] for r in rows if r["number"] not in seen)
    for number in gone:
        db.delete_issue(con, repo, number)
    con.commit()
    return gone
```

Replace `sync_issues` with a version that collects seen numbers and reconciles:

```python
def sync_issues(
    con: sqlite3.Connection,
    repo: str,
    *,
    graphql: Callable = gh_graphql,
    full: bool = False,
    log: Callable[[str], None] = print,
) -> int:
    seen: set[int] = set()

    def upsert(con_: sqlite3.Connection, node: dict) -> int:
        db.upsert_issue(con_, parse_issue_node(repo, node))
        seen.add(node["number"])
        return 1

    count = _walk_updated_desc(
        con, repo, "issues", ISSUES_QUERY, "issues", upsert, graphql, full
    )
    # A full walk is exhaustive (no cursor, so it exits only when the connection
    # is drained), and an exception would have propagated before reaching here.
    if full:
        gone = reconcile_repo(con, repo, seen)
        if gone:
            log(
                f"  reconcile {repo}: retired {len(gone)} issue(s) GitHub no longer "
                f"lists (transferred or deleted): {gone}"
            )
    return count
```

In `sync_all`, pass the logger through:

```python
            totals["issues"] += sync_issues(con, repo, full=full, log=log)
```

In `src/triage_verse/review_queue.py`, replace `_is_closed` with:

```python
def _not_reviewable(con: sqlite3.Connection, repo: str, number: int) -> bool:
    """True when the mirror says this issue cannot be reviewed.

    Either it is closed, or it is absent — which means sync reconciliation
    retired it because GitHub no longer lists it in this repo (transferred away
    or deleted), so any proposal about it is moot.
    """
    issue = db.get_issue(con, repo, number)
    return issue is None or issue["state"] != "OPEN"
```

and update its call site inside `load_undecided`:

```python
        if (
            pid in terminal_ids
            or r.get("action") not in SUPPORTED_ACTIONS
            or _not_reviewable(con, r["repo"], r["issue"])
        ):
            continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/triage_verse/test_sync_reconcile.py tests/triage_verse/test_review_queue.py tests/triage_verse/test_sync_issues.py tests/triage_verse/test_candidates.py -v`
Expected: PASS. `test_sync_issues.py` and `test_candidates.py` are included because they exercise the changed `sync_issues` signature and the candidate pipeline; if a pre-existing test asserted `_is_closed` by name, update it to `_not_reviewable`.

- [ ] **Step 5: Run the full gate**

Run: `make py-check`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/triage_verse/db.py src/triage_verse/sync.py src/triage_verse/review_queue.py tests/triage_verse/test_sync_reconcile.py tests/triage_verse/test_review_queue.py
git commit -m "fix(sync): retire mirrored issues GitHub no longer lists

A transferred issue leaves its source repo, but upsert-only sync froze the row
at OPEN forever. The ghost then paired with its own transferred copy as a
near-identical cross-repo duplicate, so dedup would propose closing the live
issue as a duplicate of one that no longer exists. Full syncs now diff the
mirrored set against GitHub and delete what vanished."
```

---

## Self-review notes

**Spec coverage.** Prompt definitions → Task 1. Action mapping and the degrade-to-`close-duplicate` fallback → Task 2. `link-duplicate.md` template → Task 3. Both executor branches, and the "no transfer mutation" property → Task 4. Stakes (`link-duplicate`/`suggest-transfer` deliberately not high-stakes), `transferred` as terminal, and the destination helper → Task 5. Autonomy exclusion and the egress-guard regression → Task 6. Completion records → Task 7. Readable params, badge, drawer block → Task 8. Transfers panel and `Mark transferred` → Task 9.

**Unchanged behavior is asserted, not assumed.** Tasks 2 and 4 both re-run the pre-existing `close-duplicate` tests, which cover the same-repo native duplicate close and the `close-and-link` cross-repo fallback. Neither path is edited.

**Deliberately not done:** no schema migration (the column exists), no `gh.py` change (so `transferIssue` keeps failing closed), no comment on `suggest-transfer` (a public "belongs elsewhere" note would go stale the moment someone moved the issue), and no reciprocal comment on the canonical issue for `link-duplicate` (a proposal targets one issue, and dedup emits no reciprocal proposal).

**Known gap, carried from the spec:** coverage is limited to issues the dedup stage paired *across repositories*. A plainly misfiled issue that duplicates nothing gets no suggestion, because `cross_repo_option` only exists on a pair verdict. Catching those needs a per-issue classification signal — a separate design.

**Task 10 is a prerequisite for shipping, not a nice-to-have.** Tasks 1-9 make `transfer` actionable, and an actioned transfer creates the ghost row that makes dedup recommend undoing it. Do not ship Tasks 1-9 without Task 10. It is ordered last only because it is independent of them; running it first is equally valid.

**Reconciliation runs on full syncs only,** because an incremental sync stops at the stored cursor and absence from that window carries no information — inferring deletion from it would retire live issues. So ghosts persist until the next `sync --full`.

At current scale that caveat is close to vacuous. Measured 2026-07-27, the active 4-repo pilot is a **~30-request** full sync (437 issues, 392 PRs, 1,212 comments; 50/page for issues and PRs, 100/page for comments), and sync costs no LLM spend at all — so `--full` can run on every scheduled pass and reconciliation is effectively continuous. Reconciliation itself adds **zero** requests: it is a `SELECT number FROM issues WHERE repo=?` diffed against a set already in memory.

This changes once `rstudio/shiny` or `posit-dev/py-shiny` is activated in `config/repos.yaml`. `rstudio/shiny` alone is ~195 requests — 6.5x the entire current pilot — and `posit-dev/py-shiny` ~75; the full 41 repos are ~767. With either of the big two active, syncs go incremental and `--full` runs sparingly, at which point ghosts really can persist between full passes. The policy and the measured figures are recorded at the top of `config/repos.yaml`, next to the lines that trigger the change.

**Residual gap after Task 10:** the transferred issue is still re-analyzed from scratch in its new repo, since nothing links B#N back to A#1. That is defensible — labels and priority appropriate in repo B may genuinely differ — but it does mean repeated analysis spend. Separately, if repo B is not active in `config/repos.yaml`, the transferred issue is never mirrored and leaves the system silently.
