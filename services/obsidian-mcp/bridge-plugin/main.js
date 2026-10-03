'use strict';

const obsidian = require('obsidian');
const { Plugin, FileSystemAdapter, TFile, TFolder } = obsidian;
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');

const VERSION = 1;
const MAX_REQUEST = 384 * 1024;
const MAX_RESPONSE = 1024 * 1024;
const MAX_NOTE = 256 * 1024;
const MAX_APPEND = 64 * 1024;
const MAX_MEDIA = 512 * 1024;
const MAX_LIST = 1000;
const MAX_SEARCH_FILES = 1000;
const MAX_SEARCH_BYTES = 4 * 1024 * 1024;
const MAX_RECEIPTS = 64;
const RECEIPT_TTL_MS = 10 * 60 * 1000;
const activeMutations = globalThis[Symbol.for('obsidian.bridge.activeMutations')] ||= { count: 0 };
const MIME = Object.freeze({ '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif' });

class BridgeError extends Error {
  constructor(code) { super(code); this.code = code; }
}
function fail(code) { throw new BridgeError(code); }
function shape(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).every(k => keys.includes(k)) && keys.every(k => Object.hasOwn(value, k));
}
function string(value, max = 4096) {
  if (typeof value !== 'string' || !value || Buffer.byteLength(value) > max || /[\u0000-\u001f\u007f]/.test(value)) fail('invalid_request');
  return value;
}
function revision(text) { return `sha256:${crypto.createHash('sha256').update(text, 'utf8').digest('hex')}`; }
function mutationDigest(config, client, operation, mutation) {
  if (typeof client !== 'string' || !/^[a-z][a-z0-9_-]{0,63}$/.test(client) ||
      !['create', 'append'].includes(operation) ||
      !shape(mutation, operation === 'create' ? ['path', 'content'] : ['path', 'content', 'expected_revision'])) fail('invalid_request');
  string(mutation.path, 1024);
  if (mutation.path.startsWith('/') || mutation.path.includes('\\') || mutation.path.includes('%') ||
      mutation.path.split('/').some(part => !part || part.startsWith('.'))) fail('invalid_path');
  if (!mutation.path.toLowerCase().endsWith('.md') || typeof mutation.content !== 'string' ||
      Buffer.byteLength(mutation.content) > (operation === 'create' ? MAX_NOTE : MAX_APPEND) ||
      (operation === 'append' && !/^sha256:[0-9a-f]{64}$/.test(mutation.expected_revision))) fail('invalid_request');
  return crypto.createHash('sha256').update(JSON.stringify([
    config.id, client, operation, mutation.path, mutation.content,
    operation === 'append' ? mutation.expected_revision : null,
  ])).digest('hex');
}
function receiptView(receipt, entry) {
  return { receipt, status: entry ? (entry.state === 'committed' ? 'committed' :
    entry.state === 'failed' ? 'failed' : entry.state === 'indeterminate' ? 'indeterminate' : 'pending') :
    'indeterminate', result: entry?.result || null, error: entry?.error || null };
}
function canonicalRoot(root) {
  if (typeof root !== 'string' || !path.isAbsolute(root) || path.normalize(root) !== root) fail('unavailable');
  const real = fs.realpathSync(root);
  if (real !== root || !fs.lstatSync(root).isDirectory()) fail('unavailable');
  return real;
}
function identity() {
  if (process.getuid() !== 1000 || process.getgid() !== 1000) fail('unavailable');
  const status = fs.readFileSync('/proc/self/status', 'utf8');
  const caps = /^CapEff:\s*([0-9a-f]+)$/mi.exec(status);
  if (!caps || BigInt(`0x${caps[1]}`) !== 0n) fail('unavailable');
}
function verifyRoot(app, config) {
  try {
    if (!(app.vault.adapter instanceof FileSystemAdapter) ||
        app.vault.adapter.getBasePath() !== config.root || app.vault.getName() !== config.appName ||
        canonicalRoot(config.root) !== config.root || path.dirname(config.root) !== config.parent) fail('unavailable');
    identity();
    const current = fs.lstatSync(config.root);
    if (current.dev !== config.rootDev || current.ino !== config.rootIno) fail('unavailable');
  } catch { fail('unavailable'); }
}
function socketAddress(config) {
  return '\0obsidian-bridge-' + crypto.createHash('sha256')
    .update(config.socket + '\0' + config.token).digest('hex');
}
function privateFile(filename, directory, maxBytes = 4096) {
  if (typeof filename !== 'string' || path.dirname(filename) !== directory) fail('unavailable');
  const stat = fs.lstatSync(filename);
  if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o600 || stat.size > maxBytes) fail('unavailable');
  return fs.readFileSync(filename, 'utf8');
}
function privateDirectory(directory) {
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o700 ||
      fs.realpathSync(directory) !== directory) fail('unavailable');
}
const VAULT_NAME = /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}$/;
const PLUGIN_ID = 'obsidian-private-bridge';
const MAX_VAULTS = 16;
function vaultSlug(name) {
  let id = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48);
  if (!/^[a-z]/.test(id)) id = `v-${id}`.slice(0, 48);
  return id.replace(/-+$/, '');
}
function writePrivate(filename, data) {
  const temporary = `${filename}.${crypto.randomBytes(6).toString('hex')}`;
  fs.writeFileSync(temporary, data, { mode: 0o600, flag: 'wx' });
  fs.renameSync(temporary, filename);
}
function registration(directory, id) {
  const runtime = path.join(directory, id);
  try {
    privateDirectory(runtime);
    const endpoint = JSON.parse(privateFile(path.join(runtime, 'endpoint.json'), runtime));
    if (!endpoint || typeof endpoint !== 'object' || typeof endpoint.app_root !== 'string') return null;
    const token = privateFile(path.join(runtime, 'credential'), runtime).trim();
    return { endpoint, token, socket: path.join(runtime, `${id}.sock`) };
  } catch { return null; }
}
function alive(socketPath, token) {
  // Another window already serves this vault if its registered address still answers.
  return new Promise(resolve => {
    const address = '\0obsidian-bridge-' + crypto.createHash('sha256').update(socketPath + '\0' + token).digest('hex');
    const probe = net.createConnection(address);
    const done = value => { probe.destroy(); resolve(value); };
    probe.setTimeout(500, () => done(false));
    probe.on('connect', () => done(true));
    probe.on('error', () => done(false));
  });
}
// Every vault window registers itself: <runtime>/<vault_id>/{credential,endpoint.json}.
async function bootstrap(app) {
  if (process.platform !== 'linux' || process.env.OBSIDIAN_BRIDGE_INSTANCE !== 'kubernetes-obsidian') return null;
  const [nodeMajor, nodeMinor] = process.versions.node.split('.').map(Number);
  if (nodeMajor < 20 || (nodeMajor === 20 && nodeMinor < 8)) return null;
  const directory = process.env.OBSIDIAN_BRIDGE_DIR;
  const parentSetting = process.env.OBSIDIAN_BRIDGE_VAULT_PARENT;
  const appConfig = process.env.OBSIDIAN_BRIDGE_APP_CONFIG || null;
  for (const value of [directory, parentSetting]) {
    if (!value || !path.isAbsolute(value) || path.normalize(value) !== value) return null;
  }
  if (appConfig !== null && (!path.isAbsolute(appConfig) || path.normalize(appConfig) !== appConfig)) return null;
  try {
    privateDirectory(directory);
    identity();
    if (!(app.vault.adapter instanceof FileSystemAdapter)) return null;
    const appRoot = canonicalRoot(app.vault.adapter.getBasePath());
    const parent = canonicalRoot(parentSetting);
    const name = app.vault.getName();
    if (path.dirname(appRoot) !== parent || name !== path.basename(appRoot) || !VAULT_NAME.test(name) ||
        directory === appRoot || directory.startsWith(appRoot + path.sep) || appRoot.startsWith(directory + path.sep)) return null;
    let id = vaultSlug(name);
    if (!/^[a-z][a-z0-9_-]{0,63}$/.test(id)) return null;
    let existing = registration(directory, id);
    if (existing && existing.endpoint.app_root !== appRoot) {
      id = `${id}-${crypto.createHash('sha256').update(appRoot).digest('hex').slice(0, 8)}`;
      existing = registration(directory, id);
    }
    if (existing && existing.endpoint.app_root === appRoot && await alive(existing.socket, existing.token)) return null;
    const runtime = path.join(directory, id);
    if (!fs.existsSync(runtime)) fs.mkdirSync(runtime, { mode: 0o700 });
    privateDirectory(runtime);
    const token = crypto.randomBytes(32).toString('base64url');
    writePrivate(path.join(runtime, 'credential'), token);
    writePrivate(path.join(runtime, 'endpoint.json'),
      JSON.stringify({ version: VERSION, vault_id: id, app_name: name, app_root: appRoot }));
    const rootStat = fs.lstatSync(appRoot);
    return { id, appName: name, root: appRoot, parent, appConfig, runtime,
      rootDev: rootStat.dev, rootIno: rootStat.ino, socket: path.join(runtime, `${id}.sock`), token };
  } catch { return null; }
}
function safePath(value, root, create = false) {
  string(value, 1024);
  if (value.startsWith('/') || value.includes('\\') || value.includes('%') || value.includes('//')) fail('invalid_path');
  const parts = value.split('/');
  if (parts.some(p => !p || p === '.' || p === '..' || p.startsWith('.'))) fail('invalid_path');
  let cursor = root;
  for (const part of parts) {
    cursor = path.join(cursor, part);
    try {
      const stat = fs.lstatSync(cursor);
      if (stat.isSymbolicLink() || fs.realpathSync(cursor) !== cursor) fail('invalid_path');
    } catch (error) {
      if (error instanceof BridgeError) throw error;
      if (error.code !== 'ENOENT' || !create) fail('invalid_path');
    }
  }
  if (!cursor.startsWith(root + path.sep)) fail('invalid_path');
  return value;
}
function note(app, root, requested) {
  const key = safePath(requested, root);
  if (!key.toLowerCase().endsWith('.md')) fail('invalid_path');
  const file = app.vault.getAbstractFileByPath(key);
  if (!(file instanceof TFile) || file.path !== key) fail('not_found');
  return file;
}
function argument(args, keys) { if (!shape(args, keys)) fail('invalid_request'); }
function limit(value, max) { if (!Number.isInteger(value) || value < 1 || value > max) fail('invalid_request'); return value; }
async function freshSize(app, config, file, max) {
  verifyRoot(app, config);
  safePath(file.path, config.root);
  const stat = await app.vault.adapter.stat(file.path);
  verifyRoot(app, config);
  safePath(file.path, config.root);
  if (!stat || !Number.isSafeInteger(stat.size) || stat.size < 0) fail('unavailable');
  if (stat.size > max) fail('limit_exceeded');
  return stat.size;
}

