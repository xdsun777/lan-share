/* ==========================================================================
   局域网文件中转站 · 前端逻辑
   - 分块并发上传（3 路）+ 断点续传 + 失败自动重试
   - 列表渲染 / 搜索 / 排序 / 删除 / 复制直链
   - 音视频在线预览（依赖服务端 Range 分段，拖动进度条不重新下载）
   ========================================================================== */
'use strict';

/* ------------------------------------------------------------ 基础工具 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const CHUNK_CONCURRENCY = 3;
const CHUNK_RETRY = 3;

const state = {
  files: [],
  filter: '',
  sort: 'time-desc',
  meta: null,
  textPreviewMax: 2 * 1024 * 1024,
  selected: new Set(),          // 勾选待打包下载的文件名
  active: [],
};

function fmtSize(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(v < 10 ? 2 : 1)) + ' ' + units[i];
}

function fmtSpeed(n) {
  if (!n || n < 1) return '0 B/s';
  return fmtSize(n) + '/s';
}

function fmtDuration(sec) {
  if (!isFinite(sec) || sec < 0) return '--';
  if (sec < 60) return Math.ceil(sec) + ' 秒';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + Math.round(sec % 60) + ' 秒';
  return Math.floor(sec / 3600) + ' 小时 ' + Math.round((sec % 3600) / 60) + ' 分';
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = (now - d.getTime()) / 1000;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + ' 天前';
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtUptime(sec) {
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟';
  if (sec < 86400) return (sec / 3600).toFixed(1) + ' 小时';
  return (sec / 86400).toFixed(1) + ' 天';
}

function fileUrl(name, download) {
  return '/f/' + encodeURIComponent(name) + (download ? '?dl=1' : '');
}

function toast(msg, kind = '') {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .25s, transform .25s';
    el.style.opacity = '0';
    el.style.transform = 'translateY(6px)';
    setTimeout(() => el.remove(), 260);
  }, 3600);
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options });
  if (res.status === 401) {
    showLock(true);
    throw new ApiError('需要访问密码', 401, {});
  }
  const ctype = res.headers.get('content-type') || '';
  let data = null;
  if (ctype.includes('application/json')) data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError((data && data.error) || ('请求失败 HTTP ' + res.status), res.status, data);
  return data;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
    ta.remove();
    return ok;
  }
}

/* ------------------------------------------------------------ 启动 & 鉴权 */

function showLock(show) {
  $('#lockScreen').classList.toggle('hidden', !show);
  if (show) setTimeout(() => $('#lockInput').focus(), 60);
}

$('#lockForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const pwd = $('#lockInput').value;
  const errBox = $('#lockError');
  errBox.classList.add('hidden');
  try {
    await api('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pwd }),
    });
    showLock(false);
    $('#lockInput').value = '';
    await loadFiles();
    toast('已进入中转站', 'ok');
  } catch (err) {
    errBox.textContent = err.message || '密码错误';
    errBox.classList.remove('hidden');
  }
});

async function boot() {
  try {
    state.meta = await api('/api/meta');
    if (state.meta.textPreviewMax) state.textPreviewMax = state.meta.textPreviewMax;
  } catch (err) {
    $('#serverLine').textContent = '无法连接服务端：' + err.message;
    return;
  }
  $('#serverLine').textContent = `当前访问地址 ${location.origin}`;
  renderAddresses();
  try {
    await loadFiles();
  } catch (err) {
    if (err.status !== 401) {
      $('#loadingState').innerHTML = '';
      $('#loadingState').textContent = '读取失败：' + err.message;
    }
  }
}

/* ------------------------------------------------------------ 地址面板 */

// 回环地址手机打不开，给它生成二维码没有意义
function isLoopbackUrl(url) {
  try {
    const h = new URL(url, location.href).hostname.toLowerCase();
    return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]';
  } catch (e) {
    return false;
  }
}

function blankBtn(text, cls) {
  const b = document.createElement('button');
  b.className = cls;
  b.type = 'button';
  b.textContent = text;
  return b;
}

