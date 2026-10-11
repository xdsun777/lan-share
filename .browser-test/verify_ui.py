#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
浏览器端 UI 验证（开发用）
真实走一遍：多文件并发上传 → 去重改名 → 大文件进度与取消 → 视频在线播放/拖进度条
→ 图片/文本预览 → 搜索排序 → 地址面板 → 移动端视口，并断言控制台无报错。

运行前提
  1. 服务已启动：python3 server.py
  2. 本目录下有测试素材（脚本不会自动生成）：
       ffmpeg -f lavfi -i "testsrc2=size=1280x720:rate=25:duration=150" \
              -f lavfi -i "sine=frequency=440:duration=150" \
              -c:v libx264 -preset ultrafast -crf 28 -pix_fmt yuv420p \
              -c:a aac -b:a 64k -shortest -movflags +faststart demo.mp4
       ffmpeg -f lavfi -i "sine=frequency=523:duration=6" -c:a libmp3lame -q:a 3 demo.mp3
       （demo.png / notes.md 任意图片与文本即可）
  3. Python 侧依赖 playwright（复用系统 Chromium，见脚本内环境变量）
"""
import json
import os
import sys
import time
import urllib.request
import zipfile

TMP = "/home/charm/.cache/pwtmp"
os.makedirs(TMP, exist_ok=True)
os.environ.setdefault("TMPDIR", TMP)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/home/charm/.cache/ms-playwright")

from playwright.sync_api import sync_playwright  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SHOTS = os.path.join(HERE, "shots")
PARTS = os.path.join(HERE, "..", "uploads", ".parts")
POOL = os.path.abspath(os.path.join(
    HERE, "..", os.environ.get("LANSHARE_DIR", "uploads")))  # 被测实例的存储目录
BASE = os.environ.get("LANSHARE_BASE", "http://127.0.0.1:8765/")
SMALL = ["demo.mp4", "demo.mp3", "demo.png", "notes.md"]
BIG = os.path.join(HERE, "big-upload.bin")


def local_pool(name):
    """被测实例的存储目录里同名文件（ui 测试实例 --dir 需指向 lan-share/uploads）"""
    return os.path.join(POOL, name)

console_errors = []
range_requests = []
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    sys.stdout.flush()


def api(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE.rstrip("/") + path, data=data,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def wait_for_files(page, names, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        got = page.eval_on_selector_all(".fname", "els => els.map(e => e.textContent)")
        if all(n in got for n in names):
            return True
        time.sleep(0.4)
    return False


def preview(page, name):
    page.eval_on_selector_all(".frow", """(els, name) => {
        const row = els.find(e => e.querySelector('.fname').textContent === name);
        row.querySelector('.btn').click();
    }""", name)


def main():
    if not os.path.exists(BIG):
        with open(BIG, "wb") as f:      # 稀疏文件，瞬间生成
            f.truncate(1536 * 1024 * 1024)
    os.makedirs(SHOTS, exist_ok=True)

    # 归零：清空文件池，保证每次运行结果可复现
    existing = [f["name"] for f in api("/api/list")["files"]]
    if existing:
        api("/api/delete", {"names": existing})
    assert not api("/api/list")["files"], "文件池未清空"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 960},
                                  device_scale_factor=1.4)
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))
        page.on("request", lambda r: range_requests.append(r.headers.get("range"))
                if "/f/demo.mp4" in r.url and r.headers.get("range") else None)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(400)

        print("\n【1】首屏")
        check("页面标题正确", page.title() == "局域网文件中转站", page.title())
        line = page.inner_text("#serverLine")
        check("顶栏显示访问地址 / 运行时长 / 磁盘", "当前地址" in line and "磁盘剩余" in line, line)
        check("未设密码时不出现锁屏", page.is_hidden("#lockScreen"))
        check("空态提示可见", "这里还没有文件" in page.inner_text("#emptyState"))
        page.screenshot(path=os.path.join(SHOTS, "01-empty.png"), full_page=True)

        print("\n【2】多文件并发上传")
        page.set_input_files("#fileInput", [os.path.join(HERE, n) for n in SMALL])
        page.wait_for_timeout(600)
        n_items = page.eval_on_selector_all(".qitem", "els => els.length")
        check("4 个文件进入上传队列", n_items == 4, f"{n_items} 项")
        page.screenshot(path=os.path.join(SHOTS, "02-uploading.png"), full_page=True)
        check("4 个文件上传完成并出现在列表", wait_for_files(page, SMALL))
        page.wait_for_timeout(400)

        rows = page.eval_on_selector_all(".frow", """els => els.map(e => ({
            name: e.querySelector('.fname').textContent,
            meta: e.querySelector('.fmeta').textContent,
            icon: e.querySelector('.ficon').className}))""")
        by_name = {r["name"]: r for r in rows}
        check("按类型渲染图标（视频/音频/图片/文本）",
              all(k in by_name for k in SMALL)
              and "video" in by_name["demo.mp4"]["icon"] and "audio" in by_name["demo.mp3"]["icon"]
              and "image" in by_name["demo.png"]["icon"] and "text" in by_name["notes.md"]["icon"])
        check("体积与时间正常显示", "86.1 MB" in by_name["demo.mp4"]["meta"],
              by_name["demo.mp4"]["meta"])

        print("\n【3】同名文件自动改名")
        page.set_input_files("#fileInput", [os.path.join(HERE, "demo.png")])
        ok = wait_for_files(page, "demo (1).png".split("|"))
        check("重复上传生成 (1) 后缀而非覆盖", ok,
              str(page.eval_on_selector_all(".fname", "e => e.map(x => x.textContent)")))

        print("\n【4】大文件上传进度与取消")
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 10,
            "downloadThroughput": -1, "uploadThroughput": 40 * 1024 * 1024})
        page.set_input_files("#fileInput", [BIG])
        page.wait_for_timeout(2500)
        stat = page.inner_text(".qitem:last-child .qstat")
        bar_w = page.eval_on_selector(".qitem:last-child .qbar > i", "e => e.style.width")
        check("显示已传/总量、速度与剩余时间",
              "MB/s" in stat and "剩余" in stat and "GB" in stat, f"{stat}｜进度 {bar_w}")
        page.screenshot(path=os.path.join(SHOTS, "03-bigfile-queue.png"), full_page=True)
        page.click(".qitem:last-child .qbtn-cancel", timeout=5000)
        page.wait_for_timeout(1500)
        state = page.inner_text(".qitem:last-child .state")
        check("取消按钮生效", "取消" in state, state)
        names = page.eval_on_selector_all(".fname", "els => els.map(e => e.textContent)")
        check("取消后不会产生半成品文件", "big-upload.bin" not in names, str(names))
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 0,
            "downloadThroughput": -1, "uploadThroughput": -1})

        print("\n【5】视频在线播放与拖动进度条")
        served_before = api("/api/list")["stats"]["served_bytes"]
        video_size = os.path.getsize(os.path.join(HERE, "demo.mp4"))
        preview(page, "demo.mp4")
        page.wait_for_selector("#previewBody video", timeout=10000)
        page.wait_for_function("() => { const v = document.querySelector('#previewBody video');"
                               "return v && v.readyState >= 2 && v.videoWidth > 0; }", timeout=30000)
        info = page.eval_on_selector("#previewBody video",
                                     "v => ({w: v.videoWidth, h: v.videoHeight, d: v.duration})")
        check("视频元数据加载（86MB 文件未下载完即可播放）",
              info["w"] == 1280 and info["h"] == 720 and info["d"] > 140,
              f"{info['w']}x{info['h']} 时长 {info['d']:.1f}s")

        target = info["d"] - 15
        page.eval_on_selector("#previewBody video", "(v, t) => { v.currentTime = t; }", target)
        page.wait_for_function(
            "(t) => Math.abs(document.querySelector('#previewBody video').currentTime - t) < 2.0",
            arg=target, timeout=20000)
        now = page.eval_on_selector("#previewBody video", "v => v.currentTime")
        served_after = api("/api/list")["stats"]["served_bytes"]
        delta = served_after - served_before
        check(f"拖动到 {target:.0f}s 后正常解码续播",
              abs(now - target) < 2.0, f"currentTime={now:.1f}s")
        check("拖动进度条走的是字节范围请求（未整文件重下）",
              len(range_requests) >= 1,
              f"Range 请求 {len(range_requests)} 个（{range_requests[:3]}），"
              f"本次服务端共送出 {delta/1024/1024:.1f}MB")
        page.wait_for_timeout(500)
        page.screenshot(path=os.path.join(SHOTS, "04-video-preview.png"))

        print("\n【6】图片与文本预览")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        preview(page, "demo.png")
        page.wait_for_selector("#previewBody img", timeout=8000)
        natural = page.eval_on_selector("#previewBody img",
                                        "i => ({w: i.naturalWidth, h: i.naturalHeight})")
        check("图片按原始尺寸渲染", natural == {"w": 960, "h": 600}, str(natural))
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, "05-image-preview.png"))

        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        preview(page, "notes.md")
        page.wait_for_selector("#previewBody pre", timeout=8000)
        page.wait_for_timeout(700)
        check("文本预览渲染内容", "中转站使用说明" in page.inner_text("#previewBody pre"))
        page.screenshot(path=os.path.join(SHOTS, "06-text-preview.png"))
        page.keyboard.press("Escape")

        print("\n【7】搜索与排序")
        page.fill("#searchInput", "notes")
        page.wait_for_timeout(300)
        check("搜索过滤生效",
              page.eval_on_selector_all(".frow", "e => e.length") == 1
              and "筛选出 1" in page.inner_text("#poolSummary"),
              page.inner_text("#poolSummary"))
        page.fill("#searchInput", "")
        page.select_option("#sortSelect", "size-desc")
        page.wait_for_timeout(300)
        order = page.eval_on_selector_all(".fname", "els => els.map(e => e.textContent)")
        check("按体积倒序排序生效", order[0] == "demo.mp4", str(order))
        page.select_option("#sortSelect", "name-asc")
        page.wait_for_timeout(300)
        order = page.eval_on_selector_all(".fname", "els => els.map(e => e.textContent)")
        check("按名称排序生效", order[0] == "demo (1).png", str(order))
        page.select_option("#sortSelect", "time-desc")

        print("\n【8】访问地址面板")
        page.click("#btnAddresses")
        page.wait_for_timeout(300)
        addrs = page.inner_text("#addressList")
        check("列出局域网访问地址并标记当前", ("192.168." in addrs or "10." in addrs)
              and "当前" in addrs, addrs.replace("\n", " "))
        page.screenshot(path=os.path.join(SHOTS, "07-addresses.png"), full_page=True)
        page.click("#btnAddresses")
        page.wait_for_timeout(200)
        page.screenshot(path=os.path.join(SHOTS, "08-desktop-list.png"), full_page=True)

        print("\n【9】批量打包下载（勾选 → 打包 → 真实下载校验）")
        page.click("#btnSelectAll")
        page.wait_for_function(
            "() => document.querySelectorAll('.fpick:checked').length"
            " === document.querySelectorAll('.fpick').length", timeout=5000)
        pick_n = page.eval_on_selector_all(".fpick", "e => e.length")
        check("全选后复选框全部勾上", pick_n >= 5, f"picked={pick_n}")
        zip_label = page.inner_text("#btnZip").strip()
        check("打包按钮显示数量且可用",
              not page.is_disabled("#btnZip") and f"（{pick_n} 个）" in zip_label, zip_label)
        page.screenshot(path=os.path.join(SHOTS, "13-zip-bar.png"), full_page=True)

        # 反选：再点一次全选应当清空
        page.click("#btnSelectAll")
        page.wait_for_function("() => document.querySelectorAll('.fpick:checked').length === 0",
                               timeout=5000)
        check("再次全选可清空勾选", page.is_disabled("#btnZip"))
        page.screenshot(path=os.path.join(SHOTS, "13-zip-bar-empty.png"), full_page=True)

        # 勾选前两个文件后打包
        two = page.eval_on_selector_all(
            ".fpick", "els => els.slice(0, 2).map(e => e.dataset.name)")
        for name in two:
            page.eval_on_selector_all(
                ".fpick", """(els, n) => {
                    const cb = els.find(e => e.dataset.name === n);
                    cb.click();
                }""", name)
        page.wait_for_function(
            "() => document.querySelectorAll('.fpick:checked').length === 2", timeout=5000)

        with page.expect_download(timeout=60000) as dl_info:
            page.click("#btnZip")
        dl = dl_info.value
        check("浏览器收到真实下载（.zip）",
              dl.suggested_filename.endswith(".zip"), dl.suggested_filename)
        tmp_zip = os.path.join(SHOTS, "_zipcheck.zip")
        dl.save_as(tmp_zip)
        with zipfile.ZipFile(tmp_zip) as zf:
            names = zf.namelist()
            check("下载的 zip 含所选 2 个文件", sorted(names) == sorted(two),
                  f"{names} vs {two}")
            check("zip CRC 校验通过", zf.testzip() is None)
            ok_bytes = all(zf.read(n) == open(local_pool(n), "rb").read()
                           for n in names if not n.endswith("/"))
            check("zip 内容与磁盘文件字节一致", ok_bytes)
        os.remove(tmp_zip)
        check("下载开始后清空勾选",
              page.eval_on_selector_all(".fpick:checked", "e => e.length") == 0)

        print("\n【10】移动端视口")
        m = ctx.new_page()
        m.set_viewport_size({"width": 390, "height": 844})
        m.goto(BASE, wait_until="networkidle")
        m.wait_for_timeout(700)
        check("移动端渲染文件列表", m.eval_on_selector_all(".frow", "e => e.length") >= 5)
        m.screenshot(path=os.path.join(SHOTS, "09-mobile.png"), full_page=True)
        m.close()

        check("浏览器控制台无报错", not console_errors, "; ".join(console_errors[:3]))
        browser.close()

    print("\n【11】收尾清理")
    names = [f["name"] for f in api("/api/list")["files"]]
    api("/api/delete", {"names": names})
    left = api("/api/list")["files"]
    parts = os.listdir(PARTS) if os.path.isdir(PARTS) else []
    check("文件池已清空", not left, str(left))
    check("取消的上传没有留下分片垃圾", not parts, str(parts))

    print("\n" + "=" * 62)
    bad = [r for r in results if not r[1]]
    print(f"通过 {len(results) - len(bad)} 项，失败 {len(bad)} 项")
    for name, _, detail in bad:
        print("  ✗", name, detail)
    print("截图目录:", SHOTS)
    print("=" * 62)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
