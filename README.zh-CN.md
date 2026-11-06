# Defeature

中文 | [English](README.md)

Defeature 是一个在本地运行的 STEP 模型去特征工具，结合 Siemens NX、Python/OCC
和 DeepSeek Harness（DSH）CAD 工作台。几何修改结果先作为候选版本提供给用户审查，
由用户决定接受或拒绝。

## 主要功能

- 在浏览器中查看 STEP 几何、选择面并检查圆角组件。
- 使用 NX 尝试删除圆角，在接受或拒绝前查看候选结果。
- 通过常驻 NX worker 执行支持的操作，减少反复启动 NX 的开销。
- 使用 DSH 插件的 CAD 工具和技能，或运行独立 Web 应用。

生产 Agent 只开放受限的 CAD 工具。显式启用的开发预设还提供 shell、源码编辑和任意
NXOpen 执行能力，不是处理不可信提示或文件的沙箱。无法确认回滚成功时，系统会隔离
worker 并阻止发布候选结果。

## 环境要求

- NX 操作需要 Windows x64、本地 Siemens NX 和有效许可证；开发过程中使用过 NX2406。
- DSH 插件需要 Node.js 22.19 或更高版本及 DSH Web；已测试的 DSH 版本见插件指南。
- 兼容的 Python 环境，包括 Python/OCC 和后端依赖。插件可以在用户确认后提供独立环境安装。

项目不包含 NX、专有 SDK 文件或许可证凭据，也不保证兼容所有 NX 或 DSH 版本。

## 文档入口

- [DSH 插件安装和打包指南](dsh-plugins/defeature-ui/README.md)
- [独立 Web 应用指南](webapp/README.md)
- [常驻 NX 运行时协议](docs/persistent_nx_runtime.md)
- [受控 CAD 工具契约](docs/nx_controlled_cad_tools.md)

## 目录结构

```text
.
|-- cad/                       # NX journal、圆角处理和 OCC/STEP 工具
|-- dsh-plugins/defeature-ui/  # DSH 插件、预设和发布打包
|-- webapp/                    # 独立 Web 前后端
|-- docs/                      # 协议、计划和项目记录
|-- scripts/                   # 仓库维护与性能测试脚本
`-- data/                      # 本地模型数据和相关工具
```

原独立 `DeFeature` 流水线和 `Dedraft` 工作流已退役。独立布尔服务不属于插件发布包。

## 本地构建与测试

在 `dsh-plugins/defeature-ui` 目录下运行：

```powershell
npm ci
npm run build
npm run release
npm run test:package
npm run test:tools
npm run test:mapping
```

生成的文件为 `dist/defeature-dsh-ui-0.1.0.tgz`；构建不会自动发布或上传。
`release-files.json` 指定插件运行时和技能的打包清单。安装方法见
[插件指南](dsh-plugins/defeature-ui/README.md)。

激活兼容的 Python/OCC 环境后，可在仓库根目录运行后端回归测试：

```powershell
python -m pytest webapp/backend/tests -q
```

独立应用的安装与启动方式见 [Web 应用指南](webapp/README.md)。

## 本地配置

可在工作台中设置 NX 路径，或使用 `UGII_BASE_DIR`、`NX_RUN_JOURNAL` 和
`NX_STEP214_EXPORT_DEF`。通过 `DEFEATURE_PYTHON` 指定 Python 解释器。独立 Web 应用
仅在显式设置 `OPENCODE_CONFIG_PATH` 时读取 OpenCode provider 配置。不要将真实密钥提交到仓库。

本地模型、作业、日志、缓存、研究论文、本地 SDK 和公司可执行文件不应纳入版本控制；
明确纳入的 STEP 测试样本除外。
个人材料可保存在 `local-artifacts/`。`.gitignore` 不会清除已经提交的历史内容。

## 发布说明

本仓库正在准备公开；构建不会自动推送。现有历史包含本地 CAD 产物、SDK 副本、公司二进制
及论文，从当前索引移除这些文件并不会清除历史。公开前请审查
[仓库清理记录](docs/repository_cleanup.md)，并确认许可证、第三方材料再分发权限及历史清理结果。
