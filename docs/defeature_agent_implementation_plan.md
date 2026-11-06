# Defeature Agent 实现计划

> **历史计划，已退役。** 本文保留当时的设计背景，不是当前实现、运行指南或待执行计划。
> 根目录 `DeFeature/`、`Dedraft/` 和 `archive/` 已由用户决定删除，文中的相关路径、
> 管线、集成点和配置不可作为现行依赖；`Defillet/` 保留。文中原根目录 `Defillet/`、
> `RefineStep/` 和 `OccUtils/` 的当前路径分别为 `cad/Defillet/`、
> `cad/refine_step.py` 和 `cad/OccUtils/`；历史路径不改写。
> 当前使用和安装请参阅
> [DSH 插件指南](../dsh-plugins/defeature-ui/README.md)及
> [独立 Web 应用指南](../webapp/README.md)。

## 1. 目标

在现有 OCC 和 NX 流水线之上构建一个迭代式 Defeature Agent。
Agent 接收 STEP 模型、已知问题面、仿真上下文和网格要求。它首先应用确定性规则，
然后对剩余的局部 BREP 区域进行推理，并从受限的操作目录中选择操作。
每项破坏性操作都会生成新的模型修订版，且只有通过几何和网格验证后才会被接受。

初始用例可由如下需求表示：

> 此模型是用于稳态热仿真的压缩机。移除不会实质影响传热路径的制造细节，
> 但保留接口、边界条件面、壁厚和接触区域。

MVP 支持：

- 圆角检测和移除。
- 拔模检测和移除。
- 精确删除明确指定的面。
- 将 NX Optimize Face 作为辅助操作。
- OCC/trimesh 网格验证。
- 可插拔的外部仿真网格划分器适配器。
- 基于严格工具白名单、兼容 OpenAI 的 LLM 规划。
- 对最终接受修订版进行人工审查。

MVP 中可以报告孔、槽、型腔、凸台、肋和倒角候选项，但在实现专用工具和安全规则之前，
不会自动修改这些候选项。

## 2. 现有能力

实现应复用当前几何操作，而不是在 Agent 内部重新实现。

| 能力 | 现有实现 |
|---|---|
| NX 面优化 | `RefineStep/refine_step.py` |
| OCC 圆角检测和分组 | `Defillet/nx_0_detect_fillet_by_rule.py` |
| NX 圆角面删除 | `Defillet/nx_1_defillet.py` |
| OCC 拔模检测 | `Dedraft/nx_0_detect_draft_by_rule.py` |
| OCC 拔模锚边提取 | `Dedraft/nx_1_inject_edge_info.py` |
| NX 拔模移除 | `Dedraft/nx_2_dedraft.py` |
| 拔模标签迁移 | `Dedraft/nx_1b_transfer_anchor_edge_labels.py` |
| NX 拔模恢复 | `Dedraft/nx_3_autodraft.py` |
| OCC 圆角恢复边推断 | `Defillet/nx_2_occ_detect_sharp_edge_group_inference_support_pairs.py` |
| NX 圆角恢复 | `Defillet/nx_3_autofillet.py` |
| STEP 三角剖分和体素 IoU | `OccUtils/compute_iou_trimesh.py` |
| OCC 布尔体积 IoU | `OccUtils/compute_iou_occ_robust.py` |
| 持久化两阶段作业清单 | `DeFeature/service_pipeline.py` |
| 隔离的 NX 布尔运算工作进程 | `NXBooleanService/` |

`DeFeature/service_pipeline.py` 是首选集成点，因为它已经提供持久化作业、清单、
工作目录和中间产物。在 Agent 达到功能对等之前，`DeFeature/pipeline.py` 中的固定控制流
应继续作为基线保留。

## 3. 设计原则

### 3.1 规则优先，LLM 其次

对高置信度情形使用确定性逻辑：

- 明确提供的问题面。
- 现有 OCC 圆角和拔模规则。
- 现有 NX Delete Face 策略回退机制。
- 现有相切边链和单边 Draft 回退机制。
- 几何有效性和包围盒保护措施。

仅当确定性规则无法安全完成任务时，才使用 LLM 在已注册操作之间做出决策。LLM 可以决定：

