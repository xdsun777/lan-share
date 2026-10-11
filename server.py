#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Share · 局域网文件中转站
===============================

在 PC（或任意一台常开的机器）上运行本程序，同一局域网内的 A / B 两台设备
用浏览器访问该地址，即可互相上传、下载、在线预览文件。

设计要点（针对"不卡顿"的两条硬需求）
------------------------------------
1) 音视频在线预览不卡顿
   - 完整实现 HTTP/1.1 Range 分段请求（206 Partial Content），支持秒开
     与拖动进度条，不需要下载整个文件；
   - 断点/分段传输直接把磁盘句柄 seek 到目标偏移，只读真正需要的字节；
   - 服务端全程 1MB 缓冲区流式读写，内存占用恒定（几十 MB 以内），
     与文件大小无关（20GB 文件同样只占 1MB 缓冲区）；
   - HTTP/1.1 keep-alive + TCP_NODELAY，删除小包延迟，避免传输抖动。

2) 超大文件上传/下载不卡顿
   - 客户端把文件切成 1~16MB 的分块（依文件大小自适应），3 路并发上传；
   - 服务端按偏移量 seek 写入临时分片文件，天然支持断点续传与失败重传；
   - 单块失败自动重试（最多 3 次），网络抖动不会导致整个大文件前功尽弃；
   - 上传完成时才原子重命名（os.replace）到正式目录，避免半成品被下载。

3) 手机扫码加入（纯前端）
   - 二维码在浏览器里用 vendored 的 qrcode.js 现场生成，服务端不参与；
   - 因此不引入任何第三方 Python 依赖，也没有额外的接口与攻击面。

仅使用 Python 标准库，Windows / Linux / macOS 通用。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "局域网文件中转站"
APP_VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PART_DIR_NAME = ".parts"          # 分片临时目录（放在存储根目录内，保证 os.replace 同盘原子）
COPY_BUFSIZE = 1024 * 1024        # 流式读写缓冲区：1MB
MAX_HEADER_BYTES = 64 * 1024
PART_MAX_AGE = 7 * 24 * 3600      # 超过 7 天未完成的分片自动清理

# 静态资源白名单：URL 路径 -> static/ 下的文件名（显式列举，杜绝目录遍历）
STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/vendor/qrcode.js": "vendor/qrcode.js",
    "/favicon.ico": "favicon.ico",
}

# 静态资源的 Content-Type（键与 STATIC_ROUTES 的取值一致）
_STATIC_CTYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "vendor/qrcode.js": "text/javascript; charset=utf-8",
}

# ---------------------------------------------------------------- MIME 映射

MIME_OVERRIDE = {
    # video
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".ogv": "video/ogg", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
    ".ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp", ".rmvb": "application/vnd.rn-realmedia-vbr",
    # audio
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".wma": "audio/x-ms-wma", ".ape": "audio/x-ape",
    # image
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml", ".avif": "image/avif", ".ico": "image/x-icon",
    ".heic": "image/heic",
    # doc / text
    ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".xml": "text/xml; charset=utf-8", ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".py": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8", ".yaml": "text/plain; charset=utf-8",
    ".ini": "text/plain; charset=utf-8", ".conf": "text/plain; charset=utf-8",
    ".sh": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8",
    ".ass": "text/plain; charset=utf-8", ".vtt": "text/vtt; charset=utf-8",
    # archive / binary
    ".zip": "application/zip", ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed", ".tar": "application/x-tar",
    ".gz": "application/gzip", ".apk": "application/vnd.android.package-archive",
    ".exe": "application/vnd.microsoft.portable-executable",
    ".iso": "application/x-iso9660-image",
}

# 浏览器（Chrome/Edge/Safari）能够直接软解的封装格式；其余格式给出"建议下载"提示
BROWSER_PLAYABLE_VIDEO = {".mp4", ".m4v", ".webm", ".ogv", ".mov"}
BROWSER_PLAYABLE_AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}

IMAGE_EXT = set(MIME_OVERRIDE) & {".jpg", ".jpeg", ".png", ".gif", ".webp",
                                  ".bmp", ".svg", ".avif", ".ico"}
VIDEO_EXT = {".mp4", ".m4v", ".webm", ".ogv", ".mov", ".mkv", ".avi", ".wmv",
             ".flv", ".ts", ".mpg", ".mpeg", ".3gp", ".rmvb"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus",
             ".wma", ".ape"}
TEXT_EXT = {".txt", ".md", ".log", ".json", ".csv", ".xml", ".html", ".htm",
            ".css", ".js", ".py", ".yml", ".yaml", ".ini", ".conf", ".sh",
            ".srt", ".ass", ".vtt", ".tsv"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}

TEXT_PREVIEW_MAX = 2 * 1024 * 1024   # 文本预览上限，防止前端卡死

