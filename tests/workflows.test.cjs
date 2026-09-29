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
