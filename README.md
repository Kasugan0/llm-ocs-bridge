# llm-ocs-bridge

本机运行的 OCS 搜题桥接服务：可选 Exa 检索，使用 OpenAI 兼容模型生成并校验答案。

OCS 的题库接口与通用 LLM API 并不直接兼容。`llm-ocs-bridge` 负责转换题目、选项和题型，调用搜索与模型服务，再把结果转换回 OCS 可识别的答案格式。它面向单用户本机使用，默认只监听 `127.0.0.1`。

## 为什么使用它

- **直接接入 OCS**：内置 `AnswererWrapper` 配置生成器，不需要手写请求和响应处理器。
- **搜索增强答题**：配置 Exa 后，先检索相关资料，再让模型基于证据作答。
- **LLM fallback**：Exa 未配置、不可用或没有有效证据时，默认允许模型直接作答。
- **兼容常见题型**：支持单选、多选、判断、填空和开放式问题。
- **本机安全边界**：默认只监听 localhost，OCS 请求使用 `X-Access-Token` 鉴权。
- **开箱即检**：`check` 会验证配置和外部服务，并输出可直接导入 OCS 的 JSON。
- **有界本地缓存**：答案、搜索结果和运行统计存储在 SQLite 中，并受配置容量限制。

## 工作方式

```text
OCS
 │  题目、选项、题型
 ▼
llm-ocs-bridge
 ├─ 命中答案缓存 ──────────────────────────────┐
 ├─ Exa 检索证据 ──► OpenAI 兼容 LLM ─────────┤
 └─ 无 Exa / 无有效证据 ─► LLM fallback ──────┤
                                                ▼
                                         OCS 格式答案
```

Exa 是可选项。未设置 `EXA_API_KEY` 时，项目不会匿名调用 Exa，而是直接进入 LLM fallback。本项目目前不支持 Exa x402 付费请求。

搜索模式答案缓存最先查询；fallback 答案缓存只在搜索不可用后查询，因此不会阻止搜索恢复。两种答案缓存命中均不返回 `X-Search-Provider`；实时作答使用 `exa`、`cache` 或 `llm-fallback` 标明证据来源。

## 代码结构

继续使用 `src/llm_ocs_bridge/` 扁平包，不引入框架式分层：

| 模块 | 职责 |
| --- | --- |
| `app.py` | HTTP、鉴权、响应映射和生命周期 |
| `service.py` | 有类型的 Runtime、缓存与答题流程 |
| `questions.py` | 题目/选项归一化、标签解析和输入校验 |
| `images.py` | 图片 data URL 解析、格式与数量大小校验 |
| `llm.py` | 模型 HTTP 请求、响应读取和重试 |
| `answers.py` | 提示词、模型答案解析、OCS 答案编码和校验 |
| `search.py` | Exa 适配、英文词/中文二元组匹配和证据评分 |
| `cache.py` | 缓存键、序列化、读写及最近搜索查询 |
| `storage.py` | 唯一数据库 owner、schema、事务、容量和权限 |
| `metrics.py` | 累计指标及脱敏错误日志 |
| `config.py` / `main.py` | 显式配置加载 / CLI 启动与检查 |
| `models.py` / `utils.py` | 共享数据结构 / 通用文本处理 |

每个 Runtime 只有一个 Database，三个 store 组合共享它。启动时完成 schema 初始化，读操作与完整写事务共用互斥锁，退出时统一关闭。事务体异常退出时回滚；在 SQLite 授权 COMMIT 前处理到的取消会拒绝排队提交并回滚，回滚期间重复取消不会提前释放锁。SQLite 已授权执行的提交不能靠后来的取消撤销，因此 `CancelledError` 不等于“必然未写入”。容量统计包括主库、WAL 和 SHM，先 checkpoint，再按写入时间淘汰，最后集中 VACUUM；不使用 TTL 或 LRU。若写入已提交但后续维护或 authorizer 清理失败，抛出 `PostCommitError`，调用者不应当把它当成未写入而盲目重试。

检索会拼接全部有效 highlights，再逐条剔除零相关证据；质量阈值设为零也不能绕过这一门槛。相关性是轻量词面启发式，不是 embedding 或答案正确性证明。内置证据算法修订号 `v4` 参与搜索和答案缓存键，已有 `.env` 使用旧 `SEARCH_CACHE_VERSION` 也不会复用旧算法的缓存；该环境变量仍可额外手动失效缓存。`PROMPT_VERSION` 只用于生成规则变化。

## 快速开始

