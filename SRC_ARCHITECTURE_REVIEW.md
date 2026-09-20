# src 全量架构与函数审阅报告

## 阅读说明与重构现状

**本文件保留历史审阅证据，不把旧问题描述伪装成当前代码状态。**

- **第 1–10 节：重构前历史基线**，对应 `3c9d2cf`。其中“当前”“尚未修复”“未改业务代码”“未调用真实上游”等措辞仅指该次审阅；旧行号、旧接口和复现脚本必须在该提交的独立检出目录使用，不能直接套到新代码。
- **第 11–12 节：修复后的架构与全量函数清单**，代码快照为 `0b1fb39`。当前源码共 **13 个文件、2344 行、126 个函数/方法、24 个类**；包含嵌套回调，不包含隐式生成的 dataclass 方法。
- **第 13–14 节：首次重构验收历史**，对应 `bdfdd9a` 及随后上游复验。原来的 174 项绿灯未覆盖错误日志参与淘汰、排队 COMMIT 取消以及回滚期间重复取消；不能据此认定所有存储缺陷已关闭。
- **第 15 节：补充复核与修复**，记录新增缺陷、确定性回归、取消的准确边界及新一轮验证结果。本文保留此前真实 LLM 503 和后续成功的证据，不把旧成功冒充本次验证。
- **第 16 节：性能、异步与并发审计**，针对同一代码快照记录本地受控压测、函数级瓶颈和优化顺序；六项性能及资源治理问题仍待处理，本节没有实施优化。
- **第 17 节：性能重构执行计划**，明确批次、函数范围、回归与性能门槛，以及实施前需要确认的行为决策；计划不等于已实施或已验证收益。
- 正确性判断：**🟢 第 15 节已复现缺陷已修复并重新验收**；183 项自动测试、静态检查、curl 与两种真实上游作答/缓存闭环通过。性能判断：**🟡 异步 I/O 链路有效，但同题并发、资源预算和数据库热路径仍有实测问题**，详见第 16 节。不声称不存在未发现缺陷，也不声称已验证真实 OCS 浏览器交互或生产容量。

## 1. 审阅结论与边界

【品味评分】**🟡 凑合：主链路清楚，局部实现简洁，但资源所有权、配置边界和工具函数归属尚不够清晰。**

【致命问题】没有发现需要整体推倒重写的架构错误；发现必须优先修复的**事务取消后未回滚**问题，以及会无谓淘汰缓存的容量处理顺序。现有测试全部通过，不能据此判断这些行为正确。

【结论】**✅ 值得做有边界的重构，不值得按“企业分层架构”重建项目。** 先修确定缺陷，再拆出答题编排和题目归一化；保留扁平包，不引入 repository/service/container 三层套娃，不增加 ORM、多 provider 插件框架或通用缓存工厂。

### 1.1 可核验范围

- 审阅基线：Git `3c9d2cfefe3641f6a4bccf5c841c2fae00b62fff`；源码与审阅开始时一致。
- 审阅基准日期：2026-09-19；复核时工作机本地时钟显示 2026-09-20（Asia/Shanghai）。两者分开记录，资料版本以实际锁定环境和检索结果为准。
- 全量范围：`src/llm_ocs_bridge/` 下 **11 个 Python 文件、2204 个物理行、117 个函数/方法（包含一个嵌套回调和 property getter）、17 个类**。逐函数清单见第 5 节。
- 辅助证据：`pyproject.toml`、`uv.lock`、现有测试、README/部署约定，以及 Context7、Exa 检索到的官方资料。旧记忆只用于识别设计意图，结论以当前源码和复现结果为准。
- 本次只新增本报告；没有修改运行时代码、测试、依赖、用户配置或生产数据库，没有调用真实 LLM/Exa 服务，没有执行 Git 提交。
- 报告内源码路径均相对于仓库根目录，后接 **1-based 起始行号**；定位基于上述提交。新文件布局是建议，不是已经实施的改动。

### 1.2 哪些不是缺陷

以下行为与已确认需求及当前测试一致，不应借“优雅”之名删除：

1. 本机单用户服务，默认绑定 loopback；不按公网多租户服务要求添加复杂鉴权和限流设施。
2. `/search` 鉴权失败仍返回 HTTP 200、OCS `code=0`；`/stats` 使用 HTTP 401。
3. 只接受 `X-Access-Token`，不接收 URL token；`check` 成功输出的 OCS JSON 有意包含 token，方便本地导入。
4. 搜索模式答案缓存优先；fallback 答案缓存只能在搜索不可用/失败/无证据后读取，避免压制恢复后的 Exa。
5. Exa 是唯一搜索源；没有设置 `SEARCH_RESULT_LIMIT` 时不增加项目侧数量上限。
6. fallback 是明确的产品能力，不是应该一律删除的“容错壳”。无 TTL、按容量淘汰同样是既定策略。
7. 字符串、换行、`#` 和数组选项输入属于已支持协议；空题型按开放问答处理。
8. `MAX_LLM_RETRIES` 表示首次调用之后的重试次数；不能替换 SDK 后悄悄改变计数、HTTP 分类或字节上限。
9. 代码当前正确地把题目/选项/证据作为 user JSON 数据，把格式约束放入 system 消息；这能明确提示层级，但不等于已经证明免疫提示注入。

## 2. 架构判断：问题在边界，不在文件数

### 2.1 当前依赖与调用链

```text
main ── 配置加载 / check / clear-cache / Uvicorn
  └─ app ── HTTP 解析 + 鉴权 + Runtime 生命周期 + 答题编排 + stats SQL
       ├─ cache ── storage ── aiosqlite
       ├─ metrics ── storage
       ├─ search ── Exa SDK
       └─ llm ── HTTPX

config.settings  ← app / storage / search / llm / metrics / main
utils            ← app / cache / search / llm / metrics / main
models           ← utils 与业务模块
```

模块 import 图没有循环。问题是：`app.py` 同时知道 HTTP 协议、重试计数、缓存键、证据选择和数据库列；`utils.py` 又同时知道题型、选项、缓存和 LLM 输出。**无循环依赖不等于职责分配正确。**

真实答题顺序是：输入归一化 → search-mode 答案缓存 → 搜索缓存/Exa → 有证据则回答 → 否则判断 fallback 开关 → fallback 答案缓存 → 纯 LLM。这个顺序合理，重构应保留并直接表达出来。

### 2.2 每个文件应拆、应并，还是保留

| 文件 | 当前行数 | 判断 | 最小合理调整 |
| --- | ---: | --- | --- |
| `__init__.py` | 1 | 保留 | 没有初始化副作用；不加自动导入/re-export 大礼包。 |
| `app.py` | 482 | **应拆** | HTTP、响应头、状态码和 lifespan 留下；缓存/搜索/答题编排移到一个 `service.py`；recent-search SQL 回归搜索缓存 store。不是因超过某个行数而拆。 |
| `cache.py` | 229 | **保留一文件** | 两种缓存记录形状不同，但同属缓存；不拆成 `search_cache.py`/`answer_cache.py`，不做通用泛型 CRUD。接收缓存键和 recent-search 查询职责。 |
| `config.py` | 166 | 保留，重做加载边界 | `Settings` 应是纯数据；增加显式加载入口，环境读取不要发生在类字段默认值求值时。 |
| `llm.py` | 229 | 保留 | 传入已归一化的 `OCSQuery`；把答案解析/验证和证据渲染归到这里。仍不足以证明需要独立 transport/prompt/parser 三个文件。 |
| `main.py` | 301 | **暂不拆** | 三个 CLI 命令是同一边界；重复帮助文本很小。先修配置与 reload，再判断 `check` 是否真的独立演进到需要 `check.py`。 |
| `metrics.py` | 146 | 保留 | 它负责持久化计数和错误记录，不应与 HTTP stats 路由合并；给记录格式和计数定义更清楚的边界。 |
| `models.py` | 33 | 保留 | 两个跨模块数据结构有真实用途；不因文件小而并入 `utils.py`。适合补充少量真正跨边界的结果类型。 |
| `search.py` | 144 | 保留 | 一个 SDK adapter 加一个小的相关性策略，不值得建 providers 包；删掉同名校验转发层，修复证据丢失与打分。 |
| `storage.py` | 177 | 保留数据库边界，调整所有权 | 三个 store 操作同一数据库，却各自持有连接、懒初始化，再用类级锁协调。优先显式初始化；后续可改成 Runtime 拥有一个数据库对象，由 store 组合使用。 |
| `utils.py` | 296 | **按概念分流，不按大小切块** | 题目/选项到 `questions.py`；答案到 `llm.py`；缓存键到 `cache.py`；检索词到 `search.py`；只留下确实通用的文本归一化、脱敏、UTF-8 截断。 |

建议保持当前扁平包，最多先新增 `service.py` 和 `questions.py`。不要把这张表当成必须同时执行的文件搬家清单；缺陷修复和职责移动分开提交、分别验证。

### 2.3 四个必须收紧的架构边界

**A1：HTTP adapter 不能兼任答题服务。**

- `app._recent_search_observability` 直接调用 `search_cache._db()` 并写 SQL，是明确的封装穿透。
- `_respond_with_answer` 同时做异常分类、错误持久化、答案持久化和 HTTP 响应；与 `search` 的业务分支互相牵制。
- `SimpleNamespace` 让 `runtime.answerer` 等所有成员绕过静态契约。一个带类型的 dataclass 足够，不要 DI 容器。
- 最短方案：服务入口接收 `OCSQuery`，返回答案及 provider/cache 信息；HTTP 层决定 OCS code/header。缓存命中时不加 provider header 的已有语义必须保留。

**A2：一个物理数据库应有一个清晰生命周期。**

- `AsyncSQLiteStore` 名义是通用数据库基类，却硬编码三个业务表的淘汰时间列；子类反过来用 `_db()` 完成自己的初始化。
- `_conn` 既表示“已建立连接”，又被当成“完整初始化完成”；这是 F10 的根因。
- 同路径全局 `_write_locks` 解决进程内多连接互斥，却把生命周期和 event loop 隐藏在类级状态。不要宣称当前单次生产运行必然因跨 loop 失败；它是复用/测试的隐式约束。
- **近期最小修复**：在 lifespan 中显式连接并建好全部表，清理覆盖初始化全过程；保留 store 公共读写 API。
- **后续结构优化**：Runtime 持有数据库对象，store 通过组合复用它；初始化、事务和容量维护由一个 owner 控制。单连接时也必须协调完整事务与同连接读取，不能仅依赖 aiosqlite 的逐语句队列 [R4]。

**A3：配置是入口输入，不是 import 副作用。**

- `Settings` 的 class body 调用 `_int/_float/_text`，`Settings()` 本身不会重新读取环境；测试反复删除 `sys.modules` 正是耦合成本。
- 所有必填数值会在导入时解析，`clear-cache(validate=False)` 仍依赖 LLM/请求大小等与清理无关的环境值。
- CLI `check` 显式读当前目录 `.env`，普通启动隐式搜索调用源码目录；F04 是用户可见后果。
- 建议 `load_settings(env_path)` 显式返回不可变配置；启动入口决定路径，校验函数接收对象。不必为此新增 `pydantic-settings`。

**A4：内部数据应只归一化一次。**

- `app.search` 已构造 `OCSQuery`；`LLMAnswerer.answer` 又拿三个独立参数重新归一化 options；`_answer_with_llm` 再次验证答案。
- format/validate/打分分别重复解析选项 label；裸 tuple/string 使 label 的含义散落在多个函数。
- 近期：统一题目归一化入口和 label 提取函数；LLM 接收 `OCSQuery`。缓存读取仍应重新验证，因为它是持久化边界，不是冗余校验。
- 不必立刻增加复杂 `Option` 类；只有 label/text 两部分确实需要跨模块传递时，再用小 dataclass 固化数据。

## 3. 确定缺陷与验证结果

优先级：**P1**＝应优先修复的资源/事务正确性；**P2**＝明确功能错误、数据丢失或重要边界缺陷；**P3**＝局部简化/可维护性。以下不把每个建议都包装成 bug。

### F01 · P1 · 取消写任务后，事务残留并被下一次写入提交

- 位置：`src/llm_ocs_bridge/storage.py:74`，`AsyncSQLiteStore._write`。
- 根因：只捕获 `Exception`。`asyncio.CancelledError` 属于 `BaseException` [R1]，取消会绕过 rollback，随后释放 Python 写锁，但 SQLite 事务仍然打开。
- 隔离复现：进入 `_write`，完成 INSERT 后在可取消点暂停，取消任务。观察 `in_transaction=True`；另一连接写入得到 `database is locked`；同 store 随后的 `delete("absent")` commit 把已取消任务的行一并提交。
- 这不是假设磁盘坏掉：任务取消是异步服务正常控制流；`MetricsStore.log_error` 还真实包含 INSERT 与 DELETE 之间的 await 边界。
- 最短修复：事务上下文对取消也执行 rollback，再原样重新抛出；清理阶段不得把取消吞掉。把 connect、close、lifespan 的取消清理一起审查，但不要把本次复现扩写成“所有清理路径都已经实测失败”。
- 验收：取消后没有未提交事务；其他连接可立即写；下一次操作不能提交被取消写入；正常异常、正常提交继续通过。

### F02 · P2 · 没有先回收 WAL 就删除数据，容量策略误删有效缓存

- 位置：`src/llm_ocs_bridge/storage.py:92`，`_enforce_capacity_locked`；调用者 `_write`、`connect`。
- 根因：发现 main/WAL/SHM 总量超限便先选行 DELETE，再 checkpoint、VACUUM。WAL 中可回收的历史帧被当成必须删除的有效数据；不是所有超限都需要淘汰 [R2]。
- 同环境对照：12 条小缓存，总文件 176976 bytes；仅 `wal_checkpoint(TRUNCATE)` 后为 61440 bytes；预算 65536 bytes 足够保留全部数据。但当前策略把 **12 条全部删掉**，剩余 45056 bytes。
- 默认预算为 1 GiB 不使逻辑正确；上述较小预算是配置允许的值。报告不声称默认 1 GiB 下已经测得相同频率或耗时。
- 最短修复：先尝试 checkpoint 并读取其结果，重新测量；仍超限才淘汰。按批删除结束后再做必要空间回收，避免每个批次都全库 VACUUM。
- `VACUUM` 重建数据库且需要额外空间 [R3]；目前的容量值是持久文件回收目标，不是写入/VACUUM 过程的硬峰值限制。
- 验收：WAL-only 超限不删缓存；确实超限才按既定全局时间顺序删除；checkpoint 受读者阻塞时不能靠循环删除掩盖原因。

### F03 · P2 · `DEBUG=true` 直接导致服务启动退出

- 位置：`src/llm_ocs_bridge/main.py:281`，尤其第 301 行 `uvicorn.run(app, ..., reload=settings.debug)`。
- 原因：传的是 app 实例；Uvicorn auto-reload 要求 import string [R5]。
- 实测：将配置对象替换为 `debug=True` 后调用 `run([])`，在打开服务前退出，exit code 为 1，输出要求 import string 的警告。
- 最短修复：改为明确的模块导入字符串，保持现有入口；不要加“reload 失败则降级正常启动”的 fallback。
- 验收：真实子进程启动 DEBUG 模式，监听成功并验证 reload；普通启动与 CLI help 不回归。当前报告没有把未执行的真实 reload 成功测试写成通过。

### F04 · P2 · `check` 与普通启动读取 `.env` 的位置不一致

- 位置：`src/llm_ocs_bridge/config.py:9`、`src/llm_ocs_bridge/main.py:132`、`main.run:281`。
- `check` 通过 `_check_env_file` 找 CWD `.env` 并显式加载；普通启动调用 `_load_settings(validate=True)`，最终依赖无路径的 `load_dotenv()`。
- python-dotenv 默认从调用脚本目录向上查找，不等于 CWD [R6]。在源码根目录运行时两者重合，安装到别处或从不同工作目录启动就不一致。
- 隔离复现：把原样 `config.py` 放入独立“安装目录”，从另一目录执行脚本，该目录 `.env` 含 `PORT=8123`；其他必填值用测试环境提供。导入退出并报 `RuntimeError: PORT is required`。
- 最短修复：入口统一显式路径与环境优先级；不要让库模块自行寻找文件。顺带解除 class body 读取环境的问题。
- 验收：从任意工作目录按既定约定运行，check 与服务加载相同配置；系统环境优先级仍是 `override=False`。

### F05 · P2 · “语义评分”将连续中文整句当作一个关键词

- 位置：`src/llm_ocs_bridge/utils.py:294`、`utils.question_terms:85`、`src/llm_ocs_bridge/search.py:120`。
- `_TERM_RE` 把连续中文匹配为整段，`_extract_terms` 没有分词；`_semantic_score` 要求整段是证据的 substring。这实际上是词面覆盖，不是语义相似度。
- 实测题目“水的化学式是什么？”，证据“水的化学式是 H2O。”：terms 仅有 `水的化学式是什么`，总分为 0，默认阈值拒绝这条直接回答题目的证据。
- 影响：无选项中文填空/开放题尤其明显；有选项时命中某选项也不能证明正确识别题意。不能靠增加摘要长度权重修好这个问题。
- 最短方案：先给中文、英文、公式题建立小型正反例；改为明确的词面策略，如中文 n-gram，并保留零相关硬门槛。不要直接引入分词/embedding 依赖并宣称提升准确率。
- 验收：上述直接证据可用，无关长文仍拒绝；中英文、单字符/公式题分别评估。未进行真实搜索召回率评测，不能报告线上准确率。

### F06 · P2 · 合法的零阈值配置绕过“零相关证据不可用”规则

- 位置：`src/llm_ocs_bridge/search.py:38`，条件 `score < settings.min_search_quality_score`；配置允许阈值 0。
- 实测阈值设 0，对 quantum mechanics 题目返回 Gardening/种胡萝卜证据：score 为 0，但仍保留 1 条结果。
- 最短修复：不可用的零相关结果与可调质量阈值分开表达；不要用阈值同时表示业务不变量和排序偏好。
- 验收：阈值 0 时零相关结果仍为空；正分结果按阈值处理。此项与 F05 独立：改善分词不会修好比较条件。

### F07 · P2 · 搜索适配器无条件丢掉第二段及之后的 highlights

- 位置：`src/llm_ocs_bridge/search.py:64`，`ExaSearch._map_results`，第 68 行只读 `highlights[0]`。
- 实测 SDK 形状结果有 `['Introduction', 'The answer is H2O.']`，映射后仅保留 `Introduction`。后续打分和 LLM 均无法看到真正答案。
- 最短修复：保留并合并全部有效 highlights；用于 prompt 时继续遵守 `MAX_EVIDENCE_BYTES`。不要再加手写第二套网页抓取作为补救。
- 当前 SDK `Result` 已有 `published_date`、`highlights` 等确定字段 [R7]；`getattr` 默认空值、强制 `str`、camelCase 备用属性是需要缩减的兼容层。
- 验收：真正答案只在后续 highlight 时仍保留；空/无效字段在 SDK 边界明确处理；证据字节上限不变。

### F08 · P2 · 非 ASCII token 从普通鉴权失败变成 HTTP 500