function renderAddresses() {
  const list = $('#addressList');
  list.innerHTML = '';
  const urls = ((state.meta && state.meta.urls) || []).slice();
  // 当前正在使用的地址一定要出现在列表里并标注出来
  if (!urls.some((u) => u.startsWith(location.origin))) urls.unshift(location.origin + '/');

  // 二维码库是随页面一起分发的静态文件；拿不到说明服务端在跑旧代码或文件缺失，
  // 此时干脆不显示扫码入口，而不是等用户点了才报错。
  const qrLibReady = typeof qrcode === 'function';
  const scannable = qrLibReady && urls.some((u) => !isLoopbackUrl(u));

  urls.forEach((url) => {
    const li = document.createElement('li');

    const left = document.createElement('div');
    left.className = 'addrLeft';
    const code = document.createElement('code');
    code.textContent = url;
    left.appendChild(code);
    if (url.startsWith(location.origin)) {
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = '当前';
      left.appendChild(tag);
    }

    const actions = document.createElement('div');
    actions.className = 'addrActions';

    if (qrLibReady && !isLoopbackUrl(url)) {
      const qrBtn = blankBtn('扫码', 'btn ghost sm qr-btn');
      qrBtn.addEventListener('click', () => openQr(url));
      actions.appendChild(qrBtn);
    }

    actions.appendChild(blankBtn('复制', 'btn ghost sm')).addEventListener('click', async () => {
      const ok = await copyText(url);
      toast(ok ? '地址已复制' : '复制失败，请手动选择文本', ok ? 'ok' : 'err');
    });

    li.appendChild(left);
    li.appendChild(actions);
    list.appendChild(li);
  });

  $('#addressHint').textContent = !qrLibReady
    ? '把任意一条地址发给 A / B 端（二维码库加载失败：请重启服务以获取最新页面）—— 都在同一个文件池里。'
    : (scannable
        ? '把任意一条地址发给 A / B 端，或让手机直接扫「扫码」二维码 —— 都在同一个文件池里。'
        : '把任意一条地址发给 A / B 端（手机请手动输入地址）—— 都在同一个文件池里。');
}

/* ------------------------------------------------------------ 扫码弹层 */

// 二维码内容就是这条访问地址，属于纯展示，因此在浏览器里用 vendored 的
// qrcode.js 现场生成 —— 不需要服务端接口，也没有额外依赖。
const QR_ECL = 'M';        // M 级纠错：可容忍约 15% 污损/反光
const QR_CELL = 1;         // 每格 1 个用户单位，实际大小由 CSS 决定
const QR_MARGIN = 2;       // 静默区（格数）

function qrSvgHtml(text) {
  // 0 = 按内容自动选最小的版本；非 ASCII 也按 UTF-8 编码，避免字节截断
  const qr = qrcode(0, QR_ECL);
  qr.addData(text);
  qr.make();
  return qr.createSvgTag({ cellSize: QR_CELL, margin: QR_MARGIN, scalable: true });
}

function openQr(url) {
  const box = $('#qrBox');
  if (typeof qrcode !== 'function') {
    toast('二维码库未加载，请刷新页面重试', 'err');
    return;
  }
  try {
    box.innerHTML = qrSvgHtml(url);
  } catch (e) {
    box.innerHTML = '';
    toast('二维码生成失败：' + (e && e.message ? e.message : e), 'err');
    return;
  }
  const svg = box.querySelector('svg');
  if (svg) {
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', '访问地址二维码：' + url);
  }
  $('#qrUrl').textContent = url;
  $('#qrModal').classList.remove('hidden');
}

function closeQr() {
  $('#qrModal').classList.add('hidden');
  $('#qrBox').innerHTML = '';    // 清掉，避免下次打开时闪现旧码
}

/* ------------------------------------------------------------ 文件列表 */

async function loadFiles() {
  const data = await api('/api/list');
  state.files = data.files || [];
  state.stats = data;

  const host = location.hostname || '127.0.0.1';
  const mem = data.memory && data.memory.peak
    ? `  ·  服务端内存峰值 ${fmtSize(data.memory.peak)}` : '';
  $('#serverLine').textContent =
    `当前地址 ${location.origin}  ·  已运行 ${fmtUptime(data.uptime || 0)}  ·  磁盘剩余 ${fmtSize(data.disk.free)}`;
  $('#footerInfo').textContent =
    `存储目录 ${data.root}  ·  共 ${data.count} 个文件 / ${fmtSize(data.totalSize)}  ·  服务端已运行 ${fmtUptime(data.uptime || 0)}${mem}  ·  ${host}`;

  $('#loadingState').classList.add('hidden');
  renderFiles();
}

