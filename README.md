# CAA AI Manual

CAA AI Manual 为开发者和智能体提供 CATIA CAADoc 的本地结构化检索：按用途发现 API，定位成员或重载，再读取本机官方文档和示例作为调用依据。仓库包含可直接查询的 SQLite 索引、命令行工具、MCP server，以及索引重建脚本。

查询工具不执行 CAA 算子。索引签名用于文档定位，不能替代完整 C++ 声明；生成的调用仍需核对目标版本的头文件，并进行编译和 CATIA 运行验证。

## 背景

CAADoc 由 API HTML、目录文件、Automation 页面和 `.edu` 示例源码组成。页面分散在多个 Framework 和文档层级中，程序化查询需要稳定的目录、API 身份、成员签名、页面锚点和源文件定位。

## 方案

项目将 CAADoc 组织为 `Layer -> Framework -> API / function-family` 查询投影：

| 组件 | 作用 |
| --- | --- |
| `data/r21/`、`data/r33/` | 每版各有 `manual.sqlite` 和 `manifest.json`，默认查询 R21 |
| `config/releases.json` | 登记版本号、文档来源和默认版本 |
| `config/catalog_zh.yaml` | 中文目录名称、能力标签和查询词 |
| `tools/caa_manual_cli.py`、`tools/caa_manual_mcp_server.py` | 命令行和 stdio MCP 查询入口 |
| `tools/build_caa_ai_manual.py`、`tools/export_public_index.py` | 构建私有完整库，导出指定版本的公开索引 |
| `cache/<版本小写>/source-search.sqlite` | 本机可选正文缓存，按版本隔离，由 Git 忽略 |

