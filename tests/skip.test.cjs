const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = readFileSync(resolve(__dirname, '../vodlock/content.js'), 'utf8');
const fixtures = require('./hud-fixtures.json');

function setup(options = {}) {
  const video = new EventTarget();
  let time = options.start || 0;
  const seeks = [];
  Object.assign(video, {
    duration: 4000, paused: options.paused || false, muted: false,
    readyState: 2, seeking: false,
    getBoundingClientRect: () => ({ width: 800, height: 450 }),
    pause() { this.paused = true; },
    async play() { this.paused = false; this.dispatchEvent(new Event('play')); options.onPlay?.(this); }
  });
  Object.defineProperty(video, 'currentTime', {
    get: () => time,
    set: value => {
      time = value;
      seeks.push(value);
      options.beforeSeek?.(video);
      queueMicrotask(() => {
        options.onSeek?.(value, api, video);
        if (!options.suppressSeeked) video.dispatchEvent(new Event('seeked'));
      });
    }
  });
  const document = {
    documentElement: {classList: {remove() {}, toggle() {}}},
    querySelectorAll: () => [video],
    querySelector: () => video
  };
  const context = vm.createContext({
    document, location: options.location || { href: 'https://www.twitch.tv/videos/test', hostname: 'www.twitch.tv' },
    URL, chrome: {storage: {local: {get: options.getIndex || (async () => ({}))}}},
    setTimeout, clearTimeout, setInterval, clearInterval, AbortController,
    performance: { now: () => options.expired ? 100000 : 0 }
  });
  const exported = source.replace('  init();', `
    showShield = () => {};
    hideShield = () => {};
    updatePlayButton = () => {};
    shield = { querySelector: () => ({ textContent: '' }) };
    let lastMessage;
    showToast = message => { lastMessage = message; };
    globalThis.api = {
      skipSegment, seekTo, matchesGameplay, onKeyDown, loadRoundIndex, seekIndexedRound,
      get index() { return roundIndex; },
      route(handler) { skipSegment = handler; },
      configure(value) { Object.assign(settings, value); },
      capture(fn) { captureHud = fn; },
      shield(fn) { showShield = fn; },
      cancel() { activeScan?.controller.abort(); },
      get message() { return lastMessage; },
      get busy() { return !!activeScan; }
    };
  `);
  vm.runInContext(readFileSync(resolve(__dirname, '../vodlock/round-index.js'), 'utf8'), context);
  vm.runInContext(exported, context);
  const api = context.api;
  const profile = fixtures.gameplay;
  api.capture(() => video.currentTime >= 100 ? profile : profile.map(() => [0, 0, 0, 0]));
  return { api, video, seeks, profile, context };
}

test('indexed Down Arrow bypasses frame scanning and keeps a five-second lead', async () => {
  const index = {schemaVersion: 1, videoId: 'ZphbktbT26k', rounds: [{map: 1, round: 1, start: 100}, {map: 1, round: 2, start: 250}]};
  const {api, video, seeks} = setup({paused: true,
    location: {href: 'https://www.youtube.com/watch?v=ZphbktbT26k', hostname: 'www.youtube.com'},
    getIndex: async () => ({'roundIndex:ZphbktbT26k': index})});
  await api.loadRoundIndex();
  api.capture(() => { throw new Error('Canvas must not be used'); });
  await api.skipSegment('break');
  assert.deepEqual(seeks, [95]);
  await api.skipSegment('break');
  assert.deepEqual(seeks, [95, 245]);
  assert.equal(video.paused, true);
  api.seekIndexedRound(-1);
  assert.equal(video.currentTime, 95);
  assert.equal(api.busy, false);
});

test('an old storage response cannot replace a newer index', async () => {
  const responses = [];
  const {api} = setup({location: {href: 'https://www.youtube.com/watch?v=ZphbktbT26k', hostname: 'www.youtube.com'},
    getIndex: () => new Promise(resolve => responses.push(resolve))});
  const old = api.loadRoundIndex();
  const newer = api.loadRoundIndex();
  const index = {schemaVersion: 1, videoId: 'ZphbktbT26k', rounds: [{map: 1, round: 1, start: 100}]};
  responses[1]({'roundIndex:ZphbktbT26k': index});
  await newer;
  responses[0]({});
  await old;
  assert.equal(api.index.rounds[0].start, 100);
});

test('an indexed jump cannot silently skip a known missing round', async () => {
  const {api, seeks} = setup({start: 150,
    location: {href: 'https://www.youtube.com/watch?v=ZphbktbT26k', hostname: 'www.youtube.com'},
    getIndex: async () => ({'roundIndex:ZphbktbT26k': {schemaVersion: 1, videoId: 'ZphbktbT26k',
      rounds: [{map: 1, round: 1, start: 100}, {map: 1, round: 3, start: 300}]}})});
  await api.loadRoundIndex();
  await api.skipSegment('break');
  assert.deepEqual(seeks, []);
  assert.match(api.message, /missing rounds/);
});

