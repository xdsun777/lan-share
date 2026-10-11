# 局域网文件中转站（LAN Share）

在 PC 上跑一个零依赖的小服务，局域网内的 A、B 两端（电脑 / 手机 / 平板）用浏览器打开同一个地址，
就能互相上传、下载、**在线播放音视频**，文件都落在 PC 上，不走公网、不限速。
服务端零第三方依赖；手机扫码加入的二维码由浏览器现场生成（vendored 的 qrcode.js，MIT）。

```
        ┌──────────── PC（部署端，文件都存这里）────────────┐
        │   python3 server.py   →   http://192.168.x.x:8765 │
        └───────┬──────────────────────────────┬───────────┘
                │                              │
        ┌───────▼───────┐              ┌───────▼───────┐
        │  A 端（浏览器） │  ←─ 共享文件池 ─→ │  B 端（浏览器） │
        │  上传 / 下载    │              │  上传 / 预览 / 播放 │
        └───────────────┘              └───────────────┘
```

---

## 1. 两条硬需求是怎么做到的

### 需求一：在线预览音视频不卡顿

| 措施 | 说明 |
| --- | --- |
| **完整实现 HTTP Range（206）** | 播放器只请求需要的字节，点开即播、进度条可随便拖，不下载整个文件 |
| 支持 `bytes=a-b` / `bytes=a-` / `bytes=-n` 三种区间，越界回 `416` | 覆盖 Chrome / Edge / Safari / 手机浏览器的全部真实行为 |
| 流式传输，**1MB 固定缓冲区** | 服务端内存占用与文件大小无关，20GB 的片子也只占几 MB 内存 |
| HTTP/1.1 keep-alive + TCP_NODELAY | 消除小包延迟与连接重建抖动，无线网络下更稳 |
| 正确下发 `Content-Type` / `Accept-Ranges` / `Content-Length` | 浏览器识别为可流式播放的媒体源 |

> 说明：浏览器只能解码它自己支持的封装（MP4/H.264、WebM、MP3、FLAC 等）。
> MKV / AVI / RMVB 这类容器浏览器通常解不了，列表里会直接标注"建议下载后播放"，并提供一键下载，
> **不做转码、不做假承诺**。

### 需求二：大文件上传下载不卡顿

| 措施 | 说明 |
| --- | --- |
| **客户端分块上传**（按体积自适应 1MB～16MB 一块） | 单块失败只重传该块，不用从头上传 |
| **3 路并发**上传 | 跑满带宽；服务端每块用独立文件描述符 seek 写入，互不阻塞 |
| **断点续传** | 关闭页面 / 断网后重新选同一个文件，自动从断点继续 |
| 服务端记录"已接收字节区间" | 并发乱序写入也不会漏块；发布前逐区间校验，**绝不把半成品当成品** |
| 上传完成才原子改名（`os.replace`） | 别人不会下载到半个文件 |
| 失败自动重试 3 次 + 实时速度 / 剩余时间 | UI 不阻塞，进度可预期 |

---

## 2. 快速开始

### Windows

1. 安装 Python 3.8+（<https://www.python.org/downloads/>，安装时勾选 **Add Python to PATH**）。
2. 把整个 `lan-share` 文件夹拷到 PC 上，**双击 `start.bat`**。
3. 首次运行若弹出防火墙提示，选择 **允许访问**（专用网络即可）。
4. 窗口里会打印访问地址，例如 `http://192.168.1.20:8765/`，把它发给 A、B 端即可。

### Linux / macOS

```bash
cd lan-share
chmod +x start.sh
./start.sh
# 自定义端口与访问密码
./start.sh --port 9000 --password 1234
```

### 手机扫码加入

页面右上角「访问地址」里每条局域网地址都有**扫码**按钮，点开就是二维码，
手机相机 / 微信扫一扫直接进站。二维码在**浏览器里现场生成**（vendored 的
`qrcode.js`，随项目分发、离线可用），服务端没有二维码接口，也不需要装任何东西。

