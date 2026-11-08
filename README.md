# llm-ocs-bridge

本机运行的 OCS 搜题桥接服务：可选 Exa 检索，OpenAI 兼容模型作答，按 OCS `AnswererWrapper` 协议双向转换题目与答案。单用户本机使用，默认监听 `127.0.0.1`。

## 功能

- `check` 校验配置并生成可直接导入 OCS 的题库 JSON，内置请求/响应 handler，无需手写。
- Exa 可选：配置后先检索证据再作答；未配置或检索失败时按 `LLM_FALLBACK_ENABLED` 决定是否直接调用模型。
- 题型：`single`、`multiple`、`judgement`、`completion`、空题型；图片题走 OCS 5 `images`/`suggestion_*` 协议，需 `VISION_MODEL`。
- SQLite 答案/搜索缓存，容量有界；相同题目的并发冷请求共享一次答题任务。

## 快速开始

要求 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp .env.example .env
chmod 600 .env
```

`.env` 至少填 `ACCESS_TOKEN`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`；`EXA_API_KEY` 可选；图片题加填 `VISION_MODEL`。

```bash
umask 077
uv run llm-ocs-bridge check > ocs-config.json   # 校验配置并生成 OCS JSON
uv run llm-ocs-bridge                           # 启动，默认 http://127.0.0.1:8009
```

把 `ocs-config.json` 导入 OCS「通用 → 全局设置 → 题库配置」。完整步骤与故障排查见 [DEPLOY.md](DEPLOY.md)。

## 答案规则

| 题型 | 返回 |
| --- | --- |
| `single` | 选项字母（按位置编号，忽略选项自带字母）；选项为对/错形态时返回 `正确`/`错误` |
| `multiple` | `#` 连接的选项字母 |
| `judgement` | `正确`/`错误` |
| `completion` | JSON 数组字符串，每空一个元素，保留分隔符 |
| 空题型 | 自由文本 |

实时作答的响应带 `X-Search-Provider`，取值为 `exa`、`cache` 或 `llm-fallback`；答案缓存命中不带该头。

## 关键约定

- 全部命令读取**当前目录** `.env`，系统环境变量优先。
- 超时：OCS 客户端默认 60s；`ANSWER_TIMEOUT_SECONDS` 默认 55，应小于客户端超时。
- 图片题请求体大：`MAX_REQUEST_BYTES` 默认 32MB（覆盖 6 张 × 3MB）。旧 `.env` 若写死 `262144` 需手改，否则大图片题被拒。
- 缓存：`PROMPT_VERSION` 仅在生成规则变化时递增；`SEARCH_CACHE_VERSION` 留空即可，需手动失效缓存时填写。
- 并发：`MAX_ACTIVE_ANSWERS=8` 个独立冷题任务，`MAX_ANSWER_WAITERS=64` 个等待者；超限立即返回业务失败。
- 生成的 OCS JSON 含真实 `ACCESS_TOKEN`；`.env`、JSON、SQLite 文件一律不要提交或公开。

## 代码结构

`src/llm_ocs_bridge/` 扁平包：

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

## CLI

| 命令 | 用途 |
| --- | --- |
| `uv run llm-ocs-bridge` | 启动服务 |
| `uv run llm-ocs-bridge check` | 检查配置和外部服务，生成 OCS JSON |
| `uv run llm-ocs-bridge clear-cache --scope search|answer|all` | 清理对应缓存（保留指标与错误日志） |
| `DEBUG=true uv run llm-ocs-bridge` | 开发 reload 模式 |

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

## 许可证

[MIT](LICENSE)
