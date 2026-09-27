const http = require('node:http');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');

const job = {id: 'a'.repeat(32), label: 'UI verification fixture', status: 'ready', created: 1,
  message: 'Index ready · review accuracy before relying on it', videoId: 'ZphbktbT26k', progress: 100,
  warnings: [], rounds: [{map: 1, round: 1, start: 100.5, verified: false}, {map: 1, round: 2, start: 250, verified: false}]};
const files = {'/': ['index.html', 'text/html'], '/app.js': ['app.js', 'text/javascript'], '/style.css': ['style.css', 'text/css']};
http.createServer(async (request, response) => {
  let value;
  let status = 200;
  if (request.url === '/api/state') value = {token: 'fixture', active: null, jobs: [job]};
  else if (request.url === '/api/export/' + job.id) value = {schemaVersion: 1, videoId: job.videoId, leadSeconds: 5, rounds: job.rounds};
  else if (request.url === '/api/review/' + job.id && request.method === 'POST') {
    let body = '';
    for await (const chunk of request) body += chunk;
    const update = JSON.parse(body);
    if (update.action === 'add') {
      job.rounds.push({map: update.map, round: update.round, start: update.start, verified: true});
      job.rounds.sort((a, b) => a.start - b.start);
    } else if (update.action === 'exclude' || update.action === 'include') {
      job.rounds[update.index].excluded = update.action === 'exclude';
    } else job.rounds[update.index] = {...job.rounds[update.index], start: update.start, verified: true};
    value = {ok: true};
  } else if (files[request.url]) {
    const [file, type] = files[request.url];
    response.writeHead(200, {'Content-Type': type});
    response.end(readFileSync(resolve(__dirname, '../indexer/web', file)));
    return;
  } else {status = 404; value = {error: 'Fixture endpoint not available'};}
  response.writeHead(status, {'Content-Type': 'application/json'});
  response.end(JSON.stringify(value));
}).listen(8767, '127.0.0.1', () => process.stdout.write('Studio UI fixture: http://127.0.0.1:8767\n'));