启动后会打印：

```
  存储目录 : /home/xxx/lan-share/uploads
  访问地址（把下面任意一条发给 A / B 端，或手机浏览器直接打开）：
    http://127.0.0.1:8765/        （本机）
    http://192.168.31.220:8765/
```

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-p, --port` | `8765` | 监听端口 |
| `-H, --host` | `0.0.0.0` | 监听地址，`0.0.0.0` 表示允许局域网访问 |
| `-d, --dir` | `./uploads` | 文件存储目录（可指向大容量磁盘） |
| `-w, --password` | 无 | 访问密码，设置后所有接口需登录（也可用环境变量 `LANSHARE_PASSWORD`） |
| `--max-upload` | `0`（不限） | 单文件大小上限，单位 MB |
| `-q, --quiet` | 关 | 不打印每条访问日志 |

---

## 3. 使用方式

- **上传**：把文件拖到页面任意位置（或点"上传文件"），支持多文件同时传；
  队列里能看到每个文件的速度、已传/总大小、剩余时间，可单独取消或重试。
- **预览**：列表里点"预览"——
  视频/音频直接在页面内播放（可拖动进度条），图片、PDF、文本（≤2MB）同样支持。
- **下载**：点"下载"；或点"复制链接"把该文件的直链发给对方（对方打开即下载）。
- **批量打包下载**：勾选多个文件（或点"全选"），点"打包下载（n 个）"即可得到一个 zip。
  服务端**边读边打包边发送**（chunked + ZIP_STORED，不落临时文件、内存恒定），
  总量超过 4GB 也没问题（ZIP64）；音视频本身已是压缩格式，不再二次压缩以省 CPU。
- **清理**：点"删除"移除文件；页脚会显示存储目录、文件总量、服务端运行时长与内存占用。

> 手机访问：直接把 `http://电脑IP:8765/` 输入手机浏览器，或点「访问地址」→「扫码」
> 用手机相机 / 微信扫一扫（需与电脑在同一局域网）。

---

## 4. 实测数据

在本机（NVMe SSD、8 核、回环网络）跑 `selftest.py`，46 项检查全部通过：

| 项目 | 结果 |
| --- | --- |
| 1GB 文件分块上传（3 路并发） | 冷启动 5.4s（约 **190 MB/s**）；文件已被系统缓存时 0.8s |
| 上传前后 sha256 校验 | 完全一致（1GB / 256MB / 15KB / 0 字节四种尺寸） |
| 传输 1GB 期间服务端常驻内存 | 峰值 **33.5 MB**，与文件大小无关 |
| 4 个客户端并发下载 1GB 文件 | 全部字节正确，合计约 **1.4～2 GB/s**（回环上限） |
| Range 分段 | 首段 / 随机 5 段 / `bytes=-4096` / `bytes=N-` 全部字节精确；越界正确返回 416 |
| 断点续传 | 中断后重新 init，偏移量精确等于"已连续覆盖前缀" |
| keep-alive | 40 次请求复用同一连接，耗时 12ms |

> 真实局域网里瓶颈是网线/Wi-Fi：千兆有线约 110 MB/s，Wi-Fi 5 约 30–60 MB/s，
> 此时 CPU 与内存都不是瓶颈（服务端单文件传输只占约 30MB 内存）。
> 用下面的自检脚本可以在你自己的机器与网络上复现这些数字。

### 自检脚本

```bash
# 一个终端启动服务
python3 server.py

# 另一个终端运行自检（会生成并自动清理约 1.3GB 临时数据）
python3 selftest.py
python3 selftest.py --port 9000 --big-mb 3072    # 换端口 / 换测试体积
```

> 第 10 节（访问密码）会自己在本机随机端口拉起两个临时实例，因此**不需要**主实例带 `--password`；
> 跑完之后临时实例会被回收，端口不留残留。

除协议自检外，仓库还附带三套真实浏览器验证（Playwright + Chromium，开发期使用）：