const MAX_TREE = 5000;
const MAX_LOGS = 500;
const SETTINGS_FILES = new Set(['app', 'appearance', 'core-plugins', 'community-plugins', 'hotkeys',
  'graph', 'daily-notes', 'templates', 'bookmarks']);
const SECRET_KEY = /(key|token|secret|password|passwd|pwd|auth|credential|cookie|session|bearer|jwt|passphras|private|signature|dsn|otp)/i;
// Value scrubber: anything that looks like a credential is replaced before an agent sees it.
const SECRET_VALUES = [
  [/([a-z][a-z0-9+.-]*:\/\/)[^\s/@:]+:[^\s/@]+@/gi, '$1[redacted]@'],
  [/([?&#](?:access_token|token|key|api_key|apikey|sig|signature|auth|code|secret|password)=)[^&#\s]+/gi, '$1[redacted]'],
  [/\b(Bearer|Basic|token)\s+[A-Za-z0-9._~+\/=-]{8,}/gi, '$1 [redacted]'],
  [/\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}/g, '[redacted-jwt]'],
  [/\b(gh[pousr]_|github_pat_|sk-|sk_live_|xox[abprs]-|AKIA|ASIA)[A-Za-z0-9_-]{8,}/g, '[redacted]'],
  [/\b[A-Fa-f0-9]{32,}\b/g, '[redacted-hex]'],
  [/[A-Za-z0-9+_-]{40,}={0,2}/g, '[redacted-base64]'],
];
function scrub(text) {
  let out = String(text);
  for (const [pattern, replacement] of SECRET_VALUES) out = out.replace(pattern, replacement);
  return out;
}
function describe(value) {
  // Bounded, no deep serialization: a huge or cyclic object must not stall the window.
  if (value instanceof Error) return `${value.name}: ${value.message}`;
  if (value === null || ['string', 'number', 'boolean', 'undefined'].includes(typeof value)) return String(value);
  if (Array.isArray(value)) return `[array of ${value.length}]`;
  try { return `{${Object.keys(value).slice(0, 10).join(', ')}}`; } catch { return '[object]'; }
}
// Ring buffer of this window's errors and warnings since the bridge loaded, for read_logs.
const consoleLog = globalThis[Symbol.for('obsidian.bridge.consoleLog')] ||= (() => {
  const entries = [];
  const record = (level, values) => {
    const message = scrub(values.map(describe).join(' ')).slice(0, 1000);
    entries.push({ time: new Date().toISOString(), level, message });
    if (entries.length > MAX_LOGS) entries.shift();
  };
  for (const level of ['error', 'warn']) {
    const original = console[level];
    if (typeof original !== 'function') continue;
    console[level] = (...values) => { try { record(level, values); } catch { /* never break logging */ } return original.apply(console, values); };
  }
  if (globalThis.addEventListener) {
    globalThis.addEventListener('error', event => { try { record('error', [event.message || 'error']); } catch { /* ignore */ } });
    globalThis.addEventListener('unhandledrejection', event => { try { record('error', [event.reason || 'unhandled rejection']); } catch { /* ignore */ } });
  }
  return entries;
})();
function redacted(value, depth = 0) {
  // Settings can hold plugin credentials; agents never receive them.
  if (depth > 8) return '[nested]';
  if (Array.isArray(value)) return value.slice(0, 500).map(item => redacted(item, depth + 1));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).slice(0, 500).map(([key, item]) =>
      // A bare "key" (hotkeys use it for the pressed key) is not a secret by name; its value
      // still goes through the value scrubber.
      [key, key.toLowerCase() !== 'key' && SECRET_KEY.test(key) ? '[redacted]' : redacted(item, depth + 1)]));
  }
  if (typeof value === 'string') return scrub(value.length > 2000 ? `${value.slice(0, 2000)}...` : value);
  return value;
}
function pluginRecord(app, root, id) {
  // State an approval covers: install, enabled flag, version and the installed bytes' digest.
  if (!/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(id)) fail('invalid_request');
  const manifests = (app.plugins && app.plugins.manifests) || {};
  const manifest = manifests[id];
  const enabled = app.plugins && app.plugins.enabledPlugins instanceof Set && app.plugins.enabledPlugins.has(id);
  if (!manifest) return { plugin_id: id, installed: false, enabled: false, version: '', digest: '' };
  const hash = crypto.createHash('sha256');
  for (const name of ['manifest.json', 'main.js', 'styles.css']) {
    const file = path.join(root, '.obsidian', 'plugins', id, name);
    try {
      const stat = fs.lstatSync(file);
      if (stat.isFile()) hash.update(`${name}\u0000`).update(fs.readFileSync(file));
    } catch { /* optional file */ }
  }
  return { plugin_id: id, installed: true, enabled: Boolean(enabled), version: String(manifest.version || ''), digest: hash.digest('hex') };
}
function settingTarget(root, settingId) {
  // "<file>.<key>": a top-level key in app.json, appearance.json or a plugin's data.json.
  const match = /^(app|appearance|plugin:[a-z0-9][a-z0-9_-]{0,63})\.([A-Za-z_][A-Za-z0-9_-]{0,127})$/i.exec(settingId || '');
  if (!match) fail('invalid_request');
  const [, file, key] = match;
  if (key.toLowerCase() !== 'key' && SECRET_KEY.test(key)) fail('invalid_request');
  if (['__proto__', 'constructor', 'prototype'].includes(key)) fail('invalid_request');
  // The bridge's own settings and anything that loads code or lifts restrictions stay with the owner.
  if (file.toLowerCase() === `plugin:${PLUGIN_ID}`) fail('invalid_request');
  if (!file.startsWith('plugin:') && /(plugin|snippet|safe|restrict)/i.test(key)) fail('invalid_request');
  const relative = file.startsWith('plugin:') ? `plugins/${file.slice(7)}/data.json` : `${file}.json`;
  return { file, key, filename: path.join(root, '.obsidian', relative), plugin: file.startsWith('plugin:') ? file.slice(7) : null };
}
function readSettingFile(root, filename) {
  const directory = path.dirname(filename);
  try {
    if (fs.realpathSync(directory) !== directory) fail('invalid_path');
  } catch (error) { if (error instanceof BridgeError) throw error; fail('not_found'); }
  let descriptor;
  try { descriptor = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW); }
  catch (error) { if (error && error.code === 'ENOENT') return { raw: '', data: {} }; fail('invalid_path'); }
  try {
    const stat = fs.fstatSync(descriptor);
    if (!stat.isFile() || stat.size > MAX_NOTE) fail('invalid_path');
    const raw = fs.readFileSync(descriptor, 'utf8');
    let data;
    try { data = JSON.parse(raw); } catch { fail('conflict'); }
    if (!data || typeof data !== 'object' || Array.isArray(data)) fail('conflict');
    return { raw, data };
  } finally { fs.closeSync(descriptor); }
}
function settingState(root, settingId) {
  const target = settingTarget(root, settingId);
  const { raw, data } = readSettingFile(root, target.filename);
  const value = Object.hasOwn(data, target.key) ? data[target.key] : null;
  const shown = value === null || ['string', 'number', 'boolean'].includes(typeof value) ? value : '[complex value]';
  return { setting_id: settingId, value: typeof shown === 'string' ? scrub(shown).slice(0, 2000) : shown,
    revision: revision(raw) };
}

