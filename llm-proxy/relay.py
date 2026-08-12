#!/usr/bin/env python3
"""OpenAI-compatible LLM relay with request/response logging (stdlib only).

为什么：
- 把 submission 的 LLM 请求经自己的服务器中转，记录每个 case 的完整 prompt/response，
  用于离线分析「为什么 LLM 选错 project / approver / 日期」（LLM 方差是主要痛点）；
- submission 端**零代码改动**：只需把 config 的 llm.base_url 指向本服务（如
  http://127.0.0.1:8787/v1），provider 保持 openai_compatible（见 llm_gateway.py）。

兼容性：
- submission 的 HttpBackend 会 POST `{base_url}/chat/completions`，body 为
  `{model, messages, temperature, max_tokens, response_format}`，返回
  `choices[0].message.content`。本服务原样转发 + 原样返回，两个路径都认。

Security（务必遵守，勿破坏）：
- 真实上游 key 只在服务器进程内存（env UPSTREAM_API_KEY），绝不落日志/文件/仓库；
- 代理自身要求 Bearer 鉴权（env PROXY_API_KEY，强随机值），不匹配 → 401，
  **拒绝启动**当 PROXY_API_KEY 未设置（防公网免费中转）；
- Authorization 头永不写入日志；日志只含 body（model/messages，key 字段打码）与响应；
- 默认绑定 127.0.0.1（本地抓取）；远程评估需改 HOST 并自行加 TLS / 更强鉴权；
- 请求体大小上限 MAX_BODY_BYTES，防内存滥用；
- 不读 submission 的 config.local.json / config.json——上游配置全走本进程 env。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

# ---------------------------------------------------------------- 配置 ----
# 全部来自环境变量；缺 PROXY_API_KEY / UPSTREAM_BASE_URL 直接拒绝启动。
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")
UPSTREAM_BASE_URL = os.environ.get("UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_API_KEY = os.environ.get("UPSTREAM_API_KEY", "")
LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
UPSTREAM_TIMEOUT_S = float(os.environ.get("UPSTREAM_TIMEOUT_S", "60"))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "524288"))

LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "calls.jsonl"

# 转发路径：submission HttpBackend 会 POST {base_url}/chat/completions。
_CHAT_PATHS = {"/chat/completions", "/v1/chat/completions"}

# body 里形似密钥的顶层字段，记录时打码（防御性；submission 正常不传）。
_SECRET_KEYS = ("api_key", "authorization", "token", "secret", "key")


def _redact_keys(obj: object) -> object:
    """递归打码 dict 键中含 secret 词的值（仅用于日志副本，不影响转发）。"""
    if isinstance(obj, dict):
        return {
            k: ("***REDACTED***" if any(s in str(k).lower() for s in _SECRET_KEYS) else _redact_keys(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_keys(v) for v in obj]
    return obj


def _log_record(rec: dict) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class RelayHandler(BaseHTTPRequestHandler):
    # 关闭默认访问日志（避免把 Authorization 或 body 打到 stderr）。
    def log_message(self, *_: object) -> None:  # noqa: D401
        pass

    def _send_json(self, code: int, obj: object) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return bool(PROXY_API_KEY) and self.headers.get("Authorization", "") == f"Bearer {PROXY_API_KEY}"

    def _read_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": {"message": "body too large"}})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except Exception:  # noqa: BLE001 —— 非 JSON 一律 400
            self._send_json(400, {"error": {"message": "invalid json"}})
            return None

    def do_GET(self) -> None:  # noqa: N802 —— http.server 方法名约定
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in _CHAT_PATHS:
            self._send_json(404, {"error": {"message": "not found"}})
            return
        if not self._authorized():
            self._send_json(401, {"error": {"message": "unauthorized"}})
            return
        body = self._read_body()
        if body is None:
            return

        rid = uuid.uuid4().hex[:8]
        ts = time.time()
        model = body.get("model")
        messages = body.get("messages")

        # 转发到真实供应商（真实 key 仅在内存）。
        req = UrlRequest(
            f"{UPSTREAM_BASE_URL}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {UPSTREAM_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        status = 502
        resp_text = ""
        err: str | None = None
        t0 = time.monotonic()
        try:
            with urlopen(req, timeout=UPSTREAM_TIMEOUT_S) as resp:  # noqa: S310 —— 本服务即中转，URL 由运维 env 配置
                status = resp.status
                resp_text = resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:  # 上游返回 4xx/5xx（含 400 json_object 拒绝）——原样透传
            status = e.code
            resp_text = e.read().decode("utf-8", errors="replace")
            err = f"upstream HTTP {e.code}"
        except (URLError, TimeoutError, OSError) as e:
            status = 502
            err = f"upstream {type(e).__name__}"
        elapsed_s = round(time.monotonic() - t0, 3)

        # 记录（打码副本；绝不含 Authorization / key）。
        _log_record(
            {
                "ts": round(ts, 3),
                "rid": rid,
                "model": model,
                "messages": _redact_keys(messages),
                "status": status,
                "elapsed_s": elapsed_s,
                "error": err,
                "response": resp_text,
            }
        )

        # 原样返回上游（含 400/500 的 JSON；网络错误时补一个 JSON 错误体）。
        if resp_text:
            self._send_raw(status, resp_text)
        else:
            self._send_json(status, {"error": {"message": err or "upstream empty"}})


def main() -> None:
    if not PROXY_API_KEY:
        print("REFUSE: PROXY_API_KEY 未设置。拒绝启动，避免公网免费中转。", file=sys.stderr)
        sys.exit(1)
    if not UPSTREAM_BASE_URL:
        print("REFUSE: UPSTREAM_BASE_URL 未设置。", file=sys.stderr)
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), RelayHandler)  # noqa: S103 —— HOST 由 env 控制（默认 127.0.0.1）
    print(f"relay  listening on http://{HOST}:{PORT}  log={LOG_FILE}", flush=True)
    print(f"  upstream={UPSTREAM_BASE_URL}  auth=on  (upstream key 只在内存，不入日志)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
