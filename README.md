# CAA AI Manual

CAA AI Manual 是面向 CATIA CAADoc 的本地结构化查询项目。仓库包含可直接查询的 SQLite 索引、命令行工具、MCP server，以及从本机 CAADoc 重建索引的脚本。

## 背景

CAADoc 由 API HTML、目录文件、Automation 页面和 `.edu` 示例源码组成。页面分散在多个 Framework 和文档层级中，程序化查询需要稳定的目录、API 身份、成员签名、页面锚点和源文件定位。

## 方案

项目将 CAADoc 组织为 `Layer -> Framework -> API / function-family` 查询投影：

| 组件 | 作用 |
| --- | --- |
| `data/manual.sqlite` | 随仓库发布的 API 结构索引 |
| `config/catalog_zh.yaml` | 中文目录名称、能力标签和查询词 |
| `tools/caa_manual_cli.py` | CLI 查询入口 |
| `tools/caa_manual_mcp_server.py` | stdio MCP server |
| `tools/build_caa_ai_manual.py` | 从本机 CAADoc 生成完整数据库 |
| `tools/export_public_index.py` | 从完整数据库生成公开索引 |
| `cache/source-search.sqlite` | 可选的本机正文检索缓存，包含官方文本，已被 Git 忽略 |

公开数据库的 `content_mode` 为 `index-only`，保留 API 结构、签名、别名和 `caadoc://` 源定位。官方正文和示例源码在查询时从用户配置的 CAADoc 读取。

当前索引以 CATIA V5R21 CAADoc 为来源基线。结构化索引查询响应包含来源基线、版本策略、内容模式和投影 schema 版本；私有正文查询返回缓存构建时间、内容模式和检索范围。

## 快速使用

项目需要 Python 3.10 或更高版本，不依赖第三方 Python 包。

创建本机配置并按照模板设置 CAADoc 根目录：

```powershell
Copy-Item .env.example .env
python .\tools\caa_manual_cli.py status
```

CAADoc 根目录应包含 `Doc/` 和相关的 `*.edu/` 目录。目录浏览、API 检索和结构查询可以直接使用公开索引；源页读取需要可访问的本机 CAADoc。

### 浏览与检索

```powershell
python .\tools\caa_manual_cli.py catalog --parent catalog:root --depth 1
python .\tools\caa_manual_cli.py catalog --parent GeometricObjects --depth 1 --limit 50
python .\tools\caa_manual_cli.py search CATGeoFactory
python .\tools\caa_manual_cli.py search "几何工厂"
python .\tools\caa_manual_cli.py search CreatePlane --framework GeometricObjects
python .\tools\caa_manual_cli.py search "CATBody::GetAllCells"
python .\tools\caa_manual_cli.py search "曲面外插"
```

`search` 按逻辑 API 聚合候选项。`match_score` 表示当前查询与候选项的词法相关度；`match_tier`、`match_reason` 和 `matched_key_en` 说明命中层级、字段和官方英文键。

`Class::Member` 支持声明位置查询；继承链来自本机 `Doc/generated/refman/_index/jsTree.js` 的静态父类记录。没有本机记录时，工具报告 `source_unavailable`，不推断继承关系。中文外插词与 `Extrapol` 词族由配置中的 `query_expansions` 展开，响应披露命中的规则；这些候选映射不代表算子输入等价。

也接受 `Class::Member(Type1,Type2&)` 或 `Class::Member()`，按索引锚点精确定位；支持空白、HTML 实体和全宽字符的表示归一，不删改类型、指针、引用或限定符。它是文档定位语法，不是完整 C++ 重载决议。无匹配时先改用 `Class::Member` 查看候选；索引锚点可能省略原文声明中的限定符。

`Class Member` 也可按限定成员查询，前提是类名已被索引收录；响应通过 `query_interpretation` 披露解释结果。指定 `member` 没有匹配时，`get-api` 返回词法相近的 `member_name_suggestions`，仍需读取候选的实际声明，不能直接作为别名调用。

目录分页返回 `total_count`、`next_offset` 和 `truncated`。使用相同 `parent`、`depth`、`limit`，把 `next_offset` 传给 `--offset` 可继续读取。

### API 与源页

