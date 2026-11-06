# 圆角删除技巧总结：耦合分组、删除顺序与失败回退

本文根据以下资料整理，目标是为无历史或历史不可靠的 STEP/B-rep 模型提供可执行的圆角删除策略：

- `shidi-output/shidi_brep_直接特征抑制调研_20260911.md`
- `shidi-output/inverse_fillet_review_2015_2026.md`
- `shidi-output/shidi_精确B-rep局部修复与特征移除_20260911.md`
- `PaperReading/.../Removal of Blends from Boundary Representation Models.md`
- `PaperReading/.../Blend Recognition Algorithm and Applications.md`
- `PaperReading/.../Brep based automatic incremental blend suppression for meshing.md`
- `PaperReading/.../圆角识别前沿调研报告_20260921.md`
- `PaperReading/.../历史无关BRep圆角实例恢复与局部抑制策略_完整实施规划_20260920.md`
- 当前项目中的 `../../nx-delete-fillet-component/references/strategy-matrix.md`

文中“圆角”包括 fillet、blend、round；“删除”指真实 B-rep/实体抑制，不等同于 CAE 中的虚拟拓扑忽略。

## 1. 先记住三条总原则

### 1.1 删除单位通常不是单个圆角面

单个面只是识别载体，默认不应直接作为删除动作。实际删除单位优先级如下：

1. **完整 leaf chain**：由连续 cross edges 连接、在 sharp edge 或 vertex blend 处终止的最小几何原子。
2. **一次可安全处理的 blend chain 或圆角 component**：由多个 leaf chain 组成，适合整体删除时使用。
3. **原生 Fillet 操作实例**：若有可靠 creator ID，可作为宏动作；但同一操作也可能包含多个不连通片段，未必就是最稳妥的几何删除单元。
4. **单个 face**：只在链已拆开、局部回退或内核明确要求时使用。

原因是圆角面之间共享 spring edge、cross edge、vertex blend 或 support face。只删其中一个面，常会留下无法闭合的缺口、错误的短边或无法判断的终止拓扑。

### 1.2 先分组，再排序；不要把连通性直接当成历史

需要区分：

- **几何连通**：圆角面通过 cross edge、vertex blend 或短程拓扑路径相连。
- **共享 support**：一个圆角的母面/支撑面是另一个圆角的一部分。
- **历史依赖**：后创建圆角引用、截断或修改了先创建圆角产生的面/边。
- **操作性依赖**：在当前 NX/内核和容差下，删除一个圆角后另一个才可删除。
- **可执行耦合**：为使一次 B-rep 重建成功，必须作为一个事务一起提交的面集。

几何 chain 不一定等于一次 Fillet creator；一次 creator 也不一定等于一个最优抑制单元。没有历史时，先保守分成 leaf chain，再通过支持面关系和试删结果合并。

### 1.3 删除成功后必须重新分析

圆角删除会改变 face、edge、vertex 的拓扑和引用。删除成功后：

- 旧 face/edge ID 不再可靠；
- 重新识别圆角、leaf chain、support 和局部图；
- 重新计算候选顺序；
- 不要把删除前保存的面 ID 直接用于下一步。

## 2. 哪些圆角应该一起删除

### 2.1 必须一起删除：同一条连续 leaf chain

以下情况应优先作为一个动作：

- 多个圆角面由 cross edges 连成连续带；
- 这些面在几何上是同一条滚动球过渡带；
- 链两端终止于 sharp edge 或同一个 vertex blend；
- 单独删除任一面会留下两个 spring edges 或开放边界。

这是最基本的“不可拆”单元。经典 blend suppression 的做法是先折叠 cross edges 和孤立终止边，再把由两条 spring edges 围成的圆角面折叠为支撑面之间的新 edge。

### 2.2 通常一起删除：同一 component 中的简单、独立圆角

若多个圆角满足以下条件，可以批量删除：

- 不共享 vertex blend；
- 不存在 blend-on-blend；
- 不存在 cliff/face-edge 过渡；
- 不互相充当 support face；
- 彼此之间没有短程拓扑依赖；
- 每个圆角删除后都能独立恢复为合法 solid。

这类圆角可以按 FE graph/face-edge graph 分成独立 component，并在一次 entire suppression 中处理。Li、Zhu、Gu 的增量方法建议先尽可能整体删除简单 component，避免把本来可稳定处理的简单组拆成大量单面动作。

### 2.3 通常一起删除：smooth-connected blend group

删除前应沿圆角面之间的切向连续关系扩张，而不是只按面法向或半径聚类。适合合并的证据包括：

