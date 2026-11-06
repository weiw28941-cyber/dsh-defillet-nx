---
name: nx-delete-fillet-component
description: 通过多视角彩色图像与常驻 NX 当前拓扑分析 Siemens NX STEP 模型中的圆角连通块，并在开发模式下将 fillet-removal-strategy 的拓扑决策转换为整组件、分段、Face Group 和受控删除操作。不要把本 Skill 当作独立的固定删除顺序。
whenToUse: 用户要求分析圆角连通块、推测或自主选择删除顺序与方法，或删除指定圆角组件时。
disable-model-invocation: false
user-invocable: true
metadata:
  domain: cad
  operation: fillet-component-analysis-and-controlled-delete
---

# NX Analyze And Delete Fillet Components

## 与策略 Skill 协作

本 Skill 是 NX 执行适配层，不是最高层的删除规划器。涉及多个圆角、耦合关系、删除顺序、blend-on-blend、cliff、支撑面或失败回退时，必须优先加载并参考 `fillet-removal-strategy`。模型应先形成当前状态下的删除假设，再选择本 Skill 提供的受控 Tool；如果当前 CAD 状态与启发式不一致，以当前状态证据和受控试删结果更新策略。

## 先判断可用能力

1. 调用 `defeature_get_context`，确认存在 Accepted Revision 且没有待审核 Candidate。
2. 若当前环境只提供传统 Tool，则遵循“传统单组件流程”。
3. 若提供 `defeature_reanalyze_current_state`、`defeature_try_delete_component` 等开发 Tool，则可执行“常驻 NX 自主流程”。
4. 用户明确要求持续删除时，不要每次成功后询问是否继续；在同一个 Runtime 中重新分析并继续使用现有受控删除 Tools，直到没有合理目标或遇到停止条件。
5. 不得调用不存在的 Tool，也不得用动态 Python 模拟缺失的生产能力。

圆角组件和 Segment 是初始候选的受控分组，不覆盖所有可能相关的面。未被圆角分类的当前面仍可通过 `defeature_try_delete_face_group` 纳入显式删除单元，但必须有拓扑、支撑/封口关系或受控试删结果支持；不得把普通曲面自动当作可删除面。

## 可见决策摘要

在每次修改 NX 的 Tool 调用前，用一两句话向用户说明当前观察到的拓扑证据、选择的目标和本次策略。删除成功或失败后，先简要说明状态变化和下一步判断，再继续调用 Tool。这里要求的是面向用户的决策摘要，不是暴露模型的完整内部思维链。

## 视觉观察

1. 调用一次 `defeature_analyze_fillet_components`，获取全部组件的彩色等轴测、正视、右视和顶视图。
2. 必须结合四个视图与颜色图例逐个判断组件形状，不得把颜色相近的组件合并。
3. 分析应描述圆角链的形状、连续性、闭环、分支、交汇、孔口或薄壁关系，并据此推测删除顺序和方法。
4. 图片只用于形状推理；组件身份必须来自 Tool 返回的 `component_id`，不得从图像猜测面 ID。

## 传统单组件流程

当开发 Tool 不可用时：

1. 只使用当前 Accepted Revision 图例中的 `fc-xxx`。
2. 用户明确要求删除时，调用 `defeature_delete_fillet_component`。
3. 当前只允许 `whole-component-face-edge-blend`。
4. 成功只表示产生 Candidate；不得自动接受或拒绝。

## 常驻 NX 自主流程（仅开发模式）

### 初始化观察

1. 调用 `defeature_reanalyze_current_state`。
2. 若目标来自 Accepted Revision 彩色图例（`fc-xxx`），必须调用 `defeature_resolve_accepted_component`，通过精确 STEP 面身份取得 state-0 的 `runtime_component_id`；严禁根据相同序号推断 `fc-003 == rs000000-fc-0003`。
3. 使用 Tool 返回的状态绑定引用，例如 `rs000000-fc-0001`；不得把 Accepted Revision 的 `fc-xxx` 直接传给运行时 Tool。
4. 结合彩色四视图和运行时组件的 `face_count`、`surface_types`、端点、分支与闭环信息选择目标。
5. `runtime_state > 0` 后不再映射到 Accepted 组件；只基于每次重新分析返回的当前拓扑继续操作。
6. `defeature_reanalyze_current_state` 返回当前状态的所有 `face_ref`，不只返回圆角候选。使用未分类面前，先确认其与目标的耦合证据和保护区域风险。

### 选择删除顺序

优先考虑：

- 面数少、独立、无分支的组件；
- 简单开放链；
- 与孔口、薄壁和复杂交汇区距离较远的组件；
- 之前相似结构成功率较高的组件。

延后处理：

- 大型连通网络；
- 多分支或复杂闭环；
- 混合曲面组件；
- 与孔口、薄壁或其他圆角高度耦合的组件。

### 选择删除方法

调用 `defeature_try_delete_component` 或 `defeature_try_delete_segment`，方法限于：

- `face_edge_blend`：优先用于候选圆角；可选 `cliff` 或 `notch`。
- `blend`：NX Blend 类型删除。
- `generic_face`：上述方式失败后的受控通用删面尝试。

`DeletePartialBlend` 被固定关闭，不是 Agent 可选参数。需要局部删除时，必须先用 `defeature_segment_fillet_component` 生成受控子段，再调用 `defeature_try_delete_segment`。

推荐尝试顺序不是硬编码规则，模型应结合形状判断；默认可从 `face_edge_blend + cliff` 开始，沟槽/内凹形态可优先考虑 `notch`。

### 失败后的处理

若 `builder.Commit()` 抛异常，Tool 会自动 Undo：

