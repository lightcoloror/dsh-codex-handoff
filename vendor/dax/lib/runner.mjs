/**
 * Headless Codex turns through the Harness subprocess seam.
 *
 * One `codex exec` (or `codex exec resume`) process per run. Output is
 * collected, not piped, so the plugin can read it without consuming the stream;
 * runs are tracked by id and can be polled or awaited.
 */
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import { parseExecEvents } from './rollout.mjs';
import { clip, singleLine } from './util.mjs';

/** Environment names a Codex child needs; credentials are intentionally absent. */
const ENV_KEYS = [
  'HOME',
  'USER',
  'LOGNAME',
  'SHELL',
  'SystemRoot',
  'ComSpec',
  'PATH',
  'PATHEXT',
  'TEMP',
  'TMP',
  'TMPDIR',
  'USERPROFILE',
  'HOMEDRIVE',
  'HOMEPATH',
  'APPDATA',
  'LOCALAPPDATA',
  'PROGRAMDATA',
  'PROGRAMFILES',
  'PROGRAMFILES(X86)',
  'WINDIR',
  'XDG_CONFIG_HOME',
  'XDG_CACHE_HOME',
  'XDG_DATA_HOME',
  'XDG_STATE_HOME',
  'NUMBER_OF_PROCESSORS',
  'PROCESSOR_ARCHITECTURE',
  'OS',
  'LANG',
  'LC_ALL',
];

