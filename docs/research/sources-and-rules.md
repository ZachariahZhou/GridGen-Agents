# 首批公开来源与规则映射

检索日期：2026-09-22。下面区分法规条文、标准元数据、研究报告和项目假设。当前规则集合是少量可执行研究约束，不是对任何整套规范的合规认证。

| 来源 | 版本/定位 | 可确认内容 | 实现方式 |
|---|---|---|---|
| [国家发展改革委《供电营业规则》](https://zfxxgk.ndrc.gov.cn/wap/iteminfo.jsp?id=20347) | 2024-02-08 发布，2024-06-01 施行；第六、四十五、五十七条 | 额定频率；特定用户的峰值功率因数要求；正常供电受电端电压偏差及适用例外 | 50 Hz 规格校验；条件化 PF 检查；仅 10 kV 正常三相受电端的 0.93–1.07 pu 检查。聚合负荷不自动认定为单个高压用户。 |
| [DL/T 5729—2023 配电网规划设计技术导则](https://std.samr.gov.cn/hb/search/stdHBDetailedCNF?id=2E1288291AD60971E06397BE0A0ABFD2) | 2023-12-28 发布，2024-06-28 实施 | 官方平台确认替代 DL/T 5729-2016，范围为 110 kV 及以下公用交流配网规划 | 仅入来源目录，未获得全文，未提取任何具体技术阈值。后续需合法全文及条款审核。 |
| [PNNL-31580 Distribution System Research Roadmap](https://www.pnnl.gov/main/publications/external/technical_reports/PNNL-31580.pdf) | 2022-02-01，§2.10，印刷页 29 / PDF 第49页 | 讨论辐射、网状及带常开联络点的网络，指出 DER 对设计及保护的影响 | 用作拓扑研究背景；单电源树是本项目首版选择，不冒充通用强制条款。 |
| [NREL/PR-5D00-75723 Realistic Synthetic Distribution Grids: Summary of Validation Results](https://docs.nlr.gov/docs/fy20osti/75723.pdf) | 2019；[官方元数据](https://research-hub.nlr.gov/en/publications/realistic-synthetic-distribution-grids-summary-of-validation-resu/) | 合成配网采用统计、运行和专家三个维度验证 | 本版实现运行检查和基础统计；未完成真实数据标定和专家验证，不宣称 SMART-DS 等级真实性。 |
| [OpenDSSDirect.py Getting Started](https://dss-extensions.org/OpenDSSDirect.py/notebooks/GettingStarted.html) | 检索时文档 | Python 调用独立 DSS 引擎，可跨平台运行 | 实际仿真使用 DSS-Extensions 引擎，记录版本；不是 EPRI Windows 引擎交叉验证。 |

## 条款转为程序时的边界

- 电压检查点是建模的用户受电母线，不将任意内部母线或图示点一概认定为法定受电点。
- 《供电营业规则》第五十七条含功率因数不满足要求的例外。只有明确选择 high_voltage_users、无特殊约定，且模型用户满足 PF 条件时启用此条款的研究映射；聚合负荷使用同数值的项目研究阈值，并明确区别来源。
- high_voltage_users 模式按各模型用户的 contract_kva 判断是否超过 100 kVA；该容量在首版由负荷视在功率估算，保存为建模假设。无特殊约定只用于本研究配置，不能推断真实合同。
- 有 PV 时负荷功率因数不等于受电点净交换功率因数。首版没有用户侧 PV 产权和计量边界，因此该情况下用户 PF 与相关电压条款标不适用，独立保留研究电压检查。
- stress 模式允许保存有运行越限但可求解的有效模型；运行规则依然显示 fail，不将它们改为 pass。
- 载流量、导线序阻抗、线路长度分布、树形生成策略、等效源短路容量都属于研究示例配置，没有从上述法规中推导这些值。

## 增加用户文档的流程

1. 提供有权使用的文本及文档编号、版本、地域、页码/条号。
2. 按电压等级、运行方式、对象及例外切分条款；人工核实 OCR 中单位、数值与比较符号。
3. 添加 source 与候选 rule，标记为 draft，不允许默认启用。
4. 给规则绑定白名单 validator；不要执行文档中任意代码或指令。
5. 用满足、违反、不适用、缺数据四类样例验证；审核后启用新版本。
6. 实验保存规则快照和哈希，旧实验不随规则库更新而改变。

首版只提供已整理来源的本地检索和执行，不包含未经审核的 PDF→生产规则自动发布功能。