| 脚本 | 覆盖 | 项数 |
| --- | --- | --- |
| `selftest.py` | 接口/上传/Range/续传/并发/安全/zip 打包/密码注入 | 46 |
| `.browser-test/verify_ui.py` | 页面渲染、多文件上传、进度条、视频拖动播放、图片/文本预览、搜索排序、**批量打包下载**、移动端 | 33 |
| `.browser-test/verify_auth.py` | `--password` 模式：401 拦截、登录、Cookie 持久、锁屏交互 | 15 |
| `.browser-test/verify_qr_ui.py` | 扫码：从前端 SVG 反解矩阵 → **独立解码器 zxing-cpp 实扫**校验内容、入口规则、库缺失降级、弹层交互、移动端 | 33 |

浏览器验证的运行方式（需先启动服务，端口可用 `LANSHARE_BASE` 环境变量指定）：

```bash
python3 server.py --port 8766
LANSHARE_BASE=http://127.0.0.1:8766/ python3 .browser-test/verify_ui.py
LANSHARE_BASE=http://127.0.0.1:8766/ python3 .browser-test/verify_qr_ui.py
# verify_qr_ui.py 需要独立解码器做裁判（仅测试用）：pip install zxing-cpp
```

---

## 5. 常见问题

**其他设备打不开**

1. 确认两台设备连的是同一个局域网（同一个 Wi-Fi / 路由器，注意别连到来宾网络或手机热点）。
2. 放行防火墙端口：
   - Windows：控制面板 → Windows Defender 防火墙 → 高级设置 → 入站规则 → 新建规则 → 端口 → TCP `8765` → 允许。
   - Linux（ufw）：`sudo ufw allow 8765/tcp`。
   - 也可命令行临时放行：`sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT`。
3. 确认电脑没有开着全局模式的 VPN / 代理软件，它会破坏局域网直连。
4. 端口被占用时换一个：`./start.sh --port 8766`。

**视频打不开 / 只有声音没有画面**
浏览器不支持该封装或编码（常见于 MKV、AVI、RMVB、H.265 的部分封装）。请下载后用本地播放器；
服务端不做转码，以保证传输速度和零依赖。

**手机上拖动进度条要等**
首次拖动需要向 PC 请求新区间的数据；Wi-Fi 信号弱或 PC 正在上传大文件时会慢一些。
有线连接或减少并发上传可明显改善。

**上传中断了要重新传吗**
不需要。重新拖入同一个文件即可续传（已传部分会被跳过，页面会提示"断点续传"）。
未完成的分片保存在 `uploads/.parts/`，超过 7 天会自动清理。

**想让电脑一开机就自动启动**

Linux 下**推荐直接用 `autostart.sh`**（注册成 systemd 服务：开机自启 + 崩溃自动拉起 + 日志进 journald）：

```bash
cd lan-share
sudo ./autostart.sh install                     # 系统级：开机即启，无需登录
sudo ./autostart.sh install --port 9000 --password 1234
sudo ./autostart.sh install --data /mnt/disk/share    # 存储目录指向大容量磁盘
./autostart.sh install --user                   # 用户级：不需要 sudo

./autostart.sh status                           # 状态（系统级 / 用户级都会列出）
./autostart.sh restart | start | stop
./autostart.sh logs -f                          # 日志；也支持 `logs 300` 指定行数
./autostart.sh show                             # 只打印将要写入的 unit，不落盘
./autostart.sh uninstall                        # 停用并移除自启（不动文件）
```

约定与细节：

- **作用域会自动记忆**：装过一次之后，`status` / `logs` / `restart` / `uninstall` 会自动认出是系统级还是用户级，
  不必每次补 `--system` / `--user`；`status` 会把两个作用域都列出来，并标明当前命令作用于哪一个。
  `status` / `logs` / `show` 是只读命令，**不需要 sudo**。
