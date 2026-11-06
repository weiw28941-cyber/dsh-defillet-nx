# 圆角组件 Tool 合同

## 两套组件身份

- Accepted Revision 分析使用 `fc-xxx`，供传统 `defeature_analyze_fillet_components` 与 `defeature_delete_fillet_component` 使用。
- 常驻 NX 当前状态使用 `rsNNNNNN-fc-NNNN`，只能由 `defeature_reanalyze_current_state` 生成。
- 两套 ID 不得混用。成功的运行时删除会使全部 `rs...` 引用失效。

## `defeature_analyze_fillet_components`

输入 `{}`。对当前 Accepted Revision 生成彩色四视图和图例；不修改模型。

## `defeature_delete_fillet_component`

```json
{
  "component_id": "fc-003",
  "strategy": "whole-component-face-edge-blend",
  "preference": "cliff"
}
```

这是传统单组件交付 Tool：成功时直接生成 Candidate。

## `defeature_reanalyze_current_state`（开发模式）

输入 `{}`。启动或复用常驻 NX，分析当前内存拓扑并返回状态绑定的组件引用。每次成功删除后必须重新调用。

## `defeature_resolve_accepted_component`（开发模式）

```json
{"component_id":"fc-003"}
```

仅在 Runtime state 0 可用。通过 Accepted 组件的完整 `step_entity_ids` 集合与 NX 导入面上的 `DSH_STEP_FACE_<id>` 名称做精确集合匹配，返回可信的 `runtime_component_id`。不得按组件序号、面数、NX Tag 或最近点回退；映射不唯一或不完整时必须失败。

## `defeature_get_component_topology`（开发模式）

```json
{"component_id":"rs000000-fc-0001"}
```

返回组件的 `face_refs`、面类型、分支面、端点面和闭环状态。

## `defeature_segment_fillet_component`（开发模式）

```json
{
  "component_id":"rs000000-fc-0001",
  "method":"topology_branch"
}
```

`method` 可为 `topology_branch`、`simple_chains`、`surface_type` 或 `bisection`。该 Tool 不修改 NX，只生成绑定到当前状态和分段方法的 `segment_id`。

## `defeature_try_delete_component`（开发模式）

```json
{
  "component_id":"rs000000-fc-0001",
  "method":"face_edge_blend",
  "preference":"cliff"
}
```

## `defeature_try_delete_segment`（开发模式）

```json
{
  "segment_id":"rs000000-fc-0001-seg-a1b2c3d4-0001",
  "method":"face_edge_blend",
  "preference":"notch"
}
```

两个删除 Tool 的共同合同：

- `method`: `face_edge_blend`、`blend` 或 `generic_face`。
- `preference`: `cliff` 或 `notch`。
- `DeletePartialBlend` 固定为 `False`，请求合同不接受该字段。
- 局部删除只能通过受控 `segment_id` 完成。
- Commit 后立即比较命名面身份和存活面的边数；非目标面消失或边界变化达到政策阈值时自动 Undo，并返回 `topology_policy_rejected=true`。
- 只有 Commit 未抛异常且拓扑政策通过才保留，`runtime_state + 1`。
- Commit 抛异常或拓扑政策拒绝均自动 Undo，状态不增加。
- 同一 runtime state 下重复相同目标面集合、method 和 preference 会被拒绝。
- `deletion_seconds` 精确记录 Commit 尝试耗时。
- 成功后必须重新分析，禁止继续使用旧引用。
- `rollback_failed` 或 ambiguous 状态不得当作普通几何失败重试。

## `defeature_rollback_latest_delete`（开发模式）

输入 `{}`。只撤销最新一次仍保留的受控删除。成功回退也会推进单调递增的 `runtime_state`，使所有旧引用失效；随后必须重新分析并选择不同策略。

## `defeature_finish_and_publish`（开发模式）

输入 `{}`。将已保留的内存状态导出一次，经 OCC 重导入和网格化后发布一个 Candidate。不得自动接受或拒绝。