- 位置：`src/llm_ocs_bridge/app.py:73`，`_auth_ok`。
- `secrets.compare_digest` 接收 str 时仅支持 ASCII [R8]。请求头经 ASGI 解码后是 str，代码未处理非 ASCII。
- 实测向 `/search` 传入原始头字节 `X-Access-Token: 0xff`，使用 `ASGITransport(raise_app_exceptions=False)`，得到 HTTP 500；没有启动外部 HTTP 服务。
- 最短修复：在鉴权边界约定 token 编码并比较 bytes，或明确拒绝非 ASCII 输入；配置 token 同时遵循相同规则。不要改成普通 `==`。
- 验收：非法头返回既定鉴权失败结果；正常 token、错误 token、`/stats` 状态码保持不变。这不是已经证实的鉴权绕过。

### F09 · P2 · 错误脱敏遗漏常见 JSON/字典形式

- 位置：`src/llm_ocs_bridge/utils.py:36`，`sanitize_error`，及第 22–26 行正则。
- 实测 `api_key=synthetic-uuid-secret` 被替换，但 `{"api_key": "synthetic-uuid-secret"}` 与 `{"Authorization": "Bearer synthetic-token-value"}` 原样保留。
- 原因：正则要求 key 紧接空白/冒号等，未覆盖引号；前缀 token 正则只补救特定 `sk-`/`exa-` 形态，不能覆盖任意服务商 token。
- 风险路径：`_resolve_evidence`/`MetricsStore.log_error` 和 CLI `_safe_error` 接收外部异常文本。**本次证明脱敏函数漏匹配，没有读取真实密钥，也没有证明某次真实异常已经泄露密钥。**
- 最短修复：优先记录可控异常类型/状态码，缩小任意字符串日志边界；仍需保留的文本脱敏覆盖 JSON/字典/quoted header 形式。不要把不可穷尽的正则当成安全保证。
- 验收：合成的不同键名、引号、UUID token 均隐藏；UTF-8 限长继续成立。删除 `_safe_error` 中未经脱敏直接输出的备用分支，或证明它有必要且同样安全。

### F10 · P2 · `_conn` 过早公开，半初始化状态可被其他读者使用

- 位置：`src/llm_ocs_bridge/storage.py:30`，尤其第 50–54 行；`MetricsStore._init_db:26`。
- `connect` 在所有表创建完成前设置 `_conn`，其他调用看到非 None 就绕过 `_connect_lock`。`_init_db` 再调用 `_db`，也迫使实现依赖这个半成品状态。
- 确定性调度复现：MetricsStore 建好 `metric_counters` 后、建 `error_log` 前暂停初始化；另一个任务调用 `get_recent_errors()`，得到 `OperationalError: no such table: error_log`。
- **适用范围**：证明 store API 的 ready 契约错误；没有声称当前每个 `/stats` 请求都会遇到该异常。现有路由顺序和共享写锁会缩小实际窗口。
- 最短修复：`_init_db` 接受尚未公开的 connection；完成 PRAGMA/建表/权限/容量初始化后再公开 ready 状态。配合 lifespan 启动时初始化，减少懒加载复杂性。
- 另一个静态问题：PRAGMA 设置发生在 try 清理块外；初始化前段异常没有统一 close；`lifespan` 逐个关闭时前一个 close 失败会跳过后续资源。用一个上下文资源栈表达所有权，不要到处添加吞异常分支。
- 验收：并发首次读写均等待完整初始化；任一步初始化失败都关闭已建立资源；其他资源关闭不被前一个异常跳过。

## 4. 冗余、性能与依赖：哪些该删，哪些不该动

### 4.1 可以直接收敛的冗余

| 位置 | 问题 | 判断与处理 |
| --- | --- | --- |
| `app._get_answerer:200` | async 函数内部没有 await，调用形成双 await | 懒加载没有并发 await 窗口，不是已证实的重复建客户端竞态；改同步 helper，或在 Runtime 生命周期直接构造。 |
| `app._answer_with_llm:230` | LLM 已验证，随后再验证一次 | 统一 LLM 输出契约后删重复内部校验；缓存读取校验保留。 |
| `app._respond_with_answer:341` | `LLMError` 与最终 `Exception` 分支内容相同 | 可合并响应/记录代码，但不能把确定性错误都标成 MissingKey，也不要掩盖内部不变量。 |
| `app._parse_request:90` | 三个末尾变量仅转发已有变量 | 直接返回完成校验的值；输入类型校验不是应删除的防御代码。 |
| `app._resolve_evidence:258` | 四元组携带未使用 score，cache 命中时伪造 0 | 只返回调用方真正消费的信息；需要扩展时用有字段名的小结果类型，不堆互相可矛盾的字符串/布尔值。 |
| `search._is_valid_result:85` | 纯一行转发全局函数 | 无额外语义、无其他引用，直接使用 `is_valid_search_result`。 |
| `search._map_results:64` | Any/getattr/str/多种日期属性 | 用当前 SDK 类型；只保留真正的外部边界校验，删除无依据兼容。 |
| `config._int:33` / `_optional_int:58` | 整数转换/范围校验重复，`default` 没有生产调用 | 小幅统一转换步骤或让 loader 区分必填/可选；不要为了两段代码建立配置解析框架。 |
| `utils.format_search_context:99` | 有界/无界分支重复拼接 | 统一生成片段；累计 byte 数，不反复编码已有全文。生产调用总是传上限。 |
| `utils._truncate_utf8:236` | 反复二分、切片、编码 | 在有效 Unicode 字符串的既定契约下，可用编码一次、字节切片、忽略末尾残缺码点解码；加多字节边界测试再替换。 |
| `utils.validate_answer:179` | completion 中 split 后再检查每段不含分隔符 | `"#" not in part` 恒成立；`all(parts)` 又接受纯空白内部段，建议统一段归一化后检查。 |
| `utils.allowed_option_labels:226` | 先 list 手动去重再转 frozenset | 直接集合构造，复用 label 解析；最多 8 个选项，不吹嘘为性能瓶颈。 |
| `utils._canonicalize:249` | 通用递归字典/Sequence 处理超出实际缓存键需求 | 生产 key 是显式参数组成；把 key schema 放回缓存模块，避免哈希层再次改写已归一化文本。现有稳定性测试应保留。 |
| `metrics.incr_many:54` | Serena 未发现调用者 | 要么实际用它批量计数，要么删除；不要保留一个永远不用的优化接口。 |
| `main._safe_error:235` | 导入纯工具失败时回退未脱敏原文 | 当前 utils 不读取配置，这个 RuntimeError fallback 没有合理依赖依据；删除比解释更好。 |

### 4.2 性能：有证据的成本，不编造吞吐量

- 正常无缓存、Exa 成功、LLM 一次成功的回答，要走 **7 次写事务**：answer miss、search miss、search request、search success、search cache 写入、LLM attempt、answer cache 写入。fallback 路径又有额外计数。应将同一阶段的多个计数合并，而不是给全部 metrics 加内存最终一致层。
- 每次 `_write` 前后检查容量并执行同步文件 stat/chmod；全局锁下还可能执行 VACUUM。真正的请求尾延迟风险在存储维护，而非 8 个选项的 for 循环。
- `_oldest_rows` 按时间排序、`_recent_search_observability` 按 created_at 排序，却没有对应索引；当前量级未做基准测试。用 `EXPLAIN QUERY PLAN`/真实规模测量再决定索引，不能只为“最佳实践”添加。
- `_semantic_score` 对每条结果重新生成相同 question terms；一次查询计算一次即可。`url_score` 对已通过 HTTP(S) URL 校验的结果通常退化成固定加分，不能代表可信度。
- `format_search_context` 累计拼接和重复编码有不必要成本，但受证据预算约束；没有测得它是主瓶颈。较小预算可以只产生 `[`，没有实际 snippet；不是越早字符截断就越有价值。
- 没有同问题在途去重，并发 cache miss 能重复打外部服务。当前单用户场景不自动等同于问题；先证明存在请求并发重复，再决定是否增加单飞机制。

### 4.3 依赖裁决

锁定环境：Python 3.12.11、SQLite 3.47.1；这些是本机和 lock 观测值，**不是“最新版本”结论**。

| 直接依赖 | lock 版本 | 使用与裁决 |
| --- | --- | --- |
| `aiosqlite` | 0.22.1 | 真正承担异步 SQLite I/O；保留。不换 ORM。 |
| `exa-py` | 2.14.0 | 唯一搜索 SDK；保留。当前没有 close/aclose/context-manager API，调用公开 client 的 aclose 符合锁定版本，别凭空改成 `AsyncExa.aclose()` [R7]。 |
| `fastapi` | 0.136.1 | 路由、Request/Response、lifespan 的基础；即使没用 Pydantic 请求模型，也不足以证明迁移纯 Starlette 划算。 |
| `httpx` | 0.28.1 | LLM 直接使用，测试亦使用；必须直接声明，不能依赖 Exa 间接安装。 |
| `python-dotenv` | 1.2.2 | CLI 与配置实际使用；保留依赖，修正加载时机/路径。 |
| `starlette` | 1.6.0 | 源码未直接 import，但直接声明有既定安全最低版本约束；不是因为 FastAPI 已依赖就可以删除。未进行本次完整漏洞扫描，不重复断言某版本无漏洞。 |
| `uvicorn` | 0.47.0 | 真实服务入口；保留，修 reload 调用。 |

`pytest`、`pytest-asyncio`、`ruff` 都有真实使用，不是死依赖。`uv_build` 是构建后端，不是运行时架构层。

**OpenAI SDK 的取舍：** `openai==2.43.0` 已经由 Exa 间接引入；说“为了少装 OpenAI SDK 才自己拼 HTTP”不符合当前依赖树。官方 SDK 支持异步、显式关闭、可关闭重试、原始流式响应 [R9]，值得借鉴。但这个项目必须保留原始响应字节上限、实际 HTTP attempt 计数和自定义重试分类，替换不会变成一行。

因此本报告**不建议立即为拿来主义迁移 SDK**：先保持单端点 HTTPX 实现；只有 SDK 真正减少净复杂度且测试证明行为等价时再替换，并把 openai 加为直接依赖。不能依赖其间接安装，也不能同时保留 SDK 和手写两个并行后端。没有证据需要 LangChain、LlamaIndex、SQLAlchemy、通用 provider 注册器或另一个配置包。

## 5. 全量逐函数审阅

说明：“保留”表示在当前需求下职责与实现基本合理，不是形式化正确性证明；“调整”主要是职责/冗余；“修复”对应已确认行为问题。以下覆盖全部 117 个函数/方法，行号为定义起始行；模块级语句、类与常量另列，避免只看函数漏掉 import 副作用。

### 5.1 `src/llm_ocs_bridge/__init__.py` · 1 行 · 0 个函数

只有包说明字符串。**保留**；不添加初始化、配置读取、客户端实例化或自动导入。这个空包入口比“方便使用”的隐式副作用更优雅。

**增量复核 06：** Serena 符号表为空，文件仍只有上述说明字符串。配置 import 副作用来自 `config.py`，不是包入口；因此不需要改 `__init__.py` 来修复 F04，也不建议通过包级自动导入替代显式加载。

### 5.2 `src/llm_ocs_bridge/app.py` · 482 行 · 22 个函数

模块级 `app = FastAPI(...)` 读取全局 settings，使导入依赖完整环境；重构配置入口时一并处理。`RequestError` 是 HTTP 输入边界错误，保留，不和 LLM 错误混在一起。

**增量复核 11：** 再读 `search` 与 `_respond_with_answer`，确认 LLM 错误映射只包围 `_answer_with_llm`，不包围后续答案缓存写入；前面的缓存读取/指标异常也不会自动变成 OCS 成功或 fallback。这符合不静默吞掉内部错误的方向，重构不能把整条链路包进“大一统 except”。需要修正的是异常身份：任意 RuntimeError 被记录成 MissingKey，会把本地存储/指标错误误认成凭据问题，应使用准确的窄错误类型。此处为控制流复核，没有伪称新 HTTP 实测。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `build_runtime` | 38 | 调整 | `SimpleNamespace` 没有字段契约；只构造 store、不建立连接。改为 typed Runtime，由生命周期完成实际初始化；配置显式传入。 |
| `lifespan` | 51 | 修复/调整 | 正确采用 lifespan 而非分散启动回调；但关闭串行裸 await，一个异常会跳过其他关闭。用资源上下文栈，覆盖创建及启动失败；见 F10。 |
| `_runtime` | 69 | 保留 | 读取 request.app.state 的单一访问点有用；把返回类型从 SimpleNamespace 换成 Runtime 即可，不建 accessor 类。 |
| `_auth_ok` | 73 | 修复 | constant-time 比较方向正确；非 ASCII str 会抛异常。修 F08，统一配置与请求 token 编码。 |
| `_ocs_response` | 80 | 保留 | OCS 成败 envelope 集中表达，属于 HTTP adapter；不把此返回类型带进 service。`msg` 的 truthy 判定在现有调用中没有空串错误路径。 |
| `_parse_request` | 90 | 调整 | 外部类型校验必要，None/别名兼容属于已支持协议；删除末尾 title/options/question_type 纯赋值转发，避免新增模型框架。 |
| `_read_body` | 120 | 保留/小调 | 先检查声明大小，再流式累计实际字节，处理 UTF-8/JSON/object，边界正确。Content-Length try 范围可缩小，避免自己抛 RequestError 又原样捕获。GET 大小是解析后 JSON 大小，不是原始 wire 大小，应清楚定义。 |
| `_validate_query` | 160 | 调整 | 长度/类型/标签不重复校验合理；属于题目域校验，应和 questions 归一化入口靠近。空 title 应在同一个校验入口处理，不必散落在 route。 |
| `_answer_key` | 175 | 移动 | 模型、来源模式、prompt/search 版本、证据配置隔离是正确设计；归 cache 模块。不要为缩短 key 删除这些字段。 |
| `_search_key` | 190 | 移动 | 完整题目、选项和类型参与哈希，优于只用标题；与答案键统一归属，保留独立 namespace。 |
| `_get_answerer` | 200 | 简化 | 无 await 的 async helper；改同步或启动时构造。当前没有 await 间隙，不把它误报为 lazy-init 并发 bug。 |
| `_cached_answer` | 206 | 移动 | 缓存边界复验、坏缓存删除及计数正确；迁到 service。不要删掉这里的验证来“消除重复”。 |
| `_answer_with_llm` | 230 | 简化/移动 | 混合 fallback 计数、attempt hook、LLM 调用和二次答案校验；保留一次调用入口/观测职责，删除重复内部校验。 |
| `_answer_with_llm.record_attempt` | 239 | 保留 | 仅记录实际 attempt 的闭包很小且合理；不抽成 class。注意 SQLite 失败发生在 HTTP 之前，llm_calls 的定义是尝试发起边界，而非远端收到。 |
| `_providers` | 254 | 保留/小调 | 单 provider 的状态列表只用于观测；不因此建 provider registry。函数保留或同处内联均可，不是高优先级重构。 |
| `_resolve_evidence` | 258 | 移动/收敛 | 核心业务应进 service；当前四元组中 score 被 route 丢弃，cache 分支填 0。用最少的命名字段表达证据与失败原因；保留 search-cache miss、坏缓存删除和 fallback 顺序。 |
| `_recent_search_observability` | 314 | **移动** | HTTP 层调用私有 `_db` 并写 SQL，是最明确的职责越界。变为 `SearchCacheStore.recent(limit)` 一类公共方法；时间排序与字段投影留在 store。 |
| `_respond_with_answer` | 341 | 拆职责 | 分离业务调用/缓存写入与 HTTP 错误映射。`RuntimeError → MissingKey` 范围过大；`LLMError`/`Exception` 重复体可合并，但不能吞掉未知内部错误后仅贴 LLM 标签。 |
| `health_root` | 382 | 保留 | GET/HEAD 共用极小 handler，正确；无需为每个路径造函数。 |
| `health` | 387 | 保留/定义清楚 | 当前只说明进程活着及 provider 已配置，并不检查 SQLite/远端 readiness。无理由让每次 health 请求调用外部服务；启动时完成必要初始化即可。uptime 可改 monotonic。 |
| `stats` | 399 | 调整 | 鉴权和响应组装属于 HTTP；把存储查询交回 store。hit_rate 是跨 search/answer cache 的 lookup 命中率，不是用户请求命中率，不应混称。 |
| `search` | 432 | 调整 | 分支顺序正确；route 只应读入、鉴权、调用 service、映射结果。用一个服务返回值消除两组 `_respond_with_answer` 的 mode/fallback/provider 重复参数，不能改掉 fallback 缓存延迟读取。 |

补充：纯 invalid-answer 耗尽只返回“答案格式不合法”，没有单独错误记录；这是可观测性缺口，不是证明 LLM 请求没有执行。是否计数需明确产品指标，避免把每次正常重试都记成最终错误。

### 5.3 `src/llm_ocs_bridge/cache.py` · 229 行 · 12 个函数

两个 store 的 schema/字段确实不同，保留两个明确类；问题是共享数据库所有权，不是它们都叫 get/set/clear。没有证据需要通用 CRUD 父类再加一层。

**增量复核 02：** 重新核对 `SearchCacheStore.set` 与 `decode_search_results`：写入会刷新 `created_at`，读取不刷新，因此当前是按最近写入时间淘汰，**不是 LRU**。坏 payload 允许逐条丢弃，不要求整批作废；这是明确的持久化边界策略。结构重构不要意外改成按访问时间淘汰，也不要把坏条目过滤挪进底层 SQLite 类。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `SearchCacheStore._init_db` | 12 | 调整 | 只建搜索表合理；调用 `_db()` 反向进入 connect，形成半初始化契约。改成接收连接；结合 F10 调整。 |
| `SearchCacheStore.get` | 31 | 保留 | 主键查询、参数绑定、显式关闭 cursor 合理；返回 str 是内部持久化契约。若继续传 payload 字符串，解码责任必须明确。 |
| `SearchCacheStore.set` | 41 | 保留/收敛 | UPSERT 比先查再插清楚；SQL 长不等于函数职责多。元数据全量存储有观测用途，不能无证据删除；序列化应由 store 接收 typed results 后承担，减少 app 包装。 |
| `SearchCacheStore.clear` | 87 | 保留 | 全表清理直白；当前 `_write` 会在 DELETE 前做容量淘汰，应在 F02 中一起消除不必要预清理成本。 |
| `SearchCacheStore.delete` | 91 | 保留 | 单键删除安全；不要为了与 answer store 重复四行而引入表名模板工厂。 |
| `AnswerCacheStore._init_db` | 99 | 调整 | 同 SearchCache 初始化问题；schema 与搜索缓存不同，不建议合并物理表。 |
| `AnswerCacheStore.get` | 118 | 保留 | 字符串返回合理；题型正确性由缓存消费边界检查，避免 store 依赖整个题目服务。 |
| `AnswerCacheStore.set` | 128 | 保留 | 模式、模型、prompt 等元数据显式，UPSERT 合理；后续若合并数据库 owner，不应把这些字段塞入泛型 JSON 一列。 |
| `AnswerCacheStore.clear` | 174 | 保留 | 范围明确；SQL 相似性不是合并类的充分理由。 |
| `AnswerCacheStore.delete` | 178 | 保留 | 坏缓存精准失效有真实调用，不是冗余 API。 |
| `encode_search_results` | 185 | 保留/内聚 | 明确稳定字段的序列化比随意 `__dict__` 更安全；可以作为 store 内部细节，减少 app 的编码知识。无须为几行引入序列化依赖。 |
| `decode_search_results` | 199 | 保留 | JSON、容器、字段、URL 都是持久化边界，坏条目过滤符合已有策略。这里的容错有真实缓存语义，不等同于应删除的内部 fallback。 |

