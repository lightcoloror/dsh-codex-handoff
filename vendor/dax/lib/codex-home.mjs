/**
 * Read-only view of the local Codex installation: home directory, executable,
 * session index, rollout files, goal rows, writer locks, and automation files.
 *
 * Nothing here writes into the Codex home; the bridge only reads these files
 * and drives Codex through the subprocess seam.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const UUID_GLOBAL = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/giu;

/** Resolve the Codex home directory (config, CODEX_HOME, then ~/.codex). */
export function resolveCodexHome(configured) {
  const candidates = [configured, process.env.CODEX_HOME, path.join(os.homedir(), '.codex')];
  for (const candidate of candidates) {
    if (typeof candidate !== 'string' || candidate.trim() === '') continue;
    const resolved = path.resolve(candidate);
    try {
      if (fs.existsSync(resolved)) return resolved;
    } catch {
      /* unreadable candidate: try the next one */
    }
  }
  return undefined;
}

/** Platform-standard placements where a Codex desktop build ships its own CLI. */
function desktopBundledExecutables(executableName) {
  const found = [];
  if (process.platform === 'win32' && process.env.LOCALAPPDATA) {
    const binRoot = path.join(process.env.LOCALAPPDATA, 'OpenAI', 'Codex', 'bin');
    try {
      const versions = [];
      for (const entry of fs.readdirSync(binRoot, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        const candidate = path.join(binRoot, entry.name, executableName);
        try {
          if (fs.statSync(candidate).isFile()) versions.push({ candidate, mtimeMs: fs.statSync(candidate).mtimeMs });
        } catch {
          /* unreadable version directory */
        }
      }
      found.push(...versions.sort((left, right) => right.mtimeMs - left.mtimeMs).map((entry) => entry.candidate));
    } catch {
      /* no desktop install */
    }
  } else if (process.platform === 'darwin') {
    const home = os.homedir();
    for (const root of ['/Applications', path.join(home, 'Applications')]) {
      const contents = path.join(root, 'Codex.app', 'Contents');
      found.push(path.join(contents, 'MacOS', executableName), path.join(contents, 'Resources', executableName));
    }
  }
  return found;
}

/** Locate a runnable Codex CLI without assuming any machine-specific path. */
export function findCodexExecutable(configured, home) {
  const executableName = process.platform === 'win32' ? 'codex.exe' : 'codex';
  const isFile = (candidate) => {
    try {
      return fs.statSync(candidate).isFile();
    } catch {
      return false;
    }
  };
  for (const candidate of [configured, process.env.CODEX_BIN]) {
    if (typeof candidate === 'string' && candidate.trim() !== '' && isFile(candidate)) return path.resolve(candidate);
  }
  for (const candidate of desktopBundledExecutables(executableName)) {
    if (isFile(candidate)) return candidate;
  }
  for (const directory of (process.env.PATH ?? '').split(path.delimiter)) {
    if (!directory) continue;
    const candidate = path.join(directory, executableName);
    if (isFile(candidate)) return candidate;
  }
  const userHome = os.homedir();
  for (const candidate of [
    path.join(userHome, '.local', 'bin', executableName),
    path.join(userHome, '.npm-global', 'bin', executableName),
    `/opt/homebrew/bin/${executableName}`,
    `/usr/local/bin/${executableName}`,
    `/usr/bin/${executableName}`,
  ]) {
    if (isFile(candidate)) return candidate;
  }
  if (home) {
    for (const candidate of [path.join(home, '.sandbox-bin', executableName), path.join(home, 'bin', executableName)]) {
      if (isFile(candidate)) return candidate;
    }
  }
  return undefined;
}

/** Read `session_index.jsonl`: thread id to friendly name and update time. */
export function readSessionIndex(home) {
  const entries = new Map();
  if (!home) return entries;
  let text;
  try {
    text = fs.readFileSync(path.join(home, 'session_index.jsonl'), 'utf8');
  } catch {
    return entries;
  }
  for (const line of text.split('\n')) {
    if (line.trim() === '') continue;
    let row;
    try {
      row = JSON.parse(line);
    } catch {
      continue;
    }
    if (!row || typeof row.id !== 'string') continue;
    entries.set(row.id, {
      name: typeof row.thread_name === 'string' ? row.thread_name : '',
      updatedAt: typeof row.updated_at === 'string' ? row.updated_at : undefined,
    });
  }
  return entries;
}

/** Scan rollout files, grouping continuation suffixes under their thread id. */
export function scanRollouts(home) {
  const threads = new Map();
  if (!home) return threads;
  const root = path.join(home, 'sessions');
  const stack = [[root, 0]];
  while (stack.length > 0) {
    const [directory, depth] = stack.pop();
    if (depth > 5) continue;
    let entries;
    try {
      entries = fs.readdirSync(directory, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        stack.push([full, depth + 1]);
        continue;
      }
      if (!entry.name.startsWith('rollout-') || !entry.name.endsWith('.jsonl')) continue;
      const ids = entry.name.match(UUID_GLOBAL);
      if (!ids || ids.length === 0) continue;
      const threadId = ids[0];
      let stat;
      try {
        stat = fs.statSync(full);
      } catch {
        continue;
      }
      const previous = threads.get(threadId);
      const record = {
        file: full,
        sizeBytes: stat.size,
        mtimeMs: stat.mtimeMs,
        continuations: new Set(previous?.continuations ?? []),
      };
      if (ids[1]) record.continuations.add(ids[1]);
      if (!previous || stat.mtimeMs >= previous.mtimeMs) threads.set(threadId, record);
    }
  }
  return threads;
}

/** Read advisory writer locks (thread id to lock mtime). */
export function scanWriterLocks(home) {
  const locks = new Map();
  if (!home) return locks;
  const directory = path.join(home, 'thread-writer-locks');
  let entries;
  try {
    entries = fs.readdirSync(directory);
  } catch {
    return locks;
  }
  for (const name of entries) {
    if (!name.endsWith('.lock')) continue;
    const id = name.slice(0, -'.lock'.length);
    if (!/^[0-9a-f-]{36}$/iu.test(id)) continue;
    try {
      locks.set(id, fs.statSync(path.join(directory, name)).mtimeMs);
    } catch {
      /* ignore */
    }
  }
  return locks;
}

/** Read automation definitions (heartbeat schedules) as flat records. */
export function readAutomations(home) {
  const automations = [];
  if (!home) return automations;
  const root = path.join(home, 'automations');
  let entries;
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return automations;
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    let text;
    try {
      text = fs.readFileSync(path.join(root, entry.name, 'automation.toml'), 'utf8');
    } catch {
      continue;
    }
    const field = (key) => {
      const match = text.match(new RegExp(`^\\s*${key}\\s*=\\s*"([^"]*)"`, 'mu'));
      return match ? match[1] : undefined;
    };
    automations.push({
      id: field('id') ?? entry.name,
      kind: field('kind'),
      name: field('name'),
      status: field('status'),
      rrule: field('rrule'),
      targetThreadId: field('target_thread_id'),
    });
  }
  return automations;
}

