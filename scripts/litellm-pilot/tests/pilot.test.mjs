import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile, mkdir, symlink, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createPilotHook, createParamsHook } from '../claude-bearer.mjs';
import { buildConfig, generate, parseArgs } from '../generate.mjs';

const gateway = 'https://ai-api.homelab0.org/v1';
const token1 = 'sk-ant-oatTEST1';
const token2 = 'sk-ant-oatTEST2';
const key = 'sk-SYNTHETIC_GATEWAY';
const selection = { model: 'qwen36-27b', smallModel: 'qwen36-27b' };
const context = (model = 'anthropic/claude-sonnet-5', provider = 'litellm-pilot', baseURL = gateway) => ({
  model: { id: model, providerID: provider, api: { url: baseURL } },
  provider: { info: { id: provider }, options: { baseURL } },
});
const scoped = (extra = {}) => ({ 'x-litellm-api-key': `Bearer ${key}`, ...extra });
const valid = (token = token1, expiresAt = Date.now() + 120_000) => ({ accessToken: token, expiresAt });

async function fixture(fn) {
  const dir = await mkdtemp(join(tmpdir(), 'litellm-pilot-'));
  try { return await fn(dir); } finally { await rm(dir, { recursive: true, force: true }); }
}

test('explicit Claude request separates gateway key and current bearer without mutating native record', async () => fixture(async (dir) => {
  const file = join(dir, 'credentials.json');
  const record = JSON.stringify(valid());
  await writeFile(file, record);
  const hook = createPilotHook({ readRecord: async () => JSON.parse(await readFile(file, 'utf8')), getGatewayKey: () => key });
  const output = { headers: scoped({ unrelated: 'kept' }) };
  await hook(context(), output);
  assert.deepEqual(output.headers, { unrelated: 'kept', 'x-litellm-api-key': `Bearer ${key}`, Authorization: `Bearer ${token1}` });
  assert.equal(await readFile(file, 'utf8'), record);
}));

test('other provider and local/ChatGPT aliases retain validated scoped auth without Claude read', async () => {
  let reads = 0;
  const hook = createPilotHook({ readRecord: async () => { reads++; throw Error('read'); }, getGatewayKey: () => key });
  for (const input of [context('anthropic/claude-sonnet-5', 'other'), context('qwen36-27b'), context('chatgpt-gpt-6-luna')]) {
    const headers = input.provider.info.id === 'other' ? { AUTHORIZATION: 'ordinary' } : scoped();
    await hook(input, { headers });
    assert.deepEqual(headers, input.provider.info.id === 'other' ? { AUTHORIZATION: 'ordinary' } : scoped());
  }
  assert.equal(reads, 0);
});

test('unqualified and unknown aliases deny before native token read', async () => {
  let reads = 0;
  const hook = createPilotHook({ readRecord: async () => { reads++; return valid(); } });
  const output = { headers: {} };
  for (const alias of ['project-router', 'smart-router', 'gpt-4']) {
    await assert.rejects(hook(context(alias), output), { message: 'Pilot Claude credential unavailable or request target not approved' });
  }
  assert.equal(reads, 0);
  assert.deepEqual(output.headers, {});
});

test('wrong origin, HTTP, path, model URL and prior Authorization fail closed before read', async () => {
  let reads = 0;
  const hook = createPilotHook({ readRecord: async () => { reads++; return valid(); }, getGatewayKey: () => key });
  for (const url of ['http://ai-api.homelab0.org/v1', 'https://evil.example/v1', `${gateway}/other`, `${gateway}?x=1`]) {
    const output = { headers: {} };
    await assert.rejects(hook(context(undefined, undefined, url), output));
    assert.deepEqual(output.headers, {});
  }
  const changed = context(); changed.model.api.url = 'https://evil.example/v1';
  await assert.rejects(hook(changed, { headers: {} }));
  const changedBase = context(); changedBase.provider.options.baseURL = 'https://evil.example/v1';
  await assert.rejects(hook(changedBase, { headers: {} }));
  const changedId = context(); changedId.model.api.id = 'gpt-4';
  await assert.rejects(hook(changedId, { headers: {} }));
  await assert.rejects(hook(context(), { headers: scoped({ aUtHoRiZaTiOn: 'existing' }) }));
  for (const alias of ['qwen36-27b', 'chatgpt-gpt-6-luna']) {
    await assert.rejects(hook(context(alias, 'litellm-pilot', 'https://evil.example/v1'), { headers: {} }));
  }
  assert.equal(reads, 0);
});

