const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const context = vm.createContext({URL, URLSearchParams});
vm.runInContext(readFileSync(resolve(__dirname, '../site/core.js'), 'utf8'), context);
const {videoId, twitchId, mediaSource, sourceKey, validateIndex, position, roundDestination, mapDestination} = context.VodlockSite;
const sample = () => ({schemaVersion: 1, videoId: 'ZphbktbT26k', rounds: [
  {map: 1, round: 1, start: 100}, {map: 1, round: 2, start: 250},
  {map: 2, round: 1, start: 700}, {map: 2, round: 2, start: 850}
]});

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

test('map navigation lands on the first round without exposing map totals', () => {
  const index = validateIndex(sample());
  assert.equal(mapDestination(index, 250, 1), 695);
  assert.equal(mapDestination(index, 800, -1), 95);
  assert.equal(mapDestination(index, 800, 1), null);
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
    assert.match(source, /YouTube Terms of Service/);
    assert.match(source, /Twitch Terms of Service/);
    assert.match(source, /@dibedy/);
    assert.doesNotMatch(source, /draft|inserted here|before public launch/i);
  }
});

test('website mentions spoiler-safe once and uses ten-second arrow navigation', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  assert.equal((index.match(/spoiler-safe/gi) || []).length, 1);
  assert.match(index, /-10 s \/ \+10 s/);
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  assert.doesNotMatch(app, /\+ 31|31 seconds/);
});

test('website avoids the prohibited design and copy patterns', () => {
  const source = ['index.html', 'privacy.html', 'terms.html', 'styles.css', 'app.js']
    .map(file => readFileSync(resolve(__dirname, '../site/' + file), 'utf8')).join('\n');
  for (const pattern of [/\u2014/u, /made with ai/i, /purple/i, /border-radius:\s*999/i,
    /animation\s*:/i, /@keyframes/i, /cursor\s*animation/i, /fake\s+(review|metric|counter)/i]) {
    assert.doesNotMatch(source, pattern);
  }
});