function visibleFiles() {
  const kw = state.filter.trim().toLowerCase();
  let list = state.files.filter((f) => !kw || f.name.toLowerCase().includes(kw));
  const [key, dir] = state.sort.split('-');
  list = list.slice().sort((a, b) => {
    let r = 0;
    if (key === 'time') r = a.mtime - b.mtime;
    else if (key === 'size') r = a.size - b.size;
    else r = a.name.localeCompare(b.name, 'zh-Hans-CN');
    return dir === 'desc' ? -r : r;
  });
  return list;
}

function typeLabel(file) {
  const map = { video: '视频', audio: '音频', image: '图片', pdf: 'PDF', text: '文本', archive: '压缩包', other: '文件' };
  return map[file.type] || '文件';
}

function renderFiles() {
  const list = visibleFiles();
  const box = $('#fileList');
  box.innerHTML = '';

  const summary = $('#poolSummary');
  if (state.filter.trim()) {
    summary.textContent = `筛选出 ${list.length} / ${state.files.length} 个`;
  } else {
    summary.textContent = state.files.length
      ? `${state.files.length} 个文件 · 合计 ${fmtSize(state.stats.totalSize)}`
      : '';
  }

  // 选中集合里可能已有被删除的文件，先收敛一遍
  const alive = new Set(state.files.map((f) => f.name));
  for (const name of [...state.selected]) {
    if (!alive.has(name)) state.selected.delete(name);
  }
  renderZipBar(list);

  if (!state.files.length) {
    $('#emptyState').classList.remove('hidden');
    return;
  }
  $('#emptyState').classList.add('hidden');

  if (!list.length) {
    const d = document.createElement('div');
    d.className = 'empty';
    d.innerHTML = '<p class="muted small">没有匹配的文件名</p>';
    box.appendChild(d);
    return;
  }

  const frag = document.createDocumentFragment();
  list.forEach((file) => frag.appendChild(renderRow(file)));
  box.appendChild(frag);
}

/* ------------------------------------------------------------ 批量打包下载 */

function toggleSelect(name, on) {
  if (on) state.selected.add(name);
  else state.selected.delete(name);
  renderZipBar(visibleFiles());
  $$(`.fpick[data-name]`).forEach((cb) => {
    if (cb.dataset.name === name) cb.checked = state.selected.has(name);
  });
  $$('.frow').forEach((r) => {
    const cb = r.querySelector('.fpick');
    if (cb) r.classList.toggle('selected', state.selected.has(cb.dataset.name));
  });
}

function renderZipBar(list) {
  const bar = $('#zipBar');
  const btn = $('#btnZip');
  const n = state.selected.size;
  const scannableList = list || [];
  $('#btnSelectAll').textContent =
    scannableList.length && scannableList.every((f) => state.selected.has(f.name))
      ? '取消全选' : '全选';
  bar.classList.toggle('hidden', !(state.files.length || n));
  btn.disabled = n === 0;
  btn.textContent = n ? `打包下载（${n} 个）` : '打包下载';
}

function zipDownload() {
  const names = [...state.selected];
  if (!names.length) return;
  const qs = names.map((n) => 'names=' + encodeURIComponent(n)).join('&');
  const a = document.createElement('a');
  a.href = '/api/zip?' + qs;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast(`已开始打包下载 ${names.length} 个文件`, 'ok');
  state.selected.clear();
  renderFiles();
}

