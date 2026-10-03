'use strict';

const { Plugin, FileSystemAdapter, TFile, TFolder } = require('obsidian');
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
function attestedMount(root) {
  if (process.getuid() !== 1000 || process.getgid() !== 1000) fail('unavailable');
  const status = fs.readFileSync('/proc/self/status', 'utf8');
  const caps = /^CapEff:\s*([0-9a-f]+)$/mi.exec(status);
  if (!caps || BigInt(`0x${caps[1]}`) !== 0n) fail('unavailable');
  const mountpoints = fs.readFileSync('/proc/self/mountinfo', 'utf8').split('\n')
    .filter(Boolean).map(line => line.split(' '))
    .filter(fields => fields.length >= 10 && fields.includes('-'))
    .map(fields => fields[4].replace(/\\([0-7]{3})/g, (_, octal) => String.fromCharCode(parseInt(octal, 8))));
  if (mountpoints.filter(point => point === root).length !== 1) fail('unavailable');
}
function verifyRoot(app, config) {
  try {
    if (!(app.vault.adapter instanceof FileSystemAdapter) ||
        app.vault.adapter.getBasePath() !== config.root || app.vault.getName() !== config.appName ||
        canonicalRoot(config.root) !== config.root) fail('unavailable');
    attestedMount(config.root);
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
function bootstrap(app) {
  if (process.platform !== 'linux' || process.env.OBSIDIAN_BRIDGE_INSTANCE !== 'kubernetes-obsidian') return null;
  const [nodeMajor, nodeMinor] = process.versions.node.split('.').map(Number);
  if (nodeMajor < 20 || (nodeMajor === 20 && nodeMinor < 8)) return null;
  const directory = process.env.OBSIDIAN_BRIDGE_DIR;
  if (!directory || !path.isAbsolute(directory) || path.normalize(directory) !== directory) return null;
  try {
    privateDirectory(directory);
    if (!(app.vault.adapter instanceof FileSystemAdapter)) return null;
    const appRoot = canonicalRoot(app.vault.adapter.getBasePath());
    const registry = JSON.parse(privateFile(path.join(directory, 'registry.json'), directory, 64 * 1024));
    if (!shape(registry, ['version', 'instance', 'endpoints']) || registry.version !== VERSION ||
        registry.instance !== 'kubernetes-obsidian' || !Array.isArray(registry.endpoints) ||
        registry.endpoints.length < 1 || registry.endpoints.length > 64) return null;
    const ids = new Set(); const roots = new Set(); const tokens = new Set();
    let selected = null; let selectedToken = null;
    for (const config of registry.endpoints) {
      if (!shape(config, ['vault_id', 'app_name', 'app_root', 'socket', 'credential_file']) ||
          !/^[a-z][a-z0-9_-]{0,63}$/.test(config.vault_id) ||
          ids.has(config.vault_id)) return null;
      ids.add(config.vault_id);
      string(config.app_name, 128);
      if (typeof config.app_root !== 'string' || !path.isAbsolute(config.app_root) ||
          path.normalize(config.app_root) !== config.app_root) return null;
      const root = config.app_root;
      if (root !== appRoot) {
        try { canonicalRoot(root); }
        catch (error) { if (error.code !== 'ENOENT') return null; }
      }
      if ([...roots].some(other => root === other || root.startsWith(other + path.sep) || other.startsWith(root + path.sep)) ||
          directory === root || directory.startsWith(root + path.sep) ||
          root.startsWith(directory + path.sep)) return null;
      roots.add(root);
      const runtime = path.join(directory, config.vault_id);
      privateDirectory(runtime);
      if (config.socket !== path.join(runtime, `${config.vault_id}.sock`) ||
          config.credential_file !== path.join(runtime, 'credential')) return null;
      const token = privateFile(config.credential_file, runtime).trim();
      if (!/^[A-Za-z0-9_-]{43,128}$/.test(token) || tokens.has(token)) return null;
      tokens.add(token);
      if (root === appRoot) { selected = config; selectedToken = token; }
    }
    if (!selected || selected.app_name !== app.vault.getName()) return null;
    attestedMount(appRoot);
    const rootStat = fs.lstatSync(appRoot);
    return { id: selected.vault_id, appName: selected.app_name, root: appRoot,
      rootDev: rootStat.dev, rootIno: rootStat.ino, socket: selected.socket, token: selectedToken };
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

async function execute(app, config, op, args, markStarted = () => {}) {
  verifyRoot(app, config);
  const vault = app.vault;
  const root = config.root;
  if (op === 'health') {
    argument(args, []);
    return { protocol: VERSION, vault: config.id, app_name: vault.getName(), app_root: root, capabilities: ['health', 'list', 'read', 'search', 'create', 'append', 'embed', 'reserve', 'receipt'] };
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
    if (parent !== '.' && !(vault.getAbstractFileByPath(parent) instanceof TFolder)) fail('not_found');
    verifyRoot(app, config);
    safePath(key, root, true);
    const bytes = Buffer.from(args.content, 'utf8');
    const data = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    markStarted();
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

class PrivateBridge extends Plugin {
  async onload() {
    const config = bootstrap(this.app);
    if (!config) return;
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
      this.bridgeServer = server;
    } catch { if (server.listening) server.close(); }
  }
  async onunload() {
    if (!this.bridgeServer) return;
    await new Promise(resolve => this.bridgeServer.close(resolve));
    this.bridgeServer = null;
  }
}
module.exports = PrivateBridge;
