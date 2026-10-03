const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const Module = require('node:module');
const crypto = require('node:crypto');
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const REAL_IPC = process.env.OBSIDIAN_BRIDGE_REAL_IPC === '1';

// The plugin only starts as the desktop identity (UID/GID 1000, no effective
// capabilities). On hosts where the test runs as another user, present files
// owned by that user as 1000-owned so protocol tests exercise a started bridge.
// The identity test below swaps these out to prove enforcement.
const HOST_UID = process.getuid();
if (HOST_UID !== 1000 || process.getgid() !== 1000) {
  const hostLstat = fs.lstatSync;
  const hostRead = fs.readFileSync;
  process.getuid = () => 1000;
  process.getgid = () => 1000;
  fs.lstatSync = function(...args) {
    const value = hostLstat.apply(this, args);
    if (!value || value.uid !== HOST_UID) return value;
    return new Proxy(value, { get(target, key) {
      const field = Reflect.get(target, key);
      return key === 'uid' || key === 'gid' ? 1000 : typeof field === 'function' ? field.bind(target) : field;
    } });
  };
  fs.readFileSync = function(filename, ...args) {
    if (filename === '/proc/self/status') {
      return hostRead.call(this, filename, ...args).replace(/^CapEff:.*$/m, 'CapEff:\t0000000000000000');
    }
    return hostRead.call(this, filename, ...args);
  };
}

// The ordinary unit gate uses a stream double. REAL_IPC runs unpatched AF_UNIX.
const listeners = new Map();
if (!REAL_IPC) {
net.createServer = function(handler) {
  const server = new EventEmitter();
  server.listen = (address, callback) => {
    if (listeners.has(address)) { process.nextTick(() => server.emit('error', Error('occupied'))); return; }
    listeners.set(address, handler);
    process.nextTick(callback);
  };
  server.close = callback => { listeners.delete([...listeners].find(([, h]) => h === handler)?.[0]); if (callback) process.nextTick(callback); };
  return server;
};
net.createConnection = function(filename) {
  const client = new EventEmitter();
  client.setTimeout = () => {};
  client.destroy = () => {};
  const handler = listeners.get(filename);
  if (!handler) { process.nextTick(() => client.emit('error', Error('unavailable'))); return client; }
  const remote = new EventEmitter();
  remote.setTimeout = () => {};
  remote.destroy = () => process.nextTick(() => client.emit('end'));
  remote.end = bytes => process.nextTick(() => { client.emit('data', Buffer.from(bytes)); client.emit('end'); });
  client.write = bytes => process.nextTick(() => remote.emit('data', Buffer.from(bytes)));
  handler(remote);
  process.nextTick(() => client.emit('connect'));
  return client;
};
}

if (REAL_IPC) test('real private AF_UNIX plugin to Python adapter, two vaults and auth isolation', async () => {
  const a = fixture('IAM');
  const b = fixture('Homelab', a.home);
  let first, second;
  try {
    a.add('Private/note.md', 'from iam');
    b.add('Private/note.md', 'from homelab');
    const createBinary = a.vault.createBinary.bind(a.vault);
    a.vault.createBinary = async (...args) => {
      const created = await createBinary(...args);
      await new Promise(resolve => setTimeout(resolve, 130));
      return created;
    };
    const processNote = a.vault.process.bind(a.vault);
    a.vault.process = async (...args) => {
      const written = await processNote(...args);
      await new Promise(resolve => setTimeout(resolve, 130));
      return written;
    };
    first = await start(a); second = await start(b);
    assert.ok(first.bridgeServer, 'real AF_UNIX bind failed for iam');
    assert.ok(second.bridgeServer, 'real AF_UNIX bind failed for homelab');
    assert.equal(a.config.vault_id, 'iam');
    assert.equal(b.config.vault_id, 'homelab');
    assert.notEqual(a.config.testToken, b.config.testToken);
    assert.equal(fs.statSync(a.runtime).mode & 0o777, 0o700);
    for (const f of [a, b]) {
      assert.equal(fs.statSync(path.dirname(f.config.socket)).mode & 0o777, 0o700);
      assert.equal(fs.statSync(f.config.endpoint_file).mode & 0o777, 0o600);
      assert.equal(fs.statSync(f.config.credential_file).mode & 0o777, 0o600);
      assert.equal(fs.existsSync(f.config.socket), false);
    }
    const manifest = path.join(a.home, 'endpoints.json');
    fs.writeFileSync(manifest, JSON.stringify([a.config, b.config].map(f => ({
      vault_id: f.vault_id, app_name: f.app_name, app_root: f.app_root,
      socket: f.socket, credential_file: f.credential_file,
    }))));
    const output = await new Promise((resolve, reject) => {
      const child = spawn('python3', [path.join(__dirname, 'test_bridge.py'), '--real-ipc-client', manifest]);
      let stdout = ''; let stderr = '';
      child.stdout.on('data', chunk => { stdout += chunk; });
      child.stderr.on('data', chunk => { stderr += chunk; });
      child.on('error', reject);
      child.on('close', code => code === 0 ? resolve(stdout) : reject(Error(`real IPC client failed (${code}): ${stderr}`)));
    });
    assert.match(output, /real IPC passed/);
    assert.ok(a.calls.reads >= 4);
    assert.equal(b.calls.reads, 1);
    assert.equal(a.calls.writes, 2);
    assert.equal(b.calls.writes, 0);
    const foreign = net.createServer(socket => socket.end('foreign'));
    await new Promise((resolve, reject) => { foreign.once('error', reject); foreign.listen(a.config.socket, resolve); });
    try {
      await first.onunload(); first = null;
      assert.equal(fs.existsSync(a.config.endpoint_file), false);
      assert.equal(fs.existsSync(a.config.credential_file), false);
      assert.equal(fs.lstatSync(a.config.socket).isSocket(), true);
      const answer = await new Promise((resolve, reject) => {
        const connection = net.createConnection(a.config.socket);
        let data = '';
        connection.on('data', chunk => { data += chunk; });
        connection.on('end', () => resolve(data));
        connection.on('error', reject);
      });
      assert.equal(answer, 'foreign');
    } finally { await new Promise(resolve => foreign.close(resolve)); }
  } finally {
    if (first) await first.onunload(); if (second) await second.onunload();
    fs.rmSync(a.home, { recursive: true, force: true });
  }
});

