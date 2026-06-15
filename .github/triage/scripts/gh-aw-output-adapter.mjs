#!/usr/bin/env node
import fs from 'node:fs';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const TOOL_TYPE = 'summarize_triage_dry_run';

export function extractTriageOutput(raw) {
  const item = extractTriageToolItem(raw);
  return {
    summary: item.summary,
    actions: item.actions,
  };
}

export function extractTriageToolItem(raw) {
  const parsed = parseJson(raw, 'GH_AW_AGENT_OUTPUT');
  const items = Array.isArray(parsed.items) ? parsed.items : [];
  const matches = items.filter((item) => item && item.type === TOOL_TYPE);

  if (matches.length !== 1) {
    throw new Error(`Agent must call ${TOOL_TYPE} exactly once; found ${matches.length}.`);
  }

  const item = matches[0];
  const decoded = parseJson(item.actions_json, 'actions_json');
  const actions = Array.isArray(decoded) ? decoded : decoded.actions;

  if (!Array.isArray(actions)) {
    throw new Error('actions_json must be a JSON array or an object with an actions array.');
  }

  return {
    summary: String(item.summary || ''),
    actions,
  };
}

function parseJson(raw, name) {
  if (!String(raw || '').trim()) {
    throw new Error(`${name} is empty.`);
  }

  try {
    return JSON.parse(raw);
  } catch (error) {
    throw new Error(`${name} is not valid JSON: ${error.message}`);
  }
}

function main() {
  const outputPath = process.env.GH_AW_AGENT_OUTPUT;
  if (!outputPath) {
    throw new Error('GH_AW_AGENT_OUTPUT is required.');
  }

  const output = extractTriageOutput(fs.readFileSync(outputPath, 'utf8'));
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
