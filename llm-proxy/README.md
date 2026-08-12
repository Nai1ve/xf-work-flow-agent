# llm-proxy — OpenAI 兼容 LLM 中转 + 全量抓取日志

把 `submission2` 的 LLM 请求经自己的服务器中转，**记录每个 case 的完整
prompt/response**，用于离线分析「为什么 LLM 选错 project / approver / 日期」
（LLM 方差是成绩反复的主痛点）。零依赖，纯 Python 标准库。

## 为什么 / 怎么做

- **为什么**：单 case 分差常来自 LLM 抽风（同样的 query 两次调用选不同房间/项目）。
  有了完整抓取，就能回放「当时喂了什么 prompt、模型回了什么」，而不是事后猜。
- **怎么做**：submission 端**零代码改动**——`llm_gateway.py` 的 `HttpBackend` 会 POST
  `{base_url}/chat/completions`。只需把 `submission2/config.json` 的
  `llm.base_url` 指向本服务（如 `http://127.0.0.1:8787/v1`），`provider` 保持
  `openai_compatible`，`llm.api_key` 改为代理 key（不再是真实上游 key）。

## 安全边界（勿破坏）

| 项目 | 做法 |
|---|---|
| 真实上游 key | 只在服务器进程内存（env `UPSTREAM_API_KEY`），**绝不**落日志/文件/仓库 |
| 代理自身鉴权 | 每次请求需 `Authorization: Bearer <PROXY_API_KEY>`，否则 401；`PROXY_API_KEY` 未设**拒绝启动** |
| 日志内容 | 只含 body（model/messages，key 字段打码）+ 响应文本；`Authorization` 头永不写入 |
| 监听 | 默认 `127.0.0.1`（只本机）；远程评估需改 `HOST` 并自行加 TLS / 更强鉴权 |
| body 上限 | `MAX_BODY_BYTES`（默认 512 KiB），防内存滥用 |
| 配置来源 | 全部来自本进程 env；**不读** submission 的 `config.local.json` / `config.json` |

## 使用

```bash
cd llm-proxy
cp .env.example .env        # 填入 PROXY_API_KEY / UPSTREAM_BASE_URL / UPSTREAM_API_KEY
set -a; source .env; set +a
python3 relay.py            # 起服务；日志写 logs/calls.jsonl
curl http://127.0.0.1:8787/healthz
```

每一条记录是一行 JSONL：

```json
{"ts": 1767..., "rid": "ab12cd34", "model": "gpt-5.4",
 "messages": [{"role":"system","content":"..."},{"role":"user","content":"..."}],
 "status": 200, "elapsed_s": 1.234, "error": null, "response": "{\"project\": ...}"}
```

## 离线测试（不触真上游）

```bash
python3 tests/test_recording.py
```

断言：转发成功、日志恰好 1 条且字段正确、**上游 key / 代理 key / Authorization 均不在日志**、
body 内 key 字段打码、上游收到的是服务端 key、错误鉴权 401、超大 body 413。