`serialize_options` 用 `#` 拼接元数据对包含 `#` 的数组元素不可逆；当前缓存主键使用 JSON options 列表，**没有因此产生主键碰撞**。如果这些元数据用于回放/重算，改为无损 JSON；否则不要把观测列当成可靠反序列化输入。

### 5.4 `src/llm_ocs_bridge/config.py` · 166 行 · 8 个函数

`Settings(frozen=True, slots=True)` 作为不可变配置类型很好；问题在第 98–143 行所有默认值都在 class body 读环境，和第 9 行隐式 dotenv。它是 import-time 全局快照，而非可重复调用的配置 loader。

**增量复核 04（新增实测）：** 在禁止 dotenv 的合成环境中导入 `Settings`，原 PORT 为 8009；将进程环境改为 8123 后再次构造 `Settings()`，新实例仍为 8009。这验证了“类字段默认值只求值一次”，不只是全局 `settings` 单例没有刷新。因此仅删除 `settings = Settings()` 不足以修好配置加载边界，环境读取必须移出 class body。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `_bool` | 12 | 保留 | 明确真/假集合，未知字符串 fail fast；配置是外部边界，这不是多余防御。 |
| `_text` | 24 | 保留 | strip、空值、必填错误集中，简短；不要静默把空值转成任意默认。 |
| `_int` | 33 | 小调 | 转换、上下界明确；required 语义与 unused default 参数可以收敛。`assert value is not None` 在前面的分支后冗余，但不是功能缺陷。 |
| `_optional_int` | 58 | 小调 | 可选 limit 是真实需求；可复用整数校验步骤，不删 None 语义，也不增加复杂描述器。 |
| `_float` | 71 | 保留 | finite 检查及开/闭下界做得正确；不把 NaN/inf 放过。统一异常文本属于低优先级。 |
| `_validate_secret` | 149 | 保留/补边界 | 必填、示例占位符检查有用；token 编码约束与 F08 配套。不应输出值，不做虚假的联网有效性判断。 |
| `validate_runtime_settings` | 156 | 调整 | 校验参数应为 Settings 对象。当前不强制 fallback-disabled 时提供 Exa，现有路由测试明确允许启动后返回业务错误，不能未经确认偷偷改变。 |
| `validate_check_settings` | 163 | 调整 | 比 runtime 更严格的 Exa 检查目前有明确差异；统一配置对象/加载路径，是否统一验证级别是产品取舍，不把现状自动判成 bug。 |

`max_options_count<=8` 是已确认 OCS label 不变量；`min_search_quality_score>=0` 与 F06 联动；`max_cache_bytes>=1` 虽形式合法，却可能小于 SQLite 自身最小体积。优先把不可用配置在启动阶段暴露，不为每个数值任意增加“推荐最低值”。

### 5.5 `src/llm_ocs_bridge/llm.py` · 229 行 · 6 个函数

`LLMError` 及四个子类（InvalidAnswer、ResponseTooLarge、ProviderProtocol、Transient）分别用于调用策略/错误映射，**有实际语义，不建议合并为一个 RuntimeError**。`AttemptHook` 的 Callable 类型也足够，不需要 observer 接口类。

**增量复核 10：** 重新沿 `answer → _call_model` 检查异常传播：attempt hook 在 HTTP try 之前执行，hook 的数据库异常不是 `TransientLLMError`，不会被上游重试循环重试；取消也不会被此循环吞掉。这是应保留的区别，不能为缩短代码改成 `except Exception: retry`。同时，原始 HTTP envelope JSON 非法属于 provider protocol error，message 内答案 JSON/题型不合法才走答案重试，两层解析不要混并。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `LLMAnswerer.__init__` | 46 | 调整 | 允许覆盖 key/base/model 有测试用途；其余行为却仍读全局 settings，形成半注入。传完整配置或所需显式字段，清楚定义空字符串是否应覆盖。 |
| `LLMAnswerer.answer` | 59 | 保留核心/收敛 | 重试次数、指数退避、无效回答反馈、瞬态错误与确定性错误分开，结构基本合理。接收 OCSQuery，删二次 options 归一化；答案 codec 归此模块。不要改成捕获所有异常重试。 |
| `LLMAnswerer.aclose` | 98 | 保留 | 关闭连接池并置空，职责单一；由 owner 保证调用，不额外加重连 fallback。 |
| `LLMAnswerer._call_model` | 103 | 保留边界/谨慎简化 | 是一个完整 HTTP adapter：复用 AsyncClient、token 上限、流式字节限制、状态码分类和 envelope 校验。手写 68 行不自动等于垃圾；SDK 替换须保留这些不变量。当前 timeout 是 HTTPX 超时配置，不能无证据称为全流程硬总时限。 |
| `LLMAnswerer._parse_payload` | 173 | 保留/小调 | JSON 与 fenced JSON 的支持有测试；裸文本返回后通常被 `_pick_field` 拒绝，不等于接受任意非结构化答案。可用明确解析失败值减少误读，别再增加“猜答案”解析器。 |
| `LLMAnswerer._build_messages` | 181 | 保留/小调 | system 与不可信 JSON 分离正确；五种输出规则是固定业务分支，可用 dict 表达，但 if/elif 本身不是坏设计。无需独立 prompt 包。 |

额外注意：`max_tokens`/temperature 对每个 OpenAI-compatible 服务不一定有相同支持；本次没有真实 provider 测试，不断言当前配置模型不兼容，也不以“最新模型”替换用户模型。重试总耗时包含多个请求及退避，不能直接用单次 HTTP timeout 推导整体 deadline。

### 5.6 `src/llm_ocs_bridge/main.py` · 301 行 · 18 个函数

`CheckError` 作为 CLI 可呈现错误类型合理；`_ArgumentParser` 仅负责中文 usage，不是多余的 OOP 框架。延迟导入 app/config 是为了环境坏掉时仍显示 help，应在去掉 import 副作用后重新简化，而不是现在机械提升所有 import。

**增量复核 05（新增实测）：** 禁止 dotenv，配置临时 cache 路径，仅把 `LLM_TIMEOUT_SECONDS` 设为非数值后调用 `run(['clear-cache', '--scope', 'search'])`：命令 exit 1，提示 LLM 超时配置无效，数据库尚未创建。证明 `validate=False` 只跳过后置校验，不能跳过导入时解析；清缓存被不相关 LLM 配置阻塞。应让清理入口只加载它真正需要的存储配置，不添加忽略配置错误的 fallback。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `_ArgumentParser.format_help` | 25 | 保留 | 替换一个固定前缀，范围小；不用重写 argparse。 |
| `_ArgumentParser.format_usage` | 28 | 保留 | 同上，覆盖不同输出入口有实际理由；两行重复不值得再抽 helper。 |
| `_clear_cache` | 32 | 调整 | 即使只清一种缓存也连接两种 store；connect 在 try 之前，第二次连接失败时第一资源不在 finally 保护范围。只打开所需 store，并让上下文栈覆盖创建/连接；不改变 scope 语义。 |
| `_help_options` | 49 | 保留 | 三处复用的帮助选项提取合理；private argparse 类型注解不是运行时依赖 bug，但可避免让私有类型成为公共 API。 |
| `_build_parser` | 55 | 保留 | 声明式命令布局清楚；不引入 Click/Typer 只为替换几十行帮助文本。 |
| `_status` | 101 | 保留 | stderr 输出集中，保证 stdout 只留 OCS JSON；不合并 stdout/stderr。 |
| `_print_ocs_instructions` | 105 | 保留 | 用户操作说明集中；不是运行时代码“冗余”，不需要为了函数短把文本挪成模板文件。 |
| `_check_env_file` | 117 | 保留 | 可读普通文件和 POSIX 0600 校验属于真实边界；不改成遇错自动 chmod 用户文件。 |
| `_load_settings` | 132 | 调整 | 当前命令参数组合隐藏加载/验证差异，且导入缓存让重复加载无效；显式 loader 返回 Settings，修 F04。 |
| `_check_cache_path` | 155 | 保留/界定 | 是目录/文件权限预检，不验证 SQLite 文件格式、表初始化或容量维护能成功；不要把“路径可写”等同于整个缓存已可用。真实初始化应在启动/受控 check 内验证。 |
| `_check_llm` | 168 | 保留 | 实际请求在 finally 关闭客户端，逻辑简单；本次只检查并跑其 mock 测试，不执行真实探测。 |
| `_check_exa` | 178 | 保留/定义清楚 | 关闭正确；不检查返回证据是否非空，实际上证明 API 可调用，不证明某个 probe 可通过当前质量策略。是否提升为端到端检查需明确承诺。 |
| `_client_host` | 189 | 保留 | wildcard 到 loopback 和 IPv6 括号是客户端 URL 构造规则，不是应删除的“特殊情况”。 |
| `_ocs_config` | 199 | 保留 | OCS 协议边界应留在 CLI；handler 是固定 JS 代码，未拼接不可信代码。输出 token 是刻意功能，不误报泄露；实际 OCS UI 集成未在本次重测。 |
| `_safe_error` | 235 | 简化 | `_utils` 实际不读配置，无需 RuntimeError fallback；直接使用可靠脱敏入口，修 F09。参数名 max_bytes 应始终真按 bytes 截断。 |
| `_check` | 244 | 保留主流程 | 检查顺序和成功后才输出 JSON 清楚；捕获边界错误转退出码合理。LLM/Exa 串行检查对一次性 CLI 无证据需要并行。 |
| `_exit_with_config_error` | 277 | 保留 | 两处复用统一错误提示有价值；应保证传入错误本身不含敏感配置值。 |
| `run` | 281 | 修复/调整 | F03：reload 不能传 app 实例；F04：统一 .env 路径。clear-cache 不应被无关 LLM 数值配置阻塞；不要顺手更改默认命令。 |

### 5.7 `src/llm_ocs_bridge/metrics.py` · 146 行 · 7 个函数

`_COUNTERS` 是明确的指标集合，适合保留；`incr` 却接受任意字符串，拼写错误会悄悄生成新行。可以通过 Literal/窄类型和测试收紧内部契约，不必加完整监控 SDK。现有部署量级不要求 Prometheus。

**增量复核 03：** 再读 `log_error` 和 `get_counters` 后补清存储策略：错误日志同时受“条数上限”和数据库全局容量淘汰约束；计数表不在 `_PRUNABLE_TABLES` 中。因此清理缓存不会重置累计指标，容量维护也不是“所有表都能删”。不要在合并数据库 owner 时把计数与可丢弃缓存一起清空。此结论来自当前 SQL/白名单，不是新增运行时测试。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `MetricsStore._init_db` | 26 | 调整 | 两个表在多个 await 中初始化，暴露 F10；传入未公开的 connection，完成后统一 ready。 |
| `MetricsStore.incr` | 50 | 保留 | UPSERT 计数入口清楚；每次独立事务是真实成本，和调用方一起优化批量，而不是修改 counter 原子性。 |
| `MetricsStore.incr_many` | 54 | 删除或使用 | Serena 查引用为空；如果保留就用在同一阶段的多个计数，否则是死接口。不能声称现有代码已经批量化。 |
| `MetricsStore._incr` | 60 | 保留 | 同一条 SQL 原子加 delta，优于 Python 读改写；timestamp 每次计算不是当前主瓶颈。 |
| `MetricsStore.log_error` | 77 | 修复依赖/保留 | 记录并按条数裁剪有真实约束；依赖 F01 事务清理和 F09 脱敏正确性。不要单独吞掉 DB 错误以制造“日志成功”。 |
| `MetricsStore.get_counters` | 104 | 保留 | 默认零值补齐好用；返回额外 DB 指标是否应允许需统一内部指标契约，不属于现有接口已证实错误。 |
| `MetricsStore.get_recent_errors` | 114 | 保留/收敛 | 上限裁剪合理；读取时再次脱敏能覆盖旧记录，是持久化边界措施，不直接删。先抽出一条记录的格式化表达，避免复杂嵌套条件。 |

### 5.8 `src/llm_ocs_bridge/models.py` · 33 行 · 1 个函数

`OCSQuery` 不可变 tuple 选项表达良好。`SearchResult` 为可变 dataclass，但当前映射后不见原地更新需求；可考虑 frozen，**不是仅为风格必须改**。source/published_at 进入缓存而非 prompt，有 provenance 用途；不要只因没进入 prompt 就删字段。

**增量复核 07：** 再读证据校验并用 Serena 追踪引用，确认相同完整性规则被缓存解码和搜索 adapter 共用。校验不依赖 config、网络或 SQLite，放在薄 models 模块可以接受；应删除的是 search 中无意义的转发包装，而不是复制两份校验或把 models 扩成带 I/O 的领域服务。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `is_valid_search_result` | 23 | 保留 | 非空字符串及 HTTP(S) netloc 检查是最小证据完整性边界，复用于缓存和 SDK 结果。它不是可信来源、安全访问或有效 DNS 的证明；当前代码不拿此 URL 发请求，不误报 SSRF。 |

### 5.9 `src/llm_ocs_bridge/search.py` · 144 行 · 10 个函数

单个 `ExaSearch` 的大小与职责可以接受；真正的问题是 typed SDK 被 Any/getattr 抹平，打分规则又被称作 semantic。无需因“以后会有别的 provider”预建基类。

**增量复核 09：** 再读 `search` 与 `score_results`，确认当前门槛作用于**整批平均分**，不是逐条相关性过滤或重排；只要整批通过，所有格式有效结果都会按原顺序保留。报告中的“可用证据”不可理解为每条都经过语义验证。F05/F06 修复时必须写清批次与单条的契约，不以简单降低总阈值冒充相关性改进；是否增加逐条门槛需用正反例评估，未作为新实测缺陷计数。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `ExaSearch.__init__` | 15 | 调整 | `Any` 丢失 SDK 合同；用真实 AsyncExa 类型，配置/依赖注入保持最少字段。 |
| `ExaSearch.enabled` | 20 | 保留 | 将可用配置状态集中表达有用；它只说明配置了 key，不说明 key 真实有效。 |
| `ExaSearch._get_client` | 23 | 简化 | lazy client 创建没有 await，并非本次并发 bug；key 重复 strip 和 RuntimeError 可在入口规范化后收敛。不要增加备用客户端。 |
| `ExaSearch.aclose` | 33 | 保留 | 锁定 SDK 的 AsyncExa 不提供 aclose，关闭 `.client` 有实际依据 [R7]；测试已验证。不能套别的 SDK 经验随意改。 |
| `ExaSearch.search` | 38 | 修复 | asyncio.timeout 外部调用边界正确；F06 的 0 分绕过需独立修。limit 同时传 SDK 并切片是执行项目侧数量上限的手段，不因重复就直接删。 |
| `ExaSearch._map_results` | 64 | 修复/简化 | F07 丢 highlights；typed Result 字段不需 snake/camelCase 两套兜底。转换与有效性过滤应清楚分工。 |
| `ExaSearch._is_valid_result` | 85 | 删除 | 只有 search 一处调用，原样转发 models 函数；删包装，不删实际边界校验。 |
| `ExaSearch.score_results` | 89 | 修复策略/收敛 | 计数、摘要长短和域名只是启发式；F05/F06 不能靠调总分补救。0 overlap 必须独立不可用；权威性不等同于域名含 docs。没有验证数据集不能称其可靠语义质量评分。 |
| `ExaSearch._semantic_score` | 120 | 修复 | F05；改名为 overlap 更准确，预计算 query terms。多条结果共用同一 terms，不反复构造。 |
| `ExaSearch._source_score` | 129 | 保留为可解释启发式 | 正确提取 hostname 并检查域名后缀边界，未把 `evilwikipedia.org` 当 Wikipedia。`.gov/.edu` 不覆盖所有国家域名，`docs.*` 不保证权威；不据此作安全判断。 |

### 5.10 `src/llm_ocs_bridge/storage.py` · 177 行 · 13 个函数

`_PRUNABLE_TABLES` 白名单使表名动态 SQL 不直接受请求控制，因此不是已发现的 SQL 注入；但它证明本类并不真正通用。名字和 owner 应坦诚表达“本项目数据库”，而不是假装可复用微型 ORM。

**增量复核 01：** 再读完整 `AsyncSQLiteStore` 后，F01/F02/F10 的判断保持。补充一个静态边界事实：`_write` 在 commit 成功后才执行权限与容量维护；后两项抛错时，调用方看到失败，但业务写入已经提交，rollback 分支不会运行。应把“事务提交结果”和“维护失败”明确分开，而不是声称上下文抛错就代表写入没发生。本条未注入权限失败实测，不提升为新的已复现缺陷。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `AsyncSQLiteStore.__init__` | 24 | 调整 | 路径归一化有用；进程全局按路径保存 asyncio.Lock 隐藏生命周期且不清理。先显式数据库 owner，避免分散锁 registry；不引入连接池来抵消三个连接的复杂度。 |
| `AsyncSQLiteStore.connect` | 30 | 修复 | F10：连接非 None 不能代表所有表 ready；PRAGMA 前段也应在失败清理范围内。双重检查本身用于懒初始化，但启动时显式初始化能删除这类状态。 |
| `AsyncSQLiteStore.close` | 60 | 调整 | 正常关闭路径直白；close 与 connect 的状态由不同锁约束，需要统一 owner 生命周期。不要承诺任意并发 close/get 都安全，先明确 API 合同。 |
| `AsyncSQLiteStore._db` | 68 | 调整 | 断言已有连接合理，但它承担隐式 I/O 又被初始化回调重入。显式 ready 后只返回连接，内部状态错误直接暴露。 |
| `AsyncSQLiteStore._write` | 74 | **修复** | F01 取消必须 rollback；F02 维护策略不能无条件在每次写前后删除/重建。事务边界值得保留，不把它内联到所有 store。 |
| `AsyncSQLiteStore.database_size_bytes` | 87 | 保留 | 三文件口径符合约定，Path stat 是同步操作；暂不为几次 stat 引入异步文件 I/O 库。不包括 VACUUM 临时峰值，见 F02。 |
| `AsyncSQLiteStore._enforce_capacity_locked` | 92 | **修复** | F02 先 checkpoint 后判断淘汰；每批 VACUUM 成本高且 checkpoint 结果被丢弃。容量政策应集中，不被多个继承实例隐式反复执行。 |
| `AsyncSQLiteStore._oldest_rows` | 115 | 保留意图/调整 | 跨 search/answer/error 全局按时间选择行符合策略；每次扫描 schema 和排序重复，启动已知表后可收敛。添加索引要用真实规模证据。 |
| `AsyncSQLiteStore._group_rows` | 143 | 保留 | 小函数把按时间行转成按表 rowid，SQL 批删所需，名字清楚；defaultdict 不会神奇提升业务性能。 |
| `AsyncSQLiteStore._database_files` | 149 | 保留 | main/WAL/SHM 单一来源明确；最多可在构造时保存 tuple，不值得复杂缓存。 |
| `AsyncSQLiteStore._prepare_database_file` | 153 | 保留 | O_EXCL + 0600 避免先创建再收权限，符合实际文件边界。FileExistsError 是预期状态，不是隐藏业务失败。 |
| `AsyncSQLiteStore._secure_file_permissions` | 166 | 保留 | 设置数据库及 sidecar 权限有明确理由，失败暴露正确；调用频率可收敛，不能直接删除权限逻辑。 |
| `AsyncSQLiteStore._init_db` | 176 | 调整 | 当前 NotImplementedError hook 本身无害；组合数据库对象后可删除继承扩展点。不要只为规范把它改 ABC 而保留错误生命周期。 |

