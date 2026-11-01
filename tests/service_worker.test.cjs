// Run with: node --test tests/service_worker.test.cjs (no packages required).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function worker() {
  const listeners = {};
  const stored = new Map();
  const fetched = [];
  const deleted = [];
  const cache = {
    async addAll(paths) {
      for (const path of paths) {
        assert.ok(path.startsWith('/static/'));
        assert.ok(fs.existsSync(`app${path}`), path);
        stored.set(path, {cached: path});
      }
    },
    async match(path) { return stored.get(path); },
    // Any attempted runtime cache write fails the test immediately.
    put() { assert.fail('Runtime cache writes are forbidden'); }
  };
  let offline = false;
  const context = {
    URL,
    self: {
      location: {origin: 'https://orbit.example'},
      clients: {async claim() {}},
      addEventListener(name, handler) { listeners[name] = handler; }
    },
    caches: {
      async open(name) {
        assert.equal(name, 'orbit-shell-v1');
        return cache;
      },
      async keys() { return ['orbit-shell-v1', 'orbit-shell-old', 'unrelated-app']; },
      async delete(name) { deleted.push(name); }
    },
    async fetch(request) {
      fetched.push(request);
      if (offline) throw new Error('Network unavailable');
      return {network: request.url};
    }
  };
  vm.runInNewContext(fs.readFileSync('app/static/sw.js', 'utf8'), context);
  async function lifecycle(name) {
    let pending;
    listeners[name]({waitUntil(promise) { pending = promise; }});
    await pending;
  }
  function request(path, method = 'GET', mode = 'cors') {
    let response;
    listeners.fetch({
      request: {url: new URL(path, context.self.location.origin).href, method, mode},
      respondWith(promise) { response = promise; }
    });
    return response;
  }
  return {stored, fetched, deleted, lifecycle, request, goOffline() { offline = true; }};
}

test('installation stores only the explicit shell, activation removes old Orbit caches', async () => {
  const sw = worker();
  await sw.lifecycle('install');
  assert.equal(sw.stored.size, 13);
  assert.deepEqual([...sw.stored.keys()].filter(path => path.endsWith('.html')), ['/static/offline.html']);
  await sw.lifecycle('activate');
  assert.deepEqual(sw.deleted, ['orbit-shell-old']);
});

test('all private routes, cross-origin traffic, and mutations bypass the cache', async () => {
  const sw = worker();
  await sw.lifecycle('install');
  for (const path of ['/dashboard', '/account/1', '/paycheck', '/almanac',
    '/dashboard/chart', '/export/almanac-years.csv', '/static/orbit.css?private=1',
    'https://other.example/static/orbit.css']) {
    assert.equal(sw.request(path), undefined, path);
  }
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    assert.equal(sw.request('/static/orbit.css', method), undefined);
    assert.equal(sw.request('/paycheck/upload', method, 'navigate'), undefined);
  }
  assert.equal(sw.fetched.length, 0);
  assert.equal(sw.stored.size, 13);
});

test('navigation always uses network and falls back only to the static offline page', async () => {
  const sw = worker();
  await sw.lifecycle('install');
  assert.deepEqual(await sw.request('/dashboard', 'GET', 'navigate'),
    {network: 'https://orbit.example/dashboard'});
  // A static resource opened as a page must also use the network.
  assert.deepEqual(await sw.request('/static/orbit.css', 'GET', 'navigate'),
    {network: 'https://orbit.example/static/orbit.css'});
  sw.goOffline();
  assert.deepEqual(await sw.request('/paycheck', 'GET', 'navigate'),
    {cached: '/static/offline.html'});
  assert.equal(sw.stored.size, 13);
});

test('shell uses the network first, the installed copy offline, and never stores at runtime', async () => {
  const sw = worker();
  await sw.lifecycle('install');
  assert.deepEqual(await sw.request('/static/charts.js'),
    {network: 'https://orbit.example/static/charts.js'});
  sw.goOffline();
  assert.deepEqual(await sw.request('/static/charts.js'), {cached: '/static/charts.js'});
  assert.equal(sw.stored.size, 13);
});
