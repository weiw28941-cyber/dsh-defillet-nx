# STEP 智能编辑器 Web MVP

本机 Web 应用，支持上传 STEP、旋转缩放、按 BREP 面多选、输入自然语言要求、由 OpenAI 兼容模型生成受限删除圆角计划、串行调用固定 NX2406 journal，并展示/下载结果。

## 架构

- `frontend/`：Vite + React + TypeScript + Three.js / React Three Fiber
- `backend/`：FastAPI + SQLite + OCC
- `cad/Defillet/nx_web_delete_fillet.py`：固定白名单 NX journal
- `jobs/`：本机作业与不可变 revision 工件（运行后生成）

大模型不能生成或执行 Python/NXOpen 代码，只能返回严格的 `fillet.delete` JSON 计划。后端验证目标面属于当前模型后调用固定 journal。NX worker 使用全局锁，单进程串行执行。NX 建模操作失败时，规范化失败信息会连同历史计划反馈给大模型，要求选择不同策略或更小目标子集，并在有界次数内继续尝试；许可证不可用、NX 无法启动或超时等基础设施错误会立即终止，不会触发无意义的几何重试。上传阶段暂不进行圆角候选判定，以避免额外启动 NX 和导入 STEP。

## 安装

在 `defeature` Conda 环境中安装后端依赖：

```powershell
conda activate defeature
pip install fastapi "uvicorn[standard]" python-multipart pydantic httpx
```

安装前端依赖：

```powershell
cd webapp\frontend
npm install
```

`pythonocc-core` 应已存在于 `defeature` 环境中。

## 配置

后端默认不读取任何本机 OpenCode 配置。需要复用 provider 时，显式设置 `OPENCODE_CONFIG_PATH`。

后端会读取该 provider 的：

- `baseURL`
- `apiKey`
- 配置中的模型名
- `Aimp-Biz-Id` 与 `AIGC-USER` 请求头

配置示例：

```powershell
$env:OPENCODE_CONFIG_PATH="D:\path\to\opencode.json"
```

仍可用环境变量覆盖配置文件：

```powershell
$env:OPENAI_BASE_URL="https://api.openai.com/v1"
$env:OPENAI_API_KEY="..."
$env:OPENAI_MODEL="your-model"
$env:UGII_BASE_DIR="C:\Program Files\Siemens\NX2406"
```

按 provider 要求设置可选请求头 `OPENAI_AIMP_BIZ_ID` 和 `OPENAI_AIGC_USER`。
不要将真实密钥写入仓库。`.env.example` 仅供参考，启动器不会自动加载 `.env`。

## 启动

推荐从仓库根目录用一个命令同时启动前后端：

```powershell
.\webapp\start.ps1
```

先激活兼容的 Python/OCC 环境。启动器默认使用当前 Python，也可通过
`DEFEATURE_PYTHON` 或 `--python` 指定解释器，不会读取作者机器的路径。
启动器会依次启动 FastAPI 和 Vite，等待两个服务就绪后自动打开：

<http://127.0.0.1:5173>

当前终端会保持占用；按 `Ctrl+C` 会同时终止前后端及其子进程。可选参数：

```powershell
.\webapp\start.ps1 --no-browser
.\webapp\start.ps1 --reload
.\webapp\start.ps1 --backend-port 8000 --frontend-port 5173
```

默认不开启 Uvicorn reload，以避免 Windows 重载子进程和端口冲突。启动前会检查 8000/5173；若已有服务占用，会直接给出明确错误，而不会误连旧服务。

如需分别调试，也可以使用两个终端：

```powershell
.\webapp\start_backend.ps1
.\webapp\start_frontend.ps1
```

## 使用

1. 上传 `.stp` 或 `.step` 文件。
2. 等待 OCC 完成逐 BREP 面三角化。
3. 在左侧视口拖动旋转、滚轮缩放，点击一个或多个 BREP 面。
4. 在文本框中输入删除圆角要求。
5. 点击“交给大模型规划并执行”。
6. 后端校验模型计划，串行调用 NX。
7. 成功后右侧显示结果模型，并可下载当前 STEP。

## 面身份

每个浏览器 mesh 对应一个 OCC BREP 面，metadata 包含：

- revision-scoped `faceId`
- OCC/FreeCAD 风格面序号
- 原 STEP `ADVANCED_FACE #N`
- 曲面类型、面积、质心
- 浏览器点击的 model-space point

NX 端以服务器生成的选择点重新定位面。客户端不能提交任意文件路径或 journal。

## 当前限制

- 首版仅支持 `fillet.delete`。
- 本机单用户运行，NX 并发为 1。
- STEP 装配 occurrence 的完整身份尚未实现；首版优先支持单实体/扁平模型。
- 大模型使用 Chat Completions 兼容接口。
- 上传阶段暂不判断候选圆角面，当前模型中的所有 BREP 面都可以选择；是否能够按圆角删除由后续 NX 操作确认。
- 删除策略由固定 journal 执行，默认 `FaceEdgeBlend + Cliff + DeletePartialBlend=False`；`DeletePartialBlend` 始终关闭且不对外暴露，失败后只可改变 Cliff/Notch 或缩小目标子集。
- 默认最多尝试 6 次，可通过 `STEP_WEB_MAX_DELETE_ATTEMPTS` 调整为 1–12。
- 浏览器 mesh 使用 JSON，适合当前数百面模型；大型装配后续应改为 GLB 和分块加载。