function renderRow(file) {
  const row = document.createElement('div');
  row.className = 'frow';
  row.setAttribute('role', 'listitem');
  if (state.selected.has(file.name)) row.classList.add('selected');

  const pick = document.createElement('input');
  pick.type = 'checkbox';
  pick.className = 'fpick';
  pick.dataset.name = file.name;
  pick.checked = state.selected.has(file.name);
  pick.title = '选中后可打包下载';
  pick.setAttribute('aria-label', '选择 ' + file.name);
  pick.addEventListener('change', () => toggleSelect(file.name, pick.checked));
  row.appendChild(pick);

  const icon = document.createElement('div');
  icon.className = 'ficon ' + file.type;
  icon.textContent = (file.ext || '?').slice(0, 4);
  row.appendChild(icon);

  const main = document.createElement('div');
  main.className = 'fmain';
  const name = document.createElement('div');
  name.className = 'fname';
  name.textContent = file.name;
  name.title = file.name;
  const meta = document.createElement('div');
  meta.className = 'fmeta';
  const s1 = document.createElement('span'); s1.textContent = fmtSize(file.size);
  const s2 = document.createElement('span'); s2.textContent = hintTime(file);
  const s3 = document.createElement('span'); s3.textContent = typeLabel(file);
  meta.append(s1, s2, s3);
  if (file.type === 'video' && !file.playable) {
    const chip = document.createElement('span');
    chip.className = 'chip warn';
    chip.textContent = '建议下载后播放';
    meta.appendChild(chip);
  }
  main.append(name, meta);
  row.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'factions';

  if (file.previewable) {
    actions.appendChild(button(file.type === 'text' ? '查看' : '预览', 'ghost sm', () => openPreview(file)));
  }
  actions.appendChild(button('下载', 'ghost sm', () => {
    const a = document.createElement('a');
    a.href = fileUrl(file.name, true);
    a.download = file.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }));
  actions.appendChild(button('复制链接', 'ghost sm hide-sm', async () => {
    const url = location.origin + fileUrl(file.name, false);
    const ok = await copyText(url);
    toast(ok ? '直链已复制，可直接发给对方' : '复制失败，请手动选择文本', ok ? 'ok' : 'err');
  }));
  actions.appendChild(button('删除', 'ghost sm danger', () => removeFile(file)));

  row.appendChild(actions);
  return row;
}

function hintTime(file) { return fmtTime(file.mtime); }

function button(text, cls, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'btn ' + cls;
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

async function removeFile(file) {
  if (!confirm(`确定删除「${file.name}」吗？\n此操作不可撤销。`)) return;
  try {
    const res = await api('/api/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: file.name }),
    });
    if (res.deleted && res.deleted.length) {
      toast('已删除 ' + file.name, 'ok');
      await loadFiles();
    } else {
      toast('删除失败：' + ((res.failed[0] && res.failed[0].error) || '未知原因'), 'err');
    }
  } catch (err) {
    toast('删除失败：' + err.message, 'err');
  }
}

/* ------------------------------------------------------------ 在线预览 */

function closePreview() {
  const body = $('#previewBody');
  $$('video, audio', body).forEach((m) => { try { m.pause(); } catch (e) {} });
  body.innerHTML = '';
  $('#previewModal').classList.add('hidden');
}

function openPreview(file) {
  const modal = $('#previewModal');
  const body = $('#previewBody');
  const url = fileUrl(file.name, false);
  body.innerHTML = '';
  $('#previewName').textContent = file.name;
  $('#previewMeta').textContent = `${fmtSize(file.size)} · ${fmtTime(file.mtime)}`;
  $('#previewOpen').href = url;
  $('#previewDownload').href = fileUrl(file.name, true);

  if (file.type === 'video' || file.type === 'audio') {
    const media = document.createElement(file.type);
    media.controls = true;
    media.autoplay = true;
    media.preload = 'metadata';
    if (file.type === 'video') media.playsInline = true;
    media.src = url;
    media.addEventListener('error', () => {
      body.innerHTML = '';
      body.appendChild(unsupportedNotice(file, '该格式无法被浏览器直接解码（常见于 MKV / AVI / RMVB 或非常规编码）。'));
    });
    body.appendChild(media);
  } else if (file.type === 'image') {
    const img = document.createElement('img');
    img.src = url;
    img.alt = file.name;
    body.appendChild(img);
  } else if (file.type === 'pdf') {
    const frame = document.createElement('iframe');
    frame.src = url;
    frame.title = file.name;
    body.appendChild(frame);
  } else if (file.type === 'text') {
    if (file.size > state.textPreviewMax) {
      body.appendChild(unsupportedNotice(file, `文本较大（${fmtSize(file.size)}），为避免浏览器卡顿不再内联展示，请下载后查看。`));
    } else {
      const pre = document.createElement('pre');
      pre.textContent = '正在加载…';
      body.appendChild(pre);
      fetch(url, { credentials: 'same-origin' })
        .then((r) => r.text())
        .then((t) => { pre.textContent = t || '(空文件)'; })
        .catch((e) => { pre.textContent = '读取失败：' + e.message; });
    }
  } else {
    body.appendChild(unsupportedNotice(file, '该类型暂不支持在线预览，可直接下载到本地打开。'));
  }

  modal.classList.remove('hidden');
}

