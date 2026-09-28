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
  const state = {catalog: [], entry: null, index: null, chat: [], chatPosition: null, player: null, ready: false, started: false, playing: false, toastTimer: null, statusTimer: null, kindFilter: 'all', teamFilter: 'all'};
  let settings = readSettings();

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

  function setSearchMessage(message, error = false) {
    $('search-message').textContent = message;
    $('search-message').classList.toggle('error', error);
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

  function renderCard(entry) {
    const kind = entryKind(entry);
    const teams = matchup(entry);
    const card = document.createElement('article');
    card.className = 'vod-row ' + kind;
    const source = document.createElement('span');
    source.className = 'source-label ' + entry.provider;
    source.textContent = entry.provider === 'youtube' ? 'YouTube' : 'Twitch';
    const body = document.createElement('div');
    body.className = 'row-body';
    const title = document.createElement('h3');
    if (teams.length === 2) {
      const first = document.createElement('strong');
      first.textContent = teams[0];
      const versus = document.createElement('span');
      versus.textContent = 'vs';
      const second = document.createElement('strong');
      second.textContent = teams[1];
      title.append(first, versus, second);
    } else {
      title.textContent = entry.title;
    }
    const event = document.createElement('p');
    const creator = kind === 'watch-party' ? entry.title.replace(/^EG\s+/i, '') + ' watch party · ' : '';
    let eventText = displayEvent(entry);
    if (kind === 'watch-party' && teams.length === 2) {
      const matchupPrefix = teams[0] + ' vs ' + teams[1] + ' · ';
      if (eventText.startsWith(matchupPrefix)) eventText = eventText.slice(matchupPrefix.length);
    }
    event.textContent = creator + eventText;
    body.append(title, event);
    const watch = document.createElement('button');
    watch.type = 'button';
    watch.textContent = entry.chat ? 'Watch + chat' : 'Watch';
    watch.addEventListener('click', () => openVideo(entryKey(entry)));
    card.append(source, body, watch);
    return card;
  }

  function renderCatalog() {
    const catalog = $('catalog');
    catalog.replaceChildren();
    const visible = state.catalog.filter(entry => (state.kindFilter === 'all' || entryKind(entry) === state.kindFilter) &&
      (state.teamFilter === 'all' || matchup(entry).includes(state.teamFilter)));
    for (const [kind, heading] of [['match', 'Official matches'], ['watch-party', 'Watch parties']]) {
      const entries = visible.filter(entry => entryKind(entry) === kind);
      if (!entries.length) continue;
      const group = document.createElement('section');
      group.className = 'catalog-group';
      const groupHeading = document.createElement('div');
      groupHeading.className = 'catalog-group-heading';
      const title = document.createElement('h3');
      title.textContent = heading;
      const detail = document.createElement('span');
      detail.textContent = kind === 'match' ? 'YouTube and official broadcasts' : 'FNS and Ohnepixel';
      groupHeading.append(title, detail);
      const grid = document.createElement('div');
      grid.className = 'catalog-grid';
      for (const entry of entries) grid.append(renderCard(entry));
      group.append(groupHeading, grid);
      catalog.append(group);
    }
    if (!visible.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-library';
      empty.textContent = 'No VODs match these filters.';
      catalog.append(empty);
    }
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
  }

  function showLibrary(updateHistory = true) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
    destroyPlayer();
    state.started = false;
    state.entry = null;
    state.index = null;
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
    applySettings();
    if (updateHistory) history.pushState({}, '', location.pathname);
    document.title = 'VODLOCK | Indexed VALORANT VODs';
    window.scrollTo({top: 0});
  }

  async function openVideo(key, updateHistory = true) {
    const source = VodlockSite.mediaSource(key);
    const entry = source && state.catalog.find(candidate => entryKey(candidate) === VodlockSite.sourceKey(source.provider, source.sourceId));
    if (!entry) {
      setSearchMessage('This VOD has not been processed yet. Completed broadcasts appear after their round index passes validation.', true);
      return;
    }
    setSearchMessage('');
    const response = await fetch(entry.index, {cache: 'no-store'});
    if (!response.ok) throw new Error('This round index is temporarily unavailable.');
    const index = VodlockSite.validateIndex(await response.json());
    if (index.provider !== entry.provider || index.sourceId !== entry.sourceId) throw new Error('The catalog and round index do not match.');
    await loadChat(entry);
    setPlaybackControlsDisabled(true);
    state.entry = entry;
    state.index = index;
    destroyPlayer();
    state.started = false;
    resetPlayerHost();
    $('watch-event').textContent = entry.event;
    $('watch-title').textContent = entry.title;
    $('library-view').hidden = true;
    $('player-view').hidden = false;
    $('start-gate').hidden = false;
    $('end-shield').hidden = true;
    $('start-watching').disabled = false;
    $('start-watching').textContent = 'Start watching';
    applySettings();
    if (updateHistory) history.pushState({source: entryKey(entry)}, '', '?v=' + encodeURIComponent(entryKey(entry)));
    document.title = entry.title + ' | VODLOCK';
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
    seekPlayer(Math.max(0, state.index.rounds[0].start - state.index.leadSeconds));
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
      $('current-map').textContent = 'Position hidden';
      $('current-round').textContent = 'Navigation remains active';
    } else if (position < 0) {
      $('current-map').textContent = 'Opening';
      $('current-round').textContent = 'Ready to begin';
    } else {
      $('current-map').textContent = 'Map ' + round.map;
      $('current-round').textContent = 'Round ' + round.round;
    }
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

  function syncFullscreenButton() {
    const chatVisible = !$('chat-panel').hidden;
    const fullscreen = document.fullscreenElement === $('watch-layout');
    $('fullscreen-button').textContent = fullscreen ? 'Exit full screen' : chatVisible ? 'Full screen + chat' : 'Full screen';
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

  $('video-search').addEventListener('submit', event => {
    event.preventDefault();
    const source = VodlockSite.mediaSource($('video-url').value);
    if (!source) {
      setSearchMessage('Paste a valid public YouTube or Twitch VOD link.', true);
      return;
    }
    openVideo(VodlockSite.sourceKey(source.provider, source.sourceId)).catch(error => setSearchMessage(error.message, true));
  });
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
  document.addEventListener('fullscreenchange', syncFullscreenButton);
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
    setSearchMessage(error.message, true);
  });
})();
