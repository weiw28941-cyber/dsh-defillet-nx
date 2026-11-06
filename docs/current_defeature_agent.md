# 当前 Defeature Agent：运行方式、处理过程与系统逻辑

> 本文主要记录提交 `f752996` 后的系统设计。安装、环境配置与打包以 `dsh-plugins/defeature-ui/README.md` 为准；下文部分部署说明属于历史记录。
> `docs/global_fillet_deletion_probe_and_batch_plan.md` 是后续优化计划，不代表生产全局删除算法已经完成改造。
> 常驻 NX Runtime 的底层协议另见 `docs/persistent_nx_runtime.md`，受控 CAD Tool 合同另见 `docs/nx_controlled_cad_tools.md`。生产 preset 仍不暴露动态 NXOpen 或新增的单步运行时 Tool。
> 下列 CAD 源码路径采用迁移目标 `cad/`；迁移及运行时路径更新完成前，相应目录仍位于仓库根目录。

## 1. 系统用途

Defeature Agent 是运行在 DeepSeek Harness（DSH）中的 Siemens NX2406 STEP 去圆角系统。用户在浏览器中上传 STEP、查看和选择 BREP 面，并通过对话要求 Agent：

- 查看当前 CAD Job、Accepted Revision 和 Candidate Revision；
- 读取用户在三维模型中选中的面；
- 使用 NX 检测圆角候选并按共享边划分连通块；
- 生成全部圆角组件的彩色四视图，交给视觉模型分析；
- 删除指定的一个或一批圆角连通块；
- 在一次 Tool 调用和一次 NX 进程中删除全部或尽可能多的圆角；
- 将成功结果作为 Candidate 交给用户审核。

系统不会让生产 Agent 生成任意 NXOpen 脚本。实际几何操作只能通过预先实现并加入白名单的固定 Journal 执行。

## 2. 系统组成

```text
浏览器：http://127.0.0.1:3080
│
├─ DeepSeek Harness Web
│  ├─ 对话与 Agent Session
│  ├─ defeature-agent / defeature-agent-dev preset
│  ├─ Defeature Host Tools
│  └─ CAD 工作台 client plugin
│
├─ @defeature/dsh-ui Host Plugin
│  ├─ 自动启动 FastAPI 子进程
│  ├─ /defeature/api → FastAPI /api 反向代理
│  └─ 注册 Session-bound CAD Tools
│
├─ FastAPI Backend
│  ├─ Job / Revision / Candidate 状态机
│  ├─ SQLite 与文件存储
│  ├─ OCC STEP 导入、精确面映射和三角化
│  └─ NX Journal 请求、报告与结果验证
│
├─ Siemens NX2406 / NXOpen
│  ├─ 固定圆角识别 Journal
│  ├─ 固定单组件/批量删除 Journal
│  ├─ 固定全局删除 Journal
│  └─ 开发模式：一个常驻 NX Journal Worker（队列式 read/commit/Undo/export）
│
└─ pythonOCC + Pillow
   ├─ STEP 结果重导入验证
   ├─ 浏览器显示网格
   └─ 圆角组件 2×2 多视角分析图
```

关键文件：

- DSH 后端管理器：`dsh-plugins/defeature-ui/lib/index.js`
- DSH CAD Tools：`dsh-plugins/defeature-ui/lib/tools.js`
- CAD 工作台：`dsh-plugins/defeature-ui/src/CadWorkbench.js`
- FastAPI 路由：`webapp/backend/app/main.py`
- Job/Revision 存储：`webapp/backend/app/store.py`
- 组件业务逻辑：`webapp/backend/app/fillet_components.py`
- NX 进程封装：`webapp/backend/app/nx_worker.py`
- 当前全局删除 Journal：`cad/Defillet/nx_web_remove_all_fillets.py`
- 常驻 NX Worker：`cad/nx_persistent_worker.py`
- 常驻 NX 协调器：`webapp/backend/app/nx_runtime_coordinator.py`
- 受控运行时 CAD Tools：`webapp/backend/app/nx_cad_tools.py`
- 视觉分析渲染：`webapp/backend/app/component_rendering.py`

## 3. 运行环境

### 3.1 Python 和 OCC

在工作台配置兼容的 Python/OCC 解释器，或经用户确认安装独立环境。
`pythonExecutable` / `DEFEATURE_PYTHON` 显式配置无效时会提示重新配置，
不会静默回退到其他环境。具体解析与安装流程见插件 README。

主要依赖包括：

- FastAPI / Uvicorn；
- pythonOCC；
- Pillow；
- Pydantic。

### 3.2 Siemens NX2406

默认 NX 根目录：

```text
D:\Program Files\Siemens\NX2406
```

主要文件：

```text
D:\Program Files\Siemens\NX2406\NXBIN\run_journal.exe
D:\Program Files\Siemens\NX2406\STEP214UG\ugstep214.def
```

可通过下列环境变量覆盖：

```powershell
$env:UGII_BASE_DIR = "D:\Program Files\Siemens\NX2406"
$env:NX_RUN_JOURNAL = "$env:UGII_BASE_DIR\NXBIN\run_journal.exe"
```

