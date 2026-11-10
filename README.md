# DSH Codex Handoff

DeepSeek Harness 插件：Codex 派单，DeepSeek 在明确登记的专属工作区执行，结果回到原工单收件箱，由 Codex 独立验收。无需 computer-use。

插件随 Harness profile 加载，复用 Harness 的 HTTP 服务，不新建常驻服务。每次加载自动发布实际端口及实例信息；Windows 客户端在发送受限令牌前，核对**已建立 TCP 连接**的操作系统归属、Harness 可执行文件和进程代次。端口变化不再要求手改客户端地址。

## 安装

需要 DeepSeek Harness `0.2.1-rc.5` 或兼容的 `0.2.x`、Node 22+、Python 3.11+。Windows 是当前自动发现验证平台；其他平台尚未实现同等连接身份检查。DeepSeek API 与峰谷门控由使用者自己的 Harness 管理，本插件不提供、不读取 API Key，也不改请求门控。便携版默认沿用模型；显式选模有下述上游限制。

在 Plugins 的添加插件入口粘贴仓库链接，或使用 CLI：

```powershell
dsh plugin --profile desktop add github:lightcoloror/dsh-codex-handoff#main
```

包包含可直接运行的源码，没有安装脚本或构建步骤。`desktop` 可以换成你实际使用的 profile。安装到未使用的 profile 不会让当前应用加载插件。

当前源码包名为 `@lightcoloror/dsh-codex-handoff`。以上是 GitHub 安装方式，不表示已发布到 npm；旧的 `v0.1.1` 标签仍使用原包名。

