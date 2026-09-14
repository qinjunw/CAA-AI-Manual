# 智能体查询与调用依据

本手册的 MCP 工具查询本机 CAADoc 和只读索引，不执行 CAA 算子。以下流程适用于支持工具调用的模型；付费测试脚本目前只接入 DeepSeek，不能据此推断其他模型的通过率。

可将 [精简系统提示](agent-system-prompt.txt) 加入智能体的系统提示或开发者提示。它包含查询规则，不包含题库答案；不是 MCP 默认自动注入的配置。付费测试可使用 `--use-guide` 加载它。

## 按已知信息选择入口

| 已知信息 | 首次查询 | 后续动作 |
| --- | --- | --- |
| 类名和方法名 | `caa_read_source(reference="Class::Member")` | 读取声明、输入约束和生命周期说明 |
| 已有索引中的参数锚点 | `caa_read_source(reference="Class::Member(Type1,Type2&)")` | 精确定位后核对完整声明；无匹配时去掉参数列表查看候选 |
| 方法有多个重载 | 使用 `ambiguous` 返回的候选 `member_id` | 按参数类型选候选，再读该成员原文 |
| 类名确定，方法名不确定 | `caa_get_api(reference="Class", include=["members"], member="Name")` | 检查名称候选；无匹配时再浏览该类成员 |
| 只有用途 | `caa_search(query="官方术语或已配置任务词")` | 确认 Framework 和 CAA / Automation 层级 |
| 只有原文中的英文短语 | `caa_search_source(query="English phrase")` | 从结果 URI 读取原文；需先构建私有缓存 |
| 要查示例 | `caa_get_api(reference="Class", include=["examples"])` | 沿 `examples_pagination.next_offset` 翻页，随后读取选中的源码 |

`Class Member` 可以解释为 `Class::Member`，但仅限两个标识符且类名已被索引收录。解释结果写入 `query_interpretation`。近似成员名只用于发现候选，不作为别名解析，也不表示参数或用途等价。

`Class::Member(...)` 的参数部分按索引锚点匹配，支持空参数 `()`、空白、HTML 实体和全宽字符的表示归一。匹配保留 `const`、指针、引用、类型名大小写及单词边界；不会进行 C++ 类型转换、补默认参数或猜测重载。索引锚点可能省略完整声明中的限定符，所以把完整 C++ 声明直接粘作定位表达式也可能无匹配：改查 `Class::Member`，选候选 `member_id` 后读取原文。`Member()` 只定位索引里的空参数锚点，不表示“使用默认实参调用”。

已知方法名时使用 `member` 筛选。无筛选地索取大型工厂类的全部成员和证据，会把无关声明带进模型上下文。示例响应只预览前 20 个关联符号；`symbols_total_count` / `symbols_truncated` 标记省略情况，完整用法仍需读源码。

## 生成 C++ 调用前检查

1. **对象层级：** 区分数学值、几何对象、拓扑对象、规格特征和 Automation 对象；相似名称不构成类型兼容证明。
2. **完整声明：** 从目标重载的原文读取返回类型、指针层级、`const`、引用、参数顺序和默认参数。索引签名用于定位，不能替代完整声明。
3. **输入合法性：** 读取类级前言和方法说明，确认 shell / face / edge、闭合性、容差、方向等限制。输入是 `CATBody*` 不代表任意 body 都符合算子的要求。
4. **执行与所有权：** 分别核查工厂是否立即运行、是否需要 `Run()`、对象采用 `delete` 还是 `Release()`、结果体由谁管理。不能按函数名前缀统一推断。
5. **声明来源：** 对继承成员同时保留声明类和请求类。成员列表默认为本类声明；查询继承记录需要本机 CAADoc。
6. **验证层级：** 分开标记“官方文档已核对”“本版本头文件已核对”“编译通过”“CATIA 运行通过”。本文档与检索测试只支持第一项。

证据不足时输出未知项及下一项验证条件，不把缺少的参数、释放规则或运行结果补成确定结论。

## 可恢复错误与停止条件

| 工具状态 | 处理方式 |
| --- | --- |
| `error_code=invalid_arguments` | 根据 `error` 指出的字段及工具 `inputSchema` 修正参数，然后重试；数字字符串和布尔字符串不自动转换 |
| `ambiguous` / `requires_overload_selection` | 选明确的成员或页面 ID；不得任取第一项 |
| `anchor_not_found` | 核对返回的锚点或成员 ID；C++ 文件没有 HTML 成员锚点，应使用字符分页 |
| `not_found` / 空候选 | 核对名称、文档层和继承范围；只能报告当前查询未验证，不能证明所有版本均不存在 |
| `not_indexed` / `source_root_changed` | 配置本机源目录并通过 CLI 重建私有正文缓存 |
| `truncated` / `has_more` | 使用对应分页字段继续；首批数量不是总数 |

已取得目标声明和约束后结束检索；不要再请求整类所有成员来重复确认。同一精确符号在索引和正文中均无证据时，应报告当前来源范围内未核实，避免反复扩大查询试图证明普遍不存在。

## 接入结构化输出

调用端应同时校验模型输出和工具证据。仅提示“返回 JSON”不能保证没有代码围栏或附加文字；支持结构化输出的提供商应开启相应选项。DeepSeek 的 `response_format={"type":"json_object"}` 用法见其 [JSON Output 文档](https://api-docs.deepseek.com/guides/json_mode/)。

输出字段应限制为调用方需要的事实。引用只保留支持结论的必要 URI，不复述整页示例列表。检查 `finish_reason`、JSON 是否完整、字段类型以及引用是否来自实际查询；截断或空白输出不能传给下一步代码生成或执行。

测试结果中的 `automation_pass` 同时要求完整结束、严格 JSON、规定字段匹配和证据检查通过；`pass` 允许从唯一 JSON 代码块提取字段。这两个指标都不是可执行 C++ 的编译通过率。具体口径见 [稳定性测试记录](../reports/agent-stability.md)。
