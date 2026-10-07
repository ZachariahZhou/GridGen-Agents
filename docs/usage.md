# 使用与输出

所有命令从发布目录执行，先按 [README](../README.md) 安装。示例配置不需要原研究目录和外部参考数据文件。

## 入口与工作区

`--workspace` 是全局参数，需要放在子命令之前，例如：

```bash
feeder-agents --workspace workspace/demo generate --spec examples/distribution_villages.yaml --id villages
feeder-agents --workspace workspace/demo hierarchy --spec examples/hierarchical_rural.yaml --id rural_lv
feeder-agents --workspace workspace/demo transmission --spec examples/transmission_139.yaml --id tx139
feeder-agents --workspace workspace/demo transmission --spec examples/transmission_237.yaml --id tx237
```

命令输出 JSON 摘要；验收失败也会保留证据，不要只用进程是否结束来判断案例是否合格。关注 `accepted`、逐项验证与错误记录。

| 类型 | 默认工作区内的位置 | 模型文件 |
|---|---|---|
| 单电压配电 | `experiments/<id>/sample_00000/` | `feeder.json`、`opendss/Master.dss` |
| 多电压配电 | `hierarchical_experiments/<id>/sample_00000/` | `feeder.json`、OpenDSS 模型 |
| 输电 | `transmission_experiments/<id>/sample_00000/` | `case.json`、`case_generated.m` |
| 自然语言设计 | `projects/<project>/designs/<id>/` | 方案与结果索引，生成模型位置由 `outcome.directory` 给出 |

模型文件、验证 JSON、可视化 HTML 和元数据应一起保存。MATPOWER 导出用于平衡模型；逐相不平衡配电使用 OpenDSS。查看每次运行实际生成的文件和能力说明，不把一个格式视为能表达全部模型。

## 模型配置

`.env.example` 只包含变量名、示例地址及示例模型名，没有密钥。复制为 `.env` 后填写自己的值。`FEEDER_MODEL` 选择模型，`OPENAI_API_KEY` 提供密钥，`OPENAI_BASE_URL` 指向兼容服务；进程环境优先于当前目录的 `.env`。

自然语言只形成草案：

```bash
feeder-agents design '生成一条10kV农村配电馈线，准确37个节点，3个村落，总负荷480kW。' --id rural_draft --draft
```

`--draft` 仍会调用 LLM 解释需求，但不执行电气生成。`--nodes 37` 可以显式指定总母线数；原文与该值冲突时应修正输入，不应默默覆盖。

## 反馈和经验

```bash
feeder-agents hierarchy --spec examples/hierarchical_urban.yaml --id urban_feedback --agent-feedback
feeder-agents transmission --spec examples/transmission_37.yaml --id tx_feedback --agent-feedback
```

这两条命令启用外部模型参与失败诊断和允许的修复。多电压配电的 `--local-topology` 还允许同配变供区内的局部用户重接，具体仍受请求和动作约束；输电拓扑许可由其规格字段控制。

单电压配置通过 `repair_policy.strategy` 选择 `none`、`fixed`、`heuristic` 或 `agent`。只有 `agent` 策略使用真实 LLM 选择修复。自然语言规划记忆由 `design --planning-memory learn|read_only|off` 控制，默认 `learn`。

新建工作区初始没有学习记录。经验通过运行积累；未经复验的失败记录不能作为成功修复知识。复制整个用户工作区会同时复制其输入、记忆和模型记录，因此不作为本代码快照的一部分。

## 内置参考与规则

```bash
feeder-agents references
feeder-agents styles
feeder-agents rules --query 电压
```

参考案例及派生参数已经包含在包内 JSON 中。原始提取脚本和下载库未包含；`source_path`、来源 URL、提交号和校验值用于溯源，不要求本机存在原始目录。来源与分发状态见 [THIRD_PARTY.md](../THIRD_PARTY.md)。

## 常见问题

- 导入报错：确认当前虚拟环境安装了本目录的包；输电需要 `transmission` 可选依赖。
- 模型认证失败：核对当前启动目录的 `.env`、进程环境以及服务可用模型名；不要把密钥写进 YAML 或提交到仓库。
- 结果未通过：查看失败的需求或电气检查项，区分需求矛盾、能力不支持、服务错误及搜索未达标。
- 查看图时位置变化：拓扑布局只影响显示。低压台区视图是筛选显示，不是新的等值网络。
