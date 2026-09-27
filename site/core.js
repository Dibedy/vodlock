(() => {
  'use strict';

  function youtubeId(value) {
    const input = String(value || '').trim();
    if (/^[A-Za-z0-9_-]{11}$/.test(input)) return input;
    let url;
    try { url = new URL(input); } catch { return null; }
    if (url.protocol !== 'https:') return null;
    if (url.hostname === 'youtu.be') {
      const identifier = url.pathname.split('/').filter(Boolean)[0];
      return /^[A-Za-z0-9_-]{11}$/.test(identifier || '') ? identifier : null;
    }
    if (!['youtube.com', 'www.youtube.com', 'm.youtube.com'].includes(url.hostname)) return null;
    const identifier = url.pathname === '/watch' ? url.searchParams.get('v') : url.pathname.match(/^\/(?:embed|shorts)\/([A-Za-z0-9_-]{11})/)?.[1];
    return /^[A-Za-z0-9_-]{11}$/.test(identifier || '') ? identifier : null;
  }

  function twitchId(value) {
    const input = String(value || '').trim();
    if (/^[0-9]{6,20}$/.test(input)) return input;
    let url;
    try { url = new URL(input); } catch { return null; }
    if (url.protocol !== 'https:' || !['twitch.tv', 'www.twitch.tv'].includes(url.hostname)) return null;
    return url.pathname.match(/^\/videos\/([0-9]{6,20})\/?$/)?.[1] || null;
  }

  function mediaSource(value) {
    const input = String(value || '').trim();
    const keyed = input.match(/^(youtube|twitch):(.+)$/);
    if (keyed) {
      const sourceId = keyed[1] === 'youtube' ? youtubeId(keyed[2]) : twitchId(keyed[2]);
      return sourceId ? {provider: keyed[1], sourceId} : null;
    }
    const youtube = youtubeId(input);
    if (youtube) return {provider: 'youtube', sourceId: youtube};
    const twitch = twitchId(input);
    return twitch ? {provider: 'twitch', sourceId: twitch} : null;
  }

  function sourceKey(provider, sourceId) {
    return provider + ':' + sourceId;
  }

  function validateIndex(value) {
    const provider = value?.schemaVersion === 1 ? 'youtube' : value?.provider;
    const sourceId = value?.schemaVersion === 1 ? value?.videoId : value?.sourceId;
    const validSource = provider === 'youtube' ? youtubeId(sourceId) : provider === 'twitch' ? twitchId(sourceId) : null;
    if (!value || ![1, 2].includes(value.schemaVersion) || !validSource || validSource !== sourceId) {
      throw new Error('The hosted index has an invalid media source or schema version.');
    }
    if (!Array.isArray(value.rounds) || !value.rounds.length || value.rounds.length > 5000) {
      throw new Error('The hosted index has no usable rounds.');
    }
    const seen = new Set();
    let previousStart = -1;
    const rounds = value.rounds.map(round => {
      const identity = round?.map + ':' + round?.round;
      if (!round || !Number.isInteger(round.map) || round.map < 1 || round.map > 99 ||
          !Number.isInteger(round.round) || round.round < 1 || round.round > 100 ||
          !Number.isFinite(round.start) || round.start < 0 || round.start > 172800 ||
          round.start <= previousStart || seen.has(identity)) {
        throw new Error('The hosted round timestamps are invalid.');
      }
      previousStart = round.start;
      seen.add(identity);
      return {map: round.map, round: round.round, start: round.start};
    });
    return {schemaVersion: 2, provider, sourceId, label: String(value.label || 'Indexed VOD').slice(0, 100), leadSeconds: 5, rounds};
  }

  function position(index, time) {
    let current = -1;
    for (let candidate = 0; candidate < index.rounds.length; candidate++) {
      if (index.rounds[candidate].start - index.leadSeconds <= time + 1) current = candidate;
      else break;
    }
    return current;
  }

  function roundDestination(index, time, direction) {
    const target = index.rounds[position(index, time) + direction];
    return target ? Math.max(0, target.start - index.leadSeconds) : null;
  }

  function mapDestination(index, time, direction) {
    const currentPosition = position(index, time);
    const currentMap = index.rounds[Math.max(0, currentPosition)]?.map;
    if (direction > 0) {
      const target = index.rounds.find(round => round.map > currentMap);
      return target ? Math.max(0, target.start - index.leadSeconds) : null;
    }
    const earlierMaps = index.rounds.filter(round => round.map < currentMap);
    if (!earlierMaps.length) return null;
    const targetMap = earlierMaps[earlierMaps.length - 1].map;
    const target = index.rounds.find(round => round.map === targetMap);
    return Math.max(0, target.start - index.leadSeconds);
  }

  function validateChat(value, sourceId) {
    if (!value || value.v !== 1 || value.source !== sourceId || !Array.isArray(value.messages) || value.messages.length > 120000) {
      throw new Error('The archived VOD chat is invalid.');
    }
    let previousTime = -1;
    const messages = value.messages.map(message => {
      if (!message || typeof message.t !== 'number' || !Number.isFinite(message.t) || message.t < previousTime || message.t < 0 ||
          typeof message.u !== 'string' || !message.u || message.u.length > 40 ||
          typeof message.c !== 'string' || !/^(?:#[0-9A-Fa-f]{6})?$/.test(message.c) ||
          !Array.isArray(message.f) || !message.f.length || message.f.length > 50) {
        throw new Error('The archived VOD chat is invalid.');
      }
      const fragments = message.f.map(fragment => {
        if (!Array.isArray(fragment) || ![1, 2].includes(fragment.length) || typeof fragment[0] !== 'string' || fragment[0].length > 500 ||
            fragment.length === 2 && (typeof fragment[1] !== 'string' || !/^[0-9]{1,20}$/.test(fragment[1]))) {
          throw new Error('The archived VOD chat is invalid.');
        }
        return fragment.slice();
      });
      previousTime = message.t;
      return {time: message.t, user: message.u, color: message.c, fragments};
    });
    return Object.freeze(messages);
  }

  globalThis.VodlockSite = Object.freeze({videoId: youtubeId, youtubeId, twitchId, mediaSource, sourceKey, validateIndex, validateChat, position, roundDestination, mapDestination});
})();
