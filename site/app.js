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
    G2: '/assets/teams/g2.png',
    KC: '/assets/teams/kc.png',
    JDG: '/assets/teams/jdg.png',
    FUT: '/assets/teams/fut.png',
    LOUD: '/assets/teams/loud.png',
    EDG: '/assets/teams/edg.png',
    PRX: '/assets/teams/prx.png',
    TL: '/assets/teams/tl.png'
  };
  const regions = [
    {id: 'americas', name: 'Americas'},
    {id: 'emea', name: 'EMEA'},
    {id: 'pacific', name: 'Pacific'},
    {id: 'china', name: 'China'}
  ];
  const vctTeams = [
    {code: '100T', name: '100 Thieves', region: 'americas'},
    {code: 'C9', name: 'Cloud9', region: 'americas'},
    {code: 'EG', name: 'Evil Geniuses', region: 'americas'},
    {code: 'ENVY', name: 'ENVY', region: 'americas'},
    {code: 'FUR', name: 'FURIA', region: 'americas'},
    {code: 'G2', name: 'G2 Esports', region: 'americas'},
    {code: 'KRU', name: 'KRÜ Visa', region: 'americas', aliases: ['KRÜ']},
    {code: 'LEV', name: 'Leviatán Esports', region: 'americas', aliases: ['Leviatán']},
    {code: 'LOUD', name: 'LOUD', region: 'americas'},
    {code: 'MIBR', name: 'MIBR', region: 'americas'},
    {code: 'NRG', name: 'NRG', region: 'americas'},
    {code: 'SEN', name: 'Sentinels', region: 'americas'},
    {code: 'BBL', name: 'BBL Esports', region: 'emea'},
    {code: 'EF', name: 'Eternal Fire', region: 'emea'},
    {code: 'FNC', name: 'FNATIC', region: 'emea'},
    {code: 'FUT', name: 'FUT Esports', region: 'emea'},
    {code: 'GX', name: 'GIANTX', region: 'emea'},
    {code: 'KC', name: 'Karmine Corp', region: 'emea'},
    {code: 'M8', name: 'Gentle Mates', region: 'emea'},
    {code: 'NAVI', name: 'NAVI', region: 'emea', aliases: ['Natus Vincere']},
    {code: 'PCF', name: 'PCIFIC Esports', region: 'emea'},
    {code: 'TH', name: 'Team Heretics', region: 'emea'},
    {code: 'TL', name: 'Team Liquid', region: 'emea'},
    {code: 'VIT', name: 'Team Vitality', region: 'emea', aliases: ['Vitality']},
    {code: 'DFM', name: 'DetonatioN FocusMe', region: 'pacific'},
    {code: 'FS', name: 'FULL SENSE', region: 'pacific'},
    {code: 'GE', name: 'Global Esports', region: 'pacific'},
    {code: 'GEN', name: 'Gen.G', region: 'pacific'},
    {code: 'KRX', name: 'Kiwoom DRX', region: 'pacific', aliases: ['DRX']},
    {code: 'NS', name: 'Nongshim RedForce', region: 'pacific'},
    {code: 'PRX', name: 'Paper Rex', region: 'pacific'},
    {code: 'RRQ', name: 'Rex Regum Qeon', region: 'pacific'},
    {code: 'T1', name: 'T1', region: 'pacific'},
    {code: 'TS', name: 'Team Secret', region: 'pacific'},
    {code: 'VL', name: 'VARREL', region: 'pacific'},
    {code: 'ZETA', name: 'ZETA DIVISION', region: 'pacific'},
    {code: 'AG', name: 'ALL GAMERS', region: 'china'},
    {code: 'BLG', name: 'Bilibili Gaming', region: 'china'},
    {code: 'DRG', name: 'Dragon Ranger Gaming', region: 'china'},
    {code: 'EDG', name: 'EDward Gaming', region: 'china'},
    {code: 'FPX', name: 'FunPlus Phoenix', region: 'china'},
    {code: 'JDG', name: 'JD Gaming', region: 'china'},
    {code: 'NOVA', name: 'NOVA Esports', region: 'china'},
    {code: 'TE', name: 'Trace Esports', region: 'china'},
    {code: 'TEC', name: 'Titan Esports Club', region: 'china'},
    {code: 'TYL', name: 'TYLOO Gaming', region: 'china'},
    {code: 'WOL', name: 'Wolves Esports', region: 'china'},
    {code: 'XLG', name: 'Xi Lai Gaming', region: 'china'}
  ];
  const teamKey = value => value.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
  const teamAliases = new Map(vctTeams.flatMap(team => [team.code, team.name, ...(team.aliases || [])].map(value => [teamKey(value), team.code])));
  const teamsByCode = new Map(vctTeams.map(team => [team.code, team]));
  const state = {catalog: [], entry: null, index: null, chat: [], chatPosition: null, player: null, ready: false, started: false, playing: false, theater: false, resume: readResume(), resumeTime: null, lastResumeWrite: 0, toastTimer: null, statusTimer: null, kindFilter: 'all', regionFilter: 'all', teamFilter: 'all', favouriteTeams: readFavouriteTeams(), watchedMatches: readWatchedMatches(), tournament: null, teamPickerMode: 'favourites'};
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
    renderChat(true);
    updateStatus();
  }

  function readFavouriteTeams() {
    try {
      const saved = JSON.parse(localStorage.getItem('spoilless-favourite-teams') || '[]');
      return new Set(Array.isArray(saved) ? saved.filter(team => teamsByCode.has(team)) : []);
    } catch {
      return new Set();
    }
  }

  function saveFavouriteTeams() {
    try { localStorage.setItem('spoilless-favourite-teams', JSON.stringify([...state.favouriteTeams])); } catch {}
  }

  function readWatchedMatches() {
    try {
      const saved = JSON.parse(localStorage.getItem('spoilless-watched-matches') || '[]');
      return new Set(Array.isArray(saved) ? saved.filter(value => typeof value === 'string') : []);
    } catch {
      return new Set();
    }
  }

  function saveWatchedMatches() {
    try { localStorage.setItem('spoilless-watched-matches', JSON.stringify([...state.watchedMatches])); } catch {}
  }

  function preventTwitchNativeFullscreen() {
    if (state.entry?.provider !== 'twitch') return;
    const iframe = $('media-player')?.querySelector('iframe');
    if (!iframe) return;
    iframe.removeAttribute('allowfullscreen');
    const currentPermissions = iframe.getAttribute('allow') || '';
    const permissions = currentPermissions.split(';').map(value => value.trim()).filter(value => value && !value.startsWith('fullscreen')).join('; ');
    if (permissions !== currentPermissions) {
      if (permissions) iframe.setAttribute('allow', permissions);
      else iframe.removeAttribute('allow');
    }
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
    $('quality-control').hidden = true;
    $('quality').disabled = true;
    $('quality').replaceChildren();
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
          image.src = emote.startsWith('7tv:')
            ? 'https://cdn.7tv.app/emote/' + emote.slice(4) + '/1x.webp'
            : 'https://static-cdn.jtvnw.net/emoticons/v2/' + emote + '/default/dark/1.0';
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
    return entry.catalogId || VodlockSite.sourceKey(entry.provider, entry.sourceId);
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

  function teamCode(value) {
    return teamAliases.get(teamKey(value)) || value.toUpperCase();
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
    return teams.length === 2 ? teams.map(teamCode).sort().join(':') : entry.title.toUpperCase();
  }

  function creatorName(entry) {
    return entry.creator || entry.title.replace(/^EG\s+/i, '').trim() || 'Creator';
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
    }).sort((first, second) => VodlockSite.playedTime(second.primary.playedAt) - VodlockSite.playedTime(first.primary.playedAt));
  }

  function tournamentName(entry) {
    if (typeof entry.tournament === 'string' && entry.tournament.trim()) return entry.tournament.trim();
    const event = displayEvent(entry).replace(/^.*?\s+vs\.?\s+.*?\s*[|·]\s*/i, '');
    const name = event.replace(/\s*[|·-]\s*(?:opening day|group stage|swiss stage|playoffs?|upper final|lower final|grand final).*$/i, '').trim();
    return name || 'Tournament archive';
  }

  function tournamentKey(entry) {
    if (typeof entry.tournamentKey === 'string' && entry.tournamentKey) return entry.tournamentKey;
    return tournamentName(entry).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^A-Za-z0-9]+/g, '-').replace(/(^-|-$)/g, '').toLowerCase() || 'tournament-archive';
  }

  function tournamentMatches() {
    const groups = new Map();
    for (const match of groupedMatches()) {
      const key = tournamentKey(match.primary);
      if (!groups.has(key)) groups.set(key, {key, name: tournamentName(match.primary), matches: []});
      groups.get(key).matches.push(match);
    }
    return [...groups.values()].map(tournament => ({...tournament, matches: tournament.matches.slice()
      .sort((first, second) => VodlockSite.playedTime(first.primary.playedAt) - VodlockSite.playedTime(second.primary.playedAt))}))
      .sort((first, second) => VodlockSite.playedTime(second.matches.at(-1)?.primary.playedAt) - VodlockSite.playedTime(first.matches.at(-1)?.primary.playedAt));
  }

  function isMatchWatched(match) {
    return state.watchedMatches.has(matchKey(match.primary));
  }

  function markMatchWatched(entry) {
    state.watchedMatches.add(matchKey(entry));
    saveWatchedMatches();
    renderFavourites();
    renderTournaments();
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
    const selectedTeam = teamsByCode.get(state.teamFilter);
    if (selectedTeam && state.regionFilter !== 'all' && selectedTeam.region !== state.regionFilter) state.teamFilter = 'all';
    const allTeams = state.regionFilter === 'all' ? 'All VCT teams' : 'All ' + regions.find(region => region.id === state.regionFilter).name + ' teams';
    $('team-filter').textContent = state.teamFilter === 'all' ? allTeams : teamsByCode.get(state.teamFilter).name;
    for (const button of $('kind-filters').querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.kind === state.kindFilter));
    }
    for (const button of $('region-filters').querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.region === state.regionFilter));
    }
  }

  function selectTeamFilter(team) {
    state.teamFilter = team;
    renderFilters();
    renderCatalog();
  }

  function renderFavourites() {
    const host = $('favourite-teams');
    host.replaceChildren();
    if (!state.favouriteTeams.size) {
      const empty = document.createElement('p');
      empty.className = 'favourite-empty';
      empty.textContent = 'Pick teams to keep their unwatched matches in one place.';
      host.append(empty);
    } else {
      for (const code of [...state.favouriteTeams].sort()) {
        const team = teamsByCode.get(code);
        const tile = document.createElement('span');
        tile.className = 'favourite-team';
        tile.textContent = team.name;
        host.append(tile);
      }
    }
    const unwatched = groupedMatches().filter(match => !isMatchWatched(match) && match.teams.map(teamCode).some(code => state.favouriteTeams.has(code))).length;
    $('favourite-match-count').textContent = String(unwatched);
    $('favourite-match-copy').textContent = unwatched === 1 ? 'unwatched match' : 'unwatched matches';
  }

  function renderTeamPicker() {
    const picker = $('team-picker');
    picker.replaceChildren();
    const filtering = state.teamPickerMode === 'filter';
    const teams = vctTeams.filter(team => !filtering || state.regionFilter === 'all' || team.region === state.regionFilter);
    if (filtering) {
      const all = document.createElement('button');
      all.type = 'button';
      all.setAttribute('aria-pressed', String(state.teamFilter === 'all'));
      const name = document.createElement('strong');
      name.textContent = state.regionFilter === 'all' ? 'All VCT teams' : 'All ' + regions.find(region => region.id === state.regionFilter).name + ' teams';
      const label = document.createElement('span');
      label.textContent = 'Clear filter';
      all.append(name, label);
      all.addEventListener('click', () => {
        selectTeamFilter('all');
        $('teams-dialog').close();
      });
      picker.append(all);
    }
    for (const team of teams) {
      const choice = document.createElement('button');
      choice.type = 'button';
      choice.setAttribute('aria-pressed', String(filtering ? state.teamFilter === team.code : state.favouriteTeams.has(team.code)));
      const name = document.createElement('strong');
      name.textContent = team.name;
      const label = document.createElement('span');
      label.textContent = team.region;
      choice.append(name, label);
      choice.addEventListener('click', () => {
        if (filtering) {
          selectTeamFilter(team.code);
          $('teams-dialog').close();
          return;
        }
        if (state.favouriteTeams.has(team.code)) state.favouriteTeams.delete(team.code);
        else state.favouriteTeams.add(team.code);
        saveFavouriteTeams();
        renderTeamPicker();
        renderFavourites();
      });
      picker.append(choice);
    }
  }

  function openTeamPicker(mode) {
    state.teamPickerMode = mode;
    const filtering = mode === 'filter';
    $('teams-title').textContent = filtering ? 'Filter matches' : 'Choose your teams';
    $('teams-copy').textContent = filtering ? 'Choose one team to narrow the match archive, or clear the filter to show every team.' : 'Select the teams you want to follow. This never exposes their tournament path.';
    renderTeamPicker();
    $('teams-dialog').showModal();
  }

  function tournamentMatchRow(match, position, status) {
    const row = document.createElement('article');
    row.className = 'tournament-match is-' + status;
    const index = document.createElement('span');
    index.className = 'tournament-match-index';
    index.textContent = 'Match ' + String(position + 1).padStart(2, '0');
    const title = document.createElement('strong');
    const label = document.createElement('span');
    if (status === 'watched') {
      title.textContent = 'Watched';
      label.textContent = 'Watched ✓';
      row.append(index, title, label);
    } else if (status === 'unlocked') {
      title.textContent = matchup(match.primary).join(' vs ') || 'Ready to watch';
      label.textContent = 'Next match';
      const watch = document.createElement('button');
      watch.type = 'button';
      watch.textContent = 'Watch';
      watch.addEventListener('click', () => openVideo(entryKey(match.primary)));
      row.append(index, title, label, watch);
    } else {
      title.textContent = 'Locked';
      label.textContent = 'Locked';
      row.append(index, title, label);
    }
    return row;
  }

  function renderTournaments() {
    const host = $('tournaments');
    if (!host) return;
    host.replaceChildren();
    const tournaments = tournamentMatches();
    const selected = tournaments.find(tournament => tournament.key === state.tournament);
    if (state.tournament && !selected) state.tournament = null;
    if (selected) {
      const path = document.createElement('div');
      path.className = 'tournament-path';
      const heading = document.createElement('div');
      heading.className = 'tournament-path-heading';
      const title = document.createElement('h3');
      title.textContent = selected.name;
      const back = document.createElement('button');
      back.type = 'button';
      back.textContent = '← All tournaments';
      back.addEventListener('click', () => {
        state.tournament = null;
        renderTournaments();
      });
      heading.append(title, back);
      path.append(heading);
      const next = selected.matches.findIndex(match => !isMatchWatched(match));
      for (const [position, match] of selected.matches.entries()) {
        const status = isMatchWatched(match) ? 'watched' : position === next ? 'unlocked' : 'locked';
        path.append(tournamentMatchRow(match, position, status));
      }
      host.append(path);
      return;
    }
    for (const tournament of tournaments) {
      const card = document.createElement('article');
      card.className = 'tournament-card';
      const details = document.createElement('div');
      const name = document.createElement('h3');
      name.textContent = tournament.name;
      const copy = document.createElement('p');
      copy.textContent = 'Follow the event in the order it was played.';
      details.append(name, copy);
      const watch = document.createElement('button');
      watch.type = 'button';
      watch.textContent = 'Watch tournament →';
      watch.addEventListener('click', () => {
        state.tournament = tournament.key;
        renderTournaments();
      });
      card.append(details, watch);
      host.append(card);
    }
  }

  function filteredMatches() {
    return groupedMatches().filter(match => {
      const codes = match.teams.map(teamCode);
      return (state.kindFilter === 'all' || match.entries.some(entry => entryKind(entry) === state.kindFilter)) &&
        (state.regionFilter === 'all' || codes.some(code => teamsByCode.get(code)?.region === state.regionFilter)) &&
        (state.teamFilter === 'all' || codes.includes(state.teamFilter));
    });
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
      watch.append(label, provider);
      watch.addEventListener('click', () => openVideo(entryKey(entry)));
      sources.append(watch);
    }
    card.append(heading, index, matchTeams, details, sources);
    return card;
  }

  function renderCatalog() {
    const catalog = $('catalog');
    catalog.replaceChildren();
    const visible = filteredMatches();
    $('filter-match-count').textContent = visible.length;
    $('filter-summary').querySelector('span').textContent = visible.length === 1 ? 'match' : 'matches';
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
        (entry.catalogId === undefined || /^(youtube|twitch):[A-Za-z0-9_-]{6,20}:[A-Za-z0-9_-]{6,20}$/.test(entry.catalogId)) &&
        typeof entry.title === 'string' && typeof entry.event === 'string' && VodlockSite.playedTime(entry.playedAt) > 0;
    });
    renderFilters();
    renderCatalog();
    renderContinue();
    renderFavourites();
    renderTeamPicker();
    renderTournaments();
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
    const entry = state.catalog.find(candidate => entryKey(candidate) === key);
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
    preventTwitchNativeFullscreen();
    setVolume(Number($('volume').value));
    const destination = state.resumeTime ?? Math.max(0, state.index.rounds[0].start - state.index.leadSeconds);
    state.resumeTime = null;
    seekPlayer(destination);
    playPlayer();
    setPlaybackControlsDisabled(false);
    updateQualityOptions();
    const player = state.player;
    if (state.entry.provider === 'twitch') setTimeout(() => {
      if (state.player === player) updateQualityOptions();
    }, 1000);
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
    preventTwitchNativeFullscreen();
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
    markMatchWatched(state.entry);
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

  function updateQualityOptions() {
    const control = $('quality-control');
    const select = $('quality');
    if (state.entry?.provider !== 'twitch' || !state.ready || typeof state.player?.getQualities !== 'function') {
      control.hidden = true;
      select.disabled = true;
      return;
    }
    let qualities;
    try {
      qualities = state.player.getQualities() || [];
    } catch {
      qualities = [];
    }
    const options = qualities.map(quality => {
      const value = typeof quality === 'string' ? quality : quality.group || quality.name;
      const label = typeof quality === 'string' ? quality : quality.name || quality.group;
      return value ? {value, label: value === 'chunked' ? 'Source' : value === 'auto' ? 'Auto' : label} : null;
    }).filter(Boolean).filter((quality, index, all) => all.findIndex(candidate => candidate.value === quality.value) === index);
    if (!options.length) {
      control.hidden = true;
      select.disabled = true;
      return;
    }
    const current = typeof state.player.getQuality === 'function' ? state.player.getQuality() : '';
    select.replaceChildren(...options.map(quality => {
      const option = document.createElement('option');
      option.value = quality.value;
      option.textContent = quality.label;
      return option;
    }));
    if (options.some(quality => quality.value === current)) select.value = current;
    control.hidden = false;
    select.disabled = false;
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

  function reclaimPlayerFocus() {
    if ($('player-view').hidden || !state.started || $('settings-dialog').open) return;
    setTimeout(() => {
      if (document.activeElement?.tagName === 'IFRAME') $('player-shell').focus({preventScroll: true});
    }, 150);
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
  $('choose-teams').addEventListener('click', () => {
    openTeamPicker('favourites');
  });
  $('teams-close').addEventListener('click', () => $('teams-dialog').close());
  $('teams-dialog').addEventListener('click', event => {
    if (event.target === $('teams-dialog')) $('teams-dialog').close();
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
  for (const button of $('region-filters').querySelectorAll('button')) {
    button.addEventListener('click', () => {
      state.regionFilter = button.dataset.region;
      renderFilters();
      renderCatalog();
    });
  }
  $('team-filter').addEventListener('click', () => {
    openTeamPicker('filter');
  });
  $('home-button').addEventListener('click', () => showLibrary());
  const spoilerWord = $('spoiler-word');
  spoilerWord.addEventListener('click', () => {
    const revealed = spoilerWord.getAttribute('aria-pressed') === 'true';
    spoilerWord.setAttribute('aria-pressed', String(!revealed));
    spoilerWord.setAttribute('aria-label', revealed ? 'Reveal spoilers' : 'Hide spoilers');
  });
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
  $('quality').addEventListener('change', event => {
    if (!state.ready || state.entry.provider !== 'twitch' || typeof state.player.setQuality !== 'function') return;
    state.player.setQuality(event.target.value);
    toast('Video quality set to ' + event.target.options[event.target.selectedIndex].textContent + '.');
  });
  $('fullscreen-button').addEventListener('click', toggleFullscreen);
  $('theater-button').addEventListener('click', toggleTheater);
  document.addEventListener('fullscreenchange', syncFullscreenButton);
  new MutationObserver(preventTwitchNativeFullscreen).observe($('player-shell'), {subtree: true, childList: true, attributes: true, attributeFilter: ['allow', 'allowfullscreen']});
  addEventListener('blur', reclaimPlayerFocus);
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
    if ($('settings-dialog').open) return;
    const key = event.code === 'Space' ? ' ' : event.key;
    if (!state.started) {
      if (key === ' ') {
        event.preventDefault();
        if (!event.repeat) startWatching();
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
    if (!actions[key]) return;
    event.preventDefault();
    if (!event.repeat || key !== ' ') actions[key]();
  }, true);

  addEventListener('popstate', () => {
    const source = new URL(location.href).searchParams.get('v');
    if (source) openVideo(source, false).catch(error => toast(error.message));
    else showLibrary(false);
  });

  applySettings();
  loadCatalog().then(() => {
    const source = new URL(location.href).searchParams.get('v');
    if (source) return openVideo(source, false);
  }).catch(error => {
    $('catalog').replaceChildren();
    const message = document.createElement('p');
    message.className = 'empty-library';
    message.textContent = error.message;
    $('catalog').append(message);
  });
})();
