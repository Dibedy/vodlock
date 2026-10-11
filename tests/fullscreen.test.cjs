const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');

function setup() {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const elements = new Map();
  const timers = new Map();
  let timerId = 0;
  const $ = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {hidden: false, querySelector: () => null, focus() {}, setAttribute() {}, classList: {
        add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name),
        toggle(name, enabled) {if (enabled) classes.add(name); else classes.delete(name);}
      }});
    }
    return elements.get(id);
  };
  const state = {ready: true, openRequest: 1};
  const notices = [];
  const document = {fullscreenElement: null, fullscreenEnabled: true, body: $('body')};
  const sandbox = vm.createContext({$, state, document, renderChat() {}, toast: value => notices.push(value),
    clearTimeout: id => timers.delete(id), setTimeout: callback => {timers.set(++timerId, callback); return timerId;}});
  vm.runInContext(app.slice(app.indexOf('  function hideFullscreenControls('), app.indexOf('  function toggleTheater(')) +
    app.slice(app.indexOf('  function syncFullscreenButton('), app.indexOf("  $('settings-button').addEventListener")), sandbox);
  $('player-view').hidden = false;
  return {sandbox, $, state, document, timers, notices};
}

test('fullscreen controls reveal on demand, hide after inactivity and protect keyboard focus', () => {
  const {sandbox, $, state, document, timers} = setup();
  sandbox.showFullscreenControls();
  assert.equal(timers.size, 0);
  document.fullscreenElement = $('watch-layout');
  sandbox.showFullscreenControls();
  assert.equal($('watch-layout').classList.contains('fullscreen-controls-visible'), true);
  sandbox.showFullscreenControls();
  assert.equal(timers.size, 1);
  [...timers.values()][0]();
  assert.equal($('watch-layout').classList.contains('fullscreen-controls-visible'), false);
  sandbox.showFullscreenControls();
  $('watch-layout').querySelector = () => ({});
  [...timers.values()][0]();
  assert.equal($('watch-layout').classList.contains('fullscreen-controls-visible'), true);
  state.ready = false;
  sandbox.showFullscreenControls();
  assert.equal(timers.size, 0);
  assert.equal($('watch-layout').classList.contains('fullscreen-controls-visible'), true);
  document.fullscreenElement = null;
  sandbox.syncFullscreenButton();
  assert.equal($('fullscreen-reveal').hidden, true);
  assert.equal($('watch-layout').classList.contains('fullscreen-controls-visible'), false);
});

test('fullscreen uses native support and falls back to a reversible browser-window view', async () => {
  const {sandbox, $, state, document, notices} = setup();
  $('watch-layout').requestFullscreen = async () => {document.fullscreenElement = $('watch-layout');};
  document.exitFullscreen = async () => {document.fullscreenElement = null;};
  await sandbox.toggleFullscreen();
  assert.equal(document.fullscreenElement, $('watch-layout'));
  assert.equal(state.windowFullscreen, undefined);
  await sandbox.toggleFullscreen();
  assert.equal(document.fullscreenElement, null);
  $('watch-layout').requestFullscreen = async () => {throw new Error('Unavailable');};
  await sandbox.toggleFullscreen();
  assert.equal(state.windowFullscreen, true);
  assert.equal(document.body.classList.contains('window-fullscreen-active'), true);
  assert.equal($('fullscreen-reveal').hidden, false);
  assert.equal(notices.length, 1);
  await sandbox.toggleFullscreen();
  assert.equal(state.windowFullscreen, false);
  assert.equal(document.body.classList.contains('window-fullscreen-active'), false);
  assert.equal($('fullscreen-reveal').hidden, true);
  document.fullscreenEnabled = false;
  await sandbox.toggleFullscreen();
  assert.equal(state.windowFullscreen, true);
});

test('cancelled fullscreen requests do not reopen playback after navigation', async () => {
  const {sandbox, $, state, document} = setup();
  let finish;
  $('watch-layout').requestFullscreen = () => new Promise((resolve, reject) => {finish = reject;});
  const request = sandbox.toggleFullscreen();
  state.openRequest++;
  finish(new Error('Unavailable'));
  await request;
  assert.equal(state.windowFullscreen, undefined);
  document.exitFullscreen = async () => {document.fullscreenElement = null;};
  $('watch-layout').requestFullscreen = () => new Promise(resolve => {finish = resolve;});
  const secondRequest = sandbox.toggleFullscreen();
  state.openRequest++;
  document.fullscreenElement = $('watch-layout');
  finish();
  await secondRequest;
  assert.equal(document.fullscreenElement, null);
});