- 哪个候选特征最能解释有问题的局部区域。
- 处理一个面、一个连通分量，还是更小的子集。
- 下一步尝试哪个已注册操作及其有界参数。
- 是否需要更多观测。
- 是否已不存在安全的自动操作。

LLM 不得：

- 生成或执行任意 Python 代码或 NX journal。
- 直接访问 NXOpen 对象。
- 调用未注册的 CAD API。
- 绕过验证或受保护区域检查。
- 覆盖已接受的模型修订版。

### 3.2 不可变修订版

每项破坏性操作必须：

- 读取不可变的输入修订版。
- 写入新的候选修订版。
- 持久化其请求、日志、结果和评估。
- 失败时保持已接受修订版不变。
- 分别归类环境、NX API、几何和验证失败。

NX Undo 在单次工具调用内仍然有用，但外部修订版才是权威事务边界。

### 3.3 Sidecar 元数据具有权威性

STEP 实体名称、NX tag 和 JournalIdentifier 是有用的传输和查找机制，但都不够稳定，
无法单独作为身份标识系统。

使用 sidecar JSON 文件作为以下信息的权威存储：

- 稳定的几何指纹。
- STEP 实体标识符和名称。
- NX tag 和 JournalIdentifier 观测值。
- 跨修订版匹配及其置信度。
- 特征候选项和证据。
- 受保护实体。
- 操作和评估历史。

继续支持将 STEP 面和边名称用作 OCC 到 NX 的传输通道。

### 3.4 硬约束先于评分

任何加权评分都不能掩盖硬性失败。候选项在以下情况下必须被拒绝：

- 生成无效 BREP。
- 生成非预期的片体或开放壳体。
- 在禁止改变实体/体数量时改变了该数量。
- 修改受保护实体或边界条件区域。
- 超出配置的体积或包围盒预算。
- 三角剖分失败。
- 新增的非流形或开放网格缺陷超出配置限制。

## 4. 高层架构

```text
Job Request
  STEP + known faces + part context + simulation requirements
    |
    v
Input Normalizer
  all face sources -> stable sidecar identities
    |
    v
BREP Observer
  global summary + local neighborhoods
    |
    v
Deterministic Rule Planner
  known faces, fillets, drafts
    |
    v
Constrained Tool Runner
  isolated OCC/NX operations
    |
    v
Evaluator
  BREP gates + geometric budget + mesh quality
    |
    +---- success criteria met -----------------> Accepted
    |
    +---- unresolved safe candidates ----------> LLM Planner
                                                  |
                                                  v
                                            next whitelisted action
```

LLM 接收有界的局部观测，而不是完整的 STEP 文本或实时 CAD 会话。

## 5. 建议的包布局

```text
DeFeature/agent/
|-- schemas.py
|-- config.py
|-- artifact_store.py
|-- state_machine.py
|-- observation.py
|-- topology_ids.py
|-- feature_candidates.py
|-- operation_catalog.py
|-- rule_planner.py
|-- llm_planner.py
|-- evaluator.py
|-- runner.py
|-- cli.py
|-- adapters/
|   |-- llm_openai.py
|   |-- mesh_base.py
|   |-- mesh_occ_trimesh.py
|   `-- mesh_external_command.py
`-- tools/
    |-- base.py
    |-- refine.py
    |-- fillet.py
    |-- draft.py
    |-- delete_face.py
    `-- validate.py
