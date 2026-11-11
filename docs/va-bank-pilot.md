# 转账任务 pilot：需要你填写什么

当前代码已经完成离线验证；这份模板没有选择模型，也没有启动真实调用。
原环境中的邮箱、收款方和账户都是模拟材料，不需要提供真实邮箱或转账账户。

## 1. 编辑一个配置文件

仓库根目录执行：

```powershell
Copy-Item configs/va_bank_pilot.example.yaml configs/va_bank_pilot.local.yaml
```

随后编辑 `configs/va_bank_pilot.local.yaml`。这个本地文件已被 Git 忽略。
需要填写／确认：

| 配置 | 你要提供的内容 |
|---|---|
| `actor.provider`、`actor.model_id` | 主agent的服务商与完整模型ID；当前provider取`openai`或`anthropic` |
| `assessment.provider`、`assessment.model_id` | 评估agent的服务商与完整模型ID；允许与actor相同 |
| 两个`api_key_env` | 保存相应key的环境变量名；模板已给出名称，无须改成key的值 |
| `repetitions_per_goal` | 每种目标重复几次；起步建议1 |
| `max_actor_turns` | 每条执行最多几次actor回复；起步建议40 |
| 两个`max_output_tokens` | 每次模型回复的最大输出；模板建议actor4096、assessment8192 |
| 两个`temperature` | 模板建议0；所选模型必须实际支持显式温度参数 |

这些数值是可编辑的起步建议，不是已经选定的正式实验预算。
正常／违规目标以及普通／VA报告自动共用同一个actor配置，无须准备四个actor。
评估模型需要能输出较长的结构化JSON；actor需要支持工具调用及完整工具历史。
现有适配器不能保证任意最新模型兼容，未知或不支持的参数会显式报错，不会自动换模型。
第三方中转、自建地址没有单独的YAML字段；若使用它们，应先确认连接与记录方式。

## 2. 在运行终端设置API key

在PowerShell中输入，key不会回显，也不会写进模板或Git：

```powershell
$vaActorSecret = Read-Host "Actor API key" -AsSecureString
$env:VA_ACTOR_API_KEY = [System.Net.NetworkCredential]::new('', $vaActorSecret).Password
$vaAssessmentSecret = Read-Host "Assessment API key" -AsSecureString
$env:VA_ASSESSMENT_API_KEY = [System.Net.NetworkCredential]::new('', $vaAssessmentSecret).Password
Remove-Variable vaActorSecret, vaAssessmentSecret
```

两个角色可以使用同一个账户／同一个key；也可以分别使用不同服务商的key。
如果复用同一个key，可以把两个`api_key_env`改成同一个环境变量名，只设置一次。
环境变量应设置在之后执行命令的同一个终端。程序不会自动加载`.env`文件。

## 3. 预算的含义

模板的`repetitions_per_goal: 1`意味着：

- 正常目标和违规目标各执行1次，共2条真实模型执行轨迹；
- 每条冻结轨迹分叉普通／VA一次报告，共4份报告；
- 每份报告独立评价，共最多4次报告评估；转账任务的主／副结果由程序规则判定。

按40轮上限计算，最多80次执行阶段逻辑模型请求，加4次报告、4次评估，
共最多88次逻辑生成请求。实际通常会提前结束；供应商token计数请求及底层重试另记。
执行轮数不是工具调用数，一条actor回复可能调用多个工具。

**当前程序没有美元硬限额，也没有独立输入token预算字段。**
不要向YAML添加`usd_budget`、`max_cost`等未实现字段；它们会被拒绝。
输入费用取决于完整历史长度，不能仅靠输出token上限估算总费用。
你还需要自行确定可接受的美元总预算，并在服务商支持的项目／账户预算机制中设置控制；
若只有告警，应按告警处理，不能当成硬停止。准确金额需在选定模型后按其实际计价估算。

这轮只检查可运行性、任务完成／副目标诱发、报告与标注可用性。
2条执行不足以判断VA效应；先检查结果再决定扩样本。

## 4. 安装、校验、运行

首次在仓库根目录创建环境（已用Python3.13验证）：

```powershell
python -m venv .venv-va
.\.venv-va\Scripts\python.exe -m pip install -r requirements-va.txt
```

填写模型后，先运行离线配置校验；这一步不创建SDK客户端、不发API请求，
也不检查模型是否在你的账户可调用：

```powershell
.\.venv-va\Scripts\python.exe run_va_experiment.py validate --config configs/va_bank_pilot.local.yaml
```

你确认配置和预算后，自行执行下面的真实调用命令：

```powershell
.\.venv-va\Scripts\python.exe run_va_experiment.py run --config configs/va_bank_pilot.local.yaml
```

每次`run`都会新建执行并可能计费，不是续跑命令。
结果位于`va_runs/bank_pilot/run-<时间>-<ID>/`，该目录已被Git忽略。
先查看`summary.json`、`pairs.csv`、`branches.csv`，再用`journal.jsonl`与快照核对
主／副任务结果、实际违规、报告缺失、截断和待复核标注。
仅重算已有结果的统计可使用`summarize`子命令（以下命令无模型请求）：

```powershell
.\.venv-va\Scripts\python.exe run_va_experiment.py summarize --run-dir va_runs/bank_pilot/实际运行目录
```

完整产物、盲评、重新评估和人工审计说明见[VA使用说明](va-experiment.md)。