- 相邻面通过 cross edge 连接；
- 半径相同或半径变化连续；
- spring edge 方向和支撑面关系一致；
- 沿同一 vertex blend 平滑连接；
- 删除后预计生成同一组锐边。

但“同半径”只能作为弱证据。相邻且同半径的两个圆角可能来自不同 creator，也可能有不同删除顺序。实例恢复时应优先使用 leaf-chain pair、support、端点和 junction 特征，而不是仅按半径合并。

### 2.4 必须作为一个局部网络处理：vertex blend 汇交

多个 edge blends 在同一顶点汇合时，中心的 n-sided vertex blend 或球状过渡面连接各边圆角。处理原则是：

- 不把 vertex blend 当成普通 edge blend 单独删除；
- 先识别它连接的所有 edge-blend 分支；
- 若各分支属于同一操作或删除器要求整体拓扑变化，则一起处理；
- 若无法确认是否同一 creator，保留为多个 leaf chain，但把 vertex junction 作为共同耦合节点；
- 试删失败时优先以“vertex blend + 相邻分支”作为局部 component 重试，而不是随意增加远处面。

2020 年增量方法明确把 vertex blend 提前处理，再处理 smooth-connected groups，最后处理剩余简单圆角。这是因为 vertex blend 往往是复杂拓扑的主要耦合点。

### 2.5 必须考虑一起处理：blend-on-blend

如果后一个圆角建立在前一个圆角面上，则后一个圆角面可能是前一个圆角的 support。此时通常要：

1. 先隔离 blend-on-blend 边；
2. 识别哪一组是后创建或覆盖层；
3. 删除后创建的上层圆角；
4. 必要时重建被其吞掉的 support face；
5. 再删除下面的基础圆角。

不要把上下两层直接混成一个面集合后盲删。若内核对整体删除支持较好，可将其作为一个局部 component 一次提交；若整体失败，应按上述父子关系从上层向下层拆解。

### 2.6 必须考虑一起处理：cliff blend 或 face-edge blend

cliff blend 一侧接支撑面、另一侧接 edge，而不是普通的两母面 edge blend。它经常伴随：

- 原支撑面被后续操作完全移除；
- 需要重建新 support face；
- 不能仅靠普通两面求交恢复；
- 删除后会产生多个 sharp terminating edges。

这类圆角应与其相邻的局部缺失面、trace face 一起进入诊断范围，但不应无条件把整个零件的邻接面都加入删除集合。优先保留可证明的局部边界，并准备 surface reconstruction 或 patch fallback。

### 2.7 多条不连通链：先不要强行合并

同一个 creator operation 可能生成多条不连通 leaf chains；反过来，一条几何 chain 也可能是多次操作形成的。无历史 STEP 中的安全策略是：

- 先以 leaf chain 为节点；
- 用 creator ID、若有的 operation step、半径/support/端点相容性生成合并候选；
- 只有在高置信度下才合并为宏动作；
- 宏动作失败时回退到各 leaf chain；
- false merge 的代价通常高于 false split，因为错误合并会扩大删除区域并改变设计语义。

## 3. 多个圆角耦合时，先删除哪个

### 3.1 最可靠的通用顺序：后创建者先删

若能从 feature history、creator reference 或可靠的 chain parent-child 关系判断创建顺序，删除顺序采用创建顺序的逆序：

```text

后创建 / 子圆角 -> 先删除
先创建 / 父圆角 -> 后删除
```

依据是后创建圆角可能修改、截断或吞掉父圆角的 support face。先删除父圆角会使边界缺失或中间实体失效。

### 3.2 support-face 依赖规则

对两个圆角 `A` 和 `B`：

- 如果 `B` 的 support face 是 `A` 的 blend face，优先删 `B`；
- 如果 `B` 的 spring/cross edge 暗示它覆盖了 `A` 的边界，优先删 `B`；
- 如果删除 `B` 后能恢复 `A` 原来的 support face，再删除 `A`；
- 如果 `A`、`B` 只共享普通支撑面而没有覆盖关系，暂不强制排序，做交换试验。

这对应经典文献中的 parent-child 关系：child chain 应先被抑制，必要时删除父 chain 后再重建 child chain。

### 3.3 vertex blend 的顺序

对一个 edge-blend 网络，默认优先级为：

1. 后创建或覆盖其他圆角的 vertex/blend-on-blend 层；
2. vertex blend 本身及其必要连接分支；
3. smooth-connected edge-blend groups；
4. 剩余独立圆角。

但“先删所有 vertex blend”不是无条件规则。若 vertex blend 是多个分支共同生成的终止面，应将它和这些分支作为一个 component 评估；若内核要求先删相邻上层圆角，则应先处理上层而不是单独删 vertex 面。