```powershell
python .\tools\caa_manual_cli.py get-api CATGeoFactory
python .\tools\caa_manual_cli.py get-api CATGeoFactory --include members
python .\tools\caa_manual_cli.py get-api CATGeoFactory --include overloads --include evidence --include examples
python .\tools\caa_manual_cli.py read-source CATGeoFactory
python .\tools\caa_manual_cli.py read-source "<member-id>" --max-chars 8000
python .\tools\caa_manual_cli.py read-source "CATBody::GetAllCells" --include-context
python .\tools\caa_manual_cli.py get-api CATBody --include members --member GetAllCells --inherited
python .\tools\caa_manual_cli.py get-api CATGeoFactory --include examples --example-offset 20
```

`get-api` 的 `include` 参数按需返回成员、重载、证据定位和示例定位。函数族包含多个物理页面时，响应返回 `requires_overload_selection` 和候选 `page_id`。

成员默认只列本类声明；`--member` 精确筛选名称，`--inherited` 加入有官方父类记录的声明并标出 `declared_in`。成员按声明类和成员组分页，默认 100 组，上限 500；示例按源 URI 去重后分页，默认 20 个，上限 100。对应参数为 `--member-offset` / `--member-limit` 和 `--example-offset` / `--example-limit`；响应中的 `members_pagination` / `examples_pagination` 给出总数和下一页。示例空结果只表示当前索引没有关联，不证明官方不存在示例。

`read-source` 将 `caadoc://` URI 映射到本机 CAADoc，也接受 URI 后的 `#anchor`。HTML 默认转换为文本，并静态读取 `activateLink` 的字面显示类型；不执行 JavaScript。C++ 文件保留解码后的源码，包括尖括号和换行。`--format raw_html` 返回原始标记；复杂动态页面仍应核对原始 HTML。

响应包含 `source_uri`、`anchor`、`resolved_path`、`encoding`、`source_kind`、`total_chars` 和 `next_offset`。`--offset` 按解码、转换后的字符计数，不是字节或行号；保持 reference、anchor、format 和源文件不变，循环读取直到 `next_offset` 为 null。单次默认 12,000 字符，上限 40,000。`--include-context` 在成员片段之外附上最多 6,000 字符的类级前言，避免遗漏输入拓扑限制；前言截断时应再读取整个类页。

成员重载不唯一时返回 `ambiguous`，使用候选中的 `member_id` 继续。环境文件、数据库和 JSON 凭据配置不属于源页读取支持的文件类型。

### 原文术语与技术文章检索

```powershell
python .\tools\caa_manual_cli.py index-sources
python .\tools\caa_manual_cli.py search-source "boundary of the shell" --limit 5
```

`index-sources` 显式构建私有 FTS 缓存，覆盖索引里的 API 页面、`Doc/online/` HTML 和 `.edu` C++ 文件；缺失文件及超过 2 MB 的文件计入 `skipped_count`。缓存包含官方文本，只留在本机，不能当作公开索引发布。查询不自动构建缓存；更换源根目录需要重建，结果文件变化会标记 `source_changed_since_index`。新增文件、解析器更新或同大小同时间戳替换也需要主动重建。

正文查询先匹配字面短语，无命中时再匹配全部英文词；响应的 `query_mode` 披露采用的方式。FTS 不是中文语义检索。

### 智能体调用顺序

1. 已知 API 或 `Class::Member` 时直接 `read-source`；不要固定执行 search → get-api → read-source 三次调用。
2. 需要区分重载、成员声明类或示例位置时才请求 `get-api` 的相应 include。
3. 只知道用途时先查官方符号、维护者别名；英文原文术语改用 `search-source`。普通索引空结果不是官方不存在该能力的证据。
4. 成员结论同时核对类级限制；按分页字段继续读，保留 URI / anchor。官方页面、安装头文件和运行实测属于不同证据层级，不能互相替代。

## MCP

MCP 配置模板位于 `config/mcp.example.toml`，提供以下工具：

| 工具 | 用途 |
| --- | --- |
| `caa_status` | 查询数据库、内容模式和源目录状态 |
| `caa_catalog` | 浏览目录层级 |
| `caa_search` | 检索 API、成员和中文查询词 |
| `caa_get_api` | 获取 API 结构及扩展内容 |
| `caa_read_source` | 读取官方源页或成员锚点 |
| `caa_search_source` | 检索本机私有缓存的正文术语和技术文章 |

MCP 的分页、成员筛选、继承与上下文参数与 CLI 同名，只把 CLI 参数中的连字符换成下划线。缓存构建只提供 CLI 命令，MCP 查询工具保持只读。

