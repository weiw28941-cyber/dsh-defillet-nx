# NX 圆角识别、着色与递增删除脚本

本目录包含两个独立的 NXOpen 工作流：

1. **圆角候选识别与着色**：扫描 STEP 中的全部面，将 NX ConnectedBlend 规则接受的面直接标色并导出 STEP。
2. **圆角递增删除**：按种子及一环增长方式尝试删除圆角，并导出处理后的 STEP 和 JSON 报告。

## 环境要求

- Windows
- Siemens NX 2406，默认从 `%ProgramFiles%\Siemens\NX2406` 定位；可通过
  `UGII_BASE_DIR`、`NX_RUN_JOURNAL` 或命令行选项指定实际安装位置。
- 普通 Python 3.10 或兼容版本
- NX STEP214 导出配置，默认使用 NX 安装目录中的 `STEP214UG\ugstep214.def`。

NXOpen journal 不能由普通 Python 直接执行，因为它依赖 NX Session、Parasolid 建模会话和 NX 授权。普通 Python 包装器会自动启动 `run_journal.exe`。

---

## 1. 全模型圆角候选识别与着色

### 文件说明

- `color_blend_faces_red.py`
  - 普通 Python 包装入口。
  - 自动定位并启动 NX `run_journal.exe`。
  - 推荐日常使用此脚本。
- `nx_color_blend_faces_red.py`
  - 真正调用 NXOpen API 的 journal。
  - 必须在 NX `run_journal.exe` 中执行。
- `color_matched_step_faces_red.py`
  - 从匹配结果提取 STEP 面并调用着色入口；默认案例数据仍位于 `data/CAE/`。
  - 支持显式传入输入、匹配结果和输出路径；`--extract-only` 只生成标记文件，不启动 NX。

### 识别规则

脚本对模型中的**每一个 NX Face**独立创建：

```text
CreateRuleFaceConnectedBlend(
    face,
    includeBlendLike=True,
    includeUnlabeledBlend=True,
    ...
)
```

满足以下条件时，该种子面被视为圆角候选：

- ConnectedBlend 规则成功创建；
- 规则 collector 非空；
- collector 中保留了当前种子面。

该规则：

- 不要求 `Face.GetBlendData()` 返回 `True`；
- 不依赖已确认圆角的邻接关系；
- 不通过 OCC 或坐标重新匹配面；
- 直接给当前 NX Face 对象设置颜色；
- 使用 NX `StepCreator` 导出彩色 AP214 STEP。

> 注意：NXOpen 没有公开的 `CanSelectAsFaceEdgeBlend(face)` 接口。ConnectedBlend 是公开 API 中最接近 Delete Fillet GUI 选择范围的非破坏性近似，但 Siemens 未保证它与 GUI 内部过滤器完全等价。

### 推荐命令

在仓库根目录执行：

```powershell
python cad\Defillet\color_blend_faces_red.py `
  --input ".\local-artifacts\input.stp" `
  --output ".\local-artifacts\colored.stp" `
  --report ".\local-artifacts\colored_report.json" `
  --overwrite
```

包装器内部实际执行：

```text
run_journal.exe nx_color_blend_faces_red.py -args ...
```

### 直接调用 NX journal

调试时也可以跳过包装器：

```powershell
& "$env:UGII_BASE_DIR\NXBIN\run_journal.exe" `
  (Resolve-Path ".\cad\Defillet\nx_color_blend_faces_red.py").Path `
  -args `
  --input "D:\path\input.stp" `
  --output "D:\path\colored.stp" `
  --report "D:\path\colored_report.json" `
  --overwrite
```

### 参数

| 参数 | 必需 | 默认值 | 说明 |
|---|---:|---|---|
| `--input` | 是 | 无 | 输入 STEP 文件 |
| `--output` | 是 | 无 | 彩色 STEP 输出文件，不能与输入相同 |
| `--report` | 否 | `<output>_report.json` | JSON 探测报告 |
| `--part-output` | 否 | 空 | 同时保存彩色 NX PRT 文件 |
| `--color` | 否 | `186` | NX 颜色索引，范围 1–216；186 为红色 |
| `--settings-file` | 否 | NX2406 的 `ugstep214.def` | STEP214 导出配置 |
| `--overwrite` | 否 | 关闭 | 允许覆盖已有输出 |
| `--keep-open` | 否 | 关闭 | 执行结束后保持 NX Part 打开 |
| `--nx-base` | 否 | 自动解析 | 仅包装器支持，指定 NX 根目录 |
| `--run-journal` | 否 | 自动解析 | 仅包装器支持，显式指定 `run_journal.exe` |

