const assert = require('node:assert/strict');
const {existsSync, readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const context = vm.createContext({URL, URLSearchParams});
vm.runInContext(readFileSync(resolve(__dirname, '../site/core.js'), 'utf8'), context);
const {videoId, twitchId, mediaSource, sourceKey, validateIndex, validateChat, playedTime, position, roundDestination, mapDestination, sourceTime, playbackTime} = context.VodlockSite;
const sample = () => ({schemaVersion: 1, videoId: 'ZphbktbT26k', rounds: [
  {map: 1, round: 1, start: 100}, {map: 1, round: 2, start: 250},
  {map: 2, round: 1, start: 700}, {map: 2, round: 2, start: 850}
]});

function playerStartup() {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const elements = new Map();
  const intervals = new Map();
  const calls = [];
  let now = 0;
  let sequence = 0;
  const player = {
    time: 9000, status: 1, muted: false,
    getCurrentTime() { return this.time; },
    getPlayerState() { return this.status; },
    isMuted() { return this.muted; },
    mute() { this.muted = true; calls.push('mute'); },
    unMute() { this.muted = false; calls.push('unmute'); },
    loadVideoById(value) { calls.push(['load', value]); },
    seekTo(value) { calls.push(['seek', value]); },
    pauseVideo() { this.status = 2; calls.push('pause'); },
    destroy() { calls.push('destroy'); }
  };
  const state = {entry: {provider: 'youtube', sourceId: 'ZphbktbT26k'}, index: {rounds: [{start: 100}], leadSeconds: 5}, player, resumeTime: null, ready: false, started: true};
  const sandbox = vm.createContext({state, Date: {now: () => now}, settings: {},
    YT: {PlayerState: {PLAYING: 1, ENDED: 0}, Player: function(host, options) { calls.push(['create', options]); return player; }},
    $: id => { if (!elements.has(id)) elements.set(id, {hidden: false, disabled: true, value: '50', replaceChildren() {}}); return elements.get(id); },
    setInterval: callback => { const id = ++sequence; intervals.set(id, callback); return id; },
    clearInterval: id => intervals.delete(id), setTimeout: callback => callback(),
    preventTwitchNativeFullscreen() {}, setVolume() {}, setPlaybackControlsDisabled(value) { calls.push(['disabled', value]); },
    seekPlayer(value) { calls.push(['seek', value]); }, playPlayer() { state.playing = true; },
    updateQualityOptions() {}, updateStatus() {}, setPlaying(value) { state.playing = value; }, toast(message) { calls.push(['toast', message]); }
  });
  vm.runInContext(app.slice(app.indexOf('  function destroyPlayer('), app.indexOf('  function renderChat(')), sandbox);
  vm.runInContext(app.slice(app.indexOf('  function playerReady('), app.indexOf('  function createTwitchPlayer(')), sandbox);
  return {sandbox, state, player, calls, elements, intervals, tick(milliseconds = 100) { now += milliseconds; for (const callback of [...intervals.values()]) callback(); }};
}

test('YouTube startup corrects an ignored live seek before revealing or unmuting playback', () => {
  const startup = playerStartup();
  startup.sandbox.createYouTubePlayer();
  const options = startup.calls.find(call => call[0] === 'create')[1];
  assert.equal(options.playerVars.start, 95);
  options.events.onReady({target: startup.player});
  assert.equal(startup.calls.find(call => call[0] === 'load')[1].startSeconds, 95);
  startup.tick();
  assert.equal(startup.calls.find(call => call[0] === 'seek')[1], 95);
  assert.equal(startup.state.ready, false);
  assert.equal(startup.player.muted, true);
  assert.equal(startup.elements.get('start-gate'), undefined);
  options.events.onStateChange({target: startup.player, data: 0});
  startup.player.time = 95;
  startup.tick();
  startup.tick(500);
  assert.equal(startup.state.ready, true);
  assert.equal(startup.player.muted, false);
  assert.equal(startup.elements.get('start-gate').hidden, true);
});

test('YouTube startup fails closed when the indexed position cannot be reached', () => {
  const startup = playerStartup();
  startup.sandbox.playerReady({target: startup.player});
  startup.tick(15000);
  assert.equal(startup.state.ready, false);
  assert.equal(startup.state.started, false);
  assert.equal(startup.state.resumeTime, 95);
  assert.equal(startup.elements.get('start-gate').hidden, false);
  assert.equal(startup.elements.get('start-watching').disabled, false);
  assert.ok(startup.calls.includes('pause'));
  assert.ok(startup.calls.includes('destroy'));
  assert.equal(startup.calls.includes('unmute'), false);
});

test('YouTube startup preserves fractional resume positions and existing mute settings', () => {
  const startup = playerStartup();
  startup.state.resumeTime = 321.25;
  startup.player.muted = true;
  startup.sandbox.playerReady({target: startup.player});
  assert.equal(startup.calls.find(call => call[0] === 'load')[1].startSeconds, 321.25);
  startup.player.time = 321.25;
  startup.tick();
  startup.tick(500);
  assert.equal(startup.state.ready, true);
  assert.equal(startup.player.muted, true);
});

test('YouTube startup timers and stale ready events are cancelled when leaving the player', () => {
  const startup = playerStartup();
  startup.sandbox.playerReady({target: startup.player});
  startup.sandbox.destroyPlayer();
  startup.sandbox.playerReady({target: startup.player});
  assert.equal(startup.intervals.size, 0);
  assert.equal(startup.calls.filter(call => call[0] === 'load').length, 1);
  assert.equal(startup.calls.includes('unmute'), false);
});

test('YouTube startup clamps early indexed starts to zero', () => {
  const startup = playerStartup();
  startup.state.index.rounds[0].start = 3;
  startup.sandbox.createYouTubePlayer();
  assert.equal(startup.calls.find(call => call[0] === 'create')[1].playerVars.start, 0);
  startup.sandbox.playerReady({target: startup.player});
  assert.equal(startup.calls.find(call => call[0] === 'load')[1].startSeconds, 0);
});

test('Twitch startup keeps its existing indexed seek and playback behavior', () => {
  const startup = playerStartup();
  startup.state.entry.provider = 'twitch';
  startup.sandbox.playerReady();
  assert.equal(startup.calls.find(call => call[0] === 'seek')[1], 95);
  assert.equal(startup.state.ready, true);
  assert.equal(startup.state.playing, true);
  assert.equal(startup.calls.includes('mute'), false);
  assert.equal(startup.state.startTimer, undefined);
});

test('website accepts supported YouTube URL forms without accepting lookalike hosts', () => {
  for (const value of ['ZphbktbT26k', 'https://www.youtube.com/watch?v=ZphbktbT26k&t=90s',
    'https://youtu.be/ZphbktbT26k', 'https://youtube.com/embed/ZphbktbT26k',
    'https://m.youtube.com/shorts/ZphbktbT26k']) assert.equal(videoId(value), 'ZphbktbT26k');
  for (const value of ['', 'http://youtube.com/watch?v=ZphbktbT26k',
    'https://youtube.com.evil.test/watch?v=ZphbktbT26k', 'https://youtu.be/short']) assert.equal(videoId(value), null);
});

test('website accepts Twitch VOD links without accepting channels or live URLs', () => {
  for (const value of ['1234567890', 'https://www.twitch.tv/videos/1234567890',
    'https://twitch.tv/videos/1234567890/']) assert.equal(twitchId(value), '1234567890');
  for (const value of ['https://www.twitch.tv/gofns', 'http://twitch.tv/videos/1234567890',
    'https://twitch.tv.evil.test/videos/1234567890']) assert.equal(twitchId(value), null);
  assert.deepEqual(JSON.parse(JSON.stringify(mediaSource('twitch:1234567890'))), {provider: 'twitch', sourceId: '1234567890'});
  assert.equal(sourceKey('twitch', '1234567890'), 'twitch:1234567890');
});

test('website validates hosted indexes and enforces its spoiler-safe lead', () => {
  const index = validateIndex({...sample(), leadSeconds: 100});
  assert.equal(index.leadSeconds, 5);
  assert.equal(position(index, 93), -1);
  assert.equal(position(index, 95), 0);
  assert.equal(roundDestination(index, 95, 1), 245);
  assert.equal(roundDestination(index, 850, 1), null);
});

test('website validates provider-neutral Twitch indexes', () => {
  const index = validateIndex({schemaVersion: 2, provider: 'twitch', sourceId: '1234567890', rounds: sample().rounds});
  assert.equal(index.provider, 'twitch');
  assert.equal(index.sourceId, '1234567890');
});

test('website maps official Twitch chat onto aligned YouTube playback', () => {
  const index = validateIndex({schemaVersion: 2, provider: 'youtube', sourceId: 'ZphbktbT26k', rounds: sample().rounds,
    alignment: {source: 'twitch:1234567890', timelineScale: 1.002, segments: [
      {offset: 120, targetStart: 0, targetEnd: 500}, {offset: 300, targetStart: 500, targetEnd: 1000}
    ]}});
  assert.equal(sourceTime(index, 100), 220.2);
  assert.equal(sourceTime(index, 600), 901.2);
  assert.equal(Math.round(playbackTime(index, 901.2, 600)), 600);
  assert.throws(() => validateIndex({...sample(), schemaVersion: 2, provider: 'youtube', sourceId: 'ZphbktbT26k',
    alignment: {source: 'twitch:1234567890', timelineScale: 2, segments: []}}));
  const catalog = JSON.parse(readFileSync(resolve(__dirname, '../site/catalog.json'), 'utf8'));
  const aligned = catalog.videos.filter(entry => entry.provider === 'youtube' && entry.chat);
  assert.ok(aligned.length > 0);
  for (const entry of aligned) {
    const hosted = validateIndex(JSON.parse(readFileSync(resolve(__dirname, '../site' + entry.index), 'utf8')));
    assert.equal((hosted.chatAlignment || hosted.alignment).source, 'twitch:' + entry.chatSourceId);
    assert.equal(entry.chat, '/chats/twitch-' + entry.chatSourceId + '.json');
  }
});

test('website validates compact synchronized Twitch chat without future data', () => {
  const messages = validateChat({v: 1, source: '1234567890', messages: [
    {t: 10, u: 'viewer', c: '#12Ab34', f: [['hello '], ['Kappa', '25'], ['KEKW', '7tv:01F6M2T8P00000000000000000']]},
    {t: 12.5, u: 'other', c: '', f: [['nice']]}
  ]}, '1234567890');
  assert.equal(messages.length, 2);
  assert.equal(messages[0].fragments[1][1], '25');
  assert.equal(messages[0].fragments[2][1], '7tv:01F6M2T8P00000000000000000');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const vercel = readFileSync(resolve(__dirname, '../site/vercel.json'), 'utf8');
  assert.match(app, /https:\/\/cdn\.7tv\.app\/emote\//);
  assert.match(app, /const maxMessages = fullscreen \? 5 : 120/);
  assert.match(vercel, /https:\/\/cdn\.7tv\.app/);
  assert.throws(() => validateChat({v: 1, source: '1234567890', messages: [
    {t: 12, u: 'viewer', c: '', f: [['later']]}, {t: 10, u: 'viewer', c: '', f: [['earlier']]}
  ]}, '1234567890'));
});

test('map navigation lands on the first round without exposing map totals', () => {
  const index = validateIndex(sample());
  assert.equal(mapDestination(index, 250, 1), 695);
  assert.equal(mapDestination(index, 800, -1), 95);
  assert.equal(mapDestination(index, 800, 1), null);
});

test('start and resume gates keep their play icon through loading and retry labels', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const html = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const gate = html.slice(html.indexOf('<div id="start-gate"'), html.indexOf('<div id="end-shield"'));
  assert.doesNotMatch(gate, /class="gate-icon"/);
  assert.match(gate, /id="start-watching-label"/);
  assert.match(gate, /<svg class="gate-brand"[^>]*role="img" aria-label="SPOILLESS"/);
  assert.doesNotMatch(gate, /<span>SPOILLESS<\/span>/);
  assert.match(gate, /<button id="start-watching"[^>]*><svg/);
  assert.doesNotMatch(html, /class="chat-warning"|Viewer messages can still contain spoilers/);
  assert.doesNotMatch(app, /\$\('start-watching'\)\.textContent/);
  const start = app.indexOf("    $('start-gate-title').textContent = state.resumeTime");
  for (const resumeTime of [null, 321.25]) {
    const elements = new Map();
    const $ = id => {if (!elements.has(id)) elements.set(id, {}); return elements.get(id);};
    const state = {resumeTime};
    vm.runInContext(app.slice(start, app.indexOf('    applySettings();', start)), vm.createContext({$, state, coverage: ''}));
    assert.equal($('start-gate-title').textContent, resumeTime === null ? 'Watch from the start' : 'Continue watching');
    assert.equal($('start-watching-label').textContent, resumeTime === null ? 'Play match' : 'Resume');
    assert.equal(state.resumeTime, resumeTime);
  }
});

test('second-round auto-skip stays within a map and requires the exact next round', () => {
  const index = {leadSeconds: 5, rounds: [
    {map: 1, round: 1, start: 100}, {map: 1, round: 2, start: 200}, {map: 1, round: 3, start: 300},
    {map: 1, round: 13, start: 1300}, {map: 1, round: 14, start: 1400}, {map: 1, round: 15, start: 1500},
    {map: 2, round: 1, start: 2000}, {map: 2, round: 2, start: 2100}, {map: 2, round: 3, start: 2200}]};
  const skip = context.VodlockSite.autoSkipDestination;
  for (const time of [0, 100, 169.99, 300, 1300, 1369.99, 1500, 2000, 2069.99]) assert.equal(skip(index, time), null);
  assert.equal(skip(index, 170), 295);
  assert.equal(skip(index, 195), 295);
  assert.equal(skip(index, 1370), 1495);
  assert.equal(skip(index, 1395), 1495);
  assert.equal(skip(index, 2100), 2195);
  assert.equal(skip(index, 2070), 2195);
  assert.equal(skip({...index, rounds: index.rounds.slice(0, 2)}, 200), null);
  assert.equal(skip({...index, rounds: index.rounds.filter(round => round.round !== 3)}, 200), null);
  assert.equal(skip({...index, rounds: [index.rounds[1], index.rounds[6]]}, 200), null);
});

test('second-round skip is off by default and preserves saved boolean settings', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const declaration = app.match(/  const defaultSettings = .*;/)[0];
  for (const saved of [{}, {autoSkipSecondRounds: true}, {autoSkipSecondRounds: false}, {autoSkipSecondRounds: 'true'}]) {
    const sandbox = vm.createContext({localStorage: {getItem: () => JSON.stringify(saved)}});
    vm.runInContext(declaration + app.slice(app.indexOf('  function readSettings('), app.indexOf('  function saveSettings(')), sandbox);
    assert.equal(sandbox.readSettings().autoSkipSecondRounds, saved.autoSkipSecondRounds === true);
  }
  const html = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  assert.match(html, /id="setting-auto-skip-second-rounds" type="checkbox"/);
});