MCP 在执行查询前校验参数类型、必填项、枚举、范围和未知字段。参数错误返回 `isError`、`error_code=invalid_arguments` 及 `next_action`；不会静默忽略拼错的参数。JSON-RPC 的无效消息与合法批处理分别处理，错误消息之后可继续接收查询。接入流程、调用前检查和停止条件见 [智能体查询指南](docs/AGENT_QUERY_GUIDE.md)。

## 本地构建

### 生成完整数据库

完整构建包含官方正文、审计关系、JSONL 数据和 SQLite 全文索引，输出目录应位于仓库外：

```powershell
python .\tools\build_caa_ai_manual.py build `
  --caadoc "<caadoc-root>" `
  --out "<private-build-root>"
```

构建器扫描目录与符号索引、refman 和 Automation 页面、在线文档及 `.edu` 示例，然后生成：

```text
<private-build-root>/
  data/manual.sqlite
  data/*.jsonl
  data/manifest.json
  reports/summary.md
```

中文目录、能力标签和查询词始终读取项目内的 `config/catalog_zh.yaml`。

### 生成公开索引

维护者可以从完整数据库导出不含官方正文的索引版：

```powershell
python .\tools\export_public_index.py `
  --source-db "<private-build-root>/data/manual.sqlite" `
  --output-db ".\data\manual.sqlite" `
  --source-manifest "<private-build-root>/data/manifest.json" `
  --output-manifest ".\data\manifest.json"
```

导出结果保留目录、API、成员、签名、别名、证据位置和示例位置，并重建公开检索使用的 FTS 索引。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m tools.benchmark_retrieval
python -m tools.benchmark_tool_stability --repeats 100 --workers 8
python .\tools\caa_manual_cli.py status
python .\tools\caa_manual_cli.py search CATGeoFactory --limit 3
```

单元测试使用合成源页，不需要安装 CATIA 或配置模型 key；离线检索诊断使用公开数据库，继承及正文检查需要本机 CAADoc。CI 配置覆盖 Windows / Linux 与 Python 3.10 / 3.13。可选的付费智能体诊断需要本机 CAADoc，以及仓库外含 `testingAPIKey` 的私有 JSON：

```powershell
python -m tools.benchmark_agent_retrieval --credentials "<private-key-file>" --repeats 2
```

该命令向官方 DeepSeek API 发送任务及必要文档片段，比较原始 HTML 读取、旧版手册和当前手册；每题最多 6 轮、每轮最多 1,600 输出 token。默认使用 `deepseek-flash`，模型可通过 `--model` 指定。密钥只在内存中使用；回答与调用摘要写入被忽略的 `outputs/`。这些已知缺陷题是诊断集，不是独立留出的通用能力评测；答案是否正确需逐项核查，不能用“模型输出了答案”代替通过率。详见 [迭代验证记录](reports/retrieval-iteration.md)。

带冻结题目和字段验收的重复测试：

```powershell
python -m tools.benchmark_agent_stability --credentials "<private-key-file>" --split all --models deepseek-flash deepseek-v4-pro --json-mode --repeats 2 --token-budget 1800000 --label stability-run
```

题库包含 12 类问题、每类两种表达；预期答案不会发送给模型。该命令最多发起 96 个案例，每例默认 6 轮、每轮 1,200 输出 token，默认 2 路并发；达到累计 token 预算后停止新请求，在途请求仍可能使总量超过预算。不得用相同 `label` 覆盖旧结果。完整回答和原文留在忽略的 `outputs/`，公开报告只保留测量数据。验证范围与失败案例见 [稳定性测试记录](reports/agent-stability.md)。

加上 `--use-guide` 可测试 `docs/agent-system-prompt.txt` 中的通用查询指引；它需由调用端显式加入系统提示，并非 MCP 自动注入。`--tasks <id> ...` 用于已知失败题回归；分析过的题目不再算独立留出集。使用 `python -m tools.summarize_agent_stability outputs/<label>.json --out reports/<new-summary>.json` 可导出去除答案和原文的测量摘要。

限定成员引用的跨 Framework 检查、配对模型结果与复核命令见 [精确引用迭代记录](reports/reference-transfer-results.md)。该轮区分“接口可正确定位”和“模型实际采用新增入口”，不把语法变体数量当作独立知识问答题数量。

## 数据来源

CATIA、CAA 和 CAADoc 属于其权利人提供的第三方资料。仓库中的索引用于定位用户本机已有的 CAADoc；本地文档的访问和使用遵循对应安装与许可条款。