test('missing key, malformed/expired access and failed native read have redacted diagnostics, no fallback', async () => {
  const cases = [
    { getGatewayKey: () => undefined, readRecord: async () => valid() },
    { getGatewayKey: () => key, readRecord: async () => valid('BAD_SECRET') },
    { getGatewayKey: () => key, readRecord: async () => valid(token1, Date.now() - 1) },
    { getGatewayKey: () => key, readRecord: async () => valid(token1, 'tomorrow') },
    { getGatewayKey: () => token1, readRecord: async () => valid() },
    { getGatewayKey: () => 'sk-ant-apiSYNTHETIC', readRecord: async () => valid() },
    { getGatewayKey: () => key, readRecord: async () => { throw Error('SENSITIVE_NATIVE_PATH'); } },
  ];
  for (const dependencies of cases) {
    const output = { headers: scoped({ untouched: 'yes' }) };
    await assert.rejects(createPilotHook(dependencies)(context(), output), (error) => {
      assert.equal(error.message, 'Pilot Claude credential unavailable or request target not approved');
      for (const secret of [token1, key, 'BAD_SECRET', 'SENSITIVE_NATIVE_PATH']) assert.ok(!error.message.includes(secret));
      return true;
    });
    assert.deepEqual(output.headers, scoped({ untouched: 'yes' }));
  }
});

test('new access record is read on every explicit Claude request', async () => fixture(async (dir) => {
  const file = join(dir, 'credentials.json');
  const readRecord = async () => JSON.parse(await readFile(file, 'utf8'));
  const hook = createPilotHook({ readRecord, getGatewayKey: () => key });
  await writeFile(file, JSON.stringify(valid(token1)));
  const first = { headers: scoped() }; await hook(context(), first);
  await writeFile(file, JSON.stringify(valid(token2)));
  const second = { headers: scoped() }; await hook(context(), second);
  assert.equal(first.headers.Authorization, `Bearer ${token1}`);
  assert.equal(second.headers.Authorization, `Bearer ${token2}`);
}));

test('default reader follows a synthetic Claude config directory and projects only access fields', async () => fixture(async (dir) => {
  const previous = process.env.CLAUDE_CONFIG_DIR;
  process.env.CLAUDE_CONFIG_DIR = dir;
  try {
    await writeFile(join(dir, '.credentials.json'), JSON.stringify({ claudeAiOauth: { ...valid(), refreshToken: 'SYNTHETIC_REFRESH' } }));
    const output = { headers: scoped() };
    await createPilotHook({ getGatewayKey: () => key })(context(), output);
    assert.equal(output.headers.Authorization, `Bearer ${token1}`);
    assert.ok(!JSON.stringify(output).includes('SYNTHETIC_REFRESH'));
  } finally { if (previous === undefined) delete process.env.CLAUDE_CONFIG_DIR; else process.env.CLAUDE_CONFIG_DIR = previous; }
}));

test('pilot aliases reject provider auth overrides and malformed header containers', async () => {
  const hook = createPilotHook({ getGatewayKey: () => key, readRecord: async () => valid() });
  for (const change of [
    (input) => { input.provider.options.apiKey = 'sk-OTHER'; },
    (input) => { input.provider.options.headers = { AUTHORIZATION: 'Bearer other' }; },
    (input) => { input.model.providerID = 'other'; },
  ]) {
    const input = context('qwen36-27b'); change(input);
    await assert.rejects(hook(input, { headers: {} }), { message: 'Pilot Claude credential unavailable or request target not approved' });
  }
  await assert.rejects(hook(context(), { headers: [] }), { message: 'Pilot Claude credential unavailable or request target not approved' });
});

test('pilot identity drift, outgoing auth and destination overrides fail before native read', async () => {
  let reads = 0;
  const hook = createPilotHook({ getGatewayKey: () => key, readRecord: async () => { reads++; return valid(); } });
  const cases = [
    (input) => { delete input.provider.info.id; },
    (input) => { input.provider.info.id = 'other'; },
    (input) => { delete input.model.providerID; },
    (input) => { input.model.api.npm = '@ai-sdk/anthropic'; },
    (input) => { input.model.options = { api_base: 'https://evil.example' }; },
    (input) => { input.model.variants = { fast: { baseURL: 'https://evil.example' } }; },
    (input) => { input.provider.options.extra = { endpoint: 'https://evil.example' }; },
    (input) => { input.model.headers = { 'x-api-key': 'other' }; },
    (input) => { input.provider.options.headers = { 'X-LiteLLM-API-Key': 'other' }; },
  ];
  for (const change of cases) {
    const input = context(); change(input);
    const output = { headers: scoped() };
    await assert.rejects(hook(input, output), { message: 'Pilot Claude credential unavailable or request target not approved' });
    assert.deepEqual(output.headers, scoped());
  }
  for (const alias of ['qwen36-27b', 'chatgpt-gpt-6-luna']) {
    for (const headers of [
      { 'x-litellm-api-key': 'Bearer sk-OTHER' },
      { 'X-LiteLLM-API-Key': `Bearer ${key}`, 'x-litellm-api-key': `Bearer ${key}` },
      scoped({ Authorization: 'Bearer foreign' }),
      scoped({ 'x-api-key': 'foreign' }),
    ]) {
      await assert.rejects(hook(context(alias), { headers }), { message: 'Pilot Claude credential unavailable or request target not approved' });
    }
  }
  assert.equal(reads, 0);
});

