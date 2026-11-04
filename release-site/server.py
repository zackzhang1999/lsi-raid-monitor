#!/usr/bin/env python3
"""DataMax-LSI release site and package download server."""
from __future__ import annotations

import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

ROOT = Path(os.environ.get("DATAMAX_RELEASE_ROOT", "/opt/lsi-full")).resolve()
STATIC = Path(os.environ.get("DATAMAX_RELEASE_STATIC", ROOT / "site")).resolve()
PORT = int(os.environ.get("DATAMAX_RELEASE_PORT", "9004"))
HOST = os.environ.get("DATAMAX_RELEASE_HOST", "0.0.0.0")
SERVICE = "datamax-lsi-release"
VERSION = "1.0.0"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest() -> dict:
    path = ROOT / "releases.json"
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            return {"latest": "", "releases": []}
        releases = data.get("releases", [])
        if not isinstance(releases, list):
            releases = []
        for release in releases:
            artifact = ROOT / str(release.get("file", ""))
            if artifact.is_file():
                release["size"] = artifact.stat().st_size
                if not release.get("sha256"):
                    release["sha256"] = sha256(artifact)
        data["releases"] = releases
        return data
    except (OSError, ValueError, TypeError):
        return {"latest": "", "releases": []}


class Handler(BaseHTTPRequestHandler):
    server_version = f"DataMaxLSIRelease/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.log_date_time_string()} {fmt % args}", flush=True)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    def json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def file(self, path: Path, attachment: bool = False) -> None:
        if not path.is_file():
            self.json(404, {"error": "not found"})
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
            ".woff2": "font/woff2",
            ".pdf": "application/pdf",
            ".md": "text/markdown; charset=utf-8",
            ".tar": "application/x-tar",
            ".gz": "application/gzip",
        }.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        if path.suffix.lower() in {".html", ".css", ".js", ".json"}:
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=3600")
        if attachment:
            encoded = quote(path.name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.json(200, {
                "status": "ok",
                "service": SERVICE,
                "version": manifest().get("latest", ""),
            })
            return
        if parsed.path == "/api/releases":
            self.json(200, manifest())
            return
        if parsed.path.startswith("/download/"):
            name = unquote(parsed.path[len("/download/"):])
            if not name or "/" in name or "\\" in name or name.startswith("."):
                self.json(400, {"error": "invalid file"})
                return
            self.file(ROOT / name, attachment=True)
            return

        relative = unquote(parsed.path).lstrip("/") or "index.html"
        target = (STATIC / relative).resolve()
        try:
            target.relative_to(STATIC)
        except ValueError:
            self.json(403, {"error": "forbidden"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() and "." not in Path(relative).name:
            target = STATIC / "index.html"
        self.file(target)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        self.json(405, {"error": "method not allowed"})

    def do_PUT(self) -> None:
        self.json(405, {"error": "method not allowed"})

    def do_DELETE(self) -> None:
        self.json(405, {"error": "method not allowed"})


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"{SERVICE} listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