// Community plugin installs: the catalog and release files come from GitHub, exactly like the
// app's own browser. Bytes are hashed for the owner's approval and pinned for the install.
const CATALOG_URL = 'https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json';
const RELEASE_FILES = ['manifest.json', 'main.js', 'styles.css'];
const MAX_PACKAGE = 8 * 1024 * 1024;
const PIN_TTL_MS = 2 * 60 * 1000;
const packagePins = new Map();
let catalogCache = null;
async function fetchBytes(url, optional = false) {
  if (typeof obsidian.requestUrl !== 'function') fail('unavailable');
  // Check the size first so an oversized asset is never buffered.
  try {
    const head = await obsidian.requestUrl({ url, method: 'HEAD', throw: false });
    const length = Number(head && head.headers && (head.headers['content-length'] || head.headers['Content-Length']));
    if (Number.isFinite(length) && length > MAX_PACKAGE) fail('limit_exceeded');
  } catch (error) { if (error instanceof BridgeError) throw error; }
  let response;
  try { response = await obsidian.requestUrl({ url, method: 'GET', throw: false }); } catch { fail('unavailable'); }
  if (response.status === 404 && optional) return null;
  if (response.status !== 200) fail(response.status === 404 ? 'not_found' : 'unavailable');
  const bytes = Buffer.from(response.arrayBuffer);
  if (bytes.length > MAX_PACKAGE) fail('limit_exceeded');
  return bytes;
}
async function catalog() {
  if (catalogCache && Date.now() - catalogCache.at < 60 * 60 * 1000) return catalogCache.entries;
  let entries;
  try { entries = JSON.parse((await fetchBytes(CATALOG_URL)).toString('utf8')); } catch (error) {
    if (error instanceof BridgeError) throw error; fail('unavailable');
  }
  if (!Array.isArray(entries)) fail('unavailable');
  catalogCache = { at: Date.now(), entries };
  return entries;
}
async function catalogEntry(id) {
  if (typeof id !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,63}$/i.test(id) || id === PLUGIN_ID) fail('invalid_request');
  const entry = (await catalog()).find(item => item && item.id === id);
  if (!entry || typeof entry.repo !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(entry.repo) ||
      entry.repo.split('/').some(part => /^\.+$/.test(part))) fail('not_found');
  return entry;
}
async function fetchPackage(id, version) {
  const entry = await catalogEntry(id);
  const base = `https://github.com/${entry.repo}/releases`;
  if (version === 'latest') {
    // Same as Obsidian's own installer: the version in the repo's default-branch manifest.json
    // (GitHub's "latest release" can be a different, e.g. beta, plugin).
    let manifest;
    try { manifest = JSON.parse((await fetchBytes(`https://raw.githubusercontent.com/${entry.repo}/HEAD/manifest.json`)).toString('utf8')); }
    catch (error) { if (error instanceof BridgeError) throw error; fail('not_found'); }
    if (!manifest || manifest.id !== id) fail('conflict');
    version = String(manifest.version || '');
  }
  if (!/^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$/.test(version)) fail('invalid_request');
  const files = {};
  let total = 0;
  for (const name of RELEASE_FILES) {
    const bytes = await fetchBytes(`${base}/download/${version}/${name}`, name === 'styles.css');
    if (!bytes) continue;
    total += bytes.length;
    if (total > MAX_PACKAGE) fail('limit_exceeded');
    files[name] = bytes;
  }
  let manifest;
  try { manifest = JSON.parse(files['manifest.json'].toString('utf8')); } catch { fail('invalid_request'); }
  if (!manifest || manifest.id !== id || String(manifest.version) !== version) fail('conflict');
  const hash = crypto.createHash('sha256');
  for (const name of RELEASE_FILES) if (files[name]) hash.update(`${name}\u0000`).update(files[name]);
  return { plugin_id: id, name: String(entry.name || id), version, source: `github.com/${entry.repo}`,
    digest: hash.digest('hex'), sizes: Object.fromEntries(Object.entries(files).map(([k, v]) => [k, v.length])), files };
}
function prunePins() {
  for (const [pin, held] of packagePins) if (Date.now() - held.at > PIN_TTL_MS) packagePins.delete(pin);
}

