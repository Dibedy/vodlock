const http = require('node:http');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const routes = {
  '/': ['tests/vodspace-fixture.html', 'text/html'],
  '/vodlock/content.css': ['vodlock/content.css', 'text/css'],
  '/vodlock/vodspace.css': ['vodlock/vodspace.css', 'text/css'],
  '/vodspace-test.js': ['vodlock/vodspace.js', 'text/javascript']
};
http.createServer((request, response) => {
  const route = routes[request.url];
  if (!route) { response.writeHead(404); response.end(); return; }
  let source = readFileSync(resolve(__dirname, '..', route[0]), 'utf8');
  if (request.url === '/vodspace-test.js') source = source.replace('location.hostname', "'vods.space'");
  response.writeHead(200, {'Content-Type': route[1]});
  response.end(source);
}).listen(8765, '127.0.0.1', () => console.log('VODLOCK fixture: http://127.0.0.1:8765'));