```

LLM 配置应与提供商无关并兼容 OpenAI：

```text
默认从 OpenCode 配置读取 `mip-chat/gpt-5.6-sol` 的 provider、`baseURL`、认证和请求头。
配置文件按 OpenCode 约定从 `~/.config/opencode/opencode.json[c]` 和项目目录解析。
`DEFEATURE_LLM_BASE_URL`、`DEFEATURE_LLM_API_KEY`、`DEFEATURE_LLM_MODEL` 仅用于显式覆盖；
`DEFEATURE_OPENCODE_CONFIG` 可指定单个 OpenCode 配置文件用于测试或独立部署。
```

未配置 LLM 时，Agent 必须仍能使用确定性规划器运行。

## 6. 作业输入契约

首版 schema 应包含结构化需求和自由格式上下文。

```json
{
  "schema": "defeature_agent_job_v1",
  "input_step": "compressor.step",
  "known_problem_faces": [
    {
      "source": "manual_selection",
      "step_face_name": "problem-face-12",
      "nx_journal_identifier": null,
      "nx_tag": null,
      "geometry_hint": {
        "centroid": [12.3, 5.1, 8.0],
        "surface_type": "cylinder"
      }
    }
  ],
  "part_context": {
    "part_type": "compressor",
    "description": "压缩机壳体",
    "material": null
  },
  "simulation": {
    "physics": ["thermal"],
    "purpose": "稳态热传导",
    "natural_language_requirements": "移除不影响主要传热路径的小型制造细节。",
    "preserve_regions": [],
    "boundary_conditions": [],
    "minimum_feature_size": 1.0,
    "maximum_volume_change_ratio": 0.02
  },
  "mesh_requirements": {
    "adapter": "occ_trimesh",
    "target_size": null,
    "max_iterations": 8,
    "require_watertight": true,
    "allow_body_count_change": false
  },
  "execution": {
    "auto_execute": true,
    "require_final_human_review": true
  }
}
```

已知问题面可以通过以下来源的任意组合提供：

- Sidecar 稳定 ID。
- 前端手动选择。
- STEP `ADVANCED_FACE` 名称。
- NX tag 或 JournalIdentifier。

规划前必须将所有来源规范化到 sidecar 中。NX tag 仅对相应部件和拓扑修订版有效。

## 7. 稳定拓扑标识

### 7.1 面指纹

每个面指纹应在信息可用时包含：

- 体和组件上下文。
- 曲面类型。
- 质心、面积、周长和局部包围盒。
- 主轴或平面法向量。
- 解析参数：
  - 平面法向量和偏移量。
  - 圆柱轴线和半径。
  - 球心和半径。
  - 圆环轴线、大半径和小半径。
- UV 边界。
- 边界线框的数量和类型。
- 边界边指纹多重集。
- 相邻面几何摘要。

### 7.2 边指纹

每个边指纹应包含：

- 曲线类型。
- 长度、端点、中点和中点切向量。
- 适用时的圆弧中心和半径。
- 闭合/开放状态。
- 所属面的指纹。
- 相邻面夹角和凸凹性。

### 7.3 跨修订版匹配

匹配必须返回评分和证据，而不是静默选择最近的实体：

```json
{
  "matched_id": "face:r3:18",
  "score": 0.94,
  "reasons": [
    "surface_type_equal",
    "centroid_distance=0.002",
    "area_ratio=0.998"
  ],
  "ambiguous_candidates": []
}
```

低置信度和一对多匹配必须触发更多观测或人工审查。Agent 不得依赖 STEP 实体顺序作为稳定标识符。

## 8. 局部 BREP 观测

规划器仅接收候选区域及其有界邻域。

```json
{
  "revision_id": "r3",
  "focus_faces": ["face:r3:18"],
  "neighborhood_depth": 2,
  "faces": [],
  "edges": [],
  "vertices": [],
  "relations": {
    "face_adjacent_faces": [],
    "face_boundary_edges": [],
    "edge_owning_faces": []
  },
  "candidate_features": [],
  "protected_entities": [],
  "previous_attempts": [],
  "mesh_feedback": {},
  "simulation_context": {}
}
```

面观测应包含：

- 曲面类型、面积、周长、质心和局部边界。
- UV 边界和法向量采样。
- 主曲率统计和半径估计。
- 边界线框数量和边类型。
- 相邻曲面类型、二面角、凸凹性和相切性。
- 该面是否与受保护区域或边界条件区域相交。

边观测应包含：

- 曲线类型、长度、端点、切向量和半径。
- 所属面以及局部光滑/尖锐分类。
- 凸性或凹性。
- 作为拔模锚边和圆角边界的可能性。

未来可选的观测包括局部 GLB 网格、多个渲染视图、候选项着色和网格质量热力图。
MVP 不应依赖视觉模型。

## 9. 特征候选项模型

基于规则的特征候选项应明确说明证据和置信度：

```json
{
  "candidate_id": "candidate-12",
  "feature_type": "fillet",
  "target_faces": ["face:r2:8", "face:r2:9"],
  "support_faces": ["face:r2:2", "face:r2:3"],
  "confidence": 0.91,
  "evidence": [
    "cylindrical_strip",
    "radius=2.0",
    "u_span<120deg",
    "tangent_to_two_support_faces"
  ],
  "source": "rule"
}
```

MVP 候选项类别：

- `fillet`
- `draft`
- `ordinary_removable_face`
- `unknown`
- `protected_or_functional`

LLM 添加仿真感知处置结论：

- `remove`
- `preserve`
- `needs_review`

对于压缩机热模型，当小圆角和制造拔模不影响主要传热路径时，可以将其移除。
通常必须保护以下内容：

- 流道和密封面。
- 接触和装配接口。
- 热源、对流、温度和热流边界面。
- 会实质改变壁厚或热阻的特征。
- 构成实际导热路径的薄壁和肋。

明确指定的受保护区域始终优先于 LLM 语义推理。

## 10. 操作目录

### 10.1 圆角候选项

| 优先级 | 操作 | 参数 |
|---:|---|---|
| 1 | 删除完整 OCC 分量 | `FaceEdgeBlend`、`Notch`，开启 partial |
| 2 | 删除完整 OCC 分量 | `FaceEdgeBlend`、`Notch`，关闭 partial |
| 3 | 删除完整 OCC 分量 | `FaceEdgeBlend`、`Cliff`，开启 partial |
| 4 | 删除完整 OCC 分量 | `FaceEdgeBlend`、`Cliff`，关闭 partial |
| 5 | 普通删除该分量 | `Face`、`Cliff`，开启 partial |
| 6 | 普通删除该分量 | `Face`、`Cliff`，关闭 partial |
| 7 | 按半径、凸凹性或连通性拆分 | 重试有界策略 |
| 8 | 删除以贪心方式选出的安全子集 | 每次拓扑变化后重新观测 |
| 9 | 停止并请求审查 | 不再自动修改 |

六种核心 Delete Face 策略已存在于 `Defillet/nx_1_defillet.py` 中，
应将其公开为已注册的操作 ID。

### 10.2 拔模候选项

| 优先级 | 操作 | 参数 |
|---:|---|---|
| 1 | 零度 Edge Draft | 相切边规则 |
| 2 | 零度 Edge Draft | 单边简单规则 |
| 3 | 使用更小的锚边集合重试 | 保持方向 |
| 4 | 推迟处理高风险锚边 | 稳定操作优先 |
| 5 | 停止并请求审查 | 不再自动修改 |

保留针对以下情况的现有保护措施：

- 出现非预期的新面。
- 相邻支撑面被消耗。
- 未产生有效几何变化。
- 对不安全结果执行 Undo。

### 10.3 普通问题面

| 优先级 | 操作 | 参数 |
|---:|---|---|
| 1 | 精确删除面集合 | `SelectTypes.Face` 和 `CreateRuleFaceDumb` |
| 2 | 拆分为连通分量 | 优先处理较小分量 |
| 3 | 每次删除一个面 | 每次成功后重新观测 |
| 4 | 优化已接受的删除结果 | 仅在通过初始几何门禁后执行 |
| 5 | 停止并请求审查 | 不再自动修改 |

除非候选项已被重新分类为圆角，否则普通面操作不得静默切换到连通圆角选择。

### 10.4 未知候选项

对于未知候选项，MVP 仅允许非破坏性操作：

- 增加邻域深度。
- 更密集地采样曲率。
- 请求局部三角剖分报告。
- 重新分类。
- 请求人工审查。

不得对未知候选项进行任意 Delete Face 尝试。

## 11. LLM 规划器契约

### 11.1 规划器输入

每个规划请求包括：

- 部件和仿真上下文。
- 当前修订版摘要。
- 局部 BREP 观测。
- 候选特征和规则证据。
- 允许的操作记录。
- 先前尝试和规范化失败原因。
- 局部和全局网格反馈。
- 几何变化预算和剩余迭代次数。
- 受保护实体。

### 11.2 规划器输出

使用通过 JSON Schema 验证的严格结构化输出：

```json
{
  "decision": "execute",
  "candidate_id": "candidate-12",
  "tool": "remove_fillet",
  "operation_id": "fillet.delete.component.notch.partial",
  "target_ids": ["face:r3:18", "face:r3:19"],
  "parameters": {
    "preference": "notch"
  },
  "reason": "局部小半径条带导致较差的网格长宽比，并且不属于热边界。",
  "expected_effect": {
    "reduce_small_triangles": true,
    "preserve_body_count": true
  },
  "stop_if": [
    "protected_region_changed",
    "solid_count_changed"
  ]
}
```

允许的决策：

- `execute`
- `observe_more`
- `accept`
- `needs_review`
- `stop_no_safe_action`

工具、操作 ID、目标集合和参数必须在执行前通过注册表验证。无效的模型输出会被记录，
且绝不会发送至 NX。

## 12. Agent 状态机

```text
CREATED
  -> NORMALIZING_INPUT
  -> OBSERVING
  -> RULE_PLANNING
  -> ACTION_RUNNING
  -> VALIDATING
  -> OBSERVING
  -> LLM_PLANNING
  -> ACTION_RUNNING
  -> VALIDATING
  -> ACCEPTED | NEEDS_REVIEW | FAILED
