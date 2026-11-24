"""Editör prototipi için yerel HTTP API (sadece 127.0.0.1). Bağımlılık yok: http.server.

GET  /                      editor/index.html
GET  /api/catalog           katalog + tüm tanılar
GET  /api/doc?id=           ham + çözülmüş + efektif görünüm
POST /api/check   {id, raw} taslağı kaydetmeden doğrula, efektif görünümü döndür
POST /api/save    {id, raw} dosyaya yaz (dizi dosyalarında sadece ilgili eleman), doğrula
POST /api/derive  {base, newId, name?, abstract?}
POST /api/compile {scenario}
"""
from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .checker import Scope
from .compiler import Builder
from .diagnostics import Diagnostics
from .editor import build_catalog, derive
from .validate import Validator
from .workspace import Workspace

EDITOR_DIR = Path(__file__).resolve().parents[2] / "editor"
_LOCK = threading.Lock()


class Api:
    def __init__(self, root: Path) -> None:
        # Sembolik bağlantılar çözülmeli (macOS: /var -> /private/var), yoksa yol güvenliği kontrolü yanlış reddeder.
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ yardımcılar
    def _load(self, overrides: dict | None = None) -> tuple[Workspace, Diagnostics]:
        d = Diagnostics()
        ws = Workspace(self.root, d, overrides=overrides).load()
        return ws, d

    def _validated(self, overrides: dict | None = None) -> tuple[Workspace, Diagnostics]:
        ws, d = self._load(overrides)
        Validator(ws, d).run()
        return ws, d

    @staticmethod
    def _diags(d: Diagnostics) -> list[dict]:
        return [x.to_dict() for x in d.sorted()]

    def _location(self, ws: Workspace, doc_id: str) -> tuple[str, int | None]:
        doc = ws.get(doc_id)
        if doc is None:
            raise KeyError(f"'{doc_id}' bulunamadı")
        return doc.file.as_posix(), doc.index

    def view(self, ws: Workspace, doc_id: str) -> dict:
        doc = ws.get(doc_id)
        r = ws.resolve(doc_id) if doc else None
        if doc is None or r is None:
            return {"id": doc_id, "missing": True}
        return {
            "id": doc_id,
            "kind": doc.kind,
            "file": doc.file.as_posix(),
            "index": doc.index,
            "raw": doc.raw,
            "resolved": r.as_document(),
            "lineage": r.linear,
            "provenance": r.provenance,
            "children": sorted(ws.children().get(doc_id, [])),
            "effective": self._effective(ws, r),
        }

    def _effective(self, ws: Workspace, r) -> dict | None:
        """Editörün gösterdiği 'efektif' değerler: varsayılanlar uygulanmış, birimler normalize edilmiş."""
        scratch = Diagnostics()
        b = Builder(ws, scratch)
        try:
            if r.kind == "SystemTemplate":
                st = ws.resolve(r.spec.get("systemType", ""))
                if not st:
                    return None
                p = b.ck.values(st.spec.get("parameters"), r.spec.get("parameters"), "p", Scope(), check_required=False)
                s = b.ck.values(st.spec.get("state"), r.spec.get("initialState"), "s", Scope(), sources={"parameters": p}, check_required=False)
                return {"parameters": p, "initialState": s}
            if r.kind == "Munition":
                mt = ws.resolve(r.spec.get("munitionType", ""))
                return {"parameters": b.ck.values(mt.spec.get("parameters"), r.spec.get("parameters"), "p", Scope(), check_required=False)} if mt else None
            if r.kind == "PlatformTemplate":
                return b.build_platform(r.id, "spec", level="template", require_complete=False)
        except Exception as e:  # editör görünümü asla çökmemeli
            return {"error": str(e)}
        return None

    # ------------------------------------------------------------------ uç noktalar
    def catalog(self) -> dict:
        ws, d = self._validated()
        return {"catalog": build_catalog(ws), "diagnostics": self._diags(d)}

    def doc(self, doc_id: str) -> dict:
        ws, _ = self._load()
        return self.view(ws, doc_id)

    def check(self, doc_id: str, raw: dict) -> dict:
        base, _ = self._load()
        loc = self._location(base, doc_id)
        ws, d = self._validated({loc: raw})
        new_id = raw.get("id", doc_id) if isinstance(raw, dict) else doc_id
        return {"view": self.view(ws, new_id), "diagnostics": self._diags(d)}

    def save(self, doc_id: str, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("doküman bir JSON nesnesi olmalı")
        with _LOCK:
            base, _ = self._load()
            file, index = self._location(base, doc_id)
            path = (self.root / file).resolve()
            if not path.is_relative_to(self.root):
                raise PermissionError("çalışma alanı dışına yazılamaz")
            new_id = raw.get("id", doc_id)
            if new_id != doc_id and new_id in base.docs:
                raise ValueError(f"'{new_id}' zaten var")
            if index is None:
                data: Any = raw
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
                data[index] = raw
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        ws, d = self._validated()
        return {"saved": file, "view": self.view(ws, new_id), "diagnostics": self._diags(d)}

    def derive(self, base_id: str, new_id: str, name: str | None, abstract: bool) -> dict:
        with _LOCK:
            ws, d = self._load()
            p = derive(ws, d, base_id, new_id, name=name, abstract=abstract)
        if p is None:
            return {"ok": False, "diagnostics": self._diags(d)}
        return {"ok": True, "id": new_id, "file": ws.rel(p).as_posix()}

    def compile(self, scenario: str) -> dict:
        ws, d = self._load()
        out = Builder(ws, d).compile_scenario(scenario)
        return {"compiled": out, "diagnostics": self._diags(d)}


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "simcfg-editor/0.1"

        def log_message(self, fmt, *args):  # sessiz
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _guard(self, fn):
            try:
                self._json(fn())
            except KeyError as e:
                self._json({"error": str(e)}, HTTPStatus.NOT_FOUND)
            except (ValueError, PermissionError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except Exception as e:  # pragma: no cover
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/catalog":
                return self._guard(api.catalog)
            if u.path == "/api/doc":
                q = parse_qs(u.query)
                return self._guard(lambda: api.doc(q.get("id", [""])[0]))
            name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (EDITOR_DIR / name).resolve()
            if not f.is_relative_to(EDITOR_DIR) or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError as e:
                return self._json({"error": f"geçersiz JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            p = urlparse(self.path).path
            routes = {
                "/api/check": lambda: api.check(body["id"], body["raw"]),
                "/api/save": lambda: api.save(body["id"], body["raw"]),
                "/api/derive": lambda: api.derive(body["base"], body["newId"], body.get("name"), bool(body.get("abstract"))),
                "/api/compile": lambda: api.compile(body["scenario"]),
            }
            if p not in routes:
                return self._json({"error": "bilinmeyen uç nokta"}, HTTPStatus.NOT_FOUND)
            self._guard(routes[p])

    return Handler


def serve(root: Path, port: int = 8765) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Api(Path(root).resolve())))
    print(f"simcfg editör: http://127.0.0.1:{port}  (çıkmak için Ctrl+C)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