### 5.11 `src/llm_ocs_bridge/utils.py` · 296 行 · 20 个函数

这是最明显的杂物模块：通用文本、题型域规则、提示、证据、哈希、错误日志混放。拆分依据应是调用边界与业务概念，不是“工具超过 N 行”。相同 label 正则定义两份也说明数据解析没有单一入口。

**增量复核 08（新增实测）：** `validate_answer('first# #third', 'completion')` 返回 True；相同内容从 `normalize_answer_payload({'answers': ['first', ' ', 'third']}, 'completion')` 输入却返回空串。说明 live LLM 和缓存验证的空白段语义不一致；当前正常生成路径不会产出该坏值，影响集中在旧/损坏缓存。修复应共享逐段归一化规则，而不是删缓存验证。没有据此宣称所有填空答案都会出错。

| 函数 | 行 | 判断 | 精确意见 |
| --- | ---: | --- | --- |
| `normalize_text` | 30 | 保留 | 通用空白归一化短而清楚；当前协议已依赖此规则。代码/诗歌等空白敏感题是否支持属于业务范围，不能未经确认改变缓存归一化。 |
| `sanitize_error` | 36 | 修复 | F09：quoted JSON/header 漏匹配；仍在专门的通用文本边界保留。不要再扩张为全局安全框架。 |
| `normalize_query` | 44 | 移动 | OCS 题目构造属于 questions，不是通用工具；一次归一化后传 OCSQuery。 |
| `serialize_options` | 56 | 移动/收敛 | 仅缓存元数据用途，移到 cache；`#` 无法无损表示任意数组元素，需用于回放时改 JSON。 |
| `format_options_for_prompt` | 60 | 移动 | 归 questions 的格式化；复用 label/text 解析。避免与 allowed_option_labels 独立实现同一标签规则。 |
| `render_search_query` | 70 | 移动 | 保留完整题目/选项/类型，与 search adapter 同处更清晰；不要改成只查提取关键词。 |
| `question_cache_parts` | 81 | 移动或内联 | 三字段拆包用于 key schema，归 cache；不是一切问题都应以 `tuple[object, ...]` 在应用层传播。 |
| `question_terms` | 85 | 移动/修复 | 搜索打分专用，归 search；F05 需要改善中文表示，去重保留顺序做得合理。 |
| `cache_key` | 93 | 移动 | 稳定 JSON + SHA256 没必要换库；归 cache，显式 schema 避免泛化到不可 JSON 的 object。 |
| `format_search_context` | 99 | 移动/简化 | LLM 证据 prompt 渲染，归 llm；去掉两份循环与重复全文编码，预算应优先形成有效证据而非只有头部。 |
| `normalize_answer_payload` | 136 | 移动 | 纯 LLM/OCS 输出 codec，归 llm；严格拒绝非结构化值正确。不同题型分支是真业务，不能为“无 if”压成难读技巧。 |
| `validate_answer` | 179 | 移动/小修 | 标准题型 label 检查有价值，缓存边界保留调用；completion 的恒真检查删掉并检查空白段。普通开放题仅检查非空符合当前契约。 |
| `_split_options` | 210 | 移动/收敛 | 多种输入是外部协议支持，归 questions；不要在内部 OCSQuery 流程重复调用。空白项过滤会改变位置，label 歧义应用真实 OCS 样例定规则。 |
| `allowed_option_labels` | 226 | 简化/移动 | 直接集合构造，共享 label 解析；显式与隐式标签碰撞目前被 app 校验拒绝，不是默默接受，别误报。 |
| `_truncate_utf8` | 236 | 简化 | 编码一次再切片解码可替代二分反复编码；保持按字节、不拆多字节码点的测试。 |
| `_canonicalize` | 249 | 收敛/移动 | 支持任意字典与序列、返回 Any，超出实际 key 形状；定义 typed cache 输入。Python Any 并非违反“TypeScript 禁 any”，但这里确实削弱契约。 |
| `_pick_field` | 262 | 移动 | 答案 codec 的内部 helper，归 llm；dict 外返回 None 表示格式不合法合理，不扩充任意别名。 |
| `_normalize_option_list` | 271 | 简化/移动 | 两条输入路径重复校验；先统一 tokens 再一次校验/去重/排序，归 llm。重复选项拒绝是协议要求，不应改为静默去重。 |
| `_strip_option_label` | 290 | 合并解析 | 与 capture regex 功能重叠；让一次解析同时给 label 和 text，归 questions，而非维护两份 regex。 |
| `_extract_terms` | 294 | 修复/移动 | F05 核心；丢弃长度 1 的 token 对部分单字/数学符号题不合适，须纳入相关性样例，不机械加通用 NLP 依赖。 |

## 6. 验证记录与测试缺口

### 6.1 已执行的仓库验证

在原业务代码上执行，使用 lock，不修改依赖：

```bash
uv run --frozen pytest -q
uv run --frozen ruff check src tests pyproject.toml
uv run --frozen python -m py_compile src/llm_ocs_bridge/*.py tests/*.py
```

结果：**121 passed；Ruff 全部通过；py_compile 通过。** pytest 有 1 个 Starlette TestClient 关于 HTTPX 的弃用警告；测试没有失败，不把警告当成本次必须更换 HTTP 客户端的证据。未运行类型检查，因为项目没有声明相应工具及配置；本报告不宣称“类型检查通过”。

现有测试已覆盖完整题目缓存隔离、fallback 恢复顺序、结构化输出、上游错误分类、大小限制、基本并发写、文件权限、CLI help/check 等重要行为。缺口不是“没有测试”，而是**正常案例和先前回归较多，资源取消、真实维护顺序、不同工作目录和中文相关性对照不足**。

### 6.2 独立复现记录

复现使用测试环境变量、合成 key、`TemporaryDirectory` 和假 SDK/ASGI 输入；禁止 dotenv 读取真实配置。数据库均为临时文件，未执行真实 `check` 命令的联网探测。

| 编号 | 实施方式 | 实际结果 | 证据适用范围 |
| --- | --- | --- | --- |
| F01 | 完成 INSERT 后暂停并取消 `_write` 任务 | `in_transaction=true`；其他连接 `database is locked`；下一次写提交了取消行 | 确认事务上下文取消语义错误；不是线上事故统计。 |
| F02 | 12 条相同形状记录的两个临时库；分别仅 checkpoint / 执行现有容量维护 | 原 176976 bytes；checkpoint 后 61440；65536 预算下原策略删到 0 条 | 确认淘汰顺序有误；字节数字受 SQLite 版本/页大小影响。 |
| F03 | 合成 Settings 设置 debug=True，调用 `run([])` | Uvicorn 警告要求 import string，`SystemExit(1)` | 确认当前启动失败；未验证修复后的 reload。 |
| F04 | 原样复制 config.py 到隔离安装目录，CWD `.env` 提供 PORT | 子进程 exit 1，`PORT is required` | 确认按调用源码查找不同于 CWD；未创建真实发行 wheel。 |
| F05 | 直接用中文题目和明确答案证据调用打分 | terms 为完整问句，score=0.0 | 确认这个反例；没有量化真实搜索准确率。 |
| F06 | fake AsyncExa response，阈值 0 | 无关结果 retained=1、score=0.0 | 确认零阈值与硬门槛冲突。 |
| F07 | 两条 highlights 的 SDK 形状对象 | snippet 仅 `Introduction` | 确认后续 highlights 被丢弃。 |
| F08 | HTTPX ASGITransport，非 ASCII header bytes | `/search` HTTP 500 | 确认应用边界异常；没有实测每种反向代理/HTTP parser。 |
| F09 | quoted JSON/Bearer 与普通 key=value 的合成文本 | quoted 两项未脱敏，普通 key=value 被脱敏 | 确认脱敏漏匹配；没有真实密钥泄露证据。 |
| F10 | 首次 metrics 初始化两次 CREATE 之间用事件控制调度 | 并发 get_recent_errors 报 `no such table: error_log` | 确认半初始化 store 可见；不夸大成所有 HTTP 路由必现。 |

额外检查：`format_search_context` 的预算为 1 byte 时只返回 `[`。这不是越界，而是说明“没有超过预算”不等于“提供了有用证据”。它被归入改进项，没有独立升级为严重缺陷。

### 6.3 最短、可重复执行的核心复现

以下片段可在仓库根目录直接执行，不依赖本次临时探针文件。只访问新建临时数据库，禁止读取真实 `.env`；不调用 OpenAI 或 Exa API。它覆盖最重要的事务取消、WAL 误删和中文/脱敏反例；其他确定性调度步骤见上表与对应 F 条目。

```bash
uv run --frozen python - <<'PY'
import asyncio
import contextlib
import os
import runpy
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

os.environ.update(runpy.run_path('tests/conftest.py')['_TEST_ENV'])
os.environ['PYTHON_DOTENV_DISABLED'] = '1'

from llm_ocs_bridge import storage
from llm_ocs_bridge.cache import AnswerCacheStore, SearchCacheStore
from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.search import ExaSearch
from llm_ocs_bridge.utils import question_terms, sanitize_error

async def check_cancel(directory):
    path = directory / 'cancel.sqlite3'
    store = AnswerCacheStore(str(path))
    await store.connect()
    entered = asyncio.Event()

    async def write_then_wait():
        async with store._write() as connection:
            await connection.execute(
                'INSERT INTO answer_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                ('cancelled', 'q', '', '', 'model', 'v1', 'search', 'answer', 1),
            )
            entered.set()
            await asyncio.Event().wait()

    try:
        task = asyncio.create_task(write_then_wait())
        await entered.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        print('transaction_after_cancel:', store._conn.in_transaction)
        with sqlite3.connect(path, timeout=0) as observer:
            try:
                observer.execute('DELETE FROM answer_cache WHERE cache_key="absent"')
            except sqlite3.OperationalError as exc:
                print('other_connection:', str(exc))
        await store.delete('absent')
        print('cancelled_row_after_next_commit:', await store.get('cancelled'))
    finally:
        await store.close()

async def seed(path):
    store = SearchCacheStore(str(path))
    await store.connect()
    for index in range(12):
        await store.set(str(index), OCSQuery('question'), 'exa', 0.8, 'x' * 1000, 'v1')
    return store

async def check_capacity(directory):
    store = await seed(directory / 'policy.sqlite3')
    control = await seed(directory / 'checkpoint.sqlite3')
    settings = storage.settings
    try:
        print('before:', store.database_size_bytes())
        async with control._conn.execute('PRAGMA wal_checkpoint(TRUNCATE)') as cursor:
            await cursor.fetchall()
        size = control.database_size_bytes()
        print('checkpoint_only:', size)
        storage.settings = replace(settings, max_cache_bytes=size + 4096)
        await store.delete('absent')
        async with store._conn.execute('SELECT count(*) FROM search_cache') as cursor:
            print('rows_after_policy:', (await cursor.fetchone())[0])
    finally:
        storage.settings = settings
        await control.close()
        await store.close()

async def main():
    with tempfile.TemporaryDirectory(prefix='ocs-review-') as temporary:
        directory = Path(temporary)
        await check_cancel(directory)
        await check_capacity(directory)
    query = OCSQuery('水的化学式是什么？', (), 'completion')
    evidence = SearchResult('水', 'https://example.org/water', '水的化学式是 H2O。')
    print('chinese_terms:', question_terms(query))
    print('chinese_score:', ExaSearch.score_results(query, [evidence], 1))
    print('json_redaction:', sanitize_error('{"api_key": "synthetic-secret"}', 4096))

asyncio.run(main())
PY
```

### 6.4 修复时应补的回归测试

| 测试位置 | 最少新增覆盖 | 防止的错误修复 |
| --- | --- | --- |
| `tests/test_cache.py`，或建立真实对应的 `test_storage.py` | 写任务取消、初始化并发、初始化失败关闭、WAL-only 超限、真实超限 | 吞取消、错误提交、只通过“删光后达标”验证容量策略。 |
| `tests/test_main.py` | DEBUG 启动 subprocess；不同 CWD 的配置一致性；只清一种缓存时不连接另一种 | mock 掉 Uvicorn 后遗漏真实启动约束；clear-cache 仍依赖无关环境。 |
| `tests/test_search.py` | 中文正例/无关反例、零阈值、第二段 highlight 才有答案 | 仅降低阈值把所有无关内容都放过。 |
| `tests/test_app.py` | 非 ASCII token 的 OCS/HTTP 行为；服务拆分后的完整缓存顺序 | 修鉴权导致协议变化；重构把 fallback 缓存提前。 |
| `tests/test_utils.py` | quoted secrets、多字节截断、completion 空白段；拆模块后移到对应测试 | 用字符长度冒充 byte 上限，或简化校验时接受错误格式。 |
| `tests/test_llm.py` | 移动 codec 后保留所有题型；若改 SDK，补重试/计数/响应大小契约 | SDK 自动重试与项目重试叠加、丢失原始响应大小限制。 |

### 6.5 本次明确未验证

- 未使用真实 API key、没有真实服务商兼容性/限流测试，不给出模型质量或费用结论。
- 未启动浏览器或执行 OCS 用户脚本端到端验证；本次没有 UI/接口行为改动。
- 未做持续高并发、1 GiB 临界数据库、性能基准或崩溃恢复测试；性能段落区分静态成本和实测行为。
- 未做依赖全量 CVE 审计，也没有验证所有 Python/操作系统版本。保留已有 Starlette 最低版本约束不等于保证依赖无漏洞。
- 报告提供修复验收条件，**不代表这些缺陷已经修复**。

## 7. 分阶段整改顺序

### 第一阶段：只修确定错误，不搬文件

1. 修 F01 的事务取消清理；补取消后的数据库状态回归。
2. 修 F02 的 WAL 回收/淘汰顺序；用保留有效行的断言验证，而不只断言磁盘小于预算。
3. 修 F03/F04 的启动与配置来源；用 subprocess 验证，避免仅 mock `uvicorn.run`。
4. 修 F08/F09 的输入与日志边界，使用合成敏感值覆盖。
5. 修 F05/F06/F07 的证据处理；先固定真实正反例，再改算法。搜索查询或评分语义变化更新 `SEARCH_CACHE_VERSION`；prompt/答案生成变化使用 `PROMPT_VERSION`，不要混用。
6. 修 F10 的完整初始化与资源收尾；优先启动时初始化，不让第一条用户请求承担 schema 建立。

### 第二阶段：缩短职责边界，不创建框架

1. `app.py` 提取一个纯应用编排模块 `service.py`，返回 typed 结果；HTTP adapter 不再读数据库私有连接。
2. typed Runtime 明确拥有 clients、stores、配置和生命周期；不要每种字段再配 Factory/Manager。
3. `utils.py` 按第 5.11 节归位，新增 `questions.py`，其余移动到既有 llm/search/cache；别建只有单函数的包。
4. 清理 `_get_answerer` 的假异步、`_is_valid_result` 转发、`incr_many` 死接口、重复 options 解析和无依据兼容字段。

### 第三阶段：有测量才优化数据库

1. 观测实际 requests/事务/maintenance 耗时，评估单数据库 owner 与事务合并的收益。
2. 用真实规模数据检查淘汰、recent-search 查询计划，再加必要索引。
3. 不为了“极致性能”引入 Redis、后台队列、写缓冲/最终一致性或连接池；只有测量证明当前需求需要时再讨论。

每项建议应是可独立回归的小变更；修复与文件移动分开。报告没有创建 commit，以上也不是自动授权修改代码/依赖。

## 8. 外部资料核查与来源

采用 Context7 获取 API/配置资料，再以 Exa 搜索/读取官方页面，辅以锁定依赖的本地接口检查。没有把论坛帖子当作本项目 bug 的证明；源码和本地复现才是主要证据。下面仅记录实际核查并用于结论的来源，不把搜索命中数当成研究深度。

| 标识 | 官方来源与精确位置 | 核查内容 / 在本项目的用途 |
| --- | --- | --- |
| R1 | Python `asyncio` exceptions：`https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError` | Exa 读取确认 CancelledError 自 Python 3.8 起继承 BaseException；解释 F01 为什么绕过 except Exception。不是针对已过时版本的猜测。 |
| R2 | SQLite PRAGMA / WAL：`https://sqlite.org/pragma.html#pragma_wal_checkpoint`；`https://sqlite.org/wal.html` | Exa 检索/读取 checkpoint、WAL 复用及 truncate 行为；支持“先回收 WAL，再考虑淘汰”，不表示 checkpoint 永远不会被 reader 阻塞。 |
| R3 | SQLite VACUUM：`https://sqlite.org/lang_vacuum.html` | Exa 读取重建、临时空间、事务/未完成语句约束；支持减少每批次全库 VACUUM，而非引入另一数据库。 |
| R4 | aiosqlite 官方说明：`https://aiosqlite.omnilib.dev/en/stable/`；API：`https://aiosqlite.omnilib.dev/en/stable/api.html` | Exa 读取每连接一个线程/请求队列、显式 commit/rollback/close；逐语句串行不能替代应用事务生命周期。 |
| R5 | Uvicorn deployment：`https://github.com/kludex/uvicorn/blob/main/docs/deployment/index.md` | Context7 CLI resolve 到 `/kludex/uvicorn` 后查文档，确认 app 实例与 reload 不兼容；本机 0.47.0 实测退出。 |
| R6 | python-dotenv find_dotenv：`https://github.com/theskumar/python-dotenv/blob/main/_autodocs/api-reference/find_dotenv.md` | Context7 `/theskumar/python-dotenv` 查询默认调用目录与 usecwd；支持 F04 的显式路径方案。 |
| R7 | Exa Python README / 实现：`https://github.com/exa-labs/exa-py/blob/master/README.md`；`https://github.com/exa-labs/exa-py/blob/master/exa_py/api.py` | Context7 `/exa-labs/exa-py` 查询 AsyncExa/highlights/client 生命周期；本机 2.14.0 检查 Result 签名与 close API，未创建联网 client。 |
| R8 | Python hmac compare_digest：`https://docs.python.org/3/library/hmac.html#hmac.compare_digest` | Exa 检索官方文档确认 str 仅支持 ASCII；F08 由本地 ASGI 输入直接验证。 |
| R9 | OpenAI Python SDK：`https://github.com/openai/openai-python/blob/main/README.md` | Context7 `/openai/openai-python` 查询 max_retries=0、with_streaming_response、异步原始流读取。只用于比较实现取舍，没有调用 API 或推荐迁移模型。 |

滚动官方文档不等于锁定版本保证：涉及实际 API 的关键点用本地 2.14.0 Exa/0.47.0 Uvicorn 与测试再次核对。对 SDK 替换仅给出评估方向，没有把新文档片段当成已验证的迁移补丁。

## 9. 交付核对

落盘校验：用 Python AST 对照第 5 节，**11/11 文件、117/117 函数/方法名称及定义起始行全部匹配**，类数量为 17；9 个资料标识均有定义。直接提取并执行第 6.3 节代码，事务、WAL、中文相关性和脱敏输出与记录一致。再次运行全部测试、Ruff 和编译检查通过；Git 确认 `src/`、`tests/`、`pyproject.toml`、`uv.lock` 未改动，唯一新增仓库文件为本报告。