class Plugin { constructor(app) { this.app = app; } }
class FileSystemAdapter {
  constructor(root) { this.root = root; }
  getBasePath() { return this.root; }
  async stat(filePath) { try { return fs.statSync(path.join(this.root, filePath)); } catch { return null; } }
}
class TFile {
  constructor(filePath, size) { this.path = filePath; this.stat = { size, mtime: 1 }; }
  get extension() { const ext = path.posix.extname(this.path); return ext ? ext.slice(1) : ''; }
}
class TFolder {
  constructor(filePath, list = () => []) { this.path = filePath; this.list = list; }
  get children() { return this.list().filter(item => path.posix.dirname(item.path) === this.path); }
}
// Obsidian's renderer: vault-open over ipcRenderer, trust flags in the shared localStorage.
const ipcCalls = [];
let onVaultOpen = null;
let vaultOpenReply = true;
const electron = { ipcRenderer: { sendSync(channel, ...args) {
  ipcCalls.push([channel, ...args]);
  if (onVaultOpen) onVaultOpen(channel, ...args);
  return vaultOpenReply;
} } };
const storage = new Map();
Object.defineProperty(globalThis, 'localStorage', { configurable: true, writable: true, value: {
  getItem: key => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => { storage.set(key, String(value)); },
  removeItem: key => { storage.delete(key); },
} });
const originalLoad = Module._load;
Module._load = function(request, parent, main) {
  if (request === 'obsidian') return { Plugin, FileSystemAdapter, TFile, TFolder };
  if (request === 'electron') return electron;
  return originalLoad.call(this, request, parent, main);
};
const Bridge = require('../bridge-plugin/main.js');
const PLUGIN_SOURCE = path.join(__dirname, '..', 'bridge-plugin');
const PLUGIN_ID = 'obsidian-private-bridge';

