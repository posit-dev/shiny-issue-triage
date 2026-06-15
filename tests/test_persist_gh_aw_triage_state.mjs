import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildTriageRecord,
  mergeCursors,
  validateCursorUpdates,
} from '../.github/triage/scripts/persist-gh-aw-triage-state.mjs';

test('mergeCursors preserves existing repos and applies incoming cursor updates', () => {
  assert.deepEqual(
    mergeCursors(
      {
        'rstudio/reactlog': { updatedAt: '2026-06-01T00:00:00Z' },
        'rstudio/shiny': { updatedAt: '2026-06-02T00:00:00Z' },
      },
      {
        'rstudio/shiny': { updatedAt: '2026-06-15T12:00:00Z' },
      },
    ),
    {
      'rstudio/reactlog': { updatedAt: '2026-06-01T00:00:00Z' },
      'rstudio/shiny': { updatedAt: '2026-06-15T12:00:00Z' },
    },
  );
});

test('validateCursorUpdates rejects malformed repo keys', () => {
  assert.throws(
    () => validateCursorUpdates({ 'not-a-repo': { updatedAt: '2026-06-15T12:00:00Z' } }),
    /owner\/repo/,
  );
});

test('validateCursorUpdates rejects non-object cursor values', () => {
  assert.throws(
    () => validateCursorUpdates({ 'rstudio/shiny': '2026-06-15T12:00:00Z' }),
    /cursor value/,
  );
});

test('buildTriageRecord creates an auditable JSONL entry', () => {
  const record = buildTriageRecord(
    {
      summary: 'Triaged one issue.',
      actions: [{ action: 'triage', repo: 'rstudio/shiny', issue_number: '123' }],
    },
    {
      runId: '42',
      repository: 'posit-dev/shiny-issue-triage',
      serverUrl: 'https://github.com',
      workflow: 'Team Issue Triage (gh-aw)',
      timestamp: '2026-06-15T12:00:00.000Z',
    },
  );

  assert.deepEqual(record, {
    timestamp: '2026-06-15T12:00:00.000Z',
    workflow: 'Team Issue Triage (gh-aw)',
    run_url: 'https://github.com/posit-dev/shiny-issue-triage/actions/runs/42',
    summary: 'Triaged one issue.',
    actions: [{ action: 'triage', repo: 'rstudio/shiny', issue_number: '123' }],
  });
});
