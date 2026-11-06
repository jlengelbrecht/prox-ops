// Project-local OpenCode plugin. Claude Code alone owns login, refresh and storage.
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

const GATEWAY = 'https://ai-api.homelab0.org/v1';
const PROVIDER = 'litellm-pilot';
const CLAUDE_MODELS = new Set(['anthropic/claude-sonnet-5']);
const PILOT_MODELS = new Set([...CLAUDE_MODELS, 'chatgpt-gpt-6-luna']);
const ERROR = 'Pilot Claude credential unavailable or request target not approved';

function fail() { throw new Error(ERROR); }
function headerNames(headers, name) {
  return Object.keys(headers).filter((key) => key.toLowerCase() === name);
}
function headerValue(headers, name) {
  const names = headerNames(headers, name);
  return names.length === 1 ? headers[names[0]] : undefined;
}
function unsafeHeaders(headers, allowGatewayKey = false) {
  if (headers === undefined) return false;
  if (!headers || typeof headers !== 'object' || Array.isArray(headers)) return true;
  return Object.keys(headers).some((name) => {
    const key = name.toLowerCase();
    return key === 'authorization' || key === 'x-api-key' || key === 'api-key' ||
      key === 'proxy-authorization' || (key === 'x-litellm-api-key' && !allowGatewayKey);
  });
}
function destinationOverride(value, seen = new Set()) {
  if (!value || typeof value !== 'object' || seen.has(value)) return false;
  seen.add(value);
  return Object.entries(value).some(([name, nested]) => {
    const key = name.toLowerCase().replace(/[-_]/g, '');
    if (['apibase', 'baseurl', 'baseuri', 'apiurl', 'endpoint', 'url', 'apikey', 'headers'].includes(key)) return true;
    return destinationOverride(nested, seen);
  });
}
function exactGateway(value) {
  try {
    const url = new URL(value);
    return url.href === `${GATEWAY}` && !url.username && !url.password && !url.search && !url.hash;
  } catch { return false; }
}
function credential(record, now) {
  const access = record?.accessToken;
  const expiry = record?.expiresAt;
  if (typeof access !== 'string' || !/^sk-ant-oat[0-9A-Za-z_-]+$/.test(access)) fail();
  if (!Number.isFinite(expiry) || expiry <= now + 30_000) fail();
  return access;
}

export function createPilotHook({
  readRecord = async () => {
    const path = join(process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude'), '.credentials.json');
    const { claudeAiOauth } = JSON.parse(await readFile(path, 'utf8'));
    return { accessToken: claudeAiOauth?.accessToken, expiresAt: claudeAiOauth?.expiresAt };
  },
  getGatewayKey = () => process.env.LITELLM_PILOT_KEY,
  now = () => Date.now(),
} = {}) {
  return async (input, output) => {
    const model = input?.model?.id;
    const provider = input?.provider?.info?.id;
    const modelProvider = input?.model?.providerID;
    if (provider !== PROVIDER && modelProvider !== PROVIDER && provider && modelProvider && provider === modelProvider) return;
    if (provider !== PROVIDER || modelProvider !== PROVIDER) fail();
    if (!PILOT_MODELS.has(model)) fail();
    const headers = output?.headers;
    if (!headers || typeof headers !== 'object' || Array.isArray(headers)) fail();
    const base = input?.provider?.options?.baseURL;
    const api = input?.model?.api?.url;
    if (!exactGateway(base) || (api !== undefined && !exactGateway(api))) fail();
    if ((input?.model?.api?.id !== undefined && input.model.api.id !== model) ||
        (input?.model?.api?.npm !== undefined && input.model.api.npm !== '@ai-sdk/openai-compatible')) fail();
    const options = input?.provider?.options;
    if (options?.apiKey !== undefined || unsafeHeaders(options?.headers, true) ||
        unsafeHeaders(input?.model?.headers) || destinationOverride(input?.model?.options) ||
        destinationOverride(input?.model?.variants) ||
        destinationOverride(Object.fromEntries(Object.entries(options ?? {}).filter(([name]) => !['baseURL', 'headers', 'apiKey'].includes(name))))) fail();
    const key = getGatewayKey();
    if (typeof key !== 'string' || !/^sk-[0-9A-Za-z_-]+$/.test(key) || key.startsWith('sk-ant-')) fail();
    if (options?.headers && headerNames(options.headers, 'x-litellm-api-key').length &&
        headerValue(options.headers, 'x-litellm-api-key') !== `Bearer ${key}`) fail();
    if (unsafeHeaders(headers, true) || headerValue(headers, 'x-litellm-api-key') !== `Bearer ${key}`) fail();
    if (!CLAUDE_MODELS.has(model)) return;
    let access;
    try { access = credential(await readRecord(), now()); } catch { fail(); }
    if (key === access) fail();
    // Mutate only after every check passes.
    headers.Authorization = `Bearer ${access}`;
  };
}

export function createParamsHook() {
  return async (input, output) => {
    const provider = input?.provider?.info?.id;
    const modelProvider = input?.model?.providerID;
    if (provider !== PROVIDER && modelProvider !== PROVIDER && provider && modelProvider && provider === modelProvider) return;
    if (provider !== PROVIDER || modelProvider !== PROVIDER || !PILOT_MODELS.has(input?.model?.id) ||
        !output?.options || typeof output.options !== 'object' || destinationOverride(output.options)) fail();
  };
}

export const ClaudeBearer = async () => ({ 'chat.headers': createPilotHook(), 'chat.params': createParamsHook() });
