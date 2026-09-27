(() => {
  'use strict';

  function validate(value) {
    if (!value || value.schemaVersion !== 1 || typeof value.videoId !== 'string' || !/^[A-Za-z0-9_-]{11}$/.test(value.videoId)) {
      throw new Error('This index needs a valid matching YouTube video ID and schema version 1.');
    }
    if (!Array.isArray(value.rounds) || !value.rounds.length || value.rounds.length > 5000) {
      throw new Error('The index must contain between 1 and 5000 rounds.');
    }
    const seen = new Set();
    let last = -1;
    const rounds = value.rounds.map(round => {
      if (!round || !Number.isInteger(round.map) || round.map < 1 || round.map > 99 ||
          !Number.isInteger(round.round) || round.round < 1 || round.round > 100 ||
          !Number.isFinite(round.start) || round.start < 0 || round.start > 172800 || round.start <= last ||
          seen.has(round.map + ':' + round.round)) {
        throw new Error('Round timestamps must be unique, valid and in chronological order.');
      }
      last = round.start;
      seen.add(round.map + ':' + round.round);
      return {map: round.map, round: round.round, start: round.start, verified: round.verified === true};
    });
    return {schemaVersion: 1, videoId: value.videoId, label: String(value.label || 'Indexed VOD').slice(0, 100),
            leadSeconds: 5, rounds};
  }

  function destination(index, time, direction) {
    let current = -1;
    for (let position = 0; position < index.rounds.length; position++) {
      if (index.rounds[position].start - index.leadSeconds <= time + 1) current = position;
      else break;
    }
    const target = index.rounds[current + direction];
    return target ? Math.max(0, target.start - index.leadSeconds) : null;
  }

  globalThis.VodlockIndex = Object.freeze({validate, destination});
})();
