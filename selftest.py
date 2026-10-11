#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网文件中转站 · 自检脚本
============================

完全按前端的真实 HTTP 协议打接口做端到端验证，适用于：
  - 在新机器上部署完成后确认功能与性能；
  - 修改 server.py 之后做回归测试；
  - 大文件传输链路（磁盘 / 网卡 / 防火墙）的健康检查。

覆盖内容
  1. 基础接口与静态资源
  2. HTTP/1.1 keep-alive 连接复用
  3. 分块上传：小文件 / 0 字节 / 大文件（sha256 一致性）
  4. 大文件传输期间服务端内存是否恒定
  5. Range 分段语义：206 / 416 / 后缀区间 / 开放区间 / HEAD / 字节逐一比对
  6. 断点续传：并发乱序写入后仍能算出正确的续传偏移
  7. 多客户端并发下载吞吐
  8. 路径穿越、磁盘不足、非法会话等防护
  9. 批量打包下载（zip 流式）
 10. 访问密码：环境变量注入与命令行优先级（systemd EnvironmentFile 依赖此行为）

用法
    # 先启动服务（另开一个终端）
    python3 server.py

    # 再运行自检（默认连 127.0.0.1:8765）
    python3 selftest.py
    python3 selftest.py --port 9000 --big-mb 3072

注意：自检会在服务端存储目录里生成并删除测试文件（默认约 1.3GB 临时数据），
      也请避免在正式使用中的存储目录里同时存放重要文件的前提下运行。
