# NX2406 中“获取所有可删除圆角面”的问题记录

> 本记录中的根目录 `Defillet/` 对应迁移目标 `cad/Defillet/`；以下保留记录时的路径和状态。

## 1. 问题目标

希望通过 NXOpen 自动获得与 Siemens NX 2406 GUI 中以下操作一致的面集合：

```text
删除面（Delete Face）
  → 类型：Face Edge Blend / 删除圆角
  → GUI 中允许用户点击选择的面
```

目标集合不仅包括 `Face.GetBlendData()` 明确认定的圆角面，也希望包括 GUI 能选中、但 `GetBlendData()` 返回 `False` 的 blend-like 或未标记圆角面。

最终用途包括：

- 标记所有可能的圆角面；
- 将这些面直接在 NX 中着色并导出 STEP；
- 将同一候选集合用于自动去圆角流程。

---

## 2. 当前已确认的事实

### 2.1 `Face.GetBlendData()` 过于保守

当前基础判断为：

```python
radius, is_blend = face.GetBlendData()
```

优点：

- `is_blend=True` 时，NX 明确认定该面为 blend；
- 调用快；
- 不修改拓扑；
- 对明显圆角具有较高置信度。

问题：

- 会漏掉部分 GUI“删除圆角”命令中可以选中的面；
- 导入 STEP 后，一些 blend-like、未标记圆角或经过拓扑重建的面可能返回 `False`；
- 拓扑经过删除与修补后，同一区域的新面可能不再被 `GetBlendData()` 标记为 blend。

在当前样例中，GUI 可选面与 `GetBlendData=True` 集合并不完全一致。

### 2.2 GUI 使用命令专用选择过滤器

GUI 的 Delete Face 命令在：

```python
DeleteFaceBuilder.Type = DeleteFaceBuilder.SelectTypes.FaceEdgeBlend
```

模式下会启用内部选择过滤器。该过滤器会阻止某些面被点击选中，例如大量普通平面。

但 NXOpen 2406 的公开接口中没有发现：

```python
CanSelectAsFaceEdgeBlend(face)
```

或其他直接返回 GUI 可选状态的 API。

因此 GUI 的内部 preselection/selection-mask 判定不能直接通过一个公开布尔函数读取。

### 2.3 `FaceConnectedBlendRule` 不能独立作为圆角判定器

曾尝试对所有面执行：

```python
rule = work_part.ScRuleFactory.CreateRuleFaceConnectedBlend(
    face,
    True,   # includeBlendLike
    True,   # includeUnlabeledBlend
    NXOpen.Features.Feature.Null,
    options,
)
```

然后根据以下条件认定候选：

```python
rule_created and seed_retained and member_count > 0
```

实际结果是几乎所有面都被认定为候选并标红。

原因：

- ConnectedBlend 是 selection-intent 规则构造器，不是 `IsBlendFace` 验证函数；
- 任意合法面都可以作为 seed；
- 即使没有发生圆角链传播，collector 通常仍包含 seed 本身；
- 对普通面常见结果是：

```text
members = [seed_face]
member_count = 1
```

因此：

```python
seed_retained and member_count > 0
```

接近恒成立，不能区分圆角和平面。

### 2.4 `FaceRecognitionBuilder.SelectEntities()` 没有接受结果

公开接口为：

```python
builder.FaceRecognized.SelectEntities([face])
```

官方/本地 API 描述仅为：

```text
Adds entities to collector
```

该方法：

- 返回 `None`；
- 不提供 accepted/rejected 列表；
- 不提供 GUI 过滤器状态；
- 程序强制传入一个面时，可能只是将它加入 collector。

所以“不抛异常”或“collector 中存在该面”都不能证明 GUI 会允许用户点击该面。

### 2.5 `Commit()` 测试的是可删除性，不是 GUI 可选性

可配置完整 Delete Face Builder：

```python
builder.Type = SelectTypes.FaceEdgeBlend
builder.FaceEdgeBlendPreference = FaceEdgeBlendPreferenceOptions.Cliff
builder.DeletePartialBlend = False
```

然后执行：

```python
builder.Commit()
```

这可以验证当前选择集合是否能够被 NX 删除并修补。

但它与 GUI 可选性仍然不同：

