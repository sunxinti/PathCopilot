# PathCopilot Agent Runtime（2A + 2B + 2C）

本目录是独立于成熟 PathCopilot 病理分析流程的 Agent 生命周期基础层。
当前阶段建立可验证的生命周期和持久化执行语义，并通过显式的 Web 集成层
接入成熟 PathCopilot Agent。导入 Runtime 包本身仍不会启动 Flask、Worker
或写入数据库；生命周期只由应用启动/退出代码显式控制。

## 已实现

- 显式 Run / Task 状态机与非法转换校验
- 主任务、子任务层级和实时派生的 Workflow 计数
- 分层取消 Token，以及 Run 级“请求取消”和“实际停止”分离
- 固定 Worker 数和有界等待队列
- SQLite WAL 事件存储、原子迁移、单 Run 连续事件序号
- 类型化、版本化、可按序读取且不可更新/删除的审计事件
- Run / Task 投影与取消事件的事务一致性
- 幂等 Run 创建和重复活动 Run 防护
- Task / Subagent spawn 命令幂等；相同命令跨线程、跨进程只创建一次
- 每个 Run 持久化且不可变的 Task 总量、最大深度策略
- SQLite 事务内原子校验父子关系、取消状态、总量、深度并创建 Task
- 单个非根 Task 的持久化子树取消，且不影响兄弟分支和所属 Run
- Spawn / Cancel 竞态下不会逸出仍可运行的子 Task
- 取消原因遵循 first-writer-wins，并可在进程重启后恢复到 Token
- Artifact 分 Run 隔离、摘要校验、写入/恢复互斥和路径安全
- 终态不可复活、活动子任务阻止父任务终止
- 独立的持久化 Task Attempt、短期 Worker Lease 与单调 Fencing Token
- SQLite 原子认领；每个固定 Worker Slot 同时最多持有一个执行权
- claim request 幂等，避免数据库提交成功但响应丢失后重复认领
- 固定槽 DurableTaskExecutor，只在槽位空闲时认领，不在内存队列预占 Lease
- 自动 heartbeat、合作式持久化取消、yield/resume 与结构化重试策略
- watchdog 回收失联 Worker、过期 Lease、取消超时和旧版孤儿 RUNNING Task
- 父 Attempt 等待子任务后可在进程重启时收敛，并沿用同一逻辑 Attempt
- `RunRegistry.create_task()` / `spawn_task()` 默认创建 Attempt-managed Task；
  旧 2A 生命周期仅由显式 `create_legacy_task()` / `spawn_legacy_task()` 兼容入口保留
- 004/005 数据库中已有 Attempt、Lease、Fence 或重试痕迹的 Task 在 006 迁移时
  自动回填为 Attempt-managed；既有 migration 校验和保持不可变
- 非重试型父 Attempt 即使需要等待活动子任务，也会在子任务收敛后终止为失败，
  不会因剩余次数而被错误重试
- Attempt-managed Task 禁止旧 transition API 绕过 Fencing 提交结果
- Artifact 与 Attempt/Lease/Fence 绑定，过期 Worker 不能发布迟到结果
- 生产环境使用 Store 内部可信时钟；外部 `now` 仅在显式测试模式允许
- `off` / `shadow` / `managed` 三种可逆 Web 接入模式
- Legacy Adapter 在内存中绑定 Socket.IO、WSI 调用参数和执行上下文，
  不把密钥、路径、诊断正文或图像序列化进 Runtime 数据库
- Managed 模式先登记 Web Run，再激活根 Task，避免 Worker 在前端状态建立前抢跑
- 进程重启后缺失本地调用绑定的 Task 会进入 `WAITING/RESOURCE`，重新绑定后恢复，
  不会被误判为临床分析失败
- Runtime Event Bridge 在转发成熟 Socket.IO 事件前持久化去敏活动摘要，
  并用 Attempt/bridge 身份、Fencing 和终态门控制迟到事件
- owner-scoped Run 状态、用户可见事件分页回放和单调游标
- 前端会话内保存 Run/cursor，请求重试复用幂等 ID；断线后按 `has_more`
  补齐活动，停止按钮等待真实 Runtime 终态后才允许新任务
- Runtime 取消会同步到成熟 `stop_agent`，且“已请求取消”和“已停止”保持分离

## 2C Web 接入边界

- `webapp/app.py` 只通过 `runtime_integration.py` 使用 Runtime 服务边界
- `legacy_runtime_adapter.py` 调用原有 `agent_loop.run_agent()`，不会复制或改写
  红框、蓝框、绿框病理下钻算法
- `runtime_event_bridge.py` 复用现有 Socket.IO UI 合约；Runtime 仅拥有生命周期、
  Attempt 执行权、审计摘要和取消权
- 默认 `PATHCOPILOT_RUNTIME_MODE=shadow`：成熟 Agent 线程仍执行，Runtime 镜像并校验
- `PATHCOPILOT_RUNTIME_MODE=managed`：DurableTaskExecutor 持有根 Task Attempt，
  并通过 Legacy Adapter 执行同一成熟 Agent
- `PATHCOPILOT_RUNTIME_MODE=off`：完全回退到原有执行路径

本版本中，PathCopilot CLI 和 MCP Server/Client 尚未接入这一套持久化 Runtime。

因此，2C 改变的是执行所有权、恢复、审计和停止语义，不改变当前成熟 Web
版本的界面、临床提示词、ROI 选择规则或诊断结果。

## 2B 的真实边界

2B 已完成 Attempt lease、heartbeat、重试、崩溃恢复、取消超时 watchdog 和
独立执行器，但仍有以下明确边界：

- Python/native 阻塞代码只能合作式取消；不能宣称任意线程可被安全强杀
- Worker 失联后由 Lease 到期和 Fencing 保证迟到结果无效，而不是回滚外部副作用
- SQLite 与文件系统不是跨资源 ACID 事务；Artifact 通过 staging、摘要和恢复收敛
- Run / Task 投影与事件在同一 SQLite 事务中维护，但尚未承诺只靠事件从零重建全部对象

## 后续接入批次

以下内容不在本版本范围内：

- CLI 与 MCP 的统一身份、capability 和协议边界
- 跨进程实时事件通知（当前断线回放通过 owner-scoped REST cursor）
- 可恢复的完整 UI Artifact 投影（当前仅持久化去敏活动摘要）
- 多用户 API Key 的请求级隔离和生产密钥托管

这些后续工作同样不能在 Runtime 包导入时隐式发生。

## 测试

Runtime 回归测试未包含在本开源仓库中。
