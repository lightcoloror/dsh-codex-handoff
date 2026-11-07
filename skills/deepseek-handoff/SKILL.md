---
name: deepseek-handoff
description: Delegate authorized tasks through an installed DSH Codex Handoff bridge, with workspace-scoped execution, durable replies and independent review. No computer-use.
---

# DeepSeek handoff

Locate the host client installed by this plugin's setup command. Read its AGENT_DISCOVERY.md and run `python -X utf8 -B <host>/cli.py doctor --service`. Do not copy example paths or invent a source chat ID.

Use the actual CODEX_THREAD_ID. Prepare a small explicit brief with inputs, goal, acceptance, workspace and task-local outputs. Keep API keys, host state, parent/sibling folders and unrelated conversations out of DeepSeek's workspace. Preserve workspace-write and the existing Harness off-peak gate; submit ready work without bypassing that gate.

Choose tools for the task. Source review may use file tools. Terminal analysis/tests require `requires_shell:true`, specific operations, cwd and file scope. Keep caches in task .tmp. Terminal permission, source edits and device operations are separate authorizations.

Reuse one job/session's stages for related serial work. Use prepare, then run-foreground with a local record of the actual assignment. Program waiting merges progress; check inbox at work checkpoints. Validate job, source, session, workspace and nonce before ack. Review actual artifacts, tool exit codes and producer completion; then stage-review/stage-next or feedback, and finally close. An ACK is not acceptance. Preserve uncertain submissions and original request IDs; never blindly replay.

Record observed usage and rework; missing cost is unknown. Start no model controllers and create no single-job automations. Durable inbox, queue notices and cold-chat wakeup are different capabilities; claim only what current evidence proves.