# ---------------------------------------------------------------- 工具函数

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}


def human_size(n: float) -> str:
    """字节数转可读字符串。"""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        n /= 1024.0
        if n < 1024 or unit == "PB":
            return f"{n:.2f} {unit}" if n < 100 else f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def safe_filename(name: str, maxlen: int = 170) -> str:
    """
    把客户端传来的文件名收敛成安全、跨平台的单个文件名。
    重点防御：路径穿越（../）、空字节、控制字符、Windows 保留名、超长名。
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _INVALID_CHARS.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "unnamed"
    stem, ext = os.path.splitext(name)
    if len(ext) > 16:            # 形如 xxx.a.b.c 的伪扩展名，整段当主干处理
        stem, ext = name, ""
    if not stem:
        stem, ext = "unnamed", ""
    if stem.upper() in _WIN_RESERVED:
        stem = "_" + stem
    keep = max(8, maxlen - len(ext))
    if len(stem) > keep:
        stem = stem[:keep]
    return stem + ext


def unique_path(directory: str, filename: str):
    """同名文件自动追加 (1)(2)，返回 (完整路径, 最终文件名)。"""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path, filename
    stem, ext = os.path.splitext(filename)
    for i in range(1, 10001):
        candidate = f"{stem} ({i}){ext}"
        path = os.path.join(directory, candidate)
        if not os.path.exists(path):
            return path, candidate
    raise RuntimeError("同目录下同名文件过多")


def guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in MIME_OVERRIDE:
        return MIME_OVERRIDE[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def classify(path: str):
    """返回 (类型, 是否可在线预览, 是否浏览器原生可播)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video", ext in BROWSER_PLAYABLE_VIDEO, ext in BROWSER_PLAYABLE_VIDEO
    if ext in AUDIO_EXT:
        return "audio", ext in BROWSER_PLAYABLE_AUDIO, ext in BROWSER_PLAYABLE_AUDIO
    if ext in IMAGE_EXT:
        return "image", True, True
    if ext == ".pdf":
        return "pdf", True, True
    if ext in TEXT_EXT:
        return "text", True, False
    if ext in ARCHIVE_EXT:
        return "archive", False, False
    return "other", False, False


def read_rss() -> int:
    """读取本进程常驻内存（字节），用于验证"传输大文件时内存恒定"。"""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return 0


def http_date(ts: float) -> str:
    from email.utils import formatdate
    return formatdate(ts, usegmt=True)


def lan_ipv4_addresses():
    """
    枚举本机可用的局域网 IPv4（排除回环 / Docker / 虚拟网卡），
    并通过 UDP connect 探测默认出口地址，把它排在第一位。
    """
    primary = None
    for probe in ("223.5.5.5", "114.114.114.114", "8.8.8.8"):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect((probe, 53))
            primary = s.getsockname()[0]
            break
        except Exception:
            continue
        finally:
            if s:
                s.close()

    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                found.add(ip)
    except Exception:
        pass
    if primary:
        found.add(primary)

    def is_virtual(ip: str) -> bool:
        # Docker 默认网桥 172.17/172.18…、常见的 172.16.0.1 网关段
        if ip.startswith("172.17.") or ip.startswith("172.18."):
            return True
        return False

    real = sorted((ip for ip in found if not is_virtual(ip)),
                  key=lambda x: (x != primary, x))
    return real or sorted(found)


# ---------------------------------------------------------------- 服务配置


class Config:
    def __init__(self, root: str, password: str | None, port: int,
                 max_upload: int, quiet: bool):
        self.root = os.path.abspath(root)
        self.part_dir = os.path.join(self.root, PART_DIR_NAME)
        self.password = password or None
        self.port = port
        self.max_upload = max_upload
        self.quiet = quiet
        self.secret = secrets.token_bytes(32)
        self.auth_token = (hmac.new(self.secret, b"lan-share-auth",
                                    hashlib.sha256).hexdigest()
                           if self.password else None)
        self.start_ts = time.time()
        self.rss = 0
        self.peak_rss = 0
        self.lock_finish = threading.Lock()
        self.part_locks = {}
        self.part_locks_guard = threading.Lock()
        self.stats = {"uploaded_bytes": 0, "uploaded_files": 0,
                      "served_bytes": 0, "requests": 0}

    def part_lock(self, uid: str) -> threading.Lock:
        with self.part_locks_guard:
            lock = self.part_locks.get(uid)
            if lock is None:
                lock = threading.Lock()
                self.part_locks[uid] = lock
            return lock


# ---------------------------------------------------------------- HTTP 处理