test('parameter hook rejects body destination overrides for pilot aliases', async () => {
  const hook = createParamsHook();
  for (const alias of ['anthropic/claude-sonnet-5', 'qwen36-27b']) {
    await hook(context(alias), { options: { temperature: 0 } });
    for (const options of [{ api_base: 'https://evil.example' }, { nested: { base_url: 'https://evil.example' } }, { api_key: 'other' }]) {
      await assert.rejects(hook(context(alias), { options }), { message: 'Pilot Claude credential unavailable or request target not approved' });
    }
  }
});

test('generator emits only explicit allowlisted models and secret references', async () => fixture(async (dir) => {
  const output = join(dir, 'pilot');
  await generate({ output, models: ['anthropic/claude-sonnet-5', 'chatgpt-gpt-6-luna', 'qwen36-27b'],
    model: 'anthropic/claude-sonnet-5', smallModel: 'qwen36-27b' });
  const config = JSON.parse(await readFile(join(output, 'opencode.json'), 'utf8'));
  assert.deepEqual(config.enabled_providers, ['litellm-pilot']);
  assert.equal(config.model, 'litellm-pilot/anthropic/claude-sonnet-5');
  assert.equal(config.small_model, 'litellm-pilot/qwen36-27b');
  assert.equal(config.share, 'disabled');
  assert.equal(config.autoshare, false);
  assert.equal(config.autoupdate, false);
  assert.equal(config.formatter, false);
  assert.equal(config.lsp, false);
  assert.deepEqual(config.mcp, {});
  assert.deepEqual(config.instructions, []);
  assert.deepEqual(config.permission, {
    '*': 'deny', read: 'allow', edit: { '*': 'allow', 'opencode.json': 'deny',
      'opencode.jsonc': 'deny', 'package.json': 'deny', '.opencode/**': 'deny',
      '.env*': 'deny', 'AGENTS.md': 'deny', 'CLAUDE.md': 'deny', 'CONTEXT.md': 'deny' }, glob: 'allow', grep: 'allow', list: 'allow',
    bash: 'deny', task: 'deny', external_directory: 'deny',
    webfetch: 'deny', websearch: 'deny', codesearch: 'deny', skill: 'deny', lsp: 'deny',
  });
  assert.deepEqual(Object.keys(config.provider['litellm-pilot'].models), ['anthropic/claude-sonnet-5', 'chatgpt-gpt-6-luna', 'qwen36-27b']);
  assert.equal(config.provider['litellm-pilot'].options.headers['x-litellm-api-key'], 'Bearer {env:LITELLM_PILOT_KEY}');
  assert.ok(!JSON.stringify(config).includes(key));
  const plugin = await readFile(join(output, '.opencode/plugins/claude-bearer.js'), 'utf8');
  assert.ok(plugin.includes('chat.headers'));
  assert.ok(plugin.includes('chat.params'));
  assert.ok(!plugin.includes('export function createPilotHook'));
  assert.ok(plugin.includes('export const ClaudeBearer'));
  await assert.rejects(generate({ output, models: ['qwen36-27b'], ...selection }), /new directory/);
  const loaded = await import(pathToFileURL(join(output, '.opencode/plugins/claude-bearer.js')).href);
  assert.equal(typeof (await loaded.ClaudeBearer())['chat.headers'], 'function');
}));

test('generator rejects metered and router aliases, duplicates and malformed endpoint', () => {
  for (const models of [[], ['gpt-4'], ['project-router'], ['anthropic/*'], ['qwen36-27b', 'qwen36-27b']]) {
    assert.throws(() => buildConfig({ models, ...selection }));
  }
  for (const endpoint of ['http://ai-api.homelab0.org/v1', 'https://evil.example/v1', `${gateway}/other`, 'bad']) {
    assert.throws(() => buildConfig({ endpoint, models: ['qwen36-27b'], ...selection }));
  }
  for (const choices of [
    { model: 'project-router', smallModel: 'qwen36-27b' },
    { model: 'qwen36-27b', smallModel: 'gpt-4' },
    { model: undefined, smallModel: 'qwen36-27b' },
    { model: 'qwen36-27b', smallModel: undefined },
  ]) {
    assert.throws(() => buildConfig({ models: ['qwen36-27b'], ...choices }), /selected from explicit allowed aliases/);
  }
});

