---
name: fillet-removal-strategy
description: >
  为 STEP/B-rep/CAD 模型制定圆角（fillet/blend/round）删除策略。用户要求判断哪些圆角应一起删除、
  分析耦合关系、决定删除顺序、规划 NX/OCCT defeaturing、处理圆角链或删除失败时使用。
  适用于无历史或历史不可靠的 B-rep，也适用于 NX journal 的组件分组、顺序选择和事务式试删。
  对“删除全部、尽可能多或持续删除圆角”的任务必须优先加载，即使用户没有明确要求制定策略；它是执行 Skill 的决策层，不是可选的补充说明。
---

# 圆角删除策略

本 Skill 只保留执行决策。论文依据、案例说明和扩展背景见
`references/strategy-background.md`。

## 术语定义

- **圆角面（fillet/blend face）**：替代原始锐边的过渡面，通常与两侧支撑面相切。
- **支撑面（support face）**：圆角滚动球接触的相邻面。删除圆角后，通常需要用它们恢复原始锐边或局部面。
- **Spring edge**：圆角面与支撑面相切连接的边，也叫接触边。普通两侧圆角通常有两组 spring edges。
- **Cross edge**：同一条连续圆角带中，连接相邻圆角面的边。它通常不是圆角删除后要保留的设计锐边，而是链内部的连接边。
- **Leaf chain**：沿 cross edge 连续的一组圆角面，直到 sharp edge 或 vertex blend 处终止的最小连续圆角单元。它是默认的最小删除候选，不是单个 face。
- **Blend chain**：由多个 leaf chain 依据几何连续性或拓扑关系组合成的圆角链。
- **Vertex blend**：多个 edge blend 在一个顶点汇合的中心过渡面，可能是球面或多边界面。
- **Blend-on-blend**：一个圆角建立在另一个圆角面上，后者的 blend face 充当前者的支撑面。
- **Cliff / face-edge blend**：一侧连接支撑面、另一侧连接边或边界的特殊圆角，不能按普通两支撑面圆角处理。
- **Component**：在当前 CAD 内核和删除器中可以作为一个局部事务尝试的圆角集合。
- **Creator operation**：历史中由同一次 Fillet 命令生成的面集合。它不一定等于几何 chain 或最优删除单元。
- **删除单元（suppress unit）**：当前删除器实际提交的 face、leaf chain 或 component。

### 候选识别不是删除白名单

当前 NX 曲面分类主要提供初始圆角候选，可能漏掉真实圆角，也可能把孔壁、凸台或其他设计曲面标为候选。候选组件不是完整的可删除面集合，也不是未识别面的永久保护名单。最终删除单元应由当前 runtime 状态、拓扑邻接、支撑/封口关系、视觉证据和受控试删结果共同决定。

未被圆角分类但有充分证据属于同一删除单元的面，可以作为 `Face Group` 成员，例如必要的支撑、trace、封口或上层/下层 blend 依赖面。普通孔壁、凸台、薄壁、外轮廓和明确保护面在没有新证据时应保留。

必须区分：

```text
几何连通性 != Creator operation != 删除单元
```

## 分组规则

### 默认必须整体处理

优先将以下对象作为一个候选动作或一个局部耦合网络：

1. 沿 cross edge 连续的一条完整 leaf chain。
2. 在同一 vertex blend 汇合、共同决定终止拓扑的 edge blends。
3. Blend-on-blend 中需要共同重建局部拓扑的上下层圆角。
4. Cliff/face-edge blend 及其必要的缺失支撑面或 trace face。
5. 删除后预计生成同一组锐边的 smooth-connected 圆角组。

不要默认逐面删除。拆开连续链容易留下开放边、错误短边或无法闭合的角点。

### 可以批量处理

多个圆角可以组成一个 component，一次尝试删除，当且仅当它们：

- 不共享 vertex blend；
- 没有 blend-on-blend 或 cliff 关系；
- 不互相充当 support face；
- 没有短程拓扑依赖；
- 各自删除后都能恢复有效 solid；
- 不侵入孔、槽、薄壁、外轮廓或其他保护区域。

### 不要只按半径合并

同半径、相邻或切向连续只能作为弱证据。还要检查 support face、spring/cross edge、端点、junction、锐边结果以及 Creator ID（若有）。

同一 Creator 生成多个不连通片段时，也先验证整体宏动作；失败后拆回 leaf chain。

## 删除顺序

### 有历史或可靠父子关系时

采用创建顺序逆序：

```text
后创建 / child / 覆盖层 -> 先删除
先创建 / parent / 基础层 -> 后删除
```

具体规则：

- 如果 `B` 的 support face 是 `A` 的 blend face，先删 `B`。
- 如果 `B` 覆盖或截断 `A` 的 spring/cross edge，先删 `B`。
- 如果删除 `B` 后可以恢复 `A` 的 support，再删 `A`。
- 创建依赖 `A -> B`（B 依赖 A）应转换成删除顺序 `B -> A`。

### 没有历史时

按证据强弱排序：

1. 明确的 support-face 覆盖关系。
2. Blend-on-blend、cliff 和 vertex junction 等拓扑依赖。
3. 有明确恢复路径的普通 edge blend。
4. 独立、短、常半径、无分支圆角。

不要机械按半径大小排序。`ring chain` 先于 `disc chain` 只能作为常半径模型的低置信度先验，不能覆盖 support 或 parent-child 证据。