| 用户要求 | 报告对应证据 |
| --- | --- |
| 严格审阅 src 全量代码 | 11 个文件的模块级判断、117 个函数/方法逐一列出，覆盖行号与类/常量说明。 |
| 架构正确、清晰、优雅 | 依赖/数据流、4 个边界问题、10 个确定缺陷；保留正确设计，拒绝无理由大重构。 |
| 文件是否应拆分、合并、建包 | 第 2.2 节逐文件裁决；仅建议两个新的概念模块，明确哪些不拆、不合并。 |
| 冗余写法与不必要依赖 | 第 4 节、逐函数表中的删除/简化建议、完整直接依赖与 SDK 取舍。 |
| Context7 与 Exa | 第 8 节记录查询和官方出处，关键点结合本地锁定 API/复现。 |
| 根目录详细报告 | 本文件 `SRC_ARCHITECTURE_REVIEW.md`；业务代码未改，整改与验收明确分开。 |
| 边审阅边更新 | 第 10 节记录第二轮 11 个模块分别读取后立即更新的过程，含三个新增隔离实测；保留第一轮未采用此工作方式的说明。 |

**最终判断：基础方向正确，离“清晰且优雅”还有一轮有针对性的整理。先修事务/容量/入口这些真实错误，再收敛 app、utils 和数据库 owner；不要从重新设计目录树开始。**

## 10. 增量复核记录

过程说明：第一轮是读取全量源码后分段写入报告，**没有做到边读边更新**。用户补充过程要求后，保留已有结论，开展第二轮逐模块复核；每个模块复核后立即更新其章节和此记录。以下是实际后续操作，不倒填第一轮过程，也不把仅改措辞冒充新增实测。

| 顺序 | 模块 | 本轮直接核查重点 | 对报告的即时更新 |
| --- | --- | --- | --- |
| 01 | `storage.py` | 完整 13 个方法、锁/ready/事务/维护顺序 | 第 5.10 节新增提交后维护异常的语义边界；保留已复现 F01/F02/F10，并标明补充项仅为静态结论。 |
| 02 | `cache.py` | UPSERT 时间戳与 payload 解码边界 | 第 5.3 节明确按写入时间而非 LRU 淘汰、逐条丢弃坏记录的现有语义。 |
| 03 | `metrics.py` | 日志裁剪、计数读取及全局可淘汰表关系 | 第 5.7 节明确日志双重约束、计数不随缓存清理重置，防止数据库重构误删指标。 |
| 04 | `config.py` | Settings class body 与重新实例化 | 第 5.4 节新增隔离实测：环境 PORT 改变但新实例仍用旧默认值，细化显式 loader 的必要性。 |
| 05 | `main.py` | clear-cache → _load_settings(validate=False) 实际路径 | 第 5.6 节新增隔离实测：错误 LLM 超时配置使清缓存提前退出，临时数据库未创建。 |
| 06 | `__init__.py` | 符号概览及单行模块内容 | 第 5.1 节确认无初始化副作用，明确配置修复不应落在包入口。 |
| 07 | `models.py` | 证据校验及两个生产引用 | 第 5.8 节补充保留共享纯校验的依据，区分合理复用与无意义转发。 |
| 08 | `utils.py` | completion 归一化与缓存验证的一致性 | 第 5.11 节新增实测：空白中间答案段被缓存验证接受、被正常生成拒绝；明确受影响边界。 |
| 09 | `search.py` | SDK 查询到批次评分的控制流 | 第 5.9 节明确批次平均分不是逐条过滤/排序，收紧“可用证据”的措辞。 |
| 10 | `llm.py` | answer 重试、HTTP adapter 与 attempt hook 异常边界 | 第 5.5 节补清两层解析、取消和本地指标异常不属于上游瞬态重试，限制不当简化。 |
| 11 | `app.py` | 完整路由分支与答题异常映射范围 | 第 5.2 节明确缓存/指标失败不能混入上游 fallback，准确限定 RuntimeError → MissingKey 的问题。 |

当前状态：第二轮复核与最终验证均已完成。11 个模块分别读取后即时更新，三个新增隔离实测结果已记录；第二轮按高风险符号和调用边界针对性复核，不声称又逐行重读了全部 2204 行。最终 117 个函数名称/定义行号、11 条增量记录及对应章节均核对通过；报告内复现代码可执行，121 项测试、Ruff、编译检查再次通过，只有上述一个依赖弃用警告。仓库仍仅新增本报告，没有修改业务代码。

## 11. 重构结果与架构裁决

本节与第 12 节更新到补充修复提交 `0b1fb39`，记录日期为 2026-09-20。首次重构及上游验收历史保留在第 13–14 节，不能替代第 15 节的新回归。用户已授权调整依赖、真实上游测试及无签名原子提交，并明确要求复用现有凭据。

### 11.1 实际落地，不是目录搬家

```text
main → config / app factory / 独立 CLI 存储生命周期
app → questions → OCSQuery
app → service → cache / search / llm / metrics
service.Runtime → 一个 Database → 三个 store 组合共享
cache / metrics → storage → aiosqlite
llm / search → questions / models / utils
```

仅增加 `service.py` 和 `questions.py` 两个运行时模块，仍为扁平包。没有 ORM、DI 容器、provider 注册框架、嵌套 repository 层、全局连接注册表或迁移期兼容壳。没有新增依赖或迁移 OpenAI SDK；为使用公开的 `set_authorizer`，将 aiosqlite 最低版本从 0.20.0 提高到 0.22.0，锁文件仍使用原来的 0.22.1，没有升级其他包。

- `app.py` 从 482 行减到 174 行，只负责协议边界、生命周期和状态汇总；SQL 全部归 store。
- `utils.py` 从 296 行减到 39 行，只保留通用文本归一化、脱敏和 UTF-8 截断。
- `service.py` 的 224 行负责实际业务顺序，传递 `OCSQuery`、`Evidence`、`AnswerResult`；不依赖 FastAPI。
- `storage.py` 为 269 行，业务写入和容量淘汰共享取消安全的 `_transaction`；没有新增存储框架，三个 store 仍组合使用同一个 owner。
- `llm.py` 的 349 行包含原来分散在 utils 的答案 codec 和证据渲染；属于同一生成协议，不值得再拆成若干小包。
- `search.py` 的 229 行包含固定词面规则和停用词。它不是语义模型；保留 `_semantic_score` 这个历史私有名字，但明确其实现只是词面覆盖率，避免把改名当作功能升级。
- 总行数从 2204 到 2310，不以“少了多少行”代替架构判断：关键收益是边界明确、重复逻辑减少、缺陷有回归和数据只有一个 owner。

**不继续拆分：** 两种缓存虽然 SQL 形状相近，但记录字段、解码与键语义不同，不引入通用 CRUD 基类；两个配置 dataclass 是存储命令与运行时所需输入的真实边界；models 保留共享叶子类型，utils 不吞回业务函数。没有查询计划或实测显示需要新索引，因此不增加索引或合并指标事务。

### 11.2 历史发现关闭情况

| 发现 | 当前状态与实现 | 直接回归证据 |
| --- | --- | --- |
| F01 取消事务残留 | 事务体取消回滚；补充修复拒绝已取消但尚未授权的排队 COMMIT，重复取消时持锁等待回滚。已执行提交不可撤销。 | `test_cancelled_write_rolls_back`、`test_cancelled_queued_commit_rolls_back`、`test_cancellation_after_commit_preserves_committed_write` |
| F02 WAL 导致误淘汰 | 已修复；checkpoint 后复测，按活跃页估算批量淘汰后集中 VACUUM。 | `test_capacity_reclaims_wal_before_evicting`、`test_capacity_vacuums_once_for_multiple_prune_batches` |
| F03 DEBUG 无法启动 | 已修复；Uvicorn 使用 import-string factory。 | `test_debug_server_starts_in_subprocess` |
| F04 配置路径不一致 | 已修复；入口统一读取 CWD 的 `.env`，环境变量优先，导入不加载。 | `test_load_uses_cwd_and_environment_priority`、`test_settings_import_does_not_read_environment` |
| F05 中文整句误杀 | 已修复该确定缺陷；中文二元组与英文词/数字 token 替代整句匹配。 | `test_relevance_corpus` 中文、英文、公式正反例 |
| F06 零阈值接受零相关 | 已修复；先逐条排除零相关，再应用整批质量阈值。 | `test_zero_quality_threshold_does_not_allow_zero_relevance` |
| F07 丢弃后续 highlights | 已修复；按顺序合并所有有效字符串片段。 | `test_all_highlights_are_used_and_irrelevant_items_are_removed` |
| F08 非 ASCII token 500 | 已修复；鉴权比较 bytes，无效值走既定 OCS 失败合同。 | `test_non_ascii_access_token_is_rejected` |
| F09 脱敏漏掉 quoted 字段 | 已修复已知形式；删除 `_safe_error` 原文回退。 | `test_quoted_and_unquoted_credentials_are_redacted`、`test_uuid_credentials_are_redacted` |
| F10 半初始化连接公开 | 已修复；同锁完成初始化后才赋 `_conn`，失败关闭未公开连接。 | `test_connection_is_not_published_until_initialized`、`test_configuration_failure_closes_connection`、`test_cancelled_initialization_closes_unpublished_connection` |
| 配置默认值在 import 时冻结 | 已修复；dataclass 纯数据，loader 每次显式取值。 | `test_loading_is_explicit_and_repeatable` |
| clear-cache 依赖 LLM 配置 | 已修复；仅加载 `StorageSettings`。 | `test_storage_settings_ignore_invalid_llm_configuration` 及 CLI 清理回归 |
| 填空缓存与生成校验不一致 | 已修复；答案每一段均拒绝纯空白。 | `test_completion_cache_and_payload_share_blank_segment_rules` |
| 所有 RuntimeError 误记 MissingKey | 已修复；专门的 `MissingKeyError`，内部错误保留真实类型。 | `test_internal_runtime_error_is_not_missing_key` |
| 提交后维护失败语义不清 | 已明确；普通维护异常包装为 `PostCommitError`，不谎称事务回滚。 | `test_post_commit_failure_does_not_claim_rollback` |

### 11.3 保持的合同与刻意不做的事

1. 搜索答案缓存优先；fallback 答案缓存必须等搜索不可用后再查。恢复后的 Exa 不被旧 fallback 压制。
2. 标题、完整选项和题型参与缓存隔离。算法修订 `SEARCH_REVISION=v4` 同时参与两种缓存键，旧 `.env` 仍配置 v3 也不能命中旧算法数据。原数据库不删；记录自然按容量淘汰。
3. `.env.example` 使用 v4，实际 `.env` 不改。prompt 没有改写，`PROMPT_VERSION` 不变。多选字符串 `A # B` 仍拒绝，数组项则按原规则去空白，避免“纯清理”偷偷放宽生成协议。
4. `/search` 的鉴权或业务失败仍 HTTP 200、`code=0`；`/stats` 鉴权失败 401。答案缓存命中不带 provider header，实时作答保留 `exa`、`cache`、`llm-fallback`。
5. `MAX_LLM_RETRIES` 仍是首次之后的重试次数；429、5xx、传输错误可重试，确定性协议/大小错误不重试；计数按实际 HTTP attempt，不按逻辑问题数。
6. 不引入 TTL，不改成 LRU；清缓存不重置累计指标或删除错误日志。三个 store 的清理作用域不互相侵入。
7. 外部 API、用户输入、环境与文件系统保留必要校验；内部未连接错误 fail-fast，不写静默重建/回退层。

## 12. 重构后逐函数职责清单

以下覆盖 `0b1fb39` 全部 **126/126** 个函数/方法，包括两个嵌套回调。每个模块标题给出当前路径，表中行号为 **`def` / `async def` 定义行**，不是装饰器起始行；不同于第 5 节的历史源码快照。每行说明保留职责及本轮裁决，不重复贴源代码。

### 12.1 `src/llm_ocs_bridge/__init__.py`

1 行、0 函数、0 类；只保留包说明，不加载配置、不启动客户端、不重导出兼容入口。

### 12.2 `src/llm_ocs_bridge/app.py`

174 行、11 函数、1 个 RequestError 边界异常类。唯一文件级运行对象是无配置副作用的 router，不存在模块级应用 Runtime。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `lifespan` | 21 | 构造 Runtime，连接数据库，finally 统一关闭；生命周期入口保留。 |
| `create_app` | 34 | 显式接受/加载 Settings，组装 FastAPI 和 router；重复校验收敛到 runtime 构造边界。 |
| `_runtime` | 42 | 从 request state 取得有类型 Runtime；保留短小边界访问器。 |
| `_auth_ok` | 46 | 验证 header token，编码后恒时比较；不读取 URL token。 |
| `_ocs_response` | 54 | 唯一 OCS 成功/失败响应构造点与 provider header 映射。 |
| `_read_body` | 64 | 限制请求大小，读取 GET 参数或 POST JSON；不解析 form，只做 HTTP 边界工作。 |
| `_providers` | 105 | 描述配置启用的 provider；不声称探测上游健康。 |
| `health_root` | 111 | 根路径探活，保留轻量 HTTP 端点。 |
| `health` | 116 | 返回进程健康信息，不调用上游。 |
| `stats` | 128 | 鉴权后组合指标/store 查询结果；SQL 已迁出。 |
| `search` | 163 | 鉴权、parse_query、调用 service、映射响应；不再编排缓存。 |

### 12.3 `src/llm_ocs_bridge/service.py`

224 行、9 函数、3 个 dataclass：`Runtime` 是资源 owner；`Evidence` 是证据与不可用原因；`AnswerResult` 是与 HTTP 无关的业务结果。不再使用 SimpleNamespace 和四元组。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `Runtime.aclose` | 35 | AsyncExitStack 关闭 LLM、搜索客户端与数据库，单项关闭失败不跳过其余资源。 |
| `build_runtime` | 57 | 校验运行设置并构造一个数据库与三个 store；不隐式连接。 |
| `_get_answerer` | 71 | 首次生成时创建 LLM 客户端；删除无 await 的假异步。 |
| `_cached_answer` | 77 | 查答案缓存、校验、删坏值、更新命中/未命中指标。 |
| `_answer_with_llm` | 101 | 传完整 OCSQuery 与证据列表，处理生成路径的指标钩子；证据渲染归 `LLMAnswerer.answer`。 |
| `_answer_with_llm.record_attempt` | 110 | 每个真实上游尝试增加 `llm_calls`，不是每道题增加一次。 |
| `_resolve_evidence` | 120 | 搜索缓存→Exa→缓存写入；返回类型化证据/失败原因。 |
| `_generate_answer` | 175 | 把生成异常映射为业务结果，成功后写答案缓存；不吞缓存写失败。 |
| `answer` | 208 | 唯一答题主流程，固定搜索缓存与 fallback 缓存的先后顺序。 |

### 12.4 `src/llm_ocs_bridge/questions.py`

121 行、8 函数；`QuestionError` 标识可映射为输入错误的边界异常。该模块不依赖存储、HTTP 或上游客户端。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `normalize_query` | 19 | 归一化标题、选项、题型并形成 OCSQuery；不在多个业务层重复拆装。 |
| `format_options_for_prompt` | 31 | 统一标签展示，供查询与 prompt 使用；不是通用文本函数。 |
| `_split_options` | 41 | 处理既定字符串/数组选项协议；不增加新输入形式。 |
| `allowed_option_labels` | 57 | 返回合法标签集合，减少重复成员判断与转换。 |
| `strip_option_label` | 66 | 删除展示标签得到选项正文；题目领域私有规则。 |
| `_fields` | 74 | 从外部字段校验并提取三项题目输入；删去无意义局部 rebind。 |
| `validate_query` | 101 | 统一标题/选项数量/长度及题型限制。 |
| `parse_query` | 116 | 组合字段提取、归一化、合法性校验；HTTP 外也可独立测试。 |

### 12.5 `src/llm_ocs_bridge/llm.py`

349 行、11 函数、7 类；`LLMAnswerer`、`LLMError` 基类及 MissingKey/InvalidAnswer/ResponseTooLarge/ProviderProtocol/TransientLLM 五类具体异常构成生成边界。维持 HTTPX 的字节限制和重试合同，不为使用 SDK 而丢失控制点。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `LLMAnswerer.__init__` | 45 | 接受 Settings、保存请求配置，客户端惰性创建。 |
| `LLMAnswerer.answer` | 54 | 接收 OCSQuery，建立 prompt，按明确错误类别重试、解析并验证答案。 |
| `LLMAnswerer.aclose` | 92 | 关闭自有 HTTPX client，不干预外部资源。 |
| `LLMAnswerer._call_model` | 97 | HTTP 流式读取、声明/实际字节限额、状态分类及 provider JSON 结构校验。 |
| `LLMAnswerer._parse_payload` | 167 | 解析生成文本的 JSON/code fence；与 provider envelope 解析分离。 |
| `LLMAnswerer._build_messages` | 175 | system 放生成约束，user 放题目/证据 JSON；纯渲染函数保留原签名。 |
| `format_search_context` | 229 | 证据按顺序渲染并按 UTF-8 字节截断；合并两套循环、避免反复全串 encode。 |
| `normalize_answer_payload` | 254 | 按题型执行生成输出 codec；填空统一 join，多选仍遵守旧输入差异。 |
| `validate_answer` | 297 | 验证最终答案与真实选项标签；同一入口用于生成与缓存。 |
| `_pick_field` | 327 | 从解析结果读取题型所需字段；短私有 helper，不增加包装层。 |
| `_normalize_option_list` | 336 | 分别归一化字符串/数组，统一标签校验、去重判错、排序；保留语义而非盲目合并分支。 |

### 12.6 `src/llm_ocs_bridge/search.py`

229 行、12 函数、1 类。先做每条相关性过滤，再做整批质量评分；不能让一条好结果替无关长文背书。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `ExaSearch.__init__` | 18 | 保存配置及可选异步客户端，类型使用 SDK 真实类型。 |
| `ExaSearch.enabled` | 24 | 只表示凭据是否配置，不执行网络探测。 |
| `ExaSearch._get_client` | 27 | 缺凭据明确报错，否则惰性创建 AsyncExa。 |
| `ExaSearch.aclose` | 35 | 关闭 SDK 内 HTTPX client；已核对锁定 exa-py 2.14.0 无公开 aclose。 |
| `ExaSearch.search` | 40 | 渲染完整问题、限定网络超时、映射结果、逐条硬过滤及批次门槛。 |
| `ExaSearch._map_results` | 77 | 保留全部有效 highlights，使用 SDK 的 published_date，不保留猜测字段名兼容壳。 |
| `ExaSearch.score_results` | 97 | 对已过滤的 `(result, relevance)` 计算批次质量；不重新分词。 |
| `ExaSearch._semantic_score` | 125 | 计算预提取题目词项覆盖率，命名不应被误解为 embedding 语义理解。 |
| `ExaSearch._source_score` | 131 | 依据 hostname 边界评分，避免伪后缀域名冒充可信域名。 |
| `render_search_query` | 204 | 保留完整标题/选项/题型的检索文本，不把缓存与检索语义简化为标题。 |
| `question_terms` | 215 | 每个问题仅计算一次词项集合；丢弃纯选项标签。 |
| `_extract_terms` | 222 | 中文二元组、英文完整词、公式/数字和轻量停用词；无新 NLP 依赖。 |

### 12.7 `src/llm_ocs_bridge/cache.py`