```

其他运行状态：

- `RETRYING`
- `ROLLING_BACK`
- `CANCELLED`
- `NX_WORKER_FAILED`
- `MESH_ADAPTER_FAILED`

默认有界执行设置：

```text
max_iterations = 8
max_attempts_per_candidate = 4
max_consecutive_no_improvement = 2
max_nx_worker_failures = 2
```

满足以下任一条件时停止：

- 所有硬性质量门禁和成功标准均通过。
- 连续两次迭代均未产生可接受的改进。
- 达到最大迭代次数。
- 不再有尚未尝试的安全操作。
- 所有未解决实体均受保护。
- NX 或网格基础设施反复失败。
- 规划器请求人工审查。

## 13. 修订版和清单 Schema

将 `defeature_job.json` 升级到 schema 版本 2，同时保留当前服务清单作为迁移来源。

```json
{
  "schema_version": 2,
  "job_id": "compressor-001",
  "state": "VALIDATING",
  "current_revision": "r3",
  "accepted_revision": "r2",
  "revisions": {
    "r0": {
      "parent": null,
      "step": "revisions/r0/model.step",
      "checksum": "...",
      "observation": "revisions/r0/observation.json"
    },
    "r1": {
      "parent": "r0",
      "action_id": "a-0001",
      "step": "revisions/r1/model.step",
      "checksum": "..."
    }
  },
  "actions": [],
  "evaluations": [],
  "simulation_context": {},
  "last_error": null
}
```

每项操作都有自己的目录：

```text
actions/a-0003/
|-- request.json
|-- result.json
|-- stdout.log
|-- stderr.log
|-- input.step
|-- output.step
|-- output.prt
`-- evaluation.json
```

