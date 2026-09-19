"""老红橘保护性收购与古树管护兑现的服务入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api import Api
from domain import DomainError

SERVICE_ID = "red-orange-procurement"
SERVICE_NAME = "老红橘保护性收购"

API = Api()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 管护兑现领域接口。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        if method == "GET" and self.path == "/health":
            self._respond(200, health_payload())
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            status, payload = API.handle(
                method, self.path.split("?", 1)[0], self.headers, raw
            )
        except DomainError as error:
            status, payload = error.status, {"error": error.message}
        except Exception:
            status, payload = 500, {"error": "服务内部错误"}
        self._respond(status, payload)

    def _respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert API.engine is not None
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
