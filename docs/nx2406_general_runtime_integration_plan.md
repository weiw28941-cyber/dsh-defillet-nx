# NX2406 开放式 Defeature Runtime 整合实施计划

> 状态：设计与实施计划，尚未实现。
>
> 本文说明如何结合两个开源项目的可复用能力，为当前 Defeature Agent 建立常驻、可回滚、可审核的 Siemens NX2406 通用 NXOpen 执行环境。本文不代表当前生产 Agent 已开放任意 NXOpen Python。
>
> 分析基线：
>
> - [Cai-aa/CAD-Agent-Hub，提交 `65e1d836`](https://github.com/Cai-aa/CAD-Agent-Hub/commit/65e1d8362ba92c5f3465f0443f3c471dfca2e9a7)
> - [DreamEnding/NX_MCP，提交 `179086b`](https://github.com/DreamEnding/NX_MCP/commit/179086b6de28a53d340132aca7678fa6ed03b422)
>
> 两个项目均采用 MIT License。复用代码时必须保留版权和许可证声明；Siemens NX 二进制、NXOpen 程序集、模板与文档不属于这两个 MIT License 的授权范围，不得随本项目重新分发。

## 1. 背景

当前 Defeature Agent 通过预先实现并加入白名单的 DSH Tools 和固定 NXOpen Journals 执行圆角识别与删除。这种方式适合已经标准化的高频任务，但存在能力上限：

- 开发者必须提前知道任务需要哪些 NX 功能；
- 每种新方法都需要实现 Tool、HTTP Schema、Journal 和测试；
- 固定 Tool 无法覆盖导入 STEP 中所有长尾去特征情况；
- 某些模型可能需要临时组合删除面、同步建模、曲面延伸、替换面、布尔或局部重建；
- 若让模型为每次尝试生成完整 Journal 并通过 `run_journal.exe` 执行，每次失败重试都会重复启动 NX 和导入模型。

本计划的核心目标是增加一个开放式 NX Runtime：模型可以针对当前几何动态生成 NXOpen Python，并在同一常驻 NX2406 Session 中连续执行、检查、回滚和修正，不再要求开发者事先封装每一种 Defeature 方法。

## 2. 目标与非目标

### 2.1 目标

1. 一个 CAD Job 只启动或分配一次 NX Runtime，并只导入一次当前 Accepted STEP。
2. 模型可以动态生成 NXOpen Python，完成未知的长尾 Defeature 操作。
3. 每次代码执行具有明确的 `read`、`preview` 或 `commit` 事务语义。
4. `preview` 无论成功或失败都必须回滚，并验证模型恢复。
5. `commit` 只有在执行和验证都成功后才保留；失败必须回滚。
6. 每次拓扑变化后使旧 Face、Edge、Vertex 引用失效，禁止模型盲目复用旧 Tag。
7. 客户端超时后通过操作账本查询实际结果，禁止未经核实的自动重试。
8. 最终结果仍通过现有 STEP 导出、OCC 重导入、三角化和 Candidate 审核流程发布。
9. 现有稳定专用 Tools 继续保留；开放式 Runtime 是补充，不是立即替换。
10. Runtime 的协议、认证、错误、状态和真实 NX 测试具有可审计的工程边界。

### 2.2 非目标

1. 不把每一个 NXOpen 类或方法映射成单独 MCP Tool。
2. 不移植两个外部项目的全部建模、CAM、工程图或机床功能。
3. 不把 NX Undo 宣称为 Python 安全沙箱。
4. 不允许模型直接覆盖 Accepted Revision 文件。
5. 不在第一阶段实现多用户 NX Worker 池或 Teamcenter 模式。
6. 不假定为 NX2412/NX2506 构建的 DLL 可直接用于 NX2406。
7. 不在本文计划阶段修改当前圆角删除业务逻辑。

## 3. 两个外部项目的职责划分

### 3.1 从 CAD-Agent-Hub 复用的能力

主要复用其 `MCP/UG` 目录中的以下思路：

- NX 启动目录自动加载 C# DLL；
- 非阻塞 `.NET Remoting` Bootstrap；
- Bootstrap 获得 NX Session 后立即返回，不长期占用 Journal；
- 通过 `Session.Execute` 调用 NX 内固定 Python Operations Module；
- `run_python` 的动态 NXOpen Python 执行思路；
- `NXOpen`、`session`、`workPart`、`displayPart` 上下文注入；
- 拓扑检查、选择条件和结果大小限制的实现经验；
- 本地 NXOpen 异常回读方式。

不直接照搬：

- 为 NX2412 构建的二进制文件；
- 固定端口和较宽的原始 Session Remoting 暴露面；
- 外部调用方可指定 Operations 文件路径的设计；
- 持久 `_EXEC_NS`；
- 未统一事务包装的原始 `exec()`；
- 与 Defeature 无关的大量 CAM、装配和机床工具。

### 3.2 从 DreamEnding/NX_MCP 复用的能力

主要复用以下控制面设计：

- MCP Sidecar 与 NX Bridge 分层；
- 随机端口和随机 256 位 Session Token；
- 原子发布的 Bridge Descriptor；
- Sidecar 每次调用重新读取 Descriptor；
- 协议版本、请求 ID 和负载大小限制；
- 结构化 `NXToolError`；
- Opaque Object Registry；
- wrong-kind、wrong-part 和 stale-object 拒绝；
- 统一 Undo Mark 和异常回滚框架；
- Workspace 双侧路径检查；
- Certified / Experimental 权限门控；
- Fake NX、协议测试和 Real NX 验收分层。

不采用为最终交互 Runtime：

- 通过长时间运行 Python Journal 泵请求的 batch-only Bridge；
- 只覆盖预定义 Tool 的能力模型；
- 为 NX2506 验证的代码和二进制假设；
- 仅在 Part 关闭时才失效的对象引用逻辑。

### 3.3 继续由当前 Defeature 项目负责的能力

以下能力保持当前系统为唯一事实来源：

- DSH Session 与 Agent Preset；
- Job、Revision 和 Candidate 状态机；
- Accepted Revision 不可被自动覆盖的规则；
- STEP 文件存储和路径所有权；
- STEP Entity ID 与浏览器 Mesh 映射；
- pythonOCC 重导入与结果验证；
- Candidate 网格、对比、接受和拒绝；
- Web GUI 与 Session-bound Host Tools；
- 当前成熟的专用圆角识别和删除 Tools。

## 4. 目标架构

```text
DeepSeek Harness / Defeature Agent
│
├─ 现有认证 Defeature Tools
│
└─ 通用 NX Runtime Tools
   ├─ nx_runtime_status
   ├─ nx_execute_python
   ├─ nx_get_operation_status
   ├─ nx_capture_views
   ├─ nx_reset_to_accepted
   └─ nx_publish_candidate
            │
            ▼
现有 FastAPI Defeature Backend
├─ Session / Job / Revision / Candidate
├─ Runtime lease 与 policy
├─ Operation ledger
├─ OCC 验证与 mesh
└─ MCP 或 Native Adapter
            │
            ▼
NX Runtime Sidecar
├─ 严格请求/响应 Schema
├─ Descriptor 发现
├─ Token / request_id / protocol_version
├─ Workspace 检查
└─ 调用串行化
            │
            ▼
authenticated localhost bridge
            │
            ▼
NX2406 内 NXDefeatureBridge.dll
├─ 非阻塞启动
├─ 固定窄接口
├─ 当前 NX Session
└─ Session.Execute
            │
            ▼
固定 nx2406_runtime_ops.py
├─ read / preview / commit
├─ fresh namespace
├─ Undo / rollback / validation
├─ object registry / runtime state
└─ 动态 NXOpen Python
            │
            ▼
当前内存 Work Part
            │
            ▼
STEP export → OCC validate → Candidate
```

## 5. MCP 与 DSH 的关系

MCP 不是 NX Runtime 本身，只是标准调用协议。实施时建议先把 NX Runtime 做成独立内部服务，并保留两个适配入口：

```text
NX Runtime Core
├─ DSH Native Tool Adapter
└─ MCP Adapter
```

近期只服务当前 Web GUI 时，DSH Native Tool Adapter 的接入成本更低，也能直接复用 Session、Attachment 和 Candidate 机制。Runtime 稳定后再开放 MCP Adapter，供其他 Agent 客户端复用。

无论使用哪种 Adapter，都必须调用同一套 Runtime Core 和 Operation Ledger，不得复制两套 NX 状态逻辑。

## 6. NX 内 Bridge 设计

### 6.1 不暴露原始 Session

不建议直接向任意外部调用方公开完整 `NXOpen.Session` Remoting Proxy。NX 内只暴露窄接口，例如：

```csharp
public interface INXDefeatureBridge
{
    string Ping(string requestJson);
    string Execute(string requestJson);
    string GetOperationStatus(string requestJson);
}
```

Bridge 内部负责：

- 校验 Token、协议版本和请求 ID；
- 只调用固定安装路径下的 `nx2406_runtime_ops.py`；
- 不接受调用方提供的 Operations 文件路径、类名或方法名；
- 串行化所有 NX 修改请求；
- 限制输入和输出大小；
- 记录桥版本、NX Build 和进程信息。

### 6.2 Descriptor

建议 Descriptor 至少包含：

```json
{
  "host": "127.0.0.1",
  "port": 54321,
  "token": "<256-bit random token>",
  "protocol_version": 1,
  "pid": 12345,
  "process_start_time": "...",
  "nx_version": "NX 2406.x",
  "bridge_build": "...",
  "created_at": "...",
  "expires_at": "...",
  "capabilities": ["dynamic-nxopen-v1"],
  "workspace_digest": "..."
}
```

要求：

- 使用操作系统分配的本机端口；
- 每次 Bridge 启动轮换 Token；
- Descriptor 原子替换；
- Windows 文件 ACL 限制为当前运行用户；
- Sidecar 校验 PID、进程启动时间、NX 版本和协议版本；
- NX 重启后 Sidecar 无需重启即可重新发现；
- Bridge 关闭时只删除属于当前 Token 的 Descriptor。

## 7. 通用 Runtime Tool 表面

### 7.1 `nx_runtime_status`

返回：

- Bridge 是否连接；
- NX 精确版本；
- 当前 Job 和 Revision；
- 当前 Work Part；
- `runtime_state`；
- 是否存在运行中、超时不明或回滚失败的操作；
- Runtime 是否允许继续修改。

### 7.2 `nx_execute_python`

建议请求：

```json
{
  "operation_id": "op-uuid",
  "base_state": 4,
  "mode": "read | preview | commit",
  "code": "...",
  "preview_operation_id": null,
  "deadline": "..."
}
```

模型只提交当前操作代码，不提交 Job ID、文件路径或输出路径；这些由 Session-bound Host Tool 和后端注入。

### 7.3 `nx_get_operation_status`

根据 `operation_id` 返回操作账本状态，用于：

- Tool 调用超时；
- 页面断线；
- Sidecar 重启；
- NX 完成操作但响应丢失；
- 防止重复执行。

### 7.4 `nx_capture_views`

对当前 NX 内存状态生成视图，使 Agent 能在多步操作后重新观察结果。视图必须绑定：

- Session；
- Job；
- Revision；
- `runtime_state`。

### 7.5 `nx_reset_to_accepted`

执行：

```text
关闭或放弃当前 Work Part
→ 重新载入当前 Accepted STEP
→ 清空运行时对象引用
→ runtime_state 重置
```

用于放弃实验路线、恢复 `rollback_failed` Runtime 或处理 GUI 外部修改。

### 7.6 `nx_publish_candidate`

执行：

```text
确认无 running/ambiguous 操作
→ NX 导出到后端指定临时路径
→ OCC 重导入和有效性检查
→ 三角化
→ 发布唯一 Candidate
```

模型不能自行指定路径，也不能直接接受 Candidate。

## 8. 动态 NXOpen Python 执行模型

### 8.1 Fresh Namespace

每次调用创建新命名空间：

```python
namespace = {
    "NXOpen": NXOpen,
    "session": session,
    "workPart": work_part,
    "displayPart": display_part,
    "ctx": runtime_context,
}
```

禁止保留 CAD-Agent-Hub 当前模块级 `_EXEC_NS`。跨调用只保存：

- JSON 可序列化结果；
- Operation Ledger；
- Runtime 管理的 Opaque Entity References；
- 几何与拓扑签名。

不保存原始 Face、Edge、Builder 或其他 NXOpen Python 对象。

### 8.2 模型代码约定

建议模型生成：

```python
def main(ctx):
    session = ctx.session
    part = ctx.work_part

    # NXOpen 查询或修改

    return {
        "changed": True,
        "summary": "Removed local blend and healed support faces"
    }

result = main(ctx)
```

Runtime 负责事务、超时状态、日志、回滚、状态升级和结果序列化；模型负责选择具体 NXOpen API 及调用顺序。

### 8.3 输出限制

- `result` 必须可 JSON 序列化；
- stdout/stderr 截断到配置上限；
- 禁止返回原始 NXOpen 对象；
- Traceback 在开发模式可返回脱敏版本，生产模式只返回结构化阶段和 NX ErrorCode；
- 请求源代码和运行日志不得进入公开 Candidate 描述。

## 9. 事务语义

### 9.1 `read`

流程：

```text
校验 base_state
→ 捕获模型修改标记和基础签名
→ 执行代码
→ 再次检查模型
→ 若发生修改则回滚并返回 READ_MUTATED_MODEL
```

适合：

- 枚举 Body/Face/Edge；
- 获取曲面类型、半径和邻接；
- `GetBlendData()`；
- `CreateRuleFaceConnectedBlend` 的只读选集分析；
- 测量和特征检查。

### 9.2 `preview`

流程：

```text
校验 base_state
→ 创建 Runtime 外层 Undo Mark
→ 捕获 baseline
→ 执行模型代码
→ NX Update
→ 验证临时结果
→ 收集结构化结果和可选视图
→ 无论成功失败都 UndoToMark
→ 重新获取 Body
→ 验证恢复到 baseline
```

只有确认恢复成功，才能返回 `rolled_back=true`。恢复无法证明时进入 `rollback_failed`，封锁 Runtime。

### 9.3 `commit`

流程：

```text
校验 base_state
→ 可选校验 preview_operation_id 和 code_hash
→ 创建 Runtime 外层 Undo Mark
→ 捕获 baseline
→ 执行模型代码
→ NX Update
→ 执行结果验证
→ 成功则保留并 runtime_state + 1
→ 失败则 Undo 并验证恢复
```

成功 Commit 只改变 NX Runtime 的工作状态，不自动改变 Accepted Revision。

### 9.4 回滚失败

若 Undo 后无法证明恢复：

```text
operation = rollback_failed
runtime = quarantined
```

后续只允许：

- 查询状态；
- 导出诊断；
- `nx_reset_to_accepted`。

禁止继续执行任何修改。

## 10. Operation Ledger 与超时

每次执行创建持久记录：

```text
received
→ rejected
→ queued
→ expired_before_start
→ running
→ validating
→ committed
或
→ rolling_back
→ rolled_back / rollback_failed
或
→ ambiguous
```

记录字段至少包括：

- `operation_id` 和幂等 Key；
- Session、Job、Accepted Revision；
- `base_state` 和 `result_state`；
- mode；
- code hash，不必默认保存完整代码到普通业务日志；
- Bridge、NX Build 和 Executor 版本；
- 开始、截止、完成时间；
- Undo Mark 标识；
- baseline 和 post-state 摘要；
- 验证报告；
- 错误阶段、NX ErrorCode、回滚证据；
- 最终状态。

### 10.1 超时规则

客户端超时只表示“没有及时收到响应”，不表示 NX 已停止。超时后：

1. 将客户端视图标记为 `ambiguous`；
2. 禁止使用新的 Operation ID 自动重试相同修改；
3. 调用 `nx_get_operation_status`；
4. 对照当前 `runtime_state` 和几何签名进行 reconciliation；
5. 明确得到 `committed`、`rolled_back` 或 `rollback_failed` 后才允许下一次修改。

如果请求在截止时间前仍未开始，Bridge 应标记 `expired_before_start`，不得稍后执行。

## 11. Runtime State 与实体身份

### 11.1 Runtime State

每个成功 Commit：

```text
state-N → state-(N+1)
```

所有调用必须声明 `base_state`。旧状态请求返回：

```text
NX_STATE_CONFLICT
```

### 11.2 Opaque Reference

建议实体引用包含：

```json
{
  "entity_id": "face-uuid",
  "kind": "face",
  "part_id": "part-uuid",
  "runtime_state": 4,
  "body_generation": 7,
  "fingerprint": {
    "surface_type": "cylinder",
    "area": 123.45,
    "centroid": [1.0, 2.0, 3.0],
    "radius": 2.5,
    "adjacency_signature": "..."
  }
}
```

原始 NX Tag 只能作为当前状态内的内部定位信息，不应直接暴露为持久身份。

### 11.3 失效规则

- Part 关闭或重载：该 Part 全部引用失效；
- Commit：受影响 Body 的 Face/Edge/Vertex 引用全部失效；
- Undo：Undo 前后拓扑引用默认失效；
- STEP 重新导入：全部 NX 引用失效；
- 无法唯一重新识别：返回 `NX_OBJECT_AMBIGUOUS`，不得最近点猜测。

该机制与现有 STEP Entity ID 并存：STEP ID 用于 Accepted STEP 和 Web Mesh；NX Runtime 引用只用于当前内存状态。

## 12. Job 与 NX Worker 生命周期

### 12.1 第一版：单 Worker 独占租约

第一版只支持：

```text
一个 NX2406 Worker
→ 同时绑定一个 Job
```

加载流程：

```text
获取 Worker lease
→ 验证当前 Accepted Revision
→ 关闭旧 Work Part
→ 导入 Accepted STEP
→ 记录 Part fingerprint
→ runtime_state = 0
```

### 12.2 调用前校验

每次操作前检查：

- DSH Session 是否绑定该 Job；
- Accepted Revision 是否未变化；
- Worker lease 是否仍属于该 Session；
- Work Part 是否为目标 Part；
- Part fingerprint 是否匹配；
- `base_state` 是否当前；
- 是否存在 running、ambiguous 或 rollback_failed 操作。

### 12.3 GUI 外部修改

若用户在 NX GUI 中手工改变 Part，Runtime 必须检测 modification stamp、Feature/Body 摘要或其他指纹变化，返回：

```text
NX_RUNTIME_DIVERGED
```

第一版不自动吸收 GUI 修改，只允许重置到 Accepted 或由用户进入专门的接管流程。

### 12.4 后续 Worker 池

稳定后再增加：

- 多 NX Worker；
- Job 调度和租约续期；
- 空闲回收；
- 心跳和崩溃检测；
- 从 Accepted 或内部 checkpoint 重建；
- 并发 Session 隔离。

## 13. Candidate 与现有审核流程

NX Runtime 中的 Commit 不等于 Accepted Revision。Agent 可进行多次内部 Commit：

```text
state-0：原始导入
state-1：删除局部圆角
state-2：修复角部过渡
state-3：重建支撑面
```

只有调用 `nx_publish_candidate` 时才：

1. 确认无不明操作；
2. NX 导出一次临时 STEP；
3. 检查输出存在且非空；
4. pythonOCC 重导入；
5. 检查 Shape、Body 和基础几何有效性；
6. 三角化；
7. 创建一个 Candidate；
8. 将 Candidate 交给用户接受或拒绝。

拒绝 Candidate：

```text
保留 Accepted 不变
→ 重置 NX Runtime 到 Accepted
```

接受 Candidate：

```text
Candidate → Accepted
→ Runtime 重新绑定新的 Accepted 基线
```

## 14. 安全模型

### 14.1 基本事实

只要执行模型提供的任意 Python，就相当于提供高权限代码执行。NX Undo 只能回滚部分 NX 模型状态，不能撤销：

- 文件写入和删除；
- 子进程；
- 网络请求；
- 环境变量和注册表修改；
- 解释器全局状态；
- NX Undo 系统未记录的应用状态；
- NX 进程崩溃或无限阻塞。

AST 检查、Import 白名单和关键词过滤只能作为防误用 Guardrail，不能被描述成真正沙箱。

### 14.2 两种 Agent 模式

#### 受限生产 Agent

`defeature-agent`：

- 继续只开放当前认证专用 Tools；
- 不提供任意 NXOpen Python；
- 适合常规任务和低风险生产流程。

#### 开放式 NX General Agent

建议新增独立 Preset：

- 可调用 `nx_execute_python`；
- 使用独立 Windows 用户或隔离执行环境；
- 无生产凭证；
- 限制网络；
- 只访问 Job 工作区副本；
- 不允许覆盖 Accepted；
- 最终 Candidate 必须人工审核。

### 14.3 双侧能力开关

任意执行能力必须同时在以下两侧显式启用：

- Sidecar/Backend；
- NX 内 Bridge/Executor。

只隐藏 MCP Tool 不足以关闭能力，因为本机调用方可能绕过 MCP 直接访问 Bridge。

### 14.4 可选开发 Guardrails

在不宣称安全沙箱的前提下，可增加：

- 每次 fresh namespace；
- 源代码长度限制；
- 禁止 `import` 或采用非常窄的 Import Allowlist；
- 禁止危险 builtins 和 dunder traversal；
- 禁止模型直接获得文件路径；
- 代码 Hash 与短期 Capability Grant 绑定；
- Capability 绑定 Session、Part、mode、过期时间和最大调用次数；
- 独立临时工作区和操作系统级访问控制。

## 15. NX2406 兼容性风险

两个外部项目分别以 NX2412 和 NX2506 为主要验证环境。NX2406 必须重新验证：

| 风险 | 可能影响 | 对策 |
|---|---|---|
| C# DLL 引用版本不同 | DLL 无法加载 | 使用本机 NX2406 程序集重新编译 |
| `.NET Framework` 目标不同 | Startup/Remoting 失败 | 按 NX2406 样例项目设置 Target Framework |
| `Session.Execute` 行为不同 | Python Gateway 失败或线程错误 | 做最小反射和 no-op 实机探针 |
| 内嵌 Python 版本不同 | Operations Module 语法或库不兼容 | NX 侧只使用兼容语法和标准库 |
| Builder API 漂移 | 删除、修补或销毁异常 | 基于 NX2406 录制代码和本地文档验证 |
| Remoting 线程行为不同 | NX 卡死或 GUI 不响应 | 连续调用、并发拒绝和 GUI 响应测试 |
| Undo 覆盖范围不同 | 回滚不完整 | 基线签名与重获取验证，失败则隔离 Runtime |
| STEP 导入拓扑不稳定 | 旧 ID 指向错误实体 | State、Generation、Opaque ID 和签名 |
| 超时后 NX 继续执行 | 重复删除 | Operation Ledger 和状态 reconciliation |
| Teamcenter 模式差异 | 保存和路径规则失效 | 第一版只认证 Native Part 模式 |

## 16. 实施阶段

### 阶段 0：冻结协议和威胁模型

交付物：

- Runtime 请求/响应 Schema；
- Operation Ledger 状态机；
- `read/preview/commit` 语义；
- Workspace 和输出策略；
- Agent 权限模式；
- NX2406 精确 Build 记录；
- 第三方 License 清单。

验收：

- 明确超时是“不确定”而不是“取消”；
- 明确 Undo 不是安全沙箱；
- Accepted 文件永不作为模型代码输出目标；
- 明确生产受限模式与开放式模式的权限差异。

### 阶段 1：离线协议与状态测试

从 DreamEnding/NX_MCP 的测试结构移植：

- Descriptor 原子写入和重发现；
- Token 正确、错误和过期；
- 协议版本不匹配；
- request_id 不匹配；
- 非法和超大 JSON；
- 严格 Schema 与未知字段拒绝；
- Workspace 路径逃逸；
- wrong-kind、wrong-part、stale reference；
- Operation ID 幂等性；
- 排队请求过期；
- 超时后的状态查询；
- rollback 和 rollback_failed 分类。

此阶段使用 Fake NX，只验证编排逻辑，不宣称 NXOpen 几何能力有效。

### 阶段 2：NX2406 最小 Bridge PoC

使用本机 NX2406 的：

```text
NXOpen.dll
NXOpen.Utilities.dll
```

重新编译 C# Bridge 和 Client。

测试：

```text
NX 加载 DLL
→ Bootstrap 快速返回
→ GUI 不保持“工作进行中”
→ 外部 ping
→ 获取 NX Build
→ 获取 Work Part
→ 连续 100 次只读调用
→ NX 重启后 Sidecar 自动重发现
```

这是最早的 Go/No-Go Gate。

### 阶段 3：固定 Python Executor

实现固定路径的 `nx2406_runtime_ops.py`：

- 接收认证后的结构化请求；
- fresh namespace；
- 注入 NXOpen 上下文；
- 结果 JSON 化和大小限制；
- Read 模式修改检测；
- Operation Ledger 写入；
- 禁止调用方替换 Operations Module。

先只允许只读代码，验证连续调用稳定性。

### 阶段 4：Preview/Commit 事务

在临时 Part 上验证：

1. 创建或删除临时几何；
2. Preview 后 Undo；
3. 证明 Body 数量、体积、面积和包围盒恢复；
4. Commit 后 State 增加；
5. 代码执行一半抛异常后自动 Undo；
6. Builder 在 `finally` 中销毁；
7. Undo 失败时 Runtime 进入 quarantine；
8. Commit 后旧 Face/Edge 引用被拒绝。

### 阶段 5：开放式 Defeature 实机验证

建立代表性 STEP Fixture：

- 定半径圆角；
- 变半径圆角；
- 角部过渡；
- 相切链；
- B-Spline 过渡；
- 孔与整圆柱混淆；
- 与倒角相邻的圆角；
- DeleteFace 直接失败但替代方法可行的模型。

验证模型可动态执行：

```text
read 检查
→ preview 方法 A
→ 回滚
→ preview 方法 B
→ 回滚
→ commit 可行方法
→ 重新检查拓扑
```

且全过程不重启 NX、不重新导入 STEP。

### 阶段 6：接入当前 Backend

实现：

- Session-bound Runtime lease；
- Job/Accepted Revision 加载；
- `runtime_state` 持久化；
- Candidate 导出；
- OCC 验证和三角化；
- Reject/Accept 后 Runtime 重置；
- Web 页面状态和错误显示。

第一版通过 DSH Native Tools 接入即可。

### 阶段 7：开放式 Agent Preset

新增独立高权限 Preset，并编写执行纪律：

1. 先 `read` 观察；
2. 修改前优先 `preview`；
3. Preview 成功后才 `commit`；
4. Commit 后重新读取拓扑；
5. 不持久保存 Tag、Face 或 Edge；
6. 超时后先查状态，禁止盲目重试；
7. 完成后调用 Candidate 发布；
8. 不自行访问文件、网络和进程。

现有 `defeature-agent` 保持受限。

### 阶段 8：MCP Adapter

Runtime 稳定后，把同一核心服务暴露为 MCP Tools/Resources：

```text
DSH Native Adapter ─┐
                    ├→ Runtime Core
MCP Adapter ────────┘
```

验证 DSH MCP Client 的：

- Tool 名称；
- Schema；
- Session 绑定；
- 超时；
- 图片附件；
- 错误 Content Blocks。

### 阶段 9：稳定性与 Worker 池

最后实施：

- Worker 心跳；
- NX 崩溃恢复；
- 多 Worker 调度；
- Job lease；
- 空闲回收；
- 长时间压力测试；
- 独立 Windows 用户或虚拟化部署；
- 审计和告警。

## 17. 测试矩阵

### 17.1 协议

- 正确/错误 Token；
- Descriptor stale、损坏、被替换；
- 协议版本不匹配；
- 请求重复；
- 响应丢失；
- 请求和响应过大；
- Sidecar 在 NX 前启动；
- NX 重启但 Sidecar 不重启。

### 17.2 事务

- Read 无修改；
- Read 意外修改；
- Preview 成功回滚；
- Preview 失败回滚；
- Commit 成功；
- Commit 失败回滚；
- Post-validation 失败回滚；
- Undo 失败；
- Builder cleanup 失败；
- 超时但 NX 后来成功；
- 超时但 NX 后来失败；
- 相同 Operation ID 重放。

### 17.3 拓扑

- Commit 后旧 Face ID；
- Undo 后旧 Face ID；
- Part 关闭后旧 ID；
- Body 重建后相似面歧义；
- STEP 重新导入；
- 多 Body 模型；
- 相同几何签名但不同拓扑位置。

### 17.4 Candidate

- NX Commit 后未发布；
- 发布前存在 ambiguous operation；
- STEP 导出失败；
- OCC 导入失败；
- OCC Shape 无效；
- Candidate 已存在；
- Reject 后恢复 Accepted；
- Accept 后建立新 Runtime 基线。

### 17.5 稳定性

- 连续 100 次只读调用；
- 连续多次 Preview；
- 20 个以上真实模型端到端运行；
- NX GUI 手工关闭 Part；
- NX GUI 外部修改；
- NX 进程崩溃；
- Bridge 卸载/重载；
- 浏览器断线和重连。

## 18. 关键验收标准

第一版开放式 Runtime 完成必须同时满足：

1. NX2406 中 Bridge 非阻塞启动，GUI 可继续响应。
2. 一个 Job 可连续执行多次动态 NXOpen Python，不重复冷启动 NX。
3. Accepted STEP 在 Job 加载期间只导入一次，除非显式 Reset 或恢复。
4. Preview 成功和失败都能证明恢复；否则 Runtime 被隔离。
5. Commit 后所有旧拓扑引用都被可靠拒绝。
6. 客户端超时后可以查询最终状态，且不会自动重复修改。
7. 任意代码不能直接指定 Accepted 或 Candidate 文件路径。
8. NX 内结果只有经过 STEP 导出和 OCC 验证后才能成为 Candidate。
9. Candidate 必须由用户接受后才成为 Accepted。
10. NX2406 真实验收覆盖至少 20 次无重试端到端运行。
11. 两个外部项目的 MIT 归属和本项目修改说明完整保留。
12. 当前受限 `defeature-agent` 不因开放式 Runtime 而自动获得高权限。

## 19. 失败与恢复策略

| 故障 | Runtime 行为 | 恢复 |
|---|---|---|
| Bridge 未连接 | 不执行 | 启动或重新发现 NX |
| State 冲突 | 拒绝请求 | Agent 重新读取状态 |
| NXOpen 异常 | Undo | 返回结构化错误 |
| Preview 恢复不一致 | quarantine | Reset 到 Accepted |
| Commit 后响应丢失 | ambiguous | 查询 Operation Ledger |
| NX 崩溃 | Worker dead | 重启并从 Accepted/checkpoint 加载 |
| GUI 外部修改 | diverged | 用户确认后 Reset 或专门接管 |
| OCC 验证失败 | 不发布 Candidate | 保留 NX 诊断状态或 Reset |
| Candidate 被拒绝 | Accepted 不变 | 重载 Accepted |
| 任意代码触发外部副作用 | Undo 无法保证恢复 | 依赖 OS 隔离和审计处理 |

## 20. 实施顺序摘要

```text
1. 冻结协议、威胁模型和权限模式
2. 移植 Descriptor / Token / Error / Ledger / Registry 的离线实现
3. 用 NX2406 程序集重编译最小非阻塞 C# Bridge
4. 验证 Session.Execute 和固定 Python Executor
5. 实现 read / preview / commit 与回滚验证
6. 实现 runtime_state 和拓扑引用失效
7. 用真实长尾 Defeature Fixture 验证动态 NXOpen Python
8. 接入现有 Job / Accepted / Candidate / OCC
9. 新增隔离的 nx-general-agent Preset
10. 稳定后增加 MCP Adapter、Worker 池和崩溃恢复
```

## 21. 最终决策

本计划采用以下组合：

> CAD-Agent-Hub 的常驻 NX 与动态 `Session.Execute` 能力，结合 DreamEnding/NX_MCP 的认证、协议、状态、事务和测试纪律，再接入当前 Defeature 系统的 Revision/Candidate/OCC 审核边界。

最终系统不重新退化为“每种 Defeature 方法都预先写成一个 Tool”。成熟专用 Tool 继续负责高频稳定路径；当其无法解决任务时，开放式 Agent 通过 `nx_execute_python` 动态组合 NXOpen API，在常驻 NX2406 中进行 `read → preview → commit` 的迭代。

这种方案能同时实现：

- 开放式长尾能力；
- 避免每次尝试冷启动 NX；
- 明确的 CAD 事务和状态一致性；
- 超时后的可恢复性；
- 拓扑身份安全；
- 与现有 Candidate 人工审核流程兼容。
