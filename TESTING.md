# HTTP 合同与隔离验收

## 自动回归（不调用真实上游）

```bash
uv run --frozen pytest
uv run --frozen ruff check src tests pyproject.toml
uv run --frozen python -m py_compile src/llm_ocs_bridge/*.py tests/*.py
```

测试使用合成凭据和临时数据库；DEBUG 启动测试会实际启动子进程、监听 loopback 并请求 HTTP。

性能基准使用独立客户端/服务端进程、临时磁盘数据库及异步模拟上游，不读取真实凭据、不调用付费 API：

```bash
uv run --frozen python tests/benchmark.py --rounds 5 --requests 600 --output /tmp/ocs-benchmark.json
```

输出逐轮请求数、成功数、吞吐、P50/P95 和实际搜索/LLM 尝试数；失败不能当作成功吞吐。该基准将冷题上限提高到 128、等待者上限提高到 256，以便对比相同负载；默认准入上限和超时另由自动回归验证。不同版本必须使用相同数据和测量条件，不将模拟上游延迟当成真实 provider 性能。

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
- 上述认证 curl 请求在冷题/等待者额度耗尽时返回 `{"code":0,"msg":"服务繁忙，请稍后重试"}`；超过答题总预算返回 `{"code":0,"msg":"答题超时"}`。两者均为 HTTP 200，且不带 `X-Search-Provider`。用自动回归的受控阻塞上游验证这些分支，不向付费接口发起过载测试。

本次实际运行结果和补充缺陷修复见 `SRC_ARCHITECTURE_REVIEW.md` 第 15 节；逐函数清单见第 12 节，历史上游阻塞与首次验收见第 13–14 节。尚未验证浏览器内真实 OCS 用户脚本交互。