"""

import argparse
import concurrent.futures as cf
import hashlib
import http.client
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, ".selftest")

HOST, PORT = "127.0.0.1", 8765
CHUNK = 8 * 1024 * 1024
CONCURRENCY = 3
TAG = "selftest"
UPLOADS = ""            # 服务端存储目录，运行时从 /api/list 获取

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    sys.stdout.flush()


class Client:
    """一个实例对应一个线程的连接，复用 keep-alive"""

    def __init__(self):
        self.conn = http.client.HTTPConnection(HOST, PORT, timeout=600)

    def req(self, method, path, body=None, headers=None):
        self.conn.request(method, path, body=body, headers=headers or {})
        r = self.conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    def json(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        h = {"Content-Type": "application/json"} if body else {}
        st, hd, data = self.req(method, path, body, h)
        return st, (json.loads(data) if data else None)

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def sha256_file(path, buf=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def local_path(name):
    return os.path.join(UPLOADS, name)


def make_file(path, size, seed=1234):
    """确定性伪随机内容，分块写入，避免一次性占用大内存"""
    rnd = random.Random(seed)
    with open(path, "wb") as f:
        left = size
        while left > 0:
            n = min(4 * 1024 * 1024, left)
            f.write(rnd.randbytes(n))
            left -= n
    return path


def upload(client, path, name, limit_chunks=None):
    """按前端协议上传：init -> 3 路并发分块 -> complete"""
    size = os.path.getsize(path)
    mtime = int(os.path.getmtime(path) * 1000)
    st, init = client.json("POST", "/api/upload/init", {
        "name": name, "size": size, "lastModified": mtime,
        "key": f"{name}|{size}|{mtime}",
    })
    if st != 200:
        return {"ok": False, "errors": [f"init -> {st} {init}"]}
    uid, offset = init["uploadId"], init["offset"]
    total = max(1, (size + CHUNK - 1) // CHUNK)
    idxs = list(range(min(total, offset // CHUNK), total))
    if limit_chunks is not None:
        idxs = idxs[:limit_chunks]

    errors, errors_lock = [], threading.Lock()
    tls = threading.local()

    def send(idx):
        c = getattr(tls, "client", None)
        if c is None:
            c = Client()
            tls.client = c
        off = idx * CHUNK
        with open(path, "rb") as f:
            f.seek(off)
            blob = f.read(min(CHUNK, size - off))
        st, hd, data = c.req(
            "POST", f"/api/upload/chunk?uid={uid}&offset={off}", blob,
            {"Content-Type": "application/octet-stream"})
        if st != 200:
            with errors_lock:
                errors.append(f"chunk {idx} -> {st} {data[:200]}")

    if size > 0:                 # 与前端的空文件处理保持一致：0 字节不发分块
        with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            list(pool.map(send, idxs))
    if errors:
        return {"ok": False, "errors": errors, "uid": uid}
    if limit_chunks is not None:
        return {"ok": True, "uid": uid, "partial": True}

    st, done = client.json("POST", "/api/upload/complete",
                           {"uid": uid, "name": name, "size": size})
    if st != 200:
        return {"ok": False, "errors": [f"complete -> {st} {done}"], "uid": uid}
    return {"ok": True, "name": done["name"]}


def rss_poller(stop_event, samples):
    c = Client()
    while not stop_event.is_set():
        try:
            st, data = c.json("GET", "/api/list")
            if st == 200 and data.get("memory"):
                samples.append(data["memory"]["rss"])
        except Exception:
            pass
        time.sleep(0.4)
    c.close()


def cleanup(client):
    """删除本次自检产生的文件"""
    try:
        st, data = client.json("GET", "/api/list")
        names = [f["name"] for f in (data or {}).get("files", []) if f["name"].startswith(TAG)]
        if names:
            client.json("POST", "/api/delete", {"names": names})
    except Exception:
        pass
    shutil.rmtree(DATA_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# 辅助：按指定方式拉起一个「独立」的服务实例
# 第 10 节要验证的核心问题是「密码能不能只靠环境变量生效」——这要求服务端
# 以受控的环境/参数启动，因此这里自己 spawn 子进程，而不是复用外部实例。
# ---------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_server(port, data_dir, env_password=None, cli_password=None):
    env = dict(os.environ)
    env.pop("LANSHARE_PASSWORD", None)
    if env_password is not None:
        env["LANSHARE_PASSWORD"] = env_password
    cmd = [sys.executable, os.path.join(BASE_DIR, "server.py"),
           "--host", "127.0.0.1", "--port", str(port), "--dir", data_dir, "-q"]
    if cli_password is not None:
        cmd += ["--password", cli_password]
    return subprocess.Popen(cmd, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_server(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def wait_ready(port, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/healthz")
            c.getresponse().read()
            c.close()
            return True
        except Exception:
            time.sleep(0.2)
    return False


def http_probe(port, method, path, payload=None, cookie=None, timeout=10):
    """单次请求（独立连接），返回 (status, headers, body)"""
    headers, body = {}, None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        c.close()


def main():
    global HOST, PORT, UPLOADS
    ap = argparse.ArgumentParser(description="局域网文件中转站自检")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--big-mb", type=int, default=1024,
                    help="大文件测试体积，单位 MB（默认 1024）")
    args = ap.parse_args()
    HOST, PORT = args.host, args.port
    BIG = max(64, args.big_mb) * 1024 * 1024
    MID = max(8 * 1024 * 1024, BIG // 4)

    mc = Client()
    try:
        st, _, _ = mc.req("GET", "/healthz")
    except Exception as exc:
        print(f"无法连接 {HOST}:{PORT} —— {exc}\n请先启动服务：python3 server.py")
        return 2

    os.makedirs(DATA_DIR, exist_ok=True)
    st, data = mc.json("GET", "/api/list")
    UPLOADS = (data or {}).get("root") or ""
    if not UPLOADS or not os.path.isdir(UPLOADS):
        print("无法确定服务端存储目录，请确认服务已正确启动。")
        return 2

    print(f"\n目标服务 http://{HOST}:{PORT}/")
    print(f"服务端存储目录 {UPLOADS}    大文件 {BIG // 1024 // 1024} MB")

    try:
        print("\n【1】基础接口与静态资源")
        st, hd, body = mc.req("GET", "/healthz")
        check("/healthz 返回 200 JSON", st == 200 and b'"ok": true' in body, f"status={st}")
        st, hd, body = mc.req("GET", "/")
        check("首页 HTML 正常", st == 200 and "局域网文件中转站".encode() in body
              and hd.get("Content-Length") == str(len(body)), f"len={len(body)}")
        st, hd, body = mc.req("GET", "/style.css")
        check("样式表正常", st == 200 and b"--accent" in body)
        st, hd, body = mc.req("GET", "/app.js")
        check("前端脚本正常", st == 200 and b"CHUNK_CONCURRENCY" in body)
        st, _, _ = mc.req("GET", "/../server.py")
        check("路径穿越读取被拒绝", st in (400, 403, 404), f"status={st}")
        check("列表接口结构完整", isinstance(data, dict) and "files" in data and "memory" in data)

        print("\n【2】keep-alive 连接复用")
        t0 = time.time()
        for _ in range(40):
            mc.req("GET", "/healthz")
        dt = time.time() - t0
        check("40 次请求复用同一连接无阻塞", dt < 5, f"{dt*1000:.0f} ms")

        print("\n【3】分块上传：小文件 / 0 字节 / 大文件")
        tiny = os.path.join(DATA_DIR, "tiny.txt")
        with open(tiny, "wb") as f:
            f.write("hello 局域网".encode() * 1000)
        r = upload(mc, tiny, f"{TAG}-tiny.txt")
        ok = r["ok"] and sha256_file(local_path(r["name"])) == sha256_file(tiny)
        check("小文件上传 sha256 一致", ok, str(r)[:140])

        zero = os.path.join(DATA_DIR, "zero.bin")
        open(zero, "wb").close()
        r = upload(mc, zero, f"{TAG}-zero.bin")
        check("0 字节文件上传", r["ok"], str(r)[:140])

        big = os.path.join(DATA_DIR, "big.bin")
        if not os.path.exists(big) or os.path.getsize(big) != BIG:
            print(f"     生成 {BIG // 1024 // 1024} MB 本地测试文件…")
            make_file(big, BIG, seed=20261011)
        samples, stop = [], threading.Event()
        poller = threading.Thread(target=rss_poller, args=(stop, samples), daemon=True)
        poller.start()
        t0 = time.time()
        r = upload(mc, big, f"{TAG}-big.bin")
        dt = max(0.001, time.time() - t0)
        stop.set()
        poller.join(timeout=3)
        ok = r["ok"] and sha256_file(local_path(r["name"])) == sha256_file(big)
        check(f"{BIG // 1024 // 1024}MB 上传 sha256 一致", ok, str(r)[:140])
        print(f"     上传耗时 {dt:.1f}s，平均 {BIG / 1024 / 1024 / dt:.0f} MB/s")
        served = r.get("name") or ""
        peak = max(samples) / 1024 / 1024 if samples else 0
        check("传输期间服务端内存恒定", 0 < peak < 300,
              f"采样 {len(samples)} 次，峰值 {peak:.1f} MB")

        print("\n【4】Range 分段（在线播放拖动进度条的基础）")
        target = local_path(served)
        total = os.path.getsize(target)
        q = urllib.parse.quote(served)
        with open(target, "rb") as f:
            head = f.read(1024)

        st, hd, body = mc.req("GET", f"/f/{q}", headers={"Range": "bytes=0-1023"})
        check("首段 Range 返回 206 且字节精确",
              st == 206 and hd.get("Content-Range") == f"bytes 0-1023/{total}" and body == head,
              f"status={st} range={hd.get('Content-Range')}")

        st, hd, body = mc.req("GET", f"/f/{q}", headers={"Range": f"bytes={total}-5"})
        check("越界 Range 返回 416",
              st == 416 and hd.get("Content-Range") == f"bytes */{total}", f"status={st}")

        bad = 0
        for _ in range(5):
            start = random.randrange(0, max(1, total - 200000))
            length = random.randrange(1000, 200000)
            st, hd, body = mc.req("GET", f"/f/{q}",
                                  headers={"Range": f"bytes={start}-{start + length - 1}"})
            with open(target, "rb") as f:
                f.seek(start)
                expect = f.read(length)
            if st != 206 or body != expect:
                bad += 1
                break
        check("5 个随机区间字节逐一比对一致", bad == 0)

        st, hd, body = mc.req("GET", f"/f/{q}", headers={"Range": "bytes=-4096"})
        with open(target, "rb") as f:
            f.seek(total - 4096)
            expect = f.read()
        check("后缀区间 bytes=-4096 正确", st == 206 and body == expect, f"status={st}")

        st, hd, body = mc.req("GET", f"/f/{q}", headers={"Range": f"bytes={total - 100}-"})
        check("开放区间 bytes=N- 正确", st == 206 and len(body) == 100, f"len={len(body)}")

        st, hd, body = mc.req("HEAD", f"/f/{q}")
        check("HEAD 返回长度且无响应体",
              st == 200 and hd.get("Content-Length") == str(total) and body == b"")

        st, hd, body = mc.req("GET", f"/f/{q}")
        check("整体下载 200 + 长度正确", st == 200 and len(body) == total)

        print("\n【5】断点续传（并发乱序写入后的续传偏移）")
        mid = os.path.join(DATA_DIR, "mid.bin")
        if not os.path.exists(mid) or os.path.getsize(mid) != MID:
            make_file(mid, MID, seed=777)
        half = (MID // CHUNK) // 2
        r1 = upload(mc, mid, f"{TAG}-mid.bin", limit_chunks=half)
        check("前半部分分块写入成功", r1["ok"], str(r1)[:140])
        st, init2 = mc.json("POST", "/api/upload/init", {
            "name": f"{TAG}-mid.bin", "size": MID,
            "lastModified": int(os.path.getmtime(mid) * 1000),
            "key": f"{TAG}-mid.bin|{MID}|{int(os.path.getmtime(mid) * 1000)}"})
        check("续传偏移 = 已连续覆盖前缀", init2["offset"] == half * CHUNK,
              f"offset={init2['offset']} 期望={half * CHUNK}")
        r2 = upload(mc, mid, f"{TAG}-mid.bin")
        ok = r2["ok"] and sha256_file(local_path(r2["name"])) == sha256_file(mid)
        check("续传完成后 sha256 一致", ok, str(r2)[:160])

        print("\n【6】多客户端并发下载")
        def dl(_):
            c = Client()
            st, hd, body = c.req("GET", f"/f/{q}")
            c.close()
            return st, len(body)

        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(dl, range(4)))
        dt = max(0.001, time.time() - t0)
        check("4 路并发下载完整文件均正确",
              all(st == 200 and n == total for st, n in results),
              f"{dt:.1f}s 合计 {BIG * 4 / 1024 / 1024 / dt:.0f} MB/s")

        print("\n【7】安全与异常参数")
        st, data2 = mc.json("POST", "/api/delete", {"name": "../../etc/passwd"})
        check("删除接口拒绝路径穿越", st == 200 and not data2.get("deleted"), str(data2)[:100])
        st, data2 = mc.json("POST", "/api/upload/init", {"name": "x", "size": 10 ** 15})
        check("磁盘空间不足被拒绝", st in (507, 413), f"status={st} {str(data2)[:70]}")
        st, _, _ = mc.req("POST", "/api/upload/chunk?uid=zzzz&offset=0", b"x")
        check("非法上传会话被拒绝", st in (400, 404), f"status={st}")

        print("\n【8】批量打包下载（zip 流式）")
        z1, z2, z3 = f"{TAG}-zip-a.bin", f"{TAG}-zip-中文.txt", f"{TAG}-zip-zero.bin"
        p1 = make_file(DATA_DIR + "/za.bin", 3 * 1024 * 1024 + 77, seed=11)
        p2 = make_file(DATA_DIR + "/zb.txt", 12345, seed=22)
        p3 = make_file(DATA_DIR + "/zc.bin", 0, seed=33)
        for src, name in ((p1, z1), (p2, z2), (p3, z3)):
            r = upload(mc, src, name)
            if not r.get("ok"):
                check(f"打包前置：上传 {name}", False, str(r)[:120])
        st, hd, blob = mc.req("GET",
                              "/api/zip?names=" + urllib.parse.quote(f"{z1}\n{z2}\n{z3}"))
        check("打包接口返回 200", st == 200, f"status={st}")
        check("Content-Type 为 application/zip",
              "application/zip" in hd.get("Content-Type", ""), hd.get("Content-Type"))
        check("使用 chunked 流式传输（无需预知总大小）",
              hd.get("Transfer-Encoding", "").lower() == "chunked",
              hd.get("Transfer-Encoding"))
        check("响应头带 attachment 下载文件名", "attachment" in hd.get("Content-Disposition", ""))
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            entries = zf.namelist()
            check("zip 结构有效（CRC 全部通过）", zf.testzip() is None)
            check("三个文件全部打包（含中文名）",
                  entries == [z1, z2, z3], str(entries))
            ok_bytes = all(sha256_bytes(zf.read(n)) == sha256_file(local_path(n))
                           for n in (z1, z2, z3))
            check("zip 内字节与源文件 sha256 一致", ok_bytes)
        except zipfile.BadZipFile as exc:
            check("zip 结构有效（CRC 全部通过）", False, str(exc))

        st, _, _ = mc.req("GET", "/api/zip?names=" + urllib.parse.quote("../etc/passwd"))
        check("打包接口拒绝路径穿越", st == 400, f"status={st}")
        st, _, _ = mc.req("GET", "/api/zip")
        check("没有可打包文件时返回 400", st == 400, f"status={st}")
        st, _, _ = mc.req("GET", "/api/zip?names=" + urllib.parse.quote(".hidden"))
        check("隐藏文件不可打包", st == 400, f"status={st}")

        print("\n【9】列表元信息与删除")
        st, data2 = mc.json("GET", "/api/list")
        names = [f["name"] for f in data2["files"]]
        check("列表包含已上传文件", served in names and f"{TAG}-tiny.txt" in names)
        entry = next((f for f in data2["files"] if f["name"] == served), None)
        check("元信息（大小 / 类型 / 时间）正确",
              entry is not None and entry["size"] == total and entry["mtime"] > 0,
              str(entry)[:140])
        targets = [n for n in names if n.startswith(TAG)]
        st, data2 = mc.json("POST", "/api/delete", {"names": targets})
        check("批量删除成功", st == 200 and len(data2["deleted"]) == len(targets),
              str(data2)[:140])
        leftover = [n for n in targets if os.path.exists(local_path(n))]
        check("删除后磁盘上确实不存在", not leftover, str(leftover))

        print("\n【10】访问密码：环境变量注入与优先级")
        auth_dir = os.path.join(DATA_DIR, "authdir")
        os.makedirs(auth_dir, exist_ok=True)

        # 10.1 只给环境变量（autostart.sh 的 systemd EnvironmentFile 就是这么传密码的）
        proc = None
        try:
            pport = free_port()
            proc = spawn_server(pport, auth_dir, env_password="envpass123")
            ready = wait_ready(pport)
            check("仅靠环境变量 LANSHARE_PASSWORD 即可启用鉴权", ready, f"port={pport}")
            if ready:
                st, _, _ = http_probe(pport, "GET", "/api/list")
                check("未登录访问 /api/list 被拒", st == 401, f"status={st}")
                st, _, _ = http_probe(pport, "POST", "/api/login", {"password": "wrong"})
                check("错误密码登录被拒", st == 401, f"status={st}")
                st, hd, _ = http_probe(pport, "POST", "/api/login", {"password": "envpass123"})
                cookie = (hd.get("Set-Cookie") or "").split(";")[0]
                check("正确密码登录成功并下发 ls_auth Cookie",
                      st == 200 and cookie.startswith("ls_auth="), f"status={st}")
                st, _, _ = http_probe(pport, "GET", "/api/list", cookie=cookie)
                check("携带 Cookie 后 /api/list 放行", st == 200, f"status={st}")
        finally:
            stop_server(proc)

        # 10.2 命令行参数必须压过环境变量（既有 CLI 语义不能被新增的回落逻辑削弱）
        proc = None
        try:
            cport = free_port()
            proc = spawn_server(cport, auth_dir,
                                env_password="envpass123", cli_password="clipass456")
            if wait_ready(cport):
                st_cli, _, _ = http_probe(cport, "POST", "/api/login", {"password": "clipass456"})
                st_env, _, _ = http_probe(cport, "POST", "/api/login", {"password": "envpass123"})
                check("命令行 --password 优先于环境变量", st_cli == 200, f"status={st_cli}")
                check("命令行优先时环境变量里的密码失效", st_env == 401, f"status={st_env}")
            else:
                check("命令行 --password 优先于环境变量", False, "实例未就绪")
                check("命令行优先时环境变量里的密码失效", False, "实例未就绪")
        finally:
            stop_server(proc)

    finally:
        print("\n清理自检数据…")
        cleanup(mc)
        mc.close()
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
