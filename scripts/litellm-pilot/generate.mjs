#!/usr/bin/env node
// Generates an opt-in OpenCode project directory. No auth store is opened.
import { readFile, mkdir, writeFile, realpath, lstat, rename, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve, dirname, join, basename, parse } from 'node:path';
import { homedir } from 'node:os';

const allowed = new Set(['anthropic/claude-sonnet-5', 'chatgpt-gpt-6-luna', 'qwen36-27b']);
const gateway = 'https://ai-api.homelab0.org/v1';

async function canonicalPath(path) {
  let cursor = resolve(path);
  const suffix = [];
  while (true) {
    try { return join(await realpath(cursor), ...suffix.reverse()); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      suffix.push(basename(cursor));
      const parent = dirname(cursor);
      if (parent === cursor) throw error;
      cursor = parent;
    }
  }
}

export function buildConfig({ endpoint = gateway, models, model, smallModel }) {
  let url;
  try { url = new URL(endpoint); } catch { throw new Error('Invalid pilot gateway endpoint'); }
  if (url.href !== gateway) throw new Error('Pilot gateway endpoint must be the pinned HTTPS gateway');
  if (!Array.isArray(models) || models.length === 0 || new Set(models).size !== models.length || models.some((m) => !allowed.has(m))) {
    throw new Error('Pilot models must be unique explicit subscription/local aliases');
  }
  if (!models.includes(model) || !models.includes(smallModel)) {
    throw new Error('Pilot main and lightweight models must be selected from explicit allowed aliases');
  }
  return {
    $schema: 'https://opencode.ai/config.json',
    enabled_providers: ['litellm-pilot'],
    share: 'disabled',
    autoshare: false,
    autoupdate: false,
    formatter: false,
    lsp: false,
    mcp: {},
    instructions: [],
    model: `litellm-pilot/${model}`,
    small_model: `litellm-pilot/${smallModel}`,
    permission: {
      '*': 'deny',
      read: 'allow',
      edit: {
        '*': 'allow',
        'opencode.json': 'deny', 'opencode.jsonc': 'deny', 'package.json': 'deny',
        '.opencode/**': 'deny', '.env*': 'deny', 'AGENTS.md': 'deny', 'CLAUDE.md': 'deny',
        'CONTEXT.md': 'deny',
      },
      glob: 'allow',
      grep: 'allow',
      list: 'allow',
      bash: 'deny',
      task: 'deny',
      external_directory: 'deny',
      webfetch: 'deny', websearch: 'deny', codesearch: 'deny', skill: 'deny', lsp: 'deny',
    },
    provider: {
      'litellm-pilot': {
        npm: '@ai-sdk/openai-compatible',
        name: 'LiteLLM pilot',
        options: {
          baseURL: gateway,
          headers: { 'x-litellm-api-key': 'Bearer {env:LITELLM_PILOT_KEY}' },
        },
        models: Object.fromEntries(models.map((model) => [model, { name: model }])),
      },
    },
  };
}

export async function generate({ output, endpoint, models, model, smallModel }) {
  if (!output || !Array.isArray(models)) throw new Error('Output directory and explicit models required');
  const dir = resolve(output);
  const config = buildConfig({ endpoint, models, model, smallModel });
  // Require a new leaf. No symlink or existing project may receive a plugin.
  let cursor = dir;
  while (cursor !== parse(cursor).root) {
    try {
      const stat = await lstat(cursor);
      if (stat.isSymbolicLink()) throw new Error('Pilot output path cannot contain symlinks');
      if (cursor === dir) throw new Error('Pilot output must be a new directory');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    cursor = dirname(cursor);
  }
  const parent = await realpath(dirname(dir));
  const home = await realpath(homedir());
  const globalPaths = await Promise.all([home, join(home, '.config', 'opencode'), join(home, '.opencode'),
    join(home, '.claude'), join(home, '.local', 'share', 'opencode'),
    process.env.XDG_CONFIG_HOME && resolve(process.env.XDG_CONFIG_HOME, 'opencode'),
    process.env.OPENCODE_CONFIG_DIR && resolve(process.env.OPENCODE_CONFIG_DIR),
    process.env.OPENCODE_CONFIG && resolve(process.env.OPENCODE_CONFIG)].filter(Boolean).map(canonicalPath));
  const target = join(parent, basename(dir));
  if (globalPaths.some((path) => target === path || target.startsWith(`${path}/`) && path !== home)) {
    throw new Error('Global OpenCode configuration is not a pilot output');
  }
  // A pilot client must not inherit project configuration or instructions.
  cursor = parent;
  while (true) {
    for (const name of ['opencode.json', 'opencode.jsonc', '.opencode', 'AGENTS.md', 'CLAUDE.md', 'CONTEXT.md']) {
      try { await lstat(join(cursor, name)); throw new Error(`Pilot output inherits project marker ${name}`); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    try { const marker = await lstat(join(cursor, '.git'));
      if (marker.isFile()) throw new Error('Pilot output must be outside a Git worktree');
      await lstat(join(cursor, '.git', 'HEAD'));
      throw new Error('Pilot output must be outside a Git worktree');
    }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (cursor === parse(cursor).root) break;
    cursor = dirname(cursor);
  }
  const source = fileURLToPath(new URL('./claude-bearer.mjs', import.meta.url));
  const plugin = (await readFile(source, 'utf8')).replace('export function createPilotHook(', 'function createPilotHook(');
  if (plugin.includes('export function createPilotHook(')) throw new Error('Pilot plugin template failed');
  const temp = await import('node:fs/promises').then(({ mkdtemp }) => mkdtemp(join(parent, '.litellm-pilot-')));
  try {
    const pluginDir = join(temp, '.opencode', 'plugins');
    await mkdir(pluginDir, { recursive: true });
    await writeFile(join(temp, 'package.json'), '{"private":true,"type":"module"}\n', { flag: 'wx', mode: 0o600 });
    await writeFile(join(pluginDir, 'claude-bearer.js'), plugin, { flag: 'wx', mode: 0o600 });
    await writeFile(join(temp, 'opencode.json'), `${JSON.stringify(config, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
    await rename(temp, target);
  } catch (error) { await rm(temp, { recursive: true, force: true }); throw error; }
  return { config: join(target, 'opencode.json'), plugin: join(target, '.opencode/plugins/claude-bearer.js') };
}

export function parseArgs(args) {
  const get = (name) => { const i = args.indexOf(name); if (i < 0) return undefined;
    const value = args[i + 1];
    if (value === undefined || value.startsWith('--')) throw new Error(`Missing value for ${name}`);
    return value;
  };
  return { output: get('--output'), endpoint: get('--gateway'), models: get('--models')?.split(','),
    model: get('--model'), smallModel: get('--small-model') };
}

if (process.argv[1] && await realpath(process.argv[1]).catch(() => '') === fileURLToPath(import.meta.url)) {
  try {
    const result = await generate(parseArgs(process.argv.slice(2)));
    process.stdout.write(`Created ${result.config} and ${result.plugin}\n`);
  } catch (error) {
    process.stderr.write(`Pilot configuration refused: ${error.message}\n`);
    process.exitCode = 1;
  }
}
