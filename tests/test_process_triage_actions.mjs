// Unit tests for sanitization helpers in process-triage-actions.mjs.
// Run with: node --test tests/test_process_triage_actions.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import {
  buildComment,
  commentKindForLabels,
  formatPriorityBadge,
  formatSummary,
  loadClaudeOutput,
  neuterMentions,
  resolveTriageLabels,
  sanitizeComment,
  sanitizeReportBody,
  validateReportTitle,
} from '../.github/triage/scripts/process-triage-actions.mjs';

test('neuterMentions wraps @-mentions in code spans', () => {
  assert.equal(neuterMentions('hi @alice and @bob/team'), 'hi `@alice` and `@bob/team`');
});

test('sanitizeComment strips control chars and neuters mentions', () => {
  const out = sanitizeComment('please ping @maintainer\u0007 thanks');
  assert.equal(out, 'please ping `@maintainer` thanks');
});

test('sanitizeComment rejects URLs', () => {
  assert.throws(
    () => sanitizeComment('see https://evil.example.com/exfil'),
    /URLs/,
  );
});

test('sanitizeComment rejects cross-repo issue refs', () => {
  assert.throws(
    () => sanitizeComment('also see other/repo#123'),
    /other repositories/,
  );
});

test('sanitizeComment rejects GH-NNN refs', () => {
  assert.throws(() => sanitizeComment('see GH-42'), /other repositories/);
});

test('sanitizeComment rejects empty comments', () => {
  assert.throws(() => sanitizeComment('   '), /empty/);
});

test('sanitizeComment enforces length cap', () => {
  assert.throws(() => sanitizeComment('a'.repeat(2000)), /too long/);
});

test('commentKindForLabels requires exactly one classification label', () => {
  assert.equal(commentKindForLabels(['needs reprex']), 'needs reprex');
  assert.equal(commentKindForLabels(['needs clarification', 'Priority: Low']), 'needs clarification');
  assert.equal(commentKindForLabels(['needs reprex', 'duplicate']), null);
  assert.equal(commentKindForLabels(['regression']), null);
  assert.equal(commentKindForLabels([]), null);
});

test('buildComment uses the template and appends the footer', () => {
  const body = buildComment('needs reprex', 'minimal app missing.');
  assert.match(body, /minimal reproducible example/);
  assert.match(body, /minimal app missing\./);
  assert.match(body, /Posted by the Team Issue Triage workflow/);
});

test('validateReportTitle accepts conventional-commit format', () => {
  assert.doesNotThrow(() => validateReportTitle('triage(reactlog): clarify regression reproduction'));
});

test('validateReportTitle rejects bad format and oversize titles', () => {
  assert.throws(() => validateReportTitle(''), /required/);
  assert.throws(() => validateReportTitle('chore: oops'), /triage/);
  assert.throws(() => validateReportTitle('triage(reactlog): ' + 'x'.repeat(80)), /too long/);
});

test('sanitizeReportBody enforces required sections and neuters mentions', () => {
  const body = [
    '## Summary',
    'thing happened',
    '## Affected repositories',
    '- rstudio/reactlog',
    '## Evidence',
    'evidence',
    '## Recommended next action',
    'do thing',
    '## Confidence',
    'high — cc @maintainer',
  ].join('\n');
  const out = sanitizeReportBody(body);
  assert.match(out, /`@maintainer`/);
});

test('sanitizeReportBody rejects bodies missing required sections', () => {
  assert.throws(
    () => sanitizeReportBody('## Summary only'),
    /missing required section/,
  );
});

test('formatPriorityBadge returns emoji badge for known priorities', () => {
  assert.equal(formatPriorityBadge('Priority: Critical'), '🔴 Critical');
  assert.equal(formatPriorityBadge('Priority: High'), '🟠 High');
  assert.equal(formatPriorityBadge('Priority: Medium'), '🟡 Medium');
  assert.equal(formatPriorityBadge('Priority: Low'), '🟢 Low');
});

test('formatPriorityBadge returns null for unknown labels', () => {
  assert.equal(formatPriorityBadge('regression'), null);
  assert.equal(formatPriorityBadge(''), null);
});

test('formatSummary returns blockquote when no actions applied', () => {
  const out = formatSummary('Nothing to do.', []);
  assert.match(out, /No triage actions were emitted/);
  assert.ok(!out.includes('## Overview'));
});