产物写入应具备原子性。清单应包含校验和、运行时版本、NX runner 路径、NX build、
OCC 版本、工具版本、LLM 模型和相关配置快照。

## 14. 验证框架

理想的三角剖分并不等同于最大 IoU。去特征操作预期会改变目标区域，
因此与输入完全相同的模型不一定是最佳结果。

验证分为四层。

### 14.1 BREP 硬性门禁

在以下情况下立即拒绝候选项：

- STEP 无法读取或不包含形状。
- `BRepCheck_Analyzer` 报告形状无效。
- 必须保留的实体变成片体或开放壳体。
- 体/实体数量发生非预期变化。
- 全局边界变化超出配置预算。
- 受保护实体无法重新匹配，或其变化超出容差。
- 请求的操作未产生预期几何变化。

### 14.2 几何变化预算

记录并比较：

- 体积和表面积变化。
- 包围盒变化。
- 全局 IoU。
- 局部目标区域差异。
- 非目标区域差异。
- 面和边数量。
- 最小局部特征尺寸。

全局 IoU 是保真约束，而非唯一目标。非目标区域和受保护区域需要比预期目标区域更严格的容差。

### 14.3 通用三角网格质量

初始 OCC/trimesh 适配器应报告：

- 三角剖分是否成功。
- 三角形和顶点数量。
- 水密状态。
- 连通分量数量。
- 非流形边数量。
- 边界边数量。
- 退化三角形数量。
- 重复面数量。
- 绕序或法向量不一致情况。
- 最小和最大角度分布。
- 纵横比分位数。
- 三角形面积分位数。
- 最短边分位数。
- 极小三角形数量。
- 可用时的自相交数量。

