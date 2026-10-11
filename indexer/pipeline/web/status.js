'use strict';
const byId = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get('token') || '';
if (location.hash) history.replaceState(null, '', location.pathname);
let snapshot = null;
let refreshing = false;
let stopping = false;
const expandedMatches = new Set();
const reviews = new Map();
const labels = {pending: 'Waiting for match data', indexing: 'Recording match', validating: 'Checking match data', provisional: 'Ready - preliminary', final: 'Ready - final', needs_review: 'Needs attention', ready: 'Ready', waiting_alignment: 'Matching the streams', running: 'Working', queued: 'Waiting to start', retryable: 'Retry scheduled', waiting_source: 'Waiting for stream', succeeded: 'Completed', pushed: 'Sent for deployment', deployed: 'Confirmed live', superseded: 'Replaced'};
function label(state) { return labels[state] || 'Unknown'; }
function stamp(value) { return value ? new Date(value).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : 'Not recorded'; }
function element(tag, text, className) { const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node; }
function badge(state) { return element('span', label(state), 'state ' + (['final', 'provisional', 'ready', 'deployed'].includes(state) ? 'good' : state === 'needs_review' ? 'bad' : ['waiting_source', 'waiting_alignment', 'retryable'].includes(state) ? 'warning' : '')); }
function upcoming(row) {
  const today = new Intl.DateTimeFormat('en-CA', {timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
  return row.day > today || row.day === today && row.state === 'pending';
}
function renderActivity(value) {
  const tasks = value.activity || [];
  byId('working-count').textContent = tasks.length ? String(tasks.length) : '';
  byId('working-state').textContent = value.worker.state === 'stopping' ? 'Finishing current work' : tasks.length ? 'In progress' : 'Waiting';
  byId('activity-empty').hidden = tasks.length !== 0;
  const names = {live: 'Recording the official stream', twitch_live: 'Recording Twitch', twitch_vod: 'Processing the Twitch archive', twitch_align: 'Synchronizing the watch party', chat_gaps: 'Recovering Twitch chat', fingerprint_archive: 'Processing the official archive', reconcile: 'Synchronizing the official archive', recover: 'Recovering missing match footage', validate: 'Checking match data', validate_upload: 'Checking the separate match upload', finalize: 'Running final archive checks', segment: 'Identifying matches', refresh: 'Checking broadcast availability', export: 'Preparing a website update', deploy: 'Publishing a website update', verify_deployment: 'Checking the published website', discover_youtube: 'Looking for official recordings', discover_twitch_vods: 'Looking for Twitch recordings'};
  byId('activity').replaceChildren(...tasks.map(task => {
    const row = element('li', '', 'activity-row');
    const content = element('div', '', 'activity-content');
    const phase = {visual_alignment: 'Matching video images', scoreboard: 'Checking round timestamps', searching: 'Searching nearby for the correct round start'};
    content.append(element('strong', phase[task.phase] || names[task.kind] || 'Processing a task', 'activity-title'));
    const matchName = match => upcoming(match) || snapshot.matches.some(row => upcoming(row) && row.day === match.day && row.team_a === match.team_a && row.team_b === match.team_b) ? 'Upcoming matchup hidden' : match.team_a + ' vs ' + match.team_b;
    const match = task.team_a && task.team_b ? matchName(task) : (task.matches || []).map(match => matchName({...match, day: task.day})).join(' / ');
    const source = task.creator === 'gofns' ? 'FNS watch party' : task.role === 'watch_party' ? 'Watch party' : task.provider === 'youtube' ? 'Official recording' : task.provider === 'twitch' ? 'Twitch recording' : '';
    content.append(element('span', [source, match, task.day].filter(Boolean).join(' / '), 'secondary'));
    if (task.progress?.map && task.progress?.round) content.append(element('span', 'Map ' + task.progress.map + ', round ' + task.progress.round, 'secondary'));
    row.append(content);
    const progress = element('div', '', 'activity-progress');
    if (Number.isInteger(task.progress?.completed) && task.progress.total > 0) {
      const done = Math.min(task.progress.completed, task.progress.total);
      progress.append(element('strong', done + ' / ' + task.progress.total + ' rounds checked'));
      const bar = document.createElement('progress'); bar.max = task.progress.total; bar.value = done; bar.setAttribute('aria-label', 'Round timestamp checks'); progress.append(bar);
    } else progress.append(element('strong', 'In progress'));
    if (task.started_at) progress.append(element('span', 'Started ' + stamp(task.started_at), 'secondary'));
    row.append(progress);
    return row;
  }));
}
function renderMatches() {
  const body = byId('matches');
  body.replaceChildren();
  const rows = snapshot.matches.filter(row => byId('day').value === 'all' || row.day === byId('day').value);
  byId('match-count').textContent = String(rows.length);
  for (const row of rows) {
    const tr = document.createElement('tr');
    const match = document.createElement('td');
    const name = element('strong', (row.team_a || 'Team to be confirmed') + ' vs ' + (row.team_b || 'Team to be confirmed'), 'match-name');
    if (upcoming(row)) {
      const spoiler = document.createElement('details'); spoiler.className = 'match-spoiler';
      spoiler.append(element('summary', 'Reveal matchup (spoilers)'), name);
      match.append(element('strong', 'Upcoming match', 'match-name'), spoiler);
    } else match.append(name);
    match.append(element('span', row.event + ' / Match ' + row.match_order, 'secondary'));
    tr.append(match, element('td', row.day));
    const index = document.createElement('td'); index.append(badge(row.state));
    const descriptions = {pending: 'No match data captured yet.', indexing: 'Collecting data from the stream.', validating: 'Checking the recording before it is ready.', provisional: 'Usable now; archive checks still pending.', final: 'Final checks complete.', needs_review: 'Match data needs a manual check.'};
    index.append(element('span', descriptions[row.state] || 'Status not yet available.', 'secondary'));
    const taskNames = {validate: 'Broadcast validation', finalize: 'Final archive check', validate_upload: 'Separate full-match upload check', recover: 'Missing recording recovery', reconcile_segment: 'Match archive synchronization', probe_archive: 'Official archive availability', fingerprint_archive: 'Official archive processing', reconcile: 'Broadcast archive synchronization', twitch_vod: 'Twitch archive processing', twitch_align: 'Twitch stream synchronization', chat_gaps: 'Missing Twitch chat recovery'};
    if (row.tasks?.length || row.checks?.length) {
      const details = document.createElement('details'); details.className = 'match-tasks';
      details.open = expandedMatches.has(row.id);
      details.addEventListener('toggle', () => { if (details.open) expandedMatches.add(row.id); else expandedMatches.delete(row.id); });
      const tasks = row.tasks || [];
      const attention = tasks.filter(task => task.state === 'needs_review').length;
      details.append(element('summary', attention ? attention + ' task' + (attention === 1 ? '' : 's') + ' requiring review' : tasks.length ? tasks.length + ' processing task' + (tasks.length === 1 ? '' : 's') : 'Recording checks requiring review', attention || row.checks?.length ? 'warning' : ''));
      for (const check of row.checks || []) details.append(element('span', check, 'secondary warning'));
      for (const task of tasks) {
        const item = document.createElement('div'); item.className = 'task-detail';
        const source = task.role === 'watch_party' ? 'Watch party' : task.provider === 'twitch' ? 'Official Twitch' : '';
        item.append(element('strong', (taskNames[task.kind] || 'Processing task') + (source ? ' / ' + source : '')));
        const states = {running: 'Working now', queued: 'Queued - not started yet', waiting_source: 'Waiting - automatic retry scheduled', retryable: 'Failed attempt - automatic retry scheduled', needs_review: 'Manual review required - not automatically retrying'};
        item.append(element('span', states[task.state] || label(task.state), 'secondary' + (task.state === 'needs_review' ? ' warning' : '')));
        if (['queued', 'waiting_source', 'retryable'].includes(task.state)) item.append(element('span', 'Eligible to run: ' + stamp(task.available_at), 'secondary'));
        if (task.reason) item.append(element('span', task.reason, 'secondary'));
        else if (['needs_review', 'retryable', 'waiting_source'].includes(task.state) && !task.checks?.length) item.append(element('span', 'No specific safe explanation recorded. Check the worker logs for this task.', 'secondary'));
        for (const check of task.checks || []) item.append(element('span', check, 'secondary'));
        if (task.scope === 'broadcast') item.append(element('span', 'Shared task for this broadcast, not just this match.', 'secondary'));
        if (task.state === 'needs_review' && task.id) {
          const review = reviews.get(task.id) || {open: false, checked: false, busy: false, message: ''};
          if (review.message.startsWith('Retry queued.')) review.message = 'The retry did not resolve this task. Review the latest issue before trying again.';
          reviews.set(task.id, review);
          const open = element('button', review.open ? 'Close review' : 'Review task'); open.type = 'button'; open.className = 'review-button';
          const panel = document.createElement('div'); panel.className = 'review-panel'; panel.hidden = !review.open;
          open.addEventListener('click', () => { review.open = !review.open; panel.hidden = !review.open; open.textContent = review.open ? 'Close review' : 'Review task'; });
          panel.append(element('h3', 'Check the source'));
          const guidance = {
            validate_upload: 'Open the separate upload and official broadcast. Check that the upload is the correct match and contains the full recording. An incomplete upload or text-recognition error may need a technical fix, not just another retry.',
            validate: 'Check the official recording for missing portions or incorrect match detection. Missing data may need recovery from another source before validation can pass.',
            finalize: 'Check that the official archive is available and complete. Finalization still needs all recording and validation checks to pass.',
            recover: 'Check whether the missing portion exists in the official archive or a registered fallback source. Retrying cannot recover footage that is absent from every source.',
            twitch_align: 'Check that the Twitch recording is the same broadcast and contains the relevant match. Stream synchronization requires enough matching visual evidence.',
            chat_gaps: 'Check Twitch archive availability and the chat-recovery setup. A missing tool or unavailable archive must be fixed before retrying.',
          };
          panel.append(element('p', guidance[task.kind] || 'Check the recorded issue and source availability. Fix the cause before retrying; unchanged input may produce the same warning again.'));
          const links = document.createElement('div'); links.className = 'review-links';
          for (const source of task.sources || []) {
            const link = element('a', source.label); link.href = source.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; links.append(link);
          }
          if (links.children.length) panel.append(links, element('p', 'Source videos may contain spoilers.', 'secondary'));
          else panel.append(element('p', 'No direct source link is available for this task.', 'secondary'));
          panel.append(element('h3', 'Run the checks again'));
          panel.append(element('p', 'Retry queues this task again. It does not approve the match, remove missing data, or bypass validation.'));
          const confirmation = document.createElement('label'); confirmation.className = 'review-confirmation';
          const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = review.checked;
          confirmation.append(checkbox, element('span', 'I checked the issue and am ready to retry.'));
          const retry = element('button', review.busy ? 'Queuing retry...' : 'Retry this task'); retry.type = 'button'; retry.disabled = !review.checked || review.busy || stopping;
          checkbox.addEventListener('change', () => { review.checked = checkbox.checked; retry.disabled = !review.checked || review.busy || stopping; });
          const message = element('p', review.message, 'review-result'); message.setAttribute('role', 'status');
          retry.addEventListener('click', async () => {
            review.busy = true; retry.disabled = true; retry.textContent = 'Queuing retry...';
            try {
              const response = await fetch('/retry-job?id=' + encodeURIComponent(task.id), {method: 'POST', headers: {Authorization: 'Bearer ' + token}, signal: AbortSignal.timeout(10000)});
              const result = await response.json();
              if (!response.ok) throw new Error(result.error || 'Retry was not confirmed. Refresh the task status.');
              review.message = 'Retry queued. The worker will rerun its checks; the task is not yet resolved.';
              review.checked = false;
              await refresh();
            } catch (error) { review.message = error.name === 'TimeoutError' || error.message === 'Failed to fetch' ? 'Retry was not confirmed. Refresh task status before trying again.' : error.message; }
            finally { review.busy = false; message.textContent = review.message; retry.textContent = 'Retry this task'; retry.disabled = !review.checked || stopping; }
          });
          const copy = element('button', 'Copy review details'); copy.type = 'button';
          copy.addEventListener('click', async () => {
            try {
              await navigator.clipboard.writeText([row.team_a + ' vs ' + row.team_b, row.day, taskNames[task.kind] || task.kind, 'Task: ' + task.id, task.reason || '', ...(task.checks || []), ...(task.sources || []).map(source => source.url)].filter(Boolean).join('\n'));
              review.message = 'Review details copied.';
            } catch { review.message = 'Could not copy review details. Clipboard access was unavailable.'; }
            message.textContent = review.message;
          });
          const actions = document.createElement('div'); actions.className = 'review-actions'; actions.append(retry, copy);
          panel.append(confirmation, actions, message); item.append(open, panel);
        } else if (reviews.get(task.id)?.message) item.append(element('span', reviews.get(task.id).message, 'secondary'));
        details.append(item);
      }
      index.append(details);
    }
    tr.append(index);
    const watchparty = document.createElement('td'); watchparty.append(row.watchparty ? badge(row.watchparty) : element('span', 'Not ready yet', 'secondary')); tr.append(watchparty);
    [...tr.children].forEach((cell, index) => cell.dataset.label = ['Match', 'Day', 'Match preparation', 'Watch party'][index]);
    body.append(tr);
  }
  byId('matches-empty').hidden = rows.length !== 0;
}
function render(value) {
  snapshot = value;
  byId('dashboard').hidden = false;
  byId('access').hidden = true;
  byId('notice').hidden = true;
  byId('connection').textContent = value.worker.state === 'stopping' ? 'Stopping' : 'Connected';
  byId('worker').textContent = value.worker.state === 'stopping' ? 'Stopping safely' : value.worker.mode === 'shadow' ? 'Running - test mode' : 'Running';
  byId('updated').textContent = 'Updated ' + stamp(value.updated_at);
  byId('stop').disabled = value.worker.state === 'stopping' || stopping;
  renderActivity(value);
  const captures = value.captures;
  byId('capture').textContent = !captures.length ? 'No live sources' : captures.some(source => ['retryable', 'waiting_source', 'needs_review'].includes(source.job_state)) ? 'Capture needs attention' : captures.some(source => source.has_checkpoint) ? 'Live sources active' : 'Waiting for first sample';
  const reasons = {daily_budget: 'Automatic limit reached', incremental_batch: 'Waiting to batch chat updates', deployment_error: 'Update needs attention'};
  byId('publication').textContent = value.worker.mode === 'shadow' ? 'Disabled in test mode' : value.publication ? reasons[value.publication.reason] || (value.publication.state === 'succeeded' ? 'No update pending' : label(value.publication.state)) : 'No update pending';
  const budget = value.budget;
  byId('budget').textContent = budget.used + ' used / ' + budget.limit + ' allowed';
  byId('incremental').textContent = budget.incremental_used + ' used / ' + budget.incremental_limit + ' allowed';
  byId('next-slot').textContent = budget.used >= budget.limit ? budget.next_slot ? stamp(budget.next_slot) : 'Not yet known' : 'Available now';
  let title = 'Worker running';
  let detail = 'Match preparation and website publication are separate. Ready matches appear on the site after a deployment is confirmed.';
  if (value.worker.mode === 'shadow') { title = 'Test mode: website updates are off'; detail = 'The worker can process data, but it will not publish changes to the website.'; }
  else if (value.publication?.reason === 'daily_budget') { title = 'Worker running. Website updates are waiting.'; detail = 'The automatic update limit has been reached. Prepared match data is retained. The next worker slot is ' + (budget.next_slot ? stamp(budget.next_slot) : 'not yet known') + '; Vercel must also allow deployment.'; }
  else if (value.publication?.reason === 'incremental_batch') { title = 'Chat updates are waiting for the next batch'; detail = 'Small chat changes are grouped to save deployments. Newly ready matches can still be published, subject to the automatic limit.'; }
  else if (value.publication?.reason === 'deployment_error' || value.verification?.has_error) { title = 'Website publication needs attention'; detail = 'A website update has not been confirmed. Check GitHub Actions for the deployment error. Match preparation can continue.'; }
  if (value.worker.state === 'stopping') { title = 'Worker is stopping safely'; detail = 'Shutdown is requested. Saved recording checkpoints will be retained.'; }
  byId('summary-title').textContent = title;
  byId('summary-detail').textContent = detail;
  const latest = value.deployments[0];
  byId('release').textContent = latest ? label(latest.state) + ' / ' + stamp(latest.created_at) : 'No release recorded';
  byId('verification').textContent = value.verification ? value.verification.has_error ? 'Not confirmed - check GitHub Actions' : value.verification.state === 'succeeded' ? 'Check completed' : label(value.verification.state) : 'Not checked yet';
  byId('deployments').replaceChildren(...value.deployments.map(release => { const li = document.createElement('li'); li.append(badge(release.state), element('span', stamp(release.updated_at))); return li; }));
  byId('jobs').replaceChildren(...['running', 'queued', 'waiting_source', 'retryable', 'needs_review'].map(state => { const row = document.createElement('div'); row.append(element('dt', label(state)), element('dd', String(value.jobs.find(job => job.state === state)?.count || 0))); return row; }));
  byId('sources').replaceChildren(...(captures.length ? captures.map(source => { const li = document.createElement('li'); const name = source.role === 'watch_party' ? 'Watch party' : source.provider === 'youtube' ? 'Official YouTube' : 'Official Twitch'; li.append(element('span', name), element('span', (source.has_checkpoint ? 'Source updated ' : 'Waiting for sample / ') + stamp(source.updated_at))); return li; }) : [element('li', 'No live sources discovered')]));
  const selected = byId('day').value;
  byId('day').replaceChildren(new Option('Recent days', 'all'), ...[...new Set(value.matches.map(row => row.day))].map(day => new Option(day, day)));
  byId('day').value = [...byId('day').options].some(option => option.value === selected) ? selected : 'all';
  renderMatches();
}
async function refresh() {
  if (refreshing) return;
  if (stopping) {
    try { await fetch('/health', {cache: 'no-store', signal: AbortSignal.timeout(3000)}); }
    catch { byId('connection').textContent = 'Stopped'; byId('worker').textContent = 'Stopped'; byId('notice').textContent = 'Worker stopped. PostgreSQL remains running.'; }
    return;
  }
  if (!token) { byId('access').hidden = false; byId('connection').textContent = 'Authentication required'; return; }
  refreshing = true; byId('refresh').disabled = true;
  try {
    const response = await fetch('/operator-status', {headers: {Authorization: 'Bearer ' + token}, cache: 'no-store', signal: AbortSignal.timeout(10000)});
    if (response.status === 401) { token = ''; byId('access').hidden = false; byId('dashboard').hidden = true; byId('access-error').textContent = 'Status token was not accepted.'; byId('connection').textContent = 'Authentication required'; byId('stop').disabled = true; return; }
    if (!response.ok) throw new Error(response.status === 503 ? 'Database unavailable. Check the launcher logs.' : 'Status request failed. Refresh to try again.');
    render(await response.json());
  } catch (error) {
    byId('connection').textContent = 'Disconnected'; byId('stop').disabled = true;
    byId('notice').hidden = false; byId('notice').textContent = error.name === 'TimeoutError' ? 'Worker did not respond. Last displayed status may be out of date.' : error.message === 'Failed to fetch' ? 'Worker is offline. Last displayed status may be out of date.' : error.message;
  } finally { refreshing = false; byId('refresh').disabled = false; }
}
byId('access').addEventListener('submit', event => { event.preventDefault(); token = byId('token').value.trim(); byId('token').value = ''; refresh(); });
byId('refresh').addEventListener('click', refresh);
byId('day').addEventListener('change', renderMatches);
byId('stop').addEventListener('click', async () => {
  if (!confirm('Stop the worker after its current processing work exits?')) return;
  byId('stop').disabled = true;
  try {
    const response = await fetch('/shutdown', {method: 'POST', headers: {Authorization: 'Bearer ' + token}, signal: AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error('Worker could not be stopped. Refresh and try again.');
    stopping = true; byId('connection').textContent = 'Stopping'; byId('worker').textContent = 'Stopping'; byId('notice').hidden = false; byId('notice').textContent = 'Shutdown requested. Saved checkpoints will be retained.';
  } catch (error) { byId('notice').hidden = false; byId('notice').textContent = error.message; byId('stop').disabled = false; }
});
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
