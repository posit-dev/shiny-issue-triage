import assert from 'node:assert/strict';
import test from 'node:test';

import {
  extractTriageOutput,
} from '../.github/triage/scripts/gh-aw-output-adapter.mjs';

test('extractTriageOutput converts a gh-aw safe-output item to Claude output', () => {
  const raw = JSON.stringify({
    items: [
      {
        type: 'summarize_triage_dry_run',
        summary: 'Triaged one issue.',
        actions_json: JSON.stringify([
          {
            action: 'triage',
            repo: 'rstudio/shiny',
            issue_number: '123',
            labels: ['needs reprex', 'Priority: Medium'],
            confidence: 'medium',
            rationale: 'Missing a minimal app.',
          },
        ]),
      },
    ],
  });

  assert.deepEqual(extractTriageOutput(raw), {
    summary: 'Triaged one issue.',
    actions: [
      {
        action: 'triage',
        repo: 'rstudio/shiny',
        issue_number: '123',
        labels: ['needs reprex', 'Priority: Medium'],
        confidence: 'medium',
        rationale: 'Missing a minimal app.',
      },
    ],
  });
});

test('extractTriageOutput accepts an actions object wrapper', () => {
  const raw = JSON.stringify({
    items: [
      {
        type: 'summarize_triage_dry_run',
        summary: 'Nothing to do.',
        actions_json: JSON.stringify({ actions: [] }),
      },
    ],
  });

  assert.deepEqual(extractTriageOutput(raw), {
    summary: 'Nothing to do.',
    actions: [],
  });
});

test('extractTriageOutput rejects missing summarize_triage_dry_run items', () => {
  assert.throws(
    () => extractTriageOutput(JSON.stringify({ items: [{ type: 'noop', message: 'done' }] })),
    /must call summarize_triage_dry_run exactly once/,
  );
});

test('extractTriageOutput rejects multiple summarize_triage_dry_run items', () => {
  const raw = JSON.stringify({
    items: [
      { type: 'summarize_triage_dry_run', actions_json: '[]' },
      { type: 'summarize_triage_dry_run', actions_json: '[]' },
    ],
  });

  assert.throws(() => extractTriageOutput(raw), /exactly once/);
});

test('extractTriageOutput rejects non-array actions_json', () => {
  const raw = JSON.stringify({
    items: [{ type: 'summarize_triage_dry_run', actions_json: JSON.stringify({ summary: 'bad' }) }],
  });

  assert.throws(() => extractTriageOutput(raw), /actions_json must be a JSON array/);
});