### 3.4 ring chain 与 disc chain

Li 等文献总结的传统顺序是先处理 ring chain，再处理 disc chain。该规则适合对应的常半径 blend-chain 拓扑，但不能作为所有 NX/OCCT 模型的普遍定律。

工程上可把它作为低置信度先验：

- 若检测到闭环、环形耦合结构，优先尝试 ring chain；
- 再处理从环向外发散或盘状汇聚的 disc chain；
- 每一步都必须验证 solid 和局部边界；
- 只要 parent-child/support 证据与该先验冲突，以 parent-child 为准。

### 3.5 简单圆角与复杂圆角的顺序

推荐不要按半径从大到小或从小到大机械排序。更稳妥的优先级是：

1. 后创建、覆盖其他圆角的复杂子链；
2. vertex blend、blend-on-blend、cliff 等拓扑耦合链；
3. 有明确 support 恢复路径的普通 edge blend；
4. 独立、短、常半径、无分支圆角。

大半径不代表后创建，小半径也不代表先创建。半径主要用于识别和分组，不是可靠的历史顺序标签。

### 3.6 无法判断时：用交换试验而不是猜

对候选 `A`、`B`，应从同一个状态副本分别测试：

```text
T(G, A) = fail
T(G, B) = success
T(T(G, B), A) = success
```

在结果通过 valid-solid、闭合 shell 和局部语义检查时，可记录为“先删 B，再删 A”的操作性依赖。

注意：一次失败不能直接证明几何依赖。失败还可能来自内核版本、容差、随机性、heal 策略或删除器能力。应记录：

- NX/OCCT 版本；
- 容差和 healing 参数；
- 目标面签名；
- kernel status/error code；
- 输出 solid 有效性；
- 是否回滚成功；
- 交换顺序的重复试验结果。

## 4. 一套可执行的删除流程

### Step 0：保护输入和建立局部图

- 保存原始 STEP 和局部 shell；
- 为 face 建立几何签名，不依赖临时 index；
- 识别圆角候选、spring edges、cross edges、support faces；
- 建立 face-edge graph 和局部 blend graph；
- 标出 vertex blend、blend-on-blend、cliff、缺失 support 和多分支 junction。

### Step 1：生成保守删除单元

按以下顺序生成：

1. 连续 cross-edge 分量作为 leaf chain；
2. 由 shared support、vertex junction 和 smooth connection 产生 component 候选；
3. 由 creator/operation 信息产生宏动作候选；
4. 对不确定节点保留拆分版本。

默认禁止直接把整个圆角候选集合一次性删除。

### Step 2：先处理确定的后继依赖

从高置信 parent-child 图建立删除偏序。删除图中应使用逆向关系：若创建关系为 `A -> B`，表示 B 依赖 A，则删除关系为 `B -> A`。

对环或低置信区域，不强行拓扑排序，进入有限搜索或交换试验。

### Step 3：按复杂度和失败风险分层尝试

推荐尝试顺序：

1. 后继覆盖层和 blend-on-blend 子链；
2. vertex blend/复杂 junction component；
3. smooth-connected group；
4. 独立简单 component；
5. 剩余单条 leaf chain；
6. 最后才是单面删除或通用 generic-face fallback。

对 NX，可参考项目已有策略矩阵：短独立链优先 `face_edge_blend + cliff`；明显槽底圆角优先 `face_edge_blend + notch`；可识别的混合圆角组件优先 `blend`；多分支先拆成 simple chains；大型失败组件二分。

### Step 4：每次动作必须是事务

每一次删除都应执行：

1. 从当前状态重新定位目标面；
2. 检查面集合完整性和局部边界；
3. 执行删除/拓扑重构/几何恢复；
4. 检查 solid、shell、manifold、方向和自交；
5. 成功则提交；失败则回滚；
6. 成功后重新识别剩余圆角和依赖。

失败后不要无信息重复相同策略。应改变目标粒度、删除顺序、策略类型或局部边界。

## 5. 按圆角类型选删除后端

