import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  collectProviderUsage,
  formatProviderUsageSummary,
} from '../.github/triage/scripts/append-gh-aw-token-usage-summary.mjs';

function makeTempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'gh-aw-usage-'));
}

test('collectProviderUsage aggregates Anthropic token usage from token-usage logs', () => {
  const root = makeTempDir();
  const usageDir = path.join(root, 'sandbox', 'firewall', 'audit', 'api-proxy-logs');
  fs.mkdirSync(usageDir, { recursive: true });
  fs.writeFileSync(
    path.join(usageDir, 'token-usage.jsonl'),
    [
      JSON.stringify({
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        input_tokens: 120,
        output_tokens: 30,
        cache_read_tokens: 80,
        cache_write_tokens: 10,
        effective_tokens: 240,
      }),
      JSON.stringify({
        provider: 'anthropic',
        model: 'claude-sonnet-4-5',
        input_tokens: 40,
        output_tokens: 15,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        effective_tokens: 55,
      }),
      JSON.stringify({
        provider: 'anthropic',
        model: 'claude-haiku-4-5',
        input_tokens: 20,
        output_tokens: 5,
        cache_read_tokens: 10,
        cache_write_tokens: 0,
        effective_tokens: 28,
      }),
      JSON.stringify({
        provider: 'openai',
        model: 'gpt-5-mini',
        input_tokens: 999,
        output_tokens: 999,
        cache_read_tokens: 999,
        cache_write_tokens: 999,
        effective_tokens: 999,
      }),
    ].join('\n'),
  );

  const usage = collectProviderUsage(root, { provider: 'anthropic' });

  assert.deepEqual(usage, {
    provider: 'anthropic',
    requestCount: 3,
    totals: {
      inputTokens: 180,
      outputTokens: 50,
      cacheReadTokens: 90,
      cacheWriteTokens: 10,
      effectiveTokens: 323,
    },
    models: [
      {
        model: 'claude-sonnet-4-5',
        requestCount: 2,
        inputTokens: 160,
        outputTokens: 45,
        cacheReadTokens: 80,
        cacheWriteTokens: 10,
        effectiveTokens: 295,
      },
      {
        model: 'claude-haiku-4-5',
        requestCount: 1,
        inputTokens: 20,
        outputTokens: 5,
        cacheReadTokens: 10,
        cacheWriteTokens: 0,
        effectiveTokens: 28,
      },
    ],
  });
});

test('collectProviderUsage returns null when no provider-specific token logs exist', () => {
  const root = makeTempDir();
  fs.mkdirSync(path.join(root, 'sandbox', 'firewall', 'audit', 'api-proxy-logs'), { recursive: true });
  fs.writeFileSync(
    path.join(root, 'sandbox', 'firewall', 'audit', 'api-proxy-logs', 'token-usage.jsonl'),
    `${JSON.stringify({ provider: 'openai', model: 'gpt-5-mini', input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0 })}\n`,
  );

  assert.equal(collectProviderUsage(root, { provider: 'anthropic' }), null);
});

test('formatProviderUsageSummary renders an Anthropic token usage table', () => {
  const markdown = formatProviderUsageSummary({
    provider: 'anthropic',
    requestCount: 3,
    totals: {
      inputTokens: 180,
      outputTokens: 50,
      cacheReadTokens: 90,
      cacheWriteTokens: 10,
      effectiveTokens: 323,
    },
    models: [
      {
        model: 'claude-sonnet-4-5',
        requestCount: 2,
        inputTokens: 160,
        outputTokens: 45,
        cacheReadTokens: 80,
        cacheWriteTokens: 10,
        effectiveTokens: 295,
      },
      {
        model: 'claude-haiku-4-5',
        requestCount: 1,
        inputTokens: 20,
        outputTokens: 5,
        cacheReadTokens: 10,
        cacheWriteTokens: 0,
        effectiveTokens: 28,
      },
    ],
  });

  assert.match(markdown, /## Anthropic Token Usage/);
  assert.match(markdown, /\| Total requests \| 3 \|/);
  assert.match(markdown, /\| `claude-sonnet-4-5` \| 2 \| 160 \| 45 \| 80 \| 10 \| 295 \|/);
  assert.match(markdown, /\| `claude-haiku-4-5` \| 1 \| 20 \| 5 \| 10 \| 0 \| 28 \|/);
});