test('grouped settings preserve fullscreen chat preferences with native radio controls', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const html = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  for (const title of ['Playback', 'Spoiler protection', 'Chat', 'Keyboard']) assert.ok(html.includes('>' + title + '</h3>'));
  assert.equal((html.match(/role="switch"/g) || []).length, 9);
  const options = ['left', 'right', 'hidden'].map(value => ({value, checked: false}));
  const controls = new Map();
  const $ = id => {
    if (!controls.has(id)) controls.set(id, {type: 'checkbox', tagName: 'INPUT', classList: {toggle() {}},
      addEventListener(type, handler) {this.handler = handler;}});
    return controls.get(id);
  };
  Object.assign($('setting-fullscreen-chat-position'), {tagName: 'FIELDSET', querySelectorAll: () => options});
  let saved = JSON.stringify({fullscreenChatPosition: 'right', autoSkipSecondRounds: true});
  const sandbox = vm.createContext({$, state: {}, renderChat() {}, updateStatus() {},
    localStorage: {getItem: () => saved, setItem(key, value) {saved = value;}}});
  const declarations = app.slice(app.indexOf('  const defaultSettings ='), app.indexOf('  const teamLogos ='));
  vm.runInContext(declarations + app.slice(app.indexOf('  function readSettings('), app.indexOf('  function readKeybinds(')) +
    '\nlet settings = readSettings();\n' + app.slice(app.indexOf('  function applySettings('), app.indexOf('  function readFavouriteTeams(')) +
    app.slice(app.indexOf('  for (const [key, identifier] of Object.entries(settingIds)) {', app.indexOf("$('teams-close')")),
      app.indexOf("  for (const button of document.querySelectorAll('[data-keybind]')) {", app.indexOf("$('teams-close')"))), sandbox);
  sandbox.applySettings();
  assert.deepEqual(options.filter(option => option.checked).map(option => option.value), ['right']);
  assert.equal($('setting-auto-skip-second-rounds').checked, true);
  $('setting-fullscreen-chat-position').handler({target: {type: 'radio', value: 'hidden'}});
  assert.equal(JSON.parse(saved).fullscreenChatPosition, 'hidden');
  assert.deepEqual(options.filter(option => option.checked).map(option => option.value), ['hidden']);
  $('setting-show-chat').handler({target: {type: 'checkbox', checked: false}});
  assert.equal(JSON.parse(saved).showChat, false);
  assert.equal(sandbox.readSettings().fullscreenChatPosition, 'hidden');
});

