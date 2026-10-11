#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫码加入 · 端到端验证（开发用）

二维码现在由浏览器用 vendored 的 qrcode.js 现场生成，服务端不参与，
所以验证也要"从前端产物反推"：

  1. 浏览器里点「扫码」→ 从生成的 SVG / <path> 反解出模块矩阵；
  2. 把这个矩阵交给**独立解码器 zxing-cpp** 真扫一遍，确认扫出来就是那条地址；
  3. 顺带断言服务端确实没有二维码接口、也没有第三方 Python 依赖。

为什么不能拿 segno 的矩阵逐格比对？
  两个实现都合规，但**掩码选择**不保证一致（实测本例 29x29 中有 252 格不同、
  格式信息位也不同）。掩码不同 ≠ 错，所以"逐字节比对"在这里是伪判据；
  真正的判据是"扫得出来，且内容正确"。

运行前提
  1. 服务已启动（不含二维码接口的新版本）：
       python3 server.py --port 8766
  2. 依赖 playwright（复用系统 Chromium）+ zxing-cpp（仅测试用，不是运行时依赖）。
"""
import os
import sys

TMP = "/home/charm/.cache/pwtmp"
os.makedirs(TMP, exist_ok=True)
os.environ.setdefault("TMPDIR", TMP)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/home/charm/.cache/ms-playwright")

import numpy as np                                # noqa: E402
from PIL import Image                             # noqa: E402
from playwright.sync_api import sync_playwright    # noqa: E402

try:
    import zxingcpp
except ImportError:
    print("需要 zxing-cpp 作为独立解码裁判：pip install zxing-cpp")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOTS = os.path.join(HERE, "shots")
os.makedirs(SHOTS, exist_ok=True)

LAN = os.environ.get("LAN_BASE", "http://192.168.31.220:8766/")
LOOP = os.environ.get("LOOP_BASE", "http://127.0.0.1:8766/")

console_errors = []
results = []

# 浏览器侧：把二维码 SVG 反解成 {viewBox, 暗块坐标, 尺寸}
EXTRACT_JS = """() => {
  const svg = document.querySelector('#qrBox svg');
  if (!svg) return null;
  const vb = (svg.getAttribute('viewBox') || '').trim().split(/\\s+/).map(Number);
  const path = svg.querySelector('path');
  const d = path ? (path.getAttribute('d') || '') : '';
  const pts = [];
  const re = /M(-?[0-9.]+),(-?[0-9.]+)l/g;
  let m;
  while ((m = re.exec(d)) !== null) pts.push([Number(m[1]), Number(m[2])]);
  const box = svg.getBoundingClientRect();
  return {
    vb: vb,
    pts: pts,
    rects: svg.querySelectorAll('rect').length,
    subpaths: (d.match(/z/g) || []).length,
    w: Math.round(box.width), h: Math.round(box.height),
    urlText: document.querySelector('#qrUrl').textContent.trim(),
  };
}"""


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  <-- {detail}" if detail and not ok else ""))
    sys.stdout.flush()


def open_panel(page, base):
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_selector(".frow, #emptyState", timeout=15000)
    page.click("#btnAddresses")
    page.wait_for_selector("#addressList li", timeout=8000)


def wait_hidden(page, selector, timeout=4000):
    """等到元素带上 hidden 类。不能用 wait_for_selector —— 隐藏元素永远不算 visible。"""
    page.wait_for_function(
        "sel => document.querySelector(sel).classList.contains('hidden')",
        arg=selector, timeout=timeout)


def rows_of(page):
    return page.eval_on_selector_all("#addressList li code",
                                     "els => els.map(e => e.textContent)")


def qr_row_index(page):
    return page.evaluate("""() => [...document.querySelectorAll('#addressList li')]
        .findIndex(r => r.querySelector('.qr-btn'))""")


def open_qr(page, idx):
    page.eval_on_selector_all(
        "#addressList li", "(els, i) => els[i].querySelector('.qr-btn').click()", idx)
    page.wait_for_selector("#qrModal:not(.hidden)", timeout=8000)


def svg_to_grid(info):
    """从 SVG 几何反推模块矩阵：cellSize / margin 都由数据本身推出来，不写死。"""
    pts = {(round(float(x)), round(float(y))) for x, y in info["pts"]}
    xs = sorted({p[0] for p in pts})
    if not xs:
        return None, None
    steps = [b - a for a, b in zip(xs, xs[1:])] or [1]
    cell = min(steps)
    margin = xs[0]
    total = info["vb"][2] if len(info["vb"]) == 4 else None
    if not total or total % cell:
        return None, None
    dim = int(total // cell - 2 * (margin // cell))
    if dim <= 0:
        return None, None
    grid = [[0] * dim for _ in range(dim)]
    for (x, y) in pts:
        c = int((x - margin) // cell)
        r = int((y - margin) // cell)
        if 0 <= r < dim and 0 <= c < dim:
            grid[r][c] = 1
    return grid, cell


def decode_grid(grid):
    """把模块矩阵画成位图，交给独立解码器 zxing-cpp 真扫。"""
    dim = len(grid)
    scale, quiet = 10, 4
    side = (dim + 2 * quiet) * scale
    img = Image.new("L", (side, side), 255)
    px = img.load()
    for r in range(dim):
        for c in range(dim):
            if grid[r][c]:
                x0, y0 = (c + quiet) * scale, (r + quiet) * scale
                for dy in range(scale):
                    for dx in range(scale):
                        px[x0 + dx, y0 + dy] = 0
    res = zxingcpp.read_barcode(np.array(img))
    if res is None:
        return None, None
    return res.text, str(res.format)


def main():
    base_lan, base_loop = LAN.rstrip("/") + "/", LOOP.rstrip("/") + "/"

    print("\n【1】服务端不应再有二维码接口与第三方依赖")
    server_py = open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()
    check("server.py 不含 segno", "segno" not in server_py)
    check("server.py 不再提供 /api/qrcode", "/api/qrcode" not in server_py)
    check("仓库已无 requirements.txt（无第三方运行依赖）",
          not os.path.exists(os.path.join(ROOT, "requirements.txt")))
    check("vendored 的 qrcode.js 保留 MIT 版权声明",
          "Kazuhiko Arase" in open(os.path.join(ROOT, "static", "vendor", "qrcode.js"),
                                   encoding="utf-8").read())

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--no-proxy-server"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 960},
                                  device_scale_factor=1.4)
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))

        print("\n【2】前端库可访问、页面已接入")
        page.goto(base_lan, wait_until="domcontentloaded")
        lib_status = page.evaluate("""async (u) => {
            const r = await fetch(u, {cache: 'no-store'});
            return r.status;
        }""", base_lan + "vendor/qrcode.js")
        check("/vendor/qrcode.js 可访问", lib_status == 200, f"status={lib_status}")
        check("页面已加载 qrcode 全局对象",
              page.evaluate("() => typeof qrcode === 'function'"))

        print("\n【3】从局域网地址进入并打开扫码")
        open_panel(page, base_lan)
        rows = rows_of(page)
        idx = qr_row_index(page)
        check("局域网地址行出现「扫码」按钮", idx >= 0, str(rows))
        check("回环地址不出现在列表里", not any("127.0.0.1" in r for r in rows), str(rows))
        if idx < 0:
            browser.close()
            return 1
        open_qr(page, idx)
        check("扫码弹层已打开", page.is_visible("#qrModal"))

        info = page.evaluate(EXTRACT_JS)
        check("弹层内是现场生成的 SVG（不再是 <img> 指向服务端）", info is not None)
        if not info:
            browser.close()
            return 1
        check("弹层显示的地址与所选行一致", info["urlText"] == rows[idx].strip(),
              f"{info['urlText']!r} vs {rows[idx]!r}")
        check("SVG 结构为 1 个背景矩形 + 与暗块数等量的子路径",
              info["rects"] == 1 and info["subpaths"] == len(info["pts"]),
              f"rects={info['rects']} subpaths={info['subpaths']} pts={len(info['pts'])}")

        print("\n【4】反解矩阵并交给独立解码器实扫")
        grid, cell = svg_to_grid(info)
        check("能从 SVG 几何反推出模块矩阵", grid is not None)
        if not grid:
            browser.close()
            return 1
        dim = len(grid)
        dark = sum(sum(r) for r in grid)
        check(f"矩阵尺寸合法（{dim}x{dim}，21+4k）",
              dim >= 21 and (dim - 21) % 4 == 0, f"dim={dim}")
        check("暗块占比合理（30%~75%）", 0.30 <= dark / (dim * dim) <= 0.75,
              f"{dark}/{dim * dim}")

        text, fmt = decode_grid(grid)
        check("独立解码器成功扫出内容", text is not None)
        check(f"扫码结果与地址完全一致（{text}）", text == base_lan, f"got {text!r}")
        check("识别类型为二维码", "QR" in (fmt or "").upper(), fmt)

        print("\n【5】二维码随容器缩放，不被裁切")
        check("渲染尺寸约 248px 见方",
              200 <= info["w"] <= 300 and 200 <= info["h"] <= 300,
              f"{info['w']}x{info['h']}")
        page.screenshot(path=os.path.join(SHOTS, "10-qr-modal.png"))
        print("     截图: shots/10-qr-modal.png")

        print("\n【6】三种关闭方式 + 不残留旧码")
        page.keyboard.press("Escape")
        wait_hidden(page, "#qrModal")
        check("Esc 可关闭", page.is_hidden("#qrModal"))
        check("关闭后清空 DOM，避免下次闪现旧码",
              page.evaluate("() => document.querySelector('#qrBox').children.length === 0"))
        open_qr(page, idx)
        page.click("#qrModal button[data-close='qrModal']")
        wait_hidden(page, "#qrModal")
        check("「关闭」按钮可关闭", page.is_hidden("#qrModal"))
        open_qr(page, idx)
        page.mouse.click(30, 30)
        wait_hidden(page, "#qrModal")
        check("点遮罩可关闭", page.is_hidden("#qrModal"))

        print("\n【7】从 127.0.0.1 进入：扫码入口只挂在局域网地址上")
        ctx2 = browser.new_context(viewport={"width": 1440, "height": 960})
        p2 = ctx2.new_page()
        p2.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))
        open_panel(p2, base_loop)
        rows2 = rows_of(p2)
        i2 = qr_row_index(p2)
        check("回环地址被视为「当前」", any("127.0.0.1" in r for r in rows2), str(rows2))
        check("仍有扫码入口", i2 >= 0, str(rows2))
        check("扫码入口不挂在回环地址上",
              "127.0.0.1" not in (rows2[i2] if i2 >= 0 else ""),
              rows2[i2] if i2 >= 0 else "")
        p2.screenshot(path=os.path.join(SHOTS, "11-qr-address-panel.png"))
        print("     截图: shots/11-qr-address-panel.png")

        print("\n【8】二维码库缺失时优雅降级（模拟旧服务端 404）")
        # 旧版本服务端的静态白名单没有 /vendor/qrcode.js，页面会拿不到库。
        # 此时应当直接不渲染扫码入口并提示重启，而不是等用户点了才弹错误。
        p2.evaluate("() => { window.qrcode = undefined; renderAddresses(); }")
        check("库缺失时不渲染任何扫码按钮",
              p2.evaluate("() => document.querySelectorAll('.qr-btn').length") == 0)
        hint = p2.inner_text("#addressHint")
        check("提示重启服务以获取最新页面", "二维码库加载失败" in hint, hint)
        p2.reload(wait_until="domcontentloaded")
        p2.wait_for_function(
            "() => typeof qrcode === 'function'", timeout=8000)
        check("刷新后库恢复，入口回归", True)
        ctx2.close()

        print("\n【9】手机视口下二维码完整可见且同样可扫")
        m = ctx.new_page()
        m.set_viewport_size({"width": 390, "height": 844})
        open_panel(m, base_lan)
        mi = qr_row_index(m)
        if mi >= 0:
            open_qr(m, mi)
        info_m = m.evaluate(EXTRACT_JS)
        check("手机视口下仍生成二维码", info_m is not None)
        if info_m:
            check("宽度未超出窄屏（<=340px）", 0 < info_m["w"] <= 340, f"width={info_m['w']}")
            gm, _ = svg_to_grid(info_m)
            tm = decode_grid(gm)[0] if gm else None
            check("手机视口下生成的码同样能扫出正确地址", tm == base_lan, f"got {tm!r}")
        m.screenshot(path=os.path.join(SHOTS, "12-qr-mobile.png"))
        print("     截图: shots/12-qr-mobile.png")
        m.close()

        check("浏览器控制台无报错", not console_errors, "; ".join(console_errors[:3]))
        browser.close()

    print("\n" + "=" * 62)
    bad = [r for r in results if not r[1]]
    print(f"通过 {len(results) - len(bad)} 项，失败 {len(bad)} 项")
    for name, _, detail in bad:
        print("  ✗", name, detail)
    print("=" * 62)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
