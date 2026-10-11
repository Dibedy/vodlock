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
  assert.match(deployment, /vercel@63\.1\.0 deploy --prod/);
});

test('production deployment rejects obsolete commits before using the provider budget', () => {
  const deployment = workflow('deploy-site.yml');
  assert.match(deployment, /git ls-remote origin "refs\/heads\/\$GITHUB_REF_NAME"/);
  assert.match(deployment, /"\$current_head" != "\$GITHUB_SHA"/);
  assert.ok(deployment.indexOf('Deployment superseded by current branch head') < deployment.indexOf('npx --yes vercel@'));
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
  assert.match(publisher, /git pull --rebase --autostash origin main/);
  assert.ok(publisher.indexOf('git pull --rebase --autostash origin main') < publisher.indexOf('git add site/catalog.json'));
});

test('publisher validates generated indexes before committing them', () => {
  const publisher = workflow('auto-publish.yml');
  assert.match(publisher, /Validate generated catalog/);
  assert.match(publisher, /python -m unittest discover -s tests -p 'test_\*\.py'/);
  assert.match(publisher, /node --test tests\/site\.test\.cjs/);
  assert.ok(publisher.indexOf('Validate generated catalog') < publisher.indexOf('git add site/catalog.json'));
});
