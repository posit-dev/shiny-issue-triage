import assert from 'node:assert/strict';
import test from 'node:test';

import {
  extractTriageOutput,
  extractTriageToolItem,
} from '../.github/triage/scripts/gh-aw-output-adapter.mjs';

test('extractTriageOutput converts a gh-aw safe-output item to Claude output', () => {
  const raw = JSON.stringify({
    items: [
      {
        type: 'apply_triage_actions',
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
        type: 'apply_triage_actions',
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

test('extractTriageOutput rejects missing apply_triage_actions items', () => {
  assert.throws(
    () => extractTriageOutput(JSON.stringify({ items: [{ type: 'noop', message: 'done' }] })),
    /must call apply_triage_actions exactly once/,
  );
});

test('extractTriageOutput rejects multiple apply_triage_actions items', () => {
  const raw = JSON.stringify({
    items: [
      { type: 'apply_triage_actions', actions_json: '[]' },
      { type: 'apply_triage_actions', actions_json: '[]' },
    ],
  });

  assert.throws(() => extractTriageOutput(raw), /exactly once/);
});

test('extractTriageOutput rejects non-array actions_json', () => {
  const raw = JSON.stringify({
    items: [{ type: 'apply_triage_actions', actions_json: JSON.stringify({ summary: 'bad' }) }],
  });

  assert.throws(() => extractTriageOutput(raw), /actions_json must be a JSON array/);
});

test('extractTriageToolItem returns optional cursor updates', () => {
  const raw = JSON.stringify({
    items: [
      {
        type: 'apply_triage_actions',
        summary: 'Triaged one issue.',
        actions_json: '[]',
        cursors_json: JSON.stringify({
          'rstudio/shiny': { updatedAt: '2026-06-15T12:00:00Z' },
        }),
      },
    ],
  });

  assert.deepEqual(extractTriageToolItem(raw).cursors, {
    'rstudio/shiny': { updatedAt: '2026-06-15T12:00:00Z' },
  });
});