test('formatSummary includes overview table with correct counts', () => {
  const applied = [
    { repo: 'rstudio/shiny', issue: '100', labels: ['Priority: High'], confidence: 'high' },
    { repo: 'rstudio/shiny', issue: '101', labels: ['Priority: Low', 'needs reprex'], confidence: 'medium' },
    { repo: 'posit-dev/py-shiny', issue: '200', labels: ['Priority: Low'], confidence: 'low' },
  ];
  const out = formatSummary('Test summary', applied);
  assert.match(out, /Issues triaged \| 3/);
  assert.match(out, /Repositories \| 2/);
  assert.match(out, /🟠 High \| 1/);
  assert.match(out, /🟢 Low \| 2/);
  assert.ok(!out.includes('🔴 Critical'));
  assert.ok(!out.includes('🟡 Medium'));
});

test('formatSummary groups issues by repository with tables', () => {
  const applied = [
    { repo: 'rstudio/shiny', issue: '10', labels: ['Priority: Medium'], confidence: 'high' },
    { repo: 'posit-dev/py-shiny', issue: '20', labels: ['regression', 'Priority: High'], confidence: 'medium' },
  ];
  const out = formatSummary(null, applied);
  assert.match(out, /### rstudio\/shiny/);
  assert.match(out, /### posit-dev\/py-shiny/);
  assert.match(out, /\[#10\]\(https:\/\/github\.com\/rstudio\/shiny\/issues\/10\)/);
  assert.match(out, /\[#20\]\(https:\/\/github\.com\/posit-dev\/py-shiny\/issues\/20\)/);
  assert.match(out, /`regression`/);
  assert.ok(!out.includes('<details>'));
});

test('formatSummary renders confidence indicators', () => {
  const applied = [
    { repo: 'rstudio/shiny', issue: '1', labels: ['Priority: Low'], confidence: 'high' },
    { repo: 'rstudio/shiny', issue: '2', labels: ['Priority: Low'], confidence: 'medium' },
    { repo: 'rstudio/shiny', issue: '3', labels: ['Priority: Low'], confidence: 'low' },
  ];
  const out = formatSummary('s', applied);
  assert.match(out, /✅ high/);
  assert.match(out, /⚠️ medium/);
  assert.match(out, /❓ low/);
});

test('formatSummary includes Claude summary in a clean header section', () => {
  const applied = [
    { repo: 'rstudio/shiny', issue: '1', labels: ['Priority: Low'], confidence: 'high' },
  ];
  const out = formatSummary('A detailed summary.', applied);
  assert.match(out, /## Triage Summary/);
  assert.match(out, /A detailed summary\./);
});

test('formatSummary shows dash for issues without priority labels', () => {
  const applied = [
    { repo: 'rstudio/shiny', issue: '5', labels: ['needs clarification', 'ai-triage:needs-review'], confidence: 'low' },
  ];
  const out = formatSummary(null, applied);
  const tableLines = out.split('\n').filter((l) => l.startsWith('| ['));
  assert.equal(tableLines.length, 1);
  assert.match(tableLines[0], /\| — \|/);
  assert.match(tableLines[0], /`needs clarification`/);
  assert.match(tableLines[0], /`ai-triage:needs-review`/);
});

test('formatSummary shows rationale column when present', () => {
  const applied = [
    { repo: 'rstudio/shiny', issue: '1', labels: ['Priority: Low'], confidence: 'high', rationale: 'This is a test rationale.' },
  ];
  const out = formatSummary(null, applied);
  assert.match(out, /\| Issue \| Priority \| Labels \| Confidence \| Rationale \|/);
  assert.match(out, /\| \[#1\].*?\| This is a test rationale\. \|/);
});

test('resolveTriageLabels applies done or needs-review based on confidence/labels', () => {
  assert.deepEqual(resolveTriageLabels(['Priority: Low'], 'medium'), ['Priority: Low', 'ai-triage:done']);
  assert.deepEqual(resolveTriageLabels(['Priority: Low', 'ai-triage:done'], 'high'), ['Priority: Low', 'ai-triage:done']);
  assert.deepEqual(resolveTriageLabels(['Priority: Low', 'ai-triage:done'], 'low'), ['Priority: Low', 'ai-triage:needs-review']);
  assert.deepEqual(resolveTriageLabels(['Priority: Low', 'ai-triage:needs-review', 'ai-triage:done'], 'medium'), ['Priority: Low', 'ai-triage:needs-review']);
});

test('loadClaudeOutput reads the fallback JSON file', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'triage-output-'));
  const outputFile = path.join(directory, 'output.json');
  t.after(() => fs.rmSync(directory, { recursive: true }));
  fs.writeFileSync(outputFile, '{"summary":"done","actions":[]}');

  assert.deepEqual(loadClaudeOutput(outputFile), {
    summary: 'done',
    actions: [],
  });
});
