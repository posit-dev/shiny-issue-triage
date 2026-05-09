// Unit tests for the gh token router's argument parsing.
// Run with: node --test tests/test_gh_token_router.mjs

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  RouterError,
  chooseRepo,
  reposFromArgs,
} from '../.github/triage/scripts/gh-token-router.mjs';

test('reposFromArgs picks up --repo flag', () => {
  const repos = reposFromArgs(['issue', 'edit', '42', '--repo', 'rstudio/reactlog', '--add-label', 'x']);
  assert.deepEqual([...repos], ['rstudio/reactlog']);
});

test('reposFromArgs picks up -R flag', () => {
  const repos = reposFromArgs(['issue', 'view', '1', '-R', 'rstudio/reactlog']);
  assert.deepEqual([...repos], ['rstudio/reactlog']);
});

test('reposFromArgs picks up --repo=value form', () => {
  const repos = reposFromArgs(['issue', 'list', '--repo=posit-dev/shiny']);
  assert.deepEqual([...repos], ['posit-dev/shiny']);
});

test('reposFromArgs picks up /repos/<owner>/<repo> in API paths', () => {
  const repos = reposFromArgs(['api', '/repos/rstudio/reactlog/issues']);
  assert.deepEqual([...repos], ['rstudio/reactlog']);
});

test('reposFromArgs ignores free-text repo: query tokens (regression for multi-repo searches)', () => {
  const repos = reposFromArgs(['search', 'issues', 'is:open repo:rstudio/reactlog OR repo:posit-dev/py-shiny']);
  assert.equal(repos.size, 0);
});

test('reposFromArgs ignores the gh subcommand itself even if it looks like owner/repo', () => {
  const repos = reposFromArgs(['some/subcommand', '--repo', 'rstudio/reactlog']);
  assert.deepEqual([...repos], ['rstudio/reactlog']);
});

test('chooseRepo throws when multiple repos are detected', () => {
  const repos = new Set(['a/b', 'c/d']);
  assert.throws(
    () => chooseRepo(repos, ['issue', 'edit'], 'a/b'),
    RouterError,
  );
});

test('chooseRepo returns the single detected repo', () => {
  assert.equal(chooseRepo(new Set(['a/b']), ['issue', 'edit'], 'x/y'), 'a/b');
});

test('chooseRepo returns the default for normal subcommands when no repo is detected', () => {
  assert.equal(chooseRepo(new Set(), ['issue', 'list'], 'a/b'), 'a/b');
});

test('chooseRepo fails closed for `gh api` without explicit repo', () => {
  assert.throws(
    () => chooseRepo(new Set(), ['api', 'graphql'], 'a/b'),
    RouterError,
  );
});

test('chooseRepo fails closed for `gh search` without explicit repo', () => {
  assert.throws(
    () => chooseRepo(new Set(), ['search', 'issues'], 'a/b'),
    RouterError,
  );
});
