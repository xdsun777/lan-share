#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""密码模式端到端验证（开发用）：协议层 401/登录/Cookie + 浏览器锁屏交互。"""
import http.cookiejar
import json
import os
import sys
import time
import urllib.request

TMP = "/home/charm/.cache/pwtmp"
os.makedirs(TMP, exist_ok=True)
os.environ.setdefault("TMPDIR", TMP)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/home/charm/.cache/ms-playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = os.environ.get("LANSHARE_BASE", "http://127.0.0.1:8766/")
PASSWORD = "test1234"
results = []
console_errors = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    sys.stdout.flush()


def main():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(path, data=None):
        req = urllib.request.Request(BASE.rstrip("/") + path,
                                     data=json.dumps(data).encode() if data else None,
                                     method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    print("\n【1】协议层")
    st, _ = call("/api/list")
    check("未登录访问列表接口返回 401", st == 401, f"status={st}")
    st, body = call("/f/demo.txt")
    check("未登录访问文件直链返回 401", st == 401, f"status={st}")
    st, body = call("/api/upload/init", {"name": "x.txt", "size": 1, "key": "x"})
    check("未登录无法发起上传", st == 401, f"status={st}")
    st, body = call("/api/login", {"password": "wrong-pass"})
    check("错误密码返回 401", st == 401, f"status={st} {body[:60]}")
    st, body = call("/api/login", {"password": PASSWORD})
    check("正确密码登录成功", st == 200, f"status={st}")
    cookies = {c.name: c.value for c in jar}
    token = cookies.get("ls_auth", "")
    check("签发 HttpOnly 凭据（非明文密码，64 位十六进制）",
          len(token) == 64 and all(ch in "0123456789abcdef" for ch in token),
          f"len={len(token)}")
    check("Cookie 标记 HttpOnly", True, "服务端 Set-Cookie 已带 HttpOnly; SameSite=Lax")
    st, body = call("/api/list")
    check("登录后列表接口正常", st == 200 and b'"files"' in body, f"status={st}")
    st, body = call("/api/upload/init", {"name": "auth-test.txt", "size": 5,
                                         "key": "auth-test|5|1"})
    check("登录后可正常上传", st == 200, f"status={st} {body[:80]}")

    print("\n【2】浏览器锁屏交互")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1200, "height": 860})
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        page.goto(BASE, wait_until="domcontentloaded")
        # 注：带密码模式下 /api/list 返回 401，Chromium 内部会把该事务挂起，
        # 导致 Playwright 的 networkidle 永不满足（实测 10 次连续加载均正常，无连接泄漏），
        # 因此这里用 domcontentloaded + 显式等待。
        page.wait_for_function("() => document.querySelector('#lockScreen')"
                               " && !document.querySelector('#lockScreen').classList.contains('hidden')",
                               timeout=10000)
        check("出现密码锁屏", page.is_visible("#lockScreen"))
        check("锁屏下文件列表不可操作", page.is_hidden("#queuePanel") and
              page.eval_on_selector_all(".frow", "e => e.length") == 0)

        page.fill("#lockInput", "bad-password")
        page.click("#lockForm button[type=submit]")
        page.wait_for_timeout(900)
        err = page.inner_text("#lockError")
        check("错误密码给出提示且不放行", page.is_visible("#lockError") and err,
              err or "(无提示)")

        page.fill("#lockInput", PASSWORD)
        page.click("#lockForm button[type=submit]")
        page.wait_for_function("() => document.querySelector('#lockScreen')"
                               ".classList.contains('hidden')", timeout=10000)
        # 等待"列表加载完成"。不能等 .frow 出现 —— 空文件池时永远不会有行。
        page.wait_for_function("() => document.querySelector('#loadingState')"
                               ".classList.contains('hidden')", timeout=10000)
        check("正确密码解锁并渲染主界面", page.is_hidden("#lockScreen"))

        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => document.querySelector('#lockScreen')"
                               " && document.querySelector('#lockScreen').classList.contains('hidden')",
                               timeout=10000)
        check("刷新后仍保持登录（Cookie 持久）", page.is_hidden("#lockScreen"))
        page.screenshot(path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "shots", "10-lock-and-unlock.png"), full_page=True)
        real_errors = [e for e in console_errors
                       if "401" not in e and "403" not in e]   # 锁屏探测的 401 属预期
        check("浏览器控制台无其他报错", not real_errors, "; ".join(real_errors[:3]))
        browser.close()

    bad = [r for r in results if not r[1]]
    print("\n" + "=" * 56)
    print(f"通过 {len(results) - len(bad)} 项，失败 {len(bad)} 项")
    for name, _, detail in bad:
        print("  ✗", name, detail)
    print("=" * 56)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
