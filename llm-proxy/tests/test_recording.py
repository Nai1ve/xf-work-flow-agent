#!/usr/bin/env python3
"""离线验证 relay 能记录请求/响应且绝不泄漏 key（stdlib only，不触真实上游）。

起两个进程内 HTTP 服务：
  1) 假上游（MockHandler）——返回固定 OpenAI 补全，记录它收到的 Authorization 头；
  2) 被测 relay（RelayHandler，env 指向假上游 + 假 key）。

断言：
  - 转发成功：返回体与上游一致；
  - 日志记录：log 里恰好 1 条，model/messages/status/response 正确；
  - 不泄漏：日志文本不含 上游 key / 代理 key / Authorization 字样；
  - 打码：body 里 key 字段在日志中被 REDACTED；
  - 上游收到的 Authorization = Bearer <UPSTREAM_API_KEY>（证明带的是服务端 key）；
  - 鉴权：无/错误代理 key → 401；
  - 限流：超大 body → 413。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

RELAY_PATH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELAY_PATH))
import relay  # noqa: E402

MOCK_RESP = {"choices": [{"message": {"content": '{"ok":true,"proj":"布展升级印刷"}'}}]}
MOCK_AUTH: str | None = None


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        global MOCK_AUTH
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        MOCK_AUTH = self.headers.get("Authorization", "")
        payload = json.dumps(MOCK_RESP).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start(handler, port: int = 0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def reload_relay(env: dict) -> None:
    """用测试 env 重载 relay 模块（其配置全来自模块级环境变量）。"""
    for k in ("PROXY_API_KEY", "UPSTREAM_API_KEY", "UPSTREAM_BASE_URL", "LOG_DIR", "HOST", "PORT"):
        os.environ[k] = env.get(k, os.environ.get(k, ""))
    importlib.reload(relay)


def post(port: int, body: dict, auth: str | None) -> object:
    headers = {"Content-Type": "application/json"}
    if auth is not None:
        headers["Authorization"] = f"Bearer {auth}"
    req = Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    proxy_key = "test-proxy-key"
    upstream_key = "SUPER-SECRET-UPSTREAM-KEY"

    tmp = Path(tempfile.mkdtemp(prefix="relay_test_"))
    logfile = tmp / "calls.jsonl"

    mock = start(MockHandler)
    mport = mock.server_address[1]

    reload_relay(
        {
            "PROXY_API_KEY": proxy_key,
            "UPSTREAM_API_KEY": upstream_key,
            "UPSTREAM_BASE_URL": f"http://127.0.0.1:{mport}/v1",
            "LOG_DIR": str(tmp),
            "HOST": "127.0.0.1",
            "PORT": "0",
        }
    )
    rly = start(relay.RelayHandler)
    rport = rly.server_address[1]

    # 1) 正常转发
    body = {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": "你是排会助手。"},
            {"role": "user", "content": "订A1四楼6人", "api_key": "should-not-leak"},
        ],
        "temperature": 0,
    }
    out = post(rport, body, auth=proxy_key)
    assert out["choices"][0]["message"]["content"] == MOCK_RESP["choices"][0]["message"]["content"]

    # 2) 记录正确
    recs = [json.loads(line) for line in logfile.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == 1, f"expect 1 record, got {len(recs)}"
    r = recs[0]
    assert r["model"] == "gpt-test"
    assert r["status"] == 200
    assert r["messages"][0]["role"] == "system"
    assert r["messages"][1]["content"] == "订A1四楼6人"
    assert r["messages"][1]["api_key"] == "***REDACTED***", "body 内 key 字段应打码"
    assert r["response"] == json.dumps(MOCK_RESP)  # 与 MockHandler 的原始字节一致
    assert r["elapsed_s"] >= 0

    # 3) 不泄漏：日志无上游 key / 代理 key / Authorization
    logtxt = logfile.read_text(encoding="utf-8")
    for secret in (upstream_key, proxy_key, "Authorization", "sk-"):
        assert secret not in logtxt, f"日志泄漏了 {secret}!"

    # 4) 上游收到的 Authorization 是服务端 key
    assert MOCK_AUTH == f"Bearer {upstream_key}"

    # 5) 鉴权失败 → 401
    for bad in (None, "wrong-key"):
        try:
            post(rport, body, auth=bad)
            raise AssertionError(f"auth={bad!r} 应被拒")
        except HTTPError as e:
            assert e.code == 401, f"auth={bad!r} → {e.code}，期望 401"

    # 6) 超大 body → 413
    try:
        post(rport, {"messages": ["x" * (relay.MAX_BODY_BYTES + 1)]}, auth=proxy_key)
        raise AssertionError("超大 body 应被拒")
    except HTTPError as e:
        assert e.code == 413, f"超大 body → {e.code}，期望 413"

    print("ALL PASS — 记录正常，上游 key/代理 key/Authorization 均未入日志")
    print(f"logfile: {logfile}")


if __name__ == "__main__":
    main()
