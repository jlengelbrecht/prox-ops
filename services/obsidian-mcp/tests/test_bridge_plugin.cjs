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
  const a = fixture('iam', 'IAM Team');
  const b = fixture('homelab', 'Homelab', a.home);
  let first, second;
  try {
    assert.notEqual(a.config.testToken, b.config.testToken);
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
    for (const f of [a, b]) {
      assert.equal(fs.statSync(f.registry).mode & 0o777, 0o700);
      assert.equal(fs.statSync(f.runtime).mode & 0o777, 0o700);
      assert.equal(fs.statSync(f.registryFile).mode & 0o777, 0o600);
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
class TFile { constructor(filePath, size) { this.path = filePath; this.stat = { size }; } }
class TFolder { constructor(filePath) { this.path = filePath; } }
const originalLoad = Module._load;
Module._load = function(request, parent, main) {
  if (request === 'obsidian') return { Plugin, FileSystemAdapter, TFile, TFolder };
  return originalLoad.call(this, request, parent, main);
};
const Bridge = require('../bridge-plugin/main.js');
Module._load = originalLoad;
const mountedRoots = new Set();
const originalReadFileSync = fs.readFileSync;
fs.readFileSync = function(filename, ...args) {
  if (filename === '/proc/self/mountinfo') {
    return [...mountedRoots].map((root, index) =>
      `${100 + index} 1 0:1 / ${root.replace(/ /g, '\\040')} rw - tmpfs tmpfs rw`).join('\n') + '\n';
  }
  return originalReadFileSync.call(this, filename, ...args);
};
function fixture(id, name, sharedHome) {
  const home = sharedHome || fs.mkdtempSync(path.join(os.tmpdir(), `bridge-${id}-`));
  const root = path.join(home, `${id}-vault`);
  const registry = path.join(home, 'runtime');
  const runtime = path.join(registry, id);
  fs.mkdirSync(root); fs.mkdirSync(registry, { recursive: true, mode: 0o700 });
  mountedRoots.add(root);
  fs.mkdirSync(runtime, { mode: 0o700 });
  fs.chmodSync(registry, 0o700); fs.chmodSync(runtime, 0o700);
  const files = new Map();
  const folders = new Map([['Private', new TFolder('Private')]]);
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
    async process(file, callback) {
      const current = fs.readFileSync(path.join(root, file.path), 'utf8');
      const next = callback(current);
      calls.writes++; add(file.path, next); return next;
    },
  };
  const app = { vault, metadataCache: { getFirstLinkpathDest: (target) => {
    if (target === 'image.png' || target === 'Private/image.png') return files.get('Private/image.png');
    return null;
  } } };
  const config = { vault_id: id, app_name: name, app_root: root,
    socket: path.join(runtime, `${id}.sock`), credential_file: path.join(runtime, 'credential') };
  const token = crypto.randomBytes(32).toString('base64url');
  const registryFile = path.join(registry, 'registry.json');
  const previous = fs.existsSync(registryFile) ? JSON.parse(fs.readFileSync(registryFile, 'utf8')).endpoints : [];
  fs.writeFileSync(registryFile, JSON.stringify({ version: 1, instance: 'kubernetes-obsidian',
    endpoints: [...previous, config] }), { mode: 0o600 });
  fs.writeFileSync(config.credential_file, token, { mode: 0o600 });
  fs.chmodSync(registryFile, 0o600);
  fs.chmodSync(config.credential_file, 0o600);
  return { home, root, runtime, registry, registryFile, config: { ...config, testToken: token }, app, vault, calls, add };
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
async function start(f) {
  process.env.OBSIDIAN_BRIDGE_INSTANCE = 'kubernetes-obsidian';
  process.env.OBSIDIAN_BRIDGE_DIR = f.registry;
  const bridge = new Bridge(f.app);
  await bridge.onload();
  return bridge;
}
if (!REAL_IPC) {
test('running receipt survives disconnect window and restart refuses replay', async () => {
  const f = fixture('lifetime', 'Lifetime');
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
    await bridge.onunload();
    bridge = await start(f);
    assert.equal((await request(f.config, 'receipt', { ...args, operation: 'append' })).result.status, 'indeterminate');
    assert.equal((await request(f.config, 'mutate', args, { op: 'append' })).error, 'unknown_receipt');
    assert.equal(f.calls.writes, 1);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('receipt binds exact mutation and replay returns the committed append once', async () => {
  const f = fixture('receipt', 'Receipts');
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
  const f = fixture('identity', 'Identity');
  const originalUid = process.getuid;
  const originalGid = process.getgid;
  const read = fs.readFileSync;
  const lstat = fs.lstatSync;
  try {
    for (const [uid, gid, caps] of [[1001, 1000, '0000000000000000'],
                                    [1000, 1001, '0000000000000000'],
                                    [1000, 1000, '0000000000000001']]) {
      process.getuid = () => uid;
      process.getgid = () => gid;
      fs.lstatSync = function(filename, ...args) {
        const value = lstat.call(this, filename, ...args);
        if ([f.registry, f.runtime, f.registryFile, f.config.credential_file].includes(filename)) {
          return new Proxy(value, { get(target, key) { return key === 'uid' ? uid : Reflect.get(target, key); } });
        }
        return value;
      };
      fs.readFileSync = function(filename, ...args) {
        if (filename === '/proc/self/status') return `CapEff:\t${caps}\n`;
        return read.call(this, filename, ...args);
      };
      const off = await start(f);
      assert.equal(off.bridgeServer, undefined);
    }
  } finally {
    process.getuid = originalUid;
    process.getgid = originalGid;
    fs.readFileSync = read;
    fs.lstatSync = lstat;
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
test('noncluster and wrong app identity stay inert', async () => {
  const f = fixture('iam', 'IAM Team');
  try {
    delete process.env.OBSIDIAN_BRIDGE_INSTANCE;
    process.env.OBSIDIAN_BRIDGE_DIR = f.registry;
    const off = new Bridge(f.app); await off.onload();
    assert.equal(off.bridgeServer, undefined);
    assert.equal(fs.existsSync(f.config.socket), false);
    process.env.OBSIDIAN_BRIDGE_INSTANCE = 'kubernetes-obsidian';
    f.vault.getName = () => 'Wrong';
    await off.onload();
    assert.equal(off.bridgeServer, undefined);
    assert.equal(fs.existsSync(f.config.socket), false);
    assert.equal(f.calls.writes, 0);
  } finally { fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('two exact vaults, typed operations, conflicts, embed and protocol rejection', async () => {
  const a = fixture('iam', 'IAM Team');
  const b = fixture('homelab', 'Homelab', a.home);
  let first, second;
  try {
    first = await start(a); second = await start(b);
    const competing = await start(a);
    assert.equal(competing.bridgeServer, undefined);
    await competing.onunload();
    assert.equal((await request(a.config, 'health')).result.vault, 'iam');
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
  const f = fixture('iam', 'IAM Team');
  try {
    fs.writeFileSync(f.config.socket, 'foreign');
    const bridge = await start(f);
    assert.ok(bridge.bridgeServer);
    assert.equal((await request(f.config, 'health')).ok, true);
    assert.equal(fs.readFileSync(f.config.socket, 'utf8'), 'foreign');
    await bridge.onunload();
    assert.equal(fs.readFileSync(f.config.socket, 'utf8'), 'foreign');
  } finally { fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('search counts actual app read bytes when file metadata is stale', async () => {
  const f = fixture('iam', 'IAM Team');
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
  const f = fixture('iam', 'IAM Team');
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
  const f = fixture('iam', 'IAM Team');
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
  const f = fixture('iam', 'IAM Team');
  let bridge;
  try {
    bridge = await start(f);
    fs.renameSync(f.root, f.root + '-old');
    fs.mkdirSync(f.root);
    assert.equal((await request(f.config, 'create', { path: 'new.md', content: 'unsafe' })).error, 'unavailable');
    assert.equal(fs.existsSync(path.join(f.root, 'new.md')), false);
    assert.equal(f.calls.writes, 0);
  } finally { if (bridge) await bridge.onunload(); fs.rmSync(f.home, { recursive: true, force: true }); }
});
test('ordinary unpinned root is inert and externally created destination retains bytes', async () => {
  const f = fixture('iam', 'IAM Team');
  let bridge;
  try {
    mountedRoots.delete(f.root);
    const off = await start(f);
    assert.equal(off.bridgeServer, undefined);
    mountedRoots.add(f.root);
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
    mountedRoots.delete(f.root);
    fs.rmSync(f.home, { recursive: true, force: true });
  }
});
test('shared registry enrolls a third vault and rejects absent or duplicate roots', async () => {
  const a = fixture('iam', 'IAM Team');
  const b = fixture('homelab', 'Homelab', a.home);
  const c = fixture('third', 'Third', a.home);
  let first, second, third;
  try {
    first = await start(a); second = await start(b); third = await start(c);
    assert.equal(process.env.OBSIDIAN_BRIDGE_DIR, a.registry);
    assert.equal((await request(c.config, 'health')).result.vault, 'third');
    assert.equal((await request(a.config, 'health')).result.vault, 'iam');
    await first.onunload(); first = null;
    fs.renameSync(c.root, c.root + '-closed');
    first = await start(a);
    assert.ok(first.bridgeServer);
    assert.equal((await request(a.config, 'health')).result.vault, 'iam');
    fs.renameSync(c.root + '-closed', c.root);
    const absent = { vault: { ...c.vault, adapter: new FileSystemAdapter(path.join(a.home, 'absent')) } };
    fs.mkdirSync(path.join(a.home, 'absent'));
    const off = new Bridge(absent); await off.onload();
    assert.equal(off.bridgeServer, undefined);
    const entries = JSON.parse(fs.readFileSync(a.registryFile, 'utf8'));
    entries.endpoints[2].app_root = a.root;
    fs.writeFileSync(a.registryFile, JSON.stringify(entries));
    await first.onunload(); first = null;
    const duplicate = new Bridge(a.app); await duplicate.onload();
    assert.equal(duplicate.bridgeServer, undefined);
    entries.endpoints[2].app_root = c.root;
    fs.writeFileSync(a.registryFile, JSON.stringify(entries));
    fs.symlinkSync(a.root, path.join(a.home, 'iam-alias'));
    entries.endpoints[2].app_root = path.join(a.home, 'iam-alias');
    fs.writeFileSync(a.registryFile, JSON.stringify(entries));
    const aliased = new Bridge(a.app); await aliased.onload();
    assert.equal(aliased.bridgeServer, undefined);
    entries.endpoints[2].app_root = c.root;
    fs.writeFileSync(a.registryFile, JSON.stringify(entries));
    fs.writeFileSync(c.config.credential_file, a.config.testToken);
    const reusedCredential = new Bridge(a.app); await reusedCredential.onload();
    assert.equal(reusedCredential.bridgeServer, undefined);
  } finally {
    if (first) await first.onunload(); if (second) await second.onunload(); if (third) await third.onunload();
    fs.rmSync(a.home, { recursive: true, force: true });
  }
});
}