function installedPlugins(app) {
  const plugins = app.plugins || {};
  const manifests = plugins.manifests || {};
  const enabled = plugins.enabledPlugins instanceof Set ? plugins.enabledPlugins : new Set();
  const internal = (app.internalPlugins && app.internalPlugins.plugins) || {};
  return {
    community: Object.values(manifests).slice(0, 500).map(manifest => ({
      id: String(manifest.id), name: String(manifest.name || manifest.id), version: String(manifest.version || ''),
      enabled: enabled.has(manifest.id) })),
    core: Object.entries(internal).slice(0, 200).map(([id, plugin]) => ({ id, enabled: plugin && plugin.enabled === true })),
  };
}
function missingFolders(vault, folderPath) {
  // Outermost first; refuses a file standing where a folder is needed.
  const missing = [];
  for (let folder = folderPath; folder !== '.'; folder = path.posix.dirname(folder)) {
    const existing = vault.getAbstractFileByPath(folder);
    if (existing instanceof TFolder) break;
    if (existing) fail('conflict');
    missing.unshift(folder);
  }
  return missing;
}
async function makeFolders(app, config, missing) {
  for (const folder of missing) {
    verifyRoot(app, config);
    safePath(folder, config.root, true);
    try { await app.vault.createFolder(folder); } catch { fail('conflict'); }
    if (!(app.vault.getAbstractFileByPath(folder) instanceof TFolder)) fail('conflict');
  }
}
function item(app, root, requested) {
  // Validate the shape, allowing an absent path, so a missing source reports not_found.
  const key = safePath(requested, root, true);
  const found = app.vault.getAbstractFileByPath(key);
  if (!(found instanceof TFile || found instanceof TFolder) || found.path !== key) fail('not_found');
  return found;
}
async function itemState(app, config, key) {
  // Revision of everything an approval covers: note text, file stat, or a folder's whole tree.
  const found = app.vault.getAbstractFileByPath(key);
  if (!found) return { path: key, kind: 'absent', revision: 'absent', count: 0 };
  if (found instanceof TFile) {
    if (found.extension === 'md') {
      await freshSize(app, config, found, MAX_NOTE);
      return { path: key, kind: 'file', revision: revision(await app.vault.read(found)), count: 1 };
    }
    const stat = await app.vault.adapter.stat(found.path);
    return { path: key, kind: 'file', revision: revision(`${stat ? stat.size : -1}:${stat ? stat.mtime : -1}`), count: 1 };
  }
  if (!(found instanceof TFolder)) fail('not_found');
  const entries = [];
  const pending = [found];
  while (pending.length) {
    for (const child of pending.pop().children || []) {
      entries.push(`${child.path}\u0000${child instanceof TFile ? `${child.stat ? child.stat.size : 0}:${child.stat ? child.stat.mtime : 0}` : 'dir'}`);
      if (entries.length > MAX_TREE) fail('limit_exceeded');
      if (child instanceof TFolder) pending.push(child);
    }
  }
  entries.sort();
  return { path: key, kind: 'folder', revision: revision(entries.join('\n')), count: entries.length };
}