优先考虑相对于父修订版的改进：

- 极小三角形更少。
- 高纵横比三角形更少。
- 非流形或开放边界没有增加。
- 三角剖分成功且稳定。
- 模型尺度或实体结构没有非预期变化。

### 14.4 外部仿真网格划分器适配器

在选择具体网格划分器之前定义集成契约：

```python
class MeshEvaluatorAdapter:
    def prepare(self, revision, requirements): ...
    def run(self, revision, requirements): ...
    def parse_report(self, artifacts): ...
    def compare(self, parent_report, candidate_report): ...
```

未来的实现可以面向 NX/Simcenter 3D、ANSYS Meshing、HyperMesh 或其他命令行/API
网格划分器。每个适配器都返回通用报告：

```json
{
  "success": true,
  "element_count": 124000,
  "failed_region_count": 0,
  "quality_metrics": {},
  "problem_regions": [],
  "artifacts": {}
}
```

### 14.5 最终人工审查

MVP 要求进行最终人工审查。审查包应展示：

- 原始模型和已接受模型。
- 已移除、已保留和未解决区域。
- 每项操作及其原因。
- 局部操作前后视图。
- 几何和网格指标。
- 存在网格质量问题的区域。
- LLM 请求和结构化响应。

## 15. 候选项接受判定

只有通过所有硬性门禁的候选项才会被评分。初始相对评分可以是：

```text
score =
    0.35 * mesh_quality_improvement
  + 0.25 * problem_region_resolution
  + 0.15 * complexity_reduction
  + 0.15 * simulation_relevance
  + 0.10 * geometric_preservation
```

定义：

- `mesh_quality_improvement`：相对于已接受父修订版的改进。
- `problem_region_resolution`：已知问题区域是否消失或得到改善。
- `complexity_reduction`：面、边和尺寸过小特征的减少程度。
- `simulation_relevance`：与结构化仿真需求的一致程度。
- `geometric_preservation`：目标区域之外的几何保留程度。

无论评分如何，硬约束始终拥有否决权。

## 16. 执行流程

### 16.1 确定性规则阶段

```text
1. Import and refine the original STEP.
2. Normalize all known problematic faces.
3. Build local BREP observations for known faces.
4. Apply a high-confidence feature rule where possible.
5. Use exact plain-face deletion only when explicitly allowed.
6. Detect high-confidence fillet components globally.
7. Process non-protected fillet components one at a time.
8. Detect high-confidence draft components globally.
9. Process non-protected draft components one at a time.
10. Evaluate each action and retain only accepted revisions.
11. Re-observe unresolved regions.
12. Enter LLM planning.
```

与当前固定流水线不同，每个修订版只处理一个分量。这样可以明确归因失败，
并使网格质量比较可控。

### 16.2 LLM 迭代

```python
while iteration < max_iterations:
    observation = observe(accepted_revision, unresolved_regions)
    candidates = classify_features(observation)
    operations = operation_catalog.for_candidates(candidates)

    decision = llm.plan(
        simulation_context,
        observation,
        candidates,
        operations,
        previous_attempts,
        evaluation_history,
    )

    validate_decision_schema(decision)
    enforce_protected_entities(decision)
    enforce_operation_whitelist(decision)

    candidate_revision = execute(decision)
    evaluation = evaluate(candidate_revision)

    if evaluation.hard_gate_failed:
        reject(candidate_revision)
        continue

    if evaluation.better_than(accepted_revision):
        accept(candidate_revision)
    else:
        reject(candidate_revision)

    if success_criteria_met:
        break
```

每次迭代仅执行一项破坏性操作。

## 17. 工具安全与隔离

### 17.1 MVP 工具白名单

- `refine_model`
- `remove_fillet_component`
- `remove_draft_component`
- `delete_exact_faces`
- `observe_more`
- `triangulate_and_evaluate`
- `stop_and_request_review`

### 17.2 参数边界

强制执行以下配置限制：

- 目标面数量。
- 最大分量大小。
- 可移除圆角的最大半径。
- 最大拔模角度。
- 最大体积和包围盒变化。
- 切向扩展边的最大数量。
- 每个候选项的尝试次数。

