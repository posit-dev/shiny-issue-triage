#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

import { extractTriageToolItem } from './gh-aw-output-adapter.mjs';

const REPO_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

export function validateCursorUpdates(cursors) {
  if (!cursors || Array.isArray(cursors) || typeof cursors !== 'object') {
    throw new Error('cursors_json must be a JSON object.');
  }

  for (const [repo, value] of Object.entries(cursors)) {
    if (!REPO_PATTERN.test(repo)) {
      throw new Error(`Cursor key must be owner/repo: ${repo}`);
    }
    if (!value || Array.isArray(value) || typeof value !== 'object') {
      throw new Error(`cursor value for ${repo} must be an object.`);
    }
  }

  return cursors;
}

export function mergeCursors(existing, incoming) {
  validateCursorUpdates(incoming);
  return {
    ...(existing && !Array.isArray(existing) && typeof existing === 'object' ? existing : {}),
    ...incoming,
  };
}

export function buildTriageRecord(output, env) {
  const serverUrl = env.serverUrl || 'https://github.com';
  const repository = env.repository || '';
  const runId = env.runId || '';
  const runUrl = repository && runId ? `${serverUrl}/${repository}/actions/runs/${runId}` : '';

  return {
    timestamp: env.timestamp || new Date().toISOString(),
    workflow: env.workflow || 'Team Issue Triage (gh-aw)',
    run_url: runUrl,
    summary: String(output.summary || ''),
    actions: Array.isArray(output.actions) ? output.actions : [],
  };
}

function readJsonIfExists(filePath, fallback) {
  if (!fs.existsSync(filePath)) return fallback;
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function writeJson(filePath, data) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(data, null, 2)}\n`);
}

function appendJsonl(filePath, data) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.appendFileSync(filePath, `${JSON.stringify(data)}\n`);
}

function main() {
  const outputPath = process.env.GH_AW_AGENT_OUTPUT;
  const stateDir = process.env.TRIAGE_STATE_DIR;

  if (!outputPath) throw new Error('GH_AW_AGENT_OUTPUT is required.');
  if (!stateDir) throw new Error('TRIAGE_STATE_DIR is required.');

  const item = extractTriageToolItem(fs.readFileSync(outputPath, 'utf8'));
  const cursors = validateCursorUpdates(item.cursors);
  const cursorPath = path.join(stateDir, 'cursors.json');
  const existingCursors = readJsonIfExists(cursorPath, {});
  writeJson(cursorPath, mergeCursors(existingCursors, cursors));

  const record = buildTriageRecord(item, {
    runId: process.env.GITHUB_RUN_ID,
    repository: process.env.GITHUB_REPOSITORY,
    serverUrl: process.env.GITHUB_SERVER_URL,
    workflow: process.env.GITHUB_WORKFLOW,
  });
  const date = record.timestamp.slice(0, 10);
  appendJsonl(path.join(stateDir, 'triage-results', `${date}-gh-aw.jsonl`), record);

  console.log(`Persisted gh-aw triage state with ${Object.keys(cursors).length} cursor update(s).`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