async function execute(app, config, op, args, markStarted = () => {}) {
  verifyRoot(app, config);
  const vault = app.vault;
  const root = config.root;
  if (op === 'health') {
    argument(args, []);
    return { protocol: VERSION, vault: config.id, app_name: vault.getName(), app_root: root,
      capabilities: ['health', 'list', 'read', 'search', 'create', 'append', 'embed', 'reserve', 'receipt',
        'vaults', 'open_vault', 'create_vault', 'state', 'mkdir', 'move', 'replace', 'trash',
        'logs', 'plugins', 'settings', 'plugin_state', 'plugin_set', 'plugin_remove',
        'setting_state', 'setting_write', 'plugin_catalog', 'plugin_package', 'plugin_release', 'plugin_install'] };
  }
  if (op === 'list') {
    argument(args, ['prefix', 'limit']);
    const prefix = args.prefix === '' ? '' : safePath(args.prefix, root);
    const cap = limit(args.limit, MAX_LIST);
    const paths = [];
    for (const item of vault.getAllLoadedFiles()) {
      // Direct children only, so agents browse folder by folder within the cap.
      if (!item.path || item.path === prefix) continue;
      const rest = prefix ? (item.path.startsWith(prefix + '/') ? item.path.slice(prefix.length + 1) : null) : item.path;
      if (rest === null || rest.includes('/')) continue;
      try { safePath(item.path, root); } catch { continue; }
      if (!(item instanceof TFile) && !(item instanceof TFolder)) continue;
      paths.push({ path: item.path, kind: item instanceof TFolder ? 'folder' : 'file' });
      if (paths.length > cap) fail('limit_exceeded');
    }
    return { entries: paths.sort((a, b) => a.path.localeCompare(b.path)) };
  }
  if (op === 'read') {
    argument(args, ['path']);
    const file = note(app, root, args.path);
    await freshSize(app, config, file, MAX_NOTE);
    const content = await vault.read(file);
    if (Buffer.byteLength(content) > MAX_NOTE) fail('limit_exceeded');
    return { path: file.path, content, revision: revision(content) };
  }
  if (op === 'search') {
    argument(args, ['query', 'limit']);
    const query = string(args.query, 256);
    const cap = limit(args.limit, 50);
    let bytes = 0; let files = 0;
    const matches = [];
    for (const file of vault.getMarkdownFiles()) {
      try { safePath(file.path, root); } catch { continue; }
      if (++files > MAX_SEARCH_FILES) fail('limit_exceeded');
      const knownSize = await freshSize(app, config, file, MAX_NOTE).catch(error => {
        if (error instanceof BridgeError && error.code === 'limit_exceeded') return null;
        throw error;
      });
      if (knownSize === null) continue;
      if (bytes + knownSize > MAX_SEARCH_BYTES) fail('limit_exceeded');
      const content = await vault.read(file);
      const size = Buffer.byteLength(content);
      if ((bytes += size) > MAX_SEARCH_BYTES) fail('limit_exceeded');
      if (size > MAX_NOTE) continue;
      const at = content.indexOf(query);
      if (at >= 0) {
        matches.push({ path: file.path, offset: at, revision: revision(content) });
        if (matches.length >= cap) break;
      }
    }
    return { matches };
  }
  if (op === 'create') {
    argument(args, ['path', 'content']);
    const key = safePath(args.path, root, true);
    if (!key.toLowerCase().endsWith('.md') || typeof args.content !== 'string' || Buffer.byteLength(args.content) > MAX_NOTE) fail('invalid_request');
    if (vault.getAbstractFileByPath(key) || fs.existsSync(path.join(root, key))) fail('conflict');
    const parent = path.posix.dirname(key);
    const missing = [];
    for (let folder = parent; folder !== '.'; folder = path.posix.dirname(folder)) {
      const existing = vault.getAbstractFileByPath(folder);
      if (existing instanceof TFolder) break;
      if (existing) fail('conflict');
      missing.unshift(folder);
    }
    verifyRoot(app, config);
    safePath(key, root, true);
    const bytes = Buffer.from(args.content, 'utf8');
    const data = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    markStarted();
    // Missing parent folders are created through the app, outermost first.
    for (const folder of missing) {
      verifyRoot(app, config);
      safePath(folder, root, true);
      try { await vault.createFolder(folder); } catch { fail('conflict'); }
      if (!(vault.getAbstractFileByPath(folder) instanceof TFolder)) fail('conflict');
    }
    try { await vault.createBinary(key, data); } catch { fail('conflict'); }
    const created = note(app, root, key);
    await freshSize(app, config, created, MAX_NOTE);
    const content = await vault.read(created);
    if (content !== args.content) fail('conflict');
    return { path: key, revision: revision(content) };
  }
  if (op === 'append') {
    argument(args, ['path', 'content', 'expected_revision']);
    const file = note(app, root, args.path);
    if (typeof args.content !== 'string' || Buffer.byteLength(args.content) > MAX_APPEND ||
        !/^sha256:[0-9a-f]{64}$/.test(args.expected_revision)) fail('invalid_request');
    await freshSize(app, config, file, MAX_NOTE);
    verifyRoot(app, config);
    await vault.process(file, current => {
      verifyRoot(app, config);
      if (revision(current) !== args.expected_revision) fail('conflict');
      if (Buffer.byteLength(current) + Buffer.byteLength(args.content) > MAX_NOTE) fail('limit_exceeded');
      markStarted();
      return current + args.content;
    });
    await freshSize(app, config, file, MAX_NOTE);
    const content = await vault.read(file);
    if (Buffer.byteLength(content) > MAX_NOTE) fail('limit_exceeded');
    return { path: file.path, revision: revision(content) };
  }
  if (op === 'logs') {
    argument(args, ['level', 'limit']);
    if (!['all', 'error', 'warn'].includes(args.level)) fail('invalid_request');
    const cap = limit(args.limit, MAX_LOGS);
    // Only errors and warnings are captured; 'all' means both.
    const wanted = args.level === 'error' ? ['error'] : ['error', 'warn'];
    const entries = consoleLog.filter(entry => wanted.includes(entry.level)).slice(-cap)
      .map(entry => ({ ...entry, message: scrub(entry.message) }));
    return { entries };
  }
  if (op === 'plugins') {
    argument(args, []);
    return installedPlugins(app);
  }
  if (op === 'settings') {
    argument(args, ['file']);
    if (typeof args.file !== 'string') fail('invalid_request');
    let relative;
    if (SETTINGS_FILES.has(args.file)) relative = `${args.file}.json`;
    else {
      const match = /^plugin:([a-z0-9][a-z0-9_-]{0,63})$/i.exec(args.file);
      if (!match) fail('invalid_request');
      relative = `plugins/${match[1]}/data.json`;
    }
    const settingsDir = path.join(root, '.obsidian');
    const filename = path.join(settingsDir, relative);
    verifyRoot(app, config);
    // Every directory on the way must resolve inside the vault; the file itself is opened
    // without following a symlink and checked on the open descriptor.
    try {
      if (fs.realpathSync(path.dirname(filename)) !== path.dirname(filename) ||
          fs.realpathSync(settingsDir) !== settingsDir) fail('invalid_path');
    } catch (error) { if (error instanceof BridgeError) throw error; fail('not_found'); }
    let descriptor;
    try { descriptor = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW); }
    catch (error) { fail(error && error.code === 'ELOOP' ? 'invalid_path' : 'not_found'); }
    let parsed;
    try {
      const stat = fs.fstatSync(descriptor);
      if (!stat.isFile()) fail('invalid_path');
      if (stat.size > MAX_NOTE) fail('limit_exceeded');
      try { parsed = JSON.parse(fs.readFileSync(descriptor, 'utf8')); } catch { fail('invalid_request'); }
    } finally { fs.closeSync(descriptor); }
    return { file: args.file, settings: redacted(parsed) };
  }
  if (op === 'plugin_catalog') {
    argument(args, ['query']);
    const query = string(args.query, 200).toLowerCase();
    const hits = (await catalog()).filter(item => item && typeof item.id === 'string' &&
      [item.id, item.name, item.description, item.author].some(field => typeof field === 'string' && field.toLowerCase().includes(query)))
      .slice(0, 20).map(item => ({ id: String(item.id), name: String(item.name || item.id).slice(0, 200),
        author: String(item.author || '').slice(0, 200), description: String(item.description || '').slice(0, 500),
        repo: String(item.repo || '').slice(0, 200) }));
    return { plugins: hits };
  }
  if (op === 'plugin_package') {
    argument(args, ['plugin_id', 'version', 'pin']);
    if (typeof args.pin !== 'boolean' || typeof args.version !== 'string') fail('invalid_request');
    prunePins();
    const fetched = await fetchPackage(args.plugin_id, args.version);
    let pin = '';
    if (args.pin) {
      if (packagePins.size >= 8) fail('limit_exceeded');
      pin = crypto.randomBytes(24).toString('base64url');
      packagePins.set(pin, { at: Date.now(), ...fetched });
    }
    const { files, ...summary } = fetched;
    return { ...summary, pin };
  }
  if (op === 'plugin_release') {
    argument(args, ['pin']);
    packagePins.delete(String(args.pin));
    return { released: true };
  }
  if (op === 'plugin_install') {
    argument(args, ['plugin_id', 'pin', 'digest']);
    prunePins();
    const held = packagePins.get(String(args.pin));
    packagePins.delete(String(args.pin));
    if (!held || held.plugin_id !== args.plugin_id || held.digest !== args.digest) fail('conflict');
    if (pluginRecord(app, root, held.plugin_id).installed) fail('conflict');
    const pluginsDir = path.join(root, '.obsidian', 'plugins');
    if (!fs.existsSync(pluginsDir)) fs.mkdirSync(pluginsDir, { recursive: true, mode: 0o755 });
    if (fs.realpathSync(pluginsDir) !== pluginsDir) fail('invalid_path');
    const target = path.join(pluginsDir, held.plugin_id);
    if (fs.existsSync(target)) fail('conflict');
    verifyRoot(app, config);
    markStarted();
    fs.mkdirSync(target, { mode: 0o755 });
    try {
      for (const [name, bytes] of Object.entries(held.files)) {
        fs.writeFileSync(path.join(target, name), bytes, { mode: 0o644, flag: 'wx' });
      }
    } catch {
      // Never leave a partial, unapproved copy behind.
      fs.rmSync(target, { recursive: true, force: true });
      fail('conflict');
    }
    if (typeof app.plugins.loadManifests === 'function') await app.plugins.loadManifests();
    await app.plugins.enablePluginAndSave(held.plugin_id);
    const now = pluginRecord(app, root, held.plugin_id);
    if (!now.installed) fail('conflict');
    return { plugin_id: held.plugin_id, version: held.version, installed: true, enabled: now.enabled };
  }
  if (op === 'plugin_state') {
    argument(args, ['plugin_id']);
    return pluginRecord(app, root, args.plugin_id);
  }
  if (op === 'plugin_set' || op === 'plugin_remove') {
    argument(args, op === 'plugin_set' ? ['plugin_id', 'enabled', 'expected_digest'] : ['plugin_id', 'expected_digest']);
    if (args.plugin_id === PLUGIN_ID) fail('invalid_request');
    const current = pluginRecord(app, root, args.plugin_id);
    if (!current.installed) fail('not_found');
    if (`${current.enabled}:${current.digest}` !== args.expected_digest) fail('conflict');
    const plugins = app.plugins;
    markStarted();
    if (op === 'plugin_set') {
      if (typeof args.enabled !== 'boolean') fail('invalid_request');
      if (args.enabled) await plugins.enablePluginAndSave(args.plugin_id);
      else await plugins.disablePluginAndSave(args.plugin_id);
      return { plugin_id: args.plugin_id, enabled: pluginRecord(app, root, args.plugin_id).enabled };
    }
    await plugins.uninstallPlugin(args.plugin_id);
    if (pluginRecord(app, root, args.plugin_id).installed) fail('conflict');
    return { plugin_id: args.plugin_id, removed: true };
  }
  if (op === 'setting_state') {
    argument(args, ['setting_id']);
    return settingState(root, args.setting_id);
  }
  if (op === 'setting_write') {
    argument(args, ['setting_id', 'value', 'expected_revision']);
    if (!['string', 'number', 'boolean'].includes(typeof args.value) ||
        (typeof args.value === 'string' && Buffer.byteLength(args.value) > 4096) ||
        (typeof args.value === 'number' && !Number.isFinite(args.value))) fail('invalid_request');
    const target = settingTarget(root, args.setting_id);
    verifyRoot(app, config);
    const { raw, data } = readSettingFile(root, target.filename);
    if (revision(raw) !== args.expected_revision) fail('conflict');
    markStarted();
    if (!target.plugin) {
      // Obsidian's own config API keeps the running app and app.json/appearance.json in step.
      app.vault.setConfig(target.key, args.value);
      if (typeof app.vault.saveConfig === 'function') await app.vault.saveConfig();
      if (typeof app.vault.getConfig === 'function' && app.vault.getConfig(target.key) !== args.value) fail('conflict');
    } else {
      if (!fs.existsSync(path.dirname(target.filename))) fail('not_found');
      const plugins = app.plugins;
      const running = Boolean(plugins && plugins.enabledPlugins instanceof Set && plugins.enabledPlugins.has(target.plugin));
      // Stop the plugin first so its own save on unload cannot overwrite the approved value.
      if (running) await plugins.disablePlugin(target.plugin);
      try {
        let mode = 0o644;
        try { mode = fs.statSync(target.filename).mode & 0o777; } catch { /* new file */ }
        const latest = readSettingFile(root, target.filename);
        writePrivate(target.filename, JSON.stringify({ ...latest.data, [target.key]: args.value }, null, 2));
        fs.chmodSync(target.filename, mode);
      } finally {
        // Never leave the owner's plugin switched off because a write failed.
        if (running) await plugins.enablePlugin(target.plugin);
      }
      if (readSettingFile(root, target.filename).data[target.key] !== args.value) fail('conflict');
    }
    return { setting_id: args.setting_id, written: true };
  }
  if (op === 'state') {
    argument(args, ['path']);
    return itemState(app, config, safePath(args.path, root, true));
  }
  if (op === 'mkdir') {
    argument(args, ['path']);
    const key = safePath(args.path, root, true);
    if (vault.getAbstractFileByPath(key) || fs.existsSync(path.join(root, key))) fail('conflict');
    const missing = missingFolders(vault, key);
    markStarted();
    await makeFolders(app, config, missing);
    return { path: key, created: true };
  }
  if (op === 'move') {
    argument(args, ['source', 'destination']);
    const moving = item(app, root, args.source);
    const destination = safePath(args.destination, root, true);
    const isFolder = moving instanceof TFolder;
    if (destination === moving.path || (isFolder && destination.startsWith(moving.path + '/'))) fail('invalid_path');
    if (!isFolder && path.posix.extname(destination).toLowerCase() !== path.posix.extname(moving.path).toLowerCase()) fail('invalid_request');
    if (vault.getAbstractFileByPath(destination) || fs.existsSync(path.join(root, destination))) fail('conflict');
    const missing = missingFolders(vault, path.posix.dirname(destination));
    const source = moving.path;
    markStarted();
    await makeFolders(app, config, missing);
    verifyRoot(app, config);
    safePath(destination, root, true);
    // fileManager.renameFile also rewrites links to the moved file across the vault.
    try { await app.fileManager.renameFile(moving, destination); } catch { fail('conflict'); }
    const moved = vault.getAbstractFileByPath(destination);
    if (!moved || moved.path !== destination || vault.getAbstractFileByPath(source)) fail('conflict');
    return { source, destination, kind: isFolder ? 'folder' : 'file' };
  }
  if (op === 'replace') {
    argument(args, ['path', 'content', 'expected_revision']);
    const file = note(app, root, args.path);
    if (typeof args.content !== 'string' || Buffer.byteLength(args.content) > MAX_NOTE ||
        !/^sha256:[0-9a-f]{64}$/.test(args.expected_revision)) fail('invalid_request');
    await freshSize(app, config, file, MAX_NOTE);
    verifyRoot(app, config);
    await vault.process(file, current => {
      verifyRoot(app, config);
      if (revision(current) !== args.expected_revision) fail('conflict');
      markStarted();
      return args.content;
    });
    const content = await vault.read(file);
    if (content !== args.content) fail('conflict');
    return { path: file.path, revision: revision(content) };
  }
  if (op === 'trash') {
    argument(args, ['path', 'expected_revision']);
    const target = item(app, root, args.path);
    if (typeof args.expected_revision !== 'string' || !/^sha256:[0-9a-f]{64}$/.test(args.expected_revision)) fail('invalid_request');
    const current = await itemState(app, config, target.path);
    if (current.revision !== args.expected_revision) fail('conflict');
    verifyRoot(app, config);
    markStarted();
    // false = the vault's own .trash folder, so the owner can restore it.
    try { await vault.trash(target, false); } catch { fail('conflict'); }
    if (vault.getAbstractFileByPath(target.path)) fail('conflict');
    return { path: target.path, trashed: true };
  }
  if (op === 'embed') {
    argument(args, ['source', 'target']);
    const source = note(app, root, args.source);
    let target = string(args.target, 1024);
    const markdown = /^!\[[^\]]*\]\(([^()]+)\)$/.exec(target);
    if (markdown) target = markdown[1];
    if (target.startsWith('![[') && target.endsWith(']]')) target = target.slice(3, -2);
    if (target.startsWith('[[') && target.endsWith(']]')) target = target.slice(2, -2);
    target = target.split('|', 1)[0].split('#', 1)[0];
    if (!target || target.includes('%') || target.startsWith('/') || target.includes('\\') ||
        /^[A-Za-z][A-Za-z0-9+.-]*:/.test(target)) fail('invalid_path');
    if (target.startsWith('./') || target.startsWith('../')) {
      const relative = path.posix.normalize(path.posix.join(path.posix.dirname(source.path), target));
      safePath(relative, root);
    } else if (target.split('/').includes('..')) fail('invalid_path');
    const resolved = app.metadataCache.getFirstLinkpathDest(target, source.path);
    if (!(resolved instanceof TFile)) fail('not_found');
    safePath(resolved.path, root);
    const mime = MIME[path.extname(resolved.path).toLowerCase()];
    if (!mime) fail('unsupported_media');
    await freshSize(app, config, resolved, MAX_MEDIA);
    const data = Buffer.from(await vault.readBinary(resolved));
    if (data.length > MAX_MEDIA) fail('limit_exceeded');
    return { path: resolved.path, mime, data: data.toString('base64') };
  }
  fail('unknown_operation');
}