| 圆角情况 | 首选动作 | 不应做的事 | 失败回退 |
|---|---|---|---|
| 短、独立、常半径 edge blend | 整条 leaf chain 一次删除 | 单面随机删除 | `notch` 或 `blend` |
| 多面连续 edge blend | 整条 chain/group 删除 | 逐面拆除 | 按 cross edge 分段 |
| vertex blend | 与相邻分支作为局部 component 评估 | 当普通 edge blend 单独删除 | 先拆上层分支再处理 junction |
| blend-on-blend | 后创建/覆盖层先删 | 先删底层 support | 重建缺失 support face |
| cliff/face-edge blend | 局部拓扑重构并准备补面 | 只求两支撑面交线 | trace-face/patch 重构 |
| 变半径 blend | 保留完整链并使用局部几何约束 | 只按平均半径排序 | 局部 patch 或优化 |
| 相切/近平行 support faces | 受界有界求交 | 无限延长后盲目求交 | 新补片、法向优化、网格 oracle |
| 大型多分支组件 | 先识别 junction 再分段 | 直接一次删除全部面 | bisection / simple chains |
| 仅为 CAE 忽略小圆角 | 虚拟拓扑/网格级处理 | 强制生成真实 STEP 无圆角面 | 保留原 B-rep，单独输出简化模型 |

## 6. 几何恢复的技术要点

### 6.1 普通 edge blend：优先有界求交

两侧母面可靠且局部不相切时，可以用支撑面延拓求交恢复原锐边，但必须：

- 只在圆角边界附近的可信区域延拓；
- 用旧 spring edge 端点、法向、面内外关系筛选交线分支；
- 让 3D edge 与两侧 p-curve 同步；
- 统一公共 edge geometry 后再缝合；
- 检查远场交点、错误分支和多余短边。

### 6.2 复杂 corner/setback：不要假定只有一条母面交线

三面或多面汇交、setback、n-sided vertex blend 往往没有单一“两母面交线”答案。应先确定局部拓扑，再联合生成 patch network，并约束 G0/G1/G2 连续性、角点位置和最小改动量。

### 6.3 母面消失：从 trace face 和滚动球条件重建

如果后续圆角吞掉了某个 support face，可利用：

- 未受影响的 trace faces；
- spring curves；
- 圆角半径和法向；
- extrude/revolve 等可识别方向；
- 局部采样和拟合。

经典方法展示了对 extrude/revolve 情况可通过 offset support、投影 spring edge、扫掠 spine、再 offset 的方式重建缺失面。若无法确定设计语义，不要伪装成精确恢复，应将其标为候选补片并加强验证。

### 6.4 用 DeFillet 思路做几何验真，而不是直接替换精确 B-rep

DeFillet 在 polygonal CAD 上利用 Voronoi 顶点估计 rolling-ball center trajectory，再用目标法向二次优化恢复锐特征。它的工程价值是：

- 验证候选区域是否具有真实滚动球圆角特征；
- 估计锐边位置和方向；
- 为局部 trim/patch 优化提供初值。

它输出的是 polygon mesh，不应直接当作精确 STEP/B-rep 删除器。

## 7. 失败模式与处理顺序

### 7.1 “少删一面”导致重建失败

表现：删除后仍有开放边、短边或无法闭合的角点。

处理：检查是否漏掉同一 leaf chain、vertex blend 或终止面；扩大到完整局部链，而不是扩大到整个模型。

### 7.2 “多删一组”改变设计语义

表现：实体有效但孔、槽、薄壁或关键外轮廓被改变。

处理：回滚；将宏动作拆成 leaf chains；保护外边界、孔口和分析载荷相关区域。

### 7.3 相邻母面相切或近平行

表现：延长后无交线、交线在远处、交线分支不稳定。

处理：不要继续盲目加大延伸距离；改用局部补片、法向约束优化或网格锐边 oracle。

### 7.4 删除器策略不匹配圆角类型

表现：`blend`、`face_edge_blend`、`notch` 对同一目标返回不同失败。

处理：按圆角类型切换策略；记录失败类型；不要相同目标无信息重复。

### 7.5 大型组件一次删除超时或结果不明确

处理：先查询原操作状态；确认是否已提交；若未提交则回滚并二分组件；对成功子段重新分析。

### 7.6 删除顺序看似合理但仍失败

不要立即把失败标成依赖错误。先排除：

- 面引用已失效；
- NX/OCCT 版本差异；
- 容差和 heal 设置；
- 目标面识别错误；
- 支撑面已被前一步修改；
- 内核本身不支持该拓扑。

## 8. 推荐的排序器

可把每个候选删除单元 `C` 评分为：

```text
priority(C) =
  dependency_confidence
  + support_recovery_confidence
  + topology_predictability
  + local_simplicity
  - branch_count
  - missing_support_risk
  - expected_shape_change
```

硬约束优先于分数：

1. 已确认的 child 必须先于 parent；
2. 必须共同删除的 leaf chain 不得拆开；
3. 保护区域不得作为目标；
4. 失败动作必须回滚；
5. 删除后必须重新分析。

