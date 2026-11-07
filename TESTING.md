# OCS 接口验证

运行 `uv run pytest`。生成配置的 JavaScript handler 测试需要 PATH 中有 Node.js；缺少 Node 时该测试模块会明确跳过。

服务启动后使用实际访问令牌执行：

```bash
curl -sS http://127.0.0.1:8009/search \
  -H "X-Access-Token: ${ACCESS_TOKEN:?}" \
  -H 'Content-Type: application/json' \
  -d '{"title":"微软推出的以井号结尾的编程语言是____。","type":"completion","options":""}'
```

所有填空题成功响应的 `answer` 都应为 JSON 数组编码的字符串。若模型回答 `C#`，应返回字符串 `"[\"C#\"]"`，OCS 解码后得到单个答案 `C#`；普通多空答案也采用相同编码，例如 `"[\"北京\",\"上海\"]"`。

```bash
curl -sS http://127.0.0.1:8009/search \
  -H "X-Access-Token: ${ACCESS_TOKEN:?}" \
  -H 'Content-Type: application/json' \
  -d '{"title":"1+1=2","type":"single","options":["对","错"]}'
```

此类单选题成功响应返回 `正确` 或 `错误`；即使模型回显 `A`/`B`，也必须先转换成判定词。测试中使用受控模型响应验证转换，真实模型正确率不由接口保证。

图片题使用 OCS 5 的图片题协议，请求体包含 base64 图片，并按 `[图片N]` 编号对应：

```bash
curl -sS http://127.0.0.1:8009/search \
  -H "X-Access-Token: ${ACCESS_TOKEN:?}" \
  -H 'Content-Type: application/json' \
  -d '{"title":"图中是哪一项？","type":"single","options":["A.甲","B.乙"],"images":["data:image/png;base64,iVBORw0KGgo="],"suggestion_title":"图中是哪一项？ [图片1]","suggestion_options":"A.甲\nB.乙"}'
```

成功响应返回选项字母；缺少 `VISION_MODEL` 时返回 `图片题未配置视觉模型`。占位符编号超过 `images` 数量时返回 `图片占位符超出图片数量`。

`ANSWER_TIMEOUT_SECONDS` 默认 55 秒，接受任意有限正数，包括 60 秒以上。OCS 客户端请求超时默认 60 秒，可在客户端配置中调整；建议服务端预算小于客户端的实际超时，并为响应传输和资源清理留出余量。验证较长预算（例如 120 秒）时，应同时调大客户端超时并保留余量。浏览器中还应确认 OCS 用户脚本对题库 URL 主机具有 `@connect` 权限。

## 真实上游冒烟

`tests/e2e_live.py` 会真实调用 Exa 与模型服务并产生费用，需要显式确认：

```bash
uv run python tests/e2e_live.py --confirm-paid --output /tmp/ocs-live.json
```

脚本在 search 与 fallback 两种模式下各跑 7 个用例，覆盖单选、多选、判断、填空、URL 图片题，以及 OCS 5 base64 图片题的合成图与真实渲染图两种。每例先冷请求再热请求，校验响应、provider、上游调用次数与持久化计数；热请求必须命中缓存且不产生新的上游调用。全部请求共享 24 次出站尝试预算，超出即判失败。

只验证单个用例时加 `--case`，例如图片题：

```bash
uv run python tests/e2e_live.py --confirm-paid --case image-ocs5-color --output /tmp/ocs-live-image.json
```

图片用例有两个：

- `image-ocs5-color` 使用运行时生成的 64×64 纯红方块 PNG，题干问颜色、选项为红/绿/蓝，正确答案固定为 `A`。它只依赖代码，不需要外部素材，用来确认视觉模型真的看到了图片。
- `image-ocs5-calculus` 使用 [tests/fixtures/calculus-integral.png](tests/fixtures/calculus-integral.png)，这是渲染好的公式图（计算 `\int_{0}^{1} x^{2}\,dx`）。图片只含题干，选项以文本随请求发送，正确答案固定为 `B`（`1/3`）。它代表真实 OCS 题目形态：文字题干、公式图片、文本选项并存。

两者都证明服务端、base64 协议和 `VISION_MODEL` 全链路可用，但不代表真实课程截图（复杂图表、长公式）的识别正确率。

脚本还会跑一组不产生任何上游调用的校验用例，覆盖鉴权失败、非法 JSON、非 UTF-8、请求体超限、图片格式与数量与大小、`[图片N]` 越界、未知题型、单选多选缺选项、空题干，以及缺少 `VISION_MODEL`。每例都断言响应符合 OCS 契约（HTTP 200 且 `code` 为 0）、不携带 provider，且上游调用与 `search_requests`、`llm_calls` 计数均无变化。这些用例不消耗出站预算，因此可以单独反复运行。

脚本会清除 `*_proxy` 环境变量，因为部分代理变量（例如 `NODE_USE_ENV_PROXY`）会被 httpx 当作代理地址解析并导致启动失败。需要代理才能访问上游时，请在脚本外显式配置受支持的变量。
