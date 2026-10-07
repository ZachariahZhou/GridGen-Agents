# 快照筛选说明

本目录按文件清单复制，未复制整个研究工作区。M87 是研发里程碑，`0.1.0` 是 Python 包版本；本次整理没有将其递增为新的算法版本。

## 包含

- 当前 `src/feeder_agents/` 中全部 100 个 Python 模块和 18 个运行时 JSON 文件。模块间存在运行期及工具调用依赖，因此保留运行时完整性。
- 网页入口、安装配置、空密钥的环境变量模板、直接依赖版本记录。
- 7 个可执行配置和 19 个选定测试文件，以及 1 个共享测试辅助文件：覆盖生成、求解、导出、需求、反馈、记忆、网页与绘图。
- 2 个运行检查脚本、简洁文档、两张已有图件以及已有上游许可文本。

## 排除

- 原 `.git`、`.env`、虚拟环境、缓存、构建残留和本地 IDE 配置。
- 原 `workspace/` 中全部模型批次、API 原始响应、记忆数据库、日志、检查点和实验快照。
- 原根目录 `reference_data/` 原始馈线库及 `data/` 中完整提取结果。
- 历史里程碑脚本、大部分历史测试、全部论文写作草稿和过程报告。
- 原 README 和 PROJECT_CONTEXT 的历史流水账；本目录重写面向使用者的文档。

## 来源与代码一致性

原运行时和 `app.py` 按字节复制，文件 SHA-256 在 [SNAPSHOT.json](../SNAPSHOT.json) 中核对。只调整发布元数据、说明、配置示例、CI 和文件布局，不改生成/规划/修复算法。

两个历史命名的关键测试改为功能命名：`test_m69_distribution_matpower.py` → `test_distribution_matpower.py`，`test_m87_transmission_equipment.py` → `test_transmission_equipment.py`。3 个测试文件的辅助对象导入改为 `planning_fixtures.py`，该文件只提取原历史测试中的必要模型替身和方案构造函数；测试断言保持不变，不要求附带整套历史测试。

输电示例根据已有 M87 规格提炼，固定种子 41，包含 37/139/237 母线。农村聚落示例为 37 母线的新小型演示配置。两张展示图保留此前 M84/M87 模型来源说明，未将其当成本次测试结果。

## Git 仓库

目标仓库为 [ZachariahZhou/GridGen-Agents](https://github.com/ZachariahZhou/GridGen-Agents)，默认分支 `main`。本目录从该仓库克隆，保留初始提交 `c91c8c200f877b9abbce9998189f4807ef1051bf` 及其 Apache-2.0 `LICENSE`，再加入经过筛选的 M87 文件。

接入仓库只调整项目展示名称、仓库地址、许可声明及软件包分发元数据；Python 包名、CLI 命令、算法代码和测试断言保持。具体提交版本以 Git 历史为准。第三方数据的公开分发范围仍按 [THIRD_PARTY.md](../THIRD_PARTY.md) 单独处理。
