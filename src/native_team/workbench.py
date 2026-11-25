"""Local read-only browser UI; intentionally independent of native wake/control."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
import json
from pathlib import Path
import re
from socketserver import TCPServer, ThreadingMixIn
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .store import TeamError
from .workbench_data import SchemaError, directory, team_detail, record_page

try:
    ASSETS = files("native_team.web_assets")
except ModuleNotFoundError as error:
    if error.name != "native_team.web_assets":
        raise
    # Source runs and existing editable installs may predate the asset package.
    ASSETS = Path(__file__).resolve().parents[2] / "web" / "prototype"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/data-source.js": ("data-source.js", "text/javascript; charset=utf-8"),
    "/workflow.js": ("workflow.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/mark.svg": ("mark.svg", "image/svg+xml"),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class WorkbenchServer(ThreadingMixIn, HTTPServer):
    """Bound concurrent sockets so idle browser preconnects cannot serialize all reads."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def server_bind(self):
        # The advertised and accepted host is a loopback literal. Startup must
        # also work while the machine's hostname resolver is unavailable.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(state, port=0):
    state = Path(state).expanduser().resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "NativeTeamWorkbench"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            # Message bodies, team identifiers and query strings do not belong in access logs.
            pass

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass

        def do_GET(self):
            host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            target = urlsplit(self.path)
            if (
                self.headers.get("Host") != host
                or origin not in (None, f"http://{host}")
                or (
                    target.path.startswith("/api/")
                    and self.headers.get("Sec-Fetch-Site")
                    not in (None, "same-origin", "none")
                )
            ):
                self.respond(403, {"error": "此工作台仅允许本机页面读取。"})
                return
            try:
                if target.path == "/config.js":
                    self.respond(
                        200,
                        b'window.nativeTeamMode = "live";',
                        "text/javascript; charset=utf-8",
                    )
                elif target.path in FILES:
                    filename, kind = FILES[target.path]
                    self.respond(200, (ASSETS / filename).read_bytes(), kind)
                elif target.path == "/api/teams" and not target.query:
                    self.respond(200, directory(state))
                else:
                    match = re.fullmatch(
                        r"/api/teams/([a-zA-Z0-9-]{1,100})(?:/(tasks|activity|decisions))?",
                        target.path,
                    )
                    if not match:
                        raise LookupError("未找到此页面。")
                    identity, section = match.groups()
                    if len(target.query) > 1024:
                        raise ValueError("Query is too long")
                    query = parse_qs(
                        target.query, keep_blank_values=True, max_num_fields=2
                    )
                    if (
                        any(key != "cursor" for key in query)
                        or len(query.get("cursor", [])) > 1
                    ):
                        raise ValueError("Invalid query")
                    if section is None:
                        if query:
                            raise ValueError("Unexpected query")
                        result = team_detail(state, identity)
                    else:
                        result = record_page(
                            state, identity, section, query.get("cursor", [None])[0]
                        )
                    self.respond(200, result)
            except LookupError as error:
                self.respond(404, {"error": str(error)})
            except ValueError as error:
                self.respond(400, {"error": str(error)})
            except SchemaError as error:
                self.respond(503, {"error": str(error)})
            except (TeamError, OSError):
                self.respond(
                    503,
                    {
                        "error": "暂时无法读取本机团队记录；请检查记录权限和 Native Team 版本，然后刷新。"
                    },
                )

        def reject_write(self):
            self.respond(405, {"error": "工作台只提供读取，不支持此操作。"})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = reject_write

    return WorkbenchServer(("127.0.0.1", port), Handler)


def serve(state, port=0, open_browser=True):
    with make_server(state, port) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Native Team 工作台：{url}", flush=True)
        print(
            "只读本机共享记录；不唤醒成员。保留此终端，按 Ctrl+C 关闭工作台服务。",
            flush=True,
        )
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