class _ChunkedSink:
    """
    把任意 write() 切成 HTTP/1.1 chunked 帧，让 zip 这类"长度未知"的内容
    也能边生成边发送（不用先落临时文件，也不用预估总大小）。
    tell() 故意抛 OSError：这样 zipfile 会走"数据描述符"的流式路径，
    CRC 与长度在每个条目写完后补记，天然支持边读边打包。
    """

    def __init__(self, handler, chunk_size=65536):
        self.h = handler
        self.n = chunk_size
        self._buf = bytearray()

    def write(self, data):
        self._buf += data
        while len(self._buf) >= self.n:
            self._emit(bytes(self._buf[:self.n]))
            del self._buf[:self.n]
        return len(data)

    def _emit(self, data):
        if not data:
            return
        self.h.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")

    def flush(self):
        if self._buf:
            self._emit(bytes(self._buf))
            self._buf.clear()

    def finish(self):
        self.flush()
        self.h.wfile.write(b"0\r\n\r\n")

    def tell(self):
        raise OSError("stream is not seekable")

    def seekable(self):
        return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"      # 开启 keep-alive，分块上传/拖动播放收益明显
    server_version = f"LanShare/{APP_VERSION}"
    disable_nagle_algorithm = True     # TCP_NODELAY，消除落盘小包延迟
    timeout = 300                      # 单次 socket 读写超时（秒）

    # ---------- 基础设施 ----------

    @property
    def cfg(self) -> Config:
        return self.server.cfg  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: A003 - 覆写标准库接口
        if self.cfg.quiet:
            return
        msg = fmt % args if args else fmt
        ts = time.strftime("%H:%M:%S")
        sys.stdout.write(f"[{ts}] {self.client_address[0]:<15} {msg}\n")
        sys.stdout.flush()

    def log_error(self, fmt, *args):
        """超时/断连属于正常现象，降级为静默处理，避免刷屏。"""
        if "timed out" in (fmt % args if args else fmt):
            return
        self.log_message("! " + fmt, *args)

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str):
        self.cfg.stats["requests"] += 1
        self._body_consumed = 0
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            # ---- 无需鉴权：静态资源与探活 ----
            if path in STATIC_ROUTES:
                return self.serve_static(STATIC_ROUTES[path])
            if path == "/healthz":
                return self.send_json({"ok": True, "app": APP_NAME,
                                       "version": APP_VERSION})
            if path == "/api/meta":
                return self.send_json({
                    "app": APP_NAME, "version": APP_VERSION,
                    "passwordRequired": bool(self.cfg.password),
                    "maxUpload": self.cfg.max_upload,
                    "urls": self.server.urls,  # type: ignore[attr-defined]
                    "textPreviewMax": TEXT_PREVIEW_MAX,
                })
            if path == "/api/login" and method == "POST":
                return self.handle_login()

            # ---- 以下全部需要鉴权 ----
            if not self.is_authenticated():
                return self.deny()

            if path == "/api/list":
                return self.api_list()
            if path.startswith("/f/"):
                return self.serve_file(path[3:], query)
            if path == "/api/delete" and method == "POST":
                return self.api_delete()
            if path == "/api/zip" and method == "GET":
                return self.api_zip(query)
            if path == "/api/upload/init" and method == "POST":
                return self.api_upload_init()
            if path == "/api/upload/chunk" and method == "POST":
                return self.api_upload_chunk(query)
            if path == "/api/upload/complete" and method == "POST":
                return self.api_upload_complete()
            if path == "/api/upload/abort" and method == "POST":
                return self.api_upload_abort()

            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            try:
                self.send_json({"error": "internal error"},
                               HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                self.close_connection = True
        finally:
            if method == "POST" and not self.close_connection:
                # 兜底：确保请求体被消费干净，否则 keep-alive 会把残留字节当成下一个请求。
                # 这里按"已消费字节数"差额读取，绝不能盲目重读 Content-Length 造成阻塞。
                self.drain_body()

    # ---------- 请求体与响应 ----------

    def content_length(self) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            te = (self.headers.get("Transfer-Encoding") or "").lower()
            if "chunked" in te:
                raise ValueError("chunked transfer-encoding not supported")
            return 0
        return int(raw)

    def read_raw(self, n: int) -> bytes:
        """读取请求体的唯一入口，用于精确统计已消费字节数。"""
        buf = self.rfile.read(n)
        if buf:
            self._body_consumed += len(buf)
        return buf

    def drain_body(self):
        """把未读取的请求体读掉，保证连接可复用。"""
        try:
            total = self.content_length()
        except (ValueError, TypeError):
            self.close_connection = True
            return
        remaining = total - getattr(self, "_body_consumed", 0)
        while remaining > 0:
            chunk = self.read_raw(min(COPY_BUFSIZE, remaining))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)

    def read_json_body(self, limit: int = 1024 * 1024) -> dict:
        length = self.content_length()
        if length <= 0:
            return {}
        if length > limit:
            self.drain_body()
            raise ValueError("body too large")
        raw = self.read_raw(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def send_json(self, obj, status: int = HTTPStatus.OK):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_text(self, text: str, status: int = HTTPStatus.OK,
                  ctype: str = "text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------- 鉴权 ----------

    def is_authenticated(self) -> bool:
        if not self.cfg.password:
            return True
        cookie = self.headers.get("Cookie") or ""
        for item in cookie.split(";"):
            key, _, value = item.strip().partition("=")
            if key == "ls_auth" and value:
                return hmac.compare_digest(value.strip(), self.cfg.auth_token)
        return False

    def deny(self):
        if self.path.startswith("/api/"):
            return self.send_json({"error": "unauthorized",
                                   "passwordRequired": True},
                                  HTTPStatus.UNAUTHORIZED)
        return self.send_text("需要访问密码", HTTPStatus.UNAUTHORIZED)

    def handle_login(self):
        data = self.read_json_body()
        pwd = str(data.get("password") or "")
        if not self.cfg.password:
            return self.send_json({"ok": True})
        if not pwd or not hmac.compare_digest(pwd, self.cfg.password):
            time.sleep(0.4)          # 轻微延时，抬高爆破成本
            return self.send_json({"error": "密码错误"},
                                  HTTPStatus.UNAUTHORIZED)
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = b'{"ok": true}'
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie",
                         f"ls_auth={self.cfg.auth_token}; Path=/; "
                         f"HttpOnly; SameSite=Lax; Max-Age=31536000")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 静态资源 ----------

    def serve_static(self, name: str):
        if name == "favicon.ico":
            return self.send_text("", HTTPStatus.NO_CONTENT)
        if name not in _STATIC_CTYPES:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        path = os.path.normpath(os.path.join(STATIC_DIR, name))
        # 双保险：即使白名单被改坏，也不允许跳出 static/
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            return self.send_text(f"缺少静态文件: {name}", HTTPStatus.NOT_FOUND)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", _STATIC_CTYPES[name])
        self.send_header("Content-Length", str(len(body)))
        # 前端资源改动后必须让浏览器立刻拿到新版本，否则升级后容易看到旧页面
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------- 文件列表 ----------

    def api_list(self):
        root = self.cfg.root
        items = []
        total = 0
        try:
            entries = os.scandir(root)
        except OSError as exc:
            return self.send_json({"error": f"无法读取目录: {exc}"},
                                  HTTPStatus.INTERNAL_SERVER_ERROR)
        with entries:
            for entry in entries:
                try:
                    if entry.name.startswith("."):
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                kind, previewable, playable = classify(entry.name)
                ext = os.path.splitext(entry.name)[1].lower().lstrip(".") or "file"
                items.append({
                    "name": entry.name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "type": kind,
                    "ext": ext.upper(),
                    "previewable": previewable,
                    "playable": playable,
                })
                total += st.st_size
        items.sort(key=lambda x: x["mtime"], reverse=True)

        disk = shutil.disk_usage(root)
        return self.send_json({
            "files": items,
            "count": len(items),
            "totalSize": total,
            "disk": {"total": disk.total, "free": disk.free, "used": disk.used},
            "root": root,
            "uptime": int(time.time() - self.cfg.start_ts),
            "urls": self.server.urls,  # type: ignore[attr-defined]
            "memory": {"rss": self.cfg.rss, "peak": self.cfg.peak_rss},
            "stats": dict(self.cfg.stats),
        })

    # ---------- 文件下载 / 在线预览（Range 分段传输的核心） ----------

    def serve_file(self, raw_name: str, query: dict):
        name = os.path.basename(raw_name)
        if not name or name.startswith("."):
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        path = os.path.join(self.cfg.root, name)
        # 双重校验：解析后的绝对路径必须仍在存储根目录内
        if os.path.realpath(path) != os.path.join(
                os.path.realpath(self.cfg.root), name):
            return self.send_json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        if not os.path.isfile(path):
            return self.send_json({"error": "file not found"},
                                  HTTPStatus.NOT_FOUND)

        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            return self.send_json({"error": "file vanished"},
                                  HTTPStatus.NOT_FOUND)

        as_download = "dl" in query or "download" in query
        ctype = guess_mime(path)
        disposition = "attachment" if as_download else "inline"

        start, end, partial = 0, max(size - 1, 0), False
        range_header = self.headers.get("Range")
        if range_header and size > 0:
            try:
                parsed = self.parse_range(range_header, size)
            except ValueError:
                self.send_response(int(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE))
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if parsed is not None:
                start, end = parsed
                partial = True

        length = 0 if size == 0 else (end - start + 1)

        self.send_response(int(HTTPStatus.PARTIAL_CONTENT if partial
                               else HTTPStatus.OK))
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", http_date(mtime))
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header(
            "Content-Disposition",
            f'{disposition}; filename="{urllib.parse.quote(name)}"; '
            f"filename*=UTF-8''{urllib.parse.quote(name)}")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if self.command == "HEAD" or length == 0:
            return

        self.cfg.stats["served_bytes"] += length
        remaining = length
        try:
            with open(path, "rb") as fp:
                fp.seek(start)
                while remaining > 0:
                    chunk = fp.read(min(COPY_BUFSIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 用户拖动进度条/关闭播放器时必然发生，属正常现象
            self.close_connection = True

    @staticmethod
    def parse_range(header: str, size: int):
        """
        解析 Range 头，仅处理最常用的单区间形式。
        返回 (start, end)；None 表示"该头看不懂，按整体 200 返回"；
        抛 ValueError 表示"语法合法但不可满足"，应回 416。
        """
        header = header.strip()
        if not header.lower().startswith("bytes="):
            return None
        spec = header[6:].split(",")[0].strip()
        if "-" not in spec:
            return None
        left, _, right = spec.partition("-")
        left, right = left.strip(), right.strip()
        try:
            if not left:                        # bytes=-N 末尾 N 字节
                if not right.isdigit():
                    return None
                n = int(right)
                if n <= 0:
                    raise ValueError("bad suffix range")
                start = max(0, size - n)
                end = size - 1
            else:
                if not left.isdigit():
                    return None
                start = int(left)
                end = size - 1 if not right else min(int(right), size - 1)
                if right and not right.isdigit():
                    return None
        except ValueError:
            raise
        if size == 0 or start >= size or start > end:
            raise ValueError("unsatisfiable range")
        return start, end

    # ---------- 批量打包下载 ----------

    def zip_targets(self, query):
        """解析并校验待打包文件名，返回 [(文件名, 绝对路径)] 与非法项。"""
        names = []
        for item in (query.get("names") or []):
            for piece in str(item).split("\n"):
                piece = piece.strip()
                if piece and piece not in names:
                    names.append(piece)

        root_real = os.path.realpath(self.cfg.root)
        ok, bad = [], []
        for item in names:
            name = os.path.basename(item)
            path = os.path.join(root_real, name)
            if not name or name.startswith(".") or os.path.dirname(path) != root_real:
                bad.append({"name": name, "error": "非法文件名"})
                continue
            if not os.path.isfile(path):
                bad.append({"name": name, "error": "文件不存在"})
                continue
            ok.append((name, path))
        return ok, bad

    def api_zip(self, query):
        targets, bad = self.zip_targets(query)
        if not targets:
            return self.send_json({"error": "没有可打包的文件", "failed": bad},
                                  HTTPStatus.BAD_REQUEST)

        zip_name = f"共享文件_{time.strftime('%Y%m%d_%H%M%S')}.zip"

        # HTTP/1.0 不支持 chunked：退化为一次性写出并关闭连接
        chunked = self.request_version != "HTTP/1.0"
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", "application/zip")
        quoted = urllib.parse.quote(zip_name)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{quoted}"; '
                         f"filename*=UTF-8''{quoted}")
        self.send_header("Cache-Control", "no-store")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        self.close_connection = not chunked

        try:
            sink = _ChunkedSink(self) if chunked else self.wfile
            # ZIP_STORED：音视频/图片本身已是压缩格式，再压一遍只会烧 CPU；
            # allowZip64 让总量超过 4GB 也没问题。
            with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as zf:
                for name, path in targets:
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue        # 打包途中被删除：跳过，不影响其余文件
                    zi = zipfile.ZipInfo(name,
                                         date_time=time.localtime(st.st_mtime)[:6])
                    zi.compress_type = zipfile.ZIP_STORED
                    zi.external_attr = 0o644 << 16
                    with open(path, "rb") as f, zf.open(zi, "w") as out:
                        while True:
                            buf = f.read(COPY_BUFSIZE)
                            if not buf:
                                break
                            out.write(buf)
                    self.cfg.stats["served_bytes"] += st.st_size
            if chunked:
                sink.finish()       # 写出 0 长度结束帧，连接仍可复用
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        self.log_message("打包下载 %d 个文件 -> %s", len(targets), zip_name)

    # ---------- 上传：分块 + 断点续传 ----------

    def api_upload_init(self):
        data = self.read_json_body()
        filename = safe_filename(str(data.get("name") or ""))
        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size < 0:
            return self.send_json({"error": "非法的文件大小"},
                                  HTTPStatus.BAD_REQUEST)
        if self.cfg.max_upload and size > self.cfg.max_upload:
            return self.send_json(
                {"error": f"超出单文件上限 {human_size(self.cfg.max_upload)}"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        free = shutil.disk_usage(self.cfg.root).free
        if size > free - 64 * 1024 * 1024:
            return self.send_json(
                {"error": f"服务端磁盘空间不足（剩余 {human_size(free)}）"},
                HTTPStatus.INSUFFICIENT_STORAGE)

        key = str(data.get("key") or f"{filename}|{size}")
        uid = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        part = os.path.join(self.cfg.part_dir, uid + ".part")
        meta_path = os.path.join(self.cfg.part_dir, uid + ".json")

        offset = 0
        if os.path.isfile(part):
            # 断点续传偏移量 = "从 0 开始连续覆盖的字节数"，
            # 不能直接用文件大小（并发分块会先写好后面的分块而不连续）。
            offset = self.contiguous_prefix(self.load_meta(meta_path).get("ranges"))
            if offset > size:
                self._remove_part(uid)
                offset = 0
        if not os.path.isfile(part):
            with open(part, "wb"):
                pass

        meta = {"name": filename, "size": size,
                "mtime": int(data.get("lastModified") or time.time() * 1000),
                "created": time.time(),
                "ranges": ([ [0, offset] ] if offset else [])}
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        return self.send_json({"uploadId": uid, "offset": offset,
                               "chunkSizeHint": 4 * 1024 * 1024,
                               "name": filename})

    def api_upload_chunk(self, query: dict):
        uid = (query.get("uid") or [""])[0]
        try:
            offset = int((query.get("offset") or ["-1"])[0])
        except ValueError:
            offset = -1
        if not re.fullmatch(r"[0-9a-f]{20}", uid or "") or offset < 0:
            return self.send_json({"error": "bad params"},
                                  HTTPStatus.BAD_REQUEST)

        part = os.path.join(self.cfg.part_dir, uid + ".part")
        if not os.path.isfile(part):
            return self.send_json({"error": "session not found"},
                                  HTTPStatus.NOT_FOUND)
        meta_path = os.path.join(self.cfg.part_dir, uid + ".json")

        try:
            length = self.content_length()
        except ValueError as exc:
            return self.send_json({"error": str(exc)},
                                  HTTPStatus.LENGTH_REQUIRED)
        if length <= 0:
            return self.send_json({"error": "empty chunk"},
                                  HTTPStatus.BAD_REQUEST)

        written = 0
        try:
            # 每个请求使用独立的文件描述符并 seek 到自己的偏移区间，
            # 不同分块的写入区间互不重叠，因此无需加锁即可并发落盘，
            # 3 路并发上传不会被串行化。
            with open(part, "r+b") as fp:
                fp.seek(offset)
                remaining = length
                while remaining > 0:
                    buf = self.read_raw(min(COPY_BUFSIZE, remaining))
                    if not buf:
                        self.close_connection = True
                        break
                    fp.write(buf)
                    written += len(buf)
                    remaining -= len(buf)
                fp.flush()
        except OSError as exc:
            return self.send_json({"error": f"写入失败: {exc}"},
                                  HTTPStatus.INTERNAL_SERVER_ERROR)

        if written:
            # 只有"已接收区间"这份元数据需要串行更新
            with self.cfg.part_lock(uid):
                self.record_range(part, meta_path, offset, written)

        self.cfg.stats["uploaded_bytes"] += written
        if written != length:
            return self.send_json({"error": "incomplete chunk",
                                   "received": written},
                                  HTTPStatus.BAD_REQUEST)
        return self.send_json({"ok": True, "received": written,
                               "offset": offset + written})

    # ---------- 已接收区间（保证断点续传不会丢失中间分块） ----------

    @staticmethod
    def load_meta(meta_path: str) -> dict:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def record_range(part_path: str, meta_path: str, start: int, length: int):
        """把新写入的区间并入 meta 的已接收区间列表（合并相邻/重叠区间）。"""
        # 会话可能已被"取消上传"删除：此时不能再补写元数据，否则留下孤儿文件
        if not os.path.isfile(part_path):
            return
        meta = Handler.load_meta(meta_path)
        ranges = meta.get("ranges") or []
        ranges.append([start, start + length])
        ranges.sort()
        merged = []
        for s, e in ranges:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        meta["ranges"] = merged
        tmp = meta_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, meta_path)

    @staticmethod
    def contiguous_prefix(ranges) -> int:
        """从 0 开始连续覆盖到多少字节（并发分块可能乱序完成，必须按区间算）。"""
        prefix = 0
        for item in sorted(ranges or []):
            try:
                s, e = int(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if s > prefix:
                break
            prefix = max(prefix, e)
        return prefix

    @staticmethod
    def missing_ranges(ranges, size: int):
        """返回尚未覆盖的字节区间；空列表表示文件完整。"""
        if size <= 0:
            return []
        missing = []
        covered = 0
        for item in sorted(ranges or []):
            try:
                s, e = int(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if s > covered:
                missing.append([covered, min(s, size)])
            covered = max(covered, e)
            if covered >= size:
                break
        if covered < size:
            missing.append([covered, size])
        return [m for m in missing if m[0] < m[1]]

    def api_upload_complete(self):
        data = self.read_json_body()
        uid = str(data.get("uid") or "")
        if not re.fullmatch(r"[0-9a-f]{20}", uid):
            return self.send_json({"error": "bad uid"},
                                  HTTPStatus.BAD_REQUEST)
        part = os.path.join(self.cfg.part_dir, uid + ".part")
        meta_path = os.path.join(self.cfg.part_dir, uid + ".json")
        if not os.path.isfile(part):
            return self.send_json({"error": "session not found"},
                                  HTTPStatus.NOT_FOUND)

        meta = self.load_meta(meta_path)
        if not meta:
            meta = {"name": "unnamed", "size": os.path.getsize(part)}

        expected = int(meta.get("size") or 0)
        actual = os.path.getsize(part)
        if expected and actual != expected:
            return self.send_json(
                {"error": "文件不完整", "received": actual, "expected": expected},
                HTTPStatus.BAD_REQUEST)

        # 逐区间校验：确保每个字节都真实收到，绝不把有空洞的半成品当成成品发布
        missing = self.missing_ranges(meta.get("ranges"), expected)
        if missing:
            return self.send_json(
                {"error": "仍有分块未上传完成", "missing": missing[:20],
                 "missingCount": len(missing)},
                HTTPStatus.BAD_REQUEST)

        with self.cfg.lock_finish:             # 保证"查重 + 改名"原子
            target, final_name = unique_path(self.cfg.root,
                                             safe_filename(meta["name"]))
            try:
                os.replace(part, target)
            except OSError as exc:
                return self.send_json({"error": f"落盘失败: {exc}"},
                                      HTTPStatus.INTERNAL_SERVER_ERROR)
            mtime_ms = meta.get("mtime")
            if mtime_ms:
                try:
                    ts = float(mtime_ms) / 1000.0
                    os.utime(target, (ts, ts))
                except (OSError, ValueError):
                    pass
            try:
                os.remove(meta_path)
            except OSError:
                pass

        self._cleanup_locks(uid)
        self.cfg.stats["uploaded_files"] += 1
        self.log_message('上传完成 "%s" (%s)', final_name, human_size(actual))
        return self.send_json({"ok": True, "name": final_name, "size": actual})

    def api_upload_abort(self):
        data = self.read_json_body()
        uid = str(data.get("uid") or "")
        if re.fullmatch(r"[0-9a-f]{20}", uid):
            # 与在途分块的元数据写入互斥，避免删除后又被重新创建
            with self.cfg.part_lock(uid):
                self._remove_part(uid)
        return self.send_json({"ok": True})

    def _remove_part(self, uid: str):
        for suffix in (".part", ".json"):
            try:
                os.remove(os.path.join(self.cfg.part_dir, uid + suffix))
            except OSError:
                pass
        self._cleanup_locks(uid)

    def _cleanup_locks(self, uid: str):
        with self.cfg.part_locks_guard:
            self.cfg.part_locks.pop(uid, None)

    # ---------- 删除 ----------

    def api_delete(self):
        data = self.read_json_body()
        raw = str(data.get("name") or "")
        names = data.get("names")
        if isinstance(names, list):
            targets = [str(x) for x in names]
        else:
            targets = [raw] if raw else []
        if not targets:
            return self.send_json({"error": "缺少文件名"},
                                  HTTPStatus.BAD_REQUEST)

        deleted, failed = [], []
        root_real = os.path.realpath(self.cfg.root)
        for item in targets:
            name = os.path.basename(item)
            path = os.path.join(root_real, name)
            if not name or name.startswith(".") or os.path.dirname(path) != root_real:
                failed.append({"name": name, "error": "非法文件名"})
                continue
            if not os.path.isfile(path):
                failed.append({"name": name, "error": "文件不存在"})
                continue
            try:
                os.remove(path)
                deleted.append(name)
            except OSError as exc:
                failed.append({"name": name, "error": str(exc)})
        if deleted:
            self.log_message("删除 %d 个文件: %s", len(deleted), ", ".join(deleted))
        return self.send_json({"ok": not failed, "deleted": deleted,
                               "failed": failed})


class LanServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def __init__(self, address, handler, cfg: Config):
        self.cfg = cfg
        self.urls = []
        super().__init__(address, handler)


# ---------------------------------------------------------------- 启动引导


def purge_stale_parts(cfg: Config):
    """清理上传垃圾：超过 PART_MAX_AGE 的分片，以及没有 .part 对应的孤儿元数据。"""
    now = time.time()
    removed = 0
    orphans = 0
    try:
        with os.scandir(cfg.part_dir) as it:
            entries = list(it)
    except OSError:
        return
    parts = {os.path.splitext(e.name)[0] for e in entries if e.name.endswith(".part")}
    for entry in entries:
        try:
            stem = os.path.splitext(entry.name)[0]
            if entry.name.endswith(".part"):
                if now - entry.stat().st_mtime > PART_MAX_AGE:
                    os.remove(entry.path)
                    removed += 1
            elif entry.name.endswith(".json") or entry.name.endswith(".tmp"):
                # 元数据必须与同名分片共存，否则一定是历史遗留
                if stem not in parts or now - entry.stat().st_mtime > PART_MAX_AGE:
                    os.remove(entry.path)
                    orphans += 1
        except OSError:
            continue
    if not cfg.quiet and (removed or orphans):
        print(f"已清理 {removed} 个过期分片、{orphans} 个孤儿元数据文件")


def print_banner(cfg: Config, port: int, ips):
    line = "=" * 62
    print(line)
    print(f"  {APP_NAME}  v{APP_VERSION}")
    print(line)
    print(f"  存储目录 : {cfg.root}")
    print(f"  上传分片 : {cfg.part_dir}")
    print(f"  访问密码 : {'已启用' if cfg.password else '未启用（同一局域网内任何人可访问）'}")
    print(f"  单文件上限: {human_size(cfg.max_upload) if cfg.max_upload else '不限'}")
    print()
    print("  访问地址（把下面任意一条发给 A / B 端，或手机浏览器直接打开）：")
    print(f"    http://127.0.0.1:{port}/        （本机）")
    for ip in ips:
        print(f"    http://{ip}:{port}/")
    print()
    print("  提示：若其他设备打不开，请放行防火墙该端口，并确认在同一个局域网。")
    print("  按 Ctrl+C 停止服务。")
    print(line)


def main(argv=None):
    # 输出重定向到文件（如 nohup / 计划任务 / Windows 重定向）时也要实时落盘；
    # 同时强制 UTF-8 并容忍不可编码字符，避免中文 Windows 的 GBK 控制台抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} —— 局域网文件互传 / 在线预览小工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python3 server.py\n"
               "  python3 server.py --port 9000 --dir /data/share --password 1234\n")
    parser.add_argument("-p", "--port", type=int, default=8765,
                        help="监听端口（默认 8765）")
    parser.add_argument("-H", "--host", default="0.0.0.0",
                        help="监听地址（默认 0.0.0.0，即允许局域网访问）")
    parser.add_argument("-d", "--dir", default=os.path.join(BASE_DIR, "uploads"),
                        help="文件存储目录（默认 ./uploads）")
    # 密码优先取命令行；未给则回落到环境变量 LANSHARE_PASSWORD。
    # 前者便于手工启动，后者供 systemd 的 EnvironmentFile 使用（避免密码出现在 ps / unit 文件里）。
    parser.add_argument("-w", "--password", default=os.environ.get("LANSHARE_PASSWORD") or None,
                        help="可选的访问密码，设置后需要登录才能使用（也可用环境变量 LANSHARE_PASSWORD）")
    parser.add_argument("--max-upload", type=int, default=0,
                        help="单文件大小上限，单位 MB，0 表示不限（默认 0）")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="不打印每个请求的访问日志")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.dir)
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, PART_DIR_NAME), exist_ok=True)

    cfg = Config(root=root, password=args.password, port=args.port,
                 max_upload=args.max_upload * 1024 * 1024, quiet=args.quiet)

    purge_stale_parts(cfg)

    def sample_rss():
        while True:
            cfg.rss = read_rss()
            if cfg.rss > cfg.peak_rss:
                cfg.peak_rss = cfg.rss
            time.sleep(1.5)

    threading.Thread(target=sample_rss, daemon=True).start()

    server = None
    last_error = None
    for host in ([args.host] if args.host != "0.0.0.0" else ["0.0.0.0", "::"]):
        try:
            server = LanServer((host, args.port), Handler, cfg)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        print(f"启动失败：端口 {args.port} 无法监听（{last_error}）", file=sys.stderr)
        print("请换一个端口，例如：python3 server.py --port 8766", file=sys.stderr)
        return 1

    ips = lan_ipv4_addresses()
    server.urls = [f"http://{ip}:{args.port}/" for ip in ips]
    print_banner(cfg, args.port, ips)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止服务…")
    finally:
        server.shutdown()
        server.server_close()
        st = cfg.stats
        print(f"本次运行共接收 {st['uploaded_files']} 个文件 "
              f"({human_size(st['uploaded_bytes'])})，"
              f"对外发送 {human_size(st['served_bytes'])}，"
              f"处理请求 {st['requests']} 次。再见。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
