const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = readFileSync(resolve(__dirname, '../vodlock/vodspace.js'), 'utf8');
const context = vm.createContext({document: {documentElement: {}}, location: {hostname: 'vods.space'}});
vm.runInContext(source.split('\n  refresh();\n  chrome.storage.sync.get')[0] + '\n globalThis.api = {getSelection, navigate};\n})();', context);

function section(selected = 0) {
  const buttons = Array.from({length: 3}, (_, index) => ({
    classList: {contains: value => value === 'bg-brand' && index === selected},
    closest: () => null,
    calls: 0,
    click() { this.calls++; }
  }));
  return {buttons, querySelectorAll: () => buttons};
}

test('round navigation delegates to the existing next-round button', () => {
  const element = section(0);
  context.api.navigate(element, 'round', 1);
  assert.equal(element.buttons[1].calls, 1);
  assert.equal(element.buttons[0].calls, 0);
});

test('previous-map navigation delegates to the existing map button', () => {
  const element = section(1);
  context.api.navigate(element, 'map', -1);
  assert.equal(element.buttons[0].calls, 1);
});

test('navigation never wraps to another map or reveals an ending', () => {
  const element = section(2);
  context.api.navigate(element, 'round', 1);
  assert.equal(element.buttons.reduce((sum, button) => sum + button.calls, 0), 0);
});

test('a missing selection does not jump to an arbitrary round', () => {
  const element = section(-1);
  context.api.navigate(element, 'round', 1);
  assert.equal(element.buttons[0].calls, 0);
});

test('extension-generated controls cannot become native navigation targets', () => {
  const element = section(0);
  const ownButton = {closest: () => ({})};
  element.querySelectorAll = () => [...element.buttons, ownButton];
  assert.equal(context.api.getSelection(element, 'map').buttons.length, 3);
});

test('vods.space and embedded YouTube receive early protection without scan controls', () => {
  const manifest = JSON.parse(readFileSync(resolve(__dirname, '../vodlock/manifest.json'), 'utf8'));
  const integration = manifest.content_scripts.find(script => script.js.includes('vodspace.js'));
  assert.equal(integration.run_at, 'document_start');
  assert.equal(integration.all_frames, true);
  assert.ok(integration.matches.includes('https://vods.space/*'));
  assert.ok(integration.matches.includes('https://www.youtube.com/embed/*'));
  assert.ok(!integration.js.includes('content.js'));
});

test('gradient removal targets only the full-video button, not playback controls', () => {
  const css = readFileSync(resolve(__dirname, '../vodlock/vodspace.css'), 'utf8');
  assert.match(css, /html\.vodlock-space\.vodlock-enabled \[class~="group\/player"\] > button\.absolute\.inset-0\s*\{\s*background-image: none !important;/);
});