test('Escape clears only the selected shortcut, persists it and allows reassignment and reset', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  let saved = '{}';
  const buttons = ['previousRound', 'nextRound', 'skipBackward', 'skipForward'].map(action => ({
    dataset: {keybind: action}, addEventListener(type, handler) {this.handler = handler;}, focus() {}
  }));
  const reset = {addEventListener(type, handler) {this.handler = handler;}};
  const notices = [];
  const sandbox = vm.createContext({$: () => reset, toast: value => notices.push(value),
    document: {querySelectorAll: () => buttons},
    localStorage: {getItem: () => saved, setItem(key, value) {saved = value;}}});
  vm.runInContext(app.match(/  const defaultKeybinds = .*;/)[0] +
    app.slice(app.indexOf('  function readKeybinds('), app.indexOf('  function formatTime(')) + '\nlet keybinds = readKeybinds();\n' +
    app.slice(app.indexOf("  for (const button of document.querySelectorAll('[data-keybind]')) {", app.indexOf("$('teams-close')")),
      app.indexOf("  for (const button of $('kind-filters')", app.indexOf("$('keybind-reset').addEventListener"))), sandbox);
  let prevented = false, stopped = false;
  buttons[0].handler({code: 'Escape', preventDefault() {prevented = true;}, stopPropagation() {stopped = true;}});
  assert.equal(prevented, true);
  assert.equal(stopped, true);
  assert.equal(buttons[0].textContent, 'Not set');
  assert.equal(sandbox.readKeybinds().previousRound, '');
  assert.equal(sandbox.readKeybinds().nextRound, 'ArrowDown');
  assert.equal(notices.length, 0);
  buttons[0].handler({code: 'KeyA', preventDefault() {}});
  assert.equal(sandbox.readKeybinds().previousRound, 'KeyA');
  assert.equal(buttons[0].textContent, 'A');
  buttons[1].handler({code: 'KeyA', preventDefault() {}});
  assert.equal(sandbox.readKeybinds().nextRound, 'ArrowDown');
  assert.equal(notices.at(-1), 'That key is already in use.');
  reset.handler();
  assert.equal(sandbox.readKeybinds().previousRound, 'ArrowUp');
  saved = JSON.stringify({previousRound: null, nextRound: '', skipForward: '!'});
  assert.equal(sandbox.readKeybinds().previousRound, 'ArrowUp');
  assert.equal(sandbox.readKeybinds().nextRound, '');
  assert.equal(sandbox.readKeybinds().skipForward, 'ArrowRight');
});

test('auto-skip only runs during playback, avoids repeat seeks and respects manual review', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const index = {leadSeconds: 5, rounds: [
    {map: 1, round: 1, start: 100}, {map: 1, round: 2, start: 200}, {map: 1, round: 3, start: 300},
    {map: 1, round: 13, start: 1300}, {map: 1, round: 14, start: 1400}, {map: 1, round: 15, start: 1500}]};
  const clock = {time: 170};
  const state = {index, ready: true, playing: true, autoSkippedRound: null};
  const settings = {autoSkipSecondRounds: false, hideRoundStatus: false};
  const destinations = [], notices = [];
  const elements = new Map();
  const $ = id => {if (!elements.has(id)) elements.set(id, {}); return elements.get(id);};
  const sandbox = vm.createContext({$, state, settings, VodlockSite: context.VodlockSite,
    currentTime: () => clock.time, seekPlayer: value => destinations.push(value), setTimeout() {},
    saveResume() {}, renderChat() {}, toast: message => notices.push(message)});
  vm.runInContext(app.slice(app.indexOf('  function seek('), app.indexOf('  function navigateRound(')), sandbox);
  sandbox.updateStatus();
  assert.equal(destinations.length, 0);
  settings.autoSkipSecondRounds = true;
  state.playing = false;
  sandbox.updateStatus();
  assert.equal(destinations.length, 0);
  state.playing = true;
  sandbox.updateStatus();
  sandbox.updateStatus();
  assert.deepEqual(destinations, [295]);
  assert.deepEqual(notices, ['Skipped round 2']);
  clock.time = 295;
  sandbox.seek(170, 'Review before round 2');
  clock.time = 170;
  sandbox.updateStatus();
  clock.time = 195;
  sandbox.updateStatus();
  assert.deepEqual(destinations, [295, 170]);
  clock.time = 1370;
  settings.hideRoundStatus = true;
  sandbox.updateStatus();
  assert.equal(destinations.at(-1), 1495);
  assert.equal(notices.at(-1), 'Skipped round');
  state.ready = false;
  state.autoSkippedRound = null;
  sandbox.updateStatus();
  assert.equal(destinations.length, 3);
});

