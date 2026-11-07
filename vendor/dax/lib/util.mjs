/**
 * Small shared helpers: defensive config reading and compact formatting.
 */

/** Clip a string to a maximum length, marking the truncation. */
export function clip(text, max) {
  const value = String(text ?? '');
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

/** Collapse whitespace and clip, for one-line summaries. */
export function singleLine(text, max) {
  return clip(String(text ?? '').replace(/\s+/gu, ' ').trim(), max);
}

/** Human-readable age from a millisecond duration. */
export function ageLabel(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 60_000) return `${Math.max(1, Math.round(ms / 1000))}s`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)}m`;
  if (ms < 86_400_000) return `${(ms / 3_600_000).toFixed(1)}h`;
  return `${(ms / 86_400_000).toFixed(1)}d`;
}

/** Compact ISO-ish local timestamp for a millisecond epoch. */
export function stamp(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '—';
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Compact token counts. */
export function tokensLabel(count) {
  if (!Number.isFinite(count) || count <= 0) return '—';
  if (count < 1_000) return String(Math.round(count));
  if (count < 1_000_000) return `${(count / 1_000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(2)}M`;
}

/**
 * Total token count from a Codex usage row.
 * Codex CLI versions differ: newer ones report input/output tokens without a
 * `total_tokens` field, so fall back to their sum instead of showing nothing.
 */
export function usageTotal(usage) {
  if (!usage || typeof usage !== 'object') return undefined;
  if (Number.isFinite(usage.total_tokens)) return usage.total_tokens;
  if (Number.isFinite(usage.totalTokens)) return usage.totalTokens;
  const input = Number.isFinite(usage.input_tokens) ? usage.input_tokens : 0;
  const output = Number.isFinite(usage.output_tokens) ? usage.output_tokens : 0;
  return input + output > 0 ? input + output : undefined;
}

/** Resolved plugin configuration with defensive defaults. */
export function readConfig(raw = {}) {
  const source = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const number = (value, fallback, min) =>
    typeof value === 'number' && Number.isFinite(value) && value >= min ? value : fallback;
  const string = (value, fallback) => (typeof value === 'string' && value.trim() !== '' ? value.trim() : fallback);
  const liveness = source.liveness && typeof source.liveness === 'object' ? source.liveness : {};
  const envSource = source.env && typeof source.env === 'object' && !Array.isArray(source.env) ? source.env : {};
  const env = {};
  for (const [key, value] of Object.entries(envSource)) {
    if (value === null || value === undefined) continue;
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') env[key] = String(value);
  }
  return {
    codexHome: string(source.codexHome, ''),
    executable: string(source.executable, ''),
    defaultCwd: string(source.defaultCwd, ''),
    approvalPolicy: string(source.approvalPolicy, 'never'),
    sandboxMode: string(source.sandboxMode, ''),
    maxOutputBytes: number(source.maxOutputBytes, 1_048_576, 4_096),
    maxStderrBytes: number(source.maxStderrBytes, 65_536, 1_024),
    spillBytes: number(source.spillBytes, 8_388_608, 65_536),
    defaultWaitMs: number(source.defaultWaitMs, 0, 0),
    maxWaitMs: number(source.maxWaitMs, 900_000, 1_000),
    disposeGraceMs: number(source.disposeGraceMs, 5_000, 0),
    nudgeWaitMs: number(source.nudgeWaitMs, 15_000, 0),
    queueTimeoutMs: number(source.queueTimeoutMs, 30_000, 1_000),
    queuePollMs: number(source.queuePollMs, 1_200, 100),
    env,
    allowNudge: source.allowNudge === true,
    liveness: {
      idleMs: number(liveness.idleMs, 1_800_000, 60_000),
      lockFreshMs: number(liveness.lockFreshMs, 120_000, 1_000),
      threads: Array.isArray(liveness.threads) ? liveness.threads.filter((item) => typeof item === 'string') : [],
    },
  };
}
