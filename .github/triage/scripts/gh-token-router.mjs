#!/usr/bin/env node
// Wraps `gh` so each invocation uses the GitHub App installation token for
// the targeted repository. Repository detection is intentionally
// conservative: only flag-style arguments (--repo / -R / --repo= / -R=) and
// `/repos/<owner>/<repo>` segments inside positional args are inspected.
// Free-text query tokens like `repo:owner/repo` are ignored so multi-repo
// search queries against a single installation still work.
//
// `gh api` and `gh search` calls without an explicit repo flag fail closed
// rather than silently fall back to the default repo's token.

import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const FAIL_CLOSED_SUBCOMMANDS = new Set(['api', 'search']);
const REPO_ARG_RE = /^([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)/;
const REPOS_PATH_RE = /^\/?repos\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)(?:\/|$)/;

export function reposFromArgs(argv) {
  const repos = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if ((arg === '--repo' || arg === '-R') && argv[index + 1]) {
      addRepo(repos, argv[index + 1]);
      index += 1;
      continue;
    }
    if (arg.startsWith('--repo=')) {
      addRepo(repos, arg.slice('--repo='.length));
      continue;
    }
    if (arg.startsWith('-R=')) {
      addRepo(repos, arg.slice('-R='.length));
      continue;
    }
    if (index === 0) continue; // skip the gh subcommand itself
    const apiMatch = arg.match(REPOS_PATH_RE);
    if (apiMatch) {
      addRepo(repos, `${apiMatch[1]}/${apiMatch[2]}`);
    }
  }
  return repos;
}

function addRepo(repos, value) {
  const match = String(value).match(REPO_ARG_RE);
  if (match) {
    repos.add(match[1]);
  }
}

export class RouterError extends Error {}

export function chooseRepo(repos, argv, defaultRepo) {
  if (repos.size > 1) {
    throw new RouterError(
      `gh commands must target one repository at a time with the token router. ` +
        `Found: ${Array.from(repos).join(', ')}`,
    );
  }
  if (repos.size === 1) {
    return Array.from(repos)[0];
  }
  const subcommand = argv[0];
  if (FAIL_CLOSED_SUBCOMMANDS.has(subcommand)) {
    throw new RouterError(
      `gh ${subcommand} requires an explicit --repo flag or /repos/<owner>/<repo> path so the token router can pick the right installation token.`,
    );
  }
  return defaultRepo;
}

function readTokenMap(filePath) {
  if (!filePath) {
    return { defaultRepo: '', tokens: {} };
  }
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function main() {
  const realGh = process.env.TRIAGE_REAL_GH;
  if (!realGh) {
    console.error('TRIAGE_REAL_GH is required.');
    process.exit(1);
  }

  let tokenMap;
  try {
    tokenMap = readTokenMap(process.env.TRIAGE_GH_TOKENS_FILE);
  } catch (error) {
    console.error(`Could not read GitHub token map: ${error.message}`);
    process.exit(1);
  }

  const args = process.argv.slice(2);

  let repo;
  try {
    repo = chooseRepo(reposFromArgs(args), args, tokenMap.defaultRepo);
  } catch (error) {
    if (error instanceof RouterError) {
      console.error(error.message);
      process.exit(1);
    }
    throw error;
  }

  const token = (repo && tokenMap.tokens[repo]) || process.env.GH_TOKEN;
  if (!token) {
    console.error(`No GitHub token is available for ${repo || '(no repo detected)'}.`);
    process.exit(1);
  }

  const result = spawnSync(realGh, args, {
    stdio: 'inherit',
    env: { ...process.env, GH_TOKEN: token },
  });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  process.exit(result.status ?? 1);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
