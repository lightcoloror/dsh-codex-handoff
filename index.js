import z from '@deepseek-ai/schemastery';
export {name,inject,apply} from './plugin.mjs';

export const Config=z.object({
  dataDir:z.string().default('').description('Host-owned state/client directory, separate from DeepSeek workspaces.'),
  workspacePaths:z.array(z.string()).default([]).description('Exact existing, registered DeepSeek workspace paths. Empty denies all.'),
  prefix:z.string().default('/codex-bridge'),
  allowWorkspaceCreate:z.boolean().default(false),
  enableInboxWakeups:z.boolean().default(false).description('Optional legacy Codex queue notices; not cold-chat wakeup proof.'),
  codexHome:z.string().default(''),
  executable:z.string().default('').description('Optional Codex CLI path for explicitly enabled queue notices.'),
});