test('7TV emotes match whole case-sensitive tokens and preserve native emotes and whitespace', () => {
  const id = '01F6M2T8P00000000000000000';
  const emotes = context.VodlockSite.seventvEmotes({emotes: [
    {name: 'KEKW', id}, {name: 'bad name', id}, {name: 'invalid', id: '../unsafe'},
    {name: '__proto__', id}
  ]});
  assert.equal(emotes.size, 2);
  const fragments = [['KEKW  KEKWish kekw\tKEKW'], ['KEKW', '25'], ['__proto__']];
  assert.deepEqual(JSON.parse(JSON.stringify(context.VodlockSite.chatFragments(fragments, emotes))), [
    ['KEKW', '7tv:' + id], ['  '], ['KEKWish'], [' '], ['kekw'], ['\t'], ['KEKW', '7tv:' + id],
    ['KEKW', '25'], ['__proto__', '7tv:' + id]
  ]);
  assert.equal(context.VodlockSite.chatFragments(fragments, new Map()), fragments);
  assert.equal(context.VodlockSite.seventvEmotes(null).size, 0);
  const vercel = readFileSync(resolve(__dirname, '../site/vercel.json'), 'utf8');
  assert.match(vercel, /connect-src 'self' https:\/\/7tv\.io;/);
});

function chatLoader(fetch) {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const sandbox = vm.createContext({fetch, AbortSignal, VodlockSite: context.VodlockSite, console: {warn() {}}});
  vm.runInContext('const state = {chat: []}; const chatEmoteSets = new Map(); let chatEmotes = new Map();\n' +
    app.slice(app.indexOf('  async function loadChat('), app.indexOf('  function resetPlayerHost(')), sandbox);
  return {
    load: (entry, index = {}) => sandbox.loadChat(entry, index),
    state: () => vm.runInContext('state', sandbox),
    emotes: () => vm.runInContext('chatEmotes', sandbox)
  };
}

test('chat protection flags likely results without changing messages or hiding ordinary chat', () => {
  const {isChatBot, isChatSpoiler} = context.VodlockSite;
  for (const name of ['Fossabot', 'NIGHTBOT', 'StreamElements']) assert.equal(isChatBot(name), true);
  for (const name of ['robot', 'fossabotfan', 'SomeViewer']) assert.equal(isChatBot(name), false);
  for (const text of ['who won earlier?', 'what was the score?', 'Team A won', 'A beat B', 'final score', 'A 2-0 B', 'A 13–8 B', 'score was 13:8']) {
    assert.equal(isChatSpoiler([[text]]), true, text);
  }
  for (const text of ['nice shot', 'they can win this', 'stream started at 13:08', '2026-10-11', '100T', 'see you at 4:30', 'I have 100-200 points']) {
    assert.equal(isChatSpoiler([[text]]), false, text);
  }
  const fragments = [['A 2'], ['-0 B'], ['KEKW', '25']];
  const before = JSON.stringify(fragments);
  assert.equal(isChatSpoiler(fragments), true);
  assert.equal(JSON.stringify(fragments), before);
  assert.equal(isChatSpoiler([['WON', '25']]), false);
});

test('late chat downloads cannot replace a newer source or a cancelled navigation', async () => {
  const resolvers = [];
  const loader = chatLoader(() => new Promise(resolve => resolvers.push(resolve)));
  const first = {provider: 'youtube', sourceId: 'abcdefghijk', chat: '/chats/twitch-1234567890.json', chatSourceId: '1234567890'};
  const second = {...first, sourceId: 'lmnopqrstuv', chat: '/chats/twitch-1234567891.json', chatSourceId: '1234567891'};
  const index = entry => ({alignment: {source: 'twitch:' + entry.chatSourceId}});
  const response = entry => ({ok: true, json: async () => ({v: 1, source: entry.chatSourceId,
    messages: [{t: 100, u: entry.sourceId, c: '', f: [['hello']]}]})});
  const older = loader.load(first, index(first));
  const newer = loader.load(second, index(second));
  resolvers[1](response(second));
  await newer;
  resolvers[0](response(first));
  await older;
  assert.equal(loader.state().chat[0].user, second.sourceId);
  assert.equal(loader.state().chatIndex.alignment.source, 'twitch:' + second.chatSourceId);
  loader.state().openRequest = 0;
  const cancelled = loader.load(first, index(first));
  loader.state().openRequest++;
  resolvers[2](response(first));
  await cancelled;
  assert.equal(loader.state().chat.length, 0);
  assert.equal(loader.state().chatIndex, null);
});

test('late index responses cannot reopen a cancelled VOD', async () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const entry = {key: 'first', provider: 'youtube', sourceId: 'abcdefghijk', index: '/indexes/first.json'};
  let resolveIndex;
  const sandbox = vm.createContext({state: {catalog: [entry], openRequest: 0}, entryKey: entry => entry.key,
    fetch: () => new Promise(resolve => {resolveIndex = resolve;}),
    VodlockSite: context.VodlockSite, loadChat: () => {throw new Error('Cancelled VOD loaded chat');}});
  vm.runInContext(app.slice(app.indexOf('  async function openVideo('), app.indexOf('  function loadYouTubeApi(')), sandbox);
  const pending = sandbox.openVideo('first');
  sandbox.state.openRequest++;
  resolveIndex({ok: true, json: () => {throw new Error('Cancelled VOD parsed its index');}});
  await pending;
  assert.equal(sandbox.state.entry, undefined);
});

test('YouTube watch parties keep separate canonical and Twitch chat alignment', async () => {
  const index = validateIndex({...sample(), schemaVersion: 2, provider: 'youtube', sourceId: 'ZphbktbT26k',
    alignment: {source: 'youtube:abcdefghijk', timelineScale: 1, strictCoverage: true,
      segments: [{targetStart: 0, targetEnd: 1000, offset: -50}]},
    chatAlignment: {source: 'twitch:1234567890', timelineScale: 1, strictCoverage: true,
      segments: [{targetStart: 100, targetEnd: 300, offset: 50}]}});
  const loader = chatLoader(async () => ({ok: true, json: async () => ({v: 1, source: '1234567890',
    messages: [{t: 200, u: 'Viewer', c: '', f: [['hello']]}]})}));
  await loader.load({provider: 'youtube', sourceId: 'ZphbktbT26k', chat: '/chats/twitch-1234567890.json',
    chatSourceId: '1234567890'}, index);
  assert.equal(loader.state().chat.length, 1);
  assert.equal(sourceTime(loader.state().chatIndex, 150), 200);
  assert.equal(sourceTime(index, 150), 100);
  assert.ok(Number.isNaN(sourceTime(loader.state().chatIndex, 350)));
});

test('match source selection prefers verified YouTube watch parties per creator with Twitch fallback', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const official = {id: 'official', kind: 'match', provider: 'youtube', match: 'first', playedAt: '2026-10-11T10:00:00Z'};
  const twitch = {id: 'twitch', kind: 'watch-party', provider: 'twitch', creator: 'FNS', match: 'first', playedAt: '2026-10-11T10:00:00Z'};
  const youtube = {...twitch, id: 'youtube', provider: 'youtube'};
  const other = {...twitch, id: 'other', creator: 'ohnepixel'};
  const state = {catalog: [official, twitch, youtube, other, {...twitch, id: 'second', match: 'second'}]};
  const sandbox = vm.createContext({state, VodlockSite: context.VodlockSite, matchKey: entry => entry.match,
    matchup: () => [], entryKind: entry => entry.kind, creatorName: entry => entry.creator});
  vm.runInContext(app.slice(app.indexOf('  function groupedMatches('), app.indexOf('  function tournamentName(')), sandbox);
  let groups = sandbox.groupedMatches();
  assert.deepEqual(Array.from(groups[0].entries, entry => entry.id), ['official', 'youtube', 'other']);
  assert.deepEqual(Array.from(groups[1].entries, entry => entry.id), ['second']);
  assert.equal(state.catalog.length, 5);
  state.catalog = [official, twitch, {...youtube, playedAt: '2026-10-10T10:00:00Z'}];
  assert.ok(sandbox.groupedMatches()[0].entries.some(entry => entry.id === 'twitch'));
  state.catalog = [official, twitch, other];
  groups = sandbox.groupedMatches();
  assert.deepEqual(Array.from(groups[0].entries, entry => entry.id), ['official', 'twitch', 'other']);
  state.catalog.push({...other, id: 'ohne-youtube', provider: 'youtube'});
  assert.deepEqual(Array.from(sandbox.groupedMatches()[0].entries, entry => entry.id), ['official', 'ohne-youtube', 'twitch']);
});

