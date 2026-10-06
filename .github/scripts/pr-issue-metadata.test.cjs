'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { parseClosingIssues, validatePrNumber, syncMetadata } = require('./pr-issue-metadata.cjs');

const parse = (body) => parseClosingIssues(body, 'team', 'project');
test('explicit references, inflections, lists, duplicate refs and same repo URLs', () => {
  assert.deepEqual(parse('Closes #12, #13 and #14\nFixed #12\nResolves https://github.com/TEAM/Project/issues/15'), [12, 13, 14, 15]);
});
test('plain mentions, foreign issues, PR URLs, malformed numbers and reference suffixes excluded', () => {
  assert.deepEqual(parse('See #1\nFixes https://github.com/other/project/issues/2\nCloses https://github.com/team/project/pull/3\nCloses #4abc\nFixes https://github.com/team/project/issues/5/comments\nCloses #0'), []);
});
test('comments, quotes, inline, fenced, unclosed fenced and indented code excluded', () => {
  assert.deepEqual(parse('<!-- Fixes #1 -->\n> Closes #2\n`Fixes #3`\n```md\nCloses #4\n```\n    Fixes #5\nResolves #6\n~~~\nCloses #7'), [6]);
});
test('fences close only on same sufficiently long marker with whitespace suffix', () => {
  for (const marker of ['```', '~~~']) {
    const other = marker[0] === '`' ? '~~~' : '```';
    for (const falseClose of [`${marker}example`, marker.slice(1), other]) {
      assert.deepEqual(parse(`${marker}\n${falseClose}\nCloses #3\n${marker}`), []);
    }
    assert.deepEqual(parse(`${marker}${marker[0]}\n${marker}\nCloses #3\n${marker}${marker[0]}`), []);
    assert.deepEqual(parse(`${marker}\nFixes #3\n  ${marker} \t\nCloses #4`), [4]);
    assert.deepEqual(parse(`${marker}\nFixes #3\n${marker}${marker[0]}\nCloses #4`), [4]);
  }
});
test('closing references and lists do not cross lines', () => {
  assert.deepEqual(parse('Fixes\n#1\nCloses #2,\n#3'), [2]);
});
test('manual PR number validation', () => {
  assert.equal(validatePrNumber('123'), 123);
  for (const input of ['0', '-1', '1e3', '1; throw', '1.2', '9007199254740992', '', ' 1']) assert.throws(() => validatePrNumber(input));
});

function fixture({ body = 'Fixes #1, #2', milestones = [7, 7], prMilestone = null, prLabels = ['manual', 'existing'], prs = [], failRead = false, failWrite = false } = {}) {
  const writes = [];
  const github = { rest: { pulls: { get: async () => ({ data: { body, milestone: prMilestone == null ? null : { number: prMilestone } } }) }, issues: {
    get: async ({ issue_number }) => {
      if (failRead) throw new Error('404 missing issue');
      return { data: { number: issue_number, milestone: milestones[issue_number - 1] == null ? null : { number: milestones[issue_number - 1] }, ...(prs.includes(issue_number) ? { pull_request: {} } : {}) } };
    },
    listLabelsOnIssue: 'labels',
    addLabels: async (args) => { if (failWrite) throw new Error('403 permission denied'); writes.push(args); },
    update: async (args) => { writes.push(args); }
  } }, paginate: async (method, { issue_number, per_page }) => {
    assert.equal(method, 'labels'); assert.equal(per_page, 100);
    return (issue_number === 10 ? prLabels : ['existing', `issue-${issue_number}`]).map((name) => ({ name }));
  } };
  return { writes, run: () => syncMetadata({ github, owner: 'team', repo: 'project', prNumber: 10 }) };
}
test('adds union, preserves manual labels and applies common milestone', async () => {
  const { run, writes } = fixture(); await run();
  assert.deepEqual(writes, [{ owner: 'team', repo: 'project', issue_number: 10, labels: ['issue-1', 'issue-2'] }, { owner: 'team', repo: 'project', issue_number: 10, milestone: 7 }]);
});
test('mixed and null milestones preserve existing milestone', async () => {
  for (const milestones of [[7, 8], [7, null], [null, null]]) {
    const { run, writes } = fixture({ milestones, prMilestone: 42 });
    assert.match(await run(), /Milestone unchanged/); assert.equal(writes.length, 1); assert.ok(!('milestone' in writes[0]));
  }
});
test('reapplication is idempotent when metadata already inherited', async () => {
  const { run, writes } = fixture({ prLabels: ['manual', 'existing', 'issue-1', 'issue-2'], prMilestone: 7 }); await run(); assert.deepEqual(writes, []);
});
test('no links and PR-only references make no writes', async () => {
  for (const options of [{ body: 'See #1' }, { prs: [1, 2] }]) {
    const { run, writes } = fixture(options); assert.match(await run(), /unchanged/); assert.deepEqual(writes, []);
  }
});
test('duplicate refs fetch once; referenced PR does not affect union or milestone', async () => {
  const { run, writes } = fixture({ body: 'Fixes #1, #1, #2', prs: [2], milestones: [7, 9] }); await run();
  assert.deepEqual(writes[0].labels, ['issue-1']); assert.equal(writes[1].milestone, 7);
});
test('read and permission failures propagate clearly', async () => {
  for (const options of [{ failRead: true }, { failWrite: true }]) {
    const { run, writes } = fixture(options); await assert.rejects(run(), /missing issue|permission denied/); assert.deepEqual(writes, []);
  }
});
test('workflow trust boundary and token scope remain explicit', () => {
  const workflow = fs.readFileSync(`${__dirname}/../workflows/pr-issue-metadata.yml`, 'utf8');
  assert.match(workflow, /pull_request_target:/); assert.match(workflow, /pull_request\.base\.sha/);
  assert.match(workflow, /branch: repository\.default_branch/); assert.match(workflow, /ref: trustedRef/);
  assert.match(workflow, /actions\/github-script@[a-f0-9]{40}/);
  assert.match(workflow, /issues: read\n  pull-requests: write/);
  assert.doesNotMatch(workflow, /actions\/checkout|head\.sha|head\.ref|\$\{\{[^}]*body/);
  assert.match(workflow, /cancel-in-progress: false/);
});
