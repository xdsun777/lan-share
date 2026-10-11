#!/usr/bin/env bash
# ============================================================
#  局域网文件中转站 · Linux / macOS 启动脚本
#  用法：./start.sh              使用默认端口 8765
#        ./start.sh --port 9000 --password 1234
#        PYTHON=/usr/bin/python3 ./start.sh
# ============================================================
set -eu
cd "$(dirname "$0")"

find_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return; fi
  # 优先使用本机已存在的虚拟环境解释器，其次系统 python3
  for candidate in \
      /home/charm/onlyone/pro/venv/bin/python \
      "$HOME/.venv/bin/python" \
      /usr/bin/python3 \
      /usr/local/bin/python3; do
    if [ -x "$candidate" ]; then echo "$candidate"; return; fi
  done
  if command -v python3 >/dev/null 2>&1; then command -v python3; return; fi
  if command -v python  >/dev/null 2>&1; then command -v python;  return; fi
  echo ""
}

PY_BIN="$(find_python)"
if [ -z "$PY_BIN" ]; then
  echo "未找到 Python 3.7+，请先安装：sudo apt install python3" >&2
  exit 1
fi

VER="$("$PY_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo '?')"
case "$VER" in
  3.7|3.8|3.9|3.1[0-9]|4.*) ;;
  *) echo "警告：当前 Python 版本为 $VER，建议 3.7 及以上。" >&2 ;;
esac

echo "使用解释器：$PY_BIN (Python $VER)"
exec "$PY_BIN" server.py "$@"
