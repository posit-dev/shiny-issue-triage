#!/usr/bin/env node
// Validates and applies the structured output produced by the Claude triage
// step. The model output is treated as untrusted: every action is checked
// against the repo and label allowlists, and only labels are ever written.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const LIMITS = Object.freeze({
  actions: 30,
  labelsPerItem: 6,
  comment: 1000,
  report: 60000,
  reportTitle: 72,
});

const REPORT_TITLE_RE = /^triage\([a-z0-9._-]+\): \S.*\S$/;
const REPORT_SECTIONS = Object.freeze([
  '## Summary',
  '## Affected repositories',
  '## Evidence',
  '## Recommended next action',
  '## Confidence',
]);

const CLASSIFICATION_LABELS = new Set([
  'regression',
  'duplicate',
  'wrong location',
  'needs reprex',
  'needs clarification',
]);

// These helpers remain exported for the unit tests and for any downstream
// code that still imports them, even though the workflow no longer posts
// comments or creates report issues.
const CONTROL_CHARS = /[\u0000-\u0008\u000B-\u001F\u007F]/g;
const MENTION_RE = /@([A-Za-z0-9][A-Za-z0-9-]*(?:\/[A-Za-z0-9._-]+)?)/g;
const URL_RE = /https?:\/\/\S+/gi;
const CROSS_REPO_REF_RE = /\b[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9._-]+#\d+\b/g;
const GH_SHORT_REF_RE = /\bGH-\d+\b/g;

export function neuterMentions(text) {
  return String(text).replace(MENTION_RE, '`@$1`');
}

export function sanitizeComment(raw) {
  const stripped = String(raw).replace(CONTROL_CHARS, '').trim();
  if (!stripped) {
    throw new Error('Comment is empty after sanitization.');
  }
  if (stripped.length > LIMITS.comment) {
    throw new Error(`Comment too long: ${stripped.length} > ${LIMITS.comment}`);
  }
  if (URL_RE.test(stripped)) {
    throw new Error('Comment may not contain URLs.');
  }
  if (CROSS_REPO_REF_RE.test(stripped) || GH_SHORT_REF_RE.test(stripped)) {
    throw new Error('Comment may not reference issues in other repositories.');
  }
  return neuterMentions(stripped);
}

export function commentKindForLabels(labels) {
  const classification = labels.filter((label) => CLASSIFICATION_LABELS.has(label));
  if (classification.length !== 1) return null;
  const kind = classification[0];
  return kind === 'needs reprex' || kind === 'needs clarification' ? kind : null;
}

export function buildComment(kind, sanitizedDetails) {
  const template = {
    'needs reprex': [
      'Thanks for the report. To investigate this we need a minimal reproducible example (reprex):',
      '- A small, runnable Shiny app or script that triggers the behavior.',
      '- The exact steps you ran and what you observed versus what you expected.',
      '- The output of `sessionInfo()` (R) or `pip list` / `python -V` (Python), and your browser if relevant.',
      '',
      'Reporter context provided by triage:',
      '',
      '{details}',
    ].join('\n'),
    'needs clarification': [
      'Thanks for the report. Before we can route this we need a bit more information:',
      '',
      '{details}',
    ].join('\n'),
  }[kind];
  if (!template) {
    throw new Error(`Unknown comment template: ${kind}`);
  }
  return `${template.replace('{details}', sanitizedDetails)}\n\n_Posted by the Team Issue Triage workflow. A maintainer will follow up; please reply in this thread._`;
}

export function sanitizeReportBody(raw) {
  const stripped = String(raw).replace(CONTROL_CHARS, '');
  if (stripped.length > LIMITS.report) {
    throw new Error(`Report body is too long: ${stripped.length} > ${LIMITS.report}`);
  }
  for (const heading of REPORT_SECTIONS) {
    if (!stripped.includes(heading)) {
      throw new Error(`Report body is missing required section heading: ${heading}`);
    }
  }
  return neuterMentions(stripped);
}

export function validateReportTitle(title) {
  if (!title) throw new Error('report_title is required.');
  if (title.length > LIMITS.reportTitle) {
    throw new Error(`Report title is too long: ${title.length} > ${LIMITS.reportTitle}`);
  }
  if (!REPORT_TITLE_RE.test(title)) {
    throw new Error(`Report title must match 'triage(<scope>): <summary>': ${title}`);
  }
}

function fail(message) {
  console.error(message);
  process.exit(1);
}

function env(name, { required = true } = {}) {
  const value = process.env[name];
  if (required && (value === undefined || value === '')) {
    fail(`${name} is required.`);
  }
  return value ?? '';
}

