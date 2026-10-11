#!/usr/bin/env bash
# ============================================================================
#  LAN Share · 开机自启安装器（Linux / systemd）
#
#  把「局域网文件中转站」注册为 systemd 服务：开机自动启动、崩溃自动拉起、
#  日志统一进 journald。装好之后不必再手工执行 start.sh。
#
#  典型用法
#      sudo ./autostart.sh install                    # 系统级：开机即启，无需登录
#      sudo ./autostart.sh install --port 9000 --password 1234
#      sudo ./autostart.sh install --data /mnt/disk/share   # 存储目录指向大容量磁盘
#      ./autostart.sh install --user                  # 用户级：不需要 sudo
#
#      ./autostart.sh status                          # 状态（系统级 / 用户级都会看）
#      ./autostart.sh restart | start | stop
#      ./autostart.sh logs -f  /  logs 300             # 日志（-f 跟随，数字=行数）
#      ./autostart.sh show                            # 只打印 unit 内容，不写盘
#      ./autostart.sh uninstall                       # 停用并移除自启（不动文件）
#
#  几点约定
#    * 作用域会自动记忆：装过之后 status / logs / restart / uninstall 会自动
#      认出是系统级还是用户级，不需要每次补 --system / --user。
#      status / logs / show 是只读命令，不需要 sudo。
#    * 默认运行身份 = 存储目录的属主（自动探测），可用 --run-as 覆盖。
#      这样服务天然拥有读写共享池的权限，不需要额外 chmod/chown。
#    * 访问密码写入 600 权限的 EnvironmentFile，而不是启动命令行，
#      因此不会出现在 ps / systemctl show 的输出里。
#    * 端口被占用时 install 会直接中止（避免装出一个起不来的服务）；
#      确认要抢占时用 --free-port（先结束占用者）或 --force（照装不误）。
# ============================================================================
set -euo pipefail

APP_DESC="LAN Share 局域网文件中转站"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$SELF_DIR/server.py"

# ---- 默认值（可被命令行覆盖）------------------------------------------------
SVC_NAME="lan-share"
PORT=8765
DATA_DIR="$SELF_DIR/uploads"
PASSWORD=""
PY_BIN=""
RUN_AS=""
FREE_PORT=0
FORCE=0
SCOPE_FORCED=""          # 空 = 自动探测
declare -a LOGS_ARGS=()

# ---- 颜色与输出 -------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[36m'; C_D=$'\033[2m'; C_0=$'\033[0m'
else
  C_R=""; C_G=""; C_Y=""; C_B=""; C_D=""; C_0=""
fi
info() { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
dim()  { printf '%s%s%s\n' "$C_D" "$*" "$C_0"; }
ok()   { printf '%s  ✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '%s  !%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%s错误:%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
  exit 0
}

# ---------------------------------------------------------------------------
# 子命令与参数
# ---------------------------------------------------------------------------
CMD="${1:-}"; [ $# -gt 0 ] && shift || true
case "$CMD" in
  -h|--help|help|"") usage ;;
  install|uninstall|status|start|stop|restart|logs|show) ;;
  *) die "未知子命令：$CMD（可用：install / uninstall / status / start / stop / restart / logs / show）" ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --user)          SCOPE_FORCED="user" ;;
    --system)        SCOPE_FORCED="system" ;;
    --port)          PORT="${2:?--port 需要端口号}"; shift ;;
    --port=*)        PORT="${1#*=}" ;;
    --data|--dir)    DATA_DIR="${2:?--data 需要目录}"; shift ;;
    --data=*|--dir=*) DATA_DIR="${1#*=}" ;;
    --password|-w)   PASSWORD="${2:?--password 需要密码}"; shift ;;
    --password=*)    PASSWORD="${1#*=}" ;;
    --python)        PY_BIN="${2:?--python 需要路径}"; shift ;;
    --python=*)      PY_BIN="${1#*=}" ;;
    --run-as)        RUN_AS="${2:?--run-as 需要用户名}"; shift ;;
    --run-as=*)      RUN_AS="${1#*=}" ;;
    --name)          SVC_NAME="${2:?--name 需要服务名}"; shift ;;
    --name=*)        SVC_NAME="${1#*=}" ;;
    --free-port)     FREE_PORT=1 ;;
    --force)         FORCE=1 ;;
    -f|--follow)     LOGS_ARGS+=("-f") ;;
    -n)              LOGS_ARGS+=("-n" "${2:?-n 需要行数}"); shift ;;
    -h|--help)       usage ;;
    -*)              die "未知选项：$1（用 -h 查看用法）" ;;
    *)
      # 只有 logs 接受裸数字（行数）；其余子命令多给参数一律报错，不静默吞掉
      if [ "$CMD" = "logs" ] && [[ "$1" =~ ^[0-9]+$ ]]; then
        LOGS_ARGS+=("-n" "$1")
      else
        die "参数 \"$1\" 不适用于 $CMD（用 -h 查看用法）"
      fi
      ;;
  esac
  shift
