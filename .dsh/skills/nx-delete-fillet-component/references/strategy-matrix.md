# 组件删除策略矩阵

## 传统生产流程

| 分析分类 | 当前策略 |
|---|---|
| 任意当前 Accepted Revision 组件 | `whole-component-face-edge-blend` |

传统 Tool 成功后生成 Candidate，不进行自主多步修改。

## 常驻 NX 开发流程

| 观察特征 | 优先动作 | 失败后的候选动作 |
|---|---|---|
| 短、独立、无分支的候选圆角链 | `face_edge_blend + cliff` | `notch`，然后 `blend` |
| 明显内凹沟槽或槽底圆角 | `face_edge_blend + notch` | `cliff`，然后分段 |
| NX 可识别的混合圆角组件 | `blend` | `face_edge_blend` 或按类型分段 |
| 非标准曲面或前述策略失败 | `generic_face` | 分段或跳过 |
| 多分支组件 | `topology_branch` / `simple_chains` | 对生成的 segment 逐个尝试 |
| 混合面类型组件 | `surface_type` | 对保持拓扑连通的类型子段尝试 |
| 大型失败组件 | `bisection` | 继续对失败子段拆分 |

这只是决策指导，不是硬编码顺序。模型必须结合四视图、当前拓扑数据和已有尝试结果选择下一动作。

## 状态纪律

- 删除成功后旧引用失效，必须重新分析。
- 删除失败且 Undo 成功时，当前引用仍可用于改换策略。
- 同一策略和相同目标失败后不得无信息地重复尝试。
- 超时结果属于 ambiguous，必须先查询原操作状态。
