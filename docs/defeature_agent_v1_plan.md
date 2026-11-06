# Defeature Agent 第一版实现计划

> **历史计划，已退役。** 本文保留当时的设计背景，不是当前实现、运行指南或待执行计划。
> 根目录 `DeFeature/`、`Dedraft/` 和 `archive/` 已由用户决定删除，文中的相关路径、
> 管线和接口不可作为现行依赖；`Defillet/` 保留，其迁移目标为 `cad/Defillet/`，
> 文中历史路径不改写。当前使用和安装请参阅
> [DSH 插件指南](../dsh-plugins/defeature-ui/README.md)及
> [独立 Web 应用指南](../webapp/README.md)。

## 1. 目标

第一版实现一个由用户指定问题面驱动的半自动 Defeature Agent。

输入为一个完整 STEP 文件和若干 `ADVANCED_FACE` 实体编号，例如 `#123`、`#456`。
系统只分析和处理这些指定面，不扫描或自动处理模型中的其他特征。

处理流程如下：

1. 将 STEP `#实体号` 解析为 OCC Face。
2. 根据共享边邻接、曲面类型和凸凹性将多个输入面拆分为候选特征组。
3. 判断每组属于圆角、拔模、普通问题面或未知类型。
4. 优先调用原管线的规则和 NX 操作完成 Defeature。
5. 规则程序执行失败时，提取目标面及一环局部 BREP；信息不足时可扩展到二环。
6. 将结构化局部 BREP、规则失败原因和预设操作列表发送给大模型。
7. 大模型每次只能选择一个白名单操作。
8. 操作始终在完整 STEP 的副本上执行，局部 BREP 仅用于推理。
9. 每次成功生成候选 STEP 后暂停，由人工接受、拒绝后继续或拒绝后停止。

第一版不自动评价 Defeature 的几何效果。人工评价是唯一的最终验收依据。

## 2. 明确边界

### 2.1 第一版包含

- STEP `ADVANCED_FACE #实体号` 解析。
- 指定面邻接分组。
- 指定面特征分类：`fillet`、`draft`、`ordinary_face`、`unknown`。
- 目标化圆角标记和现有 NX Defillet 管线复用。
- 目标化拔模标记、锚边注入和现有 NX Dedraft 管线复用。
- 普通面精确 Delete Face 操作。
- 一环、二环局部 BREP 结构化提取。
- OpenAI-compatible LLM 规划接口。
- 固定使用 OpenCode 配置中的 `mip-chat/gpt-5.6-sol`，复用其 provider endpoint、认证和请求头。
- 操作白名单和严格结构化输出。
- 一次一个操作的 LLM 尝试。
- 作业状态、操作历史和人工评价持久化。

### 2.2 第一版不包含

- 全模型特征搜索。
- 自动三角化质量评价。
- IoU 或网格指标驱动的自动接受。
- 仿真语义判断。
- 自动判断压缩机中哪些结构可以删除。
- 孔、槽、凸台、肋、倒角的自动 Defeature。
- LLM 生成任意 Python 或 NX journal。
- 多个破坏性操作并行执行。
- 无人工确认的最终结果发布。

## 3. 输入

### 3.1 请求格式

```json
{
  "schema_version": 1,
  "input_step": "model.step",
  "face_entity_ids": [123, 456, 789],
  "max_llm_attempts_per_group": 4
}
```

`face_entity_ids` 对应 STEP 文本中的实体编号：

```step
#123=ADVANCED_FACE('...',(...),#456,.T.);
```

输入时允许写成 `123` 或 `#123`，持久化时统一保存为整数。

### 3.2 编号生命周期

STEP 重写后实体编号可能变化。因此 `#123` 只用于定位原始输入 STEP 中的面。

首次解析后立即保存：

- 原始 STEP 实体号。
- OCC 面遍历序号。
- 曲面类型。
- 几何描述符和指纹。
- 邻接关系。

后续 revision 通过几何描述符匹配，不继续假定原始实体号有效。

## 4. 指定面解析

使用 `STEPControl_Reader` 读取完整 STEP，并通过 transfer reader 建立实体映射：

```text
TopoDS_Face
  -> transfer_reader.EntityFromShapeResult(face, 1)
  -> model.Number(item)
  -> STEP #实体号
```

解析时必须验证：

- 所有输入 ID 都存在。
- 对应实体为 `ADVANCED_FACE`。
- 一个 ID 只映射到一个目标面。
- 输入中没有重复 ID。

任一输入面无法定位时，作业进入 `FAILED`，不执行部分操作。

## 5. 指定面分组

多个指定面可能属于多个独立特征。系统先构建只包含指定面的邻接图：

