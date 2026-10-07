# 第三方来源与分发状态

GridGen-Agents 的原创代码采用根目录 [Apache License 2.0](LICENSE)，该文件沿用 GitHub 仓库的初始提交。本文件说明第三方材料的来源和许可范围；项目的 Apache-2.0 许可不重新授权第三方代码、参考案例或派生设备数据。不能将“公开可下载”视为可任意再分发。

## 内置数据

| 包内文件 | 来源与用途 | 保留方式 |
|---|---|---|
| `matpower_references.json`、`taxonomy_references.json` | MATPOWER 文献/参考案例及其统计 | 保留案例来源字段、提交号及现有来源文本；另附 [MATPOWER LICENSE](third_party/MATPOWER_LICENSE.txt) |
| `calibration_epri_dpv_j1_k1.json`、`equipment_epri.json`、`equipment_joint.json` | GRIDAPPSD/Powergrid-Models 中 EPRI DPV 馈线派生统计及设备观测 | 保留已有 provenance、source、permission、哈希及抽取证据 |
| `equipment_epri_large.json` | tshort/OpenDSS 中 EPRI 测试馈线设备记录 | 保留来源仓库、提交号、文件摘要及 source_text |
| `lv_conductors.json`、`lv_underground.json` 等 | 公开设备资料及项目的设备映射 | 保留各条记录内的原始来源和单位说明 |
| `sources.json`、`rules.json`、`design_requirements.json` 等 | 公开条款摘要、来源元数据和项目研究约束 | 区分来源条款、元数据和建模假设 |

**MATPOWER 上游许可明确说明：其 BSD 许可证覆盖代码，不自动覆盖案例数据。** 因此附带该许可不表示本快照全部参考案例已经获得 BSD 授权。`equipment_epri.json` 的现有 permission 字段描述研究/学习使用条件，也不自动等于整个派生目录的通用开源授权。公开分发前应按实际材料确定适用条件。

包内目录保留运行所需数据，是为了让当前本地候选目录独立运行。原始下载库、完整报告和其他提取中间产物没有复制。数据中的 `source_path` 仅作来源标识，不是对本地原研究目录的运行依赖。

## Python 依赖

LangChain、LangGraph、OpenDSSDirect.py、NetworkX、Pydantic、Streamlit、PYPOWER、SciPy、Matplotlib 等通过包管理器安装，没有将其代码或虚拟环境放入本目录。具体依赖及版本范围见 `pyproject.toml`；使用与再分发按各项目自身许可处理。

## 图件

`docs/assets/` 的两张 PNG 是本项目此前生成模型的绘图。它们用于展示绘图效果，包含 M84 配电和 M87 输电示例，不是外部实网地图，也不是新的成功率测试数据。
