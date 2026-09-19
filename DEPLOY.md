# 安装、配置与 OCS 接入

本文说明如何在本机安装 `llm-ocs-bridge`、配置外部服务、接入 OCS，以及处理常见运行问题。

## 使用前准备

你需要：

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- OpenAI 兼容服务的 API 密钥
- 已安装并可正常打开的 OCS 用户脚本
- Exa API 密钥（可选）

## 安装本项目

```
git clone https://github.com/Kasugan0/llm-ocs-bridge.git
```

进入项目目录并安装依赖：

```bash
cd llm-ocs-bridge
uv sync
```

## 创建配置文件

复制配置模板，并收紧文件权限：

```bash
cp .env.example .env
chmod 600 .env
```

生成一个随机的 OCS 访问令牌：

```bash
openssl rand -hex 32
```

编辑 `.env`，至少填写以下配置：

```dotenv
ACCESS_TOKEN=刚才生成的随机令牌
OPENAI_API_KEY=你的模型服务密钥
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

如果使用其他 OpenAI 兼容服务，请按服务商文档修改 `OPENAI_BASE_URL` 和 `OPENAI_MODEL`。

### 是否需要 Exa API 密钥

`EXA_API_KEY` 默认可以留空：

```dotenv
EXA_API_KEY=
LLM_FALLBACK_ENABLED=true
```

此时服务会跳过 Exa，直接使用 LLM 作答。`check` 会显示警告，但不会判定配置失败。

填写有效的 `EXA_API_KEY` 后，服务会优先搜索资料，并让 LLM 根据搜索证据作答。虽然 Exa 官方支持通过 x402 进行无 API Key 的按次付费请求，但本项目没有接入 x402；留空密钥代表不调用 Exa，不代表免费匿名搜索。

如果设置：

```dotenv
LLM_FALLBACK_ENABLED=false
```

则 `EXA_API_KEY` 变为必填项。Exa 不可用或没有有效搜索结果时，服务不会让 LLM 直接作答。

其他配置通常保持 [.env.example](.env.example) 中的默认值即可。

## 检查配置

运行：

```bash
uv run llm-ocs-bridge check
```

该命令会：

1. 检查 `.env` 是否存在、可读且权限正确。
2. 检查所有配置值和缓存目录。
3. 向 OpenAI 兼容服务发送一次最小请求。
4. 配置了 Exa 时，向 Exa 发送一次最小搜索请求。
5. 全部通过后生成可直接导入 OCS 的题库配置。

检查会产生少量模型调用费用；配置了 Exa 时，也会消耗一次 Exa 调用额度。

建议将生成的 OCS 配置写入临时文件：

```bash
umask 077
uv run llm-ocs-bridge check > ocs-config.json
```

检查进度和错误仍会显示在终端中，`ocs-config.json` 只包含完整 JSON。检查成功时退出码为 `0`，检查失败时为 `1`，命令参数错误时为 `2`。

生成的 JSON 包含真实的 `ACCESS_TOKEN`，不要公开、发送给他人或提交到版本库。

## 导入 OCS

1. 打开 `ocs-config.json`，复制其中的完整 JSON。
2. 打开 OCS 脚本面板。
3. 进入「通用 → 全局设置 → 题库配置」。
4. 将 JSON 粘贴到题库配置中并保存。
5. 保持该题库启用。

生成的配置已经包含请求地址、请求方法、题型转换、选项转换和 `X-Access-Token`，无需手工修改。

OCS 通过 `GM_xmlhttpRequest` 访问本机 HTTP 服务。如果浏览器提示没有权限，请确认当前 OCS 用户脚本包含：

```text
@grant GM_xmlhttpRequest
@connect 127.0.0.1
```

## 启动和停止

运行：

```bash
uv run llm-ocs-bridge
```

看到服务启动信息后保持终端窗口开启，然后正常使用 OCS。默认服务地址为：

```text
http://127.0.0.1:8009
```

停止服务时，在运行服务的终端中按 `Ctrl+C`。

以后日常使用只需先启动服务，再打开网课页面和 OCS。只有修改 `.env`、端口或访问令牌后，才需要重新运行 `check` 并将新 JSON 导入 OCS。

## CLI 命令

```bash
# 查看帮助
uv run llm-ocs-bridge --help

# 检查配置并生成 OCS 配置
uv run llm-ocs-bridge check

# 启动服务
uv run llm-ocs-bridge

# 清理缓存
uv run llm-ocs-bridge clear-cache --scope search
uv run llm-ocs-bridge clear-cache --scope answer
uv run llm-ocs-bridge clear-cache --scope all
```

清理缓存不会修改 `.env` 或 OCS 配置。

## 常见问题

### `check` 提示 `.env` 不存在

确认命令在项目根目录中执行，并重新创建配置文件：

```bash
cp .env.example .env
chmod 600 .env
```

### `check` 提示 `.env` 权限错误

运行：

```bash
chmod 600 .env
```

### `check` 提示 OpenAI 鉴权失败

检查 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL` 是否属于同一个服务商及账号。修改后重新运行 `check`。

### `check` 警告没有配置 Exa

这是非致命警告。启用 `LLM_FALLBACK_ENABLED=true` 时可以继续使用，但答案不会经过搜索证据增强。如需搜索能力，请填写有效的 `EXA_API_KEY` 后重新检查。

### OCS 无法连接本机题库

依次确认：

1. `uv run llm-ocs-bridge` 仍在运行。
2. OCS 配置中的地址与当前 `HOST`、`PORT` 一致。
3. 修改端口后已经重新运行 `check` 并重新导入 JSON。
4. OCS 用户脚本具有 `GM_xmlhttpRequest` 和 `@connect 127.0.0.1` 权限。
5. 防火墙或安全软件没有阻止浏览器访问 localhost。

### 启动时提示端口已被占用

修改 `.env` 中的 `PORT`，例如：

```dotenv
PORT=8010
```

然后重新运行 `check`、重新导入生成的 OCS JSON，再启动服务。

### 修改 `ACCESS_TOKEN` 后 OCS 无法使用

访问令牌已经写入 OCS 题库配置。修改令牌后必须重新运行 `check`，并用新生成的 JSON 替换 OCS 中的旧配置。

### 不再需要 `ocs-config.json`

导入 OCS 后可以删除该文件。它包含访问令牌，不应长期存放在共享目录中。

## 安全注意事项

- 不要公开 `.env` 或 `ocs-config.json`。
- 不要把 API 密钥、访问令牌或生成的 OCS JSON 提交到 Git。
- 保持 `.env` 权限为 `0600`。
- 默认保持 `HOST=127.0.0.1`，除非你清楚开放到局域网的风险。
- API 密钥泄露后，应立即在对应服务商控制台撤销并重新生成。