要求 Python 3.12 或更高版本，并使用 [uv](https://docs.astral.sh/uv/) 管理环境。

```bash
uv sync
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写 `ACCESS_TOKEN`、`OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。`EXA_API_KEY` 可选。需要图片题时再填写支持图片输入的 `VISION_MODEL`；从旧版本升级且 `.env` 里仍是 `MAX_REQUEST_BYTES=262144` 时，需改为 32MB，否则较大的图片题会被拒绝。

检查配置并生成 OCS 题库 JSON：

```bash
umask 077
uv run llm-ocs-bridge check > ocs-config.json
```

将 `ocs-config.json` 导入 OCS 的「通用 → 全局设置 → 题库配置」，然后启动服务：

```bash
uv run llm-ocs-bridge
```

默认服务地址为 `http://127.0.0.1:8009`。完整安装、配置和 OCS 接入步骤见 [DEPLOY.md](DEPLOY.md)。

## 支持范围

| 项目 | 支持情况 |
| --- | --- |
| 题型 | `single`、`multiple`、`judgement`、`completion`、空题型 |
| 选项格式 | 换行分隔字符串、JSON 数组（与 OCS 传入格式一致） |
| 图片题 | OCS 5 图片题协议：`images`（base64）、`suggestion_title`、`suggestion_options`；需配置 `VISION_MODEL` |
| 模型服务 | OpenAI Chat Completions 兼容接口 |
| 搜索服务 | Exa，可选 |
| 缓存 | 本机 SQLite |
| 部署方式 | 单用户本机运行 |

空题型按开放式问题处理。单选和多选答案按选项位置编号（A、B、C…）校验，与 OCS 的纯字母答案解析一致；选项文字自带字母时以位置为准。判断题形态的单选题（选项为对/错一类）返回 `正确`/`错误`，其余单选返回选项字母。多选答案使用 `#` 连接。

填空答案的 `answer` 始终返回 JSON 数组编码的字符串，由 OCS 解码后保留每个空的边界和答案中的分隔符。升级后内置答案协议修订号自动隔离旧答案缓存，无需清空数据库。

图片题使用 OCS 5 的图片题协议：题干和选项中的图片由 OCS 转成 base64 放入 `images`，原图位置替换为 `[图片N]` 并放在 `suggestion_title`、`suggestion_options`。服务按编号把图片和占位符一起交给视觉模型，文字检索仍使用原始题干。图片请求体积较大，`MAX_REQUEST_BYTES` 默认已按 OCS 的 6 张、单张 3MB 上限配置。

OCS 客户端的请求超时默认是 60 秒，可在客户端配置中调整。建议服务端 `ANSWER_TIMEOUT_SECONDS` 小于客户端的实际请求超时，并为响应传输和资源清理留出余量；需要更长答题时间时可同时调大两端配置。服务端接受任意有限正数，默认值为 55 秒。

## CLI

| 命令 | 用途 |
| --- | --- |
| `uv run llm-ocs-bridge` | 启动服务 |
| `uv run llm-ocs-bridge check` | 检查配置和外部服务，生成 OCS JSON |
| `uv run llm-ocs-bridge clear-cache --scope search` | 清理搜索缓存 |
| `uv run llm-ocs-bridge clear-cache --scope answer` | 清理答案缓存 |
| `uv run llm-ocs-bridge clear-cache --scope all` | 清理全部缓存 |
| `uv run llm-ocs-bridge --help` | 查看完整命令帮助 |

启动、`check` 和 `clear-cache` 均从当前工作目录读取 `.env`，系统环境变量优先，不在导入时读取配置。`clear-cache` 只解析存储配置，且保留累计指标和错误日志。`DEBUG=true` 使用 Uvicorn 应用工厂支持 reload。

## 并发与答题预算

同一进程内，相同完整题目的冷请求共享一次答题任务；不同选项和题型仍然隔离。媒体所属题干或选项参与缓存键，模型调用、答案缓存键及缓存记录共用实际选用的模型。搜索模式答案缓存优先，只有搜索不可用后才读取 fallback 答案缓存。

答案缓存统计按业务查询计数：入口的 search 查询和进入 fallback 后的查询分别计数；共享任务开始前为避免并发重复生成而进行的 search 复查不重复计数。因此这些指标表示缓存查询次数，而非 HTTP 请求数。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `MAX_ACTIVE_ANSWERS` | `8` | 同时运行的独立冷题任务上限 |
| `MAX_ANSWER_WAITERS` | `64` | 全部冷题任务的等待者总上限，包含发起者 |
| `ANSWER_TIMEOUT_SECONDS` | `55` | 从答题入口开始的总预算，覆盖缓存等待、搜索、生成和重试退避；接受有限正数，建议小于客户端实际请求超时（默认 60 秒，可配置） |

配置缺省时使用上述值，无需修改已有 `.env` 才能启动。达到并发上限立即返回 OCS 业务失败，不排无限队列；搜索模式答案命中不消耗上游任务名额。总预算不替代上游各阶段 timeout，且到期后仍需等待必要的事务回滚和资源清理。

取消单个等待者不会取消共享任务；即使全部等待者离开，任务仍可在原预算内完成并缓存答案，后加入者不能延长任务预算。退出应用时先取消并回收这些任务，再释放客户端与数据库。限制是进程内的，不是跨 worker 配额或整个 HTTP 服务的连接上限。

缓存计数仍按实际查询计算，共享等待不是缓存命中。冷题任务会重新检查答案缓存，避免先前未命中的请求在已有任务刚完成后重复生成；这次真实查询同样计入 hit/miss。

## 开发验证

```bash
uv run pytest
uv run ruff check src tests pyproject.toml
uv run python -m compileall -q src tests
```

`uv run pytest` 通过 pytest-cov 强制 90% 覆盖率门槛：即使测试全绿，覆盖率不足也会让命令失败。测试使用合成凭据与临时数据库，不调用付费上游。GitHub Actions 在 push 和 pull request 时运行同样的 lint 与测试。

`tests/e2e_live.py` 是可选的真实上游冒烟脚本：启动真实服务进程并实际调用 Exa 与模型服务，需要 `--confirm-paid` 与有效密钥，按需单独运行；其中另含一组不产生任何上游调用的校验用例。

## 文档

- [安装、配置、OCS 接入与故障排查](DEPLOY.md)
- [完整环境变量及默认值](.env.example)
- [OCS 题库 API 文档](https://docs.ocsjs.com/docs/other/api)

## 安全说明

生成的 OCS JSON 包含真实的 `ACCESS_TOKEN`。不要公开或提交 `.env`、`ocs-config.json`、SQLite 数据库及其 WAL/SHM 文件。除非明确需要局域网访问，否则保持 `HOST=127.0.0.1`。

## 项目状态

当前版本面向个人本机使用，不提供公网部署、多用户隔离或远程管理能力。

## 许可证

[MIT](LICENSE)