function knownVaults(config) {
  if (!config.appConfig) fail('unavailable');
  let document;
  try { document = JSON.parse(fs.readFileSync(config.appConfig, 'utf8')); } catch { fail('unavailable'); }
  const vaults = document && typeof document.vaults === 'object' && document.vaults ? document.vaults : {};
  return Object.entries(vaults)
    .filter(([, entry]) => entry && typeof entry.path === 'string' && path.dirname(entry.path) === config.parent &&
      usableName(path.basename(entry.path)))
    .slice(0, 256)
    .map(([id, entry]) => ({ id, name: path.basename(entry.path), path: entry.path, open: entry.open === true }));
}
function usableName(name) {
  return typeof name === 'string' && VAULT_NAME.test(name) && !name.endsWith('.') && !name.endsWith(' ');
}
function vaultTarget(config, args) {
  argument(args, ['name']);
  if (!usableName(args.name)) fail('invalid_request');
  return path.join(config.parent, args.name);
}
function realDirectory(target) {
  const stat = fs.lstatSync(target);
  if (stat.isSymbolicLink() || !stat.isDirectory()) fail('invalid_path');
}
// Install this plugin's own reviewed bundle into another vault and enable it there.
function ensurePlugin(config, target) {
  realDirectory(target);
  const settings = path.join(target, '.obsidian');
  if (!fs.existsSync(settings)) fs.mkdirSync(settings, { mode: 0o755 });
  realDirectory(settings);
  const plugins = path.join(settings, 'plugins');
  if (!fs.existsSync(plugins)) fs.mkdirSync(plugins, { mode: 0o755 });
  realDirectory(plugins);
  const destination = path.join(plugins, PLUGIN_ID);
  const source = path.join(config.root, '.obsidian', 'plugins', PLUGIN_ID);
  const bundle = Object.fromEntries(['main.js', 'manifest.json'].map(name => [name, fs.readFileSync(path.join(source, name))]));
  if (!fs.existsSync(destination)) fs.mkdirSync(destination, { mode: 0o755 });
  realDirectory(destination);
  // Only the two managed files may live here; anything else needs owner review.
  if (fs.readdirSync(destination).some(name => !Object.hasOwn(bundle, name))) fail('conflict');
  for (const [name, bytes] of Object.entries(bundle)) {
    const file = path.join(destination, name);
    if (fs.existsSync(file)) {
      if (!fs.lstatSync(file).isFile()) fail('conflict');
      if (fs.readFileSync(file).equals(bytes)) continue;
    }
    writePrivate(file, bytes);
    fs.chmodSync(file, 0o644);
  }
  const enabled = path.join(settings, 'community-plugins.json');
  let list = [];
  if (fs.existsSync(enabled)) {
    if (fs.lstatSync(enabled).isSymbolicLink()) fail('invalid_path');
    try { list = JSON.parse(fs.readFileSync(enabled, 'utf8')); } catch { fail('conflict'); }
    if (!Array.isArray(list)) fail('conflict');
  }
  if (!list.includes(PLUGIN_ID)) fs.writeFileSync(enabled, JSON.stringify([...list, PLUGIN_ID], null, 2));
}
function onlyBridgeEnabled(target) {
  try {
    const list = JSON.parse(fs.readFileSync(path.join(target, '.obsidian', 'community-plugins.json'), 'utf8'));
    return Array.isArray(list) && list.every(id => id === PLUGIN_ID);
  } catch { return false; }
}
function trustVault(config, target) {
  // Obsidian keeps per-vault "trust community plugins" in the shared window localStorage.
  // Trust is vault-wide, so it is granted only when this bridge is the vault's sole
  // enabled community plugin; any other plugin set stays for the owner to approve.
  if (!onlyBridgeEnabled(target)) return;
  const match = knownVaults(config).find(entry => entry.path === target);
  if (match && globalThis.localStorage) globalThis.localStorage.setItem(`enable-plugin-${match.id}`, 'true');
}
function electronRenderer() {
  // Obsidian's own vault switcher uses window.electron.ipcRenderer.
  const candidates = [() => globalThis.window && globalThis.window.electron && globalThis.window.electron.ipcRenderer,
    () => require('electron').ipcRenderer];
  for (const candidate of candidates) {
    try {
      const ipcRenderer = candidate();
      if (ipcRenderer && typeof ipcRenderer.sendSync === 'function') return ipcRenderer;
    } catch { /* try the next one */ }
  }
  fail('unavailable');
}
function openWindow(config, target) {
  const electron = { ipcRenderer: electronRenderer() };
  trustVault(config, target);
  // vault-open(path, create): create=false opens an existing folder as a vault in a new
  // window. The folder and its .obsidian are prepared first so the bridge loads there.
  const opened = electron.ipcRenderer.sendSync('vault-open', target, false);
  if (opened !== true) fail('internal_error');
  // The window is open now; a failure to record trust must not undo the vault.
  try { trustVault(config, target); } catch { /* the owner can trust it in the desktop */ }
}
function manageVaults(config, op, args) {
  if (op === 'vaults') {
    argument(args, []);
    return { vaults: knownVaults(config).map(({ name, open }) => ({ name, open })) };
  }
  const target = vaultTarget(config, args);
  if (op === 'open_vault') {
    if (!knownVaults(config).some(entry => entry.path === target) &&
        !(fs.existsSync(path.join(target, '.obsidian')))) fail('not_found');
    ensurePlugin(config, target);
    openWindow(config, target);
    return { name: args.name, opened: true };
  }
  if (op === 'create_vault') {
    // Each open vault is another Electron window in a memory-limited pod.
    if (knownVaults(config).length >= MAX_VAULTS) fail('limit_exceeded');
    electronRenderer();
    if (fs.existsSync(target)) fail('conflict');
    fs.mkdirSync(target, { mode: 0o755 });
    try {
      ensurePlugin(config, target);
      openWindow(config, target);
    } catch (error) {
      // Nothing but our own scaffolding exists yet, so undo it and let a retry start clean.
      fs.rmSync(target, { recursive: true, force: true });
      throw error;
    }
    return { name: args.name, created: true };
  }
  fail('unknown_operation');
}

