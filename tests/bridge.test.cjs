const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function setup(origin = 'http://127.0.0.1:8766', failure) {
  let listener;
  const messages = [];
  const stored = [];
  const window = {addEventListener(name, callback) {listener = callback;}, postMessage(value) {messages.push(value);}};
  const context = vm.createContext({window, location: {origin}, chrome: {storage: {local: {async set(value) {
    if (failure) throw new Error(failure);
    stored.push(value);
  }}}}});
  for (const file of ['round-index.js', 'index-bridge.js']) {
    vm.runInContext(readFileSync(resolve(__dirname, '../vodlock/' + file), 'utf8'), context);
  }
  return {messages, stored, get bound() {return !!listener;},
    async send(data, overrides) {await listener({source: window, origin, data, ...overrides});}};
}

test('bridge only binds to the Round Studio origin', () => {
  assert.equal(setup().bound, true);
  assert.equal(setup('http://127.0.0.1:9000').bound, false);
});

test('bridge validates an index before storing it and acknowledges only after saving', async () => {
  const bridge = setup();
  await bridge.send({type: 'VODLOCK_INDEX_PING'});
  assert.equal(bridge.messages[0].type, 'VODLOCK_INDEX_READY');
  await bridge.send({type: 'VODLOCK_SAVE_INDEX', requestId: 'request',
    index: {schemaVersion: 1, videoId: 'ZphbktbT26k', rounds: [{map: 1, round: 1, start: 100}]}});
  assert.equal(bridge.stored[0]['roundIndex:ZphbktbT26k'].leadSeconds, 5);
  assert.equal(bridge.messages[1].requestId, 'request');
  assert.equal(bridge.messages[1].error, undefined);
});

test('cross-origin messages and messages from another window are ignored', async () => {
  const bridge = setup();
  await bridge.send({type: 'VODLOCK_INDEX_PING'}, {origin: 'https://evil.test'});
  await bridge.send({type: 'VODLOCK_INDEX_PING'}, {source: {}});
  assert.equal(bridge.messages.length, 0);
  assert.equal(bridge.stored.length, 0);
});

test('invalid indexes and storage failures are reported without false success', async () => {
  const invalid = setup();
  await invalid.send({type: 'VODLOCK_SAVE_INDEX', index: {}});
  assert.equal(invalid.stored.length, 0);
  assert.match(invalid.messages[0].error, /valid matching YouTube/);
  const broken = setup(undefined, 'Storage unavailable');
  await broken.send({type: 'VODLOCK_SAVE_INDEX', index: {schemaVersion: 1, videoId: 'ZphbktbT26k', rounds: [{map: 1, round: 1, start: 100}]}});
  assert.equal(broken.messages[0].error, 'Storage unavailable');
});