test('an index stored under the wrong video is rejected', async () => {
  const {api} = setup({location: {href: 'https://www.youtube.com/watch?v=ZphbktbT26k', hostname: 'www.youtube.com'},
    getIndex: async () => ({'roundIndex:ZphbktbT26k': {schemaVersion: 1, videoId: 'aaaaaaaaaaa', rounds: [{map: 1, round: 1, start: 100}]}})});
  await api.loadRoundIndex();
  assert.equal(api.index, undefined);
  assert.match(api.message, /another video/);
});

test('replay makes one 31-second seek without capturing frames', async () => {
  const { api, video, seeks } = setup({ start: 8716 });
  video.duration = 20000;
  api.capture(() => { throw new Error('FRAME_ACCESS_BLOCKED'); });
  await api.skipSegment('replay');
  assert.deepEqual(seeks, [8747]);
  assert.equal(video.paused, false);
  assert.equal(video.muted, false);
  assert.equal(api.busy, false);
});

test('replay respects configured duration and preserves pause', async () => {
  const { api, video } = setup({ start: 50, paused: true });
  api.configure({ replaySkipSeconds: 20 });
  await api.skipSegment('replay');
  assert.equal(video.currentTime, 70);
  assert.equal(video.paused, true);
});

test('long break recovers return boundary with a five-second lead', async () => {
  const { api, video, seeks } = setup();
  await api.skipSegment('break');
  assert.ok(video.currentTime >= 95 && video.currentTime <= 97);
  assert.ok(seeks.length <= 11);
  assert.equal(api.message, 'Gameplay found');
});

test('cancel during seek restores original position and playback', async () => {
  const { api, video } = setup({ onSeek: (time, api) => { if (time === 30) api.cancel(); } });
  await api.skipSegment('break');
  assert.equal(video.currentTime, 0);
  assert.equal(video.paused, false);
  assert.equal(video.muted, false);
  assert.equal(api.message, 'Skip cancelled');
});

test('blocked capture rolls back rather than continuing search', async () => {
  const { api, video, seeks } = setup({ start: 15 });
  api.capture(() => { throw new Error('FRAME_ACCESS_BLOCKED'); });
  await api.skipSegment('break');
  assert.deepEqual(seeks, [45, 15]);
  assert.equal(video.currentTime, 15);
  assert.equal(api.busy, false);
});

test('built-in reference accepts both supplied gameplay layouts and rejects replay', () => {
  const { api } = setup();
  assert.equal(api.matchesGameplay(fixtures.gameplay), true);
  assert.equal(api.matchesGameplay(fixtures.preround), true);
  assert.equal(api.matchesGameplay(fixtures.replay), false);
  assert.equal(api.matchesGameplay(fixtures.gameplay.map(() => [0, 0, 1, 0])), false);
});

test('one isolated candidate cannot finish a long-break search', async () => {
  const { api, video, profile } = setup();
  api.capture(() => (video.currentTime === 30 || video.currentTime >= 100)
    ? profile : profile.map(() => [0, 0, 0, 0]));
  await api.skipSegment('break');
  assert.ok(video.currentTime >= 95 && video.currentTime <= 97);
});

test('timeout returns to original position', async () => {
  let calls = 0;
  const { api, context, video } = setup({ start: 15 });
  context.performance.now = () => calls++ === 0 ? 0 : 50000;
  await api.skipSegment('break');
  assert.equal(video.currentTime, 15);
  assert.match(api.message, /timed out/);
});

test('unmodified Right Arrow skips replay and Down Arrow searches long breaks', () => {
  const { api } = setup();
  const modes = [];
  api.route(mode => modes.push(mode));
  const event = code => ({ code, preventDefault() {}, stopImmediatePropagation() {} });
  api.onKeyDown(event('ArrowRight'));
  api.onKeyDown(event('ArrowDown'));
  assert.deepEqual(modes, ['replay', 'break']);
  api.onKeyDown({ ...event('ArrowRight'), repeat: true });
  api.onKeyDown({ ...event('ArrowDown'), target: { tagName: 'INPUT' } });
  assert.deepEqual(modes, ['replay', 'break']);
});

test('Down Arrow runs the complete scan while the player volume slider has focus', async () => {
  const { api, video, seeks } = setup();
  let prevented = false;
  let stopped = false;
  await api.onKeyDown({
    code: 'ArrowDown', target: { tagName: 'INPUT', type: 'range' },
    preventDefault() { prevented = true; },
    stopImmediatePropagation() { stopped = true; }
  });
  assert.equal(prevented, true);
  assert.equal(stopped, true);
  assert.ok(seeks.length > 0);
  assert.ok(video.currentTime >= 95 && video.currentTime <= 97);
});