// One vault window: the root is <home>/<name>, so the vault name is its basename.
// All windows of a home share the 0700 runtime directory <home>/.runtime.
function fixture(name, sharedHome) {
  const home = sharedHome || fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-')));
  const runtime = path.join(home, '.runtime');
  if (!fs.existsSync(runtime)) fs.mkdirSync(runtime, { mode: 0o700 });
  fs.chmodSync(runtime, 0o700);
  const root = path.join(home, name);
  fs.mkdirSync(root);
  const files = new Map();
  const folders = new Map();
  const everything = () => [...folders.values(), ...files.values()];
  folders.set('Private', new TFolder('Private', everything));
  fs.mkdirSync(path.join(root, 'Private'));
  function add(filePath, data) {
    const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data);
    fs.mkdirSync(path.dirname(path.join(root, filePath)), { recursive: true });
    fs.writeFileSync(path.join(root, filePath), bytes);
    files.set(filePath, new TFile(filePath, bytes.length));
  }
  add('Private/note.md', 'initial');
  add('Private/image.png', Buffer.from([137, 80, 78, 71, 1, 2]));
  const calls = { reads: 0, writes: 0, binaries: 0 };
  const vault = {
    adapter: new FileSystemAdapter(root), getName: () => name,
    getAbstractFileByPath: p => files.get(p) || folders.get(p) || null,
    getAllLoadedFiles: () => [...folders.values(), ...files.values()],
    getMarkdownFiles: () => [...files.values()].filter(f => f.path.endsWith('.md')),
    async read(file) { calls.reads++; return fs.readFileSync(path.join(root, file.path), 'utf8'); },
    async readBinary(file) { calls.binaries++; return fs.readFileSync(path.join(root, file.path)); },
    async createBinary(filePath, data) {
      if (vault.externalCreateAtWrite) vault.externalCreateAtWrite(filePath);
      const bytes = Buffer.from(data);
      fs.writeFileSync(path.join(root, filePath), bytes, { flag: 'wx' });
      calls.writes++;
      files.set(filePath, new TFile(filePath, bytes.length));
      return files.get(filePath);
    },
    async createFolder(folderPath) {
      fs.mkdirSync(path.join(root, folderPath));
      calls.writes++;
      folders.set(folderPath, new TFolder(folderPath, everything));
      return folders.get(folderPath);
    },
    async trash(entry, system) {
      calls.trashed = [...(calls.trashed || []), [entry.path, system]];
      const bin = path.join(root, '.trash');
      fs.mkdirSync(bin, { recursive: true });
      fs.renameSync(path.join(root, entry.path), path.join(bin, path.posix.basename(entry.path)));
      for (const map of [files, folders]) {
        for (const key of [...map.keys()]) if (key === entry.path || key.startsWith(entry.path + '/')) map.delete(key);
      }
      calls.writes++;
    },
    async process(file, callback) {
      const current = fs.readFileSync(path.join(root, file.path), 'utf8');
      const next = callback(current);
      calls.writes++; add(file.path, next); return next;
    },
  };
  const fileManager = { async renameFile(entry, destination) {
    calls.renamed = [...(calls.renamed || []), [entry.path, destination]];
    fs.renameSync(path.join(root, entry.path), path.join(root, destination));
    const source = entry.path;
    for (const map of [files, folders]) {
      for (const [key, value] of [...map.entries()]) {
        if (key === source || key.startsWith(source + '/')) {
          map.delete(key);
          value.path = destination + key.slice(source.length);
          map.set(value.path, value);
        }
      }
    }
    calls.writes++;
  } };
  const app = { vault, fileManager, metadataCache: { getFirstLinkpathDest: (target) => {
    if (target === 'image.png' || target === 'Private/image.png') return files.get('Private/image.png');
    return null;
  } } };
  return { home, root, runtime, name, appConfig: path.join(home, 'obsidian.json'),
    config: { app_name: name, app_root: root }, app, vault, calls, add };
}
// The registration the plugin wrote for this root, read back the way the gateway would.
function registered(f) {
  for (const id of fs.readdirSync(f.runtime)) {
    const directory = path.join(f.runtime, id);
    const endpointFile = path.join(directory, 'endpoint.json');
    const credentialFile = path.join(directory, 'credential');
    if (!fs.existsSync(endpointFile) || !fs.existsSync(credentialFile)) continue;
    const endpoint = JSON.parse(fs.readFileSync(endpointFile, 'utf8'));
    if (endpoint.app_root !== f.root) continue;
    return { endpoint, vault_id: endpoint.vault_id, app_name: endpoint.app_name, app_root: endpoint.app_root,
      socket: path.join(directory, `${id}.sock`), endpoint_file: endpointFile, credential_file: credentialFile,
      testToken: fs.readFileSync(credentialFile, 'utf8').trim() };
  }
  return null;
}
async function request(config, op, args = {}, options = {}) {
  if (op === 'create' || op === 'append') {
    const reservation = await request(config, 'reserve', { client: 'bridge', operation: op, mutation: args }, options);
    if (!reservation.ok) return reservation;
    const reply = await request(config, 'mutation', { client: 'bridge', receipt: reservation.result.receipt,
      mutation: args }, { ...options, op });
    if (reply.ok && reply.result.status === 'failed') return { ...reply, ok: false, error: reply.result.error };
    return reply;
  }
  return new Promise((resolve, reject) => {
    const client = net.createConnection(address(config));
    let data = '';
    client.on('connect', () => client.write(JSON.stringify({ v: 1, vault: config.vault_id, token: config.testToken, op, args, ...options }) + '\n'));
    client.on('data', bytes => { data += bytes; });
    client.on('end', () => resolve(JSON.parse(data)));
    client.on('error', reject);
  });
}
function rawRequest(config, bytes) {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(address(config));
    let data = '';
    client.on('connect', () => client.write(bytes));
    client.on('data', chunk => { data += chunk; });
    client.on('end', () => resolve(data));
    client.on('error', reject);
  });
}
function address(config) {
  return '\0obsidian-bridge-' + crypto.createHash('sha256').update(config.socket + '\0' + config.testToken).digest('hex');
}
// Start one window with the cluster environment; `env` overrides (undefined deletes) a variable.
async function start(f, env = {}) {
  const settings = { OBSIDIAN_BRIDGE_INSTANCE: 'kubernetes-obsidian', OBSIDIAN_BRIDGE_DIR: f.runtime,
    OBSIDIAN_BRIDGE_VAULT_PARENT: f.home, OBSIDIAN_BRIDGE_APP_CONFIG: f.appConfig, ...env };
  for (const [key, value] of Object.entries(settings)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  }
  const bridge = new Bridge(f.app);
  await bridge.onload();
  if (bridge.bridgeServer) f.config = registered(f);
  return bridge;
}
function writeOwned(filename, data) {
  fs.writeFileSync(filename, data, { mode: 0o600 });
  fs.chmodSync(filename, 0o600);
}
if (!REAL_IPC) {
test('running receipt survives disconnect window and restart refuses replay', async () => {
  const f = fixture('Lifetime');
  let bridge;
  try {
    bridge = await start(f);
    const before = await request(f.config, 'read', { path: 'Private/note.md' });
    const mutation = { path: 'Private/note.md', content: '+', expected_revision: before.result.revision };
    const reserved = await request(f.config, 'reserve', { client: 'codex', operation: 'append', mutation });
    const args = { client: 'codex', receipt: reserved.result.receipt, mutation };
    const originalProcess = f.vault.process.bind(f.vault);
    let release, started;
    const hold = new Promise(resolve => { release = resolve; });
    const entered = new Promise(resolve => { started = resolve; });
    f.vault.process = async (...input) => {
      const output = await originalProcess(...input);
      started(); await hold; return output;
    };
    const inFlight = request(f.config, 'mutate', args, { op: 'append' });
    await entered;
    assert.equal((await request(f.config, 'receipt', { ...args, operation: 'append' })).result.status, 'pending');
    for (let i = 1; i < 64; i++) {
      assert.equal((await request(f.config, 'reserve', { client: 'codex', operation: 'append', mutation })).ok, true);
    }
    assert.equal((await request(f.config, 'reserve', { client: 'codex', operation: 'append', mutation })).error, 'limit_exceeded');
    release();
    assert.equal((await inFlight).result.status, 'committed');
    assert.equal(f.calls.writes, 1);
    const oldToken = f.config.testToken;
    await bridge.onunload();
    bridge = await start(f);
    assert.ok(bridge.bridgeServer);
    assert.notEqual(f.config.testToken, oldToken);
    assert.equal((await request(f.config, 'receipt', { ...args, operation: 'append' })).result.status, 'indeterminate');
    assert.equal((await request(f.config, 'mutate', args, { op: 'append' })).error, 'unknown_receipt');
    assert.equal(f.calls.writes, 1);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('receipt binds exact mutation and replay returns the committed append once', async () => {
  const f = fixture('Receipts');
  let bridge;
  try {
    bridge = await start(f);
    const before = await request(f.config, 'read', { path: 'Private/note.md' });
    const mutation = { path: 'Private/note.md', content: '+', expected_revision: before.result.revision };
    const reservation = await request(f.config, 'reserve', { client: 'codex', operation: 'append', mutation });
    const receipt = reservation.result.receipt;
    const args = { client: 'codex', receipt, mutation };
    const first = await request(f.config, 'mutate', args, { op: 'append' });
    assert.equal(first.result.status, 'committed');
    const again = await request(f.config, 'mutate', args, { op: 'append' });
    assert.deepEqual(again.result, first.result);
    assert.equal(f.calls.writes, 1);
    assert.equal((await request(f.config, 'read', { path: 'Private/note.md' })).result.content, 'initial+');
    assert.equal((await request(f.config, 'receipt', { ...args, operation: 'append' })).result.status, 'committed');
    assert.equal((await request(f.config, 'mutate', { ...args, client: 'claude' }, { op: 'append' })).error, 'unauthorized');
    assert.equal((await request(f.config, 'receipt', { ...args, operation: 'append',
      mutation: { ...mutation, content: '?' } })).error, 'unauthorized');
    assert.equal((await request(f.config, 'mutate', { ...args, receipt: crypto.randomUUID() }, { op: 'append' })).error, 'unknown_receipt');
    assert.equal((await request(f.config, 'receipt', { ...args, receipt: crypto.randomUUID(), operation: 'append' })).result.status, 'indeterminate');
    for (let i = 1; i < 64; i++) {
      assert.equal((await request(f.config, 'reserve', { client: 'codex', operation: 'append', mutation })).ok, true);
    }
    assert.equal((await request(f.config, 'reserve', { client: 'codex', operation: 'append', mutation })).error, 'limit_exceeded');
    assert.equal(f.calls.writes, 1);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('desktop identity requires UID 1000, GID 1000 and zero effective capabilities', async () => {
  const f = fixture('Identity');
  const originalUid = process.getuid;
  const originalGid = process.getgid;
  const read = fs.readFileSync;
  const lstat = fs.lstatSync;
  let bridge;
  try {
    for (const [uid, gid, caps] of [[1001, 1000, '0000000000000000'],
                                    [1000, 1001, '0000000000000000'],
                                    [1000, 1000, '0000000000000001']]) {
      process.getuid = () => uid;
      process.getgid = () => gid;
      // Fixture files appear owned by the simulated identity, so only identity() can refuse.
      fs.lstatSync = function(filename, ...args) {
        const value = lstat.call(this, filename, ...args);
        if (typeof filename === 'string' && (filename === f.home || filename.startsWith(f.home + path.sep))) {
          return new Proxy(value, { get(target, key) {
            if (key === 'uid') return uid;
            if (key === 'gid') return gid;
            const field = Reflect.get(target, key);
            return typeof field === 'function' ? field.bind(target) : field;
          } });
        }
        return value;
      };
      fs.readFileSync = function(filename, ...args) {
        if (filename === '/proc/self/status') return `CapEff:\t${caps}\n`;
        return read.call(this, filename, ...args);
      };
      const off = await start(f);
      assert.equal(off.bridgeServer, undefined);
      assert.deepEqual(fs.readdirSync(f.runtime), []);
    }
    process.getuid = originalUid;
    process.getgid = originalGid;
    fs.readFileSync = read;
    fs.lstatSync = lstat;
    bridge = await start(f);
    assert.ok(bridge.bridgeServer, 'desktop identity should start');
  } finally {
    process.getuid = originalUid;
    process.getgid = originalGid;
    fs.readFileSync = read;
    fs.lstatSync = lstat;
    if (bridge) await bridge.onunload();
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
test('noncluster, mismatched or invalid vault names stay inert', async () => {
  const f = fixture('IAM Team');
  const dash = fixture('-dash', f.home);
  try {
    for (const env of [{ OBSIDIAN_BRIDGE_INSTANCE: undefined }, { OBSIDIAN_BRIDGE_INSTANCE: 'other' },
                       { OBSIDIAN_BRIDGE_DIR: undefined }, { OBSIDIAN_BRIDGE_VAULT_PARENT: undefined },
                       { OBSIDIAN_BRIDGE_APP_CONFIG: 'relative/obsidian.json' }]) {
      const off = await start(f, env);
      assert.equal(off.bridgeServer, undefined);
    }
    f.vault.getName = () => 'Wrong';
    assert.equal((await start(f)).bridgeServer, undefined);
    f.vault.getName = () => 'iam team';
    assert.equal((await start(f)).bridgeServer, undefined);
    assert.equal((await start(dash)).bridgeServer, undefined);
    assert.deepEqual(fs.readdirSync(f.runtime), []);
    assert.equal(f.calls.writes + dash.calls.writes, 0);
  } finally { fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('vault outside the parent, aliased roots and runtime inside the vault stay inert', async () => {
  const f = fixture('IAM Team');
  let bridge;
  try {
    const elsewhere = path.join(f.home, 'elsewhere');
    fs.mkdirSync(elsewhere);
    assert.equal((await start(f, { OBSIDIAN_BRIDGE_VAULT_PARENT: elsewhere })).bridgeServer, undefined);
    assert.equal((await start(f, { OBSIDIAN_BRIDGE_VAULT_PARENT: 'relative' })).bridgeServer, undefined);
    assert.equal((await start(f, { OBSIDIAN_BRIDGE_VAULT_PARENT: f.home + '/' })).bridgeServer, undefined);
    const parentAlias = path.join(elsewhere, 'home-alias');
    fs.symlinkSync(f.home, parentAlias);
    assert.equal((await start(f, { OBSIDIAN_BRIDGE_VAULT_PARENT: parentAlias })).bridgeServer, undefined);
    // Two levels below the parent, with a matching basename.
    const nested = path.join(f.home, 'Group', 'IAM Team');
    fs.mkdirSync(nested, { recursive: true });
    f.vault.adapter = new FileSystemAdapter(nested);
    assert.equal((await start(f)).bridgeServer, undefined);
    // A symlink directly under the parent that resolves to the real root.
    const alias = path.join(f.home, 'Alias');
    fs.symlinkSync(f.root, alias);
    f.vault.adapter = new FileSystemAdapter(alias);
    f.vault.getName = () => 'Alias';
    assert.equal((await start(f)).bridgeServer, undefined);
    f.vault.adapter = new FileSystemAdapter(f.root);
    f.vault.getName = () => 'IAM Team';
    const inside = path.join(f.root, '.bridge');
    fs.mkdirSync(inside, { mode: 0o700 }); fs.chmodSync(inside, 0o700);
    assert.equal((await start(f, { OBSIDIAN_BRIDGE_DIR: inside })).bridgeServer, undefined);
    assert.deepEqual(fs.readdirSync(inside), []);
    fs.chmodSync(f.runtime, 0o750);
    assert.equal((await start(f)).bridgeServer, undefined);
    fs.chmodSync(f.runtime, 0o700);
    assert.deepEqual(fs.readdirSync(f.runtime), []);
    bridge = await start(f);
    assert.ok(bridge.bridgeServer);
    assert.equal(f.config.vault_id, 'iam-team');
    assert.equal(f.calls.writes, 0);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('second window for a live vault stays inert; unload deregisters and a new window takes over', async () => {
  const f = fixture('IAM Team');
  let first, next;
  try {
    first = await start(f);
    const original = f.config;
    assert.equal(original.vault_id, 'iam-team');
    assert.deepEqual(original.endpoint, { version: 1, vault_id: 'iam-team', app_name: 'IAM Team', app_root: f.root });
    assert.match(original.testToken, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(fs.statSync(path.dirname(original.socket)).mode & 0o777, 0o700);
    assert.equal(fs.statSync(original.endpoint_file).mode & 0o777, 0o600);
    assert.equal(fs.statSync(original.credential_file).mode & 0o777, 0o600);
    const competing = await start(f);
    assert.equal(competing.bridgeServer, undefined);
    await competing.onunload();
    assert.equal(registered(f).testToken, original.testToken);
    assert.equal((await request(original, 'health')).result.vault, 'iam-team');
    await first.onunload(); first = null;
    assert.equal(fs.existsSync(original.endpoint_file), false);
    assert.equal(fs.existsSync(original.credential_file), false);
    await assert.rejects(request(original, 'health'));
    next = await start(f);
    assert.ok(next.bridgeServer);
    assert.equal(f.config.vault_id, 'iam-team');
    assert.notEqual(f.config.testToken, original.testToken);
    assert.equal((await request(f.config, 'health')).ok, true);
    assert.equal((await request(original, 'health', {}, { vault: 'iam-team' }).catch(() => null)), null);
    // A newer window took the entry over: unload must leave that registration alone.
    const takeover = crypto.randomBytes(32).toString('base64url');
    writeOwned(f.config.credential_file, takeover);
    await next.onunload(); next = null;
    assert.equal(fs.readFileSync(f.config.credential_file, 'utf8'), takeover);
    assert.equal(fs.existsSync(f.config.endpoint_file), true);
    // That registration answers nowhere (a crashed window), so the next window replaces it.
    next = await start(f);
    assert.ok(next.bridgeServer);
    assert.equal(f.config.vault_id, 'iam-team');
    assert.notEqual(f.config.testToken, takeover);
    assert.equal((await request(f.config, 'health')).ok, true);
  } finally {
    if (first) await first.onunload(); if (next) await next.onunload();
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
test('different roots with the same slug get distinct vault ids', async () => {
  const a = fixture('IAM Team');
  const b = fixture('IAM.Team', a.home);
  const c = fixture('2026 Notes', a.home);
  let first, second, third;
  try {
    first = await start(a); second = await start(b); third = await start(c);
    const suffix = crypto.createHash('sha256').update(b.root).digest('hex').slice(0, 8);
    assert.equal(a.config.vault_id, 'iam-team');
    assert.equal(b.config.vault_id, `iam-team-${suffix}`);
    assert.equal(c.config.vault_id, 'v-2026-notes');
    assert.equal(b.config.endpoint.app_root, b.root);
    const ha = (await request(a.config, 'health')).result;
    const hb = (await request(b.config, 'health')).result;
    assert.deepEqual([ha.vault, ha.app_name, ha.app_root], ['iam-team', 'IAM Team', a.root]);
    assert.deepEqual([hb.vault, hb.app_name, hb.app_root], [`iam-team-${suffix}`, 'IAM.Team', b.root]);
    assert.equal((await request(a.config, 'health', {}, { vault: b.config.vault_id })).error, 'unauthorized');
    assert.equal((await request({ ...b.config, testToken: a.config.testToken }, 'health').catch(() => null)), null);
    // A restart of the suffixed window keeps its own id.
    await second.onunload(); second = null;
    second = await start(b);
    assert.equal(b.config.vault_id, `iam-team-${suffix}`);
    assert.equal((await request(b.config, 'read', { path: 'Private/note.md' })).result.content, 'initial');
  } finally {
    if (first) await first.onunload(); if (second) await second.onunload(); if (third) await third.onunload();
    fs.rmSync(a.home, { recursive: true, force: true });
  }
});
test('vault management lists direct children, opens and creates vaults with the bridge enabled', async () => {
  const f = fixture('IAM');
  let bridge;
  ipcCalls.length = 0; storage.clear(); onVaultOpen = null;
  try {
    const source = path.join(f.root, '.obsidian', 'plugins', PLUGIN_ID);
    fs.mkdirSync(source, { recursive: true });
    for (const name of ['main.js', 'manifest.json']) fs.copyFileSync(path.join(PLUGIN_SOURCE, name), path.join(source, name));
    fs.writeFileSync(path.join(source, 'data.json'), '{"private":true}');
    const work = path.join(f.home, 'Work');
    fs.mkdirSync(path.join(work, '.obsidian'), { recursive: true });
    fs.writeFileSync(path.join(work, '.obsidian', 'community-plugins.json'), JSON.stringify(['calendar']));
    const appConfig = { vaults: {
      aaaa1111: { path: f.root, ts: 1, open: true },
      bbbb2222: { path: work, ts: 2 },
      cccc3333: { path: path.join(f.home, 'Group', 'Deep'), ts: 3 },
      dddd4444: { path: '/elsewhere/Other', ts: 4 },
      eeee5555: { path: path.join(f.home, '.hidden'), ts: 5 },
      ffff6666: { path: 42 },
    } };
    fs.writeFileSync(f.appConfig, JSON.stringify(appConfig));
    bridge = await start(f);
    assert.ok(bridge.bridgeServer);
    const health = await request(f.config, 'health');
    for (const op of ['vaults', 'open_vault', 'create_vault']) assert.ok(health.result.capabilities.includes(op));
    assert.deepEqual((await request(f.config, 'vaults')).result,
      { vaults: [{ name: 'IAM', open: true }, { name: 'Work', open: false }] });
    assert.equal((await request(f.config, 'vaults', { extra: 1 })).error, 'invalid_request');

    assert.equal((await request(f.config, 'open_vault', { name: 'Missing' })).error, 'not_found');
    assert.equal(fs.existsSync(path.join(f.home, 'Missing')), false);
    assert.equal(ipcCalls.length, 0);

    const opened = await request(f.config, 'open_vault', { name: 'Work' });
    assert.deepEqual(opened.result, { name: 'Work', opened: true });
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(work, '.obsidian', 'community-plugins.json'), 'utf8')),
      ['calendar', PLUGIN_ID]);
    assert.deepEqual(fs.readdirSync(path.join(work, '.obsidian', 'plugins', PLUGIN_ID)).sort(), ['main.js', 'manifest.json']);
    assert.deepEqual(ipcCalls, [['vault-open', work, false]]);
    // Work also enables 'calendar', so vault-wide trust is left for the owner.
    assert.equal(storage.get('enable-plugin-bbbb2222'), undefined);
    assert.equal((await request(f.config, 'open_vault', { name: 'Work' })).ok, true);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(work, '.obsidian', 'community-plugins.json'), 'utf8')),
      ['calendar', PLUGIN_ID]);

    // Obsidian records the new vault in obsidian.json while it opens the window.
    const target = path.join(f.home, 'Research Notes');
    onVaultOpen = (channel, opening) => {
      const document = JSON.parse(fs.readFileSync(f.appConfig, 'utf8'));
      if (!Object.values(document.vaults).some(entry => entry.path === opening)) {
        document.vaults.gggg7777 = { path: opening, ts: 7, open: true };
        fs.writeFileSync(f.appConfig, JSON.stringify(document));
      }
    };
    ipcCalls.length = 0;
    const created = await request(f.config, 'create_vault', { name: 'Research Notes' });
    assert.deepEqual(created.result, { name: 'Research Notes', created: true });
    assert.equal(fs.lstatSync(target).isDirectory(), true);
    const installed = path.join(target, '.obsidian', 'plugins', PLUGIN_ID);
    assert.deepEqual(fs.readdirSync(installed).sort(), ['main.js', 'manifest.json']);
    for (const name of ['main.js', 'manifest.json']) {
      assert.deepEqual(fs.readFileSync(path.join(installed, name)), fs.readFileSync(path.join(source, name)));
    }
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(target, '.obsidian', 'community-plugins.json'), 'utf8')), [PLUGIN_ID]);
    assert.deepEqual(ipcCalls, [['vault-open', target, false]]);
    assert.equal(storage.get('enable-plugin-gggg7777'), 'true');
    assert.deepEqual((await request(f.config, 'vaults')).result.vaults.map(v => v.name), ['IAM', 'Work', 'Research Notes']);

    ipcCalls.length = 0;
    assert.equal((await request(f.config, 'create_vault', { name: 'Research Notes' })).error, 'conflict');
    assert.equal((await request(f.config, 'create_vault', { name: 'Work' })).error, 'conflict');
    const entries = fs.readdirSync(f.home).sort();
    for (const name of ['../x', '.hidden', 'a/b', '', 'x'.repeat(101), 'trailing.', 'trailing ', 'tab\there', 7]) {
      assert.equal((await request(f.config, 'create_vault', { name })).error, 'invalid_request', `create ${name}`);
      assert.equal((await request(f.config, 'open_vault', { name })).error, 'invalid_request', `open ${name}`);
    }
    assert.equal((await request(f.config, 'create_vault', { name: 'Extra', more: 1 })).error, 'invalid_request');
    assert.deepEqual(fs.readdirSync(f.home).sort(), entries);
    assert.equal(fs.existsSync(path.join(path.dirname(f.home), 'x')), false);
    assert.equal(ipcCalls.length, 0);
    assert.equal(f.calls.writes, 0);

    // A partial or outdated managed install in another vault is repaired on open;
    // a foreign file in that directory is refused.
    const partial = path.join(work, '.obsidian', 'plugins', PLUGIN_ID);
    fs.mkdirSync(partial, { recursive: true });
    fs.writeFileSync(path.join(partial, 'main.js'), '// older image');
    assert.equal((await request(f.config, 'open_vault', { name: 'Work' })).ok, true);
    for (const name of ['main.js', 'manifest.json']) {
      assert.ok(fs.readFileSync(path.join(partial, name)).equals(fs.readFileSync(path.join(PLUGIN_SOURCE, name))), name);
    }
    fs.writeFileSync(path.join(partial, 'styles.css'), 'x');
    assert.equal((await request(f.config, 'open_vault', { name: 'Work' })).error, 'conflict');

    // A failed create leaves nothing behind, so a retry starts clean.
    fs.renameSync(path.join(source, 'manifest.json'), path.join(source, 'manifest.json.bak'));
    assert.equal((await request(f.config, 'create_vault', { name: 'Rollback' })).ok, false);
    assert.equal(fs.existsSync(path.join(f.home, 'Rollback')), false);
    fs.renameSync(path.join(source, 'manifest.json.bak'), path.join(source, 'manifest.json'));
    assert.equal((await request(f.config, 'create_vault', { name: 'Rollback' })).ok, true);

    // Obsidian refusing to open (it returns an error string) rolls the create back.
    vaultOpenReply = 'Vault already exists';
    assert.equal((await request(f.config, 'create_vault', { name: 'Refused' })).error, 'internal_error');
    assert.equal(fs.existsSync(path.join(f.home, 'Refused')), false);
    vaultOpenReply = true;

    // At most 16 vaults: each open vault is another desktop window.
    const crowded = { vaults: Object.fromEntries(Array.from({ length: 16 }, (_, i) =>
      [`v${String(i).padStart(15, '0')}`, { path: path.join(f.home, `Vault ${i}`) }])) };
    fs.writeFileSync(f.appConfig, JSON.stringify(crowded));
    assert.equal((await request(f.config, 'create_vault', { name: 'One Too Many' })).error, 'limit_exceeded');
    assert.equal(fs.existsSync(path.join(f.home, 'One Too Many')), false);
  } finally {
    onVaultOpen = null;
    if (bridge) await bridge.onunload();
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
test('organization: state, folders, link-preserving moves, revision-checked replace and trash', async () => {
  const f = fixture('Org');
  let bridge;
  try {
    bridge = await start(f);
    assert.ok(bridge.bridgeServer);
    const health = await request(f.config, 'health');
    for (const op of ['state', 'mkdir', 'move', 'replace', 'trash']) assert.ok(health.result.capabilities.includes(op), op);

    assert.deepEqual((await request(f.config, 'state', { path: 'Nope/x.md' })).result,
      { path: 'Nope/x.md', kind: 'absent', revision: 'absent', count: 0 });
    const noteState = (await request(f.config, 'state', { path: 'Private/note.md' })).result;
    assert.equal(noteState.kind, 'file');
    const folderBefore = (await request(f.config, 'state', { path: 'Private' })).result;
    assert.equal(folderBefore.kind, 'folder');
    assert.equal(folderBefore.count, 2);

    assert.deepEqual((await request(f.config, 'mkdir', { path: 'Projects/2026' })).result, { path: 'Projects/2026', created: true });
    assert.equal((await request(f.config, 'mkdir', { path: 'Projects' })).error, 'conflict');
    assert.equal((await request(f.config, 'mkdir', { path: 'Private/note.md/sub' })).error, 'invalid_path');
    assert.equal((await request(f.config, 'mkdir', { path: '.obsidian/x' })).error, 'invalid_path');

    // Moves go through fileManager.renameFile (which updates links) and refuse occupied targets.
    assert.deepEqual((await request(f.config, 'move', { source: 'Private/note.md', destination: 'Projects/2026/plan.md' })).result,
      { source: 'Private/note.md', destination: 'Projects/2026/plan.md', kind: 'file' });
    assert.deepEqual(f.calls.renamed, [['Private/note.md', 'Projects/2026/plan.md']]);
    assert.equal(fs.readFileSync(path.join(f.root, 'Projects/2026/plan.md'), 'utf8'), 'initial');
    assert.equal((await request(f.config, 'move', { source: 'Private/image.png', destination: 'Projects/2026/plan.md' })).error, 'invalid_request');
    f.add('Projects/other.md', 'other');
    assert.equal((await request(f.config, 'move', { source: 'Projects/other.md', destination: 'Projects/2026/plan.md' })).error, 'conflict');
    assert.equal((await request(f.config, 'move', { source: 'Projects', destination: 'Projects/2026/inside' })).error, 'invalid_path');
    assert.equal((await request(f.config, 'move', { source: 'Missing.md', destination: 'Elsewhere.md' })).error, 'not_found');
    assert.equal((await request(f.config, 'move', { source: 'Projects/2026', destination: 'Archive/Old/2026' })).result.kind, 'folder');
    assert.ok(f.vault.getAbstractFileByPath('Archive/Old/2026/plan.md'));

    // Replace only applies on the exact current revision.
    const current = (await request(f.config, 'read', { path: 'Archive/Old/2026/plan.md' })).result;
    assert.equal((await request(f.config, 'replace', { path: current.path, content: 'stale', expected_revision: noteState.revision.replace(/.$/, '0') })).error, 'conflict');
    const replaced = (await request(f.config, 'replace', { path: current.path, content: 'rewritten', expected_revision: current.revision })).result;
    assert.equal(fs.readFileSync(path.join(f.root, current.path), 'utf8'), 'rewritten');
    assert.equal((await request(f.config, 'replace', { path: current.path, content: 'again', expected_revision: current.revision })).error, 'conflict');

    // Trash moves to the vault's .trash (system=false) and also checks the revision.
    assert.equal((await request(f.config, 'trash', { path: current.path, expected_revision: current.revision })).error, 'conflict');
    assert.deepEqual((await request(f.config, 'trash', { path: current.path, expected_revision: replaced.revision })).result,
      { path: current.path, trashed: true });
    assert.deepEqual(f.calls.trashed, [[current.path, false]]);
    assert.ok(fs.existsSync(path.join(f.root, '.trash', 'plan.md')));
    const archive = (await request(f.config, 'state', { path: 'Archive' })).result;
    assert.equal((await request(f.config, 'trash', { path: 'Archive', expected_revision: folderBefore.revision })).error, 'conflict');
    assert.equal((await request(f.config, 'trash', { path: 'Archive', expected_revision: archive.revision })).result.trashed, true);
    assert.equal(f.vault.getAbstractFileByPath('Archive'), null);
  } finally {
    if (bridge) await bridge.onunload();
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
test('two exact vaults, typed operations, conflicts, embed and protocol rejection', async () => {
  const a = fixture('IAM Team');
  const b = fixture('Homelab', a.home);
  let first, second;
  try {
    first = await start(a); second = await start(b);
    const competing = await start(a);
    assert.equal(competing.bridgeServer, undefined);
    await competing.onunload();
    assert.equal((await request(a.config, 'health')).result.vault, 'iam-team');
    assert.equal((await request(b.config, 'health')).result.app_name, 'Homelab');
    assert.equal((await request(a.config, 'health', {}, { vault: 'homelab' })).error, 'unauthorized');
    const listed = await request(a.config, 'list', { prefix: '', limit: 100 });
    assert.deepEqual(listed.result.entries.map(e => e.path), ['Private']);
    const nested = await request(a.config, 'list', { prefix: 'Private', limit: 100 });
    assert.deepEqual(nested.result.entries.map(e => e.path), ['Private/image.png', 'Private/note.md']);
    assert.equal((await request(a.config, 'list', { prefix: 'Private', limit: 1 })).error, 'limit_exceeded');
    const firstRead = await request(a.config, 'read', { path: 'Private/note.md' });
    assert.equal(firstRead.result.content, 'initial');
    assert.equal((await request(a.config, 'search', { query: 'init', limit: 5 })).result.matches[0].path, 'Private/note.md');
    assert.equal((await request(a.config, 'create', { path: 'Private/new.md', content: 'new' })).ok, true);
    assert.equal((await request(a.config, 'create', { path: 'Private/new.md', content: 'overwrite' })).error, 'conflict');
    assert.equal((await request(a.config, 'create', { path: 'Projects/Deep/nested.md', content: 'nested' })).ok, true);
    assert.equal(a.vault.getAbstractFileByPath('Projects/Deep').path, 'Projects/Deep');
    assert.equal((await request(a.config, 'read', { path: 'Projects/Deep/nested.md' })).result.content, 'nested');
    assert.equal((await request(a.config, 'create', { path: 'Private/note.md/child.md', content: 'x' })).error, 'invalid_path');
    assert.equal((await request(a.config, 'create', { path: 'New/.hidden/x.md', content: 'x' })).error, 'invalid_path');
    a.add('Private/note.md', 'editor update');
    const before = a.calls.writes;
    assert.equal((await request(a.config, 'append', { path: 'Private/note.md', content: '+', expected_revision: firstRead.result.revision })).error, 'conflict');
    assert.equal(a.calls.writes, before);
    const fresh = await request(a.config, 'read', { path: 'Private/note.md' });
    const appended = await request(a.config, 'append', { path: 'Private/note.md', content: '+', expected_revision: fresh.result.revision });
    assert.equal(appended.ok, true);
    assert.equal((await request(a.config, 'read', { path: 'Private/note.md' })).result.content, 'editor update+');
    assert.equal((await request(b.config, 'read', { path: 'Private/note.md' })).result.content, 'initial');
    const embed = await request(a.config, 'embed', { source: 'Private/note.md', target: '[[image.png]]' });
    assert.equal(embed.result.mime, 'image/png');
    assert.equal(Buffer.from(embed.result.data, 'base64').length, 6);
    assert.equal((await request(a.config, 'embed', { source: 'Private/note.md', target: '![[image.png]]' })).ok, true);
    assert.equal((await request(a.config, 'embed', { source: 'Private/note.md', target: '![alt](image.png)' })).ok, true);
    const counts = { ...a.calls };
    assert.equal((await request(a.config, 'create', { path: '../escape.md', content: 'x' })).error, 'invalid_path');
    assert.equal((await request(a.config, 'create', { path: '.obsidian/config.md', content: 'x' })).error, 'invalid_path');
    assert.equal((await request(a.config, 'create', { path: 'Private/new.md', content: 'x' }, { token: 'bad' })).error, 'unauthorized');
    assert.equal((await request(a.config, 'create', { path: 'Private/other.md', content: 'x' }, { token: b.config.testToken })).error, 'unauthorized');
    assert.equal((await request(a.config, 'delete', {})).error, 'unknown_operation');
    assert.equal((await request(a.config, 'read', { path: 'Private/note.md', extra: true })).error, 'invalid_request');
    assert.equal((await request(a.config, 'read', { path: 'Private/note.md' }, { token: undefined })).error, 'unauthorized');
    assert.equal((await request(a.config, 'read', { path: 'Private/note.md' }, { extra: true })).error, 'unauthorized');
    const malformed = await rawRequest(a.config, '{broken json}\n');
    assert.equal(JSON.parse(malformed).error, 'internal_error');
    assert.equal(malformed.includes(a.config.testToken), false);
    assert.equal(await rawRequest(a.config, 'x'.repeat(384 * 1024 + 1)), '');
    assert.equal(a.calls.writes, counts.writes);
    assert.equal(a.calls.binaries, counts.binaries);
    fs.symlinkSync(a.root, path.join(a.root, 'linked'));
    assert.equal((await request(a.config, 'read', { path: 'linked/Private/note.md' })).error, 'invalid_path');
    assert.equal((await request(a.config, 'embed', { source: 'Private/note.md', target: 'https://example.org/a.png' })).error, 'invalid_path');
    assert.equal((await request(a.config, 'embed', { source: 'Private/note.md', target: '../../outside.png' })).error, 'invalid_path');
  } finally {
    if (first) await first.onunload(); if (second) await second.onunload();
    assert.equal(fs.existsSync(a.config.socket), false);
    assert.equal(fs.existsSync(b.config.socket), false);
    fs.rmSync(a.home, { recursive: true, force: true });
  }
});
test('stale pathname does not affect abstract listener or get unlinked', async () => {
  const f = fixture('IAM Team');
  try {
    const directory = path.join(f.runtime, 'iam-team');
    fs.mkdirSync(directory, { mode: 0o700 }); fs.chmodSync(directory, 0o700);
    const stale = path.join(directory, 'iam-team.sock');
    fs.writeFileSync(stale, 'foreign');
    const bridge = await start(f);
    assert.ok(bridge.bridgeServer);
    assert.equal(f.config.socket, stale);
    assert.equal((await request(f.config, 'health')).ok, true);
    assert.equal(fs.readFileSync(stale, 'utf8'), 'foreign');
    await bridge.onunload();
    assert.equal(fs.readFileSync(stale, 'utf8'), 'foreign');
  } finally { fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('search counts actual app read bytes when file metadata is stale', async () => {
  const f = fixture('IAM Team');
  let bridge;
  try {
    for (let i = 0; i < 17; i++) {
      const name = `Private/bulk${i}.md`;
      f.add(name, 'x'.repeat(250 * 1024));
      f.vault.getAbstractFileByPath(name).stat.size = 0;
    }
    bridge = await start(f);
    const result = await request(f.config, 'search', { query: 'absent', limit: 10 });
    assert.equal(result.error, 'limit_exceeded');
    assert.equal(f.calls.writes, 0);
  } finally {
    if (bridge) await bridge.onunload();
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
test('fresh adapter stat rejects oversized underreported note and media before full reads', async () => {
  const f = fixture('IAM Team');
  let bridge;
  try {
    f.add('Private/huge.md', 'x'.repeat(256 * 1024 + 1));
    f.add('Private/image.png', Buffer.alloc(512 * 1024 + 1));
    f.vault.getAbstractFileByPath('Private/huge.md').stat.size = 1;
    f.vault.getAbstractFileByPath('Private/image.png').stat.size = 1;
    bridge = await start(f);
    const before = { ...f.calls };
    assert.equal((await request(f.config, 'read', { path: 'Private/huge.md' })).error, 'limit_exceeded');
    assert.equal((await request(f.config, 'search', { query: 'missing', limit: 5 })).ok, true);
    assert.equal((await request(f.config, 'embed', { source: 'Private/note.md', target: 'image.png' })).error, 'limit_exceeded');
    assert.equal(f.calls.reads - before.reads, 1);
    assert.equal(f.calls.binaries - before.binaries, 0);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('root replacement before create refuses all outside writes', async () => {
  const f = fixture('IAM Team');
  const outside = path.join(f.home, 'outside');
  let bridge;
  try {
    bridge = await start(f);
    fs.mkdirSync(outside);
    fs.renameSync(f.root, f.root + '-old');
    fs.symlinkSync(outside, f.root);
    const before = f.calls.writes;
    assert.equal((await request(f.config, 'create', { path: 'new.md', content: 'unsafe' })).error, 'unavailable');
    assert.equal(f.calls.writes, before);
    assert.equal(fs.existsSync(path.join(outside, 'new.md')), false);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('different directory at the enrolled root is rejected by inode identity', async () => {
  const f = fixture('IAM Team');
  let bridge;
  try {
    bridge = await start(f);
    fs.renameSync(f.root, f.root + '-old');
    fs.mkdirSync(f.root);
    assert.equal((await request(f.config, 'create', { path: 'new.md', content: 'unsafe' })).error, 'unavailable');
    assert.equal((await request(f.config, 'vaults')).error, 'unavailable');
    assert.equal(fs.existsSync(path.join(f.root, 'new.md')), false);
    assert.equal(f.calls.writes, 0);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('externally created destination retains its bytes and receipt is indeterminate', async () => {
  const f = fixture('IAM Team');
  let bridge;
  try {
    bridge = await start(f);
    assert.ok(bridge.bridgeServer);
    f.vault.externalCreateAtWrite = key => fs.writeFileSync(path.join(f.root, key), 'external', { flag: 'wx' });
    const result = await request(f.config, 'create', { path: 'Private/race.md', content: 'bridge' });
    assert.equal(result.result.status, 'indeterminate');
    assert.equal(fs.readFileSync(path.join(f.root, 'Private/race.md'), 'utf8'), 'external');
    assert.equal(f.calls.writes, 0);
    f.vault.externalCreateAtWrite = null;
    const success = await request(f.config, 'create', { path: 'Private/unicode.md', content: 'café' });
    assert.equal(success.ok, true);
    assert.equal(fs.readFileSync(path.join(f.root, 'Private/unicode.md'), 'utf8'), 'café');
  } finally {
    if (bridge) await bridge.onunload();
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
}