`run_journal.exe` 的解析优先级：

1. `--run-journal`
2. 环境变量 `NX_RUN_JOURNAL`
3. `--nx-base`
4. 环境变量 `UGII_BASE_DIR`
5. 默认 NX2406 路径

### JSON 报告关键字段

- `total_face_count`：扫描到的全部 NX 面数。
- `probed_face_count`：实际执行 ConnectedBlend 探测的面数；正常应与总面数相等。
- `get_blend_data_confirmed_count`：`GetBlendData()` 明确认定的圆角数。
- `connected_blend_candidate_count`：ConnectedBlend 规则接受的候选数。
- `connected_blend_only_count`：ConnectedBlend 接受、但 `GetBlendData()` 未确认的候选数。
- `colored_blend_face_count`：实际在 NX 中着色的面数。
- `face_probe_results`：所有面的完整探测结果。
- `candidate_faces`：最终被着色的候选面。

每个面的主要诊断字段包括：

- `is_blend`
- `blend_radius`
- `connected_blend_rule_created`
- `connected_blend_seed_retained`
- `connected_blend_member_count`
- `connected_blend_member_journal_identifiers`
- `connected_blend_error`
- `connected_blend_candidate`

---

## 2. 圆角递增删除

### 文件

`nx_grow_delete_fillet_from_face.py`

这是 NXOpen journal，需要通过 `run_journal.exe` 执行。

### 示例

```powershell
& "D:\Program Files\Siemens\NX2406\NXBIN\run_journal.exe" `
  (Resolve-Path ".\cad\Defillet\nx_grow_delete_fillet_from_face.py").Path `
  -args `
  --input "D:\path\input.stp" `
  --output "D:\path\defillet.stp" `
  --report "D:\path\defillet_report.json" `
  --max-faces 50 `
  --max-runtime-seconds 600 `
  --skip-phase1 `
  --overwrite
```

### 当前删除策略

```text
SelectTypes.FaceEdgeBlend
FaceEdgeBlendPreferenceOptions.Cliff
DeletePartialBlend=False（固定关闭，不作为外部参数）
```

Phase 2 会：

1. 对当前拓扑的所有面执行 ConnectedBlend 候选探测；
2. 从候选种子开始尝试删除；
3. 失败时逐个加入可用的一环候选；
4. 每次失败均 Undo；
5. 成功 Commit 后保留结果、重新扫描拓扑；
6. 达到全局时间限制后停止并导出当前最佳结果。

### 主要参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--max-faces` | `50` | 单个递增集合最大面数，允许范围 1–50 |
| `--max-runtime-seconds` | `600` | 全局处理时间限制，默认 10 分钟 |
| `--skip-phase1` | 默认启用 | 跳过逐个单面删除阶段 |
| `--run-phase1` | 关闭 | 启用逐个单面删除阶段 |
| `--seed-face` | 空 | 可选的优先种子显示名称 |
| `--selection-x/y/z` | 空 | 可选种子选择点，三项必须同时提供 |
| `--overwrite` | 关闭 | 覆盖已有输出和报告 |

NX 正在执行的单次 `Commit()` 不能安全地中途终止，因此最终总耗时可能略高于 `--max-runtime-seconds`。

---

## 3. 常见问题

### 普通 Python 报错找不到 `NXOpen`

不要直接执行：

```text
python cad\Defillet\nx_color_blend_faces_red.py
```

应执行包装器：

```text
python cad\Defillet\color_blend_faces_red.py ...
```

或者使用 NX 的 `run_journal.exe`。

### 输出文件已存在

添加：

```text
--overwrite
```

### STEP 中颜色没有显示

确认使用支持 AP214 面颜色的查看器，并检查 JSON 报告中的：

- `success`
- `colored_blend_face_count`
- `output_size`
- `errors`

### 候选数量多于 `GetBlendData()` 圆角数量

这是预期行为。ConnectedBlend 开启了 `includeBlendLike` 和 `includeUnlabeledBlend`，识别范围比 `GetBlendData()` 更宽。应通过报告中的逐面证据和 NX GUI 抽查候选准确性。

### `success: true` 的含义

- 着色脚本：表示扫描、着色及 STEP 导出完成。
- 删除脚本：表示结果 STEP 成功发布，不表示所有圆角均已删除。
