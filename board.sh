#!/usr/bin/env bash
# board — agent-board 看板 一键启动/查看 (轻量 bash, 零第三方依赖)
#
# 用法:
#   board                     打开浏览器访问看板(服务未运行则自动拉起)
#   board open                同上
#   board start               启动服务(幂等: 已在跑则跳过)
#   board stop                停止服务
#   board restart             重启服务
#   board status              查看运行状态
#   board log [n]             查看最近 n 行服务日志(默认 30)
#
# 环境变量:
#   AGENTBOARD_PORT   端口 (默认 8710)
#   AGENTBOARD_ROOT   数据根目录 (默认 ~/.hermes/agent-board)
#   SRV_PY            服务端脚本路径 (默认本仓库 server/agentboard_server.py)
#   SKIP_AUTO_INSTALL 1 时只检测/提示缺失依赖, 不自动安装 (默认自动安装)
#
# 依赖: python3 (>=3.8) + curl, 缺少时自动用 apt/dnf/yum/apk/brew 安装。
# 优先用 systemd 用户服务 (agentboard.service, 开机自启);
# 无 systemd 时回退为 nohup 后台进程, 保证任何机器上都能一键。
set -u

SRV_PY="${SRV_PY:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/server/agentboard_server.py}"
PORT="${AGENTBOARD_PORT:-8710}"
ROOT="${AGENTBOARD_ROOT:-$HOME/.hermes/agent-board}"
URL="http://127.0.0.1:$PORT"
SERVICE="agentboard.service"

say() { printf '%s\n' "$*"; }

# 服务是否可访问(HTTP 200)
is_up() { curl -s -o /dev/null -m 2 -w '%{http_code}' "$URL/" 2>/dev/null | grep -q '^200$'; }

# systemd 用户服务是否可用
has_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user list-unit-files "$SERVICE" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 依赖检测 / 自动安装
#   运行所需的仅两个: python3 (>=3.8, 服务端与 discover 用) + curl (健康检查用)。
#   缺了会尝试用 apt/dnf/yum/apk/brew 自动安装(装完复查), 失败则提示手动装。
#   环境变量 SKIP_AUTO_INSTALL=1 可关闭自动安装, 只报告缺失。
# ---------------------------------------------------------------------------
pkg_mgr() {
    command -v apt-get >/dev/null 2>&1 && { echo apt; return; }
    command -v dnf     >/dev/null 2>&1 && { echo dnf; return; }
    command -v yum     >/dev/null 2>&1 && { echo yum; return; }
    command -v apk     >/dev/null 2>&1 && { echo apk; return; }
    command -v brew    >/dev/null 2>&1 && { echo brew; return; }
    echo ""
}

have_python3() {
    command -v python3 >/dev/null 2>&1 \
        && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' 2>/dev/null
}

have_curl() { command -v curl >/dev/null 2>&1; }

install_pkgs() {
    local mgr; mgr=$(pkg_mgr)
    [ -z "$mgr" ] && { say "未找到包管理器 (apt/dnf/yum/apk/brew), 请手动安装: $*" >&2; return 1; }
    [ "${SKIP_AUTO_INSTALL:-0}" = "1" ] && {
        say "SKIP_AUTO_INSTALL=1, 跳过自动安装, 请手动安装: $*" >&2; return 1; }
    local SUDO=""
    [ "$(id -u 2>/dev/null)" != "0" ] && SUDO="sudo"
    say "检测到包管理器 $mgr, 正在安装: $* ..."
    case "$mgr" in
        apt) $SUDO apt-get update -y && $SUDO apt-get install -y "$@" ;;
        dnf) $SUDO dnf install -y "$@" ;;
        yum) $SUDO yum install -y "$@" ;;
        apk) apk add --no-cache "$@" ;;
        brew) brew install "$@" ;;
    esac
}

check_deps() {
    local missing=()
    have_python3 || missing+=("python3")
    have_curl    || missing+=("curl")
    [ "${#missing[@]}" -eq 0 ] && return 0
    say "缺少依赖: ${missing[*]}"
    install_pkgs "${missing[@]}" || return 1
    # 装完复查
    local still=()
    have_python3 || still+=("python3")
    have_curl    || still+=("curl")
    if [ "${#still[@]}" -eq 0 ]; then
        say "依赖安装完成: ${missing[*]}"
        return 0
    fi
    say "下列依赖仍未就绪, 请手动安装: ${still[*]}" >&2
    return 1
}

do_start() {
    check_deps || return 1
    if is_up; then
        say "看板已在运行: $URL"
        return 0
    fi
    if has_systemd; then
        systemctl --user start "$SERVICE" 2>&1
    else
        mkdir -p "$ROOT"
        nohup python3 "$SRV_PY" --port "$PORT" --root "$ROOT" \
            >>"$ROOT/server.log" 2>&1 &
        disown 2>/dev/null || true
    fi
    # 等待就绪(最多 25 秒)
    for _ in $(seq 1 50); do
        is_up && { say "看板已就绪: $URL"; return 0; }
        sleep 0.5
    done
    say "启动超时, 请查看日志: board log" >&2
    return 1
}

do_stop() {
    if has_systemd; then
        systemctl --user stop "$SERVICE" 2>&1
    else
        pkill -f "$SRV_PY" 2>/dev/null && say "已停止" || say "未在运行"
    fi
}

open_browser() {
    local cmd=""
    for c in xdg-open sensible-browser google-chrome firefox; do
        command -v "$c" >/dev/null 2>&1 && { cmd="$c"; break; }
    done
    if [ -n "$cmd" ]; then
        "$cmd" "$URL" >/dev/null 2>&1 &
        say "已打开: $URL"
    else
        say "未找到浏览器, 请手动访问: $URL"
    fi
}

log_tail() {
    local n="${1:-30}"
    if has_systemd; then
        journalctl --user -u "$SERVICE" -n "$n" --no-pager 2>&1
    else
        tail -n "$n" "$ROOT/server.log" 2>&1 || say "(无日志文件)"
    fi
}

case "${1:-open}" in
    start)  do_start ;;
    stop)   do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    status)
        check_deps || exit 1
        if is_up; then
            say "● 运行中: $URL"
            if has_systemd; then
                systemctl --user status "$SERVICE" --no-pager | head -5
            fi
        else
            say "○ 未运行 (可用: board start)"
        fi
        ;;
    log)
        log_tail "${2:-30}"
        ;;
    open|"")
        do_start && open_browser
        ;;
    *)
        sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
        ;;
esac