class PrivateBridge extends Plugin {
  async onload() {
    const config = await bootstrap(this.app);
    if (!config) return;
    this.bridgeConfig = config;
    const receipts = new Map();
    const prune = () => {
      for (const [id, entry] of receipts) {
        if (entry.state !== 'running' && entry.state !== 'indeterminate' &&
            Date.now() - entry.created > RECEIPT_TTL_MS) receipts.delete(id);
      }
    };
    const lookup = (args, needsOperation) => {
      if (!shape(args, needsOperation ? ['client', 'operation', 'receipt', 'mutation'] :
                      ['client', 'receipt', 'mutation'])) fail('invalid_request');
      if (typeof args.receipt !== 'string' ||
          !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(args.receipt)) fail('invalid_request');
      const entry = receipts.get(args.receipt);
      const operation = needsOperation ? args.operation : entry?.operation;
      if (!entry && !needsOperation) return null;
      const digest = mutationDigest(config, args.client, operation, args.mutation);
      if (entry && (entry.digest !== digest || entry.operation !== operation)) fail('unauthorized');
      return entry;
    };
    const dispatch = async (op, args) => {
      verifyRoot(this.app, config);
      prune();
      if (op === 'vaults' || op === 'open_vault' || op === 'create_vault') return manageVaults(config, op, args);
      if (op === 'reserve') {
        if (!shape(args, ['client', 'operation', 'mutation'])) fail('invalid_request');
        const digest = mutationDigest(config, args.client, args.operation, args.mutation);
        safePath(args.mutation.path, config.root, args.operation === 'create');
        if (receipts.size >= MAX_RECEIPTS) fail('limit_exceeded');
        const receipt = crypto.randomUUID();
        receipts.set(receipt, { digest, operation: args.operation, state: 'reserved', created: Date.now() });
        return { receipt };
      }
      if (op === 'receipt') {
        const entry = lookup(args, true);
        return receiptView(args.receipt, entry);
      }
      if (op === 'create' || op === 'append') {
        const entry = lookup(args, false);
        if (!entry || entry.operation !== op) fail('unknown_receipt');
        if (entry.state !== 'reserved') return receiptView(args.receipt, entry);
        if (activeMutations.count >= MAX_RECEIPTS) {
          entry.state = 'failed'; entry.error = 'limit_exceeded'; entry.created = Date.now();
          return receiptView(args.receipt, entry);
        }
        entry.state = 'running';
        activeMutations.count++;
        let started = false;
        try {
          const result = await execute(this.app, config, op, args.mutation, () => { started = true; });
          entry.state = 'committed'; entry.result = result;
        } catch (error) {
          if (!started && error instanceof BridgeError) {
            entry.state = 'failed'; entry.error = error.code;
          } else entry.state = 'indeterminate';
        } finally {
          activeMutations.count--;
        }
        if (entry.state === 'committed' || entry.state === 'failed') entry.created = Date.now();
        return receiptView(args.receipt, entry);
      }
      return execute(this.app, config, op, args);
    };
    // An abstract AF_UNIX address has no pathname for Node to unlink on close.
    const server = net.createServer(socket => {
      let raw = Buffer.alloc(0); let finished = false;
      socket.on('error', () => {});
      socket.setTimeout(5000, () => socket.destroy());
      socket.on('data', chunk => {
        if (finished) return;
        raw = Buffer.concat([raw, chunk]);
        if (raw.length > MAX_REQUEST) { finished = true; socket.destroy(); return; }
        const newline = raw.indexOf(10);
        if (newline < 0) return;
        finished = true;
        if (newline !== raw.length - 1) { socket.destroy(); return; }
        (async () => {
          let reply;
          try {
            const request = JSON.parse(raw.subarray(0, newline).toString('utf8'));
            if (!shape(request, ['v', 'vault', 'token', 'op', 'args']) || request.v !== VERSION ||
                request.vault !== config.id || typeof request.token !== 'string' ||
                Buffer.byteLength(request.token) !== Buffer.byteLength(config.token) ||
                !crypto.timingSafeEqual(Buffer.from(request.token), Buffer.from(config.token))) fail('unauthorized');
            reply = { v: VERSION, vault: config.id, ok: true, result: await dispatch(request.op, request.args) };
          } catch (error) {
            reply = { v: VERSION, vault: config.id, ok: false, error: error instanceof BridgeError ? error.code : 'internal_error' };
          }
          const output = JSON.stringify(reply);
          if (Buffer.byteLength(output) > MAX_RESPONSE) reply = { v: VERSION, vault: config.id, ok: false, error: 'limit_exceeded' };
          if (!socket.destroyed) socket.end(JSON.stringify(reply) + '\n');
        })();
      });
    });
    server.maxConnections = 16;
    try {
      await new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(socketAddress(config), () => { server.removeListener('error', reject); resolve(); });
      });
      server.on('error', () => { if (this.bridgeServer === server) this.bridgeServer = null; server.close(); });
      this.bridgeServer = server;
    } catch { if (server.listening) server.close(); }
  }
  async onunload() {
    if (!this.bridgeServer) return;
    await new Promise(resolve => this.bridgeServer.close(resolve));
    this.bridgeServer = null;
    // Deregister only if the runtime entry is still this window's own.
    const config = this.bridgeConfig;
    try {
      const credential = path.join(config.runtime, 'credential');
      if (privateFile(credential, config.runtime).trim() === config.token) {
        fs.unlinkSync(path.join(config.runtime, 'endpoint.json'));
        fs.unlinkSync(credential);
      }
    } catch { /* already gone */ }
  }
}
module.exports = PrivateBridge;