done

[[ "$PORT" =~ ^[0-9]+$ ]] || die "端口必须是数字：$PORT"
(( PORT >= 1 && PORT <= 65535 )) || die "端口超出范围：$PORT"
[[ "$SVC_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || die "服务名只能包含字母、数字、_ . @ -：$SVC_NAME"
SVC_NAME="${SVC_NAME%.service}"

# ---------------------------------------------------------------------------
# 身份与路径
#   「以 root 执行」不等于「给 root 装」——若是 sudo 出来的，目标仍是原用户。
# ---------------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
  ACT_UID=0
  CALLER_USER="${SUDO_USER:-root}"
else
  ACT_UID="$(id -u)"
  CALLER_USER="$(id -un)"
fi
CALLER_HOME="$(getent passwd "$CALLER_USER" 2>/dev/null | cut -d: -f6)"
[ -n "$CALLER_HOME" ] || CALLER_HOME="${HOME:-/root}"
CALLER_UID="$(id -u "$CALLER_USER" 2>/dev/null || printf '0')"

SYSTEM_UNIT="/etc/systemd/system/${SVC_NAME}.service"
USER_UNIT="$CALLER_HOME/.config/systemd/user/${SVC_NAME}.service"
USER_UNIT_DIR="$(dirname "$USER_UNIT")"
SYSTEM_ENV="/etc/${SVC_NAME}.env"
USER_ENV="$CALLER_HOME/.config/${SVC_NAME}.env"

# ---------------------------------------------------------------------------
# 作用域解析：显式指定 > 已装的那个 > 谁能装就装谁
# ---------------------------------------------------------------------------
resolve_scope() {
  if   [ -n "$SCOPE_FORCED" ]; then SCOPE="$SCOPE_FORCED"
  elif [ -f "$SYSTEM_UNIT" ];   then SCOPE="system"
  elif [ -f "$USER_UNIT" ];     then SCOPE="user"
  elif [ "$ACT_UID" -eq 0 ];    then SCOPE="system"
  else                               SCOPE="user"
  fi

  if [ "$SCOPE" = "system" ]; then
    UNIT_FILE="$SYSTEM_UNIT"; ENV_FILE="$SYSTEM_ENV"; TARGET_USER="${RUN_AS:-}"
    SCTL=(systemctl)
    JCTL=(journalctl)
    SCOPE_DESC="系统级"
  else
    UNIT_FILE="$USER_UNIT"; ENV_FILE="$USER_ENV"; TARGET_USER="${RUN_AS:-$CALLER_USER}"
    if [ "$ACT_UID" -eq 0 ] && [ "$CALLER_USER" != "root" ]; then
      # 以 sudo 装用户级：systemctl/journalctl 必须切回该用户，否则找不到它的 user bus
      SCTL=(runuser -u "$CALLER_USER" -- env "XDG_RUNTIME_DIR=/run/user/$CALLER_UID" systemctl --user)
      JCTL=(runuser -u "$CALLER_USER" -- env "XDG_RUNTIME_DIR=/run/user/$CALLER_UID" journalctl --user)
    else
      SCTL=(systemctl --user)
      JCTL=(journalctl --user)
    fi
    SCOPE_DESC="用户级"
  fi
}

# 需要 root 的只有「改系统级状态」这类操作；status / logs / show 一律不需要
need_root_if_system() {
  case "$CMD" in
    install|uninstall|start|stop|restart) ;;
    *) return 0 ;;
  esac
  if [ "$SCOPE" = "system" ] && [ "$ACT_UID" -ne 0 ]; then
    die "系统级服务需要 root 权限：sudo $0 $CMD
       （只想给当前用户装的话，用：$0 $CMD --user）"
  fi
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
find_python() {
  local c
  for c in \
      /home/charm/onlyone/pro/venv/bin/python \
      "$CALLER_HOME/.venv/bin/python" \
      /usr/bin/python3 \
      /usr/local/bin/python3; do
    [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  c="$(command -v python3 2>/dev/null || true)"; [ -n "$c" ] && { printf '%s' "$c"; return 0; }
  c="$(command -v python  2>/dev/null || true)"; [ -n "$c" ] && { printf '%s' "$c"; return 0; }
  return 1
}

lan_ip() {
  local ip=""
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  printf '%s' "${ip:-127.0.0.1}"
}

port_occupant_pids() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u
  fi
}

port_busy() {
  local pids; pids="$(port_occupant_pids || true)"
  [ -n "$pids" ] && return 0
  # 兜底：跨 PID 命名空间时 ss 查不到属主，用一次真实连接判断
  (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") >/dev/null 2>&1 && return 0
  return 1
}

wait_listening() {                       # $1 = 最多等多少秒（默认 12）
  local secs="${1:-12}" i=0 n=$(( ${1:-12} * 4 ))
  while [ "$i" -lt "$n" ]; do
    (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") >/dev/null 2>&1 && return 0
    sleep 0.25; i=$((i + 1))
  done
  return 1
}

run_kill()          { local p; for p in $1; do kill "$p" 2>/dev/null || true; done; }
run_kill_by_name()  { pkill -f "server\.py" 2>/dev/null || true; }

render_unit() {
  local want_by where_user=""
  if [ "$SCOPE" = "system" ]; then
    want_by="multi-user.target"
    local grp; grp="$(id -gn "$RUN_AS" 2>/dev/null || printf '%s' "$RUN_AS")"
    where_user=$'\n'"User=$RUN_AS"$'\n'"Group=$grp"
  else
    want_by="default.target"
  fi

  cat <<EOF
[Unit]
Description=$APP_DESC（端口 $PORT）
Documentation=file:$SELF_DIR/README.md
After=network-online.target
Wants=network-online.target
# 端口被占等情况下不要无限重启：60 秒内失败 5 次即停止
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$SELF_DIR
Environment=PYTHONUNBUFFERED=1
# 访问密码从 600 权限的文件读入（不带 - 表示文件不存在也不报错）
EnvironmentFile=-$ENV_FILE$where_user
ExecStart=$PY_BIN $SERVER --port $PORT --dir $DATA_DIR
Restart=always
RestartSec=3
# 用 SIGINT 停止：Python 走 KeyboardInterrupt 分支，能立刻释放监听端口
KillSignal=SIGINT
TimeoutStopSec=15
# 轻量加固：不提权，且拥有独立 /tmp
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=$want_by
EOF
}

show_addresses() {
  step "访问地址："
  info "    http://127.0.0.1:$PORT/        （本机）"
  info "    http://$(lan_ip):$PORT/    （局域网内其它设备 / 手机）"
}

# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
cmd_show() {
  [ -z "$PY_BIN" ] && PY_BIN="$(find_python || echo /usr/bin/python3)"
  [ -z "$RUN_AS" ] && RUN_AS="$TARGET_USER"
  step "服务名    : $SVC_NAME（$SCOPE_DESC）"
  step "unit 路径 : $UNIT_FILE"
  step "解释器    : $PY_BIN"
  step "存储目录  : $DATA_DIR"
  step "运行身份  : $RUN_AS"
  [ -n "$PASSWORD" ] && step "访问密码  : 已设置（写入 $ENV_FILE，权限 600）"
  echo
  render_unit
}

cmd_install() {
  [ -f "$SERVER" ] || die "找不到 $SERVER"

  # 1) 解释器
  if [ -z "$PY_BIN" ]; then PY_BIN="$(find_python)" || die "未找到 Python 3，请先安装：sudo apt install python3"; fi
  [ -x "$PY_BIN" ] || die "解释器不可执行：$PY_BIN"
  local pyver; pyver="$("$PY_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo '?')"
  case "$pyver" in
    3.[7-9]|3.1[0-9]|4.*) ok "解释器：$PY_BIN（Python $pyver）" ;;
    *) warn "Python 版本为 $pyver，项目要求 3.7 及以上" ;;
  esac

  # 2) 存储目录
  if [ ! -d "$DATA_DIR" ]; then mkdir -p "$DATA_DIR"; ok "已创建存储目录：$DATA_DIR"; fi
  DATA_DIR="$(cd "$DATA_DIR" && pwd)"

  # 3) 运行身份：系统级默认取存储目录属主
  if [ -z "$RUN_AS" ]; then
    if [ "$SCOPE" = "system" ]; then RUN_AS="$(stat -c '%U' "$DATA_DIR")"; else RUN_AS="$TARGET_USER"; fi
  fi
  id "$RUN_AS" >/dev/null 2>&1 || die "运行身份不存在：$RUN_AS"
  ok "运行身份：$RUN_AS"
  if [ "$SCOPE" = "system" ] && [ "$RUN_AS" != "root" ] && [ "$ACT_UID" -eq 0 ]; then
    if ! runuser -u "$RUN_AS" -- test -w "$DATA_DIR" 2>/dev/null; then
      warn "$RUN_AS 对 $DATA_DIR 可能没有写权限，服务将无法保存上传的文件。"
      warn "  修复： sudo chown -R $RUN_AS:'$(id -gn "$RUN_AS")' '$DATA_DIR' '$SELF_DIR'"
    fi
  fi

  # 4) 另一个作用域若已装同名单服务，会与本服务抢同一个端口
  local other
  if [ "$SCOPE" = "system" ]; then other="$USER_UNIT"; else other="$SYSTEM_UNIT"; fi
  if [ -f "$other" ]; then
    warn "检测到另一个作用域也已安装：$other"
    warn "  两份服务会抢同一个端口 $PORT，建议先卸载其一。"
  fi

  # 5) 端口预检——端口不空闲时装了也起不来，直接中止而不是给出一串假成功
  if port_busy; then
    local pids; pids="$(port_occupant_pids | tr '\n' ' ' || true)"
    if [ "$FREE_PORT" = "1" ]; then
      step "端口 $PORT 已被占用（PID: ${pids:-未知}），按 --free-port 要求结束它"
      if [ -n "${pids// /}" ]; then run_kill "$pids"; else run_kill_by_name; fi
      sleep 1
      port_busy && die "端口 $PORT 仍被占用，请手动处理后重试"
      ok "端口已释放"
    elif [ "$FORCE" = "1" ]; then
      warn "端口 $PORT 已被占用（PID: ${pids:-未知}），按 --force 继续安装；服务可能起不来"
    else
      warn "端口 $PORT 已被占用（PID: ${pids:-未知}），服务无法绑定，安装已中止。"
      warn "  三选一："
      warn "    a) 结束占用者： sudo pkill -f 'server\\.py'   或   sudo fuser -k ${PORT}/tcp"
      warn "    b) 换端口安装： sudo $0 install --port 8766"
      warn "    c) 确认要抢占： sudo $0 install --free-port"
      exit 1
    fi
  fi

  # 6) 密码写入 600 的 EnvironmentFile
  if [ -n "$PASSWORD" ]; then
    ( umask 077; printf 'LANSHARE_PASSWORD=%s\n' "$PASSWORD" > "$ENV_FILE" )
    chmod 600 "$ENV_FILE"
    if [ "$SCOPE" = "system" ]; then chown root:root "$ENV_FILE" 2>/dev/null || true
    else chown "$CALLER_USER" "$ENV_FILE" 2>/dev/null || true; fi
    ok "密码已写入 $ENV_FILE（权限 600）"
  elif [ -f "$ENV_FILE" ]; then
    ok "沿用已存在的 $ENV_FILE"
  fi

  # 7) 落盘 unit
  if [ "$SCOPE" = "system" ]; then
    mkdir -p "$(dirname "$UNIT_FILE")"
  else
    mkdir -p "$USER_UNIT_DIR"
    [ "$ACT_UID" -eq 0 ] && chown -R "$CALLER_USER" "$USER_UNIT_DIR" 2>/dev/null || true
  fi
  if [ -f "$UNIT_FILE" ]; then
    cp -f "$UNIT_FILE" "$UNIT_FILE.bak.$(date +%Y%m%d%H%M%S)"
    ok "已备份旧 unit"
  fi
  render_unit > "$UNIT_FILE"
  chmod 644 "$UNIT_FILE"
  [ "$SCOPE" = "user" ] && [ "$ACT_UID" -eq 0 ] && chown "$CALLER_USER" "$UNIT_FILE" 2>/dev/null || true
  ok "已写入 $UNIT_FILE"

  # 8) 生效并启动
  "${SCTL[@]}" daemon-reload
  "${SCTL[@]}" enable "$SVC_NAME" >/dev/null
  ok "已设为开机自启"
  "${SCTL[@]}" restart "$SVC_NAME"

  # 9) 真实就绪探测：Type=simple 下 is-active 在进程刚拉起时就是 true，
  #    光看它会把「端口被占、进程随即要崩」也判成成功。必须验端口。
  step "等待服务监听 $PORT …"
  if wait_listening 12; then
    ok "服务已启动，端口 $PORT 正在监听"
  else
    warn "服务已启用，但 $PORT 没有监听起来。最近日志："
    "${JCTL[@]}" -u "$SVC_NAME" -n 20 --no-pager 2>/dev/null | sed 's/^/      /' >&2 || true
    echo >&2
    warn "常见原因：端口被其它进程占用、存储目录权限不足。"
    exit 1
  fi

  # 10) 用户级提示
  if [ "$SCOPE" = "user" ]; then
    local linger; linger="$(loginctl show-user "$CALLER_USER" -p Linger --value 2>/dev/null || echo '?')"
    if [ "$linger" = "yes" ]; then
      ok "linger 已开启：无需登录也会开机自启"
    else
      echo
      warn "用户级服务默认只在「登录之后」启动。若要做到开机即启（无需登录），执行一次："
      warn "  sudo loginctl enable-linger $CALLER_USER"
    fi
  fi

  echo
  step "完成（$SCOPE_DESC）。"
  show_addresses
  echo
  info "  查看状态： $0 status     查看日志： $0 logs -f     重启： $0 restart"
}

