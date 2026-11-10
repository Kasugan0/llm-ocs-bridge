# llm-ocs-bridge

本机运行的 OCS 搜题桥接服务：把 OCS 题目转换为可选的 Exa 检索加 OpenAI 兼容模型作答，再把结果转回 OCS 答案格式。面向单用户，默认只监听 `127.0.0.1`。

OCS 的题库协议是 HTTP 请求加 JavaScript handler，不能直接对接 LLM API；`check` 命令会生成可直接导入的 `AnswererWrapper` 配置，无需手写。

## 快速开始

需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
cp .env.example .env && chmod 600 .env
```

在 `.env` 中填 `ACCESS_TOKEN`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`，然后：

```bash
umask 077
uv run llm-ocs-bridge check > ocs-config.json
uv run llm-ocs-bridge
```

把 `ocs-config.json` 导入 OCS「通用 → 全局设置 → 题库配置」，服务默认在 `http://127.0.0.1:8009` 运行。完整步骤、可选配置与故障排查见 [DEPLOY.md](DEPLOY.md)。

## 支持范围

| 项目 | 支持情况 |
| --- | --- |
| 题型 | `single`、`multiple`、`judgement`、`completion`、空题型 |
| 图片题 | OCS 5 `images`/`suggestion_*` 协议，需 `VISION_MODEL` |
| 模型 | OpenAI Chat Completions 兼容接口 |
| 检索 | Exa，可选；失败时按 `LLM_FALLBACK_ENABLED` 决定是否直接作答 |
| 缓存 | 本机 SQLite，容量有界 |

## 文档

- [安装、配置、OCS 接入与故障排查](DEPLOY.md)
- [完整环境变量及默认值](.env.example)
- [OCS 题库 API 文档](https://docs.ocsjs.com/docs/other/api)

## 许可证

[MIT](LICENSE)
