# M87 独立目录检查

日期：2026-10-07。平台：Linux / Python 3.12.7。以下为精简快照整理阶段的运行检查，未调用外部 LLM，也未重跑完整论文实验。接入 GridGen-Agents 仓库时仅更新项目名称、链接、许可及分发元数据；原运行时和测试内容通过 SHA-256 对照保持一致。

## 安装与实际生成

将发布目录复制到项目之外的临时目录，使用已安装的 setuptools PEP 517 后端离线构建 wheel，再以 `pip --no-deps --no-index --target` 安装到独立目录。离开源码目录调用安装后的 CLI 和求解脚本，确认导入位置属于独立安装目录。

运行依赖复用本机已安装的 Python 环境，不宣称本轮验证了全新系统的联网依赖安装。直接依赖版本记录在 [requirements-tested.txt](../requirements-tested.txt)。当前环境缺少 `python -m build` 前端，所以本地采用同一 setuptools 后端直接构建；CI 会显式安装该前端。

| 检查 | 结果 |
|---|---|
| wheel 构建、独立目录安装、CLI 启动 | 通过 |
| 安装包内 Python / JSON | 100 个 Python 模块、18 个运行时 JSON 文件 |
| 平衡配电 OpenDSS 生成与求解 | 通过，9 母线 |
| 不平衡配电 OpenDSS 生成与求解 | 通过，9 母线 |
| 多电压配电生成与求解 | 通过，23 母线 |
| 输电 AC 验证与 MATPOWER 文件重载 | 通过，9 母线 |
| 7 个发布示例配置 | 7/7 通过实际生成及验收 |

示例包括 25/37 母线配电、城乡多电压配电、37/139/237 母线输电。结构化示例没有 LLM 随机性，这些结果不是自然语言成功率。

## 选定回归测试

本次选择 19 个测试文件，共 159 项参数化后的测试，涵盖需求、规划、配置读取、执行恢复、模型生成、电气检查、双格式交付、反馈、记忆、网页和绘图。

最初收集时发现缺少两个历史测试模块中的辅助对象。已将必要对象提取为 `tests/planning_fixtures.py`，不加入整套历史测试。后续完整运行记录为 156 项通过、3 项失败；三个失败均定位到测试替身中遗漏的历史模块导入，诊断记录为 `ModuleNotFoundError`，不是生成器或电气模型错误。

修正最后一处辅助导入后，先对涉及这次整理的三个测试文件重跑，**48/48 通过**，包括先前的三个失败项。提交前又在最终 `GridGen-Agents` 目录完整运行了选定测试集，**159/159 通过，耗时 112.04 秒**；该最终结果来自同一轮完整执行。

这三个文件是 `test_adaptive_planning.py`、`test_distribution_matpower.py`、`test_planning_memory.py`。测试条件和断言保持，只有测试辅助对象的导入位置改变。

## 文件检查

- `src/feeder_agents/` 与 `app.py` 按字节保持原 M87 代码；原目录中的已选文件校验值未变。
- 发布目录不含 `.env`、Git 历史、虚拟环境、缓存、运行数据库或原始实验日志；只有空密钥的 `.env.example`。
- 文件列表、逐文件 SHA-256 及复制来源记录在 [SNAPSHOT.json](../SNAPSHOT.json)。
- 本地文档链接、Python 语法、测试辅助依赖和预览图可读性纳入最终检查。
- 对文本文件检查了常见凭据格式和原机器绝对路径；这是定向文件检查，不等于穷尽所有敏感信息识别。

原始安装/测试日志留在原研究工作区，不混入发布目录；可分享的摘要为 [verification-summary.json](verification-summary.json)。

## 验证范围

本记录覆盖本地发布前检查，GitHub 远端 CI 状态以 [Actions](https://github.com/ZachariahZhou/GridGen-Agents/actions/workflows/package-check.yml) 为准。本轮未进行新的真实 LLM 端到端实验。目标 GitHub 仓库的 Apache-2.0 许可证保持不变；第三方参考数据的公开分发条件仍按 [THIRD_PARTY.md](../THIRD_PARTY.md) 单独处理。仓库接入和分发元数据检查见 [repository-preparation.json](repository-preparation.json)。
