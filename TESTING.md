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
