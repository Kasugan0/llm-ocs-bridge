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
openssl rand -hex 32        # 生成 ACCESS_TOKEN
```

`.env` 最少配置：

```dotenv
ACCESS_TOKEN=刚才生成的随机令牌
OPENAI_API_KEY=你的模型服务密钥
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

其余配置项及默认值见 [.env.example](.env.example)。常用开关：

- `EXA_API_KEY` 留空且 `LLM_FALLBACK_ENABLED=true`：跳过检索直接作答，`check` 显示非致命警告。本项目未接入 Exa x402，留空不代表匿名搜索。
- `LLM_FALLBACK_ENABLED=false`：`EXA_API_KEY` 变为必填，检索失败时不直接作答。
- `ANSWER_TIMEOUT_SECONDS`：服务端答题总预算，默认 55s，应小于 OCS 客户端超时（默认 60s）。需要更长预算时两端同时调大。

### 图片题

```dotenv
VISION_MODEL=支持图片输入的模型名
MAX_REQUEST_BYTES=33554432
```

- `VISION_MODEL` 留空时图片题直接报错；文本模型不支持图片时填写也无效。
- OCS 单题最多 6 张图、单张解码后 3MB，base64 后上限约 24MB，32MB 留有余量。
- 旧 `.env` 常写着 `MAX_REQUEST_BYTES=262144`，会覆盖代码默认值导致图片题被拒，需手改后重启。`check` 会对这类不可用图片配置告警。

## 检查与导入

```bash
umask 077
uv run llm-ocs-bridge check > ocs-config.json
```

`check` 依次校验 `.env` 权限、全部配置、图片配置，并向 OpenAI（配置了 Exa 时也包括 Exa）发一次最小请求——会产生小额调用费用。状态与错误进 stderr，stdout 只含 JSON；退出码 `0` 成功、`1` 失败、`2` 参数错误。

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

修改 `.env`、端口或令牌后，重新 `check` 并重新导入 JSON。

SQLite 按单实例使用：不要让多个实例同时初始化同一个全新数据库。升级无需清空数据库，内置缓存修订号自动隔离旧记录。

## CLI

```bash
uv run llm-ocs-bridge check
uv run llm-ocs-bridge
uv run llm-ocs-bridge clear-cache --scope search|answer|all
DEBUG=true uv run llm-ocs-bridge    # 开发 reload
```

所有命令读取**执行时所在目录**的 `.env`，系统环境变量优先。`clear-cache` 只要求存储配置合法，保留累计指标与错误日志。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `.env` 不存在 | 在项目根目录 `cp .env.example .env && chmod 600 .env` |
| `.env` 权限错误 | `chmod 600 .env` |
| HTTP 401 | 检查 `OPENAI_API_KEY`（或对应密钥项）是否有效 |
| HTTP 403 | 检查账号权限与配额 |
| HTTP 404 | 检查 `OPENAI_BASE_URL` 的 `/v1` 路径与 `OPENAI_MODEL` 可用性；三个 OpenAI 配置须属同一服务商 |
| HTTP 429 | 限流或配额不足，稍后重试 |
| `VISION_MODEL 未配置` / 图片题被拒 | 填写视觉模型；`MAX_REQUEST_BYTES` 改 32MB |
| OCS 连不上题库 | 确认服务在运行、URL 与 `HOST:PORT` 一致、已重新导入 JSON、脚本 `@connect` 匹配 |
| 端口被占用 | 改 `.env` 的 `PORT`，重新 `check` 并导入 |
| 改 `ACCESS_TOKEN` 后失效 | 令牌写在题库配置里，重新 `check` 并导入 |

## 安全

- `.env` 保持 `0600`；`.env`、`ocs-config.json`、SQLite 文件不要提交或公开。
- 默认 `HOST=127.0.0.1`，除非明确需要局域网访问。
- 密钥泄露立即在服务商控制台撤销并轮换。
