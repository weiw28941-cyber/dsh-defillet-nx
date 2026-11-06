---
name: nx-remove-fillets
description: 使用 Siemens NX2406 删除 STEP 模型中的全部或尽可能多的圆角，并生成一个供用户审核的累计 Candidate。该任务必须结合 fillet-removal-strategy Skill 进行当前拓扑、耦合关系、删除顺序和失败回退决策；不要只按固定 NX 执行流程调用 Tools。
whenToUse: 当前 DSH 会话已经上传 STEP 模型，且用户要求删除全部或多个圆角时。
disable-model-invocation: false
user-invocable: true
metadata:
  domain: cad
  application: Siemens NX2406
---

# NX Remove Fillets

## 首要策略层

这是全局圆角删除的任务入口，但不是唯一 Skill。开始执行前，必须加载并优先采用 `fillet-removal-strategy`：它负责解释当前 CAD 状态、识别耦合和支撑关系、决定删除单元与顺序，并在状态变化后重新评估假设。`nx-delete-fillet-component` 只负责把当前策略转换为安全的 NX 操作，不得替代策略分析。

策略 Skill 是决策辅助而不是固定脚本。模型应以当前 `reanalyze_current_state`、拓扑证据、视觉观察和已完成/失败 Tool 结果为依据，自主选择组件、Segment、Face Group、方法、顺序、回滚或跳过目标。

## 全局删除

用户要求删除全部或尽可能多的圆角时：

1. 调用 `defeature_get_context`，确认存在 Accepted Revision 且没有待审核 Candidate。
2. 先使用 `fillet-removal-strategy` 建立当前删除假设，再在开发模式下调用 `defeature_reanalyze_current_state` 验证和补充该假设；不要把连续删除委托给单独的自动循环 Tool。
3. 每次成功删除后立即重新分析并继续，不询问用户是否继续；只有最终调用 `defeature_finish_and_publish` 后才等待审核。
4. 生产模式仍使用一次 `defeature_remove_all_fillets`，不要把本开发模式规则套用到生产流程。
5. 不要自行计算组件、面集合、连通关系或二分集合；这些由现有分析和分段 Tools 返回。
6. 没有候选、达到预算、映射/回滚/基础设施失败时停止并报告原因。

圆角识别结果只是初始候选，不是可删除面的白名单。若已识别圆角因漏检或耦合而无法单独删除，应依据当前状态和试删返回的连带影响判断是否组成 Face Group；未识别面可以纳入显式 Face Group，但不得仅因邻近或曲面类型相似就删除。

连续删除中，封口面必须根据当前邻接、几何角色和受控试删重新判断。显式加入封口面与使用 `allow_related_faces=true` 让 NX 自动恢复封口不是等价操作；后者成功后必须检查实际连带消失范围，超出预期立即回滚。若专用 `face_edge_blend` 组合在同一前态失败，可对已确认完整的拓扑叶链受控尝试 `generic_face`，但不得借此扩大到圆锥、薄壁、外轮廓或已确认支撑面。案例参考：`.dsh/skills/nx-delete-fillet-component/references/layered-deletion-case.md`。

开发模式连续删除时，每次修改 NX 前输出简短的可见决策摘要，说明当前拓扑证据、目标和所选策略；Tool 返回后说明成功、回滚或状态变化。不要伪造不存在的分析结果，也不要输出或声称输出完整私有思维链。

## 返回状态

- `succeeded`：至少删除了一个组件，`candidate_revision` 是待审核的累计结果。
- `no_components`：模型中没有检测到可处理的圆角组件。
- `no_deletable_components`：检测到了组件，但没有任何组件能够成功删除。
- `aborted`：基础设施、身份映射或结果验证失败；报告 `failure_category` 和 `error`。

`successful_batches`、`attempts` 和 `rounds` 只用于向用户汇总本次执行结果，不应用来发起后续 Tool 调用。

## 单组件操作

用户明确指定一个 `fc-xxx` 组件时，加载并遵循 `nx-delete-fillet-component` Skill。不要使用全局 Tool 替代指定组件的分析流程。

## 安全边界

- Tool 自动绑定当前 DSH Session。
- Agent 不得构造或提交 Job ID、Revision ID、文件路径、URL、Journal、组件列表或 STEP 面 ID。
- 不得使用历史案例中的面 ID 或组件 ID。
- 不得自动接受或拒绝 Candidate。

## Reference

需要确认 Tool 的输入或返回字段时，读取 [工具接口](references/api-contract.md)。
