# 安装、配置与 OCS 接入

## 准备

- Python 3.12+、[uv](https://docs.astral.sh/uv/)
- OpenAI 兼容服务的 API 密钥
- 已安装的 OCS 用户脚本；Exa API 密钥（可选）

## 安装

```bash
git clone https://github.com/Kasugan0/llm-ocs-bridge.git
cd llm-ocs-bridge
uv sync
```

## 配置

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32        # 生成一个随机串用作 ACCESS_TOKEN
```

`.env` 最少配置（尖括号内为需替换的值）：

```dotenv
ACCESS_TOKEN=<random-token>
OPENAI_API_KEY=<your-api-key>
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

### Exa 与 fallback

- `EXA_API_KEY` 留空且 `LLM_FALLBACK_ENABLED=true`：跳过检索直接作答，`check` 显示非致命警告。本项目未接入 Exa x402，留空不代表匿名搜索。
- `LLM_FALLBACK_ENABLED=false`：`EXA_API_KEY` 必填，检索失败时不再直接作答。

### 图片题

```dotenv
VISION_MODEL=<vision-capable-model>
MAX_REQUEST_BYTES=33554432
```

- `VISION_MODEL` 留空时图片题直接报错；模型本身不支持图片输入时填写也无效。
- OCS 单题最多 6 张图、单张解码后 3MB，base64 后上限约 24MB，32MB 留有余量。
- 旧 `.env` 常写着 `MAX_REQUEST_BYTES=262144`，会覆盖代码默认值导致图片题被拒，需手改后重启。`check` 会对这类不可用图片配置告警。

### 全部配置项

所有命令读取**执行时所在目录**的 `.env`，同名系统环境变量优先。下表“默认”列为缺省值（`.env.example` 未列出时使用代码内置值）。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址；改动需同步 OCS 脚本 `@connect` |
| `PORT` | `8009` | 监听端口，1–65535 |
| `DEBUG` | `false` | `true` 启用 Uvicorn reload（开发） |
| `APP_NAME` | `llm-ocs-bridge` | 服务名 |
| `ACCESS_TOKEN` | 必填 | OCS 请求头 `X-Access-Token` 的令牌；`check` 会写入生成的 JSON |
| `OPENAI_API_KEY` | 必填 | 模型服务密钥 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `OPENAI_MODEL` | `gpt-4o-mini` | 文本题模型 |
| `VISION_MODEL` | 空 | 图片题模型；留空则图片题报错 |
| `EXA_API_KEY` | 空 | Exa 密钥；`LLM_FALLBACK_ENABLED=false` 时必填 |
| `EXA_TIMEOUT_SECONDS` | `30` | Exa 请求超时（秒，须为正） |
| `MAX_ACTIVE_ANSWERS` | `8` | 同时运行的独立冷题任务上限 |
| `MAX_ANSWER_WAITERS` | `64` | 全部冷题任务的等待者总上限；超限立即返回业务失败 |
| `ANSWER_TIMEOUT_SECONDS` | `55` | 单次答题总预算（秒）；建议小于 OCS 客户端超时（默认 60s） |
| `CACHE_PATH` | `bridge.sqlite3` | SQLite 缓存文件路径 |
| `SEARCH_RESULT_LIMIT` | 空（不限制） | Exa 结果数量上限 |
| `MIN_SEARCH_QUALITY_SCORE` | `0.4` | 搜索质量阈值 0.0–1.0，低于视为无可用证据 |
| `LLM_FALLBACK_ENABLED` | `true` | 无 Exa 或无证据时是否直接调用模型 |
| `MAX_LLM_RETRIES` | `2` | 首次失败后的最大重试次数；0 为只调用一次 |
| `LLM_TIMEOUT_SECONDS` | `30` | 单次模型请求超时（秒，须为正） |
| `LLM_RETRY_BACKOFF_SECONDS` | `0.5` | 重试退避基数（秒） |
| `MAX_LLM_OUTPUT_TOKENS` | `512` | 模型输出 token 上限 |
| `MAX_LLM_RESPONSE_BYTES` | `65536` | 模型响应体字节上限 |
| `MAX_EVIDENCE_BYTES` | `131072` | 送入提示词的检索证据字节上限 |
| `PROMPT_VERSION` | `v1` | 生成规则修订号；提示词变化时递增以隔离旧答案缓存 |
| `SEARCH_CACHE_VERSION` | 内置修订号 | 留空即可；仅在需要手动失效搜索/答案缓存时填写 |
| `MAX_REQUEST_BYTES` | `33554432` | 请求体字节上限，需容纳图片题 |
| `MAX_IMAGES` | `6` | 单题图片张数上限 |
| `MAX_IMAGE_BYTES` | `3145728` | 单张图片解码后字节上限（3MB） |
| `MAX_TITLE_LENGTH` | `8192` | 题干字符上限（建议题干同样受限） |
| `MAX_OPTIONS_COUNT` | `8` | 选项数量上限（建议选项同样受限） |
| `MAX_OPTION_LENGTH` | `2048` | 单个选项字符上限 |
| `MAX_CACHE_BYTES` | `1073741824` | SQLite 总容量上限（1GB），含 WAL/SHM |
| `MAX_ERROR_LOGS` | `1000` | 保留的错误日志条数 |
| `MAX_ERROR_MESSAGE_LENGTH` | `4096` | 单条错误信息字节上限 |
| `CACHE_PRUNE_BATCH_SIZE` | `100` | 单次容量淘汰的批量大小 |
| `STATS_RECENT_LIMIT` | `10` | `/stats` 返回的最近检索与错误条数 |
| `SQLITE_BUSY_TIMEOUT_MS` | `5000` | SQLite 锁等待毫秒数 |

## 检查与导入

```bash
umask 077
uv run llm-ocs-bridge check > ocs-config.json
```

`check` 依次校验 `.env` 权限（0600）、全部配置、图片配置，并向 OpenAI（配置了 Exa 时也包括 Exa）发一次最小请求——会产生小额调用费用。状态与错误进 stderr，stdout 只含 JSON；退出码 `0` 成功、`1` 失败、`2` 参数错误。

导入：打开 `ocs-config.json` 复制全部内容，粘贴到 OCS 面板的「通用 → 全局设置 → 题库配置」并保存。

OCS 通过 `GM_xmlhttpRequest` 访问本机服务，用户脚本元信息需要：

```text
@grant GM_xmlhttpRequest
@connect 127.0.0.1
```

`@connect` 必须与题库 URL 的 `HOST` 一致；改 `HOST` 后要同步更新脚本。

## 运行

```bash
uv run llm-ocs-bridge      # 默认 http://127.0.0.1:8009，Ctrl+C 停止
```

修改 `.env`、端口或令牌后，重新 `check` 并重新导入 JSON。SQLite 按单实例使用；升级无需清空数据库，内置缓存修订号自动隔离旧记录。

CLI：

```bash
uv run llm-ocs-bridge              # 启动服务
uv run llm-ocs-bridge check        # 检查配置并生成 OCS JSON
uv run llm-ocs-bridge clear-cache --scope search|answer|all   # 清缓存（保留指标与错误日志）
DEBUG=true uv run llm-ocs-bridge   # 开发 reload
```

## HTTP 接口

除注明外，业务接口需要 `X-Access-Token` 请求头。

| 接口 | 说明 |
| --- | --- |
| `GET /`、`GET /health` | 健康检查（无鉴权）；`/health` 返回 `{status, uptime, search_providers}` |
| `GET /stats` | 累计指标、缓存命中率、最近检索与错误（鉴权） |
| `GET/POST /search` | 答题入口（鉴权） |

`POST /search` 请求体为 JSON 对象：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `title`（或 `question`） | string | 题干，可含 `<img>`/`<audio>` URL 引用 |
| `options` | string 或数组 | 选项；OCS 传入换行分隔字符串 |
| `type`（或 `question_type`） | string | `single`/`multiple`/`judgement`/`completion` 或空 |
| `images` | string 数组 | base64 data URL（png/jpeg/gif/webp），受 `MAX_IMAGES`/`MAX_IMAGE_BYTES` 限制 |
| `suggestion_title` | string | OCS 5 建议题干，含 `[图片N]` 占位符 |
| `suggestion_options` | string 或数组 | OCS 5 建议选项 |

响应恒为 HTTP 200 的 OCS 契约：成功 `{"code":1,"question","answer"}`，失败 `{"code":0,"msg"}`。实时作答另带 `X-Search-Provider` 响应头（`exa`/`cache`/`llm-fallback`），答案缓存命中不带。

验证服务可用：

```bash
curl -sS http://127.0.0.1:8009/search \
  -H "X-Access-Token: <your-access-token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"1+1=2","type":"single","options":["对","错"]}'
```

成功时应返回 `code:1` 且 `answer` 为 `正确` 或 `错误`；`code:0` 时按 `msg` 对照故障排查。

## 返回格式

| 题型 | `answer` |
| --- | --- |
| `single` | 选项字母（按位置编号）；选项为对/错形态时返回 `正确`/`错误` |
| `multiple` | `#` 连接的选项字母 |
| `judgement` | `正确`/`错误` |
| `completion` | JSON 数组字符串，每空一个元素，保留分隔符 |
| 空题型 | 自由文本 |

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `.env` 不存在 | 在项目根目录 `cp .env.example .env && chmod 600 .env` |
| `.env` 权限错误 | `chmod 600 .env` |
| `配置项 X 缺失` | `.env` 缺少对应必填项，参照上表补齐 |
| `配置项 X 仍是示例占位符` | 把 `replace-with-*` 值替换为真实值 |
| HTTP 401 | 检查 `OPENAI_API_KEY`（或对应密钥项）是否有效 |
| HTTP 403 | 检查账号权限与配额 |
| HTTP 404 | 检查 `OPENAI_BASE_URL` 的 `/v1` 路径与 `OPENAI_MODEL` 可用性；三个 OpenAI 配置须属同一服务商 |
| HTTP 429 | 限流或配额不足，稍后重试 |
| `VISION_MODEL 未配置` / 图片题被拒 | 填写视觉模型；`MAX_REQUEST_BYTES` 改 32MB |
| `请求体超过大小限制` | 图片题需 `MAX_REQUEST_BYTES` 足够容纳 base64 图片（默认 32MB） |
| `图片占位符超出图片数量` | 客户端 `images` 数量与 `[图片N]` 引用不一致，检查 OCS 侧提交 |
| OCS 连不上题库 | 确认服务在运行、URL 与 `HOST:PORT` 一致、已重新导入 JSON、脚本 `@connect` 匹配 |
| 端口被占用 | 改 `.env` 的 `PORT`，重新 `check` 并导入 |
| 改 `ACCESS_TOKEN` 后失效 | 令牌写在题库配置里，重新 `check` 并导入 |

## 安全

- `.env` 保持 `0600`；`.env`、`ocs-config.json`、SQLite 文件不要提交或公开。
- 默认 `HOST=127.0.0.1`，除非明确需要局域网访问。
- 密钥泄露立即在服务商控制台撤销并轮换。