test('generator refuses existing and symlinked targets without partial files', async () => fixture(async (dir) => {
  const existing = join(dir, 'existing');
  await mkdir(existing);
  await writeFile(join(existing, 'opencode.json'), '{}');
  await assert.rejects(generate({ output: existing, models: ['qwen36-27b'], ...selection }), /new directory/);
  assert.deepEqual(await readdir(existing), ['opencode.json']);
  const alias = join(dir, 'alias');
  await symlink(existing, alias);
  await assert.rejects(generate({ output: alias, models: ['qwen36-27b'], ...selection }), /symlinks/);
  assert.deepEqual(await readdir(existing), ['opencode.json']);
}));

test('generator refuses Git worktrees and synthetic global locations', async () => fixture(async (dir) => {
  const repo = join(dir, 'repo');
  await mkdir(join(repo, '.git'), { recursive: true });
  await writeFile(join(repo, '.git', 'HEAD'), 'ref: refs/heads/main\n');
  await assert.rejects(generate({ output: join(repo, 'pilot'), models: ['qwen36-27b'], ...selection }), /Git worktree/);
  const prior = process.env.OPENCODE_CONFIG_DIR;
  process.env.OPENCODE_CONFIG_DIR = join(dir, 'global');
  try { await assert.rejects(generate({ output: join(dir, 'global'), models: ['qwen36-27b'], ...selection }), /Global OpenCode/); }
  finally { if (prior === undefined) delete process.env.OPENCODE_CONFIG_DIR; else process.env.OPENCODE_CONFIG_DIR = prior; }
  assert.deepEqual((await readdir(dir)).sort(), ['repo']);
}));

test('generator refuses inherited project markers and canonical global aliases', async () => fixture(async (dir) => {
  const inherited = join(dir, 'inherited');
  await mkdir(inherited);
  await writeFile(join(inherited, 'AGENTS.md'), 'synthetic');
  await assert.rejects(generate({ output: join(inherited, 'pilot'), models: ['qwen36-27b'], ...selection }), /project marker/);
  const actual = join(dir, 'actual');
  const alias = join(dir, 'alias');
  await mkdir(actual);
  await symlink(actual, alias);
  const prior = process.env.OPENCODE_CONFIG_DIR;
  process.env.OPENCODE_CONFIG_DIR = alias;
  try {
    await assert.rejects(generate({ output: join(actual, 'pilot'), models: ['qwen36-27b'], ...selection }), /Global OpenCode/);
  } finally { if (prior === undefined) delete process.env.OPENCODE_CONFIG_DIR; else process.env.OPENCODE_CONFIG_DIR = prior; }
  assert.deepEqual(await readdir(actual), []);
}));

test('generator refuses each inherited instruction filename', async () => fixture(async (dir) => {
  for (const name of ['AGENTS.md', 'CLAUDE.md', 'CONTEXT.md']) {
    const inherited = join(dir, name.replace('.md', '').toLowerCase());
    await mkdir(inherited);
    await writeFile(join(inherited, name), 'synthetic instruction marker');
    await assert.rejects(
      generate({ output: join(inherited, 'pilot'), models: ['qwen36-27b'], ...selection }),
      /project marker/,
    );
    assert.deepEqual((await readdir(inherited)), [name]);
  }
}));

test('generator refuses worktree gitfile and symlink ancestor', async () => fixture(async (dir) => {
  const repo = join(dir, 'repo');
  await mkdir(repo);
  await writeFile(join(repo, '.git'), 'gitdir: /synthetic/gitdir');
  await assert.rejects(generate({ output: join(repo, 'pilot'), models: ['qwen36-27b'], ...selection }), /Git worktree/);
  const alias = join(dir, 'repo-alias');
  await symlink(repo, alias);
  await assert.rejects(generate({ output: join(alias, 'pilot'), models: ['qwen36-27b'], ...selection }), /symlinks/);
}));

test('malformed native JSON is redacted', async () => fixture(async (dir) => {
  const previous = process.env.CLAUDE_CONFIG_DIR;
  process.env.CLAUDE_CONFIG_DIR = dir;
  try {
    await writeFile(join(dir, '.credentials.json'), 'SYNTHETIC_SECRET_INVALID_JSON');
    await assert.rejects(createPilotHook({ getGatewayKey: () => key })(context(), { headers: scoped() }),
      { message: 'Pilot Claude credential unavailable or request target not approved' });
  } finally { if (previous === undefined) delete process.env.CLAUDE_CONFIG_DIR; else process.env.CLAUDE_CONFIG_DIR = previous; }
}));

test('CLI parser rejects missing flag values', () => {
  assert.throws(() => parseArgs(['--output', '--models', 'qwen36-27b']), /Missing value for --output/);
  assert.deepEqual(parseArgs(['--output', 'pilot', '--models', 'qwen36-27b', '--model', 'qwen36-27b', '--small-model', 'qwen36-27b']),
    { output: 'pilot', endpoint: undefined, models: ['qwen36-27b'], ...selection });
});
