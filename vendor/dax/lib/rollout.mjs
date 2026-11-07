/**
 * Bounded reads of Codex rollout transcripts under `.codex/sessions`. Rollout
 * files can reach hundreds of megabytes, so every read starts from the tail and
 * only the requested window is parsed.
 */
import fs from 'node:fs';
import { clip } from './util.mjs';

/** Read the last `maxBytes` of a file as complete lines (first line may be partial and is dropped). */
export function readTailLines(file, maxBytes) {
  const stat = fs.statSync(file);
  const size = stat.size;
  const start = Math.max(0, size - maxBytes);
  const length = size - start;
  const handle = fs.openSync(file, 'r');
  try {
    const buffer = Buffer.alloc(length);
    fs.readSync(handle, buffer, 0, length, start);
    const lines = buffer.toString('utf8').split('\n');
    if (start > 0 && lines.length > 0) lines.shift();
    return { lines: lines.filter((line) => line.trim() !== ''), truncated: start > 0, sizeBytes: size };
  } finally {
    fs.closeSync(handle);
  }
}

/** Read the session header line of a rollout file (the line can exceed 64KB). */
export function readSessionMeta(file) {
  let handle;
  try {
    handle = fs.openSync(file, 'r');
    const chunks = [];
    let length = 0;
    let newlineAt = -1;
    while (newlineAt === -1 && length < 4_194_304) {
      const chunk = Buffer.alloc(64 * 1024);
      const read = fs.readSync(handle, chunk, 0, chunk.length, length);
      if (read <= 0) break;
      chunks.push(chunk.subarray(0, read));
      length += read;
      newlineAt = Buffer.concat(chunks).indexOf('\n');
    }
    if (length === 0) return undefined;
    const text = Buffer.concat(chunks).toString('utf8');
    const first = text.split('\n')[0];
    const row = JSON.parse(first);
    const payload = row?.payload ?? {};
    return {
      sessionId: payload.session_id ?? payload.id,
      cwd: payload.cwd,
      cliVersion: payload.cli_version,
      modelProvider: payload.model_provider,
      originator: payload.originator,
      timestamp: row?.timestamp,
    };
  } catch {
    return undefined;
  } finally {
    if (handle !== undefined) {
      try {
        fs.closeSync(handle);
      } catch {
        /* ignore */
      }
    }
  }
}

/**
 * Reduce a rollout tail to a bounded, chronological list of conversation items.
 * Supported kinds: user, assistant, heartbeat, developer, tool, turn-end.
 */
export function summarizeEvents(lines, options = {}) {
  const limit = Number.isInteger(options.limit) && options.limit > 0 ? options.limit : 12;
  const maxChars = Number.isInteger(options.maxChars) && options.maxChars > 0 ? options.maxChars : 600;
  const kinds = Array.isArray(options.kinds) && options.kinds.length > 0 ? new Set(options.kinds) : undefined;
  const items = [];
  let meta;
  let lastEventAt;
  let usage;
  let modelProvider;
  for (const line of lines) {
    let row;
    try {
      row = JSON.parse(line);
    } catch {
      continue;
    }
    if (typeof row.timestamp === 'string') lastEventAt = row.timestamp;
    if (row.type === 'session_meta') {
      meta = row.payload ?? {};
      modelProvider = meta.model_provider;
      continue;
    }
    let item;
    if (row.type === 'response_item' && row.payload?.type === 'message') {
      const role = row.payload.role;
      const body = (row.payload.content ?? [])
        .map((part) => (typeof part?.text === 'string' ? part.text : ''))
        .join('\n')
        .trim();
      if (body === '') continue;
      if (role === 'user') item = { kind: 'user', text: body };
      else if (role === 'assistant') item = { kind: body.includes('<heartbeat>') ? 'heartbeat' : 'assistant', text: body };
      else if (role === 'developer') item = { kind: 'developer', text: body };
    } else if (row.type === 'response_item' && row.payload?.type === 'custom_tool_call') {
      item = { kind: 'tool', text: String(row.payload.name ?? 'tool') };
    } else if (row.type === 'event_msg') {
      const type = row.payload?.type;
      if (type === 'task_complete') item = { kind: 'turn-end', text: String(row.payload?.last_agent_message ?? '').trim() };
      else if (type === 'token_count') usage = row.payload?.info?.total_token_usage ?? usage;
    }
    if (!item) continue;
    if (kinds !== undefined && !kinds.has(item.kind)) continue;
    items.push({ at: row.timestamp, kind: item.kind, text: clip(item.text, maxChars) });
  }
  return {
    meta: meta
      ? { cwd: meta.cwd, cliVersion: meta.cli_version, modelProvider, sessionId: meta.session_id }
      : undefined,
    lastEventAt,
    usage,
    items: items.slice(-limit),
  };
}

/**
 * Find the turn a queued message started inside a rollout tail.
 * Scans rows newer than `afterTimestamp` for a user message containing `marker`,
 * then returns the first task_complete after that message.
 */
export function findQueuedTurn(lines, options = {}) {
  const after = typeof options.afterTimestamp === 'string' ? Date.parse(options.afterTimestamp) : Number.NaN;
  const marker = typeof options.marker === 'string' ? options.marker : '';
  if (marker === '') return undefined;
  let seen = false;
  for (const line of lines) {
    let row;
    try {
      row = JSON.parse(line);
    } catch {
      continue;
    }
    const at = typeof row.timestamp === 'string' ? Date.parse(row.timestamp) : Number.NaN;
    if (Number.isFinite(after) && Number.isFinite(at) && at <= after) continue;
    if (row.type === 'response_item' && row.payload?.type === 'message' && row.payload?.role === 'user') {
      if (!seen) {
        const body = (row.payload.content ?? [])
          .map((part) => (typeof part?.text === 'string' ? part.text : ''))
          .join('\n');
        if (body.includes(marker)) seen = true;
      }
      continue;
    }
    if (seen && row.type === 'event_msg' && row.payload?.type === 'task_complete') {
      return {
        at: row.timestamp,
        finalMessage: String(row.payload?.last_agent_message ?? '').trim(),
        error: row.payload?.error ? String(row.payload.error.message ?? row.payload.error) : undefined,
      };
    }
  }
  return undefined;
}

/** Extract the final assistant text and turn facts from `codex exec --json` output. */
export function parseExecEvents(text) {
  let threadId;
  let finalMessage;
  let usage;
  let error;
  for (const line of String(text ?? '').split('\n')) {
    if (line.trim() === '') continue;
    let row;
    try {
      row = JSON.parse(line);
    } catch {
      continue;
    }
    if (row.type === 'thread.started' && typeof row.thread_id === 'string') threadId = row.thread_id;
    else if (row.type === 'item.completed' && row.item?.type === 'agent_message') finalMessage = String(row.item.text ?? '');
    else if (row.type === 'turn.completed') usage = row.usage ?? undefined;
    else if (row.type === 'error' || row.type === 'turn.failed') {
      error = String(row.message ?? row.error?.message ?? 'codex reported an error');
    }
  }
  return { threadId, finalMessage, usage, error };
}
