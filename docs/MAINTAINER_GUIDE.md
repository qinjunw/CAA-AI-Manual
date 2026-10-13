# 构建与验证指南

本文用于索引维护和检索评测。普通查询直接使用仓库中的公开索引，接入步骤见 [README](../README.md)。以下命令使用 PowerShell，在仓库根目录运行。

## 本地构建

### 生成完整数据库

完整构建包含官方正文、审计关系、JSONL 数据和 SQLite 全文索引，输出目录应位于仓库外：

```powershell
python .\tools\build_caa_ai_manual.py build `
  --caadoc "<caadoc-root>" `
  --out "<private-build-root>"
```

省略 `--release` 时默认构建 R21。构建 R33 必须显式指定版本，并使用独立的私有输出目录：

```powershell
python .\tools\build_caa_ai_manual.py build `
  --release R33 `
  --caadoc "<r33-caadoc-root>" `
  --out "<private-r33-build-root>"
```

构建器会检查安装目录中的 `B21` / `B33` 版本标记；从 `B33` 来源构建却声明 R21 会失败。R33 完整构建拒绝写入仓库及其子目录；已有输出库属于其他版本或版本未知时也拒绝覆盖，需指定另一个私有输出目录。没有安装目录标记的自定义路径仍须由维护者确认版本，不能将 `--release` 当作版本转换。

构建器扫描目录与符号索引、refman 和 Automation 页面、在线文档及 `.edu` 示例，然后生成：

```text
<private-build-root>/
  data/manual.sqlite
  data/*.jsonl
  data/manifest.json
  reports/summary.md
```

中文目录、能力标签和查询词始终读取项目内的 `config/catalog_zh.yaml`；当前配置来源是 R21，以 `catalog_source_baseline` 单独记录，不覆盖所选文档的 `source_release` / `source_baseline`。构建产物中的 `source_tree_sha256` 记录 `Doc/generated/refman/_index/jsTree.js` 指纹，仅用于层级文件匹配，不证明每个源页或 CAA 运行兼容性。完整数据库和本机正文缓存包含官方文本，不能作为公开索引提交。

### 生成公开索引

维护者可以从完整数据库导出不含官方正文的索引版。以下命令会更新仓库中的公开数据库和 manifest，执行前检查 Git 状态并保留现有改动：

```powershell
python .\tools\export_public_index.py `
  --source-db "<private-build-root>/data/manual.sqlite" `
  --output-db ".\data\manual.sqlite" `
  --source-manifest "<private-build-root>/data/manifest.json" `
  --output-manifest ".\data\manifest.json"
```

R33 导出到独立子目录，保留默认 R21 文件：

```powershell
python .\tools\export_public_index.py `
  --source-db "<private-r33-build-root>/data/manual.sqlite" `
  --output-db ".\data\r33\manual.sqlite" `
  --source-manifest "<private-r33-build-root>/data/manifest.json" `
  --output-manifest ".\data\r33\manifest.json"
```

导出结果保留目录、API、成员、签名、别名、证据位置和示例位置，并重建公开检索使用的 FTS 索引。源 manifest 的版本字段必须与源数据库一致；不一致时会在替换输出数据库前拒绝导出。导出后运行下方单元测试；其中的文档测试会检查公开数据库的内容模式和正文是否清空。

### 重建私有正文缓存

保留 R21 默认配置，按 [README](../README.md) 在独立 `.env.r33` 中配对 R33 数据库与同版本 CAADoc，再显式运行 `index-sources`：

```powershell
python .\tools\caa_manual_cli.py --env-file .env.r33 index-sources
python .\tools\caa_manual_cli.py --env-file .env.r33 search-source "boundary of the shell" --limit 5
```

CLI 的 `--env-file` / `--db` 必须放在子命令前；`--db` 只选库，不切换源目录。进程环境变量仍优先于配置文件。R33 缓存位于 `cache/source-search-r33.sqlite`；R21 和来源未知的旧库使用 `cache/source-search.sqlite`。缓存 schema 2 绑定数据库 SHA256 与源根目录，旧缓存或另一数据库的缓存不能直接复用；遇到 `not_indexed` / `source_root_changed` 时，核对选库与源配置，再重建缓存。查询不会自动构建或迁移缓存。

## 不调用模型的验证

```powershell
python -m unittest discover -s tests -v
python -m tools.benchmark_retrieval
python -m tools.benchmark_tool_stability --repeats 100 --workers 8
python .\tools\caa_manual_cli.py status
python .\tools\caa_manual_cli.py search CATGeoFactory --limit 3
```

单元测试使用合成源页，不需要安装 CATIA 或配置模型 key；离线检索诊断使用公开数据库，继承及正文检查需要本机 CAADoc。CI 配置覆盖 Windows / Linux 与 Python 3.10 / 3.13。

R33 的 CLI 冒烟检查使用 `--env-file .env.r33 status` 和相同配置参数的 `search` / `read-source`；确认 `metadata.source_release="R33"`，并实际读取目标页。保留上方默认入口的 R21 检查，确保两版均可查询。MCP 并存时分别验证 `caa_ai_manual` 与 `caa_ai_manual_r33`，每个服务器应提供六个工具。`status` 从所选数据库同目录读取 `manifest.json`；索引检查成功不证明源目录可读。既有模型评测报告保留原来源与验收口径，不作为 R33 的测量结果。

## 付费模型评测（可选）

这些命令向官方 DeepSeek API 发送任务及必要文档片段，会产生模型费用。运行前确认本机 CAADoc 可读、文档许可和数据处理要求允许发送片段，并设置测试范围及预算。CLI / MCP 查询和单元测试不需要以下凭据。

### 凭据格式

将 [config/testing-api.example.json](../config/testing-api.example.json) 复制到仓库外的私有位置，只在私有副本中填写 key，并将文件路径传给 `--credentials`。脚本读取顶层对象中第一个含非空 `testingAPIKey` 的子对象，因此需要以下嵌套结构：

```json
{
  "key_1": {
    "testingAPIKey": "<your-test-api-key>"
  }
}
```

`key_1` 名称可自定；扁平的 `{"testingAPIKey": "..."}` 不符合当前读取逻辑。不要将真实 key 填入仓库模板、命令行参数或提交记录。密钥从私有文件读入内存；回答与调用摘要写入被忽略的 `outputs/`。

### 已知问题诊断

```powershell
python -m tools.benchmark_agent_retrieval --credentials "<private-key-file>" --repeats 2
```

该命令比较原始 HTML 读取、旧版手册和当前手册；每题最多 6 轮、每轮最多 1,600 输出 token。默认使用 `deepseek-flash`，模型可通过 `--model` 指定。这些已知缺陷题是诊断集，不是独立留出的通用能力评测；答案是否正确需逐项核查，不能用“模型输出了答案”代替通过率。详见 [迭代验证记录](../reports/retrieval-iteration.md)。

### 带字段验收的重复测试

```powershell
python -m tools.benchmark_agent_stability --credentials "<private-key-file>" --split all --models deepseek-flash deepseek-v4-pro --json-mode --repeats 2 --token-budget 1800000 --label stability-run
```

题库包含 12 类问题、每类两种表达；预期答案不会发送给模型。该命令最多发起 96 个案例，每例默认 6 轮、每轮 1,200 输出 token，默认 2 路并发；达到累计 token 预算后停止新请求，在途请求仍可能使总量超过预算。不得用相同 `label` 覆盖旧结果。完整回答和原文留在忽略的 `outputs/`，公开报告只保留测量数据。验证范围与失败案例见 [稳定性测试记录](../reports/agent-stability.md)。

加上 `--use-guide` 可测试 `docs/agent-system-prompt.txt` 中的通用查询指引；它需由调用端显式加入系统提示，并非 MCP 自动注入。`--tasks <id> ...` 用于已知失败题回归；分析过的题目不再算独立留出集。使用 `python -m tools.summarize_agent_stability outputs/<label>.json --out reports/<new-summary>.json` 可导出去除答案和原文的测量摘要。

限定成员引用的跨 Framework 检查、配对模型结果与复核命令见 [精确引用迭代记录](../reports/reference-transfer-results.md)。该轮区分“接口可正确定位”和“模型实际采用新增入口”，不把语法变体数量当作独立知识问答题数量。文档检索测试不能代替 CAA 编译和运行验收。
