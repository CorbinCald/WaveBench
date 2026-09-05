const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const html = readFileSync(require('node:path').join(__dirname, 'gpt6AstraPro.html'), 'utf8');
const source = html.slice(html.indexOf('  class TaskQueue'), html.indexOf('  class BrowserCache')) + html.slice(html.indexOf('  const osmQueue'), html.indexOf('  const heightStops'));
function client(fetch) {
  let now = Date.now();
  const saved = new Map();
  const context = vm.createContext({
    fetch, AbortController, URL, setTimeout, clearTimeout,
    Date: {now: () => now}, sleep: async ms => { now += ms; },
    cache: {get: async key => saved.get(key), put: (key, value) => saved.set(key, value)}
  });
  vm.runInContext(source + '\nglobalThis.api = {fetchOSM, fetchJSON, state: () => ({error: osmLastError, label: osmRequestLabel})};', context);
  return {...context.api, saved, advance: ms => {now += ms;}};
}
const ok = {elements: [{type: 'way', id: 10197618}]};
const response = data => ({ok: true, json: async () => data});
test('failed primary falls back; later roads/buildings reuse the healthy service and cache', async () => {
  const calls = [];
  const c = client(async (url, options) => {
    calls.push(url);
    assert.equal(options.method, 'POST');
    assert.equal(decodeURIComponent(options.body.slice(5)), '[out:json];way[highway];out geom;');
    if (url.includes('overpass-api.de')) return {ok: false, status: 504};
    return response(ok);
  });
  const query = '[out:json];way[highway];out geom;';
  assert.equal(await c.fetchOSM(query, 'roads', 0, 'local roads'), ok);
  assert.equal(await c.fetchOSM(query, 'buildings', 3, 'buildings'), ok);
  assert.equal(await c.fetchOSM(query, 'roads', 0, 'local roads'), ok);
  assert.equal(calls.length, 3);
  assert.ok(calls[1].includes('maps.mail.ru'));
  assert.equal(calls[1], calls[2]);
  assert.equal(c.state().error, '');
  assert.equal(c.state().label, '');
});
test('all-service outage reports hosts, avoids queued retry storms, then recovers', async () => {
  let calls = 0, available = false;
  const c = client(async () => { calls++; if (!available) throw new TypeError('Failed to fetch'); return response(ok); });
  await assert.rejects(c.fetchOSM('query', 'roads', 0, 'roads'), /overpass-api.de: Failed to fetch.*maps.mail.ru: Failed to fetch.*overpass.private.coffee: Failed to fetch/);
  assert.equal(calls, 3);
  assert.equal(c.saved.size, 0);
  await assert.rejects(c.fetchOSM('query', 'buildings', 3, 'buildings'));
  assert.equal(calls, 3);
  assert.equal(c.state().label, '');
  c.advance(61000); available = true;
  assert.equal(await c.fetchOSM('query', 'roads', 0, 'roads'), ok);
  assert.equal(calls, 4);
});
test('partial Overpass runtime-error responses are not cached as successful maps', async () => {
  let calls = 0;
  const c = client(async () => response(++calls === 1 ? {...ok, remark: 'runtime error: Query timed out'} : ok));
  assert.equal(await c.fetchOSM('query', 'roads', 0, 'roads'), ok);
  assert.equal(calls, 2);
  assert.equal(c.saved.get('roads'), ok);
});
test('missing elements fails over to a valid response', async () => {
  let calls = 0;
  const c = client(async () => response(++calls === 1 ? {} : ok));
  assert.equal(await c.fetchOSM('query', 'roads', 0, 'roads'), ok);
  assert.equal(calls, 2);
});
test('timeouts report the duration instead of an opaque AbortError', async () => {
  const c = client(async (url, {signal}) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason))));
  await assert.rejects(c.fetchJSON('https://example.test', {}, 5), /Request timed out after 0.005s/);
});
