#!/usr/bin/env python3
"""A deliberately tiny, jailed file API for one Home Assistant local app."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOST = "0.0.0.0"
PORT = 8099
ROOT = Path("/local_apps/aula_assistant")
MAX_BODY = 16 * 1024 * 1024
MAX_READ = 16 * 1024 * 1024
MAX_LIST = 5000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _root() -> Path:
    if not ROOT.exists() or not ROOT.is_dir():
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"Project root is unavailable: {ROOT}")
    if ROOT.is_symlink():
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Project root must not be a symlink")
    return ROOT.resolve(strict=True)


def _relative_parts(raw: str | None) -> list[str]:
    if raw is None:
        raw = "."
    if not isinstance(raw, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, "path must be a string")
    if "\x00" in raw:
        raise ApiError(HTTPStatus.BAD_REQUEST, "NUL is not allowed in path")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ApiError(HTTPStatus.FORBIDDEN, "Absolute paths are not allowed")
    parts = [p for p in candidate.parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ApiError(HTTPStatus.FORBIDDEN, "Parent traversal is not allowed")
    return parts


def _jailed(raw: str | None, *, must_exist: bool = False, allow_root: bool = True) -> Path:
    root = _root()
    parts = _relative_parts(raw)
    if not parts and not allow_root:
        raise ApiError(HTTPStatus.FORBIDDEN, "Operation on project root is not allowed")

    current = root
    for part in parts:
        current = current / part
        if os.path.lexists(current) and os.path.islink(current):
            raise ApiError(HTTPStatus.FORBIDDEN, "Symlinks are not allowed")

    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ApiError(HTTPStatus.FORBIDDEN, "Path escapes Aula project")

    if must_exist and not os.path.lexists(resolved):
        raise ApiError(HTTPStatus.NOT_FOUND, "Path does not exist")
    return resolved


def _rel(path: Path) -> str:
    root = _root()
    return "." if path == root else path.relative_to(root).as_posix()


def _info(path: Path) -> dict:
    st = path.stat()
    if path.is_dir():
        kind = "directory"
        size = None
    elif path.is_file():
        kind = "file"
        size = st.st_size
    else:
        kind = "other"
        size = st.st_size
    return {
        "path": _rel(path),
        "name": path.name if path != _root() else ".",
        "type": kind,
        "size": size,
        "mode": format(stat.S_IMODE(st.st_mode), "04o"),
        "mtime_ns": st.st_mtime_ns,
    }


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length_raw = handler.headers.get("Content-Length", "0")
    try:
        length = int(length_raw)
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
    if length < 0 or length > MAX_BODY:
        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
    body = handler.rfile.read(length)
    if not body:
        return {}
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Body must be valid JSON")
    if not isinstance(value, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "AulaProjectBridge/0.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _send(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, payload: dict | list) -> None:
        self._send(HTTPStatus.OK, payload)

    def _dispatch(self) -> None:
        try:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            route = parsed.path.rstrip("/") or "/"

            if self.command == "GET" and route == "/health":
                root = _root()
                self._ok({"ok": True, "root": str(root), "project": "aula_assistant", "version": "0.1.0"})
                return

            if self.command == "GET" and route == "/stat":
                path = _jailed(query.get("path", ["."])[0], must_exist=True)
                self._ok({"ok": True, "entry": _info(path)})
                return

            if self.command == "GET" and route == "/files":
                path = _jailed(query.get("path", ["."])[0], must_exist=True)
                if not path.is_dir():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "path is not a directory")
                entries = []
                for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
                    if child.is_symlink():
                        entries.append({"path": _rel(child), "name": child.name, "type": "symlink", "blocked": True})
                    else:
                        entries.append(_info(child))
                    if len(entries) >= MAX_LIST:
                        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Directory contains too many entries")
                self._ok({"ok": True, "path": _rel(path), "entries": entries})
                return

            if self.command == "GET" and route == "/file":
                path = _jailed(query.get("path", [None])[0], must_exist=True, allow_root=False)
                if not path.is_file():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "path is not a file")
                size = path.stat().st_size
                if size > MAX_READ:
                    raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File too large")
                data = path.read_bytes()
                encoding = query.get("encoding", ["utf-8"])[0]
                digest = hashlib.sha256(data).hexdigest()
                if encoding == "base64":
                    content = base64.b64encode(data).decode("ascii")
                elif encoding == "utf-8":
                    try:
                        content = data.decode("utf-8")
                    except UnicodeDecodeError:
                        raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "File is not UTF-8; request encoding=base64")
                else:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "encoding must be utf-8 or base64")
                self._ok({"ok": True, "path": _rel(path), "encoding": encoding, "sha256": digest, "content": content})
                return

            if self.command == "PUT" and route == "/file":
                body = _read_json(self)
                path = _jailed(body.get("path"), allow_root=False)
                if path.exists() and path.is_dir():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Target is a directory")
                if "content" in body and "content_base64" in body:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Use content or content_base64, not both")
                if "content_base64" in body:
                    try:
                        data = base64.b64decode(body["content_base64"], validate=True)
                    except Exception:
                        raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid base64 content")
                else:
                    content = body.get("content", "")
                    if not isinstance(content, str):
                        raise ApiError(HTTPStatus.BAD_REQUEST, "content must be a string")
                    data = content.encode("utf-8")
                if len(data) > MAX_BODY:
                    raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File too large")
                path.parent.mkdir(parents=True, exist_ok=True)
                _jailed(_rel(path.parent), must_exist=True)
                fd, tmp_name = tempfile.mkstemp(prefix=".aula-bridge-", dir=str(path.parent))
                try:
                    with os.fdopen(fd, "wb") as tmp:
                        tmp.write(data)
                        tmp.flush()
                        os.fsync(tmp.fileno())
                    if path.exists():
                        os.chmod(tmp_name, stat.S_IMODE(path.stat().st_mode))
                    else:
                        os.chmod(tmp_name, 0o644)
                    os.replace(tmp_name, path)
                finally:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                self._ok({"ok": True, "entry": _info(path), "sha256": hashlib.sha256(data).hexdigest()})
                return

            if self.command == "POST" and route == "/mkdir":
                body = _read_json(self)
                path = _jailed(body.get("path"), allow_root=False)
                path.mkdir(parents=bool(body.get("parents", True)), exist_ok=bool(body.get("exist_ok", True)))
                self._ok({"ok": True, "entry": _info(path)})
                return

            if self.command == "POST" and route == "/rename":
                body = _read_json(self)
                src = _jailed(body.get("from"), must_exist=True, allow_root=False)
                dst = _jailed(body.get("to"), allow_root=False)
                if dst.exists() and not bool(body.get("overwrite", False)):
                    raise ApiError(HTTPStatus.CONFLICT, "Destination already exists")
                dst.parent.mkdir(parents=True, exist_ok=True)
                _jailed(_rel(dst.parent), must_exist=True)
                if dst.exists():
                    if dst.is_dir():
                        shutil.rmtree(dst)
                    else:
                        dst.unlink()
                os.replace(src, dst)
                self._ok({"ok": True, "entry": _info(dst)})
                return

            if self.command == "POST" and route == "/copy":
                body = _read_json(self)
                src = _jailed(body.get("from"), must_exist=True, allow_root=False)
                dst = _jailed(body.get("to"), allow_root=False)
                if dst.exists() and not bool(body.get("overwrite", False)):
                    raise ApiError(HTTPStatus.CONFLICT, "Destination already exists")
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=bool(body.get("overwrite", False)))
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                else:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Only regular files and directories can be copied")
                self._ok({"ok": True, "entry": _info(dst)})
                return

            if self.command == "POST" and route == "/chmod":
                body = _read_json(self)
                path = _jailed(body.get("path"), must_exist=True, allow_root=False)
                mode_raw = body.get("mode")
                if not isinstance(mode_raw, str) or not mode_raw.isdigit():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be an octal string such as 0755")
                try:
                    mode = int(mode_raw, 8)
                except ValueError:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid mode")
                if mode < 0 or mode > 0o777:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "mode must be between 0000 and 0777")
                os.chmod(path, mode)
                self._ok({"ok": True, "entry": _info(path)})
                return

            if self.command == "POST" and route == "/delete":
                body = _read_json(self)
                path = _jailed(body.get("path"), must_exist=True, allow_root=False)
                if path.is_dir():
                    if not bool(body.get("recursive", False)):
                        path.rmdir()
                    else:
                        shutil.rmtree(path)
                else:
                    path.unlink()
                self._ok({"ok": True, "deleted": body.get("path")})
                return

            raise ApiError(HTTPStatus.NOT_FOUND, "Unknown endpoint")

        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.message})
        except PermissionError:
            self._send(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Permission denied"})
        except FileNotFoundError:
            self._send(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Path does not exist"})
        except OSError as exc:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"Filesystem error: {exc.strerror or str(exc)}"})
        except Exception as exc:
            print(f"Unhandled error: {exc!r}", flush=True)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Internal error"})

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()


if __name__ == "__main__":
    print(f"Aula Project Bridge starting on {HOST}:{PORT}; jail={ROOT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