function unsupportedNotice(file, reason) {
  const box = document.createElement('div');
  box.className = 'notice';
  const h = document.createElement('h4');
  h.textContent = '无法在线预览';
  const p = document.createElement('p');
  p.textContent = reason;
  const actions = document.createElement('div');
  actions.style.display = 'flex';
  actions.style.gap = '10px';
  actions.style.justifyContent = 'center';
  const dl = document.createElement('a');
  dl.className = 'btn primary';
  dl.textContent = '下载文件';
  dl.href = fileUrl(file.name, true);
  dl.setAttribute('download', file.name);
  actions.appendChild(dl);
  if (file.type === 'video' || file.type === 'audio') {
    const force = document.createElement('button');
    force.className = 'btn';
    force.type = 'button';
    force.textContent = '仍然尝试加载';
    force.addEventListener('click', () => {
      const m = document.createElement(file.type);
      m.controls = true;
      m.autoplay = true;
      m.src = fileUrl(file.name, false);
      $('#previewBody').innerHTML = '';
      $('#previewBody').appendChild(m);
    });
    actions.appendChild(force);
  }
  box.append(h, p, actions);
  return box;
}

/* ------------------------------------------------------------ 上传引擎 */

function pickChunkSize(size) {
  // 目标 ≈ 48 个分块；限制在 1MB ~ 16MB，并向上取 2 的幂，落盘更规整
  const ideal = Math.max(1 << 20, Math.min(16 << 20, Math.ceil(size / 48)));
  return 1 << Math.ceil(Math.log2(ideal));
}

class UploadTask {
  constructor(file) {
    this.file = file;
    this.key = `${file.name}|${file.size}|${file.lastModified}`;
    this.chunkSize = pickChunkSize(file.size);
    this.total = Math.max(1, Math.ceil(file.size / this.chunkSize));
    this.uid = null;
    this.startOffset = 0;
    this.nextIndex = 0;
    this.doneBytes = 0;
    this.inflight = new Map();
    this.status = 'pending';
    this.message = '';
    this.speed = 0;
    this.cancelled = false;
    this.xhrs = new Set();
    this.el = {};
    this._lastT = 0;
    this._lastB = 0;
    this._needsUI = true;
  }

  get loaded() {
    let inflight = 0;
    this.inflight.forEach((v) => { inflight += v; });
    return Math.min(this.file.size, this.doneBytes + inflight);
  }

  chunkLength(idx) {
    const offset = idx * this.chunkSize;
    return Math.max(0, Math.min(this.chunkSize, this.file.size - offset));
  }

  cancel() {
    this.cancelled = true;
    this.xhrs.forEach((x) => { try { x.abort(); } catch (e) {} });
    this.xhrs.clear();
    if (this.uid) {
      // 主动取消 = 丢弃已传分片；断网/关页面属于意外中断，分片会保留以便续传
      fetch('/api/upload/abort', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uid: this.uid }),
        keepalive: true,
      }).catch(() => {});
    }
    if (this.status !== 'done') {
      this.status = 'canceled';
      this.message = '已取消';
      renderQueue();
    }
  }
}

const uploads = [];

function hasActiveUpload() {
  return uploads.some((t) => t.status === 'pending' || t.status === 'running');
}

function handleFiles(fileList) {
  const files = Array.from(fileList || []).filter((f) => f && typeof f.size === 'number');
  if (!files.length) return;
  let accepted = 0;
  files.forEach((file) => {
    const key = `${file.name}|${file.size}|${file.lastModified}`;
    if (uploads.some((t) => t.key === key && (t.status === 'running' || t.status === 'pending'))) {
      toast(`「${file.name}」正在上传中，已跳过重复选择`, 'info');
      return;
    }
    accepted++;
    const task = new UploadTask(file);
    uploads.push(task);
    const built = buildQueueItem(task);
    $('#queueList').appendChild(built.root);
  });
  if (!accepted) return;
  $('#queuePanel').classList.remove('hidden');
  toast(`已加入 ${accepted} 个文件`, 'info');
  renderQueue();
  uploads.filter((t) => t.status === 'pending').forEach((t, i) => {
    // 轻微错峰启动，避免所有分块同时抢占首包
    setTimeout(() => runUpload(t), i * 120);
  });
}