test('a scan startup failure releases busy state and restores playback', async () => {
  const { api, video } = setup({ start: 15 });
  api.shield(() => { throw new Error('UI unavailable'); });
  await api.skipSegment('break');
  assert.equal(api.busy, false);
  assert.equal(video.currentTime, 15);
  assert.equal(video.muted, false);
  assert.equal(video.paused, false);
  assert.match(api.message, /UI unavailable/);
});

test('timer panel alone cannot qualify as gameplay', () => {
  const { api } = setup();
  const hud = fixtures.gameplay.map(() => [0, 0, 1, 0]);
  hud[0] = fixtures.gameplay[0];
  assert.equal(api.matchesGameplay(hud), false);
});

test('side-stack matching tolerates two missing player cards per team', () => {
  const { api } = setup();
  const hud = fixtures.gameplay.map(region => [...region]);
  for (const index of [1, 2, 6, 7, 11, 12, 13, 14]) hud[index] = [0, 0, 1, 0];
  assert.equal(api.matchesGameplay(hud), true);
});

test('loaded target frame completes a seek without a seeked event', async () => {
  const { api, video } = setup({ suppressSeeked: true });
  await api.seekTo(video, 30);
  assert.equal(video.currentTime, 30);
});

test('seeked before frame readiness waits for loadeddata rather than failing', async () => {
  const { api, video } = setup({
    beforeSeek(video) { video.readyState = 1; },
    onSeek(time, api, video) {
      setTimeout(() => {
        video.readyState = 2;
        video.dispatchEvent(new Event('loadeddata'));
      }, 20);
    }
  });
  await api.seekTo(video, 30);
  assert.equal(video.readyState, 2);
});

test('muted scan playback allows a paused player to decode a requested frame', async () => {
  let frames = 0;
  const { api, video } = setup({
    paused: true, suppressSeeked: true,
    beforeSeek(video) { video.seeking = true; video.readyState = 1; },
    onSeek(time, api, video) {
      if (!video.paused) {
        video.seeking = false;
        video.readyState = 2;
        video.dispatchEvent(new Event('loadeddata'));
      }
    },
    onPlay(video) {
      assert.equal(video.muted, true);
      queueMicrotask(() => {
        video.seeking = false;
        video.readyState = 2;
        video.dispatchEvent(new Event('loadeddata'));
      });
    }
  });
  api.capture(() => {
    frames++;
    assert.equal(video.paused, false);
    assert.equal(video.muted, true);
    return video.currentTime >= 100 ? fixtures.gameplay : fixtures.gameplay.map(() => [0, 0, 0, 0]);
  });
  await api.skipSegment('break');
  assert.ok(frames > 0);
  assert.equal(api.message, 'Gameplay found');
  assert.equal(video.paused, true);
  assert.equal(video.muted, false);
});

test('an unloaded frame is never treated as a successful seek', async () => {
  const { api, video, context } = setup({ beforeSeek(video) { video.readyState = 1; } });
  let timeout;
  context.setTimeout = (callback, milliseconds) => {
    timeout = milliseconds;
    return setTimeout(callback, 10);
  };
  await assert.rejects(api.seekTo(video, 30), /FRAME_LOAD_TIMEOUT/);
  assert.equal(timeout, 12000);
});

test('play AbortError during a seek does not reject a frame that subsequently loads', async () => {
  const { api, video } = setup({
    paused: true,
    beforeSeek(video) { video.seeking = true; video.readyState = 1; },
    onSeek(time, api, video) {
      setTimeout(() => {
        video.seeking = false;
        video.readyState = 2;
        video.dispatchEvent(new Event('loadeddata'));
      }, 20);
    }
  });
  video.play = async () => {
    const error = new Error('The play request was interrupted by a new load request');
    error.name = 'AbortError';
    throw error;
  };
  await api.seekTo(video, 30, undefined, true);
  assert.equal(video.currentTime, 30);
  assert.equal(video.readyState, 2);
});

test('a playback denial is reported by its actual name if no frame loads', async () => {
  const { api, video, context } = setup({
    paused: true,
    beforeSeek(video) { video.readyState = 1; }
  });
  const denied = new Error('Playback requires user interaction');
  denied.name = 'NotAllowedError';
  video.play = async () => { throw denied; };
  context.setTimeout = callback => setTimeout(callback, 10);
  await assert.rejects(api.seekTo(video, 30, undefined, true), error => {
    assert.equal(error.message, 'FRAME_LOAD_TIMEOUT');
    assert.equal(error.cause, denied);
    return true;
  });
});

test('a blocked play call does not prevent paused-frame analysis when the frame loads', async () => {
  const { api, video } = setup({ paused: true });
  video.play = async () => {
    const error = new Error('Playback denied');
    error.name = 'NotAllowedError';
    throw error;
  };
  await api.skipSegment('break');
  assert.equal(api.message, 'Gameplay found');
  assert.equal(video.paused, true);
  assert.equal(video.muted, false);
});