配置模板、查询模块、评测脚本和私有产物的目录说明见[构建与验证指南](docs/MAINTAINER_GUIDE.md#仓库目录与脚本)。

公开数据库的 `content_mode` 为 `index-only`，保留 API 结构、签名、别名和 `caadoc://` 源定位。官方正文和示例源码在查询时从用户配置的 CAADoc 读取。

仓库保留 CATIA V5R21 和 V5-6R2023（R33）两个独立索引。结构化查询响应的 `metadata.source_release`、`source_baseline` 和 `version_policy` 标明当前库的来源；缺失版本元数据时返回 `unknown` / 未提供，不推断为 R21。中文目录映射仍来自 R21 配置，构建产物用 `catalog_source_baseline` 单独记录其来源，不代表 R33 的运行兼容性。

## 快速使用

项目需要 Python 3.10 或更高版本，不依赖第三方 Python 包。CLI 和 MCP 查询本身不需要模型 API key；智能体客户端的模型认证单独配置。以下示例使用 PowerShell，取得仓库后在仓库根目录运行查询命令。

### 1. 仅查公开索引

```powershell
git clone https://github.com/qinjunw/CAA-AI-Manual.git
cd CAA-AI-Manual
python .\tools\caa_manual_cli.py status
python .\tools\caa_manual_cli.py search CATGeoFactory --limit 3
```

已有仓库时跳过 clone。无需安装 CATIA、创建 `.env` 或重建数据库，即可浏览目录、检索 API 和查询结构。`status` 应返回 `status="ok"`、`metadata.content_mode="index-only"` 和非零 `counts.api_pages`；搜索应包含 `CATGeoFactory` 候选。这只验证索引可读，不证明官方源文件可访问。

默认查询 R21。需要指定版本时，把 `--release` 放在子命令前。两条命令可以在同一终端交替运行。

```powershell
python .\tools\caa_manual_cli.py --release R21 status
python .\tools\caa_manual_cli.py --release R33 search CATGeoFactory --limit 3
```

查看 `status` 的 `db_path` 和 `metadata.source_release` 可以核实实际打开的数据库。`--release` 同时选择该版本的本机源配置，忽略进程中的 `CAA_AI_MANUAL_DB` 和 `CAA_CAADOC_ROOT`。自定义数据库仍可用 `--db` 或 `--env-file`，两者沿用命令行、进程环境、配置文件的优先顺序。`--db` 单独使用时只切换数据库。

**从旧布局升级**　直接用 SQLite、脚本或 wiki 读取 R21 的使用方，请把 `data/manual.sqlite` 和 `data/manifest.json` 改为 `data/r21/` 下的同名文件。CLI 和 MCP 能将配置中已缺失的旧 R21 数据库路径解析到新位置。R33 的公开文件路径保持为 `data/r33/`。

### 2. 读取本机官方原文（可选）

把 [.env.example](.env.example) 复制为 `.env.r21` 或 `.env.r33`，在本机文件中把 `<caadoc-root>` 换成该版本的 CAADoc 目录。目录应包含 `Doc/` 和相关 `*.edu/`。已有 R21 `.env` 可以继续使用。仅查公开索引时可以跳过这一步。

```powershell
python .\tools\caa_manual_cli.py --release R21 read-source CATGeoFactory --max-chars 1200
python .\tools\caa_manual_cli.py --release R33 read-source "CATBody::GetAllCells" --include-context
```

成功读取会返回 `status="ok"`、`caadoc://` 开头的 `source_uri` 和非空 `content`。源目录缺失时仍可用 `search` 和 `get-api` 查索引。

新 R33 库记录 `source_tree_sha256`。读取原文或解析继承前，会核对本机 `Doc/generated/refman/_index/jsTree.js` 的 SHA256；文件缺失或指纹不匹配时拒绝读取，并提示源版本不匹配。该检查只绑定层级文件，不证明所有源页内容一致。没有该字段的旧 R21 库保留原行为，不强制指纹校验。

### 3. 接入智能体（可选）

先完成索引检查；需要官方原文时再完成源页检查，然后按下方 [MCP 接入](#mcp) 配置客户端。模型的查询入口和证据检查顺序见 [智能体查询指南](docs/AGENT_QUERY_GUIDE.md)。

### 交给 Agent 配置

分享时提供整个仓库，保留数据库、配置和 `tools/` 的相对位置。以下文字可交给能够访问本机文件的 Agent。MCP 提供工具定义，查询指南需要单独读取。

```text
请帮我接入当前仓库的 CAA API 查询工具：
1. 先阅读 README.md 和 docs/AGENT_QUERY_GUIDE.md，目标是查找 API 及其官方文档位置。
2. 检查仓库位置、可用的 Python 3.10+ 和我使用的 Agent 客户端；缺少必要信息时再询问，不猜测路径。
3. 确认目标版本，用 --release R21 或 --release R33 分别执行 status 和 search CATGeoFactory，核对返回的版本。标准查询无需重建数据库或配置模型 API key。
4. 确认我是否有同版本的本机 CAADoc；没有时保留仅索引模式。需要读原文时，按 .env.example 配置，再实际执行 read-source 验证。
5. 按实际客户端配置本机 stdio MCP，保留已有配置；config/mcp.example.toml 仅适用于 Codex。客户端不支持本机 stdio 时可使用 CLI；也不能执行本机命令时，报告接入限制。
6. 同时使用两版时保留 caa_ai_manual（R21）和 caa_ai_manual_r33（R33）两个服务器。逐个确认各自的六个工具可见，调用 caa_status 检查版本，再调用 caa_search；配置了 CAADoc 时再调用 caa_read_source。不要把“配置已保存”当作查询成功。
7. 告诉我配置位置、已通过的检查、当前可用模式和一个查询示例。按我的任务选择回答格式，不强制 JSON，不运行付费评测或 CAA 算子。
```

## 查询用法

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

`Class::Member` 支持声明位置查询；继承链来自本机 `Doc/generated/refman/_index/jsTree.js` 的静态父类记录。未配置源目录时报告 `source_unavailable`，不推断继承关系；已配置目录的带指纹索引还需通过上述层级文件检查。中文外插词与 `Extrapol` 词族由配置中的 `query_expansions` 展开，响应披露命中的规则；这些候选映射不代表算子输入等价。

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

`get-api` 的 `include` 参数按需返回成员、重载、证据定位和示例定位。函数族未明确选定页面、索引中没有聚合页且存在多个详情页时，返回 `status="requires_overload_selection"`、同名布尔字段 `true`，以及 `overloads` 中的候选 `page_id`。存在聚合页时可先读取聚合页；多个物理页面本身不等于必须选择重载。需要主动列出所有页面时使用 `--include overloads`。

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

缓存 schema 2 绑定数据库 SHA256 和源根目录。同一版本的构建与查询都使用相同的 `--release`，例如 `--release R33 index-sources`。旧安装可在关闭使用缓存的进程后，将 `cache/source-search.sqlite` 移到 `cache/r21/source-search.sqlite`，将 `cache/source-search-r33.sqlite` 移到 `cache/r33/source-search.sqlite`。也可直接执行 `index-sources` 重建。`not_indexed` 或 `source_root_changed` 表示需核对数据库、源目录和缓存。

正文查询先匹配字面短语，无命中时再匹配全部英文词；响应的 `query_mode` 披露采用的方式。FTS 不是中文语义检索。

### 智能体调用顺序

1. 首次接入或配置变更后先用 `status` 检查索引与源配置。未配 CAADoc 时使用 `search` / `get-api` 返回索引信息；`status="ok"` 或源路径非空都不证明原文可读，需实际读取验证。
2. 原文可读且已知 API 或 `Class::Member` 时直接 `read-source`；不要固定执行 search → get-api → read-source 三次调用。
3. 需要区分重载、成员声明类或示例位置时才请求 `get-api` 的相应 include。
4. 只知道用途时先查官方符号、维护者别名；已构建私有正文缓存时，英文原文术语可用 `search-source`。普通索引空结果不是官方不存在该能力的证据。
5. 需要原文依据时按需读取类级说明；按分页字段继续读，保留 URI / anchor。官方页面、安装头文件和运行实测属于不同证据层级，不能互相替代。

## MCP

本项目提供由客户端启动的本机 stdio 服务，不提供远程 MCP URL。[config/mcp.example.toml](config/mcp.example.toml) 是 **Codex 专用 TOML 模板**，其他支持本机 stdio 的 MCP 客户端需按其格式配置相同的 `command`、`args` 和 `env`。

Codex 接入步骤：

1. 将模板中的配置段合并到用户级 `~/.codex/config.toml`，或受信任项目的 `.codex/config.toml`；不要覆盖其他服务器配置。这些位置及 stdio 字段见 [Codex 官方 MCP 文档](https://developers.openai.com/codex/mcp)。
2. 把 `<python-executable>` 和 `<project-root>` 换成本机绝对路径，Windows 的 TOML 路径可用 `/`。两个服务器分别通过 `--release R21` 和 `--release R33` 选库，启动后读取对应的本机源配置。需要同时查两版时保留两段，只查一版时复制对应段。
3. 让客户端重新加载 MCP 配置并检查服务器。Codex CLI 可用 `codex mcp list` 查看已配置服务器，TUI 中用 `/mcp` 查看活动服务器；配置存在不等于工具调用成功。
4. 每个服务器各提供下表的 6 个工具。分别调用两个服务器的 `caa_status` 核对 `metadata.source_release`，再用各自的 `caa_search` 查询 `CATGeoFactory`。若配置了原文，再调用同一服务器的 `caa_read_source` 读取它；通过标准与前面的 CLI 检查相同。失败时核对 Python 路径、数据库路径及 CAADoc 路径。

本机查询返回的文档片段可能由客户端发送给模型提供商；使用前确认文档许可和客户端的数据处理设置。

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

## 验证

```powershell
python -m unittest discover -s tests -v
```

单元测试使用合成源页，不需要安装 CATIA 或配置模型 key。CI 配置覆盖 Windows / Linux 与 Python 3.10 / 3.13。索引重建、公开导出、离线诊断和付费模型评测见 [构建与验证指南](docs/MAINTAINER_GUIDE.md)；普通查询用户无需执行这些步骤。

已发布的 [稳定性测试记录](reports/agent-stability.md) 和 [精确引用迭代记录](reports/reference-transfer-results.md) 给出测试范围、验收口径及失败案例。检索定位正确、模型回答通过字段验收和 C++ 编译运行通过是不同结果；现有测量不能推导出所有通用模型的准确率或固定效率提升比例。

[R33 构建与查询验证](reports/r33-build.md) 记录两版并存、成员表格适配、实库定位检查和本机源目录中缺失的两个页面。

## 数据来源

CATIA、CAA 和 CAADoc 属于其权利人提供的第三方资料。仓库中的索引用于定位用户本机已有的 CAADoc；本地文档的访问和使用遵循对应安装与许可条款。