test('rematches keep separate source lists and watched identities across dates and match IDs', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const older = {title: 'VIT vs LOUD', playedAt: '2026-09-30T09:08:51Z', provider: 'youtube', kind: 'match'};
  const today = {...older, title: 'LOUD vs VIT', playedAt: '2026-10-16T08:08:45+02:00', expectedMatchId: 'today'};
  const sandbox = vm.createContext({state: {catalog: [today, older, {...older, provider: 'twitch', kind: 'watch-party', creator: 'FNS'}]},
    VodlockSite: context.VodlockSite, matchup: entry => entry.title.split(' vs '), teamCode: value => value,
    entryKind: entry => entry.kind, creatorName: entry => entry.creator});
  vm.runInContext(app.slice(app.indexOf('  function matchKey('), app.indexOf('  function creatorName(')) +
    app.slice(app.indexOf('  function groupedMatches('), app.indexOf('  function tournamentName(')), sandbox);
  const groups = sandbox.groupedMatches();
  assert.equal(groups.length, 2);
  assert.equal(groups[0].entries.length, 1);
  assert.equal(groups[1].entries.length, 2);
  assert.notEqual(sandbox.matchKey(today), sandbox.matchKey(older));
  assert.notEqual(sandbox.matchKey(today), sandbox.matchKey({...today, expectedMatchId: 'another-same-day-series'}));
  assert.notEqual(sandbox.matchKey({...today, expectedMatchId: undefined}), sandbox.matchKey(older));
  assert.equal(sandbox.matchKey({...older, playedAt: '2026-09-30T11:08:51+02:00'}), sandbox.matchKey(older));
});

test('late-start watch parties retain actual round numbers and clearly label their available start', () => {
  const raw = {schemaVersion: 2, provider: 'youtube', sourceId: 'abcdefghijk',
    rounds: [{map: 1, round: 5, start: 55}, {map: 1, round: 6, start: 155}],
    coverage: {type: 'late-start', omittedOpeningRounds: 4, firstMap: 1, firstRound: 5}};
  const index = validateIndex(raw);
  assert.equal(context.VodlockSite.coverageLabel(index.coverage), 'Starts at round 5');
  assert.equal(roundDestination(index, 55, -1), null);
  assert.equal(roundDestination(index, 55, 1), 150);
  for (const change of [{firstRound: 1}, {omittedOpeningRounds: true}, {omittedOpeningRounds: 0}, {type: 'gap'}]) {
    assert.throws(() => validateIndex({...raw, coverage: {...raw.coverage, ...change}}), /partial coverage/);
  }
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  assert.match(app, /coverageLabel\(entry.coverage\)/);
  assert.match(app, /Earlier rounds are not in this recording/);
});

test('chat protection defaults on for existing users and preserves explicit opt-outs', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const declaration = app.match(/  const defaultSettings = .*;/)[0];
  for (const saved of [{}, {showChat: false}, {hideChatBots: false, filterChatSpoilers: false}]) {
    const sandbox = vm.createContext({localStorage: {getItem: () => JSON.stringify(saved)}});
    vm.runInContext(declaration + app.slice(app.indexOf('  function readSettings('), app.indexOf('  function saveSettings(')), sandbox);
    const settings = sandbox.readSettings();
    assert.equal(settings.hideChatBots, saved.hideChatBots ?? true);
    assert.equal(settings.filterChatSpoilers, saved.filterChatSpoilers ?? true);
  }
});

test('chat rendering removes bots and spoilers before the cap without placeholders', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const makeNode = tag => ({tag, children: [], style: {}, handlers: {}, isConnected: true,
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren() { this.children.forEach(node => {node.isConnected = false;}); this.children = []; },
    addEventListener(event, callback) { this.handlers[event] = callback; },
    setAttribute() {}, classList: {toggle() {}}});
  const elements = new Map();
  const $ = id => {if (!elements.has(id)) elements.set(id, makeNode('div')); return elements.get(id);};
  const message = (user, text, time = 100) => ({user, fragments: [[text]], time});
  const state = {chat: [message('Viewer', 'nice shot'), message('Other', 'who won earlier?'),
    ...Array.from({length: 125}, () => message('Fossabot', 'bot text')),
    ...Array.from({length: 125}, () => message('Other', 'who won earlier?')), message('Future', 'future text', 999)], chatPosition: null};
  const settings = {showChat: true, hideChatBots: true, filterChatSpoilers: true};
  const sandbox = vm.createContext({$, state, settings, VodlockSite: context.VodlockSite,
    document: {fullscreenElement: null, createElement: makeNode, createTextNode: text => ({text})},
    currentTime: () => 100, syncFullscreenButton() {}, formatTime: String});
  vm.runInContext('const chatEmotes = new Map();' +
    app.slice(app.indexOf('  function renderChat('), app.indexOf('  async function loadChat(')), sandbox);
  sandbox.renderChat();
  const host = $('chat-messages');
  assert.equal(host.children.length, 1);
  assert.equal(host.children[0].tag, 'p');
  sandbox.renderChat(true);
  assert.equal(host.children.length, 1);
  sandbox.document.fullscreenElement = $('watch-layout');
  sandbox.renderChat(true);
  assert.equal(host.children.length, 1);
  sandbox.document.fullscreenElement = null;
  settings.filterChatSpoilers = false;
  sandbox.renderChat(true);
  assert.equal(host.children.length, 120);
  assert.equal(host.children[1].tag, 'p');
  settings.hideChatBots = false;
  sandbox.renderChat(true);
  assert.equal(host.children.length, 120);
  settings.filterChatSpoilers = true;
  settings.hideChatBots = true;
  state.chat = [message('Other', 'who won earlier?')];
  sandbox.renderChat(true);
  assert.equal(host.children.length, 1);
  assert.equal(host.children[0].className, 'chat-empty');
  state.chatIndex = {alignment: {timelineScale: 1, strictCoverage: true,
    segments: [{targetStart: 200, targetEnd: 300, offset: 0}]}};
  sandbox.renderChat(true);
  assert.match(host.children[0].textContent, /Chat is not synced/);
});

test('chat toggle displays its current visibility rather than the opposite action', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const body = app.slice(app.indexOf('  function renderChat('), app.indexOf('    const layout =', app.indexOf('  function renderChat(')));
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {setAttribute(name, value) {this[name] = value;}});
    return elements.get(id);
  };
  const sandbox = vm.createContext({$, state: {chat: [{}]}, settings: {showChat: true}, document: {fullscreenElement: null}});
  vm.runInContext(body + '\n}', sandbox);
  sandbox.renderChat();
  assert.equal($('chat-toggle').textContent, 'Chat on');
  assert.equal($('chat-toggle')['aria-pressed'], 'true');
  assert.equal($('chat-panel').hidden, false);
  sandbox.settings.showChat = false;
  sandbox.renderChat();
  assert.equal($('chat-toggle').textContent, 'Chat off');
  assert.equal($('chat-toggle')['aria-pressed'], 'false');
  assert.equal($('chat-panel').hidden, true);
});

