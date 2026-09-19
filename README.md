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

## 快速开始

要求 Python 3.12 或更高版本，并使用 [uv](https://docs.astral.sh/uv/) 管理环境。

```bash
uv sync
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写 `ACCESS_TOKEN`、`OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。`EXA_API_KEY` 可选。

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
| 选项格式 | 换行字符串、`#` 分隔字符串、JSON 数组 |
| 模型服务 | OpenAI Chat Completions 兼容接口 |
| 搜索服务 | Exa，可选 |
| 缓存 | 本机 SQLite |
| 部署方式 | 单用户本机运行 |

空题型按开放式问题处理。单选和多选答案会根据实际选项标签校验，多选答案使用 `#` 连接。

## CLI

| 命令 | 用途 |
| --- | --- |
| `uv run llm-ocs-bridge` | 启动服务 |
| `uv run llm-ocs-bridge check` | 检查配置和外部服务，生成 OCS JSON |
| `uv run llm-ocs-bridge clear-cache --scope search` | 清理搜索缓存 |
| `uv run llm-ocs-bridge clear-cache --scope answer` | 清理答案缓存 |
| `uv run llm-ocs-bridge clear-cache --scope all` | 清理全部缓存 |
| `uv run llm-ocs-bridge --help` | 查看完整命令帮助 |

## 文档

- [安装、配置、OCS 接入与故障排查](DEPLOY.md)
- [完整环境变量及默认值](.env.example)
- [OCS 题库 API 文档](https://docs.ocsjs.com/docs/other/api)
- [Exa x402 文档](https://exa.ai/docs/integrations/payments/x402/quickstart)

## 安全说明

生成的 OCS JSON 包含真实的 `ACCESS_TOKEN`。不要公开或提交 `.env`、`ocs-config.json`、SQLite 数据库及其 WAL/SHM 文件。除非明确需要局域网访问，否则保持 `HOST=127.0.0.1`。

## 项目状态

当前版本面向个人本机使用，不提供公网部署、多用户隔离或远程管理能力。