### 顺序不确定时

从同一个状态副本测试两个方向：

```text
T(G, A) = fail
T(G, B) = success
T(T(G, B), A) = success
```

只有两步都通过实体、拓扑和保护区域检查，才记录为 `B BEFORE A`。单次失败不能单独证明历史依赖，还要排除面引用失效、容差、heal 参数和内核错误。

## NX 策略选择

结合当前拓扑和已有尝试结果选择，不要把下表当成硬编码顺序：

| 形态 | 首选策略 | 失败回退 |
|---|---|---|
| 短、独立、无分支 | `face_edge_blend + cliff` | `notch`，再 `blend` |
| 内凹沟槽或槽底 | `face_edge_blend + notch` | `cliff`，再分段 |
| NX 可识别混合组件 | `blend` | `face_edge_blend` 或分段 |
| 多分支组件 | `topology_branch` / `simple_chains` | 逐段尝试 |
| 混合曲面类型 | `surface_type` | 按类型和拓扑拆段 |
| 大型失败组件 | `bisection` | 继续二分失败子段 |
| 非标准曲面 | `generic_face` | 局部重建或跳过 |

项目既有矩阵见：`.dsh/skills/nx-delete-fillet-component/references/strategy-matrix.md`。

## 识别漏检或耦合面的处理

当已识别圆角不能单独删除时，不要直接判定该圆角不可删除：

```text
试删已识别目标
→ 读取 topology_policy_rejected 和 collateral_disappeared_faces
→ 重新分析当前状态并定位所有相关 face_ref
→ 判断额外面是合理耦合面还是保护面
→ 必要时显式组成 Face Group
→ 再执行一次受控试删
```

`collateral_disappeared_faces` 只是耦合候选证据，不是自动授权。Face Group 必须只使用当前 `reanalyze_current_state` 返回的状态绑定 `face_ref`；不得使用旧引用、裸 NX Tag、STEP ID 或根据顺序猜测引用。`allow_related_faces` 只在预期相关面的范围已经明确时使用。

封口面需要单独判断几何角色。显式把封口面加入 Face Group，可能与只选择圆角链并让 NX 自动恢复封口产生不同结果；两者不能视为等价重试。使用 `allow_related_faces=true` 后，必须逐项核对实际消失面、总面数、邻接和支撑边界，超出预期立即回滚。删除后再次判断剩余平面的角色，因为同一平面可能从封口变为主体支撑面，不能仅依据 `Planar` 类型或删除前邻接决定删除资格。

对于混合或非标准曲面，若完整拓扑叶链已经确认、较专用的 `face_edge_blend` 组合在同一前态回滚失败，可尝试受控 `generic_face`。这表示切换删除后端，不表示可以无限扩大选面；圆锥、薄壁、外轮廓及已确认的支撑面仍应保护。案例记录见 `.dsh/skills/nx-delete-fillet-component/references/layered-deletion-case.md`。

## 事务执行纪律

每个删除动作都必须：

1. 从当前状态重新定位目标面，不能复用旧 face/edge index。
2. 检查目标集合完整性、局部边界和保护区域。
    3. 执行删除及必要的拓扑/几何重建；必要时提交包含未分类相关面的显式 Face Group。
4. 检查 solid、shell、manifold、方向、自交和闭合性。
5. 成功则提交，失败则回滚。
6. 成功后重新识别剩余圆角、leaf chain、support 和依赖。

同一状态下，相同目标和相同策略失败后不得无信息重复。应改用更小 chain、不同顺序、不同策略或二分组件。

失败去重键应至少包含“当前目标身份集合 + method + preference”；只改变 `allow_related_faces` 不构成新的有信息尝试。任何成功、回滚或重新导入后都必须重新分析并重新获取引用。

超时视为 `ambiguous`：先查询原操作状态，再决定提交、回滚或重试。

## 几何恢复边界

- 普通两侧解析母面：只在局部可信域内有界求交，并同步恢复公共 3D edge 与两侧 p-curve。
- 变半径：结合接触边界、法向、半径变化和局部优化，不按平均半径处理。
- Corner/setback：使用多边界 patch network 和 G0/G1/G2 约束，不假设只有一条母面交线。
- 母面消失：利用 trace face、spring curve、半径、法向及 extrude/revolve 先验重建。
- 相切或近平行母面：停止无限延长求交，改用局部补片、约束优化或网格辅助估计。
- Euler-like 操作只负责拓扑提交，不负责生成正确几何；sewing 只负责连接和容差收尾。
- DeFillet 的 Voronoi/rolling-ball 方法可作为识别和初值验证，但不能直接当作精确 STEP/B-rep 删除器。

## 输出格式

分析具体模型或制定方案时，按以下顺序输出：

1. **候选分组**：leaf chain/component、面、support、junction 和风险。
2. **关系标签**：`MUST_GROUP`、`BEFORE`、`INDEPENDENT` 或 `UNCERTAIN`，附判断依据。
3. **推荐顺序**：动作序列和删除策略。
4. **失败回退**：拆链、换策略、二分、回滚或跳过条件。
5. **验证条件**：实体有效性、拓扑闭合、局部几何变化、保护区域和 STEP 再导入。
6. **证据边界**：明确区分论文结论、项目经验和工程推断。

信息不足时，不要把 Creator 历史或顺序推断写成事实；标为 `UNCERTAIN`，并建议使用同一前态交换试验。
