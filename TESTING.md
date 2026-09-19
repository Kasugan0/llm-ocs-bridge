# HTTP 合同与隔离验收

## 自动回归（不调用真实上游）

```bash
uv run --frozen pytest
uv run --frozen ruff check src tests pyproject.toml
uv run --frozen python -m py_compile src/llm_ocs_bridge/*.py tests/*.py
```

测试使用合成凭据和临时数据库；DEBUG 启动测试会实际启动子进程、监听 loopback 并请求 HTTP。

## 手工 HTTP 验收

在项目目录运行。以下服务复用已有 `.env` 的凭据，但用临时数据库，不修改 `.env` 或业务数据库。不要执行 `source .env` 或打印其内容。

```bash
cache_dir=$(mktemp -d)
trap 'rm -rf "$cache_dir"' EXIT
CACHE_PATH="$cache_dir/e2e.sqlite3" HOST=127.0.0.1 PORT=18080 DEBUG=false uv run --frozen llm-ocs-bridge
```

在另一个终端调用；认证请求可能产生真实 Exa/LLM 费用。把 `X-Access-Token` 请求头放入本机权限为 `0600` 的临时文件，通过 `--header @文件路径` 使用，避免把凭据写入 shell 历史或命令参数。该文件不要提交。

```bash
curl --silent --show-error http://127.0.0.1:18080/health
curl --silent --show-error --include http://127.0.0.1:18080/stats
curl --silent --show-error --include http://127.0.0.1:18080/search \
  --json '{"title":"一加一等于几？","options":["A.2","B.3"],"type":"single"}'
curl --silent --show-error --include http://127.0.0.1:18080/search \
  --header @/path/to/private-token-header \
  --json '{"title":"一加一等于几？","options":["A.2","B.3"],"type":"single"}'
```

- `/health`：HTTP 200；不校验上游可用性。
- 未认证的 `/stats`：HTTP 401；未认证的 `/search`：HTTP 200、`code=0`。
- 输入不合法，包括非 ASCII token：按既定失败合同返回，不应出现 500。
- 成功作答：HTTP 200、`code=1`、`answer` 为选项标签；首次实时作答带 `X-Search-Provider`，重复答案缓存命中不带该头。
- 上游失败：检查认证后的 `/stats` 脱敏错误记录；`/search` 仍以 HTTP 200、`code=0` 表达业务失败，不能只看 HTTP 状态码宣布 E2E 成功。

本次实际运行结果、真实上游阻塞原因和逐函数复核见 `SRC_ARCHITECTURE_REVIEW.md` 第 11–14 节；尚未验证浏览器内真实 OCS 用户脚本交互。