async function runUpload(task) {
  if (task.cancelled) return;
  task.status = 'running';
  task.message = '正在建立会话…';
  renderQueue();
  try {
    const init = await api('/api/upload/init', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: task.file.name,
        size: task.file.size,
        lastModified: task.file.lastModified,
        key: task.key,
      }),
    });
    task.uid = init.uploadId;
    task.startOffset = init.offset || 0;
    task.doneBytes = task.startOffset;
    task.nextIndex = Math.min(task.total, Math.floor(task.startOffset / task.chunkSize));
    if (task.startOffset > 0 && task.startOffset < task.file.size) {
      task.message = `断点续传，从 ${fmtSize(task.startOffset)} 继续`;
      toast(`「${task.file.name}」断点续传：已跳过 ${fmtSize(task.startOffset)}`, 'info');
    } else {
      task.message = '上传中';
    }

    if (task.file.size > 0) await pump(task);
    if (task.cancelled) return;

    task.message = '正在校验并落盘…';
    renderQueue();
    const done = await api('/api/upload/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uid: task.uid, name: task.file.name, size: task.file.size }),
    });
    task.status = 'done';
    task.doneBytes = task.file.size;
    task.inflight.clear();
    task.message = done.name !== task.file.name ? `已保存为 ${done.name}` : '已完成';
    renderQueue();
    toast(`「${done.name}」上传完成`, 'ok');
    loadFiles().catch(() => {});
    setTimeout(() => {
      const idx = uploads.indexOf(task);
      if (idx >= 0 && uploads[idx].status === 'done') {
        uploads.splice(idx, 1);
        if (task.el.root) task.el.root.remove();
        renderQueue();
      }
    }, 6000);
  } catch (err) {
    if (task.cancelled) return;
    task.status = 'error';
    task.message = err.message || '上传失败';
    task.inflight.clear();
    renderQueue();
    toast(`「${task.file.name}」上传失败：${task.message}`, 'err');
  }
}

function pump(task) {
  return new Promise((resolve, reject) => {
    let active = 0;
    let settled = false;

    const finish = (err) => {
      if (settled) return;
      if (err) { settled = true; reject(err); return; }
      if (task.nextIndex >= task.total && active === 0) { settled = true; resolve(); }
    };

    const tick = () => {
      if (settled) return;
      if (task.cancelled) return finish(new Error('已取消'));
      while (active < CHUNK_CONCURRENCY && task.nextIndex < task.total) {
        const idx = task.nextIndex++;
        active++;
        sendChunk(task, idx)
          .then(() => {
            active--;
            task.doneBytes += task.chunkLength(idx);
            finish(null);
            if (!settled) tick();
          })
          .catch((err) => {
            active--;
            if (task.cancelled) return finish(new Error('已取消'));
            finish(err);
          });
      }
      finish(null);
    };
    tick();
  });
}

async function sendChunk(task, idx, attempt = 0) {
  const offset = idx * task.chunkSize;
  const len = task.chunkLength(idx);
  const blob = task.file.slice(offset, offset + len);
  try {
    await putChunk(task, idx, offset, blob);
  } catch (err) {
    if (task.cancelled) throw new Error('已取消');
    if (attempt < CHUNK_RETRY) {
      task.message = `第 ${idx + 1} 块重试 ${attempt + 1}/${CHUNK_RETRY}…`;
      renderQueue();
      await sleep(400 * (attempt + 1));
      return sendChunk(task, idx, attempt + 1);
    }
    throw err;
  }
}

function putChunk(task, idx, offset, blob) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    task.xhrs.add(xhr);
    xhr.open('POST', `/api/upload/chunk?uid=${task.uid}&offset=${offset}`, true);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.timeout = 0;   // 大块传输不设超时，靠服务端与浏览器自行处理断连

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) task.inflight.set(idx, e.loaded);
    };
    const done = (fn) => (ev) => {
      task.xhrs.delete(xhr);
      task.inflight.delete(idx);
      fn(ev);
    };
    xhr.onload = done(() => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let msg = 'HTTP ' + xhr.status;
      try {
        const j = JSON.parse(xhr.responseText);
        if (j && j.error) msg = j.error;
      } catch (e) {}
      if (xhr.status === 401) showLock(true);
      reject(new Error(msg));
    });
    xhr.onerror = done(() => reject(new Error('网络中断')));
    xhr.onabort = done(() => reject(new Error('已取消')));
    xhr.ontimeout = done(() => reject(new Error('传输超时')));
    xhr.send(blob);
  });
}

