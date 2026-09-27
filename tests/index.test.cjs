const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const context = vm.createContext({});
vm.runInContext(readFileSync(resolve(__dirname, '../vodlock/round-index.js'), 'utf8'), context);
const {validate, destination} = context.VodlockIndex;
const sample = () => ({schemaVersion: 1, videoId: 'ZphbktbT26k', leadSeconds: 999,
  rounds: [{map: 1, round: 1, start: 100}, {map: 1, round: 2, start: 250}, {map: 2, round: 1, start: 700}]});

test('imports enforce a five-second lead and navigate across map boundaries', () => {
  const index = validate(sample());
  assert.equal(index.leadSeconds, 5);
  assert.equal(destination(index, 0, 1), 95);
  assert.equal(destination(index, 95, 1), 245);
  assert.equal(destination(index, 260, 1), 695);
  assert.equal(destination(index, 695, -1), 245);
  assert.equal(destination(index, 700, 1), null);
  assert.equal(destination(index, 95, -1), null);
});

test('malformed, duplicate and out-of-order indexes are rejected', () => {
  for (const change of [value => {value.videoId = 12345678901;}, value => {value.rounds = [];},
    value => {value.schemaVersion = 2;}, value => {value.rounds[1].start = 50;},
    value => {value.rounds[1].round = 1;}, value => {value.rounds[1].start = Infinity;},
    value => {value.rounds[0].map = 0;}]) {
    const value = sample();
    change(value);
    assert.throws(() => validate(value));
  }
});

test('timestamps near the beginning are clamped to zero', () => {
  const value = sample();
  value.rounds[0].start = 2;
  assert.equal(destination(validate(value), -10, 1), 0);
});