- GUI 可选的圆角面可能单面 Commit 失败，因为需要整条圆角链一起删除；
- 非典型面如果能被 NX 删除并修补，也可能 Commit 成功；
- Commit 成功表示“该建模操作可执行”，不严格表示“该面是圆角”；
- Commit/Undo 成本较高，且必须严格验证 Undo 后拓扑恢复。

### 2.6 Preview 也不是 GUI 可选性的直接结果

`PreviewBuilder.Preview()` 或 `Builder.ShowResults()` 可以测试 NX 是否能够生成操作预览。

但 Preview 成功表示操作具有可预览结果，并不保证 GUI 内部选择过滤器会接受该面；反之，GUI 可选面也可能因为当前选择集合不完整而 Preview 失败。

---

## 3. 已出现的错误方案

### 3.1 仅使用 `GetBlendData()`

结果：

- 精度较高；
- 但漏掉 GUI 可选的 blend-like 面。

### 3.2 标记已确认圆角的所有共享边邻面

结果：

- 大量圆角支撑平面被错误标记；
- “与圆角共享拓扑边”不等于“可能是圆角”；
- 无法发现远离已知圆角区域的独立漏检面。

该逻辑已经确认不符合目标。

### 3.3 对所有面创建 ConnectedBlend 并检查 seed 是否保留

结果：

- 所有或几乎所有面被标记；
- 原因是 ConnectedBlend 规则天然保留 seed；
- 规则创建成功和 seed retention 都不是圆角资格验证。

该逻辑不能继续作为生产判据。

### 3.4 NX 面到 OCC 面的坐标映射

曾从 NX `journal_identifier` 中提取一个坐标，再在 OCC 中寻找最近面并标色。

问题：

- 坐标靠近公共边时可能对应多个面；
- NX 与 OCC 对 STEP 的拓扑拆分可能不同；
- 容差放宽后可能匹配到支撑平面；
- OCC 重新导出还可能拆分面，使着色数量变化；
- NX 的 `geometry_digest`、面序号和半径没有参与 OCC 匹配。

因此最终着色应尽量直接使用 NX 中的真实 Face 对象，不应依赖 NX→OCC 坐标二次映射。

---

## 4. NXOpen 2406 公开接口能够提供什么

### `Face.GetBlendData()`

提供：

- blend 半径；
- 是否为 NX 明确认定的 blend。

不能提供：

- GUI FaceEdgeBlend 选择过滤器是否接受该面。

### `CreateRuleFaceConnectedBlend()`

提供：

- 从 seed 出发收集 connected blend、blend-like 或 unlabelled blend 链的 selection-intent 规则。

不能提供：

- seed 本身是否为有效圆角；
- GUI 是否允许选择该 seed。

### `FaceRecognitionBuilder.SelectEntities()`

提供：

- 将实体加入识别 collector。

不能提供：

- accepted/rejected 状态；
- GUI 内部选择过滤结果。

### `DeleteFaceBuilder.Commit()`

提供：

- 实际执行删除和修补；
- 可用作“当前集合是否可被删除”的最强操作性测试。

不能严格提供：

- GUI 是否允许最初点击 seed；
- seed 是否在语义上一定是圆角。

---

## 5. 当前最可行的研究方案

### 5.1 建立 GUI 人工真值集

选择一批代表性面，人工记录 GUI 中是否能够在 FaceEdgeBlend 模式下点击进入选择列表：

- `GetBlendData=True` 的圆角；
- `GetBlendData=False` 但 GUI 可选的面；
- 明显普通平面；
- 孔和凸台的完整圆柱面；
- 球面、圆环面；
- 导入的 B 样条圆角；
- 需要多面链才能删除的圆角；
- Notch/Cliff 行为不同的区域。

这是判断 GUI 可选性的唯一可靠真值来源。

### 5.2 对每个样本采集多个 NXOpen 信号

建议记录：

- `GetBlendData()`；
- `SolidFaceType`；
- ConnectedBlend member count；
- 是否从 seed 扩展到其他面；
- `SelectEntities()` 前后 collector 差异；
- FaceRecognition runtime 是否存在 `Validate()`，以及结果；
- Preview 是否成功；
- 单面 Commit/Undo 是否成功；
- ConnectedBlend 整链 Commit/Undo 是否成功；
- Undo 后面数、体数及定位器是否恢复。

然后与 GUI 人工标签比较，选择在 NX2406 当前环境中最接近 GUI 的组合判据。

### 5.3 候选置信度分层

在没有完全等价接口的情况下，建议不要输出单一且未经解释的 `is_fillet`，而是分层：

