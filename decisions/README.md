# Decision records

This folder holds **decision records**: short, dated documents that capture a
choice we made, the options we weighed, and why we landed where we did.

They exist so that six months from now — or for a teammate who wasn't in the
room — the *reasoning* behind a choice is recoverable, not just the outcome.
The code shows what we did; a decision record shows why, and what we
deliberately turned down.

## When to write one

Write a record when a choice is:

- **Hard to reverse** or expensive to revisit — a dependency, a storage format,
  a data model, an external service.
- **Non-obvious** — a reasonable engineer might have chosen differently, or the
  "obvious" option was rejected for a reason worth remembering.
- **Cross-cutting** — it shapes more than the one file it lives next to.

Skip it for choices the code already makes self-evident, or that are trivially
reversible.

## Format

One file per decision, named `YYYY-MM-DD-<short-kebab-topic>.md`, dated the day
the decision was made. Begin with a short header:

```markdown
# <Title — the decision, stated as a conclusion>

**Date:** YYYY-MM-DD
**Status:** Decided | Superseded by <file> | Notes
**Related:** optional pointers (issues, specs), by name — not as a dependency
```

Then a body along these lines (adapt as the decision needs):

- **Context** — what problem forced the choice, and which constraints matter.
- **Options considered** — each with honest **pros and cons**, including both
  the option we picked and the ones we rejected.
- **Decision** — what we chose, and the deciding factor.
- **Consequences** — what follows: new obligations, risks, follow-ups.

## House rules

- **Each record stands alone.** A reader should understand it without opening
  any other document. Explain context in prose; don't lean on section-number
  references (e.g. "see section 5") into other files — restate what you need.
- **Record the costs honestly.** A record that lists only upsides isn't a
  decision, it's an advertisement. Name the downsides of the option you chose.
- **Decisions are immutable; status changes.** Don't rewrite history when a
  choice is revisited. Add a new record and mark the old one
  `Superseded by <new file>`.