cmd_uninstall() {
  local removed=0
  if [ -f "$UNIT_FILE" ]; then
    "${SCTL[@]}" stop "$SVC_NAME" 2>/dev/null || true
    "${SCTL[@]}" disable "$SVC_NAME" >/dev/null 2>&1 || true
    rm -f "$UNIT_FILE"
    "${SCTL[@]}" daemon-reload 2>/dev/null || true
    "${SCTL[@]}" reset-failed "$SVC_NAME" 2>/dev/null || true
    ok "已移除 $UNIT_FILE（不再开机自启）"
    removed=1
  fi
  if [ -f "$ENV_FILE" ]; then rm -f "$ENV_FILE"; ok "已删除 $ENV_FILE"; fi

  # 另一个作用域可能还留着——顺手报出来，避免以为已经清干净
  local other
  if [ "$SCOPE" = "system" ]; then other="$USER_UNIT"; else other="$SYSTEM_UNIT"; fi
  if [ -f "$other" ]; then
    warn "另一个作用域仍有残留：$other"
    warn "  清除：$0 uninstall $([ "$SCOPE" = system ] && echo '--user' || echo '--system')"
  elif [ "$removed" = "0" ]; then
    info "未发现自启配置（$UNIT_FILE 不存在），无需卸载"
  fi
  info "共享文件池 $DATA_DIR 保持原样，未做任何清理。"
}