function splitCsv(value) {
  return String(value || '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function readTokens(filePath) {
  if (!filePath) {
    fail('TRIAGE_ISSUE_TOKENS_FILE is required for issue writes.');
  }
  try {
    const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    return parsed.tokens || {};
  } catch (error) {
    fail(`Could not read issue token map: ${error.message}`);
  }
  return {};
}

function parseClaudeOutput(raw) {
  if (!String(raw || '').trim()) {
    fail('Claude did not produce structured output. Cannot process triage actions.');
  }
  try {
    return JSON.parse(raw);
  } catch (error) {
    fail(`Claude structured output was not valid JSON: ${error.message}`);
  }
  return {};
}

function runGh(args, { token }) {
  if (!token) {
    fail('runGh called without a token.');
  }
  execFileSync('gh', args, {
    stdio: 'inherit',
    env: { ...process.env, GH_TOKEN: token },
  });
}

const PRIORITY_BADGES = new Map([
  ['Priority: Critical', '🔴 Critical'],
  ['Priority: High', '🟠 High'],
  ['Priority: Medium', '🟡 Medium'],
  ['Priority: Low', '🟢 Low'],
]);

export function formatPriorityBadge(label) {
  return PRIORITY_BADGES.get(label) || null;
}

export function formatSummary(claudeSummary, applied) {
  const lines = [];
  lines.push('# Team Issue Triage Applied Changes');
  lines.push('');

  if (!applied.length) {
    lines.push('> No triage actions were emitted.');
    lines.push('');
    return lines.join('\n');
  }

  const repos = new Set(applied.map((a) => a.repo));
  const priorityCounts = new Map();
  for (const entry of applied) {
    for (const label of entry.labels) {
      if (PRIORITY_BADGES.has(label)) {
        priorityCounts.set(label, (priorityCounts.get(label) || 0) + 1);
      }
    }
  }

  lines.push('## Overview');
  lines.push('');
  lines.push('| Metric | Count |');
  lines.push('| --- | ---: |');
  lines.push(`| Issues triaged | ${applied.length} |`);
  lines.push(`| Repositories | ${repos.size} |`);
  for (const [label, badge] of PRIORITY_BADGES) {
    const count = priorityCounts.get(label) || 0;
    if (count > 0) {
      lines.push(`| ${badge} | ${count} |`);
    }
  }
  lines.push('');

  if (claudeSummary) {
    lines.push('<details>');
    lines.push('<summary>Claude summary</summary>');
    lines.push('');
    lines.push(claudeSummary);
    lines.push('');
    lines.push('</details>');
    lines.push('');
  }

  const byRepo = new Map();
  for (const entry of applied) {
    if (!byRepo.has(entry.repo)) byRepo.set(entry.repo, []);
    byRepo.get(entry.repo).push(entry);
  }

  for (const [repo, entries] of byRepo) {
    lines.push(`### ${repo}`);
    lines.push('');
    lines.push('| Issue | Priority | Labels | Confidence |');
    lines.push('| --- | --- | --- | --- |');
    for (const entry of entries) {
      const issueLink = `[#${entry.issue}](https://github.com/${repo}/issues/${entry.issue})`;
      const priorityLabel = entry.labels.find((l) => PRIORITY_BADGES.has(l));
      const priority = priorityLabel ? formatPriorityBadge(priorityLabel) : '—';
      const otherLabels = entry.labels
        .filter((l) => !PRIORITY_BADGES.has(l))
        .map((l) => `\`${l}\``)
        .join(', ') || '—';
      const confidence = entry.confidence === 'high' ? '✅ high'
        : entry.confidence === 'medium' ? '⚠️ medium'
        : entry.confidence === 'low' ? '❓ low'
        : entry.confidence;
      lines.push(`| ${issueLink} | ${priority} | ${otherLabels} | ${confidence} |`);
    }
    lines.push('');
  }

  return lines.join('\n');
}

function main() {
  const allowedRepos = new Set(splitCsv(env('TRIAGE_ALLOWED_REPOS')));
  if (allowedRepos.size === 0) {
    fail('TRIAGE_ALLOWED_REPOS is empty. Refusing to process actions.');
  }
  const allowedLabels = new Set(splitCsv(env('TRIAGE_ALLOWED_LABELS')));
  if (allowedLabels.size === 0) {
    fail('TRIAGE_ALLOWED_LABELS is empty. Check labels.yaml allowed_safe_output_labels.');
  }
  const maxIssuesPerRepo = parseInt(env('TRIAGE_MAX_ISSUES_PER_REPO', { required: false }) || '0', 10);

  const issueTokens = readTokens(env('TRIAGE_ISSUE_TOKENS_FILE'));
  const tokenForRepo = (repo) => {
    const token = issueTokens[repo];
    if (!token) fail(`No write-capable GitHub App token is available for ${repo}.`);
    return token;
  };

  const output = parseClaudeOutput(env('CLAUDE_OUTPUT'));
  const items = Array.isArray(output.actions) ? output.actions : [];
  if (items.length > LIMITS.actions) {
    fail(`Too many triage actions: ${items.length} > ${LIMITS.actions}`);
  }

  const applied = [];
  const perRepoCounts = new Map();

  for (const item of items) {
    const action = String(item.action || '').toLowerCase();
    if (action !== 'triage') {
      fail(`Unknown triage action: ${item.action}`);
    }

    const repo = String(item.repo || '');
    const issueNumber = String(item.issue_number || '');
    if (!allowedRepos.has(repo)) fail(`Repository is not allowlisted: ${repo}`);
    if (!/^[1-9][0-9]*$/.test(issueNumber)) fail(`Issue number must be a positive integer: ${issueNumber}`);

    if (maxIssuesPerRepo > 0) {
      const next = (perRepoCounts.get(repo) || 0) + 1;
      if (next > maxIssuesPerRepo) {
        fail(`Per-repo action cap exceeded for ${repo}: ${next} > ${maxIssuesPerRepo}`);
      }
      perRepoCounts.set(repo, next);
    }

    const labels = Array.isArray(item.labels) ? item.labels.map(String).filter(Boolean) : [];
    if (labels.length > LIMITS.labelsPerItem) {
      fail(`Too many labels for one item: ${labels.length} > ${LIMITS.labelsPerItem}`);
    }
    for (const label of labels) {
      if (!allowedLabels.has(label)) fail(`Label is not allowlisted: ${label}`);
    }

    const token = tokenForRepo(repo);

    for (const label of labels) {
      runGh(['issue', 'edit', issueNumber, '--repo', repo, '--add-label', label], { token });
    }

    applied.push({
      repo,
      issue: issueNumber,
      labels,
      confidence: String(item.confidence || 'unknown'),
    });
  }

  const body = formatSummary(output.summary, applied);
  if (process.env.GITHUB_STEP_SUMMARY) {
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, body);
  }
  console.log(body);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