/* ------------------------------------------------------------ 队列 UI */

function buildQueueItem(task) {
  const root = document.createElement('div');
  root.className = 'qitem';

  const row = document.createElement('div');
  row.className = 'qrow';
  const name = document.createElement('span');
  name.className = 'qname';
  name.textContent = task.file.name;
  name.title = task.file.name;
  const stat = document.createElement('span');
  stat.className = 'qstat';
  stat.textContent = fmtSize(task.file.size);
  row.append(name, stat);

  const bar = document.createElement('div');
  bar.className = 'qbar';
  const fill = document.createElement('i');
  bar.appendChild(fill);

  const foot = document.createElement('div');
  foot.className = 'qfoot';
  const stateText = document.createElement('span');
  stateText.className = 'state';
  stateText.textContent = '等待中';
  const actions = document.createElement('div');
  actions.className = 'headActions';
  const cancelBtn = button('取消', 'ghost sm danger qbtn-cancel', () => task.cancel());
  const retryBtn = button('重试', 'ghost sm', () => {
    task.status = 'pending';
    task.cancelled = false;
    task.message = '重新排队…';
    task.nextIndex = Math.min(task.total, Math.floor(task.doneBytes / task.chunkSize));
    renderQueue();
    runUpload(task);
  });
  retryBtn.classList.add('hidden');
  actions.append(retryBtn, cancelBtn);
  foot.append(stateText, actions);

  root.append(row, bar, foot);
  task.el = { root, fill, stat, stateText, cancelBtn, retryBtn };
  return { root };
}

function renderQueue() {
  const total = uploads.length;
  $('#queuePanel').classList.toggle('hidden', total === 0);
  if (!total) return;

  let aggSpeed = 0;
  let running = 0;
  let doneCount = 0;

  uploads.forEach((task) => {
    const el = task.el;
    if (!el.root || !el.root.isConnected) return;
    const loaded = task.loaded;
    const pct = task.file.size ? Math.min(100, (loaded / task.file.size) * 100) : (task.status === 'done' ? 100 : 0);

    el.root.classList.toggle('done', task.status === 'done');
    el.root.classList.toggle('error', task.status === 'error');
    el.fill.style.width = pct.toFixed(2) + '%';

    const st = task.file.size
      ? `${fmtSize(loaded)} / ${fmtSize(task.file.size)}（${pct.toFixed(pct < 10 ? 1 : 0)}%）`
      : '0 B';
    el.stat.textContent = task.status === 'running' && task.speed > 0
      ? `${st} · ${fmtSpeed(task.speed)} · 剩余 ${fmtDuration((task.file.size - loaded) / task.speed)}`
      : st;

    const labels = {
      pending: '等待中',
      running: task.message || '上传中',
      done: task.message || '已完成',
      error: '失败：' + task.message,
      canceled: '已取消',
    };
    el.stateText.textContent = labels[task.status] || task.status;
    el.cancelBtn.classList.toggle('hidden', task.status === 'done' || task.status === 'error' || task.status === 'canceled');
    el.retryBtn.classList.toggle('hidden', task.status !== 'error' && task.status !== 'canceled');

    if (task.status === 'running') { aggSpeed += task.speed || 0; running++; }
    if (task.status === 'done') doneCount++;
  });

  const parts = [];
  if (running) parts.push(`${running} 个上传中 · 合计 ${fmtSpeed(aggSpeed)}`);
  if (doneCount) parts.push(`${doneCount} 个已完成`);
  $('#queueSummary').textContent = parts.join(' · ');
}

/* 速度采样：按 250ms 窗口计算瞬时速度并做指数平滑 */
setInterval(() => {
  if (!uploads.length) return;
  const now = performance.now();
  let needUI = false;
  uploads.forEach((task) => {
    if (task.status !== 'running') return;
    const loaded = task.loaded;
    if (!task._lastT) { task._lastT = now; task._lastB = loaded; return; }
    const dt = (now - task._lastT) / 1000;
    if (dt < 0.25) return;
    const inst = Math.max(0, (loaded - task._lastB) / dt);
    task.speed = task.speed > 0 ? task.speed * 0.6 + inst * 0.4 : inst;
    task._lastT = now;
    task._lastB = loaded;
    needUI = true;
  });
  if (needUI) renderQueue();
}, 250);