/** Locate the goals database without assuming an exact schema-version filename. */
function goalsDatabaseCandidates(home) {
  const candidates = [];
  const primary = path.join(home, 'goals_1.sqlite');
  if (fs.existsSync(primary)) candidates.push(primary);
  try {
    for (const name of fs.readdirSync(home)) {
      if (!/^goals.*\.sqlite$/u.test(name)) continue;
      const candidate = path.join(home, name);
      if (!candidates.includes(candidate)) candidates.push(candidate);
    }
  } catch {
    /* home unreadable */
  }
  return candidates;
}

/**
 * Read official Codex goal rows from the local sqlite database.
 * Returns `{ available, rows, reason }`; read-only, WAL databases are opened
 * readonly so a concurrently running desktop app stays authoritative.
 */
export async function readGoals(home) {
  if (!home) return { available: false, rows: [], reason: 'codex home not found' };
  const files = goalsDatabaseCandidates(home);
  if (files.length === 0) return { available: false, rows: [], reason: 'no goals database found' };
  let DatabaseSync;
  try {
    ({ DatabaseSync } = await import('node:sqlite'));
  } catch {
    return { available: false, rows: [], reason: 'node:sqlite unavailable in this runtime' };
  }
  let lastReason = 'goals database unreadable';
  for (const file of files) {
    let database;
    try {
      database = new DatabaseSync(file, { readOnly: true });
    } catch (error) {
      lastReason = `cannot open ${path.basename(file)}: ${String(error?.message ?? error)}`;
      continue;
    }
    try {
      const rows = database
        .prepare('SELECT thread_id, goal_id, objective, status, tokens_used, updated_at_ms FROM thread_goals')
        .all();
      return {
        available: true,
        rows: rows.map((row) => ({
          threadId: String(row.thread_id),
          goalId: String(row.goal_id),
          objective: String(row.objective ?? ''),
          status: String(row.status ?? ''),
          tokensUsed: Number(row.tokens_used ?? 0),
          updatedAtMs: Number(row.updated_at_ms ?? 0),
        })),
      };
    } catch (error) {
      lastReason = `goal query failed on ${path.basename(file)}: ${String(error?.message ?? error)}`;
    } finally {
      try {
        database.close();
      } catch {
        /* ignore */
      }
    }
  }
  return { available: false, rows: [], reason: lastReason };
}