### 17.3 NX 隔离

遵循现有 NX Boolean Service 进程隔离模型：

- 一个 NX Session 每次处理一项操作。
- 绝不在同一个 NX Session 中并发建模。
- 超时或进程失败后终止并重建工作进程。
- 不允许工作进程失败改变已接受修订版。
- 为每项操作持久化完整的 stdout 和 stderr。

## 18. 交付阶段

### 阶段 0：建立基线

任务：

- 锁定 NX 和 OCC 运行时版本。
- 选择具有代表性的圆角、拔模和普通面用例。
- 记录当前流水线的成功情况和质量指标。
- 记录 STEP 名称、NX JID 和 PRT 兼容性假设。
- 在每份基线报告中包含实际 NX runner/build。

验收标准：

- 基线数据集可重复运行。
- 每个阶段都生成可追踪的产物和日志。
- 当前成功/失败行为已有文档记录。

### 阶段 1：作业、修订版和操作基础设施

任务：

- 添加清单 schema v2。
- 实现不可变修订版和产物校验和。
- 定义操作请求/结果 schema。
- 持久化操作日志。
- 实现状态转换以及修订版接受/拒绝机制。

验收标准：

- 已停止的作业可以恢复。
- 失败的操作无法覆盖已接受修订版。
- 每项操作都可以根据存储的输入和配置复现。

### 阶段 2：问题面规范化和 BREP 观测

任务：

- 定义 sidecar schema。
- 解析 STEP 名称、NX tag/JID 和前端选择。
- 实现面和边的几何指纹。
- 实现带歧义报告的跨修订版匹配。
- 构建有界局部 BREP 图。
- 定义特征候选项记录。

验收标准：

- 每个提供的问题面都能在修订版 `r0` 中定位。
- 拓扑变更操作后，保留下来的实体能够重新匹配。
- 对歧义匹配进行报告，而不是静默解决。

### 阶段 3：工具注册表

任务：

- 定义通用 `ToolRequest` 和 `ToolResult` 契约。
- 规范化超时和错误类别。
- 封装精化、圆角、拔模、精确删面和验证工具。
- 持久化工具版本和环境详情。

验收标准：

- 每个工具都可通过结构化 JSON 独立调用。
- 环境、API、建模和验证失败可以区分。
- 启动 NX 前验证目标和参数。

### 阶段 4：评估器

任务：

- 实现 BREP 有效性检查和模型统计。
- 添加受保护区域比较。
- 实现 OCC/trimesh 质量报告。
- 添加局部问题区域反馈。
- 定义外部网格划分器适配器接口。

验收标准：

- 拒绝无效 STEP、片体转换、体丢失和受保护区域变化。
- 可以相对于已接受父修订版衡量候选网格的改进。
- 对同一修订版的重复评估结果稳定。

### 阶段 5：确定性规则规划器

任务：

- 优先处理已知问题面。
- 实现圆角、拔模和精确面操作目录。
- 拆分分量并避免重复尝试。
- 添加有界重试和停止条件。

验收标准：

- 当前生产场景可通过新状态机运行。
- 在测试集上，结果不劣于固定流水线基线。
- 每个已接受修订版都有明确的评估依据。

### 阶段 6：兼容 OpenAI 的 LLM 规划器

任务：

- 实现 prompt 构造和严格的 JSON Schema 输出。
- 验证操作 ID、目标和参数。
- 记录 prompt、响应、token、延迟和模型标识。
- 处理无效输出和暂时性 API 故障。
- LLM 不可用时回退到确定性规划。

验收标准：

- LLM 无法调用未注册的工具或参数。
- 无效输出绝不会到达 NX。
- 可以根据持久化产物重放规划决策。
- 没有 LLM 时系统仍可运行。

### 阶段 7：仿真语义和审查工作流

任务：

- 实现结构化仿真上下文和受保护区域。
- 对边界条件面集合建模。
- 添加仿真感知特征策略。
- 添加审查报告和前端兼容的审查数据。
- 集成首个具体的外部网格划分器。

验收标准：

- 受保护接口和边界条件面不能被自动删除。
- 用户可以检查并接受/拒绝每项修改。
- 拒绝后作业返回先前已接受修订版。

