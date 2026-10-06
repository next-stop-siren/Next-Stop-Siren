'use strict';

// Intentionally conservative: only visible, line-local closing references.
function parseClosingIssues(body, owner, repo) {
  let fence = null;
  const visible = String(body ?? '').replace(/<!--[\s\S]*?(?:-->|$)/g, '').split('\n').map((line) => {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null;
      return '';
    }
    if (fence || /^\s*>|^(?: {4}|\t)/.test(line)) return '';
    return line.replace(/(`+)[\s\S]*?\1/g, '');
  }).join('\n');
  const found = new Set();
  const reference = '(?:#[1-9]\\d*|https://github\\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9]\\d*)';
  const groups = new RegExp(`\\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)[ \t]*:?[ \t]+(${reference}(?:(?:[ \t]*,[ \t]*|[ \t]+and[ \t]+)${reference})*)`, 'gi');
  for (const match of visible.matchAll(groups)) {
    for (const ref of match[1].matchAll(new RegExp(reference, 'g'))) {
      // Reject partial references such as #12abc or URL suffix paths/queries.
      const end = match.index + match[0].indexOf(match[1]) + ref.index + ref[0].length;
      if (/[\w/?#-]/.test(visible[end] ?? '')) continue;
      const value = ref[0];
      if (value.startsWith('#')) found.add(Number(value.slice(1)));
      else {
        const url = new URL(value);
        const [linkedOwner, linkedRepo, , number] = url.pathname.slice(1).split('/');
        if (linkedOwner.toLowerCase() === owner.toLowerCase() && linkedRepo.toLowerCase() === repo.toLowerCase()) found.add(Number(number));
      }
    }
  }
  return [...found].filter(Number.isSafeInteger).sort((a, b) => a - b);
}

function validatePrNumber(value) {
  if (!/^[1-9]\d*$/.test(String(value)) || !Number.isSafeInteger(Number(value))) {
    throw new Error('PR number must be a positive safe integer.');
  }
  return Number(value);
}

async function syncMetadata({ github, owner, repo, prNumber }) {
  const number = validatePrNumber(prNumber);
  const params = { owner, repo };
  // Fetch current body, rather than replaying stale event payloads.
  const { data: pr } = await github.rest.pulls.get({ ...params, pull_number: number });
  const references = parseClosingIssues(pr.body, owner, repo);
  if (!references.length) return 'No qualifying closing issue references; metadata unchanged.';
  const issues = [];
  const labels = new Set();
  for (const issue_number of references) {
    const { data: issue } = await github.rest.issues.get({ ...params, issue_number });
    if (issue.pull_request) continue;
    issues.push(issue);
    const issueLabels = await github.paginate(github.rest.issues.listLabelsOnIssue, { ...params, issue_number, per_page: 100 });
    for (const label of issueLabels) labels.add(label.name);
  }
  if (!issues.length) return 'References identify PRs, not issues; metadata unchanged.';
  const existing = await github.paginate(github.rest.issues.listLabelsOnIssue, { ...params, issue_number: number, per_page: 100 });
  const existingNames = new Set(existing.map((label) => label.name));
  const additions = [...labels].filter((label) => !existingNames.has(label)).sort();
  if (additions.length) await github.rest.issues.addLabels({ ...params, issue_number: number, labels: additions });
  const milestone = issues[0].milestone?.number;
  const consistent = milestone != null && issues.every((issue) => issue.milestone?.number === milestone);
  if (consistent && pr.milestone?.number !== milestone) {
    await github.rest.issues.update({ ...params, issue_number: number, milestone });
  }
  return `Linked issues: ${issues.map((issue) => `#${issue.number}`).join(', ')}. Added ${additions.length} label(s). ${consistent ? `Milestone #${milestone} applied or already present.` : 'Milestone unchanged: linked issues have absent or mixed milestones.'}`;
}

module.exports = { parseClosingIssues, validatePrNumber, syncMetadata };