- 节点是指定面。
- 两个面共享至少一条拓扑边时建立连接。
- 图的每个连通分量形成一个初始候选组。

初始候选组按以下条件进一步拆分：

- 曲面类别冲突，例如平面和圆柱面。
- 凸凹性符号相反。
- 圆角半径明显不同。
- 拔模方向或拔模角不一致。

每个组独立分类、执行和人工评价，不把多个互不相关的特征放进同一个 NX 操作。

## 6. 特征分类

### 6.1 圆角

复用 `Defillet/nx_0_detect_fillet_by_rule.py` 中的单面规则，只判断指定面：

- Cylinder：小半径局部圆柱条带。
- Sphere：排除球冠后的局部球面。
- Torus：小半径且具有候选支撑关系。
- BSplineSurface：曲率采样符合圆角半径特征。

同组内所有指定面均满足圆角规则时分类为 `fillet`。

### 6.2 拔模

复用 `Dedraft/nx_0_detect_draft_by_rule.py` 中的单面规则：

- 面为 Plane。
- 相对 X/Y/Z 标准方向存在配置范围内的小拔模角。
- 可计算后续锚边信息。

同组内所有指定面均满足拔模规则时分类为 `draft`。

### 6.3 普通问题面

不符合圆角或拔模规则，但具有可读取几何和完整邻接关系时，分类为
`ordinary_face`。这表示可以尝试精确普通 Delete Face，不表示该操作一定成功。

### 6.4 未知类型

以下情况分类为 `unknown`：

- 一个组内出现冲突的特征证据。
- 曲面或拓扑信息提取失败。
- 无法安全构造规则操作。

`unknown` 视为规则阶段不可执行，可进入 LLM 规划。

## 7. 规则阶段

### 7.1 圆角

只给当前组中的指定面写入 defillet JSON 名称，再调用现有
`Defillet/nx_1_defillet.py`。

当前实现将 `DeletePartialBlend=False` 固定在后端，不再作为规划参数。允许的差异只来自 `Cliff/Notch` 偏好和安全的目标子集缩小。

关闭全局 residual detect 和 local residual cleanup，避免处理用户未指定的面。

### 7.2 拔模

只给当前组中的指定面写入 `DRAFT` 名称，然后：

1. 调用现有锚边注入逻辑。
2. 调用 `Dedraft/nx_2_dedraft.py` 创建零度 Edge Draft。
3. 优先使用相切边链规则。
4. 相切边链失效时回退单边规则。

### 7.3 普通删面

给当前组中的指定面写入专用名称，使用：

```text
DeleteFaceBuilder.SelectTypes.Face
CreateRuleFaceDumb
```

普通删面操作优先尝试 `Face` 策略，不先使用 `FaceEdgeBlend` 语义。

### 7.4 规则程序失败定义

仅以下情况进入 LLM：

- 输入面或后续目标面无法定位。
- 分类为 `unknown`，无法构造规则操作。
- 目标标签或 Draft anchor 无法生成。
- NX journal 报错或超时。
- NX Builder 提交失败。
- 输出 STEP 不存在、为空或无法读取。
- 输出不再包含有效实体。
- 操作未产生预期变化。

规则操作执行成功且输出有效时进入人工评价。人工拒绝规则结果后停止并保留规则前模型，
不自动转入 LLM。

## 8. 局部 BREP

### 8.1 邻域范围

默认提取一环：

- 目标面。
- 目标面的全部边和顶点。
- 与目标面共享边的邻面。

LLM 返回 `observe_more` 时扩展到二环：

- 一环邻面的其他边。
- 通过这些边连接的额外邻面。

### 8.2 Face 信息

- 本地 ID 和角色：target、neighbor、second_hop。
- 原始 STEP 实体号，如仍可用。
- 曲面类型。
- 面积、质心和包围盒。
- UV 范围。
- 平面法向和偏移。
- 圆柱、球面、圆环参数。
- 曲率摘要和半径估计。
- 边界边列表。
- 邻面列表。

### 8.3 Edge 信息

- 本地 ID。
- 曲线类型。
- 长度、端点、中点和切向。
- 圆弧半径。
- owning faces。
- 与相邻面的连续性和凸凹关系，能够可靠计算时提供。

### 8.4 使用约束

局部 BREP 是 LLM 的只读观测。所有 NX 操作必须作用于完整 STEP 的副本，不能对局部裁剪
STEP 执行正式 Defeature，因为 Delete Face 和 Draft 需要完整支撑几何。

## 9. LLM 操作白名单

### 9.1 圆角

