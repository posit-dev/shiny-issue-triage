# Embedding runtime: fastembed over sentence-transformers

**Date:** 2026-06-29
**Status:** Decided — adopted for the analysis pipeline
**Related:** GitHub issue #8 (analysis pipeline); `docs/superpowers/specs/2026-06-12-shinyverse-issue-triage-design.md`

## Context

The triage hub mirrors issues, pull requests, and comments from roughly forty
repositories into a local SQLite database. The analysis pipeline turns that
mirror into triage proposals. One stage finds **duplicate candidates**: for each
open issue it retrieves the most similar other issues — across all
repositories — by semantic similarity, then hands those candidate pairs to a
language model that makes the actual duplicate / related / distinct call.

The retrieval step needs sentence embeddings over each issue's title and body.
Embeddings are computed locally (no embedding vendor, no per-token cost) and
stored in SQLite through the `sqlite-vec` extension. The chosen model is
`sentence-transformers/all-MiniLM-L6-v2` — a small, widely used,
384-dimension model that strikes a good speed/quality balance for short-text
retrieval.

Embeddings serve **retrieval only**. Their job is to surface a generous
candidate set (the nearest neighbors above a cosine threshold) that a language
model then adjudicates. Recall at this stage is what matters; a few extra
false-positive candidates are cheap, because the model filters them out.
Embedding *precision* is not load-bearing.

This decision is about the **runtime library** used to produce the vectors, not
about the model. The model id is the same under every option and stays
configurable.

Two execution surfaces shape the choice, and both run this tool constantly:

- **Continuous integration**, where install time and cache size are paid on
  every run, and where tests must run with no network access.
- **Local laptops**, where a multi-gigabyte dependency is an unwelcome surprise
  for a tool whose whole job is small, CPU-bound, batch work.

## Options considered

### A. fastembed (ONNX Runtime) — chosen

`fastembed` runs the model through ONNX Runtime on CPU, serving the same
`sentence-transformers/all-MiniLM-L6-v2` weights.

**Pros**

- Small install — tens of megabytes, no PyTorch. Fast cold installs and small
  CI caches.
- CPU-only by design, which matches how this tool actually runs: batch jobs in
  CI and on laptops, never a GPU.
- Fast model load and inference for short text.
- Keeps the exact model the design already chose, so the stored vectors match
  the model name we document.

**Cons**

- Narrower model catalog than the PyTorch ecosystem. If we later want a model
  with no ONNX build, we would have to convert it or change runtimes.
- One more layer (ONNX Runtime) between us and the weights; rare model-specific
  quirks surface as ONNX issues rather than well-trodden PyTorch ones.
- Weights download on first use and are then cached, so the first real run (and
  the one-time smoke run) needs network access once.

### B. sentence-transformers (PyTorch)

The library the model is named for, and the most common way to run it.

**Pros**

- Reference implementation: the broadest model support and by far the most
  documentation and community answers.
- A trivial path to GPU acceleration if we ever wanted it.

**Cons**

- Pulls in PyTorch — on the order of one to two gigabytes installed. That is a
  heavy, slow dependency to install and cache on every CI run and every laptop,
  for a workload that never needs a GPU.
- Slower cold start than ONNX Runtime at this model size.

### C. model2vec (static distilled vectors)

Distills the model into a static lookup table, so an "embedding" becomes a table
lookup with no neural-network inference.

**Pros**

- The smallest footprint and the fastest of all — effectively free at runtime.

**Cons**

- Lower retrieval recall than running the real model. Because the candidate
  stage feeds a language model we want to *trust* to catch real duplicates,
  giving up recall here risks losing duplicate pairs before the model ever sees
  them. The footprint win is not worth a quiet, hard-to-detect quality loss at
  the very front of the pipeline.

## Decision

Use **fastembed (ONNX Runtime)**, keeping `sentence-transformers/all-MiniLM-L6-v2`
as the model and `sqlite-vec` as the vector store.

The deciding factor is footprint on the two surfaces that run this tool
constantly — CI and laptops — where a one-to-two-gigabyte PyTorch dependency is
a real, recurring cost that buys us nothing, because the workload is CPU-only
batch retrieval. fastembed gives the same model and the same vectors at a
fraction of the install size. model2vec is cheaper still, but trades away
retrieval recall at exactly the stage where we are counting on it, so it is
rejected.

## Consequences

- The embedding model is reached through a small internal `Embedder` interface.
  Production code uses the fastembed-backed implementation; tests inject a
  deterministic fake, so CI needs neither the model nor the network.
- The model id, vector dimension, and cosine threshold live in configuration,
  so swapping models — or runtimes — later is a config-and-implementation
  change, not a redesign.
- `sqlite-vec` loads as a SQLite extension, which requires a Python whose
  `sqlite3` module was built with extension-loading support. The uv-managed
  CPython builds used here have it; the macOS *system* Python typically does
  not. The pipeline checks this early and fails with a clear message rather
  than a cryptic error deep in a run.
- The first real run (and the one-time smoke run) downloads and caches the model
  weights once. Fully offline first use would require pre-seeding that cache.
