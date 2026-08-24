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

公开数据库的 `content_mode` 为 `index-only`，保留 API 结构、签名、别名和 `caadoc://` 源定位。官方正文和示例源码在查询时从用户配置的 CAADoc 读取。

当前索引以 CATIA V5R21 CAADoc 为来源基线。查询响应包含来源基线、版本策略、内容模式和投影 schema 版本。

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
```

`search` 按逻辑 API 聚合候选项。`match_score` 表示当前查询与候选项的词法相关度；`match_tier`、`match_reason` 和 `matched_key_en` 说明命中层级、字段和官方英文键。

### API 与源页

```powershell
python .\tools\caa_manual_cli.py get-api CATGeoFactory
python .\tools\caa_manual_cli.py get-api CATGeoFactory --include members
python .\tools\caa_manual_cli.py get-api CATGeoFactory --include overloads --include evidence --include examples
python .\tools\caa_manual_cli.py read-source CATGeoFactory
python .\tools\caa_manual_cli.py read-source "<member-id>" --max-chars 8000
```

`get-api` 的 `include` 参数按需返回成员、重载、证据定位和示例定位。函数族包含多个物理页面时，响应返回 `requires_overload_selection` 和候选 `page_id`。

`read-source` 将 `caadoc://` URI 映射到本机 CAADoc。默认返回锚点附近的官方纯文本；`--format raw_html` 返回原始 HTML。响应包含 `source_uri`、`anchor`、`resolved_path`、`encoding` 和 `truncated`。

## MCP

MCP 配置模板位于 `config/mcp.example.toml`，提供以下工具：

| 工具 | 用途 |
| --- | --- |
| `caa_status` | 查询数据库、内容模式和源目录状态 |
| `caa_catalog` | 浏览目录层级 |
| `caa_search` | 检索 API、成员和中文查询词 |
| `caa_get_api` | 获取 API 结构及扩展内容 |
| `caa_read_source` | 读取官方源页或成员锚点 |

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
python .\tools\caa_manual_cli.py status
python .\tools\caa_manual_cli.py search CATGeoFactory --limit 3
```

## 数据来源

CATIA、CAA 和 CAADoc 属于其权利人提供的第三方资料。仓库中的索引用于定位用户本机已有的 CAADoc；本地文档的访问和使用遵循对应安装与许可条款。