export function createRuns({ ctx, config, home, resolveExecutable }) {
  const runs = new Map();
  let disposed = false;

  const buildEnv = () => {
    const env = {};
    for (const key of ENV_KEYS) {
      const value = process.env[key];
      if (value !== undefined) env[key] = value;
    }
    if (home) env.CODEX_HOME = home;
    env.NO_COLOR = '1';
    env.TERM = 'dumb';
    for (const [key, value] of Object.entries(config.env ?? {})) env[key] = value;
    return env;
  };

  const resolveWorkdir = (cwd) => {
    for (const candidate of [cwd, config.defaultCwd, process.cwd()]) {
      if (typeof candidate !== 'string' || candidate.trim() === '') continue;
      try {
        if (fs.statSync(candidate).isDirectory()) return candidate;
      } catch {
        /* try next */
      }
    }
    return process.cwd();
  };

  const readCollected = (run) => {
    const stdout = (() => {
      try {
        return run.handle?.collected?.stdout?.readFrom(0) ?? undefined;
      } catch {
        return undefined;
      }
    })();
    const stderr = (() => {
      try {
        return run.handle?.collected?.stderr?.readFrom(0) ?? undefined;
      } catch {
        return undefined;
      }
    })();
    return {
      stdoutText: stdout?.text ?? '',
      stdoutLossy: stdout?.lossy === true,
      stdoutSpillPath: stdout?.spillPath,
      stderrText: stderr?.text ?? '',
    };
  };

  const finalize = (run, outcome) => {
    if (run.status !== 'running') return;
    run.endedAt = Date.now();
    run.exitCode = outcome?.exitCode ?? null;
    run.signal = outcome?.signal ?? null;
    const output = readCollected(run);
    run.stdout = output.stdoutText;
    run.stderr = output.stderrText;
    run.stdoutLossy = output.stdoutLossy;
    run.stdoutSpillPath = output.stdoutSpillPath;
    const parsed = parseExecEvents(run.stdout);
    if (parsed.threadId && !run.threadId) run.threadId = parsed.threadId;
    run.finalMessage = parsed.finalMessage ?? '';
    run.usage = parsed.usage;
    run.error = parsed.error;
    const exitCode = run.exitCode ?? 1;
    run.status = exitCode === 0 && parsed.error === undefined ? 'completed' : 'failed';
    if (run.finalMessage === '' && run.status === 'failed') {
      run.finalMessage = clip(singleLine(run.stderr, 1_200) || 'codex exited without a message', 1_200);
    }
  };

  const buildArgv = ({ executable, kind, threadId, cwd, model, approvalPolicy, sandboxMode }) => {
    const common = [];
    if (model) common.push('-m', model);
    if (approvalPolicy) common.push('-c', `approval_policy=${approvalPolicy}`);
    if (sandboxMode) common.push('-c', `sandbox_mode=${sandboxMode}`);
    if (kind === 'resume') return [executable, 'exec', 'resume', '--json', '--skip-git-repo-check', ...common, threadId, '-'];
    return [executable, 'exec', '--json', '--skip-git-repo-check', '-C', resolveWorkdir(cwd), ...common, '-'];
  };

  /**
   * Spawn with the current executable resolution. The Codex desktop app replaces
   * its bin directory on update, so a path cached at plugin load can go stale;
   * resolve on every spawn and retry once when the spawn reports ENOENT.
   */
  const spawnResolved = (buildSpec) => {
    let executable = resolveExecutable();
    if (!executable) {
      throw new Error('codex executable not found: set config.executable, CODEX_BIN, or install the Codex desktop app');
    }
    try {
      return ctx.subprocess.spawn(buildSpec(executable));
    } catch (error) {
      const message = String(error?.message ?? error);
      if (!/ENOENT|no such file/iu.test(message)) throw error;
      const retryTarget = resolveExecutable();
      if (!retryTarget || retryTarget === executable) throw error;
      return ctx.subprocess.spawn(buildSpec(retryTarget));
    }
  };

  const start = ({ kind, threadId, prompt, cwd, model, approvalPolicy, sandboxMode }) => {
    if (disposed) throw new Error('codex bridge is shutting down');
    const id = randomUUID();
    const run = {
      id,
      kind,
      threadId,
      startedAt: Date.now(),
      status: 'running',
      promptPreview: singleLine(prompt, 200),
      stdout: '',
      stderr: '',
      finalMessage: '',
      error: undefined,
      usage: undefined,
      handle: undefined,
      settled: undefined,
    };
    const buildSpec = (executable) => ({
      argv: buildArgv({
        executable,
        kind,
        threadId,
        cwd,
        model,
        approvalPolicy: approvalPolicy ?? config.approvalPolicy,
        sandboxMode: sandboxMode ?? config.sandboxMode,
      }),
      cwd: resolveWorkdir(cwd),
      stdio: {
        stdin: { data: prompt },
        stdout: { maxBytes: config.maxOutputBytes, spill: { maxBytes: config.spillBytes } },
        stderr: { maxBytes: config.maxStderrBytes },
      },
      graceMs: config.disposeGraceMs,
      env: buildEnv(),
    });
    const handle = spawnResolved(buildSpec);
    run.handle = handle;
    run.settled = handle.done.then(
      (outcome) => {
        finalize(run, outcome);
      },
      (error) => {
        run.status = 'failed';
        run.endedAt = Date.now();
        run.error = String(error?.message ?? error);
        if (run.finalMessage === '') run.finalMessage = run.error;
      },
    );
    runs.set(id, run);
    return run;
  };

  /**
   * Run a short CLI command through the same seam/env/credential policy as turns.
   * Used for one-shot commands such as `codex queue` that are not tracked runs.
   */
  const invoke = async ({ args, cwd, stdin = '', timeoutMs = 30_000 }) => {
    if (disposed) throw new Error('codex bridge is shutting down');
    const budget = Math.min(Math.max(Number(timeoutMs) || 30_000, 1_000), config.maxWaitMs);
    const buildSpec = (executable) => ({
      argv: [executable, ...args],
      cwd: resolveWorkdir(cwd),
      stdio: {
        stdin: { data: stdin },
        stdout: { maxBytes: config.maxOutputBytes },
        stderr: { maxBytes: config.maxStderrBytes },
      },
      graceMs: config.disposeGraceMs,
      env: buildEnv(),
    });
    const handle = spawnResolved(buildSpec);
    let timer;
    const outcome = await Promise.race([
      handle.done,
      new Promise((resolve) => {
        timer = setTimeout(() => resolve({ timedOut: true }), budget);
        if (typeof timer?.unref === 'function') timer.unref();
      }),
    ]);
    if (timer !== undefined) clearTimeout(timer);
    if (outcome?.timedOut === true) {
      try {
        handle.terminate();
      } catch {
        /* ignore */
      }
    }
    const read = (stream) => {
      try {
        return stream?.readFrom(0);
      } catch {
        return undefined;
      }
    };
    const stdout = read(handle.collected?.stdout);
    const stderr = read(handle.collected?.stderr);
    return {
      timedOut: outcome?.timedOut === true,
      exitCode: outcome?.timedOut === true ? null : outcome?.exitCode ?? null,
      signal: outcome?.signal ?? null,
      stdoutText: stdout?.text ?? '',
      stderrText: stderr?.text ?? '',
      stdoutLossy: stdout?.lossy === true,
    };
  };

  const wait = async (run, waitMs) => {
    if (!run || run.status !== 'running' || waitMs <= 0) return;
    const budget = Math.min(waitMs, config.maxWaitMs);
    let timer;
    try {
      await Promise.race([
        run.settled,
        new Promise((resolve) => {
          timer = setTimeout(resolve, budget);
          if (typeof timer?.unref === 'function') timer.unref();
        }),
      ]);
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  };

  const snapshot = (run) => ({
    runId: run.id,
    kind: run.kind,
    threadId: run.threadId,
    status: run.status,
    startedAt: run.startedAt,
    endedAt: run.endedAt,
    elapsedMs: (run.endedAt ?? Date.now()) - run.startedAt,
    exitCode: run.exitCode,
    prompt: run.promptPreview,
    finalMessage: clip(run.finalMessage, 6_000),
    error: run.error,
    usage: run.usage,
    stdoutLossy: run.stdoutLossy === true,
    stderrTail: clip(run.stderr, 1_500),
    stderrSpillPath: undefined,
  });

  const list = () => [...runs.values()].sort((left, right) => right.startedAt - left.startedAt);

  const runningFor = (threadId) =>
    list().find((run) => run.threadId === threadId && run.status === 'running');

  const dispose = () => {
    disposed = true;
    for (const run of runs.values()) {
      if (run.status !== 'running') continue;
      try {
        run.handle?.terminate();
      } catch {
        /* ignore */
      }
    }
  };

  return {
    start,
    wait,
    invoke,
    list,
    snapshot,
    runningFor,
    dispose,
    get executable() {
      return resolveExecutable();
    },
    home,
  };
}
