'use strict';

const $ = id => document.getElementById(id);
let sourceKind = 'youtube';
let state;
let timer;
let noticeTimer;
let bridgeAvailable = false;
let reviewJobId;

function notice(text) {
  $('notice').hidden = true;
  $('review-notice').hidden = true;
  const target = $('review').open ? $('review-notice') : $('notice');
  target.textContent = text;
  target.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { target.hidden = true; }, 5500);
}

async function api(path, body) {
  if (body !== undefined && !state) throw new Error('Round Studio is still connecting. Please try again in a moment.');
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-VODLOCK-Token': state.token},
    body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'The request failed.');
  return value;
}

function chooseSource(kind) {
  sourceKind = kind;
  $('youtube-tab').setAttribute('aria-pressed', kind === 'youtube');
  $('local-tab').setAttribute('aria-pressed', kind === 'local');
  $('source').type = kind === 'youtube' ? 'url' : 'text';
  $('source').value = '';
  $('source').placeholder = kind === 'youtube' ? 'https://www.youtube.com/watch?v=…' : 'C:\\Videos\\my-recording.mp4';
  $('source-label').textContent = kind === 'youtube' ? 'YouTube video link' : 'Full path to a recording on this PC';
  $('source-help').textContent = kind === 'youtube' ? 'Use the original VOD. No sign-in or account cookies are used.' : 'Paste the full file path. The video stays on this PC and is never uploaded.';
  $('linked-video').hidden = kind !== 'local';
  $('download-notice').textContent = kind === 'youtube' ? '↳ A video-only analysis copy (up to 720p / 2 GB) is downloaded to this PC. Indexing can take several minutes.' : '↳ The source recording is read-only. Indexing can take several minutes.';
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function action(parent, label, callback, className) {
  const button = node('button', label, className);
  button.type = 'button';
  button.addEventListener('click', () => Promise.resolve(callback()).catch(error => notice(error.message)));
  parent.append(button);
}

function render() {
  $('start').disabled = !!state.active;
  const list = $('jobs');
  list.replaceChildren();
  const jobs = [...state.jobs].sort((a, b) => b.created - a.created);
  if (!jobs.length) list.append(node('div', 'Nothing here yet. Add your first VOD above to get started.', 'empty-library'));
  for (const job of jobs) {
    const card = node('article', undefined, 'job-card');
    card.append(node('div', job.status === 'ready' ? '▤' : '◈', 'job-icon'));
    const info = node('div', undefined, 'job-info');
    info.append(node('h3', job.label));
    const detail = node('p');
    const labels = {ready: 'INDEX READY', analyzing: 'ANALYSING', downloading: 'DOWNLOADING', queued: 'QUEUED', error: 'NEEDS ATTENTION', cancelled: 'CANCELLED'};
    detail.append(node('span', labels[job.status] || job.status, 'job-badge' + (job.status === 'error' ? ' error' : '')));
    detail.append(document.createTextNode(job.message));
    info.append(detail);
    if (job.id === state.active) {
      const progress = node('div', undefined, 'progress');
      progress.setAttribute('role', 'progressbar');
      progress.setAttribute('aria-label', 'Indexing progress');
      progress.setAttribute('aria-valuenow', job.progress);
      progress.setAttribute('aria-valuemin', 0);
      progress.setAttribute('aria-valuemax', 100);
      const fill = node('span');
      fill.style.width = job.progress + '%';
      progress.append(fill);
      info.append(progress);
    }
    card.append(info);
    const actions = node('div', undefined, 'job-actions');
    if (job.status === 'ready') {
      action(actions, 'Review index', () => openReview(job));
      action(actions, 'Export JSON', () => download(job));
      if (job.videoId) {
        action(actions, 'Use in SPOILLESS', () => transfer(job), 'use-index');
        const watch = node('a', 'Watch ↗');
        watch.href = 'https://www.youtube.com/watch?v=' + job.videoId;
        watch.target = '_blank';
        watch.rel = 'noopener noreferrer';
        actions.append(watch);
      }
    }
    if (job.id === state.active) action(actions, 'Cancel', async () => { await api('/api/cancel', {}); notice('Stopping safely…'); });
    card.append(actions);
    list.append(card);
  }
}

async function refresh() {
  clearTimeout(timer);
  try {
    state = await api('/api/state');
    render();
    $('form-error').textContent = '';
  } catch (error) {
    $('form-error').textContent = 'Round Studio is not responding. Keep its launcher running, then refresh this page.';
  }
  timer = setTimeout(refresh, state?.active ? 2000 : 10000);
}

function formatTime(seconds) {
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const rest = String(total % 60).padStart(2, '0');
  return (hours ? hours + ':' + String(minutes).padStart(2, '0') : minutes) + ':' + rest;
}

function parseTime(value) {
  if (!/^\d{1,3}:\d{2}(?::\d{2})?$/.test(value.trim())) throw new Error('Enter a time as mm:ss or hh:mm:ss.');
  const parts = value.trim().split(':').map(Number);
  if (parts.slice(1).some(part => part > 59)) throw new Error('Minutes and seconds must be below 60.');
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function openReview(job) {
  reviewJobId = job.id;
  $('review-title').textContent = job.label;
  $('rounds').replaceChildren();
  $('review-warnings').replaceChildren();
  for (const warning of job.warnings) $('review-warnings').append(node('p', warning, 'form-error'));
  job.rounds.forEach((round, index) => {
    const row = node('tr');
    row.classList.toggle('excluded-round', !!round.excluded);
    row.append(node('td', `Map ${round.map} · Round ${round.round}`));
    const time = node('td');
    const input = node('input');
    input.value = formatTime(round.start);
    input.setAttribute('aria-label', `Actual start for map ${round.map} round ${round.round}`);
    time.append(input);
    row.append(time);
    const check = node('td');
    let link;
    if (job.videoId) {
      link = node('a', 'Check clip ↗');
      link.href = `https://www.youtube.com/watch?v=${job.videoId}&t=${Math.max(0, Math.floor(round.start - 5))}s`;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      check.append(link);
    } else check.textContent = 'Check your recording';
    row.append(check);
    const save = node('td');
    const label = node('span', round.verified ? 'Checked' : 'Unreviewed', 'verified');
    action(save, 'Save', async () => {
      const start = input.value === formatTime(round.start) ? round.start : parseTime(input.value);
      await api('/api/review/' + job.id, {index, map: round.map, round: round.round, start});
      round.start = start;
      round.verified = true;
      label.textContent = 'Checked';
      if (link) link.href = `https://www.youtube.com/watch?v=${job.videoId}&t=${Math.max(0, Math.floor(start - 5))}s`;
      notice('Saved. Send the updated index to SPOILLESS when you finish reviewing.');
    });
    save.append(label);
    action(save, round.excluded ? 'Include' : 'Exclude', async () => {
      await api('/api/review/' + job.id, {index, map: round.map, round: round.round, action: round.excluded ? 'include' : 'exclude'});
      await refresh();
      openReview(state.jobs.find(entry => entry.id === job.id));
      notice('Saved. Excluded detections can be re-included at any time.');
    });
    row.append(save);
    $('rounds').append(row);
  });
  $('review').showModal();
}

async function download(job) {
  const value = await api('/api/export/' + job.id);
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'}));
  const link = node('a');
  link.href = url;
  link.download = 'vodlock-' + (job.videoId || job.id) + '.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function transfer(job) {
  if (!bridgeAvailable) throw new Error('The SPOILLESS extension was not detected. Reload this page after updating it, or export JSON and import it in the extension popup.');
  const index = await api('/api/export/' + job.id);
  window.postMessage({type: 'VODLOCK_SAVE_INDEX', index, requestId: crypto.randomUUID()}, location.origin);
}

window.addEventListener('message', event => {
  if (event.source !== window || event.origin !== location.origin) return;
  if (event.data?.type === 'VODLOCK_INDEX_READY') bridgeAvailable = true;
  if (event.data?.type === 'VODLOCK_INDEX_SAVED') notice(event.data.error || 'Saved to SPOILLESS. Open the matching YouTube VOD and use Next Round or Down Arrow.');
});

$('youtube-tab').addEventListener('click', () => chooseSource('youtube'));
$('local-tab').addEventListener('click', () => chooseSource('local'));
$('close-review').addEventListener('click', () => $('review').close());
$('add-round').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/review/' + reviewJobId, {action: 'add', map: Number($('manual-map').value),
              round: Number($('manual-round').value), start: parseTime($('manual-start').value)});
    await refresh();
    openReview(state.jobs.find(entry => entry.id === reviewJobId));
    $('manual-round').value = '';
    $('manual-start').value = '';
    notice('Round added. Send the updated index to SPOILLESS when finished.');
  } catch (error) {
    notice(error.message);
  }
});
$('help').addEventListener('click', () => $('help-dialog').showModal());
$('close-help').addEventListener('click', () => $('help-dialog').close());
$('new-vod').addEventListener('submit', async event => {
  event.preventDefault();
  $('start').disabled = true;
  $('form-error').textContent = '';
  try {
    await api('/api/jobs', {kind: sourceKind, source: $('source').value.trim(), label: $('label').value,
                          youtube: $('youtube-link').value.trim()});
    await refresh();
    $('library').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch (error) {
    $('form-error').textContent = error.message;
    $('start').disabled = !!state?.active;
  }
});
window.postMessage({type: 'VODLOCK_INDEX_PING'}, location.origin);
setTimeout(() => window.postMessage({type: 'VODLOCK_INDEX_PING'}, location.origin), 1500);
refresh();
