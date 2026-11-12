# 构建与验证指南

本文用于索引维护和检索评测。普通查询直接使用仓库中的公开索引，接入步骤见 [README](../README.md)。以下命令使用 PowerShell，在仓库根目录运行。

## 仓库目录与脚本

| 路径 | 内容与入口 |
| --- | --- |
| `data/<版本小写>/` | 每个发布版本的 `manual.sqlite` 和 `manifest.json`。仓库根 `data/` 只放版本目录 |
| `config/`、`.env.example` | `releases.json` 登记版本及默认值，`catalog_zh.yaml` 记录中文映射，`mcp.example.toml` 和 `testing-api.example.json` 是客户端与评测模板 |
| `tools/caa_manual_cli.py`、`tools/caa_manual_mcp_server.py` | 用户查询入口，两者共享版本选择和索引查询逻辑 |
| `tools/caa_manual_layout.py`、`caa_manual_query.py`、`caa_manual_source_search.py` | 同目录模块分别负责版本及路径、SQLite 查询与源页定位、私有正文缓存 |
| `tools/build_caa_ai_manual.py`、`caa_manual_projection.py`、`export_public_index.py` | 同目录脚本分别负责扫描 CAADoc、生成查询投影、导出公开数据库及清单 |
| `tools/benchmark_*.py`、`tools/summarize_*.py` | 离线与模型评测、结果脱敏汇总，按本文下方的 `python -m tools.…` 命令运行 |
| `docs/`、`reports/` | 接入及维护说明、可选智能体提示，以及注明版本和日期的既有构建或评测记录 |
| `tests/`、`.github/workflows/tests.yml` | 回归测试、`tests/fixtures/` 中的测试数据及 CI 平台矩阵 |
| `.env`、`.env.r<数字>`、`cache/`、`outputs/` | 仅本机使用的源配置、正文缓存和评测产物，均由 Git 忽略 |

## 构建一个版本

下例构建 R33。将 `$release` 换成注册表中的其他版本，两步命令的其余部分相同。每个版本在仓库与 CAADoc 目录之外使用单独的私有输出目录。

```powershell
$release = 'R33'
$privateOut = Join-Path '<private-build-parent>' $release
python .\tools\build_caa_ai_manual.py build --release $release --caadoc '<caadoc-root>' --out $privateOut
```

配置过 `.env.<版本小写>` 时可以省略 `--caadoc`。构建器检查安装路径里的 `B<数字>` 标记、必需的源文件和已有私有库的版本。挪到自定义目录的 CAADoc 可能没有安装标记，维护者需确认来源。所有版本都必须显式传入 `--out`。

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

沿用上节的 `$release` 和 `$privateOut`，导出器会按注册表定位目标目录，一次写出数据库和同目录清单。

```powershell
python .\tools\export_public_index.py --release $release --source-db (Join-Path $privateOut 'data/manual.sqlite')
python .\tools\caa_manual_cli.py --release $release status
```

导出器读取完整数据库的版本元数据，自动采用其旁边已有的 `manifest.json`。版本或来源字段冲突时，导出会在替换目标库前失败。公开产物保留目录、签名、别名、证据及示例定位，清空官方正文并重建 FTS。向仓库外另存副本时可显式指定 `--output-db` 和 `--output-manifest`。

### 接入下一版 CAADoc

1. 在 `config/releases.json` 的 `releases` 下增加 `R<数字>` 项，填写文档的 `source_baseline` 和 `version_policy`。`default_release` 控制未显式选库时的默认值。
2. 从同版本 CAADoc 按上面两步构建并导出。核对清单和 `status` 的版本、API 页数、成员数，再读一个已知成员或重载的原文。新版 HTML 或目录格式可能需要调整解析器。
3. 运行下方单元测试，将注册表和 `data/<版本小写>/` 中的一对公开产物一同提交。需要同时接入 MCP 时，参照模板增加一个指定 `--release` 的服务器。

### 私有正文缓存

```powershell
python .\tools\caa_manual_cli.py --release $release index-sources
python .\tools\caa_manual_cli.py --release $release search-source 'boundary of the shell' --limit 5
```

缓存位于 `cache/<版本小写>/source-search.sqlite`。来源未知的自定义数据库使用 `cache/unknown/`。缓存 schema 2 校验数据库 SHA256 及源根目录，查询不会自动重建。旧缓存的迁移位置见 [README](../README.md#原文术语与技术文章检索)。

## 不调用模型的验证

```powershell
python -m unittest discover -s tests -v
python -m tools.benchmark_retrieval
python -m tools.benchmark_tool_stability --repeats 100 --workers 8
python .\tools\caa_manual_cli.py status
python .\tools\caa_manual_cli.py search CATGeoFactory --limit 3
```

单元测试使用合成源页，不需要安装 CATIA 或配置模型 key；离线检索诊断使用公开数据库，继承及正文检查需要本机 CAADoc。CI 配置覆盖 Windows / Linux 与 Python 3.10 / 3.13。

对每个发布版本分别执行 `--release <版本> status`、`search` 和 `read-source`，核对 `metadata.source_release` 及目标页。MCP 同样逐个服务器验证工具列表与查询。`status` 从所选数据库的同目录读取 `manifest.json`。既有模型评测各自保留原始版本与验收口径。

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
