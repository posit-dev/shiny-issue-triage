#!/usr/bin/env node
// Validates gh-aw triage proposals and publishes a dry-run summary without
// mutating GitHub issues, labels, or the triage-state branch.

import fs from 'node:fs';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { extractTriageOutput } from './gh-aw-output-adapter.mjs';
import {
  formatPriorityBadge,
  neuterMentions,
  resolveTriageLabels,
} from './process-triage-actions.mjs';

const LIMITS = Object.freeze({
  actions: 30,
  labelsPerItem: 6,
});

export function buildDryRunPlan(output, options) {
  const allowedRepos = new Set(options.allowedRepos || []);
  const allowedLabels = new Set(options.allowedLabels || []);
  const maxIssuesPerRepo = Number.parseInt(String(options.maxIssuesPerRepo || '0'), 10);

  if (allowedRepos.size === 0) {
    throw new Error('TRIAGE_ALLOWED_REPOS is empty. Refusing to process actions.');
  }
  if (allowedLabels.size === 0) {
    throw new Error('TRIAGE_ALLOWED_LABELS is empty. Check labels.yaml allowed_safe_output_labels.');
  }

  const items = Array.isArray(output.actions) ? output.actions : [];
  if (items.length > LIMITS.actions) {
    throw new Error(`Too many triage actions: ${items.length} > ${LIMITS.actions}`);
  }

  const planned = [];
  const perRepoCounts = new Map();

  for (const item of items) {
    const action = String(item.action || '').toLowerCase();
    if (action !== 'triage') {
      throw new Error(`Unknown triage action: ${item.action}`);
    }

    const repo = String(item.repo || '');
    const issueNumber = String(item.issue_number || '');
    if (!allowedRepos.has(repo)) throw new Error(`Repository is not allowlisted: ${repo}`);
    if (!/^[1-9][0-9]*$/.test(issueNumber)) {
      throw new Error(`Issue number must be a positive integer: ${issueNumber}`);
    }

    if (maxIssuesPerRepo > 0) {
      const next = (perRepoCounts.get(repo) || 0) + 1;
      if (next > maxIssuesPerRepo) {
        throw new Error(`Per-repo action cap exceeded for ${repo}: ${next} > ${maxIssuesPerRepo}`);
      }
      perRepoCounts.set(repo, next);
    }

    const labels = resolveTriageLabels(item.labels, String(item.confidence || ''));
    if (labels.length > LIMITS.labelsPerItem) {
      throw new Error(`Too many labels for one item: ${labels.length} > ${LIMITS.labelsPerItem}`);
    }
    for (const label of labels) {
      if (!allowedLabels.has(label)) throw new Error(`Label is not allowlisted: ${label}`);
    }

    planned.push({
      repo,
      issue: issueNumber,
      labels,
      confidence: String(item.confidence || 'unknown'),
      rationale: String(item.rationale || ''),
    });
  }

  return planned;
}

export function formatDryRunSummary(agentSummary, planned) {
  const lines = [];
  lines.push('# Team Issue Triage Dry Run');
  lines.push('');
  lines.push('No GitHub labels were applied. This report shows what the gh-aw workflow would do.');
  lines.push('');

  if (!planned.length) {
    lines.push('> No triage actions were proposed.');
    lines.push('');
    return lines.join('\n');
  }

  const repos = new Set(planned.map((entry) => entry.repo));
  lines.push('## Overview');
  lines.push('');
  lines.push('| Metric | Count |');
  lines.push('| --- | ---: |');
  lines.push(`| Proposed issue updates | ${planned.length} |`);
  lines.push(`| Repositories | ${repos.size} |`);
  lines.push('');

  if (agentSummary) {
    lines.push('<details>');
    lines.push('<summary>Claude summary</summary>');
    lines.push('');
    lines.push(neuterMentions(agentSummary));
    lines.push('');
    lines.push('</details>');
    lines.push('');
  }

  const byRepo = new Map();
  for (const entry of planned) {
    if (!byRepo.has(entry.repo)) byRepo.set(entry.repo, []);
    byRepo.get(entry.repo).push(entry);
  }

  for (const [repo, entries] of byRepo) {
    lines.push(`### ${repo}`);
    lines.push('');
    lines.push('| Issue | Priority | Labels | Confidence | Rationale |');
    lines.push('| --- | --- | --- | --- | --- |');
    for (const entry of entries) {
      const issueLink = `[#${entry.issue}](https://github.com/${repo}/issues/${entry.issue})`;
      const priorityLabel = entry.labels.find((label) => formatPriorityBadge(label));
      const priority = priorityLabel ? formatPriorityBadge(priorityLabel) : '-';
      const otherLabels = entry.labels
        .filter((label) => !formatPriorityBadge(label))
        .map((label) => `\`${label}\``)
        .join(', ') || '-';
      const rationale = entry.rationale ? neuterMentions(entry.rationale.replace(/\s+/g, ' ').trim()) : '-';
      lines.push(`| ${issueLink} | ${priority} | ${otherLabels} | ${entry.confidence} | ${rationale} |`);
    }
    lines.push('');
  }

  return lines.join('\n');
}

function splitCsv(value) {
  return String(value || '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function env(name, { required = true } = {}) {
  const value = process.env[name];
  if (required && (value === undefined || value === '')) {
    throw new Error(`${name} is required.`);
  }
  return value ?? '';
}

function main() {
  const outputPath = env('GH_AW_AGENT_OUTPUT');
  const output = extractTriageOutput(fs.readFileSync(outputPath, 'utf8'));
  const planned = buildDryRunPlan(output, {
    allowedRepos: splitCsv(env('TRIAGE_ALLOWED_REPOS')),
    allowedLabels: splitCsv(env('TRIAGE_ALLOWED_LABELS')),
    maxIssuesPerRepo: env('TRIAGE_MAX_ISSUES_PER_REPO', { required: false }),
  });

  const body = formatDryRunSummary(output.summary, planned);
  if (process.env.GITHUB_STEP_SUMMARY) {
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, body);
  }
  console.log(body);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