```text
fillet.delete.face_edge_blend.notch.partial_on
fillet.delete.face_edge_blend.notch.partial_off
fillet.delete.face_edge_blend.cliff.partial_on
fillet.delete.face_edge_blend.cliff.partial_off
fillet.delete.plain_face.cliff.partial_on
fillet.delete.plain_face.cliff.partial_off
fillet.delete.subset
```

### 9.2 拔模

```text
draft.zero.tangent_chain
draft.zero.single_edge
draft.zero.smaller_anchor_set
```

### 9.3 普通删面

```text
face.delete.group
face.delete.connected_subset
face.delete.single
```

### 9.4 非破坏性操作

```text
observe.expand_to_two_hops
stop.no_safe_operation
```

LLM 每次只返回一个操作。输出必须通过 JSON Schema 校验，且目标只能引用局部 BREP 中的
target face ID。

## 10. 人工评价

### 10.1 规则结果

- `accept`：将候选结果设为最终结果。
- `reject_and_stop`：停止，保留规则操作前的模型。

规则结果没有 `reject_and_try_llm`。

### 10.2 LLM 结果

- `accept`：将候选结果设为最终结果。
- `reject_and_try_next`：拒绝当前候选，尝试下一项未使用的白名单操作。
- `reject_and_stop`：停止并保留进入 LLM 前的模型。

每次只展示一个候选结果，并记录可选原因和备注。

## 11. 状态机

```text
CREATED
  -> RESOLVING_FACES
  -> GROUPING_FACES
  -> CLASSIFYING_FEATURES
  -> RULE_READY
  -> RULE_RUNNING
  -> WAITING_RULE_REVIEW
  -> ACCEPTED | REJECTED

RULE_RUNNING
  -> RULE_FAILED
  -> EXTRACTING_LOCAL_BREP
  -> LLM_READY
  -> LLM_PLANNING
  -> LLM_ACTION_READY
  -> LLM_ACTION_RUNNING
  -> WAITING_LLM_REVIEW
  -> ACCEPTED | LLM_PLANNING | REJECTED | FAILED
```

## 12. 作业目录

```text
<job>/
|-- agent_job.json
|-- input/
|   `-- original.step
|-- groups/
|   `-- g-001/
|       |-- classification.json
|       |-- local_brep_h1.json
|       `-- local_brep_h2.json
|-- actions/
|   `-- a-0001/
|       |-- request.json
|       |-- result.json
|       |-- stdout.log
|       |-- candidate.step
|       `-- candidate.prt
`-- accepted/
    `-- result.step
```

`agent_job.json` 记录：

- 原始 STEP 路径和副本。
- 输入实体号。
- 解析后的面描述。
- 自动分组和分类。
- 当前状态。
- 当前待评价 action。
- 规则和 LLM 尝试历史。
- 人工评价。
- 最终接受结果。

## 13. 模块划分

```text
DeFeature/agent/
|-- __init__.py
|-- schemas.py
|-- step_face_resolver.py
|-- face_grouping.py
|-- feature_classifier.py
|-- local_brep.py
|-- operation_catalog.py
|-- state_store.py
|-- rule_runner.py
|-- llm_planner.py
|-- action_runner.py
`-- cli.py
```

## 14. 实施阶段

### 阶段 1：输入解析、分组和分类

- 实现 `#ADVANCED_FACE` 解析。
- 生成输入面描述符。
- 构建指定面邻接图并分组。
- 分类为圆角、拔模、普通面或未知。
- 生成作业清单。

### 阶段 2：局部 BREP 和操作目录

- 提取一环和二环局部 BREP。
- 定义稳定本地 ID。
- 定义白名单操作和参数限制。
- 生成供 LLM 使用的结构化上下文。

### 阶段 3：规则执行

- 只给指定组写目标标签。
- 复用现有 Defillet 和 Dedraft 管线。
- 为普通删面增加 plain-face 优先模式。
- 每个组生成独立候选结果。

### 阶段 4：LLM 与人工评价

- 接入 OpenAI-compatible chat completions。
- 强制 JSON 输出和白名单验证。
- 每次只执行一个操作。
- 保存人工接受、继续或停止决定。

## 15. 本轮代码交付范围

本轮先实现代码结构和静态接口，不实际启动 NX 或调用 LLM：

- Agent 数据结构和状态存储。
- STEP 面实体号解析。
- 指定面邻接分组。
- 指定面基础特征分类。
- 一环、二环局部 BREP 提取。
- 操作白名单。
- 目标面标记工具。
- 规则执行器和 LLM 规划器接口。
- CLI：准备作业、查看作业、生成执行计划、记录人工评价。
- 为普通删面增加 plain-face 优先参数。

实际 NX 和 LLM 调用由后续显式命令触发，本轮不运行外部建模或推理服务。