test('7TV lookup is nonblocking, cached, and channel aliases override global emotes', async () => {
  const calls = [];
  const resolveEmotes = [];
  const loader = chatLoader(url => {
    calls.push(url);
    if (url.startsWith('/chats/')) return Promise.resolve({ok: true, json: async () => ({v: 1, source: '1234567890', messages: [{t: 1, u: 'Viewer', c: '', f: [['KEKW']]}]})});
    return new Promise(resolve => resolveEmotes.push(resolve));
  });
  const entry = {provider: 'twitch', sourceId: '1234567890', chat: '/chats/twitch-1234567890.json', chatChannelId: '66418614'};
  await loader.load(entry);
  assert.equal(loader.state().chat.length, 1);
  assert.equal(loader.emotes().size, 0);
  const globalId = '01F6M2T8P00000000000000000';
  const channelId = '01F6M2T8P00000000000000001';
  resolveEmotes[0]({ok: true, json: async () => ({emotes: [{name: 'KEKW', id: globalId}]})});
  resolveEmotes[1]({ok: true, json: async () => ({emote_set: {emotes: [{name: 'KEKW', id: channelId}]}})});
  await new Promise(setImmediate);
  assert.equal(loader.emotes().get('KEKW'), '7tv:' + channelId);
  assert.equal(loader.state().chatPosition, null);
  await loader.load(entry);
  await new Promise(setImmediate);
  assert.equal(calls.filter(url => url.startsWith('https:')).length, 2);
  assert.equal(loader.emotes().get('KEKW'), '7tv:' + channelId);
});

test('late 7TV responses cannot alter another VOD chat', async () => {
  const resolveEmotes = [];
  const loader = chatLoader(url => url.startsWith('/chats/')
    ? Promise.resolve({ok: true, json: async () => ({v: 1, source: '1234567890', messages: []})})
    : new Promise(resolve => resolveEmotes.push(resolve)));
  await loader.load({provider: 'twitch', sourceId: '1234567890', chat: '/chats/twitch-1234567890.json', chatChannelId: '66418614'});
  await loader.load({provider: 'youtube', sourceId: 'ZphbktbT26k'});
  for (const resolve of resolveEmotes) resolve({ok: true, json: async () => ({emotes: [{name: 'KEKW', id: '01F6M2T8P00000000000000000'}]})});
  await new Promise(setImmediate);
  assert.equal(loader.emotes().size, 0);
  assert.equal(loader.state().chat.length, 0);
});

test('7TV errors preserve readable chat and malformed channel IDs are never requested', async () => {
  let requests = 0;
  const loader = chatLoader(url => {
    if (url.startsWith('https:')) { requests++; return Promise.reject(new Error('offline')); }
    return Promise.resolve({ok: true, json: async () => ({v: 1, source: '1234567890', messages: [{t: 1, u: 'Viewer', c: '', f: [['KEKW']]}]})});
  });
  const entry = {provider: 'twitch', sourceId: '1234567890', chat: '/chats/twitch-1234567890.json', chatChannelId: '../unsafe'};
  await loader.load(entry);
  assert.equal(requests, 0);
  await loader.load({...entry, chatChannelId: '66418614'});
  await new Promise(setImmediate);
  assert.equal(requests, 2);
  assert.equal(loader.emotes().size, 0);
  assert.equal(loader.state().chat[0].fragments[0][0], 'KEKW');
});

test('website rejects malformed hosted indexes', () => {
  for (const mutate of [
    value => { value.videoId = 'invalid'; },
    value => { value.rounds = []; },
    value => { value.rounds[1].start = 50; },
    value => { value.rounds[1].round = 1; },
    value => { value.rounds[0].map = 0; }
  ]) {
    const value = sample();
    mutate(value);
    assert.throws(() => validateIndex(value));
  }
});

test('website release surfaces include the favicon and legal pages', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  assert.match(index, /href="\/favicon\.svg"/);
  assert.match(index, /href="\/privacy\.html"/);
  assert.match(index, /href="\/terms\.html"/);
  for (const page of ['privacy.html', 'terms.html']) {
    const source = readFileSync(resolve(__dirname, '../site/' + page), 'utf8');
    assert.match(source, /href="https:\/\/www\.youtube\.com\/t\/terms"/);
    assert.match(source, /href="https:\/\/www\.twitch\.tv\/p\/en\/legal\/terms-of-service\/"/);
    assert.match(source, /@dibedy/);
    assert.doesNotMatch(source, /draft|inserted here|before public launch/i);
  }
});

test('website keeps concise archive copy and uses ten-second arrow navigation', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  assert.match(index, /id="spoiler-word" class="spoiler-word"/);
  assert.doesNotMatch(index, /spoiler-safe/gi);
  assert.match(index, /aria-label="Go back 10 seconds"/);
  assert.match(index, /aria-label="Skip forward 10 seconds"/);
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  assert.doesNotMatch(app, /\+ 31|31 seconds/);
  assert.match(app, /twitch-timeline-hidden/);
  assert.match(app, /await \$\('watch-layout'\)\.requestFullscreen\(\)/);
  assert.match(index, /setting-hide-twitch-timeline/);
  assert.match(index, /setting-show-chat/);
  assert.match(index, /setting-fullscreen-chat-position/);
  assert.match(index, /id="watch-layout" class="watch-layout"/);
  assert.match(index, /id="chat-toggle"/);
  assert.doesNotMatch(index, /SYNCED/);
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  assert.match(app, /spoilerWord\.setAttribute\('aria-pressed', String\(!revealed\)\)/);
  assert.doesNotMatch(index, /Click to reveal/);
  assert.match(styles, /\.spoiler-word > span \{[^}]+filter: blur\(14px\)[^}]+transition: filter 240ms ease/);
  assert.match(styles, /assets\/noise\.svg/);
  assert.match(styles, /body \{[^}]+background: var\(--bg\) url\("\/assets\/noise\.svg"\) repeat/);
  assert.doesNotMatch(styles, /assets\/halftone\.svg/);
  assert.match(styles, /\.library-heading::after \{[^}]+background: #455047/);
  assert.match(styles, /\.library-heading::before \{[^}]+radial-gradient\(ellipse 24% 36% at 22% 48%/);
  assert.doesNotMatch(styles, /\.library-summary::before/);
  assert.doesNotMatch(styles, /\.library-title::before/);
  assert.match(styles, /\.library-summary \{[^}]+transform: translateY\(clamp\(12px, 2vw, 28px\)\)/);
  assert.match(styles, /twitch-timeline-hidden #media-player iframe[^}]+top: -90px[^}]+height: calc\(100% \+ 180px\)/);
  assert.match(styles, /watch-layout:is\(:fullscreen, \.window-fullscreen\) \.chat-panel/);
  assert.match(styles, /\.chat-panel \{[^}]+width: min\([^}]+clamp\(340px, 22vw, 430px\)[^}]+height: 16vh[^}]+top: 34vh[^}]+left: clamp/);
  assert.match(styles, /fullscreen-chat-right \.chat-panel/);
  assert.match(styles, /watch-layout:is\(:fullscreen, \.window-fullscreen\) \.chat-message \{[^}]+font-size: clamp\(12px, \.85vw, 16px\)/);
  assert.match(styles, /fullscreen-controls-visible \.player-controls \{ visibility: visible; pointer-events: auto; \}/);
});