1. `nx_confirmed`
   - `GetBlendData=True`；
   - 最高置信度。

2. `gui_calibrated_likely`
   - 与 GUI 人工样本校准后的组合规则匹配。

3. `delete_commit_confirmed`
   - FaceEdgeBlend Commit 成功且 Undo 完整恢复；
   - 表示可删除性，不等同于 GUI 可选性。

4. `indeterminate`
   - 探测失败、Undo 无法验证或信号冲突。

### 5.4 着色建议

建议按置信度使用不同颜色：

- 红色：`nx_confirmed`；
- 橙色：`gui_calibrated_likely`；
- 黄色：`delete_commit_confirmed` 但未被 `GetBlendData` 确认；
- 不着色：rejected；
- 紫色或单独报告：indeterminate。

这样可以避免把启发式候选误当成 NX 确认圆角。

---

## 6. 安全要求

若使用 Commit/Undo 探测：

1. 必须在一次性导入的 Part 中操作；
2. 每个候选设置独立 Undo Mark；
3. 成功和失败都必须 Undo；
4. 每次 Undo 后重新枚举 Face；
5. 不得跨 Commit/Undo 保留 NX Face 对象；
6. 必须通过纯数据 locator 重新定位；
7. 验证体数、面数和候选定位器恢复；
8. Undo 验证失败时立即终止当前上下文；
9. 探测阶段不保存 PRT 或 STEP；
10. 所有候选完成后，再在恢复后的拓扑上统一着色并导出。

---

## 7. 当前代码状态与待修正项

相关文件：

- `Defillet/nx_color_blend_faces_red.py`
- `Defillet/color_blend_faces_red.py`
- `Defillet/nx_grow_delete_fillet_from_face.py`
- `Defillet/OCC_Imple/color_report_remaining_blends_red.py`
- `Defillet/README.md`

当前已知问题：

- `nx_color_blend_faces_red.py` 当前 ConnectedBlend 判据会将 seed-only 结果视为候选，可能导致全模型着色；
- `nx_grow_delete_fillet_from_face.py` 当前全模型 ConnectedBlend 候选逻辑存在同样的 seed retention 问题；
- 两处都必须停止使用：

```python
rule_created and seed_retained and member_count > 0
```

作为圆角判据；
- `README.md` 中将该规则描述为候选识别方式的内容需要在最终修复后同步更新；
- OCC 邻接扩张着色脚本仅适合历史调试，不应作为最终圆角识别方案。

---

## 8. 后续决策

在继续实现前，需要从以下方案中选择：

### 方案 A：保守生产规则

只采用：

- `GetBlendData=True`；
- 明确 `SolidFaceType=Blending`；
- 经过严格几何/曲率规则验证的非平面候选。

优点：安全、快速。

缺点：仍可能漏掉 GUI 可选面。

### 方案 B：Preview 近似

对所有未确认的非平面面配置 FaceEdgeBlend Builder 并尝试 Preview。

优点：非永久修改，比静态规则更接近建模操作。

缺点：Preview 成功不等价于 GUI 可选。

### 方案 C：Commit/Undo 可删除性验证

对候选或整条 ConnectedBlend 链执行 Commit，成功后立即 Undo，并严格验证恢复。

优点：最强的公开操作性证据。

缺点：耗时；表示可删除性，而非严格 GUI 可选性；单面探测会漏掉必须整链删除的圆角。

### 方案 D：先构建校准探针

先不着色，输出所有可获取信号，与 GUI 人工标签比较，再确定生产判据。

优点：最符合“不要猜测”的要求。

缺点：需要一次人工标注和校准过程。

---

## 9. 结论

当前核心困难不是“如何遍历所有面”，而是：

> NX2406 公开 NXOpen API 没有直接暴露 Delete Face → FaceEdgeBlend GUI 内部的面选择过滤结果。

已经确认：

- `GetBlendData()` 可靠但会漏检；
- ConnectedBlend seed retention 会产生全选，不能作为判据；
- 邻接已知圆角会误选支撑平面；
- OCC 坐标映射存在面对应风险；
- Commit/Undo 是最强可验证的可删除性测试，但不等价于 GUI 可选性。

因此，若要尽可能复现 GUI 行为，下一步应优先构建校准探针并使用人工 GUI 真值验证，而不是继续假设某个 collector 或 selection rule 等价于 GUI 过滤器。