默认普通 NX Journal 超时为 600 秒，全局删除 Journal 超时为 3600 秒：

```text
STEP_WEB_NX_TIMEOUT_SECONDS
STEP_WEB_NX_GLOBAL_TIMEOUT_SECONDS
```

### 3.3 DSH 模型

组件视觉分析 Tool 要求当前模型声明支持图像输入。当前 DSH 默认模型配置包含：

```yaml
input:
  - text
  - image
```

若当前路由只支持文本，`defeature_analyze_fillet_components` 会拒绝执行，而不是只把图片路径作为文本交给模型。

## 4. 启动方式

### 4.1 当前机器的部署前提

以下启动步骤适用于当前已经配置好的机器。它依赖两项仓库外的 DSH 配置：

- Web Profile 已将本仓库的 `dsh-plugins/defeature-ui` 以本地 link 安装为 `@defeature/dsh-ui` bundle；
- `%DSH_HOME%\.agent-presets\` 中已经安装 `defeature-agent` 和 `defeature-agent-dev` 两个 preset。

在新机器上，仅克隆本仓库并执行 `dsh web` 不会自动获得 CAD 工作台和这两个 preset；需要先将 Plugin 安装到目标 DSH Web Profile，并部署相应 preset 配置。

### 4.2 正常使用

在 PowerShell 中进入仓库，然后启动现有 DSH Web：

```powershell
cd C:\path\to\Defeature
dsh web
```

浏览器访问：

```text
http://127.0.0.1:3080
```

不需要单独启动 FastAPI。`@defeature/dsh-ui` Host Plugin 会：

1. 选择 Python；
2. 分配一个仅监听 `127.0.0.1` 的随机端口；
3. 启动：

   ```text
   python -m uvicorn webapp.backend.app.main:app --host 127.0.0.1 --port <随机端口>
   ```

4. 轮询 `/api/health`，等待后端就绪；
5. 将浏览器和 Tool 请求从 `/defeature/api/*` 转发到 FastAPI `/api/*`；
6. DSH 退出时终止该 FastAPI 子进程。

后端日志写入：

```text
%DSH_HOME%\logs\defeature-backend.log
```

后端状态可在以下地址查看：

```text
http://127.0.0.1:3080/defeature/status
```

### 4.3 选择 Agent Preset

新建会话时选择以下之一：

#### `defeature-agent`

生产 CAD Agent。只挂载受控 Defeature Tools、Skill、目标和交互工具，不提供通用 Shell、文件编辑、网络搜索或任意脚本执行能力。

注意：当前生产 preset 的 persona 仍残留“删除 Tool 尚未出现”的旧指令，而实际 Tool 已经注册。该冲突可能让模型拒绝本来已经支持的删除请求。完成 preset 修正前，若生产会话错误地声称不支持删除，应将其视为配置问题，而不是后端能力缺失。

#### `defeature-agent-dev`

开发调试 Agent。除 Defeature Tools 外，还提供代码读取、修改、Shell、测试和调试能力。它用于开发系统，不应替代生产 Agent 的受控 CAD 执行路径。

CAD 工作台只在这两个 preset 的会话中启用。其他 preset 会显示“CAD 工作台未在此会话启用”。

### 4.4 修改后的重新加载规则

- 修改 Host Tool、FastAPI 管理器或普通后端代码后，应重启 `dsh web`，然后刷新页面；
- 修改 CAD client plugin 后，先执行：

  ```powershell
  npm --prefix dsh-plugins/defeature-ui run build
  ```

  当前没有 `pnpm run dev:web` watcher 时，还需刷新页面；
- 不要另起一个 Vite 服务来替代当前 GUI。DSH Web 会注入自己的启动上下文，独立 Vite 页面不是完整应用。

## 5. Job、Session 和 Revision 数据模型

### 5.1 Session-bound Job

用户从 CAD 工作台上传 STEP 时，请求包含当前 DSH Session ID。后端创建一个 Job 并绑定该 Session。

生产 Tool 不接受用户或模型提交的 Job ID；它根据调用 Tool 的 Agent Session 自动查找对应 Job，从而避免跨 Job 操作。

### 5.2 文件存储

默认根目录：

```text
webapp/jobs/
```

主要结构：

```text
webapp/jobs/
├─ jobs.sqlite3
└─ <job-id>/
   ├─ revisions/
   │  ├─ r0/
   │  │  ├─ model.stp
   │  │  ├─ mesh.json
   │  │  └─ NX 检测、图片和日志文件
   │  └─ r1/
   │     ├─ model.stp
   │     ├─ mesh.json
   │     └─ 操作报告
   └─ operations/
      └─ remove-all-<uuid>/
```

SQLite 保存：

- Job 状态；
- Session 绑定；
- Accepted/Candidate Revision；
- Revision 父子关系和状态；
- 任务事件；
- 当前鼠标选面。

### 5.3 Accepted 与 Candidate

每个 Job 始终以 Accepted Revision 作为后续操作基线。

```text
上传 STEP
→ r0 = Accepted
→ 删除成功
→ r1 = Candidate，r0 仍是 Accepted
```

用户操作：

- 接受 Candidate：旧 Accepted 变为 `superseded`，Candidate 变为新的 Accepted；
- 拒绝 Candidate：Candidate 变为 `rejected`，原 Accepted 不变。

同一 Job 最多允许一个待审核 Candidate。存在 Candidate 时，新的删除操作会返回冲突，Agent 不得自动接受或拒绝 Candidate。

## 6. STEP 上传和显示过程

上传只接受 `.stp` 和 `.step`，默认最大 100 MiB，可由 `STEP_WEB_MAX_UPLOAD_BYTES` 修改。

处理过程：

```text
上传 STEP
→ 创建 Job 和 r0
→ 保存 revisions/r0/model.stp
→ OCC 读取 STEP
→ 按面三角化
→ 建立 STEP Part 21 ADVANCED_FACE ID 映射
→ 写入 mesh.json
→ Job 进入 ready
```

`mesh.json` 中每个可渲染面包含：

- Revision 内部 `faceId`；
- `faceIndex`；
- 精确 `stepEntityId`；
- 曲面类型、面积和质心；
- Three.js 使用的顶点和三角形索引。

浏览器分别显示 Accepted 和 Candidate。用户只能在 Accepted 视图上选择目标面。

## 7. 当前注册的 Agent Tools

| Tool | 用途 | 是否修改几何 |
|---|---|---:|
| `defeature_get_context` | 读取当前 Session 的 Job、Accepted、Candidate 和状态 | 否 |
| `defeature_get_selection` | 读取 CAD 工作台当前选中的面 | 否 |
| `defeature_detect_fillet_components` | 用 NX 检测圆角候选及共享边连通块 | 否 |
| `defeature_analyze_fillet_components` | 生成全部组件的彩色四视图并交给视觉模型 | 否 |
| `defeature_delete_fillet_component` | 删除一个当前 Revision 的组件 | 是 |
| `defeature_delete_fillet_components` | 用一次 NX Commit 删除指定组件批次 | 是 |
| `defeature_remove_all_fillets` | 一次 Tool 调用内删除全部或尽可能多的组件 | 是 |

以上 7 个 Tool 是生产 `defeature-agent` 的成熟 allowlist。生产删除策略仍只有：

```text
whole-component-face-edge-blend
```

开发 preset 在 `enableDynamicRuntime=true` 时另外注册以下受控 Tool：

| Tool | 用途 | 是否修改几何 |
|---|---|---:|
| `defeature_reanalyze_current_state` | 检测当前 NX 内存状态并签发状态绑定的组件引用 | 否 |
| `defeature_resolve_accepted_component` | 用精确 STEP 面身份映射 Accepted 组件与 state-0 运行时组件 | 否 |
| `defeature_get_component_topology` | 查询组件面、曲面类型、分支、端点和闭环 | 否 |
| `defeature_segment_fillet_component` | 生成受控、状态绑定的局部 Segment | 否 |
| `defeature_try_delete_component` | 对完整组件执行一次 Commit 式删除 | Commit 后拓扑检查；通过才保留，否则回滚 |
| `defeature_try_delete_segment` | 对一个受控 Segment 执行一次局部删除 | Commit 后拓扑检查；通过才保留，否则回滚 |
| `defeature_try_delete_face_group` | 对模型明确提供的一组当前状态面执行开发模式删除，可选择允许预期的关联支撑面变化 | 仅开发模式；提交异常仍回滚，默认仍执行拓扑政策 |
| `defeature_rollback_latest_delete` | 撤销最新一次仍保留的受控删除 | LIFO 回退；成功后状态版本前进且旧引用失效 |
| `defeature_finish_and_publish` | 最终统一导出、OCC 验证并发布一个 Candidate | 导出当前累计状态 |

运行时删除方法允许：

```text
face_edge_blend
blend
generic_face
```

NX FaceEdgeBlend Preference 允许：

```text
cliff（默认）
notch
```

`DeletePartialBlend` 在所有活跃删除路径中固定为 `False`，不作为 Tool 或 HTTP 参数开放。开发模式的局部删除只能通过后端生成的 `segment_id` 完成。

开发模式现在还允许模型把多个当前状态的 `face_ref` 组成显式 Face Group。该能力用于圆角删除必须连带删除相邻支撑面、封口面或其他已确认相关面的情况。生产模式不注册此 Tool；开发模式也不会接受原始 NX Tag、STEP Face ID 或任意面集合，只接受最近一次 `reanalyze_current_state` 返回的状态绑定引用。`allow_related_faces=true` 仅放宽“非目标命名面消失”的拓扑政策，Commit 异常、回滚失败和严重存活面边界变化仍会阻止保留。

## 8. 鼠标选面逻辑

用户点击 Accepted 模型时，工作台保存：

- `face_id`；
- `face_index`；
- `step_entity_id`；
- 点击点或面质心。

选择会异步同步到当前 Session。后端逐项校验它们是否属于当前 Accepted Revision 的 `mesh.json`。Accepted Revision 变化后，前端会清除旧选择；后端读取到跨 Job 或跨 Revision 的旧选择时也会返回空集合。

当前生产 CAD Tool 只提供 `defeature_get_selection` 读取选择。单组件删除仍以 NX 检测产生的 `fc-xxx` 为受控输入，不允许 Agent 根据鼠标选择自行拼装 STEP Face ID 调用删除 Journal。

## 9. 圆角组件检测逻辑

调用 `defeature_detect_fillet_components` 后：

1. 后端检查当前 Session、Accepted Revision、Job 状态和 Candidate 状态；
2. 若当前 Revision 已有匹配 `schema` 和 `revision_id` 的缓存，默认直接返回；
3. 否则为 STEP 中的面注入：

   ```text
   DSH_STEP_FACE_<Part21Id>
   ```

4. 启动固定 NX 检测 Journal；
5. NX 按支持的曲面类别筛选圆角候选；当前候选包括 Cylinder、Sphere、Torus 和 B-Surface/B-Spline，Plane 和 Cone 不纳入；
6. 按 NX 拓扑共享边建立邻接图并划分连通块；
7. 返回 `fc-001`、`fc-002` 等组件；
8. 后端验证：
   - 每个组件 ID 唯一；
   - 一个 STEP Face 不属于多个组件；
   - 组件面总数与 NX 报告一致；
   - 每个 STEP ID 都能精确映射到当前 Accepted Mesh；
9. 验证通过后写入当前 Revision 缓存。

禁止使用最近质心、Face 枚举顺序、历史组件编号或 NX Tag 代替精确 STEP 身份映射。

## 10. 多组件视觉分析逻辑

调用 `defeature_analyze_fillet_components` 后：

1. Tool 先确认当前模型支持图像输入；
2. 后端读取检测缓存；缓存缺失时会先运行一次组件检测；
3. 将 `component.step_entity_ids` 与 `mesh.faces[].stepEntityId` 严格映射；
4. 为各组件分配稳定且互不相同的颜色；
5. 非组件面显示为灰色；
6. 使用正交投影生成一张 2×2 PNG：
   - Isometric；
   - Front；
   - Right；
   - Top；
7. 图片右侧生成 `颜色 → fc-xxx` 图例；
8. Host Tool 将 PNG 存入 DSH Attachment Service，并以真正的图像 Content Block 返回模型。

视觉模型根据四个视图分析圆角链的连续性、闭环、分支、交汇、孔口或薄壁关系，并推测可能的处理方式。

图片只用于形状推理。组件身份必须来自图例，不能从图片猜测 Face ID。在生产模式中，图片推测不能扩展生产 allowlist；在开发模式中，模型可以把形状判断用于选择已实现的整组件、分段和删除 Tool，但实际目标仍必须来自运行时 Tool 返回的状态绑定引用。

## 11. 删除执行路径

当前存在两条明确隔离的执行路径，不能混用组件 ID 或成功语义。

### 11.1 生产模式：固定 Journal，一次操作产生 Candidate

生产 `defeature-agent` 的单组件和指定批量删除使用相同底层逻辑：

```text
确认 Accepted Revision 且没有 Candidate
→ 检测或读取当前组件缓存
→ 验证所有 fc-xxx 属于当前 Revision
→ 汇总并去重精确 STEP Face ID
→ 创建下一 Revision 目录
→ 给源 STEP 注入 DSH_STEP_FACE 名称
→ 启动固定 NX 删除 Journal
→ 一次 DeleteFaceBuilder.Commit()
→ NX 导出结果 STEP
→ OCC 重导入和三角化验证
→ 发布一个 Candidate
```

NX 参数固定包括：

```text
SelectTypes.FaceEdgeBlend
DeletePartialBlend = False
```

任何一项失败时：

- 不发布 Candidate；
- 输出 Revision 标记为 failed；
- Accepted Revision 不变；
- 错误分类为 infrastructure、validation 或 geometry。

生产单组件 Tool 不在失败后自行切换方法；若用户请求删除全部圆角，应使用第 12 节的一次调用全局算法。

### 11.2 开发模式：常驻 NX 中的完整自主删除循环

`defeature-agent-dev` 可以在一个常驻 `run_journal.exe` 和同一个 NX Session 中执行多次受控删除。完整过程为：

```text
get_context
→ 确认存在 Accepted、Job 可操作且没有 Candidate
→ 可选：analyze_fillet_components 生成 Accepted 基线的彩色四视图
→ reanalyze_current_state
→ 常驻 Worker 只导入 Accepted STEP 一次
→ 返回当前 runtime_state 下的组件引用
→ 模型选择目标和方法
→ try_delete_component 或先 segment 再 try_delete_segment
   ├─ Commit 未抛异常：保留修改，runtime_state + 1
   │  → 所有旧引用失效
   │  → 必须 reanalyze_current_state
   │  → 基于新拓扑继续选择下一目标
   └─ Commit 抛异常：Undo 并验证恢复
      → runtime_state 不变
      → 根据失败类别、形状、拓扑和尝试历史选择新策略
→ 没有更多合理目标或达到预算
→ finish_and_publish 只调用一次
→ NX 只导出最终累计 STEP 一次
→ OCC 重导入和网格化一次
→ 发布且只发布一个 Candidate
→ 等待用户接受或拒绝
```

#### 11.2.1 初始化与两套组件身份

初始彩色四视图来自 Accepted Revision，组件 ID 为 `fc-xxx`。它用于理解模型和安排初始优先级。运行时操作必须先调用：

```text
defeature_reanalyze_current_state
```

该 Tool 返回类似：

```text
rs000000-fc-0001
rs000000-face-1
```

其中 `rs000000` 表示 `runtime_state=0`。Accepted 的 `fc-xxx` 与运行时的 `rs...-fc-...` 不能互换；模型不得自行转换编号。每次成功 Commit 后拓扑都可能改变，因此初始彩色图只能继续作为形状参考，目标身份以最新一次运行时重分析结果为准。

#### 11.2.2 目标选择

模型先利用 `defeature_get_component_topology` 查询候选组件，主要观察：

- `face_count`：组件大小；
- `surface_types`：是否混合了 Cylinder、Sphere、Torus 或 B-Surface；
- `branch_face_refs`：是否有三度及以上分支节点；
- `end_face_refs`：是否为开放链；
- `closed_loop`：是否形成闭环；
- 彩色四视图中与孔口、薄壁、槽底和复杂交汇区域的关系。

通常优先尝试小型、独立、无分支、开放且远离薄壁/复杂交汇的组件；大型网络、多分支、混合曲面、闭环和孔口附近组件延后处理。这是模型的决策启发式，不是后端硬编码排序。

#### 11.2.3 单步删除与成功判定

整组件调用：

```text
defeature_try_delete_component(component_id, method, preference)
```

局部 Segment 调用：

```text
defeature_segment_fillet_component(component_id, segmentation_method)
→ defeature_try_delete_segment(segment_id, method, preference)
```

每次删除都创建 Undo Mark，并只执行一次 `DeleteFaceBuilder.Commit()`。NX API 执行成功标准仍严格限定为：

```text
builder.Commit() 未抛异常
```

Commit 返回后、保留状态前，Tool 会在同一 NX 操作中比较命名面集合与存活面的边数。若出现非目标命名面消失，或某个存活支撑面的边界变化达到明显异常阈值（当前为边数绝对变化至少 8），则返回 `topology_policy_rejected=true` 并 Undo；这不会改写“Commit 是否成功”的含义，而是独立的提交后接受政策。通过政策后才保留 NX 内存修改，不做 Preview，也不做第二次 Commit。`deletion_seconds` 只统计 Commit 本身耗时；STEP/OCC 验证仍统一推迟到最终发布。

#### 11.2.4 成功后的强制重分析

成功返回的关键字段包括：

```text
commit_succeeded = true
retained = true
runtime_state_before = N
runtime_state_after = N + 1
references_invalidated = true
next_required_action = reanalyze_current_state
```

此时所有旧 `component_id`、`segment_id` 和 `face_ref` 都必须视为失效。模型不能继续执行成功前拟定的“下一个旧组件”，而必须重新调用 `defeature_reanalyze_current_state`，从变化后的 NX 拓扑重新决策。

若后续重分析、视觉或拓扑判断发现最新保留删除不可接受，可调用一次 `defeature_rollback_latest_delete`。Worker 只允许按 LIFO 撤销最新检查点；Undo 后验证模型签名，消费该检查点，并把 `runtime_state` 从 N 单调推进到 N+1，避免旧状态编号和引用复活。回退失败会隔离 Runtime。回退成功后必须重新分析并选择不同策略。

### 11.3 删除失败后如何选择新策略

策略切换不是后端自动盲重试。后端只负责执行一个明确动作、在异常时 Undo 并返回结构化结果；模型依据错误类别、组件形状、当前拓扑和本轮尝试历史选择下一动作。

#### 第一步：先判断能否继续

| 返回情况 | 处理方式 |
|---|---|
| `commit_succeeded=false` 且 `rolled_back=true` | 普通可恢复失败；当前 `runtime_state` 和引用仍有效，可以换策略 |
| `topology_policy_rejected=true` | Commit 技术上成功但附带变化过大，已经自动 Undo；不得保留或重复同一策略 |
| identity/mapping/stale reference | 不猜测目标；调用 `reanalyze_current_state` 获取新引用 |
| timeout / `ambiguous` | 不把它当失败，不用新 operation ID 重试；先协调原操作结果 |
| `rollback_failed`、`reset_required` 或恢复签名不一致 | 立即停止几何尝试并重置/隔离 Runtime |
| infrastructure | 立即停止；不能用几何策略掩盖 NX、许可证、进程或队列故障 |
| final validation | 不发布 Candidate，保留 Accepted 不变并报告失败 |

#### 第二步：普通几何失败时选择替代动作

允许的删除方法为：

- `face_edge_blend`：优先用于圆角候选；
- `blend`：尝试 NX 的 Blend 类型识别；
- `generic_face`：前述专用方法失败后的受控通用删面；
- `preference=cliff|notch`：只对相应 Builder 偏好进行切换。

一个典型但非强制的策略梯度为：

```text
face_edge_blend + cliff
→ 若形状是槽底/内凹或 Cliff 失败，尝试 face_edge_blend + notch
→ 尝试 blend
→ 谨慎尝试 generic_face
→ 整组件仍失败时，生成 Segment 并局部删除
→ 无新信息时不重复同一“目标 + 方法 + preference”组合
```

模型应结合形状调整顺序。例如明显内凹沟槽可先用 `notch`；混合曲面组件可较早尝试 `blend` 或 `surface_type` 分段；复杂分支组件应较早分段，而不是对超大整块反复切换相同参数。

#### 第三步：整组件失败时选择局部分段

可用分段方法及用途：

| 分段方法 | 当前行为 | 适用情形 |
|---|---|---|
| `topology_branch` | 移除分支节点后取连通链，并把分支面作为独立 Segment | 多分支圆角网络 |
| `simple_chains` | 移除分支节点后返回各连通链，不单独加入分支面 | 希望先试简单链段 |
| `surface_type` | 按曲面类型划分，并保证每组内部拓扑连通 | Cylinder/Torus/B-Surface 等混合组件 |
| `bisection` | 按 STEP 面身份稳定排序后切为两半 | 大组件失败后的确定性缩小范围；跨 Worker 重导入仍选择同一组 STEP 面，但不保证对应语义圆角链 |

分段 Tool 本身不修改 NX。它只签发状态绑定的 `segment_id`，随后由 `defeature_try_delete_segment` 删除该 Segment 中的完整 NX Faces。当前不支持切开单张 Face，也不允许模型自由拼装任意 Face 集合。

`DeletePartialBlend` 固定为 `False`。所谓“局部删除”只能来自受控 Segment，而不是启用 NX Partial Blend。

当用户明确要求开发模式持续删除圆角时，Agent 应在同一个常驻 NX 会话中自然循环执行“重分析 -> 选择策略 -> 受控删除 -> 成功后继续重分析”，中间不询问用户是否继续，也不发布 Candidate；只有循环结束后才调用 `defeature_finish_and_publish`，导出、验证并发布一个累计 Candidate。用户确认的是最终 Candidate，而不是每一次中间删除。遇到不可恢复的 Runtime、歧义结果、回滚失败或达到合理尝试预算时停止。

#### 第四步：维护本轮尝试历史

模型在同一个 `runtime_state` 内至少应记录：

```text
目标 component_id 或 segment_id
删除 method
cliff/notch preference
Commit 是否抛异常
NX 错误摘要
rolled_back 状态
deletion_seconds
```

同一状态下，不应无信息重复完全相同的动作。以下事件会结束当前尝试历史：

- 某次 Commit 成功：状态前进，旧历史中的引用全部失效，重新分析；
- Runtime 被 reset：重新初始化；
- 最终发布或停止：结束循环。

#### 第五步：停止条件与最终发布

满足任一条件即可停止继续删圆角：

- 重新分析后没有候选组件；
- 剩余组件的所有合理方法/分段均失败；
- 达到调用方设定的尝试预算；
- 遇到 ambiguous、rollback failure 或 infrastructure 错误；
- 继续修改的几何风险高于收益。

只要当前运行时至少有需要交付的保留修改，模型就调用一次 `defeature_finish_and_publish`。它负责最终 STEP 导出、OCC 重导入、网格化和唯一 Candidate 发布。模型不得在每次成功删除后发布，也不得自动接受或拒绝 Candidate。

## 12. 当前全局删除算法

> 本节必须与未来优化计划区分。当前代码仍采用“成功后重新检测”的多轮算法。

Agent 在用户要求删除全部或尽可能多的圆角时，应：

1. 调用 `defeature_get_context`；
2. 只调用一次 `defeature_remove_all_fillets`；
3. 不自行调用检测、单组件或批量删除 Tool 编排二分。

后端只启动一次 `run_journal.exe`。同一 NX 进程内：

```text
导入 STEP 一次
→ 检测当前模型的全部圆角连通块
→ 尝试整批删除
→ 几何失败时按组件列表二分
→ 找到第一个成功批次后保留删除结果
→ 基于变化后的模型重新检测
→ 开始下一轮整批删除和二分
→ 某一轮完全找不到可删批次时结束
→ 导出累计 STEP 一次
```

当前 `attempt_delete` 行为：

- 每次尝试创建短名称 Undo Mark；
- 几何失败时 Undo，并比较模型快照确认回滚完整；
- 成功时保留 Commit，不 Undo；
- 每轮找到第一个成功批次后停止该轮二分并重新检测；
- 直到没有组件，或当前轮没有任何可删除组件。

因此，当前算法可能对拓扑变化后的模型建立多棵二分树，也可能重复尝试困难区域。初始 N 个组件时，尝试次数不受单棵树 `2N-1` 限制。这正是 `docs/global_fillet_deletion_probe_and_batch_plan.md` 计划解决的问题，但尚未修改当前代码。

全局过程结束后：

1. NX 只导出一次累计 STEP；
2. 后端核对成功批次、成功尝试和已删除 STEP ID；
3. OCC 只对最终 STEP 重导入和三角化一次；
4. 成功时发布一个累计 Candidate。

返回状态：

- `succeeded`：至少删除一个批次并生成 Candidate；
- `no_components`：没有检测到候选；
- `no_deletable_components`：检测到组件，但最后一轮找不到可删除批次；
- `aborted`：基础设施、身份或验证失败。

## 13. Agent 的决策逻辑

### 13.1 全局删除请求

例如“删除全部圆角”：

```text
加载 nx-remove-fillets Skill
→ get_context
→ 确认 Accepted 可操作且无 Candidate
→ remove_all_fillets（只调用一次）
→ 汇总返回结果
→ 等待用户审核 Candidate
```

### 13.2 组件分析或指定组件删除

生产 `defeature-agent` 处理“分析所有组件”或“删除 fc-003”时：

```text
加载 nx-delete-fillet-component Skill
→ get_context
→ analyze_fillet_components（需要视觉模型）
→ 结合图片和图例分析组件形状
→ 若用户明确要求删除，则调用 delete_fillet_component
→ 等待用户审核 Candidate
```

开发 `defeature-agent-dev` 若要自主尝试多种方法或局部 Segment，则执行：

```text
加载 nx-delete-fillet-component Skill
→ get_context / analyze_fillet_components
→ reanalyze_current_state
→ 查询 topology
→ 整组件删除或分段后局部删除
→ 失败时依据第 11.3 节切换受控策略
→ 成功后强制重新分析
→ 循环结束后 finish_and_publish 一次
→ 等待用户审核 Candidate
```

生产 Agent 不得把视觉推测出的分支拆分方案冒充为生产 Tool；开发 Agent 只能使用已经注册的受控运行时分段和删除 Tool，不能生成任意 NXOpen。

### 13.3 删除后经验记录与 Skill 更新

开发 Agent 完成一轮受控删除后，应在结束前记录可复用证据。流程为：

```text
读取本轮操作历史、Tool 返回、回滚记录和最终重分析
→ 区分观察事实、工程推断和未验证假设
→ 将完整轨迹写入 Skill reference
→ 仅把有重复性证据支持的规则提炼到相关 SKILL.md
→ 检查生产 Tool allowlist 和生产 preset 未改变
→ 运行 git diff --check 并报告准确变更文件
```

开发 preset 的经验写入范围受限于：

- `.dsh/skills/nx-delete-fillet-component/references/`
- `.dsh/skills/nx-remove-fillets/references/cases/`
- `.dsh/skills/fillet-removal-strategy/` 下的 Skill 内容
- `.dsh/skills/nx-delete-fillet-component/SKILL.md`
- `.dsh/skills/nx-remove-fillets/SKILL.md`
- `docs/`

不得为了记录经验修改生产 preset、生产 Tool allowlist、NX/后端源代码、运行时配置、`.git/` 或全局 `~/.agents/skills/`。历史 face ID、component ID 和 revision ID 只能作为 reference 的审计数据，不能写成未来 Tool 输入。没有足够证据时，应记录失败边界，不得强行提炼通用规则。

## 14. 遗留的选面 Planner 执行链

后端仍保留一条较早的 HTTP 接口：

```text
POST /api/jobs/{job_id}/execute
```

独立的 `webapp/frontend` 会调用它，但当前 DSH `CAD 工作台` 和生产 Defeature Tools 不调用这条接口。

该遗留流程为：

```text
用户提交 Accepted Revision、选中面和自然语言要求
→ 后端根据 Mesh 验证选面
→ 调用单独配置的 OpenAI-compatible Planner
→ Planner 只能从用户所选 faceId 中选择目标
→ 固定 NX Journal 执行删除
→ 失败时把规范化错误反馈给 Planner
→ 最多尝试 STEP_WEB_MAX_DELETE_ATTEMPTS 次
→ 成功后经 OCC 验证并发布 Candidate
```

当前默认最多 6 次，配置会限制在 1～12 次之间。此链路属于遗留兼容功能，不是当前生产 Agent 的组件级或全局去圆角主流程。

另外，遗留 Planner 的 system prompt 仍错误声称 Partial Blend 固定开启，而当前活跃删除 Journal 已固定关闭。不要依据这条旧接口文案判断当前生产 Tool 行为。

## 15. 用户审核流程

删除成功后，CAD 工作台同时显示：

- 左侧 Accepted；
- 右侧 Candidate。

用户可以：

- 下载 Accepted；
- 下载 Candidate；
- 接受 Candidate；
- 拒绝 Candidate；
- 查看 Revision 列表和任务时间线。

Agent 不得替用户自动审核。只有接受后，Candidate 才成为下一次操作的 Accepted 基线。

## 16. 并发与安全边界

- NX Journal 通过进程内锁串行运行，避免多个 NX 操作同时争用运行环境；
- 几何 Tool 被标记为非并发安全；
- `begin_execute` 原子声明当前 Accepted Revision，防止重复操作；
- 一个 Job 只允许一个 Candidate；
- Tool 通过 Session 自动推导 Job 和 Revision；
- Agent 不能提交输入/输出路径、Journal 路径、任意 STEP Face 列表或任意 NX 代码；
- 删除、选面和遗留 Execute 等受控 HTTP 请求体使用 Pydantic `extra="forbid"`；内部 NX 请求同时使用固定 schema；
- STEP 输出必须经过 OCC 重导入并包含有效 BREP Face 后才能发布 Candidate；
- 基础设施错误（NX 启动、许可证、超时等）不会通过切换几何策略掩盖。

## 17. 日志与故障定位

### DSH/FastAPI 启动问题

查看：

```text
http://127.0.0.1:3080/defeature/status
%DSH_HOME%\logs\defeature-backend.log
```

### Job 事件

工作台“任务时间线”来自 SQLite `events` 表，也可通过：

```text
GET /defeature/api/jobs/<job-id>/event-log
```

读取。

### NX 日志

每个检测、单批删除或全局删除操作都会写独立 `.log` 和 JSON 报告。全局操作先写入临时 `operations/remove-all-*`；成功后关键请求、报告和日志会复制到 Candidate Revision。

### 常见失败类别

- `state`：Revision 不可操作或已有 Candidate；
- `infrastructure`：NX runner、许可证、进程、超时或 Undo 完整性问题；
- `geometry`：NX 无法延伸、修补或闭合删除后的模型；
- `validation`：报告不匹配、STEP 身份错误、导出结果无效或 OCC 重导入失败；
- `identity/mapping`：组件或面无法唯一映射到当前 Revision。

## 18. 开发验证命令

后端测试：

```powershell
python -m pytest webapp/backend/tests -q
```

Host Tool 测试：

```powershell
npm --prefix dsh-plugins/defeature-ui run test:tools
```

映射测试：

```powershell
npm --prefix dsh-plugins/defeature-ui run test:mapping
```

重新构建 CAD client plugin：

```powershell
npm --prefix dsh-plugins/defeature-ui run build
```

## 19. 当前已知限制与待改进项

1. **全局删除仍会成功后重新检测。** 因而可能产生多轮二分和较多 NX 几何尝试。
2. **质量优先的新二分方案尚未实施。** “全部探测均回滚、候选归并、动态拓扑批次执行”的设计仅记录于 `docs/global_fillet_deletion_probe_and_batch_plan.md`。
3. **生产模式仍只有一种删除策略。** `face_edge_blend`、`blend`、`generic_face` 和 Segment 局部删除目前只在 `defeature-agent-dev` 开放，尚未晋升到生产 allowlist。
4. **当前运行时候选分类较宽。** 常驻重分析把支持的曲面类型作为候选并按共享边连通，不保证每个组件都是语义上的设计圆角；模型必须结合四视图和拓扑判断。
5. **Segment 以完整 NX Face 为最小粒度。** 不支持切开单张 Face；`bisection` 是确定性缩小集合的方法，不保证形成自然圆角链。
6. **运行时单步成功仅由 Commit 异常决定。** 单步不会进行 OCC 几何质量判断，最终有效性在 `finish_and_publish` 才验证。
7. **独立 `webapp/frontend` 不是当前 DSH GUI 的主入口。** 当前用户界面是 DSH Web 中的 `CAD 工作台` client plugin。

## 20. 当前完整用户流程示例

### 20.1 生产流程

```text
1. 启动 dsh web，打开 http://127.0.0.1:3080
2. 新建 defeature-agent 会话并上传 STEP
3. OCC 生成 r0 Mesh，r0 成为 Accepted
4. 用户提出分析、指定组件删除或全部删除要求
5. Agent 加载对应 Skill 并调用生产 allowlist Tool
6. NX 固定 Journal 执行单次组件/批次删除，或一个进程内完成全局算法
7. 后端验证 NX 报告和最终 STEP
8. OCC 重导入并生成 Candidate Mesh
9. 工作台对比 Accepted 与 Candidate
10. 用户接受或拒绝 Candidate
11. 接受后，新 Revision 成为后续操作基线
```

### 20.2 开发模式常驻 NX 自主流程

```text
1. 重启已加载最新 Plugin/Backend 的 DSH Host
2. 新建 defeature-agent-dev 会话并上传 STEP
3. get_context，确认 Accepted 可操作且无 Candidate
4. analyze_fillet_components，读取彩色四视图
5. reanalyze_current_state，常驻 NX 导入 STEP 并返回 rs... 引用
6. 查询目标 topology，选择整组件方法或分段方法
7. 执行一次 try_delete_component / try_delete_segment
8. Commit 失败且 Undo 成功：记录失败组合并选择新方法或 Segment
9. Commit 成功：保留修改、状态加一、丢弃全部旧引用并重新分析
10. 若用户要求连续删除，不询问用户是否继续；重复 6～9，直到没有合理候选、达到预算或遇到不可恢复错误
11. finish_and_publish 只调用一次
12. NX 导出最终累计 STEP，OCC 重导入并创建一个 Candidate
13. 用户接受或拒绝 Candidate
```