test('website library supports source, region, and all VCT team filters', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  assert.match(index, /id="kind-filters"/);
  assert.match(index, /data-kind="match"/);
  assert.match(index, /data-kind="watch-party"/);
  assert.match(index, /id="region-filters"/);
  assert.match(index, /data-region="americas"/);
  assert.match(index, /data-region="emea"/);
  assert.match(index, /data-region="pacific"/);
  assert.match(index, /data-region="china"/);
  assert.match(index, /id="team-filter"/);
  assert.match(index, /aria-haspopup="dialog"/);
  assert.doesNotMatch(index, /<select id="team-filter"/);
  assert.match(app, /function matchup\(entry\)/);
  assert.match(app, /const vctTeams = \[/);
  assert.equal((app.match(/region: '(?:americas|emea|pacific|china)'/g) || []).length, 48);
  assert.match(app, /state\.regionFilter/);
  assert.match(app, /state\.teamFilter/);
  assert.match(app, /function filteredMatches\(\)/);
  assert.match(app, /Official matches/);
  assert.match(app, /Watch parties/);
  assert.match(app, /vod-card/);
  assert.doesNotMatch(index, /class="hero"/);
  assert.doesNotMatch(app, /team-mark/);
  assert.doesNotMatch(index, /video-search|video-url|Open a VOD/);
  assert.match(index, /<h2 id="catalog-title" class="major-heading">Matches<\/h2>/);
  assert.doesNotMatch(index, /LIVE ARCHIVE|Choose a match\.<\/h2>/);
  assert.doesNotMatch(app, /arrow\.textContent/);
  assert.doesNotMatch(styles, /\.section-heading h2 \{[^}]+7vw|\.catalog-library \{[^}]+border-top: 3px/);
});

test('website groups broadcasts by match and keeps resume state spoiler safe', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const vercel = readFileSync(resolve(__dirname, '../site/vercel.json'), 'utf8');
  assert.match(app, /function matchKey\(entry\)/);
  assert.match(app, /function groupedMatches\(\)/);
  assert.match(app, /playedTime\(second\.primary\.playedAt\) - VodlockSite\.playedTime\(first\.primary\.playedAt\)/);
  assert.match(app, /source-list/);
  assert.match(app, /teamLogos/);
  for (const team of ['ge', 'vit', '100t', 't1', 'ns', 'nrg', 'jdg', 'fut', 'loud', 'edg']) {
    assert.equal(existsSync(resolve(__dirname, '../site/assets/teams/' + team + '.png')), true);
  }
  assert.doesNotMatch(vercel, /owcdn\.net/);
  assert.match(index, /id="continue-panel"/);
  assert.match(app, /spoilless-resume/);
  assert.match(index, /id="theater-button"/);
  assert.match(app, /function toggleTheater\(\)/);
  assert.doesNotMatch(app, /getDuration|remainingDuration|progressPercentage|totalRounds|totalMaps/);
  assert.doesNotMatch(index, /progress-bar|round-progress|map-progress/);
  assert.match(app, /'MAP ' \+ round\.map \+ ' · ROUND ' \+ round\.round/);
});

test('website keeps playback shortcuts available after interacting with provider frames', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  assert.match(index, /id="player-shell" class="player-shell" tabindex="-1"/);
  assert.match(app, /function reclaimPlayerFocus\(\)/);
  assert.match(app, /document\.activeElement\?\.tagName === 'IFRAME'/);
  assert.match(app, /addEventListener\('blur', reclaimPlayerFocus\)/);
  assert.match(app, /event\.code === 'Space'/);
  assert.doesNotMatch(styles, /twitch-timeline-hidden #media-player iframe[^}]+pointer-events: none/);
});

test('website keeps favourite teams spoiler safe while tournament browsing is unavailable', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  const catalog = JSON.parse(readFileSync(resolve(__dirname, '../site/catalog.json'), 'utf8'));
  assert.match(index, /id="choose-teams"/);
  assert.match(index, /id="teams-dialog"/);
  assert.match(index, /id="favourite-match-count"/);
  assert.doesNotMatch(index, /id="tournaments"/);
  assert.match(app, /spoilless-favourite-teams/);
  assert.match(app, /spoilless-watched-matches/);
  assert.match(app, /function openTeamPicker\(mode\)/);
  assert.match(app, /function tournamentMatches\(\)/);
  assert.match(app, /tournament-match is-' \+ status/);
  assert.match(app, /title\.textContent = 'Locked'/);
  assert.match(app, /markMatchWatched\(state\.entry\)/);
  assert.match(styles, /\.team-picker \{/);
  assert.ok(catalog.videos.every(entry => typeof entry.tournament === 'string' && typeof entry.tournamentKey === 'string'));
});

test('website parses match times for strict newest-first ordering', () => {
  assert.equal(playedTime('2026-09-29T12:00:00Z'), Date.parse('2026-09-29T12:00:00Z'));
  assert.equal(playedTime('invalid'), 0);
  assert.equal(playedTime(null), 0);
  const catalog = JSON.parse(readFileSync(resolve(__dirname, '../site/catalog.json'), 'utf8'));
  const times = catalog.videos.map(entry => playedTime(entry.playedAt));
  assert.ok(times.every(time => time > 0));
  assert.deepEqual(times, times.slice().sort((first, second) => second - first));
});

test('website keeps Twitch native fullscreen from bypassing the hidden timeline', () => {
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const vercel = readFileSync(resolve(__dirname, '../site/vercel.json'), 'utf8');
  assert.match(app, /function preventTwitchNativeFullscreen\(\)/);
  assert.match(app, /iframe\.removeAttribute\('allowfullscreen'\)/);
  assert.match(app, /new MutationObserver\(preventTwitchNativeFullscreen\)/);
  assert.match(vercel, /fullscreen=\(self\)/);
  assert.doesNotMatch(vercel, /fullscreen=\(self [^)]+\)/);
});

test('website keeps settings visible in the sticky header', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  const header = index.match(/<header[\s\S]*?<\/header>/)?.[0] || '';
  const footer = index.match(/<footer[\s\S]*?<\/footer>/)?.[0] || '';
  assert.match(header, /id="settings-button" class="header-settings"/);
  assert.match(header, /class="header-support" href="https:\/\/ko-fi\.com\/dibedy"/);
  assert.ok(header.indexOf('class="header-support"') < header.indexOf('id="site-info"'));
  assert.ok(header.indexOf('id="site-info"') < header.indexOf('id="settings-button"'));
  assert.doesNotMatch(footer, /settings-button/);
  assert.match(styles, /\.header-settings/);
});

test('website accepts bounded round-level alignments without filling their unverified gaps', () => {
  const segments = Array.from({length: 250}, (_, index) => ({offset: 100, targetStart: index * 20, targetEnd: index * 20 + 10}));
  const value = {...sample(), schemaVersion: 2, provider: 'twitch', sourceId: '1234567890',
    alignment: {source: 'youtube:ZphbktbT26k', timelineScale: 1, strictCoverage: true, segments}};
  const index = validateIndex(value);
  assert.equal(index.alignment.segments.length, 250);
  assert.equal(sourceTime(index, 2005), 2105);
  assert.ok(Number.isNaN(sourceTime(index, 2015)));
  assert.throws(() => validateIndex({...value, alignment: {...value.alignment,
    segments: Array.from({length: 5001}, (_, index) => ({offset: 100, targetStart: index * 20, targetEnd: index * 20 + 10}))}}));
  const overlapping = segments.map(segment => ({...segment}));
  overlapping[200].targetStart = overlapping[199].targetEnd - 1;
  assert.throws(() => validateIndex({...value, alignment: {...value.alignment, segments: overlapping}}));
  const invalid = segments.map(segment => ({...segment}));
  invalid[200].offset = NaN;
  assert.throws(() => validateIndex({...value, alignment: {...value.alignment, segments: invalid}}));
});

