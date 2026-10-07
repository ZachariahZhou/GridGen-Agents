# GridGen-Agents

面向科研的电网算例生成工具：自然语言或结构化需求输入，输出可调用的 OpenDSS / MATPOWER 模型、电气验证记录和交互拓扑图。

项目仓库：[ZachariahZhou/GridGen-Agents](https://github.com/ZachariahZhou/GridGen-Agents)。本目录是 **M87 研发里程碑的独立代码快照**，软件包版本为 **0.1.0**。保留当前运行时实现，精选配置、测试和文档；不包含历史实验工作区。Python 包名和 CLI 命令保留为 `feeder-agents`。

原创代码采用 [Apache License 2.0](LICENSE)。第三方依赖、参考案例和派生设备数据保留各自的适用条件，详见 [第三方来源说明](THIRD_PARTY.md)。

## 可以做什么

- 城市、农村单电压配电馈线，支持平衡和逐相不平衡模型、负荷及等效光伏接入。
- 中压—配变—低压—用户的多电压模型，支持架空、直埋、穿管及分区配置。
- 单电压或多电压平衡交流输电网，包含发电机和变压器，导线参数采用条件联合选型。
- LangChain 工具调用、LangGraph 执行与反馈循环；在许可范围内修改设备或局部拓扑，求解后接受或回退。
- 需求检查、设计文档规则提取、项目记忆及经过复验的修复经验。
- 单案例、批量生成、指定工况下的目标搜索、CLI 和 Streamlit 网页。

输出是科研网络模型。8760 小时时序、动态仿真、保护配合、真实 GIS 复原和通用 OPF/N−1 设计不属于当前交付能力。软件不会保证任意需求都可实现。

## 安装

目前验证平台为 Linux，Python 3.11 及以上。部分进程锁使用 `fcntl`，Windows 用户可在 WSL 中使用；未将 Windows 原生运行列为已验证平台。

在本目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,transmission,plots]'
feeder-agents --help
```

仅使用配电 CLI 时可以安装 `python -m pip install -e .`。`transmission` 增加 PYPOWER/SciPy，`plots` 增加 Matplotlib，`dev` 增加 pytest。[requirements-tested.txt](requirements-tested.txt) 记录本次检查所用的直接依赖版本，不是完整的跨平台锁文件。

## 无 API 密钥试用

结构化入口在本地生成、求解和导出，不调用外部 LLM。以下配置每个只生成一条案例：

```bash
feeder-agents generate --spec examples/distribution_radial.yaml --id radial_demo
feeder-agents generate --spec examples/distribution_villages.yaml --id villages_demo
feeder-agents hierarchy --spec examples/hierarchical_urban.yaml --id urban_demo
feeder-agents transmission --spec examples/transmission_37.yaml --id transmission_demo
```

更多示例：

| 配置 | 内容 | 命令 |
|---|---|---|
| [distribution_radial.yaml](examples/distribution_radial.yaml) | 25 母线、10 kV、主干与分支 | `generate` |
| [distribution_villages.yaml](examples/distribution_villages.yaml) | 37 母线、10 kV、3 个村落、三相不平衡 | `generate` |
| [hierarchical_urban.yaml](examples/hierarchical_urban.yaml) | 城市、10/0.4 kV、3 台配变、24 个用户 | `hierarchy` |
| [hierarchical_rural.yaml](examples/hierarchical_rural.yaml) | 农村、10/0.38 kV、3 台配变、24 个用户 | `hierarchy` |
| [transmission_37.yaml](examples/transmission_37.yaml) | 37 母线、110 kV 输电 | `transmission` |
| [transmission_139.yaml](examples/transmission_139.yaml) | 139 母线、500 kV 输电 | `transmission` |
| [transmission_237.yaml](examples/transmission_237.yaml) | 237 母线、500/220 kV 输电 | `transmission` |

默认输出在本目录的 `workspace/` 中。重复使用相同 ID 用于恢复同配置任务；改参数后应换 ID。输出目录不进入代码仓库。完整字段以对应 Pydantic schema 为准，见 [架构说明](docs/architecture.md)。

## 自然语言与网页

```bash
cp .env.example .env
# 编辑 .env，填写自己的 API 密钥、服务地址和可用模型名称
feeder-agents design '生成一条10kV农村配电馈线，准确37个节点，聚集成3个村落，总负荷480kW，其余使用研究默认条件。' --id language_demo
python -m streamlit run app.py
```

网页默认地址为 `http://localhost:8501`。从本目录启动，模型配置会读取当前目录的 `.env`；进程环境变量优先，修改文件后下一次模型调用会重新读取。模板中的 `qwen3.7-plus` 是示例名称，实际可用性取决于你的模型服务。

自然语言规划和显式启用的 Agent 反馈会调用所配置的外部模型。结构化命令默认不调用；详细接口和运行记录见 [使用说明](docs/usage.md)。

## 可视化

![代表性网络](docs/assets/network_overview.png)

上图是此前已保存案例的展示：配电来自 M84，输电来自 M87，并非本次发布检查的新生成样本，也不用于估计成功率。完整模型保留所有母线和支路；图中位置是显示布局，不代表实际地理距离。61 母线例采用简化单环结构。

![中低压分层](docs/assets/hierarchy_detail.png)

## 检查与复现

```bash
python scripts/release_smoke.py --transmission
python scripts/check_examples.py
python -m pytest -q
```

上述测试不需要真实模型密钥。涉及 Agent 决策的回归使用可控模型替身，底层生成器和 OpenDSS/PYPOWER 仍实际运行；不等同于新的自然语言端到端实验。

本次独立安装与检查结果见 [验证记录](docs/verification.md)。CI 配置见 [.github/workflows/package-check.yml](.github/workflows/package-check.yml)，远端运行状态以 [GitHub Actions](https://github.com/ZachariahZhou/GridGen-Agents/actions/workflows/package-check.yml) 为准。

## 目录

```text
src/feeder_agents/   当前运行时与内置 JSON 规则/设备目录
app.py              Streamlit 网页入口
examples/           7 个小型输入配置
scripts/            安装烟雾检查与示例检查
tests/              精选的关键回归测试
docs/               架构、用法、验证记录及两张预览图
third_party/        已保留的上游许可文本
SNAPSHOT.json       来源映射、版本和文件校验值
```

筛选范围见 [快照说明](docs/snapshot-scope.md)。完整运行时中的 `benchmark/`、`paper/` 是评测功能模块；历史实验脚本、私有评分数据和已生成的实验结果均未附带。