cmd_status() {
  step "自启状态：$SVC_NAME（restart / logs / uninstall 会作用于「$SCOPE_DESC」）"
  local shown=0
  for s in system user; do
    local unit label
    local -a sctl
    if [ "$s" = system ]; then
      unit="$SYSTEM_UNIT"; label="系统级"; sctl=(systemctl)
    else
      unit="$USER_UNIT"; label="用户级"
      if [ "$ACT_UID" -eq 0 ] && [ "$CALLER_USER" != "root" ]; then
        sctl=(runuser -u "$CALLER_USER" -- env "XDG_RUNTIME_DIR=/run/user/$CALLER_UID" systemctl --user)
      else
        sctl=(systemctl --user)
      fi
    fi
    echo
    printf '%s── %s  %s%s\n' "$C_B" "$label" "$unit" "$C_0"
    if [ ! -f "$unit" ]; then
      dim "     未安装"
      continue
    fi
    shown=1
    "${sctl[@]}" --no-pager --lines=0 status "$SVC_NAME" 2>&1 | head -8 | sed 's/^/     /' || true
    if [ "$s" = user ]; then
      local linger; linger="$(loginctl show-user "$CALLER_USER" -p Linger --value 2>/dev/null || echo '?')"
      dim "     linger（免登录自启）= $linger"
    fi
  done

  echo
  step "端口 $PORT："
  if port_busy; then
    local pids; pids="$(port_occupant_pids | tr '\n' ' ' | sed 's/ *$//')"
    if [ -n "$pids" ]; then ok "正在监听（PID: $pids）"; else ok "正在监听"; fi
    info "    http://$(lan_ip):$PORT/"
  else
    warn "没有进程在监听 $PORT"
  fi

  if [ "$shown" = "0" ]; then
    echo
    info "尚未安装自启。执行： sudo $0 install   或   $0 install --user"
  fi
}

cmd_logs() {
  [ -f "$UNIT_FILE" ] || die "尚未安装自启（找不到 $UNIT_FILE）。可执行： $0 install"
  if [ "${#LOGS_ARGS[@]}" -eq 0 ]; then
    "${JCTL[@]}" -u "$SVC_NAME" -n 200
  else
    "${JCTL[@]}" -u "$SVC_NAME" "${LOGS_ARGS[@]}"
  fi
}

cmd_lifecycle() {
  [ -f "$UNIT_FILE" ] || die "尚未安装自启（找不到 $UNIT_FILE）。可执行： $0 install"
  "${SCTL[@]}" "$CMD" "$SVC_NAME"
  sleep 1
  "${SCTL[@]}" --no-pager --lines=0 status "$SVC_NAME" 2>&1 | head -6 || true
}

resolve_scope
need_root_if_system

case "$CMD" in
  show)      cmd_show ;;
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  start|stop|restart) cmd_lifecycle ;;
esac
