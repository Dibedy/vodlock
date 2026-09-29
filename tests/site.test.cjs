const assert = require('node:assert/strict');
const {existsSync, readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const context = vm.createContext({URL, URLSearchParams});
vm.runInContext(readFileSync(resolve(__dirname, '../site/core.js'), 'utf8'), context);
const {videoId, twitchId, mediaSource, sourceKey, validateIndex, validateChat, position, roundDestination, mapDestination} = context.VodlockSite;
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

test('website validates compact synchronized Twitch chat without future data', () => {
  const messages = validateChat({v: 1, source: '1234567890', messages: [
    {t: 10, u: 'viewer', c: '#12Ab34', f: [['hello '], ['Kappa', '25']]},
    {t: 12.5, u: 'other', c: '', f: [['nice']]}
  ]}, '1234567890');
  assert.equal(messages.length, 2);
  assert.equal(messages[0].fragments[1][1], '25');
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

test('website keeps concise archive copy and uses ten-second arrow navigation', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  assert.match(index, /id="spoiler-word" class="spoiler-word"/);
  assert.doesNotMatch(index, /spoiler-safe/gi);
  assert.match(index, /aria-label="Go back 10 seconds"/);
  assert.match(index, /aria-label="Skip forward 10 seconds"/);
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  assert.doesNotMatch(app, /\+ 31|31 seconds/);
  assert.match(app, /twitch-timeline-hidden/);
  assert.match(app, /requestFullscreen\) \$\('watch-layout'\)\.requestFullscreen/);
  assert.match(index, /setting-hide-twitch-timeline/);
  assert.match(index, /setting-show-chat/);
  assert.match(index, /id="watch-layout" class="watch-layout"/);
  assert.match(index, /id="chat-toggle"/);
  assert.doesNotMatch(index, /SYNCED/);
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  assert.match(app, /spoilerWord\.setAttribute\('aria-pressed', String\(!revealed\)\)/);
  assert.doesNotMatch(index, /Click to reveal/);
  assert.match(styles, /\.spoiler-word > span \{[^}]+filter: blur\(14px\)[^}]+transition: filter 240ms ease/);
  assert.match(styles, /assets\/noise\.svg/);
  assert.match(styles, /body \{[^}]+background: url\("\/assets\/halftone\.svg"\) repeat, var\(--bg\) url\("\/assets\/noise\.svg"\) repeat/);
  assert.match(styles, /assets\/halftone\.svg/);
  assert.match(styles, /\.library-heading::after \{[^}]+background: #455047/);
  assert.doesNotMatch(styles, /\.library-summary::before/);
  assert.doesNotMatch(styles, /\.library-title::before/);
  assert.match(styles, /\.library-summary \{[^}]+transform: translateY\(clamp\(12px, 2vw, 28px\)\)/);
  assert.match(styles, /twitch-timeline-hidden #media-player iframe[^}]+top: -90px[^}]+height: calc\(100% \+ 180px\)/);
  assert.match(styles, /watch-layout:fullscreen \.chat-panel/);
  assert.match(styles, /watch-layout:fullscreen \.player-controls \{ display: none; \}/);
});

test('website library supports source and automatically derived team filters', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  assert.match(index, /id="kind-filters"/);
  assert.match(index, /data-kind="match"/);
  assert.match(index, /data-kind="watch-party"/);
  assert.match(index, /id="team-filters"/);
  assert.match(app, /function matchup\(entry\)/);
  assert.match(app, /state\.teamFilter/);
  assert.match(app, /Official matches/);
  assert.match(app, /Watch parties/);
  assert.match(app, /vod-card/);
  assert.doesNotMatch(index, /class="hero"/);
  assert.doesNotMatch(app, /team-mark/);
  assert.doesNotMatch(index, /video-search|video-url|Open a VOD/);
});

test('website groups broadcasts by match and keeps resume state spoiler safe', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const app = readFileSync(resolve(__dirname, '../site/app.js'), 'utf8');
  const vercel = readFileSync(resolve(__dirname, '../site/vercel.json'), 'utf8');
  assert.match(app, /function matchKey\(entry\)/);
  assert.match(app, /function groupedMatches\(\)/);
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

test('website keeps settings visible in the sticky header', () => {
  const index = readFileSync(resolve(__dirname, '../site/index.html'), 'utf8');
  const styles = readFileSync(resolve(__dirname, '../site/styles.css'), 'utf8');
  const header = index.match(/<header[\s\S]*?<\/header>/)?.[0] || '';
  const footer = index.match(/<footer[\s\S]*?<\/footer>/)?.[0] || '';
  assert.match(header, /id="settings-button" class="header-settings"/);
  assert.doesNotMatch(footer, /settings-button/);
  assert.match(styles, /\.header-settings/);
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
  assert.doesNotMatch(header, /<svg/);
});

test('website avoids the prohibited design and copy patterns', () => {
  const source = ['index.html', 'privacy.html', 'terms.html', 'styles.css', 'app.js']
    .map(file => readFileSync(resolve(__dirname, '../site/' + file), 'utf8')).join('\n');
  for (const pattern of [/\u2014/u, /made with ai/i, /purple/i, /border-radius:\s*999/i,
    /animation\s*:/i, /@keyframes/i, /cursor\s*animation/i, /fake\s+(review|metric|counter)/i]) {
    assert.doesNotMatch(source, pattern);
  }
});