test('canonical chat alignment does not guess inside an unverified discontinuity', () => {
  const index = validateIndex({schemaVersion: 2, provider: 'youtube', sourceId: 'ZphbktbT26k', rounds: sample().rounds,
    alignment: {source: 'twitch:1234567890', timelineScale: 1, strictCoverage: true, segments: [
      {offset: 100, targetStart: 0, targetEnd: 400}, {offset: 200, targetStart: 600, targetEnd: 1000}
    ]}});
  assert.equal(sourceTime(index, 300), 400);
  assert.ok(Number.isNaN(sourceTime(index, 500)));
  assert.ok(Number.isNaN(playbackTime(index, 700, 500)));
});

test('website explains processing targets without promising a deadline or exposing spoilers', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  const header = index.match(/<header[\s\S]*?<\/header>/)?.[0] || '';
  assert.match(header, /id="site-info" class="site-info"/);
  assert.match(header, /aria-label="How SPOILLESS works"/);
  assert.match(header, /within about 2 hours after the match ends/);
  assert.match(header, /not a fixed deadline/);
  assert.match(header, /official VOD doesn't wait for the watch party/);
  assert.match(header, /Chat can be added separately/);
  assert.match(header, /I'm aiming/);
  assert.match(header, /My indexer finds and checks/);
  assert.doesNotMatch(header, /\b(?:We|We're|we|our)\b/);
  assert.doesNotMatch(index, /faq-section|processing-status/);
  assert.match(styles, /\.info-content[^}]*overflow-y: auto/);
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  assert.match(app, /\$\('site-info'\)\.contains\(event\.target\)/);
  assert.match(app, /event\.key === 'Escape' && \$\('site-info'\)\.open/);
  assert.match(app, /\$\('settings-dialog'\)\.open \|\| \$\('site-info'\)\.open \|\| event\.defaultPrevented/);
});

test('website exposes Twitch quality without exposing playback length', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  assert.match(index, /id="quality" aria-label="Video quality"/);
  assert.match(app, /state\.player\.getQualities\(\)/);
  assert.match(app, /state\.player\.setQuality\(event\.target\.value\)/);
  assert.match(app, /state\.entry\?\.provider !== 'twitch'/);
  assert.doesNotMatch(app, /getDuration/);
});

test('website keeps secondary player controls compact', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  assert.match(index, /class="map-inline" aria-label="Map navigation"/);
  assert.match(index, /class="control-group playback-controls"/);
  assert.match(index, /class="control-group audio-controls"/);
  assert.match(index, /class="control-group view-controls"/);
  assert.doesNotMatch(index, /class="map-controls"|player-footnote|timeline-status|class="shortcuts"/);
  assert.match(styles, /\.view-controls/);
  assert.match(styles, /\.player-controls button \{[^}]+height: 44px/);
  assert.match(styles, /\.map-inline button \{[^}]+min-height: 38px/);
});

test('website uses the SPOILLESS public identity', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const source = [index, ...['privacy.html', 'terms.html', 'app.js']
    .map(file => readFileSync(resolve(__dirname, '../site/' + file), 'utf8'))].join('\n');
  const vercel = readFileSync(resolve(__dirname, '../site/vercel.json'), 'utf8');
  assert.match(source, /SPOILLESS/);
  assert.doesNotMatch(source, /VODLOCK/);
  assert.match(vercel, /spoilless\.vercel\.app/);
  assert.doesNotMatch(vercel, /vodlock\.vercel\.app/);
  const header = index.match(/<header[\s\S]*?<\/header>/)?.[0] || '';
  assert.match(header, /<svg[\s\S]*?M5 5l22 22/);
});

test('website is ready for crawling and sharing', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const privacy = readFileSync(resolve(__dirname, '../site/privacy.html'), 'utf8');
  const terms = readFileSync(resolve(__dirname, '../site/terms.html'), 'utf8');
  const robots = readFileSync(resolve(__dirname, '../site/robots.txt'), 'utf8');
  const sitemap = readFileSync(resolve(__dirname, '../site/sitemap.xml'), 'utf8');
  assert.match(index, /rel="canonical" href="https:\/\/spoilless\.vercel\.app\/"/);
  assert.match(index, /property="og:title" content="SPOILLESS \| Spoiler-free VALORANT VODs"/);
  assert.match(index, /application\/ld\+json/);
  assert.match(index, /id="info-title"/);
  assert.match(privacy, /rel="canonical" href="https:\/\/spoilless\.vercel\.app\/privacy"/);
  assert.match(terms, /rel="canonical" href="https:\/\/spoilless\.vercel\.app\/terms"/);
  assert.match(robots, /Sitemap: https:\/\/spoilless\.vercel\.app\/sitemap\.xml/);
  assert.match(sitemap, /https:\/\/spoilless\.vercel\.app\//);
});

test('legal pages stay compact and describe actual browser storage and emote requests', () => {
  const privacy = readFileSync(resolve(__dirname, '../site/privacy.html'), 'utf8');
  const terms = readFileSync(resolve(__dirname, '../site/terms.html'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  for (const source of [privacy, terms]) {
    assert.doesNotMatch(source, /SPOILLESS LEGAL|class="eyebrow"/);
    assert.match(source, /Updated 11 October 2026/);
  }
  assert.match(terms, /<h1>Terms<\/h1>/);
  assert.match(privacy, /<h1>Privacy<\/h1>/);
  assert.match(privacy, /favourite teams, watched matches, keyboard shortcuts and your last playback position/);
  assert.match(privacy, /7TV to look up emotes/);
  assert.match(privacy, /IP address and browser details/);
  assert.match(privacy, /only after you press Start watching/);
  assert.match(styles, /\.legal-page h1 \{[^}]*font-size: 40px;[^}]*letter-spacing: 0;/);
  assert.doesNotMatch(styles, /\.legal-intro[^}]*28%/);
});

test('website loads Vercel Web Analytics on every page', () => {
  for (const page of ['index.html', 'privacy.html', 'terms.html']) {
    const source = readFileSync(resolve(__dirname, '../site/' + page), 'utf8');
    assert.match(source, /<script defer src="\/_vercel\/insights\/script\.js"><\/script>/);
  }
  const privacy = readFileSync(resolve(__dirname, '../site/privacy.html'), 'utf8');
  assert.match(privacy, /Vercel Web Analytics/);
  assert.doesNotMatch(privacy, /does not provide [^<]*analytics/);
});

test('website avoids the prohibited design and copy patterns', () => {
  const source = ['index.html', 'privacy.html', 'terms.html', 'styles.css', 'app.js']
    .map(file => readFileSync(resolve(__dirname, '../site/' + file), 'utf8')).join('\n');
  for (const pattern of [/\u2014/u, /made with ai/i, /purple/i, /border-radius:\s*999/i,
    /animation\s*:/i, /@keyframes/i, /cursor\s*animation/i, /fake\s+(review|metric|counter)/i]) {
    assert.doesNotMatch(source, pattern);
  }
});
