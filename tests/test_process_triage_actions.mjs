// Unit tests for sanitization helpers in process-triage-actions.mjs.
// Run with: node --test tests/test_process_triage_actions.mjs

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildComment,
  commentKindForLabels,
  neuterMentions,
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