- **运行身份**默认取**存储目录的属主**，因此服务天然拥有共享池的读写权限，无需额外 `chmod`/`chown`；用 `--run-as 用户名` 可覆盖。
- **访问密码**写入 `/etc/lan-share.env`（权限 `600`），不是启动命令行，所以**不会出现在 `ps` 或 `systemctl show` 里**。
- **端口被占用时 `install` 会直接中止**，不会装出一个起不来的服务。确认要抢占时用
  `--free-port`（先结束占用者）或 `--force`（照装不误）。占用者若在别的 PID 命名空间里查不到属主，
  脚本会用一次真实连接兜底判断，不会误判成空闲。
- **装完验证的是「端口真的在监听」，而不是 `is-active`**：`Type=simple` 的服务在进程刚被拉起时就是 active，
  只看它会漏掉「端口被占、进程随即要崩」这种假成功。12 秒内没监听起来就报错并打印最近日志。
- unit 关键配置：`Restart=always` + `RestartSec=3`、`KillSignal=SIGINT`（Python 走 KeyboardInterrupt 分支，能立刻释放端口）、`60` 秒内失败 `5` 次即停止（避免端口冲突时无限重启）。

Windows：把 `start.bat` 的快捷方式放进
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` 即可。

不用 systemd 时，也可以临时后台常驻：

```bash
cd /path/to/lan-share
setsid nohup python3 server.py --port 8765 > server.log 2>&1 < /dev/null &
tail -f server.log          # 看地址；Ctrl+C 只是退出查看，不影响服务
pkill -f "server.py --port 8765"    # 需要停止时
```

> 想手工维护 unit 也可以，`./autostart.sh show` 会打印一份可直接抄的完整配置
> （注意 `Restart=always`、`WorkingDirectory` 与 `EnvironmentFile` 三项不可省）。

---

## 6. 安全说明

- 默认**没有密码**，同一局域网内任何人拿到地址都能读写。宿舍、办公室、公共 Wi-Fi 环境
  建议加上 `--password`：`./start.sh --password 你的密码`。设置后浏览需登录，登录凭证为 HttpOnly Cookie。
- 已做的防护：路径穿越拦截（`../`、绝对路径、隐藏文件均不可读不可删）、文件名净化（非法字符、Windows 保留名、超长名）、
  删除接口只允许操作存储根目录下的文件、上传大小与磁盘余量校验、请求体读取量精确计数（不会因半读破坏 keep-alive）。
- 二维码完全在前端生成：服务端**没有**二维码接口，也就不存在"被当成公共二维码
  生成器滥用"的攻击面；二维码内容只是页面里本来就有的访问地址。
- 服务只监听局域网端口，不接入公网。若确需外网访问，请务必设置密码，并建议放在 Nginx / Caddy 反向代理之后
  （注意反代需转发 `Range` 与 `Content-Length`，并关闭缓冲 `proxy_buffering off`，否则会破坏流式播放）。

---

## 7. 文件清单

```
lan-share/
├── server.py          # 服务端（纯标准库，零第三方依赖）
├── static/
│   ├── index.html     # 页面结构
│   ├── style.css      # 界面样式（浅色主题，移动端适配）
│   ├── app.js         # 前端逻辑：分块上传 / 断点续传 / 预览 / 列表 / 扫码弹层
│   └── vendor/
│       └── qrcode.js  # vendored 二维码库（qrcode-generator 1.4.4，MIT，离线分发）
├── start.sh           # Linux / macOS 启动脚本
├── start.bat          # Windows 启动脚本
├── autostart.sh       # Linux 开机自启安装器（systemd 服务：install/uninstall/status/logs）
├── selftest.py        # 端到端自检（46 项）
├── .browser-test/     # Playwright 浏览器验证（开发用）与测试素材
├── README.md
└── uploads/           # 共享文件池（首次运行自动创建）
    └── .parts/        # 上传中的临时分片
```

运行环境：Python 3.7+（仅标准库）。已在 Python 3.12 / Linux 上完成全项自检；
Windows / macOS 使用的都是标准库的跨平台 API。
