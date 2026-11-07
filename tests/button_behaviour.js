// Runs the example button in Node with a fake page. Prints one JSON line per scenario.
'use strict';

const fs = require('fs');
const vm = require('vm');

const source = fs.readFileSync(process.argv[2], 'utf8');

function page(options) {
  const store = new Map(Object.entries(options.stored || {}));
  const sent = [];
  const prompts = [];
  const nowPlaying = options.nowPlaying;
  const window = {
    location: { href: options.pageUrl || 'https://media.example.com/web/', origin: options.origin || 'https://media.example.com' },
    localStorage: {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, value),
      removeItem: (key) => store.delete(key),
    },
    prompt: (text) => {
      prompts.push(text);
      return options.promptAnswer === undefined ? null : options.promptAnswer;
    },
    setTimeout: () => 0,
    ApiClient: options.noApi ? undefined : {
      deviceId: () => 'device-1',
      getCurrentUserId: () => 'user-1',
      accessToken: () => 'media-session',
      getSessions: () => Promise.resolve(nowPlaying === null ? [{}] : [{ NowPlayingItem: nowPlaying }]),
      getLiveTvChannel: () => Promise.resolve({ ChannelNumber: options.lookedUpNumber }),
    },
    fetch: (url, init) => {
      sent.push({ url, method: init.method, credentials: init.credentials, authorization: init.headers.Authorization, body: init.body });
      if (options.fetchFails) {
        return Promise.reject(new Error('network'));
      }
      return Promise.resolve({
        status: options.status || 200,
        headers: { get: (name) => (name === 'Retry-After' ? options.retryAfter || null : null) },
        json: () => (options.notJson ? Promise.reject(new Error('html')) : Promise.resolve({ result: options.result || 'rotated' })),
      });
    },
  };
  const context = { window, URL, Promise, JSON, String, document: { body: null }, console };
  let code = source;
  if (options.flagUrl) {
    code = code.replace("'https://dispatcharr.example.com/api/stream-demote/flag/'", JSON.stringify(options.flagUrl));
  }
  if (options.useGateway) {
    code = code.replace('useGateway: false', 'useGateway: true');
  }
  vm.runInNewContext(code, context);
  return { window, sent, prompts, store };
}

const TV = { Type: 'TvChannel', Id: 'item', ChannelNumber: '101' };
const KEY = 'streamDemote.flagToken';

async function run(name, options) {
  const p = page(options);
  const message = await p.window.StreamDemoteFlag.flag();
  console.log(JSON.stringify({ name, message, sent: p.sent, prompts: p.prompts.length, stored: p.store.get(KEY) || null }));
}

function element(tag) {
  const node = {
    tag, children: [], style: {}, attrs: {}, listeners: {}, className: '', disabled: false,
    setAttribute(name, value) { node.attrs[name] = value; },
    addEventListener(name, fn) { node.listeners[name] = fn; },
    appendChild(child) { node.children.push(child); child.parentNode = node; return child; },
    insertBefore(child) { node.children.unshift(child); child.parentNode = node; return child; },
    removeChild(child) { node.children = node.children.filter((item) => item !== child); },
    querySelector(selector) {
      const wanted = selector.replace('.', '');
      return node.children.find((child) => child.className.split(' ').includes(wanted)) || null;
    },
  };
  return node;
}

// Runs the script on a fake player page, then reports what the controls ended up showing.
async function runOnPage(name, nowPlaying) {
  const row = element('div');
  const body = element('body');
  const shown = [];
  const add = body.appendChild;
  body.appendChild = (child) => { shown.push(child.textContent); return add(child); };
  const document = {
    body,
    createElement: element,
    querySelector: (selector) => (selector.includes('videoOsdBottom') ? row : null),
  };
  const sent = [];
  let wake = null;
  const window = {
    location: { href: 'https://media.example.com/web/', origin: 'https://media.example.com' },
    localStorage: { getItem: () => 'sdf_abc', setItem: () => {}, removeItem: () => {} },
    setTimeout: (fn) => { fn(); return 0; },
    MutationObserver: class { constructor(fn) { wake = fn; } observe() {} },
    ApiClient: {
      deviceId: () => 'device-1',
      getCurrentUserId: () => 'user-1',
      accessToken: () => 'media-session',
      getSessions: () => Promise.resolve([{ NowPlayingItem: nowPlaying }]),
      getLiveTvChannel: () => Promise.resolve({}),
    },
    fetch: (url, init) => {
      sent.push(init.body);
      return Promise.resolve({ status: 200, json: () => Promise.resolve({ result: 'rotated' }) });
    },
  };
  vm.runInNewContext(source, { window, document, URL, Promise, JSON, String, Date, console });
  await new Promise((resolve) => setImmediate(resolve));
  wake();
  await new Promise((resolve) => setImmediate(resolve));
  const button = row.children[0];
  const result = { name, buttons: row.children.length, display: button.style.display, title: button.title };
  button.listeners.click();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  result.sent = sent;
  result.toasts = shown;
  console.log(JSON.stringify(result));
}

(async () => {
  await runOnPage('page_live_tv', TV);
  await runOnPage('page_movie', { Type: 'Movie', Id: 'm' });
  await run('token_saved', { nowPlaying: TV, stored: { [KEY]: 'sdf_abc' } });
  await run('prompts_once', { nowPlaying: TV, promptAnswer: ' sdf_new ' });
  await run('prompt_cancelled', { nowPlaying: TV });
  await run('not_live_tv', { nowPlaying: { Type: 'Movie', Id: 'm' }, stored: { [KEY]: 'sdf_abc' } });
  await run('nothing_playing', { nowPlaying: null, stored: { [KEY]: 'sdf_abc' } });
  await run('number_looked_up', { nowPlaying: { Type: 'TvChannel', Id: 'item' }, lookedUpNumber: '7.1', stored: { [KEY]: 'sdf_abc' } });
  await run('refused_token_is_forgotten', { nowPlaying: TV, stored: { [KEY]: 'sdf_old' }, status: 401, result: 'unauthorized' });
  await run('plain_http_is_refused', { nowPlaying: TV, stored: { [KEY]: 'sdf_abc' }, flagUrl: 'http://dispatcharr.example.com/api/stream-demote/flag/' });
  await run('not_signed_in', { noApi: true });
  await run('html_reply', { nowPlaying: TV, stored: { [KEY]: 'sdf_abc' }, notJson: true });
  await run('network_failure', { nowPlaying: TV, stored: { [KEY]: 'sdf_abc' }, fetchFails: true });
  await run('limit_says_how_long', { nowPlaying: TV, stored: { [KEY]: 'sdf_abc' }, status: 429, result: 'rate_limited', retryAfter: '1800' });
  await run('limit_a_minute', { nowPlaying: TV, stored: { [KEY]: 'sdf_abc' }, status: 429, result: 'rate_limited', retryAfter: '41' });
  await run('limit_with_no_header', { nowPlaying: TV, stored: { [KEY]: 'sdf_abc' }, status: 429, result: 'rate_limited' });
  await run('gateway_same_origin', { nowPlaying: TV, useGateway: true, flagUrl: '/flag-stream' });
  await run('gateway_other_origin_is_refused', { nowPlaying: TV, useGateway: true, flagUrl: 'https://elsewhere.example.com/flag' });
})();