/* ------------------------------------------------------------ 事件绑定 */

$('#btnPick').addEventListener('click', () => $('#fileInput').click());
$('#dropzone').addEventListener('click', () => $('#fileInput').click());
$('#dropzone').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#fileInput').click(); }
});
$('#fileInput').addEventListener('change', (e) => {
  handleFiles(e.target.files);
  e.target.value = '';
});

$('#btnRefresh').addEventListener('click', async () => {
  try { await loadFiles(); toast('列表已刷新', 'ok'); }
  catch (err) { if (err.status !== 401) toast('刷新失败：' + err.message, 'err'); }
});

$('#btnZip').addEventListener('click', zipDownload);
$('#btnSelectAll').addEventListener('click', () => {
  const list = visibleFiles();
  const allPicked = list.length && list.every((f) => state.selected.has(f.name));
  list.forEach((f) => {
    if (allPicked) state.selected.delete(f.name);
    else state.selected.add(f.name);
  });
  renderFiles();
});

$('#btnAddresses').addEventListener('click', () => {
  $('#addressPanel').classList.toggle('hidden');
});
$$('[data-close]:not(button)').forEach((el) => el.addEventListener('click', (e) => {
  if (e.target !== el) return;
  const id = el.dataset.close;
  if (id === 'previewModal') closePreview();
  else if (id === 'qrModal') closeQr();
  else $('#' + id).classList.add('hidden');
}));
$$('button[data-close]').forEach((b) =>
  b.addEventListener('click', () => {
    const id = b.getAttribute('data-close');
    if (id === 'previewModal') closePreview();
    else if (id === 'qrModal') closeQr();
    else $('#' + id).classList.add('hidden');
  }));

$('#searchInput').addEventListener('input', (e) => { state.filter = e.target.value; renderFiles(); });
$('#sortSelect').addEventListener('change', (e) => { state.sort = e.target.value; renderFiles(); });

$('#btnCancelAll').addEventListener('click', () => {
  uploads.filter((t) => t.status === 'running' || t.status === 'pending').forEach((t) => t.cancel());
});
$('#btnClearDone').addEventListener('click', () => {
  for (let i = uploads.length - 1; i >= 0; i--) {
    const t = uploads[i];
    if (t.status === 'done' || t.status === 'canceled' || t.status === 'error') {
      if (t.el.root) t.el.root.remove();
      uploads.splice(i, 1);
    }
  }
  renderQueue();
});

/* 拖拽上传：用"dragover 心跳"判断是否仍在窗口内，比 dragenter/dragleave 计数更稳 */
let dragTimer = null;
const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');

function showDropOverlay(show) {
  $('#dropOverlay').classList.toggle('hidden', !show);
  $('#dropzone').classList.toggle('hot', show);
}

window.addEventListener('dragenter', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  showDropOverlay(true);
});
window.addEventListener('dragover', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
  showDropOverlay(true);
  clearTimeout(dragTimer);
  dragTimer = setTimeout(() => showDropOverlay(false), 180);
});
window.addEventListener('dragleave', (e) => {
  if (!hasFiles(e)) return;
  clearTimeout(dragTimer);
  dragTimer = setTimeout(() => showDropOverlay(false), 180);
});
window.addEventListener('dragend', () => { clearTimeout(dragTimer); showDropOverlay(false); });
window.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  clearTimeout(dragTimer);
  showDropOverlay(false);
  handleFiles(e.dataTransfer.files);
});

/* 快捷键与离开提醒 */
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('#qrModal').classList.contains('hidden')) closeQr();
  else if (!$('#previewModal').classList.contains('hidden')) closePreview();
});
window.addEventListener('beforeunload', (e) => {
  if (hasActiveUpload()) {
    e.preventDefault();
    e.returnValue = '仍有文件在上传，确定离开吗？';
    return e.returnValue;
  }
});

/* 打开预览时点击遮罩关闭 */
$('#previewModal').addEventListener('click', (e) => {
  if (e.target.id === 'previewModal') closePreview();
});

boot();