## 19. 测试策略

### 19.1 单元测试

- Schema 验证。
- 操作白名单和参数边界。
- 状态转换和修订版父子关系。
- 几何指纹相似度和歧义处理。
- 拒绝无效 LLM 输出。
- 硬性验证门禁。
- 候选项评分和停止条件。

### 19.2 集成测试

- 一个凸圆角。
- 一个凹圆角。
- 多面和多半径圆角。
- 一个拔模面。
- 相切的拔模锚边链。
- 一个可安全移除的普通面。
- 一个 NX 无法安全修复的普通面。
- 多体和装配体 STEP 文件。
- 缺少 STEP 实体名称。
- 拓扑变化后 NX JID 无效。
- NX 超时或进程失败。
- 三角剖分失败。
- 尝试执行与受保护区域相交的操作。

### 19.3 回归指标

- 工具和端到端成功率。
- 有效 BREP 和实体保留率。
- 已知问题解决率。
- 三角剖分成功率和网格质量改善率。
- 非目标几何偏差。
- 每个用例的平均 NX 操作次数和 LLM 迭代次数。
- 人工审查接受率。
- 运行时间和 LLM token 成本。

## 20. MVP 完成标准

MVP 满足以下条件时即可进行受控评估：

1. 接受 STEP 输入。
2. 将全部四类问题面来源规范化到 sidecar 中。
3. 支持移除圆角、拔模和指定普通面。
4. 通过已注册工具执行所有破坏性变更。
5. 为每项操作创建不可变修订版。
6. 自动拒绝失败的候选项并回滚。
7. 检查 BREP 有效性、体/实体数量、体积和边界。
8. 生成 OCC/trimesh 三角剖分和网格质量报告。
9. 运行次数不超过配置的迭代预算。
10. 仅使用兼容 OpenAI 的 LLM 进行受限操作选择。
11. 防止 LLM 绕过受保护实体或参数边界。
12. 生成完整操作历史和最终人工审查包。

## 21. 主要风险

### 21.1 拓扑标识漂移

Delete Face、Draft、Optimize Face 和 STEP 往返转换都会改变拓扑标识符。
因此需要几何指纹和显式歧义处理。

### 21.2 代理网格指标可能与仿真需求不一致

三角形数量更少或形状更好，并不一定意味着热、结构或 CFD 模型更好。
在集成实际仿真网格划分器之前，OCC/trimesh 指标只是第一阶段代理指标。

### 21.3 功能特征误分类

小特征仍可能是热边界、密封面、接触接口或实际的薄壁导热路径。
明确指定的受保护区域必须优先于特征尺寸和 LLM 推理。

### 21.4 OCC 与 NX 的容差差异

OCC 可能识别出看似有效、但 NX/Parasolid 无法修复的候选项。应保留现有策略回退、
Undo 保护和外部修订版回滚机制。

### 21.5 局部改进可能导致全局劣化

移除一个局部特征可能在其他位置产生狭长面或劣质三角形。
每项操作都需要局部和全局验证。

### 21.6 目标定义不正确

仅最大化 IoU 会倾向于不执行任何操作。目标必须综合考虑仿真约束、已知问题解决、
网格改善、复杂度降低和受控几何变化。

## 22. 建议的首个开发切片

在添加更多特征类别之前，先实现最小完整闭环：

```text
STEP + problem-face sidecar + simulation context
  -> NX Refine
  -> local BREP observation
  -> deterministic rule planner
  -> one fillet/draft/plain-face tool action
  -> BREP validation
  -> OCC/trimesh quality evaluation
  -> accept or reject revision
  -> constrained LLM planner for remaining candidates
  -> repeat up to eight iterations
  -> accepted STEP + manifest + review report
```

明确推迟以下事项：

- 自动移除孔、槽、型腔、凸台、肋和倒角。
- 由 LLM 生成任意代码或 journal。
- 并行执行破坏性操作。
- 在一个 NX Session 中并发建模。
- 完整物理仿真和自动修改边界条件。
- 未经人工审查即发布到生产环境。

此顺序保留项目中经过验证的 OCC/NX 几何操作，并将初期工程工作集中在缺失的 Agent
基础能力上：稳定观测、受限工具、持久化状态和客观验证闭环。
