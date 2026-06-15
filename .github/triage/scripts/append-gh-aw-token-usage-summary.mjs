#!/usr/bin/env node
// Reads gh-aw token usage artifacts from the downloaded agent artifact and
// appends a provider-specific usage summary to the GitHub Actions run summary.

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const TOKEN_USAGE_FILENAMES = new Set(['token-usage.jsonl', 'token_usage.jsonl']);

function asNumber(value) {
  return Number.isFinite(value) ? value : Number.parseInt(String(value || '0'), 10) || 0;
}

function walk(rootDir) {
  const found = [];
  const stack = [rootDir];

  while (stack.length) {
    const current = stack.pop();
    let entries = [];
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }

    for (const entry of entries) {
      const fullPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(fullPath);
        continue;
      }
      if (entry.isFile()) {
        found.push(fullPath);
      }
    }
  }

  return found;
}

function parseJsonLines(filePath) {
  const text = fs.readFileSync(filePath, 'utf8');
  const records = [];

  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    records.push(JSON.parse(trimmed));
  }

  return records;
}

export function collectProviderUsage(rootDir, { provider = 'anthropic' } = {}) {
  const modelTotals = new Map();
  const totals = {
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheWriteTokens: 0,
    effectiveTokens: 0,
  };
  let requestCount = 0;

  const files = walk(rootDir).filter((filePath) => TOKEN_USAGE_FILENAMES.has(path.basename(filePath)));

  for (const filePath of files) {
    for (const record of parseJsonLines(filePath)) {
      if (String(record.provider || '') !== provider) continue;

      const model = String(record.model || 'unknown');
      const entry = modelTotals.get(model) || {
        model,
        requestCount: 0,
        inputTokens: 0,
        outputTokens: 0,
        cacheReadTokens: 0,
        cacheWriteTokens: 0,
        effectiveTokens: 0,
      };

      entry.requestCount += 1;
      entry.inputTokens += asNumber(record.input_tokens);
      entry.outputTokens += asNumber(record.output_tokens);
      entry.cacheReadTokens += asNumber(record.cache_read_tokens);
      entry.cacheWriteTokens += asNumber(record.cache_write_tokens);
      entry.effectiveTokens += asNumber(record.effective_tokens);
      modelTotals.set(model, entry);

      requestCount += 1;
      totals.inputTokens += asNumber(record.input_tokens);
      totals.outputTokens += asNumber(record.output_tokens);
      totals.cacheReadTokens += asNumber(record.cache_read_tokens);
      totals.cacheWriteTokens += asNumber(record.cache_write_tokens);
      totals.effectiveTokens += asNumber(record.effective_tokens);
    }
  }

  if (requestCount === 0) {
    return null;
  }

  return {
    provider,
    requestCount,
    totals,
    models: Array.from(modelTotals.values()).sort((left, right) => right.effectiveTokens - left.effectiveTokens),
  };
}

export function formatProviderUsageSummary(usage) {
  const title = usage.provider === 'anthropic'
    ? '## Anthropic Token Usage'
    : `## ${usage.provider} Token Usage`;
  const lines = [title, ''];

  lines.push('| Metric | Value |');
  lines.push('| --- | ---: |');
  lines.push(`| Total requests | ${usage.requestCount} |`);
  lines.push(`| Input tokens | ${usage.totals.inputTokens} |`);
  lines.push(`| Output tokens | ${usage.totals.outputTokens} |`);
  lines.push(`| Cache read tokens | ${usage.totals.cacheReadTokens} |`);
  lines.push(`| Cache write tokens | ${usage.totals.cacheWriteTokens} |`);
  if (usage.totals.effectiveTokens > 0) {
    lines.push(`| Effective tokens | ${usage.totals.effectiveTokens} |`);
  }
  lines.push('');

  lines.push('| Model | Requests | Input | Output | Cache read | Cache write | Effective |');
  lines.push('| --- | ---: | ---: | ---: | ---: | ---: | ---: |');
  for (const model of usage.models) {
    lines.push(
      `| \`${model.model}\` | ${model.requestCount} | ${model.inputTokens} | ${model.outputTokens} | ${model.cacheReadTokens} | ${model.cacheWriteTokens} | ${model.effectiveTokens} |`,
    );
  }
  lines.push('');

  return lines.join('\n');
}

function env(name, { required = true } = {}) {
  const value = process.env[name];
  if (required && (value === undefined || value === '')) {
    throw new Error(`${name} is required.`);
  }
  return value ?? '';
}

function main() {
  const rootDir = env('GH_AW_ARTIFACT_ROOT');
  const provider = env('GH_AW_USAGE_PROVIDER', { required: false }) || 'anthropic';
  const usage = collectProviderUsage(rootDir, { provider });
  if (!usage) {
    const note = `## ${provider === 'anthropic' ? 'Anthropic' : provider} Token Usage\n\nNo provider-specific token usage records were found in the downloaded gh-aw agent artifact.\n`;
    if (process.env.GITHUB_STEP_SUMMARY) {
      fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, note);
    }
    console.log(note);
    return;
  }

  const summary = formatProviderUsageSummary(usage);
  if (process.env.GITHUB_STEP_SUMMARY) {
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary);
  }
  console.log(summary);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