278 行、19 函数、2 类。两个 store 组合使用 Database；序列化和 key 与缓存语义同处一模块，删除原 utils 兼容入口。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `SearchCacheStore.__init__` | 15 | 接受共享 Database，不负责连接或初始化。 |
| `SearchCacheStore.get` | 18 | 在共享读锁下读取搜索 payload。 |
| `SearchCacheStore.set` | 28 | 保存完整题目、provider、质量及版本元数据；事务归 Database。 |
| `SearchCacheStore.clear` | 74 | 仅清搜索缓存，保留其他表。 |
| `SearchCacheStore.delete` | 78 | 精确删除坏缓存键。 |
| `SearchCacheStore.recent` | 84 | 最近搜索 SQL 与记录转换；从 HTTP 层迁回数据所有者。 |
| `AnswerCacheStore.__init__` | 110 | 接受同一 Database，无独立生命周期。 |
| `AnswerCacheStore.get` | 113 | 读取答案，业务有效性由 service+codec 判断。 |
| `AnswerCacheStore.set` | 123 | 保存答案、题目、模型、prompt 版本及缓存模式。 |
| `AnswerCacheStore.clear` | 169 | 仅清答案缓存。 |
| `AnswerCacheStore.delete` | 173 | 精确移除不合法答案记录。 |
| `encode_search_results` | 180 | 将共享 SearchResult 转为持久化格式，不保存 SDK 对象。 |
| `decode_search_results` | 194 | 校验文件边界的 JSON 记录并过滤坏条目；坏数据不流入 prompt。 |
| `serialize_options` | 227 | 以 `#` 拼接选项，仅作存储元数据；缓存隔离使用 `question_cache_parts` 中的结构化数组。 |
| `question_cache_parts` | 231 | 一处定义完整问题的 key 输入。 |
| `cache_key` | 235 | 规范 JSON 后 SHA-256，稳定且避免分隔符碰撞。 |
| `_canonicalize` | 241 | 对受控 key 输入归一化；返回 object，移除不必要 Any。 |
| `answer_key` | 254 | 隔离模型/模式/生成与搜索修订，防旧算法答案复用。 |
| `search_key` | 270 | 隔离问题、查询配置和内置/用户搜索修订。 |

### 12.8 `src/llm_ocs_bridge/storage.py`

269 行、18 函数、2 类（Database、PostCommitError）；schema 仍是原四张表。锁属于数据库 owner，不属于文件路径全局注册表。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `Database.__init__` | 65 | 保存 StorageSettings、未连接状态与实例锁。 |
| `Database.connect` | 71 | 锁内建立连接、PRAGMA、完整 schema，成功后公开 ready 状态；失败关闭。 |
| `Database.close` | 92 | 同锁关闭单连接并清空状态，不由 store 各自关闭。 |
| `Database.read` | 103 | 未连接 fail-fast；持锁读取，不能看到另一任务未提交的数据。 |
| `Database.write` | 109 | 持锁执行容量预检、业务事务和提交后维护；普通维护失败归为 `PostCommitError`。 |
| `Database._transaction` | 123 | 统一业务/淘汰事务的 BEGIN、commit、回滚和 authorizer 生命周期；区分提交后的清理错误。 |
| `Database._transaction.authorize` | 127 | 仅在已处理取消后拒绝尚未授权的 COMMIT，允许 ROLLBACK；使用线程安全 Event，不访问 aiosqlite 私有队列。 |
| `Database._rollback` | 156 | 等待受 shield 保护的回滚完成，重复取消不释放事务锁；清理后继续传播取消。 |
| `Database.database_size_bytes` | 168 | 主库/WAL/SHM 实际大小总计。 |
| `Database._enforce_capacity_locked` | 173 | 先回收 WAL，再用统一事务批次淘汰，最后集中 VACUUM；指标计数不淘汰。 |
| `Database._checkpoint` | 197 | 执行 WAL checkpoint；不添加隐藏重试。 |
| `Database._live_size_bytes` | 203 | 根据 SQLite 页状态估算有效占用，避免每批删行都 VACUUM。 |
| `Database._oldest_rows` | 212 | 以 `rowid AS row_id` 统一普通表与 INTEGER PRIMARY KEY 表的列名，按写入时间选取；完整初始化后不再探测缺表。 |
| `Database._group_rows` | 235 | 按内部表白名单分组待删 rowid；不接受外部表名。 |
| `Database._database_files` | 241 | 一处定义 SQLite 主库和附属文件路径。 |
| `Database._prepare_database_file` | 245 | 创建数据库文件并收敛权限，不创建父目录；父目录必须已存在。 |
| `Database._secure_file_permissions` | 258 | 已存在数据库相关文件收敛到 0600。 |
| `Database._init_db` | 268 | 一次性创建全部所需表，幂等兼容已有数据库。 |

### 12.9 `src/llm_ocs_bridge/metrics.py`

111 行、5 函数、1 类。删除无调用的 `incr_many`，单次增加逻辑直接留在 `incr`，不保留只剩一次调用的 `_incr`。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `MetricsStore.__init__` | 24 | 接收共享 Database 与日志限制配置。 |
| `MetricsStore.incr` | 28 | 原子增加指定计数；不引入没有性能证据的批量接口。 |
| `MetricsStore.log_error` | 41 | 脱敏、截断后持久化，并实施错误条数限制。 |
| `MetricsStore.get_counters` | 68 | 读取已存计数并返回完整计数集合。 |
| `MetricsStore.get_recent_errors` | 78 | 查询有限的近期错误，兼顾读取历史记录时的脱敏边界。 |

### 12.10 `src/llm_ocs_bridge/config.py`

215 行、11 函数、2 个 frozen/slots dataclass。StorageSettings 与 Settings 仅保存值，密钥字段不进入 repr。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `_bool` | 14 | 环境布尔值严格解析。 |
| `_text` | 26 | 处理空白与必填文本，集中边界错误。 |
| `_int` | 37 | 整数与上下界校验，删除从未使用的 default 参数。 |
| `_optional_int` | 58 | 仅处理“未设置”这一真实差异，其余复用 `_int`。 |
| `_float` | 67 | 浮点、有限性与边界校验。 |
| `_environment` | 133 | 显式 mapping 或 CWD dotenv+环境合并，不修改 os.environ。 |
| `load_storage_settings` | 141 | 仅解析数据库所需四项设置，不触碰 LLM 必填项。 |
| `load_settings` | 153 | 一次显式组装运行配置，无模块初始化副作用。 |
| `_validate_secret` | 198 | 对运行时必需凭据给出明确配置错误，不输出值。 |
| `validate_runtime_settings` | 205 | 启动所需 access/LLM 凭据校验边界。 |
| `validate_check_settings` | 212 | 在运行校验上要求禁用 fallback 时必须配置 Exa，匹配 CLI 自检职责。 |

### 12.11 `src/llm_ocs_bridge/main.py`

301 行、18 函数、2 类（CheckError、ArgumentParser 子类）。保留 CLI 文案与实际诊断流程，不为行数拆出 commands 包。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `_ArgumentParser.format_help` | 26 | 定制既定帮助输出。 |
| `_ArgumentParser.format_usage` | 29 | 保持 usage 文案一致。 |
| `_clear_cache` | 33 | 使用独立存储配置/Database，按 scope 清理后关闭。 |
| `_help_options` | 48 | 统一帮助选项定义，避免 CLI 参数重复。 |
| `_build_parser` | 54 | 声明命令与参数，不在帮助路径加载运行配置。 |
| `_status` | 100 | 统一自检状态显示。 |
| `_print_ocs_instructions` | 104 | 输出本地 OCS 导入说明；保留有意输出 token 的用户命令合同。 |
| `_check_env_file` | 116 | 检查当前工作目录配置文件。 |
| `_load_settings` | 131 | 显式加载并验证完整配置；删去无调用的 validate 参数。 |
| `_check_cache_path` | 151 | 文件系统写入权限检查，退出清理测试资源。 |
| `_check_llm` | 164 | 实际 LLM 自检，finally 关闭自有客户端。 |
| `_check_exa` | 174 | 实际搜索自检，finally 关闭客户端。 |
| `_client_host` | 185 | 把监听地址转为本地可连接地址，包含 IPv6。 |
| `_ocs_config` | 195 | 构造既定 OCS 配置，不在日志路径隐式输出。 |
| `_safe_error` | 231 | 始终脱敏及裁剪，删除原文备用分支。 |
| `_check` | 237 | 串联环境/文件/上游诊断，报告状态并决定退出码。 |
| `_exit_with_config_error` | 270 | 统一配置失败文案与非零退出。 |
| `run` | 274 | 先解析 CLI，再按命令加载设置；服务使用 Uvicorn factory 支持 reload。 |

### 12.12 `src/llm_ocs_bridge/models.py`

33 行、1 函数、2 个数据类（OCSQuery、SearchResult）。叶子模块不需要合并到 service 或绑定 FastAPI。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `is_valid_search_result` | 23 | 搜索和缓存解码共同使用的纯证据有效性判定，保留真实复用。 |

### 12.13 `src/llm_ocs_bridge/utils.py`

39 行、3 函数、0 类。已经没有题目语义、缓存 key、答案协议和搜索评分。

| 函数 | 行 | 当前职责与裁决 |
| --- | --- | --- |
| `normalize_text` | 24 | 折叠 Unicode 空白并去首尾空白；不执行 NFKC 字符归一化。 |
| `sanitize_error` | 30 | 对已知 credential 形式脱敏，再按字节限额裁剪；不是通用 DLP 保证。 |
| `truncate_utf8` | 38 | 编码一次、切 bytes、忽略末尾不完整字符，替代反复编码二分搜索。 |

## 13. 重构验收记录

本节是首次验收历史，不包含第 15 节的后续缺陷修复；这里的测试总数、代码提交和逐函数校验只对应当时快照。

### 13.1 自动回归与静态检查

在代码提交 `bdfdd9a` 对应的工作树执行：

```bash
uv run --frozen pytest
uv run --frozen ruff check src tests pyproject.toml
uv run --frozen python -m py_compile src/llm_ocs_bridge/*.py tests/*.py
git diff --check
```

结果：**174 passed，3.10 秒；Ruff 全通过；编译检查通过；diff 无空白错误。** 基线 121 项，新增/参数化回归净增 53 项。只有 Starlette TestClient 对 HTTPX 的既有弃用警告；没有为了消除警告引入新传输依赖。未配置独立静态类型检查器，因此不把 py_compile 称为类型检查。

最后清理额外运行 `tests/test_utils.py` 与 `tests/test_llm.py`：45 项通过。补充多选字符串/数组差异、重复标签、混合类型、UTF-8 每个字符边界及多证据分隔符字节限制，确保删重复代码不改变合同。

文档落盘后另用 AST 校验第 12 节：13/13 文件、123/123 函数名称及定义行全部一致；检查包内相对导入图无循环、HTTP 模块不存在 SQL 字符串。该检查只证明这些具体边界，不把简单静态扫描当作全面形式验证。

curl 成功后的最终复跑：**174 passed，3.50 秒；Ruff、编译检查及 diff 检查通过**，仍只有上述同一个依赖弃用警告。运行时代码未改变，不通过修改重试策略或测试期专用生产逻辑获取成功。

主要验证覆盖：

- 事务取消、初始化失败/取消、半成品连接不可见、共享连接读写互斥。
- 单 owner 并发 connect、已初始化数据库的跨 owner 并发写、旧表/数据/累计指标保留。
- WAL 回收不删有效记录、实际超限淘汰、多个 prune 批次只 VACUUM 一次、提交后维护失败。
- CWD 配置选择、环境优先级、重复加载、导入无副作用、非法 LLM 设置不阻塞清缓存。
- DEBUG 真实子进程启动、loopback HTTP；不是只断言 uvicorn.run 参数。
- 非 ASCII token、quoted/Bearer/UUID 脱敏、填空逐段校验、内部错误不误报缺密钥。
- service 脱离 FastAPI 的临时库答题流程、搜索恢复优先于 fallback 缓存、关闭失败仍释放其他资源。
- 完整问题缓存隔离、v4 失效、全部 highlights、零阈值硬门槛及中文/英文/公式语料。

### 13.2 本地 TCP 闭环：通过

单独运行临时脚本 `/tmp/ocs_local_tcp_e2e.py`：一个真实 Uvicorn 进程事件循环中的两个本地服务，通过 HTTPX TCP 请求联通桥接服务与一个确定性 OpenAI-compatible HTTP fixture。它不是 TestClient，也不是直接替换 LLMAnswerer 方法；但 fixture **不是实际 LLM**。

实际校验 `/health`、未认证 `/search`、认证作答、重复题答案缓存、`/stats`、provider header 与 HTTP 上游调用次数。摘要：

```json
{
  "status": "passed",
  "upstream": "local TCP fixture, not real LLM",
  "upstream_calls": 1,
  "env_unchanged": true,
  "live_answer": "A",
  "cache_hit": true,
  "llm_calls": 1,
  "llm_fallbacks": 1,
  "answer_cache_hits": 1,
  "error_count": 0
}
```

全部数据库在 TemporaryDirectory，凭据为合成值，测试结束关闭服务并清理临时库。内存哈希核对 `.env` 未变，不输出密钥或哈希。缓存 miss 计数按实际查找记录，因此两次请求合计答案 miss=3、搜索 miss=2，不以请求数错误要求每个计数都等于 1。

### 13.3 真实上游：历史阻塞与成功复验

**以下首轮结果是历史记录；当前状态以本节末尾的成功复验为准。**

临时脚本 `/tmp/ocs_live_e2e.py` 复用当前配置的凭据、模型和 endpoint，仍覆盖为独立临时数据库。题目为“水的化学式”，选项 H2O/CO2/O2/N2。为控制调用，搜索条数限定为 3，首轮无 LLM 重试，第二轮仅增加一次重试。

- 首轮桥接服务到达真实 Exa，取得可用证据并进入生成；LLM HTTP 503，`/search` 返回业务失败。
- 第二轮有界重试仍失败，没有无限重试，没有换密钥或切模型。
- 随后一个独立最小请求，仅要求回复 OK，仍返回 HTTP 503。经替换全部已知 secret 后读取错误体：`code=model_not_found`、`type=new_api_error`，内容明确说明当前 `deepseek-flash` 模型的分组没有可用渠道，要求检查 API Key 分组、权限和模型能力配置。
- 后续只读复核：使用同一凭据请求该 endpoint 的 `/models`，HTTP 200，响应 `data` 确认为空数组，未列出当前配置模型。目录本身不能证明推理权限，但与上述实际生成请求的渠道错误一致；这次复核没有再调用付费推理。
- 当前 endpoint 是第三方 OpenAI-compatible 服务。该错误说明已到达上游，**不是本地网络未出站，也不是凭据缺失；不能据此断言 OpenAI 官方服务故障或账号余额不足**。
- 由于第一个场景即在真实 LLM 失败，真实 fallback 作答及随后答案缓存命中场景**尚未完成**；本地 TCP fixture 的成功不能替代它。
- 未启动真实 OCS 用户脚本或浏览器课程页面，因此没有宣称 OCS UI 联调通过。

上述阻塞期间没有添加生产 fallback、修改 `.env` 或提高 retry 次数掩盖路由问题。

**2026-09-20 后续复验：** 用户要求使用 curl。实际调用系统 curl，凭据通过标准输入配置传入，不进入进程参数或临时文件；对原 endpoint 的 `/chat/completions` 发送最小请求。结果 HTTP 200、curl exit 0、回答 `OK`。请求模型仍是 `deepseek-flash`，上游响应的 model 字段为 `deepseek-v4.1-flash`；这是响应观测，不是客户端自行切换模型。无法仅据先后结果归因于 curl，也没有证据说明此前错误是 HTTPX 引起。

随后复建已清理的临时 E2E 脚本，通过真实 Uvicorn 和 HTTPX 使用项目原实现完成两个场景。沿用原凭据、endpoint、请求模型；仅隔离数据库并覆盖测试超时、搜索条数及场景开关，最多允许一次重试，实际两场景都仅调用 LLM 一次：

| 场景 | 实时答案 | provider header | 重复请求 | Exa 次数 | LLM 次数 | 错误记录 |
| --- | --- | --- | --- | --- | --- | --- |
| 真实 Exa＋LLM（水的化学式） | HTTP 200、code=1、A | `exa` | 相同答案、缓存命中、无 provider header | 1 次成功 | 1，缓存请求不增加 | 0 |
| 真实 LLM fallback（一加一） | HTTP 200、code=1、A | `llm-fallback` | 相同答案、缓存命中、无 provider header | 0 | 1，缓存请求不增加 | 0 |

同时验证 `/health`、未认证 `/stats` 的 401、未认证 `/search` 的 HTTP 200/code=0，以及两个场景各自的 `answer_cache_hits=1` 和 fallback 计数。汇总结果 `status=passed`、`env_unchanged=true`。数据库均位于 TemporaryDirectory，退出时释放客户端/连接并清理临时库，不触及真实缓存。

因此此前渠道阻塞已经解除，且不只是 curl 探针成功，项目 HTTPX 请求链路也成功。真实 OCS 合同在本地 HTTP 边界验收，浏览器用户脚本界面仍未单独验证。

### 13.4 资料核查与安全约束

本轮实现核查使用 Context7 的 `/omnilib/aiosqlite`（worker queue、rollback/close）、`/theskumar/python-dotenv`（显式路径与 dotenv_values）、`/exa-labs/exa-py`（异步 API/highlights），并对锁定安装源码/签名复核。一次追加 Exa 文档检索超时，未把超时冒充检索成功；历史审阅的官方出处仍保留在第 8 节。

本次没有新增/升级依赖，主要锁定版本为 aiosqlite 0.22.1、exa-py 2.14.0、FastAPI 0.136.1、HTTPX 0.28.1、Uvicorn 0.47.0、Starlette 1.6.0。这里记录的是本地安装/锁文件，不宣称它们是最新版本。

没有覆盖 `.env`、清空真实数据库、创建新 API key、提交凭据或推送远程。真实 E2E 只读取配置，在临时库执行；网络测试仅发送合成题目，错误输出先移除已知凭据，再执行项目脱敏。

## 14. 提交记录与剩余边界

### 14.1 已完成的原子代码提交

| 批次 | 提交 | 变更 |
| --- | --- | --- |
| 1 | `9c2f9f1` | `fix(storage): make initialization and transactions cancellation-safe` |
| 2 | `69100f3` | `refactor(config): load settings explicitly at application boundaries` |
| 3 | `9387325` | `fix(boundaries): reject invalid inputs and redact credentials` |
| 4 | `2c60246` | `refactor(core): separate question processing and answer orchestration` |
| 5 | `9746eb5` | `refactor(storage): give each runtime one shared database owner` |
| 6 | `fdcecb7` | `fix(search): filter evidence by lexical relevance before quality scoring` |
| 7 代码清理 | `bdfdd9a` | `refactor(core): remove redundant helpers and test scaffolding` |

报告及 README/DEPLOY/TESTING 作为独立文档提交，不与缺陷修复混合；所有提交使用 `git -c commit.gpgsign=false commit`，英文、scope、动词原形描述逻辑，无远程 push。

### 14.2 不应隐瞒的限制