1. 确认 `rolled_back=true`；若 `rollback_failed` 或 `reset_required=true`，立即停止并重置 Runtime。
2. Commit 后若发现非目标命名面消失，或存活支撑面的边界变化超过策略阈值，Tool 会返回 `topology_policy_rejected=true` 并在同一操作内 Undo；必须把它视为失败，不能保留。
3. 普通几何失败时，可以切换 `cliff/notch`、切换 `blend/generic_face`，或调用 `defeature_segment_fillet_component`。
4. 同一 runtime state 下，禁止重复完全相同的“目标面集合 + method + preference”；后端也会拒绝重复失败策略。
5. 分段方法包括 `topology_branch`、`simple_chains`、`surface_type` 和 `bisection`。
6. 获得 `segment_id` 后，调用 `defeature_try_delete_segment`。
7. 不得用裸 NX Tag、最近点或遍历序号构造替代目标。
8. 若 `topology_policy_rejected=true`，检查 `topology_change.collateral_disappeared_faces`。这些面是待分析的耦合候选；确认后可重新分析并调用 `defeature_try_delete_face_group`，而不是反复重试原单面策略。

对 `collateral_disappeared_faces` 先核对每个面的几何角色和保护风险。拓扑策略拒绝只说明当前动作不安全，不授权牺牲相关面。若 `simple_chains` 子段试删仅连带消失两端球面角点，且当前邻接确认它们属于同一 junction，可重分析后将角点与子段组成显式 Face Group，先保持 `allow_related_faces=false` 核对精确消失范围；成功后重分析可能分离出新短链。不得仅凭球面类型或邻近关系套用。封口面显式加入 Face Group 与使用 `allow_related_faces=true` 让 NX 自动恢复封口不是等价方案；后者成功后必须核对实际连带范围，若超出预期立即调用回滚。删除后还要重新判断存活平面是否已成为主体支撑面。

### Face Group 删除

`defeature_try_delete_face_group` 是开发模式下处理识别漏检和耦合删除的受控入口。它可以接收当前状态中任意面类型的 `face_ref`，因此 Face Group 可以混合已识别圆角面和明确相关的未分类面。它不接受任意 STEP ID、NX Tag 或旧状态引用。

使用前应说明：

- 每个成员与删除目标的关系；
- 为什么该成员不是孔壁、凸台、薄壁、外轮廓或保护面；
- 预期消失面范围以及是否需要 `allow_related_faces=true`。

`allow_related_faces=true` 只放宽预期的非目标关联面消失检查，不会放宽 Commit 异常、严重存活面边界变化、身份映射、过期引用或回滚失败保护。

`allow_related_faces` 不是失败重试的去重维度。相同当前目标集合、方法和 preference 已失败时，仅切换该布尔值不得无信息重复；应改用更小的完整叶链、不同方法、不同顺序或分段。对混合非标准曲面，若专用方法在同一前态失败，可在拓扑完整的叶链上受控尝试 `generic_face`，但不得因此把保护面并入目标。

### 成功后的处理

若 `commit_succeeded=true`：

1. 修改已经保留，不要再次 Commit。
2. `runtime_state` 增加，所有旧 `component_id`、`segment_id` 和 `face_ref` 立即失效。
3. 必须调用 `defeature_reanalyze_current_state` 获取新引用，然后再决定下一步。
4. 不得继续使用成功操作前制定的组件编号列表。
5. 若后续重分析或几何判断发现刚才保留的删除不可接受，立即调用 `defeature_rollback_latest_delete`；它只允许撤销最新一次保留删除，并在成功后再次使所有引用失效。
6. 回退后必须重新分析，并选择不同的目标、方法、preference 或更小 Segment；不得重复被回退的删除策略。

### 完成交付

当用户的连续删除目标已经完成、没有更多合理动作或达到尝试预算时，调用一次 `defeature_finish_and_publish`：

- 从当前 NX 内存状态导出一次 STEP；
- OCC 重导入并网格化；
- 只生成一个 Candidate；
- 不得自动接受或拒绝 Candidate。

若用户只指定单个组件，删除成功后可以结束并发布；若用户要求删除全部、尽可能多或持续删除，则删除成功后直接重新分析，不等待用户确认。用户确认的是最终 Candidate，而不是每一次中间删除。

## 成功判定

NX API 层面的成功仍定义为：

```text
DeleteFaceBuilder.Commit() 未抛异常
```

受控 Tool 在 Commit 后还执行拓扑政策检查。若非目标命名面消失或支撑面边界发生明显异常变化，则把该动作标记为政策拒绝并自动 Undo，因此不会作为可保留成功返回。`deletion_seconds` 只统计 Commit 尝试；最终 STEP/OCC 验证仍属于 `defeature_finish_and_publish`。

## 失败规则

- NX Commit 几何异常：Undo 成功后允许选择其他受控方法。
- identity/mapping/stale reference：重新分析；禁止任何猜测或 Tag 回退。
- ambiguous timeout：查询原 operation，不得使用新 ID 重试修改。
- rollback failure：立即停止并重置 Runtime。
- infrastructure：立即停止。
- final validation：不得发布 Candidate。

## 安全边界

生产 Agent 仍只使用成熟的传统 Tool。常驻 NX 单步自主 Tools 仅在 `defeature-agent-dev` 中开放。Agent 只能提交 Tool 返回的状态绑定引用和允许的枚举参数，不能提交文件路径、Journal、动态代码或裸 NX Tag。

## References

- [策略矩阵](references/strategy-matrix.md)
- [验证与回滚](references/validation.md)
- [Tool 合同](references/api-contract.md)
- [分层删除案例](references/layered-deletion-case.md)
