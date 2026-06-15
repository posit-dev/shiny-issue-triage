import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildDryRunPlan,
  formatDryRunSummary,
} from '../.github/triage/scripts/dry-run-triage-actions.mjs';

test('buildDryRunPlan validates and normalizes proposed triage actions without applying them', () => {
  const plan = buildDryRunPlan(
    {
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
    },
    {
      allowedRepos: ['rstudio/shiny'],
      allowedLabels: ['needs reprex', 'Priority: Medium', 'ai-triage:done', 'ai-triage:needs-review'],
      maxIssuesPerRepo: 25,
    },
  );

  assert.deepEqual(plan, [
    {
      repo: 'rstudio/shiny',
      issue: '123',
      labels: ['needs reprex', 'Priority: Medium', 'ai-triage:done'],
      confidence: 'medium',
      rationale: 'Missing a minimal app.',
    },
  ]);
});

test('buildDryRunPlan rejects labels outside the allowlist', () => {
  assert.throws(
    () => buildDryRunPlan(
      {
        actions: [
          {
            action: 'triage',
            repo: 'rstudio/shiny',
            issue_number: '123',
            labels: ['unapproved-label'],
            confidence: 'medium',
          },
        ],
      },
      {
        allowedRepos: ['rstudio/shiny'],
        allowedLabels: ['ai-triage:done'],
      },
    ),
    /Label is not allowlisted: unapproved-label/,
  );
});

test('buildDryRunPlan enforces per-repo action caps', () => {
  assert.throws(
    () => buildDryRunPlan(
      {
        actions: [
          { action: 'triage', repo: 'rstudio/shiny', issue_number: '1', labels: [], confidence: 'low' },
          { action: 'triage', repo: 'rstudio/shiny', issue_number: '2', labels: [], confidence: 'low' },
        ],
      },
      {
        allowedRepos: ['rstudio/shiny'],
        allowedLabels: ['ai-triage:needs-review'],
        maxIssuesPerRepo: 1,
      },
    ),
    /Per-repo action cap exceeded/,
  );
});

test('formatDryRunSummary makes non-mutation explicit', () => {
  const summary = formatDryRunSummary('Triaged one issue.', [
    {
      repo: 'rstudio/shiny',
      issue: '123',
      labels: ['needs reprex', 'Priority: Medium', 'ai-triage:done'],
      confidence: 'medium',
      rationale: 'Missing a minimal app.',
    },
  ]);

  assert.match(summary, /# Team Issue Triage Dry Run/);
  assert.match(summary, /No GitHub labels were applied/);
  assert.match(summary, /https:\/\/github.com\/rstudio\/shiny\/issues\/123/);
  assert.match(summary, /`needs reprex`, `ai-triage:done`/);
});
