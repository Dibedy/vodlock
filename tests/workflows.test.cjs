const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const assert = require('node:assert/strict');

const workflow = name => readFileSync(resolve(__dirname, '../.github/workflows/' + name), 'utf8');

test('VOD processing and site deployment use independent workflows', () => {
  const publisher = workflow('auto-publish.yml');
  const deployment = workflow('deploy-site.yml');
  assert.doesNotMatch(publisher, /^  push:/m);
  assert.doesNotMatch(publisher, /vercel@latest|Deploy production site/);
  assert.match(publisher, /cancel-in-progress: false/);
  assert.match(deployment, /^  push:/m);
  assert.match(deployment, /- site\/\*\*/);
  assert.match(deployment, /node --test tests\/site\.test\.cjs/);
  assert.match(deployment, /vercel@latest deploy --prod/);
});

test('publisher exposes health and held-source recovery controls', () => {
  const publisher = workflow('auto-publish.yml');
  assert.match(publisher, /retry_held:/);
  assert.match(publisher, /--retry-held/);
  assert.match(publisher, /Pipeline health summary/);
  assert.match(publisher, /--summary-only/);
});

test('publisher uses a local PO-token provider for YouTube downloads', () => {
  const publisher = workflow('auto-publish.yml');
  assert.match(publisher, /VODLOCK_YOUTUBE_POT: "1"/);
  assert.match(publisher, /Start YouTube PO-token provider/);
  assert.match(publisher, /brainicism\/bgutil-ytdlp-pot-provider:2\.0\.0/);
  assert.match(publisher, /127\.0\.0\.1:4416:4416/);
  assert.match(publisher, /Stop YouTube PO-token provider/);
});

test('publisher synchronizes before committing generated indexes', () => {
  const publisher = workflow('auto-publish.yml');
  assert.ok(publisher.indexOf('git pull --rebase origin main') < publisher.indexOf('git add site/catalog.json'));
});