1. **真实 HTTP E2E 已通过，范围不等于浏览器 UI。** 真实 Exa/LLM、fallback、OCS HTTP 响应、缓存及计数均有成功记录；没有操作浏览器中的真实 OCS 用户脚本。此前 503 保留为历史上游故障，不承诺未来服务可用性。
2. **单机单实例是当前部署目标。** 多个不同 Database owner 同时对同一个全新 DELETE-journal 库切换 WAL，仍可能 SQLite locked。单 owner 并发初始化与初始化后的跨连接写有测试；未增加路径全局锁、文件锁或隐藏重试解决未要求的多实例首次启动。
3. **提交不能被取消“撤销”。** 原验收只测试了事务体取消，没有覆盖尚未执行的排队 COMMIT 和回滚期间重复取消；这是真实验收缺口，不是“已完成提交不可撤销”能够解释掉的情况。第 15 节给出修复和两侧边界回归；进程 kill、掉电及任意时点组合故障不在该保证内。
4. **相关性是启发式。** 中文单字题、同义词、改写、纯符号题仍可能被过滤；正反例覆盖已发现缺陷，不代表召回率或正确率评测。没有语料/指标依据前不增加 embedding/NLP 服务。
5. **脱敏有识别范围。** 已修复已知格式并尽量减少上游错误内容，但正则不能保证过滤任意未知字符串中的密钥。`check` 主动输出含 token 的 OCS 导入配置仍是既定用户功能，运行时自行保管终端输出。
6. **无性能夸大。** 单连接目的是生命周期与事务边界清楚，不声称提高并发吞吐；每次指标更新仍有事务/维护成本。未做压测，不宣布 QPS 改善，不无依据加索引。
7. **类型与平台范围明确。** 有类型注解、测试、Ruff 和编译检查，没有独立类型检查通过记录；本轮运行平台为 Linux/Python 3.12，未声称 Windows/macOS 文件权限和服务行为已验收。

【历史裁决的更正】首次重构建立了清晰边界，但 174 项绿灯不足以关闭全部存储正确性问题。后续发现的日志淘汰崩溃与取消竞态必须单独复现、修复和验证，最终状态以第 15 节为准。

## 15. 补充缺陷修复与重新验收（2026-09-20）

### 15.1 缺陷、函数与直接证据

| 问题 | 根因与修复 | 回归证据 |
| --- | --- | --- |
| 错误日志参与淘汰后写入、清缓存、重启失败 | `Database._oldest_rows` 对 INTEGER PRIMARY KEY 表使用未起别名的 rowid，结果列名实际为 id；改用显式 `row_id`。 | `test_capacity_prunes_mixed_cache_and_error_rows`：三个入口在旧实现全部失败，修复后全部通过；32 条正常大小证据、非空错误日志和累计指标共同参与验证。 |
| 排队 COMMIT 在任务取消后仍提交 | aiosqlite 取消等待并不会移除工作队列里的操作。`Database._transaction.authorize` 拒绝已取消、尚未授权的 COMMIT，业务与淘汰事务共用实现。 | `test_cancelled_queued_commit_rolls_back`：真实工作线程阻塞、COMMIT 入队后取消；write/prune 两条路径、单次/重复取消共四例，旧实现全部失败，修复后通过。 |
| 重复取消提前释放锁 | 原 rollback 的 await 可被再次取消；`Database._rollback` 持锁等待回滚任务完成，再传播取消。 | 上述四例中的两个重复取消场景断言 worker 未释放时锁仍持有、writer 未结束；随后无残留事务、无误提交，可继续写入并重连。 |
| 提交后的新增清理错误分类 | authorizer 清理是事务退出边界；已完成提交后清理失败也必须使用 `PostCommitError`。 | `test_post_commit_failure_does_not_claim_rollback` 现在覆盖容量维护及 authorizer 清理两个分支；后者先失败再修复。 |
| 测试重载技巧与过期结构探测 | 删除 `test_check_reports_invalid_configuration` 的 `sys.modules.pop`；schema 必须完整初始化，淘汰不再查询 sqlite_master 并静默跳过缺表。 | 存储、CLI、配置加载针对性测试 41 项通过；没有增加兼容入口。 |

`test_cancellation_after_commit_preserves_committed_write` 明确验证相反边界：SQLite 已提交、结果尚未返回调用方时发生取消，记录保持持久化且取消仍传播。**准确合同是：取消被事务协程处理后、SQLite 尚未授权 COMMIT 时保证拒绝提交并回滚；不保证撤销已经授权执行的 COMMIT，也不把“调用了 Task.cancel”视作跨线程同步屏障。** 回滚受 shield 保护，COMMIT 不受 shield 保护；不是用强制完成提交掩盖误提交。