也可在 Harness 的 **Custom plugin indexes / 自定义插件索引** 中添加 [本插件索引源](https://raw.githubusercontent.com/lightcoloror/dsh-codex-handoff/main/market-index.json)，再从 Discover 安装。此索引由本项目维护，不代表第三方市场收录。

## 首次配置

1. 在 Harness 中登记 DeepSeek 的专属 workspace。
2. 在本插件设置中，将 `workspacePaths` 设为这个目录的完整路径，将 `dataDir` 设为**不同的宿主目录**。例如共同父目录内的 `C:/AgentWork/bridge` 和 `C:/AgentWork/deepseek`。`workspacePaths` 初始为空，授权范围也为空。
3. 运行一次配对。`--home` 须与 `dataDir` 一致，`--harness-executable` 是正在运行的 Harness 程序完整路径：

```powershell
dsh plugin --profile desktop exec dsh-codex-handoff-setup --home C:/AgentWork/bridge --workspace C:/AgentWork/deepseek --harness-executable 'C:/Apps/DeepSeek Harness/DeepSeek Harness.exe'
```

也可在下载的插件源码目录运行 `python -B setup.py` 并使用相同参数。配对会核实当前连接与已配置工作区，生成仅覆盖该 workspace、不能创建 workspace 的桥接凭据，并将客户端复制到宿主目录。不会启动模型、创建聊天或修改沙箱。既有不一致的配对及代码会保留并报错，不静默覆盖。

4. 使用配对命令打印的 CLI 路径：

```powershell
python -X utf8 -B C:/AgentWork/bridge/cli.py doctor --service
python -X utf8 -B C:/AgentWork/bridge/cli.py prepare --request C:/AgentWork/task.json
python -X utf8 -B C:/AgentWork/bridge/cli.py run-foreground JOB_ID --authorization-ref C:/AgentWork/assignment.md --timeout-seconds 900
```

`CODEX_THREAD_ID` 应来自真实来源聊天，不可冒充其他聊天。`assignment.md` 记录真实用户交办，仅作证据。独立验收使用 `inbox`、核对身份后的 `ack`、`stage-review` / `stage-next` 或 `feedback`，最后 `close`。参考 `task.example.json` 和 `skills/deepseek-handoff/SKILL.md`。相关串行步骤可在同一工单的 `stages` 复用会话；用量和返工保留实际观测，费用缺失为 `unknown`。

## 重启与权限

- 插件与 Harness 同生命周期；Harness 尚未启动或未监听时，客户端报告未就绪，不猜端口、不扫端口、不改全局代理。
- 旧端口仅保留为配置兼容字段。启用发现后，以 `.state/endpoint.json` 为准，不再需要 `DSH_CODEX_BRIDGE_URL`；过时的显式覆盖会报冲突。
- 每次请求读取实例记录，并在每个新连接上核对 OS 归属。响应丢失的修改请求标记 `uncertain`，保留 job/session/request ID，先对账，不自动重投。
- 多实例请使用不同 `dataDir` 和路由前缀。同一宿主目录被另一存活进程占用时拒绝接管。
- DeepSeek 只在自己的已授权工作区操作。宿主 state/令牌、父目录和兄弟项目不属于模型工作区。任务子目录不是额外 OS 沙箱。
- 终端、代码修改、设备操作分别授权；需要跑测试的工单明确 `requires_shell:true`、命令、cwd、输入和输出范围，缓存放任务 `.tmp`。允许终端不等于允许改代码或操作设备。
- 默认 `enableInboxWakeups:false`。收件箱持久化和当前聊天内程序等待可用；可选旧原生队列通知不代表未加载聊天、应用退出或整机重启后能自动唤醒。不得为单工单追踪擅建自动化。
- 本地身份检查信任宿主状态目录和指定 Harness 程序，不能抵抗同一账号下已能修改这些文件的恶意程序。桥接令牌不是 DeepSeek API Key。
- 更新若改变模块路径或客户端源码，需要审阅后重新配对，保留旧状态。不要删除旧工单或换 ID 绕过冲突。
- 默认 `routing:"inherit"`。已在部分 Harness 版本观察到 `SessionController.selectModel` 在选会话模型后写回全局默认；因此显式 provider/model/reasoningEffort 不能承诺只改本工单。要求保留全局默认时沿用当前模型，先核对版本行为，不自动改配置恢复默认。v0.1.0 的这一已知限制也列在 Release 说明中。

## 验证范围

当前已验证：发布不同实际端口、启动未就绪/卸载、Windows 真实连接归属、错误进程/模块/工作区拒绝、实例变化、丢失提交响应不重发，以及既有 Harness 中无进程重启的插件加载与只读健康校验。**尚未执行整机重启验收**，也未为发布启动额外付费模型任务。自定义市场索引可供导入，不代表被第三方公共市场收录。

```powershell
python -X utf8 -B -m unittest test_endpoint_transport test_setup
node plugin.test.mjs
npm pack --ignore-scripts
```

## 协议与来源

- `vendor/collab`：ESROAMER/codex-dsh-collab，固定提交见 `vendor-manifest.json`，MIT。
- `vendor/dax`：Dax233/dsh-codex-bridge，固定提交见 `vendor-manifest.json`，MIT。
- 许可证原文和固定文件摘要随包保留。工作区约束与原生审批仍由 Harness 实际运行策略决定。
- [Harness 插件打包安装](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md)
- [Harness 自定义市场索引](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/plugin-market.md)

## English

A workspace-scoped Codex ↔ DeepSeek Harness handoff plugin. It loads with Harness, publishes its current loopback endpoint, and authenticates the connected peer using Windows TCP ownership before sending a scoped credential. Configure explicit workspaces, pair once, and use the printed Python CLI. No API keys, automatic approvals, GUI automation, background model controllers, or automatic task replay are included. See the commands above. A GitHub installation link and a custom format-1 index are supported; listing in a third-party public market is separate.

## 同类项目与代码来源

参阅[同类插件对比与固定代码来源](https://github.com/lightcoloror/dsh-codex-handoff/blob/main/COMPARISON.zh.md)。本项目直接复用 ESROAMER 与 Dax233 的固定 MIT 源码；工作区受限凭据和双向协作均不是本项目首创。

## Native followup safety fix (0.1.1)

The optional legacy two-hour followup now uses hourly recurrence stepping,
avoiding a filtered-day loop in the affected desktop recurrence library. Cached
plans are re-encoded without changing due time or backoff. See
[FOLLOWUP-RRULE-SAFETY.md](FOLLOWUP-RRULE-SAFETY.md) for the trigger, tests and
limits. Foreground supervision needs no automation. Existing paired clients are
not upgraded by a repository push; use an explicitly reviewed client update.
