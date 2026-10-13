# R33 索引构建与查询验证

验证日期：2026-10-13。来源为本机 CATIA V5-6R2023（R33）CAADoc；目标是定位 API、成员和官方原文。R21 与 R33 使用独立数据库、源目录配置和正文缓存。

## 发布数据

| 项目 | R21 | R33 |
| --- | ---: | ---: |
| 数据库 | `data/manual.sqlite` | `data/r33/manual.sqlite` |
| API 页面记录 | 6,616 | 7,378 |
| 成员条目 | 31,229 | 36,047 |
| 示例／用例定位记录 | 18,717 | 24,259 |

R21 数据库未改写，SHA256 为 `ef760ffeee6601e69aeffba527ecf48f849fa59e5305c111863eb389738e7bb9`。R33 公开库为 61,939,712 字节，SHA256 为 `677c916ac484a0e1ee7166bfdd0c51807343b2b565f51034244cc591a876d09b`。两库均为 `index-only`，不包含官方正文或示例源码；完整构建与正文缓存留在本机。

R33 构建输入包含 10,492 个 HTML、207 个 TOC XML 和 1,411 个 C++ 文件，统计见 [manifest](../data/r33/manifest.json)。`source_tree_sha256` 为 `83135970316e3fd537304f8be9658b5d0326ff3837408b46cd9c7aef21b6ec1b`，只对应 `caadoc://Doc/generated/refman/_index/jsTree.js`，不是整个文档树的校验和。中文目录配置继续记录其 R21 来源，不作为跨版本运行兼容性证据。

## 格式适配

首轮使用旧解析器时，R33 仅生成 3,886 个成员条目。R33 将成员索引改为双列表格；旧逻辑按 R21 的 `dl` 列表终止符解析，因而合并多行、遗漏方法。构建器现在分别处理两种结构，保留重载锚点，并按索引边界结束解析，避免固定 120 KB 截断。R21 标题内的空锚点也有回归覆盖。

R33 顶部导航链接带有 `id`，旧上下文提取会在导航处提前结束。读取成员的 `include_context` 现在优先提取类说明的语义区段，保留约束说明并排除成员索引和定义；旧 R21 页面保留原格式回退。

版本字段从构建参数写入 SQLite 和 manifest。R33 完整构建拒绝写入仓库；已有输出属于其他版本或版本未知时拒绝覆盖。公开导出在替换数据库前检查源 manifest 与数据库的版本字段。查询按所选数据库读取同目录 manifest，并在读取带指纹索引的原文或继承信息前校验层级文件。

## 验证结果

- 95 项单元测试通过，覆盖两版格式、版本标注、输出保护、公开正文清除、源目录匹配和缓存隔离。
- 检查全部 7,378 个页面定位，其中 7,376 个文件存在；另 2 个为源目录自身的悬空链接，索引已标为不可读，见下节。
- 从 R33 成员索引表独立枚举到 36,047 个条目，与数据库逐页核对无遗漏；全部已收录成员的原文锚点存在。
- 24,259 条示例／用例定位对应 2,349 个不同源文件，全部存在。
- 实读 `CATGeoFactory`、`CATBody::GetAllCells`、两种 `CATICGMSkinExtrapol*`、`CATISpecObject`、`CATIPrtFactory` 和 `CATIProduct`。继承成员定位到 `CATTopology`，返回成员声明及类级上下文。
- 6 个 R33 Framework 的上下文抽查均包含类说明，且不混入导航或成员索引。
- 通过实际 stdio 会话调用 R33 的全部 6 个 MCP 工具；仅索引查询、拒绝绑定 R21 源目录及旧 R21 原文读取也通过。
- R21 旧／新解析器抽查 8 页，7 页的成员字段集合一致；`CATGeoFactory` 唯一减少项为原误收为方法的枚举 `CATDatumId`。没有执行全 R21 重建。

R33 正文缓存收录 11,606 个文档，跳过 3 个：上述 2 个缺失页面，以及超过 2,000,000 字节阈值的 `caadoc://Doc/online/CAAScrSettings/CAAScrTocAttMasterIndex.htm`（2,083,940 字节）。该限制只影响正文缓存收录，不阻止按 URI 分页读取存在的源文件。

R21 私有缓存也已重建到 schema 2，保留独立的 10,334 个文档。两版同时检索 `boundary of the shell`，分别返回 4 条和 5 条结果；两套查询实例各自的 6 个 MCP 工具均通过只读检查，响应版本与所选库一致。缓存重建前后，两份公开数据库的 SHA256 均未变化。

## 来源缺口与使用

`caadoc://Doc/generated/interfaces/NavigatorInterfaces/CAANavigatorInterfacesToc.xml` 与同目录 `jsFwLink.js` 引用了以下两个页面，但本机 R33 文档树中没有对应文件：

- `define_CatAnnotatedViewCmdOption_NoAnimation_87685.htm`
- `define_CatAnnotatedViewCmdOption_NoToolbar_80261.htm`

保留这些索引项及 `source_available=false`，可定位目录记录，但不能读取缺失的定义页；不使用其他版本正文补齐。

并存配置和查询步骤见 [README](../README.md)，重建步骤见 [维护指南](../docs/MAINTAINER_GUIDE.md)。CLI 使用默认 `.env` 查询 R21，使用独立 `.env.r33` 查询 R33；MCP 模板提供两个独立服务器配置段。查询验证不包含 CAA 编译、算子执行或模型问答准确率测量。