采用已有库的公开能力而非自行维护线程队列：Context7 与本地锁定源码核对了单连接工作队列和 `set_authorizer`；[aiosqlite 0.22.0 变更记录](https://aiosqlite.omnilib.dev/en/v0.22.0/changelog.html)确认此公开接口的最低版本，[SQLite authorizer 文档](https://sqlite.org/c3ref/set_authorizer.html)说明授权发生在语句准备阶段。因此仅提升依赖下限，不新增依赖、不迁移 SDK、不改缓存版本。

### 15.2 本次实际验证与保护边界

- 修复前新增混合淘汰三例、取消四例分别失败；没有通过改写预期把旧实现变绿。
- 取消两侧边界共五例连续复跑 20 次通过。补齐清理失败分类后，存储与缓存针对性测试 32 项通过，Ruff 与改动文件格式检查通过。
- 在 `0b1fb39` 运行用户指定的三条冻结环境命令：**183 passed，2.80 秒；Ruff 全通过；py_compile 通过**。相较 174 项净增 9 项，相较原 121 项净增 62 项。仍只有既有 Starlette TestClient/HTTPX 弃用警告，没有为消除警告增加依赖。
- `git diff --check` 通过；AST 核对第 12 节 **13/13 文件、126/126 函数名称与定义行号**一致，包内相对导入图无循环。Serena 确认测试已无 `sys.modules.pop` 或 `importlib.reload`；搜索适配测试模拟 SDK 响应的 `SimpleNamespace` 不属于 Runtime 兼容壳，保留。
- 所有自动回归使用合成凭据和临时数据库；真实 E2E 只读取既有凭据并使用独立临时库，不覆盖 `.env`、不清业务数据库、不推送远程。

本次重新用 **curl** 调用既有 LLM endpoint，请求模型仍为 `deepseek-flash`：退出码 0、**HTTP 200、正文答案恰为 `OK`**，返回模型标识 `deepseek-v4.1-flash`。Authorization 和请求体经 `curl --config -` 的 stdin 传入，不在命令参数中暴露凭据；这只是本次实际响应标识，不据此推断上游模型版本策略。

随后通过真实 Uvicorn loopback TCP 与真实上游，在两个独立临时库中验证 OCS HTTP 合同：

| 场景 | 实时答案 / provider | LLM 调用数 | 再次请求 | 错误记录 |
| --- | --- | --- | --- | --- |
| 真实 Exa＋LLM，水的化学式 | `A` / `exa` | 1 | 相同答案，答案缓存命中 1 次，LLM 调用数不增加 | 0 |
| 禁用 Exa、真实 LLM fallback，一加一 | `A` / `llm-fallback` | 1 | 相同答案，答案缓存命中 1 次，LLM 调用数不增加 | 0 |

两场景均验证 health、未认证 stats/search、实时成功 code、provider header 及缓存命中不带 provider header。搜索场景 `search_requests=1/search_successes=1`；fallback 场景 `search_requests=0/llm_fallbacks=1`，其 `search_cache_misses=2/answer_cache_misses=3` 保留既有按查询路径计数口径，没有改成按题目去重。加上独立 curl 共执行 3 次真实 LLM 请求、1 次 Exa 搜索；没有发生重试。两个脚本都确认 `.env` 内容摘要前后一致，仅发送合成题目，没有验证浏览器中的 OCS 用户脚本。

### 15.3 原子提交

| 提交 | 逻辑改动 |
| --- | --- |
| `7e341ec` | 修复日志淘汰的 rowid 列别名，并补三入口回归。 |
| `3a5d87e` | 拒绝已取消的排队 COMMIT，等待重复取消下的回滚，提升 aiosqlite 下限。 |
| `a8ceab1` | 删除模块重载和过期 schema 探测。 |
| `0b1fb39` | 区分提交后的 authorizer 清理错误，补充对应失败回归。 |

## 16. 性能、异步与并发审计

审计日期：**2026-09-20（Asia/Shanghai）**。代码快照仍为 `0b1fb39`，文档追加前 HEAD 为 `96547d3`；本节源码行号沿用第 12 节的实际 `def` 起始行号。本次只补充审计记录，不修改业务代码、测试、依赖或生产 schema。

【品味评分】**🟡 异步实现正确，但并发治理与数据库热路径尚未优化。**

【主要问题】32 个相同冷题请求会重复发起 32 次搜索和 32 次生成；答案缓存命中仍等待指标持久化；一次容量维护能让同库读取等待约 680 ms。三者分别是重复工作、请求热路径成本和数据库队头阻塞，不能混称为“async 没生效”。

【结论】**✅ 值得做有测量依据的局部优化，不值得先换数据库、加 Redis、堆 worker 或拆多层框架。** 第 15 节的正确性回归通过，不代表吞吐、尾延迟和过载行为已经达标；本节也不把性能改进项伪装成已修复。

### 16.1 测量方法与适用边界

| 项目 | 本次条件 |
| --- | --- |
| 运行环境 | Linux；Python 3.12.11；SQLite 3.47.1。 |
| 锁定依赖 | aiosqlite 0.22.1、HTTPX 0.28.1、exa-py 2.14.0、Uvicorn 0.47.0、FastAPI 0.136.1。 |
| 数据与凭据 | 合成题目与合成凭据；配置从 `.env.example` 构造，不加载真实 `.env`。 |
| 数据库 | `~/.cache/ocs-perf-*` 下的临时库，与仓库位于同一 Btrfs 文件系统，不用 `/tmp` tmpfs 代替真实磁盘；实验结束自动清理。 |
| 磁盘条件 | 测量时所在卷占用约 97%，其他轮次出现明显 I/O 长尾；结果不是隔离硬件上的容量承诺。 |
| HTTP 测量 | Uvicorn loopback TCP；客户端与服务端同进程、同事件循环，包含压测客户端开销，不代表独立压测机结果。 |
| 上游测量 | 使用真实 AsyncExa/HTTPX 调用本地受控 HTTP fixture；固定搜索等待 100 ms、LLM 等待 200 ms，不调用外部付费 API。 |
| 样本量 | 缓存 HTTP 每个并发档 600 请求；直接 service 每档 1000 请求、两轮；最近搜索每组 60 次串行查询。 |
| 统计口径 | RPS 为完成请求数除以整轮耗时；P50/P95 为单请求延迟分位数；事件循环延迟是采样值，不是硬实时保证。 |

**已排除一项压测工具干扰：** 初轮手动监听 socket 未显式设置 `TCP_NODELAY`，连健康接口都出现约 42 ms 延迟。修正 helper 后重跑，本节 HTTP 吞吐、上游并发和慢分块响应采用修正后的结果，不引用初轮 TCP 数据。初轮独立的数据库查询计划和容量维护测量不经过该 TCP 路径，仍可使用。

上游场景先以不同题目预热连接，再测冷题；不能把连接预热误写成答案缓存命中。不同请求量的串行与并行实验只用来验证能否重叠等待，不直接计算“加速倍数”。

### 16.2 已经正确的异步与资源边界

| 函数位置 | 审计判断 |
| --- | --- |
| `src/llm_ocs_bridge/search.py:27` `ExaSearch._get_client`；`src/llm_ocs_bridge/search.py:40` `ExaSearch.search` | 使用 AsyncExa，网络等待可让出事件循环；搜索调用有 `asyncio.timeout` 边界。 |
| `src/llm_ocs_bridge/llm.py:97` `LLMAnswerer._call_model` | 复用异步 HTTPX 客户端，不是每题新建连接池；读取响应有既定字节限额。 |
| `src/llm_ocs_bridge/llm.py:54` `LLMAnswerer.answer` | 重试等待使用 `asyncio.sleep`，不是同步 sleep；默认两次重试指首次调用之后再尝试两次。 |
| `src/llm_ocs_bridge/storage.py:103` `Database.read`；`src/llm_ocs_bridge/storage.py:109` `Database.write` | aiosqlite 单连接工作线程执行 SQLite 操作；owner 锁保护完整事务和同连接读取，不让其他任务读到未提交状态。锁内数据库操作串行是当前正确性边界，不是简单删锁的理由。 |
| `src/llm_ocs_bridge/service.py:120` `_resolve_evidence`；`src/llm_ocs_bridge/service.py:175` `_generate_answer` | 外部网络等待不持有数据库事务锁，不同题的上游请求能够并发。 |
| `src/llm_ocs_bridge/service.py:208` `answer`；`src/llm_ocs_bridge/service.py:35` `Runtime.aclose` | 搜索证据到 LLM 输入是真实数据依赖，不能盲目 `gather`；现有 Runtime 集中关闭资源，新增后台/共享任务也应纳入此生命周期。 |

受控上游实测：

| 场景 | 请求数 / 并发 | 总耗时 | Exa / LLM 调用数 | Exa / LLM 峰值并发 |
| --- | --- | --- | --- | --- |
| 不同冷题串行 | 16 / 1 | 4.9786 s | 16 / 16 | 1 / 1 |
| 不同冷题并发 | 32 / 32 | 0.4449 s | 32 / 32 | 32 / 32 |
| 相同冷题并发 | 32 / 32 | 0.4126 s | 32 / 32 | 32 / 32 |

串行单题 P50 为 310.728 ms，接近 100 ms 搜索加 200 ms 生成的受控等待。不同题并行总耗时未线性累加，证明 I/O 并发确实有效；相同题的重复调用则暴露另一个问题，见 P01。

### 16.3 缓存命中基准

以下 HTTP 请求全部命中搜索模式答案缓存，不调用上游：

| 并发 | 请求数 | RPS | P50（ms） | P95（ms） |
| --- | --- | --- | --- | --- |
| 1 | 600 | 891.0 | 1.088 | 1.384 |
| 8 | 600 | 836.6 | 9.033 | 10.667 |
| 32 | 600 | 791.4 | 39.324 | 45.202 |
| 128 | 600 | 530.8 | 188.437 | 319.714 |

直接调用 service 的相同缓存命中路径，每档两轮结果范围如下；绕过 HTTP，不是同一层的吞吐保证：

| 并发 | 每轮请求数 | RPS 范围 | P95 范围（ms） |
| --- | --- | --- | --- |
| 1 | 1000 | 2325.8–3196.1 | 0.403–0.577 |
| 8 | 1000 | 2153.9–2569.6 | 3.615–5.201 |
| 32 | 1000 | 2860.2–2885.5 | 12.745–14.058 |
| 128 | 1000 | 2521.1–2618.4 | 50.529–50.939 |

第二轮单独 store 读取约为 14335–17008 次/s。由此可以排除“缓存命中全部成本就是主键查询”这种解释；service 还包含事务写入和维护检查，HTTP 层又有解析、传输与调度成本。并发提高后延迟上升、吞吐未同步提高，不能据此把所有差额都归咎于 SQLite，也不能把本机短测结果当成生产最大 QPS。

### 16.4 函数级发现与最小优化方向

#### P01 · 高优先级：相同冷题没有在途请求合并

- **位置：** `src/llm_ocs_bridge/service.py:208` `answer`、`src/llm_ocs_bridge/service.py:120` `_resolve_evidence`、`src/llm_ocs_bridge/service.py:175` `_generate_answer`。
- **证据：** 32 个相同冷题并发产生 32 次 Exa 和 32 次 LLM 调用。已有持久化缓存只在结果写入后生效，不能合并正在进行的相同工作。
- **影响：** 突发重复题放大上游流量、费用和限流压力；单纯提高连接池或 worker 数量不能消除重复工作。
- **最小方向：** 在单个 Runtime 内按完整题目缓存身份合并进行中的任务，包含题目、选项、题型及现有隔离语义；不同题继续并发，不使用一个全局业务锁串行全部题目。
- **实施前必须定义：** 某个等待者取消是否影响共享任务、全部等待者离开后的处理、完成/失败后移除条目、应用关闭时回收任务；区分按 HTTP 请求计数和按真实上游尝试计数，保留 provider header 合同及搜索优先于 fallback 的顺序。
- **验收建议：** 32 个相同冷题只产生一次有效搜索/生成流程；不同选项或题型不合并；异常、取消和关闭不会留下永久占位或泄漏任务。此方案只承诺进程内去重，不暗示跨 worker 合并。

#### P02 · 高优先级：缺少总时限和应用层并发准入

- **位置：** `src/llm_ocs_bridge/llm.py:97` `LLMAnswerer._call_model`、`src/llm_ocs_bridge/llm.py:54` `LLMAnswerer.answer`、`src/llm_ocs_bridge/app.py:163` `search`、`src/llm_ocs_bridge/main.py:274` `run`。
- **现状：** Exa 有整体调用超时，LLM 使用 HTTPX 的连接/读取/写入/连接池等待超时；后者不是覆盖排队、搜索、重试退避和生成的端到端 deadline。应用未设置 Semaphore、有界等待队列或整条答题流程总时限；默认重试退避没有 jitter。
- **不能误报：** 锁定 HTTPX 客户端默认连接池有 100 个总连接和 20 个 keepalive 连接，不是“无限连接”；`httpx.Limits()` 构造器参数默认值不能代替 `AsyncClient` 实际默认值。连接数限制也不等于限制服务端任务数或等待队列。Uvicorn 默认 `limit_concurrency=None`，本项目启动未显式配置。
- **慢响应实测：** LLM timeout 设置 80 ms，本地上游每 40 ms 返回一小块数据，调用在 **328.95 ms** 后仍成功返回 `A`。这是阶段性读取超时的预期语义，不能声称“80 ms 内必定结束”。
- **客户端断开实测：** 客户端 60 ms 读超时退出，模拟 LLM 等待 250 ms；后端仍完成一次生成并把 `A` 写入缓存。断开后继续填充缓存可以是产品选择，不直接认定为正确性 bug；缺口是尚无明确资源预算与取消策略。
- **最小方向：** 先定义可接受的并发上游数、排队时长、总时限和超载响应合同，再引入简单的准入限制；总预算应覆盖重试，不能每次重试重新获得完整总时限。若采用 P01，共享任务不应随任意一个等待者断开而被误取消。
- **验收建议：** 用慢分块、排队、重试和断开 fixture 验证资源上限及释放；超限仍保持约定 OCS 状态码/错误格式。阈值须依据真实负载选择，本次没有给出生产推荐数值。

#### P03 · 中优先级：缓存命中仍同步等待指标持久化

- **位置：** `src/llm_ocs_bridge/service.py:77` `_cached_answer`、`src/llm_ocs_bridge/metrics.py:28` `MetricsStore.incr`、`src/llm_ocs_bridge/storage.py:109` `Database.write`、`src/llm_ocs_bridge/storage.py:258` `Database._secure_file_permissions`。
- **证据：** 一次正常搜索模式答案命中含 1 次读和 1 次指标写事务；插桩记录 **8 次 aiosqlite 工作线程操作**（execute=3、fetchone=1、close=1、set_authorizer=2、commit=1），以及 **15 次 Python 层 stat、3 次 chmod**。这里的 8 次不是 8 条业务 SQL，也不把 Python 调用计数冒充内核 syscall 追踪。

在正常、未重试、无坏缓存、未触发淘汰的路径上，按源码调用顺序核对的业务读写数量为：

| 路径 | store 读取次数 | 写事务次数 |
| --- | --- | --- |
| 搜索模式答案缓存命中 | 1 | 1 |
| Exa 禁用且搜索证据缓存未命中，fallback 答案缓存命中 | 3 | 3 |
| 搜索与答案缓存均未命中，Exa 和 LLM 一次成功 | 2 | 7 |

- **判断：** 指标和维护检查是真实热路径成本；表中不是说每次事务都执行 VACUUM，容量检查与全库重建必须区分。
- **最小方向：** 先测量可合并的同阶段持久化操作，减少重复事务及维护检查，不为尚未量化的收益建立通用批处理框架。不能把累计指标擅自改成纯内存或不保证刷盘的后台任务。
- **权限 A/B 的负面结果：** 原权限函数平均约 23–39 μs；仅在不是 0600 时 chmod 的条件版约 16–22 μs。但三轮端到端吞吐没有稳定收益，且受磁盘长尾干扰；**不能把跳过 chmod 宣称为主要优化，更不能宣称已经验证吞吐提升。**
- **验收建议：** 对比同环境下事务数、工作线程往返、吞吐和尾延迟，并验证累计计数、取消边界及 0600 权限不回退。

#### P04 · 中优先级：容量维护造成数据库队头阻塞

- **位置：** `src/llm_ocs_bridge/storage.py:109` `Database.write`、`src/llm_ocs_bridge/storage.py:173` `Database._enforce_capacity_locked`、`src/llm_ocs_bridge/storage.py:212` `Database._oldest_rows`。
- **方法：** 临时库写入 50000 条搜索缓存，每条 payload 约 1000 bytes；库及附属文件总计约 **66.36 MiB**。将容量预算调至比当时体积低 4096 bytes，再增加一个指标，触发容量维护。
- **实测：** 触发维护的整次写调用耗时 **679.385 ms**，同时发起的答案缓存读取等待 **679.62 ms**；事件循环最大额外延迟约 **1.055 ms**。SQL trace 记录该次路径只有 **1 次 VACUUM**，没有回到每批删除都压缩的旧实现。
- **判断：** 约 680 ms 是本次“维护加写入”整条调用耗时，不冒充单独 VACUUM 的计时；**事件循环仍能运行，但所有争用该数据库 owner 锁的读写都需等待。** 单次压缩仍有全库重建成本。
- **最小方向：** 研究接近容量边界时能否减少反复淘汰/压缩；任何淘汰余量或压缩时点调整都要与硬容量合同一起决定，不暗改为 TTL/LRU。仅把 VACUUM 包进 `create_task` 不会消除同连接及共享锁竞争。
- **验收建议：** 增加持续接近容量上限的混合读写实验，记录维护频率、锁等待 P95/P99、文件实际大小及有效缓存保留量；当前一次样本不能证明长期维护频率。

#### P05 · 中优先级：最近搜索查询已有时间索引依据

- **位置：** `src/llm_ocs_bridge/cache.py:84` `SearchCacheStore.recent`；schema 入口 `src/llm_ocs_bridge/storage.py:268` `Database._init_db`；相关淘汰查询 `src/llm_ocs_bridge/storage.py:212` `Database._oldest_rows`。
- **方法：** 查询 `ORDER BY created_at DESC LIMIT 10`；每组 60 次串行调用。仅在临时数据库创建 `search_cache(created_at)` 索引 `bench_created_at`，没有改变生产 schema。

| 情形 | 行数 | EXPLAIN QUERY PLAN | P50（ms） | P95（ms） |
| --- | --- | --- | --- | --- |
| 无索引 | 50000 | `SCAN search_cache`；`USE TEMP B-TREE FOR ORDER BY` | 51.193 | 57.469 |
| 临时添加时间索引 | 49900 | `SCAN search_cache USING INDEX bench_created_at` | 0.099 | 0.177 |

- **边界：** 有索引组在前述容量淘汰和 VACUUM 之后测量，少 100 行，数据布局也已改变；不是严格保持所有条件一致的 A/B，不能把比值宣传为固定加速倍数。查询计划确实从全表扫描加临时排序变为使用索引，已足以支持进一步评估。
- **最小方向：** 优先评估这一条时间索引，在同样规模与布局下补齐读写对比、索引体积和迁移幂等性；不要顺手给所有时间列加索引，也不能把最近搜索查询收益外推为整体淘汰查询的同等收益。
- **验收建议：** 保存索引前后计划和重复测量结果；同时测缓存写入成本及容量变化，确认既有数据库可直接初始化使用。

#### P06 · 较低优先级：大证据的同步 CPU 工作仍在事件循环执行

- **位置：** `src/llm_ocs_bridge/search.py:125` `ExaSearch._semantic_score`、`src/llm_ocs_bridge/cache.py:180` `encode_search_results`、`src/llm_ocs_bridge/llm.py:229` `format_search_context`。
- **方法：** 构造 10 条中文证据；每档执行 5 次取中位数。测量相关性计算、缓存 JSON 序列化和 LLM 证据渲染，不包括网络等待或 SDK 响应解析。

| 每条证据字符数 | 缓存 JSON（bytes） | LLM context（bytes） | 同步处理耗时中位数 |
| --- | --- | --- | --- |
| 988 | 27670 | 26999 | 0.84 ms |
| 19988 | 537670 | 131071 | 16.77 ms |
| 99997 | 2685280 | 131071 | 83.94 ms |

- **判断：** `MAX_EVIDENCE_BYTES` 限制交给 LLM 的上下文，不限制此前 SDK 响应、词面评分和缓存序列化成本；末端截断不等于整个证据处理流程有相同预算。这是合成压力样本，**不是实际 Exa 常态响应量**，也不是直接测得的事件循环延迟分位数。
- **最小方向：** 先调查真实返回大小、明确输入规模预算和超限策略，避免在渲染前无界处理大证据。不要立即增加 NLP 依赖或把所有纯函数丢进线程池；也不能为了缩短输入重新丢弃后续有效 highlights。
- **验收建议：** 测量大证据下事件循环延迟及并发请求尾延迟，继续验证全部有效 highlights、相关性规则和响应/证据字节上限。CPU 表来自当次终端实测，未另存 JSON，证据留存强度低于其他表。

### 16.5 优化顺序与行为约束

建议实施顺序是 **P01 → P02 → P03/P05 → P04 → P06**：先消除重复付费工作、明确资源预算，再缩短数据库热路径；容量和大证据优化按真实工作集推进。此顺序是基于本次证据的建议，不是已经执行的代码改动。

1. 保持扁平包和当前 Runtime 所有权，不引入 ORM、Redis、DI 容器或 provider 框架。
2. 不删除保护完整事务的锁，不把一个 Runtime 的实例锁当作跨进程锁；本次没有验证多 worker 的吞吐及跨进程维护竞争。
3. 不为追求吞吐牺牲取消后回滚、提交后错误分类、0600 权限、累计指标及已有数据库兼容性。
4. 保持完整题目的缓存隔离、搜索模式答案优先、搜索不可用后才读取 fallback 答案，以及 OCS 状态码和 provider header 语义。
5. 不改变重试次数定义、瞬态/确定性错误分类、按写入时间淘汰、无 TTL 和字节上限；性能改动本身不是随意升级缓存版本的理由。
6. 每项优化先补针对性回归，再用相同测量口径验证；最终仍需第 15 节使用的三条冻结环境检查。没有实测收益的“微优化”不应以性能名义合入。

**尚未验证：** 独立压测进程/机器下的容量、长时间稳定负载、真实上游吞吐及限流、生产数据分布、多 worker 行为、真实 OCS 浏览器并发。前述本地 fixture 压测不能替代这些验证，也不覆盖第 15 节之外的新正确性场景。本次文档追加未重新运行 183 项测试，不把历史绿灯表述为新一次执行。

### 16.6 证据留存与资料依据

以下临时材料在写入文档时已确认存在；**不属于仓库永久交付物，`/tmp` 清理后不保证可用**。本节保留关键数据、条件和函数位置，不把数百行实验脚本复制进项目：

| 临时材料 | 用途与限制 |
| --- | --- |
| `/tmp/ocs_perf_analysis_20260920.py` | 主实验脚本，当前 helper 已显式启用 `TCP_NODELAY`。 |
| `/tmp/ocs_perf_corrected_results_20260920.json` | 本节采用的缓存基准、操作计数、上游并发和慢分块响应结果。 |
| `/tmp/ocs_perf_results_20260920.json` | 保留容量维护与查询计划数据；其中旧 TCP 吞吐数据已排除。 |
| `/tmp/ocs_perf_followup_20260920.py`；`/tmp/ocs_perf_followup_results_20260920.json` | 权限 A/B 与客户端断开实验；其中未修正 TCP 的延迟不用于本节吞吐结论。 |
| `/tmp/ocs_perf_tcp_nodelay_20260920.json` | 健康接口及 GET/POST 的 TCP 干扰复核。 |

库行为依据沿用性能分析时通过 Context7、Exa 和锁定依赖源码完成的核对：aiosqlite 的单连接工作队列、HTTPX 的分阶段超时与连接池限制、SQLite VACUUM 的重建成本。SQLite 与 aiosqlite 资料见第 8 节 R3/R4；HTTPX 对照文档为 `https://www.python-httpx.org/advanced/timeouts/` 和 `https://www.python-httpx.org/advanced/resource-limits/`。文档说明用于解释机制，具体性能判断来自本节实验，不用官方机制描述代替实测。

## 17. 性能重构执行计划

制定日期：**2026-09-20（Asia/Shanghai）**。本计划依据第 16 节实测和当前源码；**尚未修改运行时实现**。制定计划时重新执行冻结环境检查：**183 passed，4.52 秒；Ruff、py_compile、git diff --check 均通过**，保留一项既有 Starlette TestClient/HTTPX 弃用警告。这是新的正确性基线，不是优化后的性能结果。

目标是消除重复上游工作、降低数据库热路径及维护成本、明确并发资源预算。保持现有扁平包，不新增运行时模块、数据库 owner、ORM、Redis、DI 容器或 provider 框架；本计划没有新增依赖需求，不迁移 LLM SDK。

### 17.1 实施前待确认的行为决策

| 决策 | 推荐合同 | 确认状态 |
| --- | --- | --- |
| 同题共享任务取消 | 单个等待者取消不取消共享任务；全部等待者离开后，任务仍可在其总预算内完成并填充缓存。应用关闭则取消并等待业务任务清理，再关闭客户端与数据库。 | 用户已于 2026-09-20 明确接受。 |
| 冷路径超载 | 限制独立冷题任务及共享等待者数量；达到上限快速失败，不建立无界队列。搜索模式答案缓存命中不占上游任务名额。保持 HTTP 200、OCS `code=0` 的业务失败格式，不添加成功 provider header。 | 用户已于 2026-09-20 明确接受。 |
| 总预算 | 新任务的预算覆盖搜索、生成和重试退避，不因重试或晚到的等待者而重置；预算到期取消业务工作，但等待必要回滚及资源清理，不承诺精确时刻强制中断。 | 用户已于 2026-09-20 明确接受；并发数和时限须显式配置、校验，不冒称本地 fixture 能确定真实上游配额。 |
| 容量与证据合同 | 不放宽硬容量上限，不增加 TTL 或改为 LRU，不以超额淘汰、丢弃后续有效 highlights 换取吞吐。若优化必须改变这些合同，单独提出方案。 | 沿用既有要求。 |

这些准入限制只约束冷路径，不等于限制整个 HTTP 服务器的所有连接或任务；不把连接池大小、Uvicorn 全局并发限制与业务上游配额混成一个参数。实施时分别核对异常分类、计数及配置加载，不把新增超载/超时统一冒充 `MissingKey`。

### 17.2 批次 0：建立可重复比较的基准

- 保留一个小型、独立运行的基准入口；不依赖 `/tmp` 脚本长期存在，不增加压测库，不默认纳入普通 pytest 的耗时阈值断言。
- 将 HTTP 客户端与服务端分进程，校验 TCP 配置；使用临时磁盘数据库、合成凭据和本地可控上游，不读取真实 `.env` 或业务数据库。
- 固定搜索答案命中、fallback 答案命中、不同冷题、相同冷题、近容量混合读写及大证据场景；记录吞吐、P50/P95、事件循环延迟、锁等待、工作线程操作、事务数和上游尝试数。
- 优化前后使用相同数据、请求数与存储条件交错重复测量；至少保存五轮原始结果及汇总。索引比较必须重新使用相同行数和布局，不能沿用第 16 节“淘汰前对淘汰后”的非严格 A/B。
- 性能数据输出不包含请求凭据或真实题目。波动范围不能支持收益结论时，明确标为未证实，不按最好一轮选数。

### 17.3 分批实现与验收

每批都按“补回归 → 修改实现 → 针对性验证 → 前后基准”执行。功能修复、性能优化和纯文件移动分别提交；本计划不包含为美观而移动文件。函数所在文件及当前行号见第 12 节，实施后更新定位。

| 批次 / 对应发现 | 函数与最小改动 | 先补回归 | 性能及完成门槛 |
| --- | --- | --- | --- |
| **1：同题在途合并 / P01** | `service.answer`、`Runtime`、`Runtime.aclose`：由 Runtime 持有共享任务，沿用完整题目缓存身份；缓存命中保留快速路径，不同题不串行。完成、失败、取消后清理注册项。 | 扩展 `tests/test_service.py`：32 个同题并发、不同选项/题型隔离、晚到请求与任务刚完成的竞争、失败重试、单个/全部等待者取消、关闭回收；补 HTTP provider header 对照。 | 同一冷题批次只有一次有效搜索和生成；不同题保持并行；热缓存路径没有可重复的性能回退；无未取出的任务异常或永久占位。 |
| **2：准入与总预算 / P02** | `service.answer`、`Runtime.aclose`、`config.Settings` 及加载/校验入口、`app.search`：实现确认后的冷路径上限、失败映射和总预算；保留现有上游分阶段超时。 | `tests/test_service.py`、`tests/test_config*.py`、`tests/test_app.py`、`tests/test_llm.py`：超载、慢分块、重试退避、取消等待者、关闭，以及所有异常路径释放额度。 | 独立任务和共享等待者不超过各自预算；重试/晚到请求不延长共享工作截止时间；缓存命中不被上游名额阻塞；保持 OCS 合同。 |
| **3：减少数据库往返 / P03** | `SearchCacheStore.get`、`AnswerCacheStore.get` 和其他已有结果数量上限的读取采用 `execute_fetchall`；`Database.database_size_bytes` 合并重复文件状态查询；仅对同阶段实际发生的多个指标更新评估合并提交。 | `tests/test_cache.py`、`tests/test_storage.py`、`tests/test_service.py`：空结果、结果顺序、读取隔离、文件不存在与其他文件错误的区分、计数一致性；复跑全部取消与提交后错误回归。 | 搜索答案命中工作线程操作目标由 8 次减至 6 次，并验证端到端收益。保留一次指标持久化提交，不宣称“命中零写入”；不跨网络等待持有数据库事务。 |
| **4：最近搜索索引 / P05** | `Database._init_db`、`SearchCacheStore.recent`：评估并加入幂等的 `search_cache(created_at)` 索引；单独检查 `_oldest_rows`，不机械索引所有字段。 | `tests/test_storage.py`、`tests/test_cache.py`：旧数据库初始化、重复初始化、结果排序、容量变化、缓存清理及累计指标保留。 | 同一数据集查询计划消除最近搜索的临时排序；保存读延迟、写入成本及索引体积对照，收益足以解释新增存储成本。 |
| **5：容量维护 / P04** | `Database._enforce_capacity_locked`、`_oldest_rows`、`_checkpoint`：分段测量，减少已证实的重复扫描和无效维护；不删除 owner 锁或把 VACUUM 简单搬进无人管理的后台任务。 | `tests/test_storage.py`：仅 WAL 超限、混合表全局时间淘汰、重复维护、并发读取等待、取消回滚、提交后维护失败；补近容量持续混合负载。 | 比较维护频率、锁等待 P95/P99、真实文件大小和保留的有效缓存；单次与累计成本都要报告。不靠放宽硬上限或额外大量淘汰过关。 |
| **6：大证据 CPU / P06** | `ExaSearch._semantic_score`、`encode_search_results`、`format_search_context`：先定位 CPU，再减少等价重复扫描、编码和中间对象；不预设线程池或 NLP 依赖。 | `tests/test_search.py`、`tests/test_cache.py`、`tests/test_llm.py`：中英文/公式固定样例、评分等价、全部有效 highlights、后续片段包含答案、UTF-8 字节边界及缓存往返。 | 保存大证据同步耗时及并发尾延迟对照；输出、评分和证据保留不回退。若必须新增输入限额，先确认超限合同，不能静默裁剪。 |

`execute_fetchall` 已通过 Context7 和锁定 aiosqlite 源码核对：执行与取结果在一次工作队列操作中完成。这里只用于主键或已有数量限制的查询，不把无界结果全集载入内存作为通用优化。原来的事务 owner、authorizer 取消保护、回滚等待、提交后失败分类及文件权限必须保留。

同题合并后，缓存 hit/miss 仍按实际查询记录，共享等待本身不是缓存命中；搜索及 LLM 指标按真实上游事件记录。新增的二次缓存检查也要明确计数，不以“减少了重复请求”为由静默改成按题目计数。是否新增合并计数应结合持久化成本决定，不为观测优化再次制造每个等待者的额外独立写事务。

### 17.4 总验收与提交边界

以下门槛全部满足后，才能将相应性能发现标为完成；若某项只有正确性绿灯或没有稳定收益，只能记录为待处理/未证实。

1. 所有新增针对性回归通过；特别复核初始化失败、排队 COMMIT 取消、回滚期间重复取消、已提交后取消及提交后维护失败的既有边界。
2. 搜索模式答案优先、搜索不可用后才查 fallback 答案、完整题目缓存隔离、重试次数定义、瞬态/确定性错误分类、OCS 鉴权/状态码/provider header 和字节上限保持合同。
3. 最终运行并记录：

   ```bash
   uv run --frozen pytest
   uv run --frozen ruff check src tests pyproject.toml
   uv run --frozen python -m py_compile src/llm_ocs_bridge/*.py tests/*.py
   git diff --check
   ```

4. 将优化前后原始测量摘要、环境、波动和失败尝试补入本报告；更新受影响的既有 README/TESTING 和 `.env.example`。生产 `.env` 只读，真实数据库不作实验目标。
5. 本地验证通过后才做少量真实上游 E2E；使用既有凭据、合成题目和独立临时库，记录包括重试在内的真实尝试数。不使用付费 API 做负载压测，不把本地 fixture 的吞吐当作真实上游性能。
6. 等价性能重构和索引变更不更新缓存版本；只有搜索语义变化才更新 `SEARCH_CACHE_VERSION`，只有 prompt/答案生成规则变化才更新 `PROMPT_VERSION`。
7. 实施提交采用带 scope 的英文 Conventional Commits；按逻辑改动原子拆分，文档与运行时改动不混为一次无边界提交，不自动推送远程。本次仅写入计划，没有执行实现提交。

**执行启动状态（2026-09-20）：** 用户已明确接受第 17.1 节的行为决策，开始逐批实施。原始审计、计划和既有测量不代表优化已完成；实际改动与验收结果另行记录。
