# CAA AI Manual

CAA AI Manual 提供 CATIA CAADoc 的本地结构化查询接口。仓库随附一份索引版 SQLite 数据库；访客配置自己的 CAADoc 目录后，可以通过 CLI 或 MCP 浏览目录、检索 API、查看成员并读取对应官方源页。

当前索引以 CATIA V5R21 CAADoc 为来源基线。查询响应包含 `source_baseline`、`version_policy`、`content_mode` 和投影 schema 版本。

## 项目组成

| 路径 | 说明 |
| --- | --- |
| `data/manual.sqlite` | 随仓库发布的 API 结构索引 |
| `data/manifest.json` | 来源基线、内容模式和记录统计 |
| `config/catalog_zh.yaml` | 中文目录名称与查询词 |
| `tools/caa_manual_cli.py` | 命令行查询入口 |
| `tools/caa_manual_mcp_server.py` | stdio MCP server |
| `tools/caa_manual_query.py` | CLI 与 MCP 共用的查询实现 |
| `tools/build_caa_ai_manual.py` | 本地完整数据库构建器 |
| `tools/export_public_index.py` | 公开索引数据库导出器 |

## 配置

创建本机配置：

```powershell
Copy-Item .env.example .env
```

按照 `.env.example` 设置 `CAA_CAADOC_ROOT`。项目目录和 `data/manual.sqlite` 是默认查询位置；`CAA_AI_MANUAL_ROOT` 与 `CAA_AI_MANUAL_DB` 用于覆盖默认位置。

配置后检查数据库和源目录状态：

```powershell
python .\tools\caa_manual_cli.py status
```

## 目录

目录按照 `Layer -> Framework -> API / function-family` 组织：

```powershell
python .\tools\caa_manual_cli.py catalog --parent catalog:root --depth 1
python .\tools\caa_manual_cli.py catalog --parent CAA-refman --depth 1 --limit 50
python .\tools\caa_manual_cli.py catalog --parent GeometricObjects --depth 1 --limit 50
```

`parent` 接受目录节点 ID、官方英文键或已配置的中文查询词。根目录包含 C++ API、Automation、概念文档、官方示例和能力标签入口。

## 检索

```powershell
python .\tools\caa_manual_cli.py search CATGeoFactory
python .\tools\caa_manual_cli.py search "几何工厂"
python .\tools\caa_manual_cli.py search CreatePlane --framework GeometricObjects
```

索引检索覆盖官方 API 名称、成员名称、签名、Framework、能力标签和中文查询词。主要返回字段：

| 字段 | 说明 |
| --- | --- |
| `candidate_groups` | 按逻辑 API 聚合的候选项 |
| `match_score` | 当前查询与候选项的词法相关度 |
| `match_tier` | 英文键、中文词、成员或 FTS 索引匹配层级 |
| `match_reason` | 本次匹配使用的字段和规则 |
| `matched_key_en` | 实际命中的官方英文键 |
| `requires_selection` | 同分候选的选择状态 |

`match_score` 用于当前候选集的排序和消歧。

## API 结构

```powershell
python .\tools\caa_manual_cli.py get-api CATGeoFactory
python .\tools\caa_manual_cli.py get-api CATGeoFactory --include members
python .\tools\caa_manual_cli.py get-api CATGeoFactory --include overloads --include evidence --include examples
```

`include` 支持：

| 值 | 返回内容 |
| --- | --- |
| `members` | 成员分组、签名和锚点 |
| `overloads` | 函数族的物理页面候选 |
| `evidence` | 官方源 URI、锚点和提取类型 |
| `examples` | 已关联的官方示例源 URI 和符号列表 |

函数族包含多个详情页时，响应通过 `requires_overload_selection` 和 `page_id` 提供候选选择。

## 官方源页

```powershell
python .\tools\caa_manual_cli.py read-source CATGeoFactory
python .\tools\caa_manual_cli.py read-source "<member_id>" --max-chars 8000
python .\tools\caa_manual_cli.py read-source "<page_id>" --anchor "<anchor>" --format raw_html
```

`read-source` 将 `caadoc://` URI 解析到 `CAA_CAADOC_ROOT`。默认 `format=text` 返回锚点附近的官方纯文本；`format=raw_html` 返回对应 HTML。响应包含 `source_uri`、`anchor`、`resolved_path`、`encoding` 和 `truncated`。

## 数据结构

公开数据库使用以下查询表：

| 表 | 内容 |
| --- | --- |
| `catalog_nodes` | Layer、Framework、API 和函数族目录 |
| `api_pages` | API 物理页面、签名、头文件和源 URI |
| `api_members` | 页面成员、重载分组和 HTML 锚点 |
| `api_aliases` | 中文查询词、官方英文键和适用范围 |
| `api_examples` | API 到本地官方示例文件的定位关系 |
| `evidence` | 官方源 URI、锚点、提取类型和证据等级 |
| `projection_metadata` | schema、来源基线、内容模式和 FTS 状态 |

公开数据库的 `content_mode` 为 `index-only`。API 摘要、证据正文和示例源码由本机 CAADoc 提供；`entities`、`relations` 和 `chunks` 兼容表在公开数据库中为空。本地完整构建会填充这些审计表。

## MCP

MCP server 公开五个工具：

| 工具 | 用途 |
| --- | --- |
| `caa_status` | 查询数据库、内容模式和源目录状态 |
| `caa_catalog` | 浏览目录层级 |
| `caa_search` | 检索 API、成员和中文查询词 |
| `caa_get_api` | 获取 API 结构及扩展内容 |
| `caa_read_source` | 从本机 CAADoc 读取官方源页或成员锚点 |

MCP 客户端配置模板位于 `config/mcp.example.toml`。

## 本地构建

完整构建应输出到仓库外的本机目录：

```powershell
python .\tools\build_caa_ai_manual.py build --caadoc "<caadoc-root>" --out "<private-build-root>"
```

构建结果包含完整 JSONL、SQLite 数据库和官方正文索引。维护公开数据库时，从完整构建导出索引版：

```powershell
python .\tools\export_public_index.py `
  --source-db "<private-build-root>/data/manual.sqlite" `
  --output-db ".\data\manual.sqlite" `
  --source-manifest "<private-build-root>/data/manifest.json" `
  --output-manifest ".\data\manifest.json"
```

导出器保留结构、签名和源定位，清除嵌入正文，并重新建立公开检索所需的 FTS 索引。

## 验证

```powershell
python -m unittest discover -s tests -v
python .\tools\caa_manual_cli.py status
python .\tools\caa_manual_cli.py search CATGeoFactory --limit 3
```

## 数据来源

CATIA、CAA 和 CAADoc 属于其权利人提供的第三方资料。仓库中的公开索引用于定位用户本机已有的 CAADoc；本地文档的访问和使用遵循对应安装与许可条款。
