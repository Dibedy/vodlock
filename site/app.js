(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const defaultSettings = {hideTwitchTimeline: true, hideMetadata: false, hideRoundStatus: false, showChat: true, showChatTimestamps: false, endScreenProtection: true};
  const settingIds = {
    hideTwitchTimeline: 'setting-hide-twitch-timeline',
    hideMetadata: 'setting-hide-metadata',
    hideRoundStatus: 'setting-hide-round-status',
    showChat: 'setting-show-chat',
    showChatTimestamps: 'setting-show-chat-timestamps',
    endScreenProtection: 'setting-end-screen-protection'
  };
  const teamLogos = {
    GE: '/assets/teams/ge.png',
    VIT: '/assets/teams/vit.png',
    '100T': '/assets/teams/100t.png',
    T1: '/assets/teams/t1.png',
    NS: '/assets/teams/ns.png',
    NRG: '/assets/teams/nrg.png',
    JDG: '/assets/teams/jdg.png',
    FUT: '/assets/teams/fut.png',
    LOUD: '/assets/teams/loud.png',
    EDG: '/assets/teams/edg.png'
  };
  const state = {catalog: [], entry: null, index: null, chat: [], chatPosition: null, player: null, ready: false, started: false, playing: false, theater: false, resume: readResume(), resumeTime: null, lastResumeWrite: 0, toastTimer: null, statusTimer: null, kindFilter: 'all', teamFilter: 'all'};
  let settings = readSettings();

  function readResume() {
    try {
      const value = JSON.parse(localStorage.getItem('spoilless-resume') || 'null');
      return value && typeof value.key === 'string' && Number.isFinite(value.time) && value.time >= 0 ? value : null;
    } catch {
      return null;
    }
  }

  function saveResume(force = false) {
    if (!state.entry || !state.ready) return;
    const now = Date.now();
    if (!force && now - state.lastResumeWrite < 4000) return;
    state.lastResumeWrite = now;
    state.resume = {key: entryKey(state.entry), time: Math.max(0, currentTime()), updatedAt: now};
    try { localStorage.setItem('spoilless-resume', JSON.stringify(state.resume)); } catch {}
  }

  function readSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem('vodlock-settings') || '{}');
      return Object.fromEntries(Object.keys(defaultSettings).map(key => [key, typeof saved[key] === 'boolean' ? saved[key] : defaultSettings[key]]));
    } catch {
      return {...defaultSettings};
    }
  }

  function saveSettings() {
    try { localStorage.setItem('vodlock-settings', JSON.stringify(settings)); } catch {}
  }

  function formatTime(seconds) {
    const value = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor(value % 3600 / 60);
    const remainder = value % 60;
    return (hours ? hours + ':' + String(minutes).padStart(2, '0') : minutes) + ':' + String(remainder).padStart(2, '0');
  }

  function applySettings() {
    for (const [key, identifier] of Object.entries(settingIds)) $(identifier).checked = settings[key];
    const twitchTimelineHidden = state.entry?.provider === 'twitch' && settings.hideTwitchTimeline;
    $('player-shell').classList.toggle('twitch-timeline-hidden', twitchTimelineHidden);
    $('watch-metadata').hidden = settings.hideMetadata;
    $('timeline-status').textContent = state.entry?.provider === 'twitch'
      ? twitchTimelineHidden ? 'Timeline hidden' : 'Twitch controls visible'
      : 'Timeline hidden';
    renderChat(true);
    updateStatus();
  }

  function toast(message) {
    clearTimeout(state.toastTimer);
    $('toast').textContent = message;
    $('toast').hidden = false;
    state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500);
  }

  function destroyPlayer() {
    if (state.player?.destroy) state.player.destroy();
    state.player = null;
    state.ready = false;
    state.playing = false;
  }

  function renderChat(force = false) {
    const available = state.entry?.provider === 'twitch' && state.chat.length > 0;
    const visible = available && settings.showChat;
    $('chat-panel').hidden = !visible;
    $('chat-toggle').hidden = !available;
    $('chat-toggle').textContent = visible ? 'Chat off' : 'Chat on';
    $('chat-toggle').setAttribute('aria-label', visible ? 'Turn chat off' : 'Turn chat on');
    document.querySelector('.watch-layout').classList.toggle('chat-visible', visible);
    syncFullscreenButton();
    if (!visible) return;
    const now = currentTime();
    const position = Math.floor(now);
    if (!force && state.chatPosition === position) return;
    state.chatPosition = position;
    const messages = state.chat.filter(message => message.time <= now && message.time >= now - 90).slice(-120);
    const host = $('chat-messages');
    host.replaceChildren();
    if (!messages.length) {
      const empty = document.createElement('p');
      empty.className = 'chat-empty';
      empty.textContent = 'No archived messages at this point in the VOD.';
      host.append(empty);
      return;
    }
    for (const message of messages) {
      const row = document.createElement('p');
      row.className = 'chat-message';
      if (settings.showChatTimestamps) {
        const time = document.createElement('time');
        time.textContent = formatTime(message.time);
        row.append(time);
      }
      const user = document.createElement('strong');
      user.textContent = message.user + ':';
      if (message.color) user.style.color = message.color;
      row.append(user);
      for (const [text, emote] of message.fragments) {
        if (emote) {
          const image = document.createElement('img');
          image.src = 'https://static-cdn.jtvnw.net/emoticons/v2/' + emote + '/default/dark/1.0';
          image.alt = text;
          image.loading = 'lazy';
          row.append(image);
        } else {
          row.append(document.createTextNode(text));
        }
      }
      host.append(row);
    }
    host.scrollTop = host.scrollHeight;
  }

  async function loadChat(entry) {
    state.chat = [];
    state.chatPosition = null;
    if (entry.provider !== 'twitch' || typeof entry.chat !== 'string' || !/^\/chats\/twitch-[0-9]{6,20}\.json$/.test(entry.chat)) return;
    const response = await fetch(entry.chat, {cache: 'no-store'});
    if (!response.ok) return;
    state.chat = VodlockSite.validateChat(await response.json(), entry.sourceId);
  }

  function resetPlayerHost() {
    const current = $('media-player');
    if (current && current.tagName === 'DIV') {
      current.replaceChildren();
      return;
    }
    if (current) current.remove();
    const host = document.createElement('div');
    host.id = 'media-player';
    $('player-shell').prepend(host);
  }

  function entryKey(entry) {
    return VodlockSite.sourceKey(entry.provider, entry.sourceId);
  }

  function matchup(entry) {
    const candidates = [entry.title, entry.event.split('|')[0]];
    for (const candidate of candidates) {
      const parts = candidate.split(/\s+vs\.?\s+/i);
      if (parts.length !== 2) continue;
      const teams = parts.map(part => part.replace(/^[^A-Za-z0-9]+|[^A-Za-z0-9]+$/g, '').trim());
      if (teams.every(Boolean)) return teams;
    }
    return [];
  }

  function entryKind(entry) {
    if (entry.kind === 'match' || entry.kind === 'watch-party') return entry.kind;
    return entry.label === 'Full match' || matchup({title: entry.title, event: ''}).length === 2 ? 'match' : 'watch-party';
  }

  function displayEvent(entry) {
    return entry.event.split('|').map(part => part.trim()).filter(part => part && !part.startsWith('!'))
      .map(part => part.replace(/\s*#\S+/g, '').trim()).filter(Boolean).join(' · ');
  }

  function matchKey(entry) {
    const teams = matchup(entry);
    return teams.length === 2 ? teams.map(team => team.toUpperCase()).sort().join(':') : entry.title.toUpperCase();
  }

  function creatorName(entry) {
    return entry.title.replace(/^EG\s+/i, '').trim() || 'Creator';
  }

  function sourceName(entry) {
    if (entryKind(entry) === 'watch-party') return creatorName(entry) + ' watch party';
    return 'Official broadcast';
  }

  function groupedMatches() {
    const matches = new Map();
    for (const entry of state.catalog) {
      const key = matchKey(entry);
      if (!matches.has(key)) matches.set(key, []);
      matches.get(key).push(entry);
    }
    return [...matches.values()].map(entries => {
      const primary = entries.find(entry => entryKind(entry) === 'match') || entries[0];
      return {primary, teams: matchup(primary), entries: entries.slice().sort((first, second) => {
        const kindOrder = Number(entryKind(first) === 'watch-party') - Number(entryKind(second) === 'watch-party');
        if (kindOrder) return kindOrder;
        return Number(first.provider === 'twitch') - Number(second.provider === 'twitch');
      })};
    });
  }

  function teamIdentity(team) {
    const identity = document.createElement('span');
    identity.className = 'team-identity';
    const crest = document.createElement('span');
    crest.className = 'team-crest';
    const fallback = document.createElement('b');
    fallback.textContent = team;
    crest.append(fallback);
    if (teamLogos[team]) {
      const image = document.createElement('img');
      image.alt = '';
      image.addEventListener('load', () => crest.classList.add('has-image'));
      image.src = teamLogos[team];
      crest.prepend(image);
    }
    const name = document.createElement('strong');
    name.textContent = team;
    identity.append(crest, name);
    return identity;
  }

  function renderFilters() {
    const teams = [...new Set(state.catalog.flatMap(matchup))].sort((a, b) => a.localeCompare(b));
    if (state.teamFilter !== 'all' && !teams.includes(state.teamFilter)) state.teamFilter = 'all';
    const host = $('team-filters');
    host.replaceChildren();
    const all = document.createElement('button');
    all.type = 'button';
    all.dataset.team = 'all';
    all.textContent = 'All teams';
    host.append(all);
    for (const team of teams) {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.team = team;
      button.textContent = team;
      host.append(button);
    }
    for (const button of host.querySelectorAll('button')) {
      const selected = button.dataset.team === state.teamFilter;
      button.setAttribute('aria-pressed', String(selected));
      button.addEventListener('click', () => {
        state.teamFilter = button.dataset.team;
        renderFilters();
        renderCatalog();
      });
    }
    for (const button of $('kind-filters').querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.kind === state.kindFilter));
    }
  }

  function renderCard(match, number) {
    const {primary, teams, entries} = match;
    const card = document.createElement('article');
    card.className = 'vod-card';
    const heading = document.createElement('h3');
    heading.className = 'visually-hidden';
    heading.id = 'match-title-' + number;
    heading.textContent = teams.length === 2 ? teams.join(' vs ') : primary.title;
    card.setAttribute('aria-labelledby', heading.id);
    const index = document.createElement('span');
    index.className = 'match-number';
    index.textContent = String(number).padStart(2, '0');
    const matchTeams = document.createElement('div');
    matchTeams.className = 'match-teams';
    if (teams.length === 2) {
      const versus = document.createElement('span');
      versus.className = 'versus';
      versus.textContent = 'vs';
      matchTeams.append(teamIdentity(teams[0]), versus, teamIdentity(teams[1]));
    } else {
      const title = document.createElement('h3');
      title.textContent = primary.title;
      matchTeams.append(title);
    }
    const details = document.createElement('div');
    details.className = 'match-details';
    const event = document.createElement('p');
    event.textContent = displayEvent(primary).replace(/^.*?\s+vs\.?\s+.*?\s+·\s+/i, '');
    const availability = document.createElement('span');
    availability.textContent = entries.length + (entries.length === 1 ? ' broadcast' : ' broadcasts');
    details.append(event, availability);
    const sourceGroups = {match: 'Official matches', 'watch-party': 'Watch parties'};
    const sources = document.createElement('div');
    sources.className = 'source-list';
    sources.setAttribute('aria-label', 'Choose from ' + [...new Set(entries.map(entry => sourceGroups[entryKind(entry)]))].join(' and '));
    for (const entry of entries) {
      const watch = document.createElement('button');
      watch.type = 'button';
      const label = document.createElement('strong');
      label.textContent = sourceName(entry);
      const provider = document.createElement('span');
      provider.textContent = entry.provider === 'youtube' ? 'YouTube' : entry.chat ? 'Twitch · archived chat' : 'Twitch';
      const arrow = document.createElement('b');
      arrow.textContent = '→';
      watch.append(label, provider, arrow);
      watch.addEventListener('click', () => openVideo(entryKey(entry)));
      sources.append(watch);
    }
    card.append(heading, index, matchTeams, details, sources);
    return card;
  }

  function renderCatalog() {
    const catalog = $('catalog');
    catalog.replaceChildren();
    const visible = groupedMatches().filter(match =>
      (state.kindFilter === 'all' || match.entries.some(entry => entryKind(entry) === state.kindFilter)) &&
      (state.teamFilter === 'all' || match.teams.includes(state.teamFilter)));
    for (const [position, match] of visible.entries()) catalog.append(renderCard(match, position + 1));
    if (!visible.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-library';
      empty.textContent = 'No matches are available for these filters.';
      catalog.append(empty);
    }
  }

  function renderContinue() {
    const entry = state.resume && state.catalog.find(candidate => entryKey(candidate) === state.resume.key);
    $('continue-panel').hidden = !entry;
    if (!entry) return;
    $('continue-title').textContent = matchup(entry).join(' vs ') || entry.title;
    $('continue-source').textContent = sourceName(entry);
    $('continue-button').onclick = () => openVideo(entryKey(entry), true, state.resume.time);
  }

  async function loadCatalog() {
    const response = await fetch('/catalog.json', {cache: 'no-store'});
    if (!response.ok) throw new Error('The VOD catalog could not be loaded.');
    const value = await response.json();
    if (![1, 2].includes(value?.version) || !Array.isArray(value.videos)) throw new Error('The VOD catalog is invalid.');
    state.catalog = value.videos.map(entry => {
      const provider = entry?.provider || 'youtube';
      const sourceId = entry?.sourceId || entry?.videoId;
      return {...entry, provider, sourceId};
    }).filter(entry => {
      const parsed = VodlockSite.mediaSource(VodlockSite.sourceKey(entry.provider, entry.sourceId));
      return parsed && typeof entry.index === 'string' && /^\/indexes\/[A-Za-z0-9_-]+\.json$/.test(entry.index) &&
        typeof entry.title === 'string' && typeof entry.event === 'string';
    });
    renderFilters();
    renderCatalog();
    renderContinue();
  }

  function showLibrary(updateHistory = true) {
    saveResume(true);
    clearInterval(state.statusTimer);
    state.statusTimer = null;
    destroyPlayer();
    state.started = false;
    state.entry = null;
    state.index = null;
    state.resumeTime = null;
    state.chat = [];
    state.chatPosition = null;
    setPlaybackControlsDisabled(true);
    resetPlayerHost();
    $('player-view').hidden = true;
    $('library-view').hidden = false;
    $('end-shield').hidden = true;
    $('start-gate').hidden = false;
    $('start-watching').disabled = false;
    $('start-watching').textContent = 'Start watching';
    $('start-gate-title').textContent = 'Start at round one';
    $('start-gate-copy').textContent = 'The original video loads only after you choose to start.';
    state.theater = false;
    document.body.classList.remove('theater-mode');
    $('player-view').classList.remove('theater-active');
    applySettings();
    renderContinue();
    if (updateHistory) history.pushState({}, '', location.pathname);
    document.title = 'SPOILLESS | VALORANT VODs';
    window.scrollTo({top: 0});
  }

  async function openVideo(key, updateHistory = true, resumeTime = null) {
    const source = VodlockSite.mediaSource(key);
    const entry = source && state.catalog.find(candidate => entryKey(candidate) === VodlockSite.sourceKey(source.provider, source.sourceId));
    if (!entry) {
      toast('This VOD has not been processed yet.');
      return;
    }
    const response = await fetch(entry.index, {cache: 'no-store'});
    if (!response.ok) throw new Error('This round index is temporarily unavailable.');
    const index = VodlockSite.validateIndex(await response.json());
    if (index.provider !== entry.provider || index.sourceId !== entry.sourceId) throw new Error('The catalog and round index do not match.');
    await loadChat(entry);
    setPlaybackControlsDisabled(true);
    state.entry = entry;
    state.index = index;
    state.resumeTime = Number.isFinite(resumeTime) && resumeTime >= 0 ? resumeTime : null;
    destroyPlayer();
    state.started = false;
    resetPlayerHost();
    const teams = matchup(entry);
    $('watch-event').textContent = displayEvent(entry);
    $('watch-title').textContent = entry.title;
    $('watch-team-one').textContent = teams[0] || entry.title;
    $('watch-team-two').textContent = teams[1] || '';
    for (const [identifier, team] of [['watch-team-one-logo', teams[0]], ['watch-team-two-logo', teams[1]]]) {
      const image = $(identifier);
      image.hidden = !teamLogos[team];
      image.src = teamLogos[team] || '';
    }
    $('watch-source').textContent = sourceName(entry) + ' · ' + (entry.provider === 'youtube' ? 'YouTube' : 'Twitch');
    $('library-view').hidden = true;
    $('player-view').hidden = false;
    $('start-gate').hidden = false;
    $('end-shield').hidden = true;
    $('start-watching').disabled = false;
    $('start-gate-title').textContent = state.resumeTime === null ? 'Start at round one' : 'Continue watching';
    $('start-gate-copy').textContent = state.resumeTime === null ? 'The original video loads only after you choose to start.' : 'The player will return to your saved position.';
    $('start-watching').textContent = state.resumeTime === null ? 'Start watching' : 'Continue watching';
    applySettings();
    if (updateHistory) history.pushState({source: entryKey(entry)}, '', '?v=' + encodeURIComponent(entryKey(entry)));
    document.title = entry.title + ' | SPOILLESS';
    window.scrollTo({top: 0});
  }

  function loadYouTubeApi() {
    if (window.YT?.Player) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('YouTube took too long to respond.')), 15000);
      const previous = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = () => {
        clearTimeout(timeout);
        if (previous) previous();
        resolve();
      };
      if (!document.querySelector('script[src="https://www.youtube.com/iframe_api"]')) {
        const script = document.createElement('script');
        script.src = 'https://www.youtube.com/iframe_api';
        script.onerror = () => reject(new Error('The YouTube player could not be loaded.'));
        document.head.append(script);
      }
    });
  }

  function loadTwitchApi() {
    if (window.Twitch?.Player) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Twitch took too long to respond.')), 15000);
      const existing = document.querySelector('script[src="https://player.twitch.tv/js/embed/v1.js"]');
      const script = existing || document.createElement('script');
      script.addEventListener('load', () => {
        clearTimeout(timeout);
        window.Twitch?.Player ? resolve() : reject(new Error('The Twitch player could not be loaded.'));
      }, {once: true});
      script.addEventListener('error', () => reject(new Error('The Twitch player could not be loaded.')), {once: true});
      if (!existing) {
        script.src = 'https://player.twitch.tv/js/embed/v1.js';
        document.head.append(script);
      }
    });
  }

  function playerReady() {
    state.ready = true;
    setVolume(Number($('volume').value));
    const destination = state.resumeTime ?? Math.max(0, state.index.rounds[0].start - state.index.leadSeconds);
    state.resumeTime = null;
    seekPlayer(destination);
    playPlayer();
    setPlaybackControlsDisabled(false);
    const revealDelay = state.entry.provider === 'twitch' && settings.hideTwitchTimeline ? 1400 : 0;
    setTimeout(() => {
      if (!state.ready) return;
      $('start-gate').hidden = true;
      $('start-watching').textContent = 'Start watching';
    }, revealDelay);
    updateStatus();
    clearInterval(state.statusTimer);
    state.statusTimer = setInterval(updateStatus, 500);
  }

  function createYouTubePlayer() {
    state.player = new YT.Player('media-player', {
      host: 'https://www.youtube-nocookie.com',
      videoId: state.entry.sourceId,
      playerVars: {autoplay: 0, controls: 0, disablekb: 1, fs: 0, iv_load_policy: 3, playsinline: 1, rel: 0},
      events: {
        onReady: playerReady,
        onStateChange: event => {
          setPlaying(event.data === YT.PlayerState.PLAYING);
          if (event.data === YT.PlayerState.ENDED) setTimeout(finishWatching, 0);
        },
        onError: () => toast('YouTube could not play this broadcast here.')
      }
    });
  }

  function createTwitchPlayer() {
    $('player-shell').classList.toggle('twitch-timeline-hidden', settings.hideTwitchTimeline);
    state.player = new Twitch.Player('media-player', {
      video: 'v' + state.entry.sourceId,
      parent: [location.hostname],
      width: '100%',
      height: '100%',
      autoplay: false
    });
    state.player.addEventListener(Twitch.Player.READY, playerReady);
    state.player.addEventListener(Twitch.Player.PLAY, () => setPlaying(true));
    state.player.addEventListener(Twitch.Player.PAUSE, () => setPlaying(false));
    state.player.addEventListener(Twitch.Player.ENDED, () => setTimeout(finishWatching, 0));
  }

  async function createPlayer() {
    try {
      await (state.entry.provider === 'youtube' ? loadYouTubeApi() : loadTwitchApi());
      if (!state.entry || !state.index) return;
      destroyPlayer();
      resetPlayerHost();
      if (state.entry.provider === 'youtube') createYouTubePlayer();
      else createTwitchPlayer();
    } catch (error) {
      state.started = false;
      $('start-gate').hidden = false;
      $('start-watching').disabled = false;
      $('start-watching').textContent = 'Try again';
      toast(error.message);
    }
  }

  function finishWatching() {
    if (!settings.endScreenProtection) {
      setPlaying(false);
      return;
    }
    clearInterval(state.statusTimer);
    state.statusTimer = null;
    destroyPlayer();
    state.started = false;
    setPlaybackControlsDisabled(true);
    resetPlayerHost();
    $('end-shield').hidden = false;
    if (state.resume?.key === entryKey(state.entry)) {
      state.resume = null;
      try { localStorage.removeItem('spoilless-resume'); } catch {}
    }
  }

  function currentTime() {
    return state.ready && state.player?.getCurrentTime ? state.player.getCurrentTime() : 0;
  }

  function setPlaying(playing) {
    state.playing = playing;
    $('play-button').textContent = playing ? 'Pause' : 'Play';
    $('play-button').setAttribute('aria-label', playing ? 'Pause' : 'Play');
  }

  function playPlayer() {
    if (state.entry.provider === 'youtube') state.player.playVideo();
    else state.player.play();
  }

  function pausePlayer() {
    if (state.entry.provider === 'youtube') state.player.pauseVideo();
    else state.player.pause();
  }

  function seekPlayer(destination) {
    if (state.entry.provider === 'youtube') state.player.seekTo(destination, true);
    else state.player.seek(destination);
  }

  function isMuted() {
    return state.entry.provider === 'youtube' ? state.player.isMuted() : state.player.getMuted();
  }

  function setMuted(muted) {
    if (state.entry.provider === 'youtube') muted ? state.player.mute() : state.player.unMute();
    else state.player.setMuted(muted);
  }

  function setVolume(value) {
    state.player.setVolume(state.entry.provider === 'youtube' ? value : value / 100);
  }

  function setPlaybackControlsDisabled(disabled) {
    for (const identifier of ['play-button', 'back-ten', 'skip-replay', 'mute-button', 'volume',
      'previous-round', 'next-round', 'previous-map', 'next-map', 'restart-round']) $(identifier).disabled = disabled;
  }

  function seek(destination, message) {
    if (!state.ready || destination === null) {
      if (destination === null) toast('No indexed item in that direction.');
      return;
    }
    $('end-shield').hidden = true;
    const resume = state.playing;
    seekPlayer(destination);
    if (resume) setTimeout(() => state.player && playPlayer(), 200);
    updateStatus();
    if (message) toast(message);
  }

  function updateStatus() {
    if (!state.index) return;
    const position = VodlockSite.position(state.index, currentTime());
    const round = state.index.rounds[Math.max(position, 0)];
    if (settings.hideRoundStatus) {
      $('current-round').textContent = 'Position hidden · Navigation active';
    } else if (position < 0) {
      $('current-round').textContent = 'Ready to begin';
    } else {
      $('current-round').textContent = 'MAP ' + round.map + ' · ROUND ' + round.round;
    }
    saveResume();
    renderChat();
  }

  function navigateRound(direction) {
    seek(VodlockSite.roundDestination(state.index, currentTime(), direction), direction > 0 ? 'Next round' : 'Previous round');
  }

  function navigateMap(direction) {
    seek(VodlockSite.mapDestination(state.index, currentTime(), direction), direction > 0 ? 'Next map' : 'Previous map');
  }

  function togglePlayback() {
    if (!state.ready) return;
    if (state.playing) pausePlayer();
    else playPlayer();
  }

  function startWatching() {
    if (state.started) return;
    state.started = true;
    $('start-watching').disabled = true;
    $('start-watching').textContent = 'Loading ' + (state.entry.provider === 'youtube' ? 'YouTube' : 'Twitch') + '...';
    createPlayer();
  }

  function toggleMute() {
    if (!state.ready) return;
    const muted = !isMuted();
    setMuted(muted);
    $('mute-button').textContent = muted ? 'Muted' : 'Sound';
    $('mute-button').setAttribute('aria-label', muted ? 'Unmute' : 'Mute');
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else if ($('watch-layout').requestFullscreen) $('watch-layout').requestFullscreen();
    else toast('Full screen is not available in this browser.');
  }

  function toggleTheater() {
    state.theater = !state.theater;
    document.body.classList.toggle('theater-mode', state.theater);
    $('player-view').classList.toggle('theater-active', state.theater);
    $('theater-button').textContent = state.theater ? 'Exit theater' : 'Theater';
    $('theater-button').setAttribute('aria-label', state.theater ? 'Exit theater mode' : 'Enter theater mode');
  }

  function syncFullscreenButton() {
    const chatVisible = !$('chat-panel').hidden;
    const fullscreen = document.fullscreenElement === $('watch-layout');
    $('fullscreen-button').textContent = fullscreen ? 'Exit full screen' : 'Full screen';
    $('fullscreen-button').setAttribute('aria-label', fullscreen ? 'Exit fullscreen' : chatVisible ? 'Enter fullscreen with chat' : 'Enter fullscreen');
  }

  $('settings-button').addEventListener('click', () => $('settings-dialog').showModal());
  $('settings-close').addEventListener('click', () => $('settings-dialog').close());
  $('settings-dialog').addEventListener('click', event => {
    if (event.target === $('settings-dialog')) $('settings-dialog').close();
  });
  for (const [key, identifier] of Object.entries(settingIds)) {
    $(identifier).addEventListener('change', event => {
      settings = {...settings, [key]: event.target.checked};
      saveSettings();
      applySettings();
    });
  }

  for (const button of $('kind-filters').querySelectorAll('button')) {
    button.addEventListener('click', () => {
      state.kindFilter = button.dataset.kind;
      renderFilters();
      renderCatalog();
    });
  }
  $('home-button').addEventListener('click', () => showLibrary());
  $('back-button').addEventListener('click', () => showLibrary());
  $('return-library').addEventListener('click', () => showLibrary());
  $('start-watching').addEventListener('click', startWatching);
  $('play-button').addEventListener('click', togglePlayback);
  $('back-ten').addEventListener('click', () => seek(Math.max(0, currentTime() - 10)));
  $('skip-replay').addEventListener('click', () => seek(currentTime() + 10, 'Skipped 10 seconds'));
  $('mute-button').addEventListener('click', toggleMute);
  $('chat-toggle').addEventListener('click', () => {
    settings = {...settings, showChat: !settings.showChat};
    saveSettings();
    applySettings();
  });
  $('volume').addEventListener('input', event => {
    if (!state.ready) return;
    setVolume(Number(event.target.value));
    if (Number(event.target.value)) setMuted(false);
  });
  $('fullscreen-button').addEventListener('click', toggleFullscreen);
  $('theater-button').addEventListener('click', toggleTheater);
  document.addEventListener('fullscreenchange', syncFullscreenButton);
  addEventListener('pagehide', () => saveResume(true));
  $('previous-round').addEventListener('click', () => navigateRound(-1));
  $('next-round').addEventListener('click', () => navigateRound(1));
  $('previous-map').addEventListener('click', () => navigateMap(-1));
  $('next-map').addEventListener('click', () => navigateMap(1));
  $('restart-round').addEventListener('click', () => {
    const position = VodlockSite.position(state.index, currentTime());
    const round = state.index.rounds[Math.max(0, position)];
    seek(Math.max(0, round.start - state.index.leadSeconds), 'Round restarted');
  });

  document.addEventListener('keydown', event => {
    if ($('player-view').hidden || ['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) return;
    if (!state.started) {
      if (event.key === ' ') {
        event.preventDefault();
        startWatching();
      }
      return;
    }
    const actions = {
      ' ': togglePlayback,
      ArrowUp: () => navigateRound(-1),
      ArrowDown: () => navigateRound(1),
      ArrowLeft: () => seek(Math.max(0, currentTime() - 10)),
      ArrowRight: () => seek(currentTime() + 10, 'Skipped 10 seconds'),
      f: toggleFullscreen,
      F: toggleFullscreen,
      t: toggleTheater,
      T: toggleTheater,
      m: toggleMute,
      M: toggleMute
    };
    if (!actions[event.key]) return;
    event.preventDefault();
    actions[event.key]();
  });

  addEventListener('popstate', () => {
    const source = new URL(location.href).searchParams.get('v');
    if (source) openVideo(source, false).catch(error => toast(error.message));
    else showLibrary(false);
  });

  applySettings();
  loadCatalog().then(() => {
    const source = VodlockSite.mediaSource(new URL(location.href).searchParams.get('v'));
    if (source) return openVideo(VodlockSite.sourceKey(source.provider, source.sourceId), false);
  }).catch(error => {
    $('catalog').replaceChildren();
    const message = document.createElement('p');
    message.className = 'empty-library';
    message.textContent = error.message;
    $('catalog').append(message);
  });
})();
