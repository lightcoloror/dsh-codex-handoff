# 同类插件与开源来源

核对日期：2026-11-07（Asia/Shanghai）。执行：Codex，精确运行模型未独立核验。

本页比较项目维护者公开文档与本项目代码/实测记录；没有在本机逐一安装运行同类插件，不构成性能、安全性或成本排名。“文档未说明”不表示对方一定没有实现。

## 有哪些同类

| 项目 | 公开文档中的主要方向 | 与本项目的关系及差别 |
| --- | --- | --- |
| [ESROAMER/codex-dsh-collab](https://github.com/ESROAMER/codex-dsh-collab) | Codex 向 DSH workspace 派发，等待执行、读取轨迹并继续协作；支持受限凭据。README 明确由 Codex 发起交互，不负责唤醒空闲 Codex。 | 最接近，也是本项目直接复用的正向协议基础。本项目围绕绑定原来源的持久回传收件箱、ACK/独立验收、相关阶段复用和双端用量增加编排，并增加 Windows 已连接套接字身份校验及重启端点发现。不能把“工作区受限凭据”说成我们的独有功能。 |
| [Dax233/dsh-codex-bridge](https://github.com/Dax233/dsh-codex-bridge) | DSH 侧查看 Codex 线程、读有界轨迹、向指定线程发送或排队、查询执行与活性。 | 本项目直接复用其 runner、Codex 可执行文件发现等基础模块，但没有把通用线程浏览、任意目标发送、创建聊天或活性催办工具暴露给 DeepSeek。本项目回传绑定原 job/session/source/workspace/nonce。 |
| [heswy/dsh-plugin-codex-bridge](https://github.com/heswy/dsh-plugin-codex-bridge) | codex2dsh / dsh2codex 双向协作，工作区同步、持锁宿主代投、原会话修复，面向 Git 分支、推送及 PR 的交回检查。README 标注 Windows 未验证。 | 双向开发协作的思路相近。我们的验收围绕通用工单产物，不以 Git commit/push/PR 为回传前提，并针对 Windows 实测重启地址变化。没有复制该项目源码；Git 发布仍需单独授权。 |
| [knottttt/codex-dsh-bridge](https://github.com/knottttt/codex-dsh-bridge) | Codex 插件/MCP + Harness 设置面板；通过 HTTP RPC 创建/查询会话、排队或 steer、等待、取消和 fork；每次调用重新读取端点配置。 | 更偏 MCP 接入与设置 UI。重新读取可变端点的目标有重叠；我们的连接路径使用宿主发布的实际绑定地址，并在发受限令牌前检查 Windows 已建立连接的进程归属与代次。未在本机验证对方完整自动发现路径，不能断言其一定不能处理重启。未复制其源码。 |
| [本项目 dsh-codex-handoff](https://github.com/lightcoloror/dsh-codex-handoff) | 以 Codex 规划/验收、DeepSeek 专属 workspace 执行为目标的工单编排。 | 默认 workspace 授权为空；配对令牌只覆盖选定 workspace，不能创建 workspace。相关串行阶段复用会话；程序等待合并进度；用量/返工可观察，费用缺失为 unknown。当前自动连接身份验证只验证 Windows。 |

## 哪些代码直接复用了

不是仅“参考思路”：下列源文件以固定提交直接随包分发，原始 MIT 许可证与逐文件摘要保留在 `vendor/`、`NOTICE` 和 [vendor-manifest.json](vendor-manifest.json)。

1. **ESROAMER/codex-dsh-collab**，提交 [`abdc2368bdbc0b74cb58cd83e27254da8dc4784a`](https://github.com/ESROAMER/codex-dsh-collab/tree/abdc2368bdbc0b74cb58cd83e27254da8dc4784a)：正向 workspace/task HTTP 桥、Python 协议客户端和协议文档。
2. **Dax233/dsh-codex-bridge**，提交 [`4a0743ccdd95b55247a032c9718dc73167cf24c1`](https://github.com/Dax233/dsh-codex-bridge/tree/4a0743ccdd95b55247a032c9718dc73167cf24c1)：`runner.mjs`、`rollout.mjs`、`util.mjs`、`codex-home.mjs`。
3. **DeepSeek Harness 官方运行时与文档**：使用 `webServer`、workspace/session 等原生服务；依照 [bundle 打包安装](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md)及[市场索引格式](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/plugin-market.md)集成。Harness 自身不被打包复制进插件。

heswy 与 knottttt 在本次作为同类对照和架构参考，不是当前 vendor 代码来源。对照文档反映其核对时主分支；本项目的固定 vendor 提交与上游最新版本不等价。

## 保留的边界与证据

- DeepSeek 文件读写、缓存和临时文件留在授权 workspace/任务目录，父目录、兄弟目录、宿主状态与凭据不在工单授权内。保持 Harness 原生沙箱和审批；工单子目录不声称构成独立 OS 沙箱。
- 本桥与配套 Skill 不使用 computer-use、CUA 或 GUI，不启动额外控制模型。终端分析/测试、代码修改、设备操作分别授权。
- 回传收件与 ACK 不等于验收通过，也不保证空闲/未加载 Codex 会话自动唤醒；默认关闭旧队列通知。修改请求响应丢失时保留 ID，对账后恢复，不自动重发。
- 默认沿用模型。部分 Harness 版本显式选模会写回全局默认，不能承诺只影响本工单。现有请求峰谷门控保持；插件自身不重新定义提供商谷价。
- 已观察到真实服务进程换代、实际端口改变后，稳定 CLI 自动恢复并通过 Windows 连接归属与 workspace 范围检查。整机重启和冷聊天唤醒尚未由这些结果证明。

## 公共市场入口

Harness 官方的 Discover 使用配置的索引源，并不内置唯一公共目录。常用 [dsh-market](https://github.com/dsh-market/dsh-market) 使用 [Awesome DSH Plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) 社区目录。本项目按其单条 YAML 规范提交收录；维护者合并和市场同步前，不声称已能在该公共目录搜索到。

本项目的[自定义索引](https://raw.githubusercontent.com/lightcoloror/dsh-codex-handoff/main/market-index.json)与 GitHub 安装链接独立可用。社区 README 当前说明其 format-1 npm 索引只覆盖有 npm 版本的条目；GitHub-only 目录收录不自动等于进入该 npm 索引。