对无法排序的候选，使用有限 beam search、best-first 或 MCTS；RL 只负责选择下一个实例/链、拆分、回滚和切换策略，不负责替代几何内核。

## 9. 最小检查清单

### 删除前

- [ ] 已保存原始模型和当前状态副本。
- [ ] 已识别完整 leaf chain，而不是只选一个 face。
- [ ] 已标记 spring edge、cross edge、support face 和终止边。
- [ ] 已检查 vertex blend、blend-on-blend、cliff 和缺失 support。
- [ ] 已区分几何连通、creator 实例和可执行删除单元。
- [ ] 已给候选关系标记 `BEFORE`、`INDEPENDENT` 或 `UNCERTAIN`。

### 删除中

- [ ] 当前动作来自当前状态，而不是旧 face ID。
- [ ] 采用 child-first / 后创建者优先，除非交换试验证明相反。
- [ ] 每次动作都有事务、日志和回滚。
- [ ] 没有对相同目标和相同策略进行无信息重复。

### 删除后

- [ ] solid、shell、manifold、方向和自交检查通过。
- [ ] 局部公共 edge 和 p-curve 一致。
- [ ] 没有误删孔、槽、薄壁或保护外轮廓。
- [ ] 已重新识别剩余圆角和依赖。
- [ ] 已记录成功、失败、回滚、耗时和内核错误类型。

## 10. 最终决策表

| 问题 | 默认答案 |
|---|---|
| 多个连续圆角面是否一起删？ | 是，至少按完整 leaf chain 删除。 |
| 同一 vertex blend 周围的圆角是否一起删？ | 作为局部 component 评估；不一定盲目合并，但不能忽略 junction。 |
| 同半径相邻圆角是否一起删？ | 不是充分条件，需结合 support、端点和 creator/操作性证据。 |
| blend-on-blend 谁先删？ | 后创建、覆盖或依赖别人的上层圆角先删。 |
| 父子圆角谁先删？ | child 先删，parent 后删。 |
| 没有历史时怎么判断先后？ | support/覆盖证据优先；否则做同前态交换试验。 |
| ring/disc 规则是否普遍适用？ | 仅作常半径链的低置信度先验，不能覆盖直接依赖证据。 |
| 相切母面还能否继续延长求交？ | 不应盲目继续；切换局部补片或约束重建。 |
| 失败后是否把更多面一次性加进去？ | 不应。先回滚、诊断，再按 chain/component 二分或切换策略。 |
| DeFillet 是否可直接生成无圆角 STEP？ | 不可。它是 polygon mesh 方法，可作为识别、验真和几何初值。 |

## 11. 资料边界

本文的“后创建者先删”“leaf chain 作为最小原子”“parent-child 逆序”“support face 缺失时需要重建”等结论，主要来自 Venkataraman 等经典 blend recognition/suppression 工作、2020 年 Li 等增量抑制论文以及当前项目策略文档。

“使用 DeFillet 的 rolling-ball/Voronoi 结果作为 B-rep 局部优化初值”“以 false merge 高于 false split 为代价函数”“用 beam/MCTS/RL 选择删除动作”等属于基于论文的工程迁移建议，不是这些论文已经直接证明的通用算法结论。

公开资料仍不能证明存在一个对所有自由曲面、变半径、setback、多分支圆角和相切支撑面都鲁棒的精确 B-rep 通用删除算法。因此任何排序规则都必须通过当前 NX 版本的事务式试删、实体有效性检查和失败回滚进行校准。

## 参考资料

1. Venkataraman, Sohoni, Rajadhyaksha, *Removal of Blends from Boundary Representation Models*, 2002, DOI: `10.1145/566282.566297`。
2. Venkataraman, Sohoni, Elber, *Blend Recognition Algorithm and Applications*, 2001, DOI: `10.1145/376957.376970`。
3. Li, Zhu, Gu, *B-rep based automatic incremental blend suppression for meshing*, 2020, DOI: `10.1109/WCMEIM52463.2020.00087`。
4. Slyadnev, Turlapov, *Simplification of CAD Models by Automatic Recognition and Suppression of Blend Chains*, 2020, DOI: `10.1134/S0361768820030081`。
5. Jiang et al., *DeFillet: Detection and Removal of Fillet Regions in Polygonal CAD Models*, 2025, DOI: `10.1145/3731166`。
6. Kulkarni et al., *Leveraging feature information for defeaturing sheet metal feature-based CAD part model*, 2016, DOI: `10.1080/16864360.2016.1168238`。
7. Yoon and Kim, *CAD model simplification using feature simplifications*, 2016, DOI: `10.1299/jamdsm.2016jamdsm0099`。
