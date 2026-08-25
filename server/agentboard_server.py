#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agentboard_server.py — 多 agent 协同实时看板 · 网页化后端(纯 stdlib,零依赖)
============================================================================

复用 agentboard.sh / hermes_board_discover.py 已落盘的看板数据,做成 HTTP 服务,
让前端页面拉取渲染。数据源与 bash 看板完全一致:
    $BOARD_ROOT/<run>/__discovered__/   (auto-discover 生成)
    $BOARD_ROOT/<run>/<agent>.*          (手动 start/log 登记的 agent)
其中 $BOARD_ROOT 默认 ~/.hermes/agent-board, run 默认当天日期(可用 AGENTBOARD_RUN 指定)。

每次轮询(默认 0.2s, 5Hz)重新扫盘 → 内存快照 → /api/data 返回 JSON。
另有 /api/events 返回事件流、/health 探活、/ 返回静态页面。

运行:
    python3 agentboard_server.py [--port 8710] [--root DIR] [--run RUN] [--interval 0.2]
    # 静态页默认从 web/index.html 读取(--web 可指定)。

数据模型(与 bash 版一致):
  进程卡: .pid .start .cmd .log .think
  cron卡: .cron   (name|state|last_status|sched|next_run|last_run|err)
状态:  running:<secs> / exited / nopid / cron:<last_status>
"""

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 轻量 WebSocket(纯 stdlib, RFC6455) ----
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


class WebSocket:
    """服务端 WebSocket 连接: 握手后可在独立线程收发帧。
    本看板仅用服务端→客户端推送(文本帧), 客户端不发业务消息。
    """

    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.closed = False
        self._lock = threading.Lock()
        # 升级握手必须由 handler 线程完成(因为它持有 HTTP 请求行/头)

    def _recv_frame(self):
        b = self.sock.recv(2)
        if len(b) < 2:
            raise ConnectionError("short header")
        b1, b2 = b[0], b[1]
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length)
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x8:  # close
            raise ConnectionError("close frame")
        return opcode, payload

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("eof")
            data += chunk
        return data

    def send_text(self, text):
        if self.closed:
            return
        payload = text.encode("utf-8")
        n = len(payload)
        head = bytearray([0x81])  # FIN + text
        if n < 126:
            head.append(n)
        elif n < 65536:
            head.append(126)
            head += struct.pack(">H", n)
        else:
            head.append(127)
            head += struct.pack(">Q", n)
        try:
            with self._lock:
                self.sock.sendall(bytes(head) + payload)
        except OSError:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


DEFAULT_ROOT = os.environ.get("AGENTBOARD_ROOT", os.path.expanduser("~/.hermes/agent-board"))
DEFAULT_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "index.html")
# 自动发现脚本: 周期重扫 /proc + codex sqlite, 刷新 __discovered__/
# 优先用本仓库自带的 discover/ 脚本(自包含), 找不到再回退到 HERMES_SCRIPTS / ~/.hermes
_DISCOVER_HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "discover",
                              "hermes_board_discover.py")
DISCOVER_SCRIPT = _DISCOVER_HERE if os.path.exists(_DISCOVER_HERE) else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts", "utils", "hermes_board_discover.py")
if not os.path.exists(DISCOVER_SCRIPT):
    cand = os.environ.get("HERMES_SCRIPTS")
    DISCOVER_SCRIPT = os.path.join(cand or os.path.expanduser("~/.hermes/scripts"), "utils",
                                   "hermes_board_discover.py")

# 与 agentboard.sh status_icon/status_of 对应的状态归类
RUNNING_WORDS = ("done", "ok", "success", "complete", "completed",
                 "running", "start", "started", "working")
ERROR_WORDS = ("error", "failed", "fail", "blocked", "stalled", "exited")
THINK_WORDS = ("think", "thinking")
WAIT_WORDS = ("waiting", "queued", "pending", "scheduled", "idle", "paused",
              "needs_input", "awaiting", "reviewing", "blocked_on", "on_hold")


def classify(status):
    """把任意 status 归一化成 {shell, label, color-group} 用于前端配色"""
    s = (status or "").lower()
    if s in ("done", "completed", "complete", "success", "finished"):
        return {"group": "idle", "label": "DONE"}
    if s in RUNNING_WORDS:
        return {"group": "running", "label": "RUNNING"}
    if s in ERROR_WORDS:
        return {"group": "error", "label": "ERROR"}
    if s in THINK_WORDS:
        return {"group": "thinking", "label": "THINKING"}
    if s in WAIT_WORDS:
        return {"group": "waiting", "label": "WAITING"}
    return {"group": "idle", "label": (status or "IDLE").upper()}


def find_run(root):
    run = os.environ.get("AGENTBOARD_RUN")
    if run:
        return run
    # 取最近有数据的 run(目录名倒序的字典序即日期倒序)
    try:
        runs = [d for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d)) and re.match(r"^\d{8}$", d)]
        if runs:
            return max(runs)
    except OSError:
        pass
    return time.strftime("%Y%m%d")


class BoardScanner:
    """扫描看板文件 → 内存快照(进程卡 + cron 卡 + 全局统计)。"""

    def __init__(self, root, run=None):
        self.root = os.path.abspath(root)
        self.run = run or find_run(root)
        self.board_dir = os.path.join(self.root, self.run)
        os.makedirs(self.board_dir, exist_ok=True)

    # ---- 进程卡 ----
    def read_agent(self, agent):
        base = os.path.join(self.board_dir, agent)          # 手动登记
        dbase = os.path.join(self.board_dir, "__discovered__", agent)  # 自动发现
        card = {"agent": agent, "source": "manual"}
        pidf = startf = cmdf = logf = thinkf = cronf = None
        # 优先 __discovered__
        for f in (dbase, base):
            if os.path.isfile(f + ".pid"):
                pidf, card["source"] = f + ".pid", "discovered"
                break
        for f in (dbase, base):
            if os.path.isfile(f + ".cron"):
                cronf = f + ".cron"
                break
        for f in (dbase, base):
            if os.path.isfile(f + ".start") and pidf:
                startf = f + ".start"
                break
        for f in (dbase, base):
            if os.path.isfile(f + ".cmd"):
                cmdf = f + ".cmd"
                break
        # log / think: 都可能有,取存在的那个目录
        for f in (dbase, base):
            if os.path.isfile(f + ".log"):
                logf = f + ".log"
                break
        for f in (dbase, base):
            if os.path.isfile(f + ".think"):
                thinkf = f + ".think"
                break

        if cronf:
            return self._cron_card(agent, cronf, thinkf)
        if not pidf:
            return None

        # ---- 进程存活 ----
        status = "nopid"
        dur = None
        pidv = self._read(pidf).strip()
        try:
            pid = int(pidv)
        except ValueError:
            pid = 0
        # pid=0 的会话卡: 无真实进程, 视为存活且用 .start 算时长
        alive = self._alive(pid) if pid else (pid == 0)
        if alive:
            start_epoch = self._read(startf).strip() if startf else ""
            try:
                dur = max(0, int(time.time()) - int(float(start_epoch)))
            except (ValueError, TypeError):
                dur = None
            status = "running"
        elif pid:
            status = "exited"
        else:
            status = "running"   # 无 pid 文件内容的兜底

        # 最后结构化事件
        last = self._last_event(logf, status, dur)
        cmd = self._read(cmdf).strip() if cmdf else ""
        think = self._think(thinkf)
        # 卡片展示归类(与 bash cell_collect 一致): 运行中进程→running(除非最后事件是 done);
        # exited 未标记 done→error; 否则取最后事件状态
        if status == "running":
            disp = last["status"] if last["status"] in ("done", "ok", "success", "complete", "completed") else "running"
        elif status in ("exited", "error"):
            disp = last["status"] if last["status"] in ("done", "ok", "success", "complete", "completed") else "exited"
        else:
            disp = last["status"]
        return {
            "agent": agent,
            "type": "proc",
            "source": "discovered" if dbase else "manual",
            "pid": pidv,
            "status": status,
            "dur": dur,
            "cmd": cmd,
            "cls": classify(disp),
            "last": last,
            "think": think,
        }

    @staticmethod
    def _read(f):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _last_event(self, logf, status, dur):
        line = ""
        if logf:
            try:
                with open(logf, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        pass
                line = (line or "").strip()
            except OSError:
                pass
        ts, stage, st, msg = "", "-", "idle", ""
        if line:
            parts = line.split("|", 3)
            if len(parts) >= 3:
                ts, stage, st = parts[0], parts[1], parts[2]
            msg = parts[3] if len(parts) == 4 else ""
            msg = msg.replace("\\|", "|")
        if status == "running" and st not in RUNNING_WORDS:
            # 保留更细的子状态(等待/思考/错误), 否则仅当完全无信息时兜底为 running
            if st not in WAIT_WORDS and st not in THINK_WORDS and st not in ERROR_WORDS:
                st = "running"
        return {"ts": ts, "stage": stage, "status": st, "cls": classify(st), "msg": msg, "dur": dur}

    def _cron_card(self, agent, cronf, thinkf):
        raw = self._read(cronf).strip()
        parts = raw.split("|") if raw else ["?"] * 7
        name = parts[0] or agent
        state = parts[1] if len(parts) > 1 else "?"
        lst = parts[2] if len(parts) > 2 else "-"
        sched = parts[3] if len(parts) > 3 else "?"
        next_run = parts[4] if len(parts) > 4 else "-"
        last_run = parts[5] if len(parts) > 5 else "-"
        err = parts[6] if len(parts) > 6 else ""
        st = lst if lst in RUNNING_WORDS or lst in ERROR_WORDS or lst in THINK_WORDS else "idle"
        return {
            "agent": agent,
            "type": "cron",
            "source": "discovered",
            "name": name,
            "state": state,
            "status": st,
            "sched": sched,
            "next_run": self._short_ts(next_run),
            "last_run": self._short_ts(last_run),
            "err": err,
            "cls": classify(lst),
            "think": self._think(thinkf),
        }

    @staticmethod
    def _short_ts(s):
        s = s.replace("T", " ").replace("Z", "")
        # 只保留 月-日 时:分:秒
        m = re.search(r"(\d{2}-\d{2}) (\d{2}:\d{2}(?::\d{2})?)", s)
        if m:
            return f"{m.group(1)} {m.group(2)}"
        m = re.search(r"(\d{2}):(\d{2})(?::\d{2})?$", s)
        if m:
            return f"{m.group(1)}:{m.group(2)}"
        return s[:16]

    def _think(self, thinkf):
        out = []
        if not thinkf:
            return out
        try:
            with open(thinkf, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    ln = ln.rstrip("\n")
                    if not ln:
                        continue
                    if "|" in ln:
                        hms, txt = ln.split("|", 1)
                    else:
                        hms, txt = "", ln
                    out.append({"ts": hms, "text": txt})
        except OSError:
            pass
        return out

    # ---- 扫描入口 ----
    def scan(self):
        agents = set()
        # __discovered__: .pid / .cron
        dd = os.path.join(self.board_dir, "__discovered__")
        if os.path.isdir(dd):
            for fn in os.listdir(dd):
                m = re.match(r"^(.*)\.(pid|cron)$", fn)
                if m:
                    agents.add(m.group(1))
        # 手动登记: 根目录 .pid/.log/.cron
        for fn in os.listdir(self.board_dir):
            m = re.match(r"^(.*)\.(pid|cron)$", fn)
            if m:
                agents.add(m.group(1))
        cards = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(self.read_agent, sorted(agents)))
        for r in results:
            if r:
                cards.append(r)
        return cards

    def _stats(self, cards, events):
        ntotal = len(cards)
        nrt = 0
        nerr = 0
        for c in cards:
            if c.get("type") == "cron":
                continue
            if c.get("status") == "running":
                nrt += 1
            elif c.get("status") in ("exited", "error"):
                nerr += 1
        return {"total": ntotal, "running": nrt, "error": nerr, "events": events}


class BoardStore:
    """持有快照 + 事件流,周期性刷新。变化时通过 WebSocket 实时推送。"""

    def __init__(self, root, run=None, interval=0.2):
        self.scanner = BoardScanner(root, run)
        self.interval = float(interval)
        self.stats = {}
        self.agents = []
        self.events = []
        self.total_events = 0
        self._clients = []          # WebSocket 连接
        self._clients_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._last_sig = None       # 上次推送的数据签名(去重,避免无变化时也推送)
        self._refresh()
        self._last_sig = self._sig()

    def add_client(self, ws):
        with self._clients_lock:
            self._clients.append(ws)
        # 新连接首次进入立即推一份当前快照(相当于首次拉取)
        self.broadcast()

    def remove_client(self, ws):
        with self._clients_lock:
            try:
                self._clients.remove(ws)
            except ValueError:
                pass

    def broadcast(self):
        """把当前快照推给所有连接的 WebSocket。
        无客户端或数据未变化时静默跳过(有变化判断由调用方或轮询对比负责)。
        """
        with self._clients_lock:
            clients = list(self._clients)
        if not clients:
            return
        msg = json.dumps(self.data(), ensure_ascii=False)
        dead = []
        for ws in clients:
            try:
                ws.send_text(msg)
            except Exception:
                dead.append(ws)
        if dead:
            with self._clients_lock:
                for ws in dead:
                    try:
                        self._clients.remove(ws)
                    except ValueError:
                        pass

    def _sig(self):
        """当前数据快照的签名: 内部文件 mtime_ns + size + 事件数汇总。变化才推送。"""
        parts = [self.total_events, self.stats.get("total"), self.stats.get("running")]
        # 纳秒级 mtime + size 能识别同一秒内多次追加的思维链刷新。
        dd = os.path.join(self.scanner.board_dir, "__discovered__")
        for d in (self.scanner.board_dir, dd):
            if not os.path.isdir(d):
                continue
            try:
                for fn in os.listdir(d):
                    if fn.endswith(('.log', '.think', '.pid', '.cron')):
                        path = os.path.join(d, fn)
                        st = os.stat(path)
                        parts.append(fn)
                        parts.append(st.st_mtime_ns)
                        parts.append(st.st_size)
            except OSError:
                pass
        return tuple(parts)

    def _collect_events(self):
        ev = []
        total = 0
        for fn in os.listdir(self.scanner.board_dir):
            if fn.endswith(".log") and not fn.startswith("_"):
                total += self._append_log(fn[:-4], os.path.join(self.scanner.board_dir, fn), ev)
        dd = os.path.join(self.scanner.board_dir, "__discovered__")
        if os.path.isdir(dd):
            for fn in os.listdir(dd):
                if fn.endswith(".log"):
                    total += self._append_log(fn[:-4], os.path.join(dd, fn), ev)
        ev.sort(key=lambda e: e["ts"], reverse=True)
        # 事件流只保留最新信息，避免把历史日志持续推送给前端。
        return ev[:12], total

    @staticmethod
    def _append_log(agent, path, ev):
        n = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    ln = ln.rstrip("\n")
                    if not ln:
                        continue
                    n += 1
                    parts = ln.split("|", 3)
                    if len(parts) < 3:
                        continue
                    ev.append({"agent": agent, "ts": parts[0], "stage": parts[1],
                               "status": parts[2], "cls": classify(parts[2]),
                               "msg": parts[3].replace("\\|", "|") if len(parts) == 4 else ""})
        except OSError:
            pass
        return n

    def _refresh(self):
        with self._refresh_lock:
            self.agents = self.scanner.scan()
            self.events, self.total_events = self._collect_events()
            self.stats = self.scanner._stats(self.agents, self.total_events)

    def data(self):
        return {"ts": int(time.time()), "run": self.scanner.run, "stats": self.stats,
                "agents": self.agents, "events": self.events}

    def run_loop(self):
        # 每轮先跑一次自动发现(将 /proc + codex sqlite 的最新状态刷成 __discovered__/ 文件),
        # 再做内存快照 → 前端才能看到 agent 的实时活动(否则静态文件停留在上次 discover 的时刻)。
        # discover 负责把外部会话/进程/思维链物化成 __discovered__ 文件。
        # 卡片实时性取决于它的写入频率,因此默认跟随 5Hz 刷新节奏。
        discover_span = max(0.2, self.interval)
        last_disc = [0.0]
        stop = [False]

        def _loop_discover():
            while not stop[0]:
                now = time.time()
                if now - last_disc[0] >= discover_span:
                    last_disc[0] = now
                    try:
                        with self._refresh_lock:
                            self._run_discover()
                    except Exception:
                        pass
                time.sleep(min(0.05, discover_span / 4))

        threading.Thread(target=_loop_discover, daemon=True).start()

        while not stop[0]:
            time.sleep(self.interval)
            try:
                self._refresh()
                sig = self._sig()
                if sig != self._last_sig:
                    self._last_sig = sig
                    self.broadcast()
            except Exception:
                pass

    def _run_discover(self):
        if not DISCOVER_SCRIPT or not os.path.exists(DISCOVER_SCRIPT):
            return
        env = dict(os.environ)
        env.setdefault("AGENTBOARD_ROOT", self.scanner.root)
        env.setdefault("AGENTBOARD_RUN", self.scanner.run)
        try:
            import subprocess
            subprocess.run(["python3", DISCOVER_SCRIPT], env=env, capture_output=True, timeout=30)
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
    store = None
    web_file = DEFAULT_WEB
    html_cache = None

    def log_message(self, *a):
        pass

    def _ws_upgrade(self):
        """RFC6455 握手 → 在 handler 线程同步读帧(收 close/ping), 直到连接断开。
        同步阻塞在 _ws_upgrade 内, 因此 finish()/close_connection 不会在半途关掉 socket。
        """
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_response(400)
            self.end_headers()
            return
        accept = ws_accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        # 阻止 http.server 在握手后继续用 HTTP 循环复用/关闭 socket
        self.close_connection = True
        ws = WebSocket(self.connection, self.client_address)
        store = self.store
        store.add_client(ws)
        try:
            while True:
                try:
                    opcode, payload = ws._recv_frame()
                except Exception:
                    break
                if opcode == 0x9:  # ping → pong
                    try:
                        ws.sock.sendall(b"\x8a" + bytes([len(payload)]) + payload)
                    except OSError:
                        break
        finally:
            ws.close()
            store.remove_client(ws)

    def _send_bytes(self, body, ctype="application/json; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/ws":
            self._ws_upgrade()
            return
        if path == "/health":
            self._send_bytes(b'{"ok":true}\n')
        elif path == "/api/data":
            self._send_bytes(json.dumps(self.store.data(), ensure_ascii=False).encode("utf-8"))
        elif path == "/api/events":
            self._send_bytes(json.dumps({"events": self.store.events}, ensure_ascii=False).encode("utf-8"))
        elif path == "/" or path == "/index.html":
            try:
                if self.html_cache is None:
                    with open(self.web_file, "r", encoding="utf-8") as f:
                        self.html_cache = f.read()
                self._send_bytes(self.html_cache.encode("utf-8"), "text/html; charset=utf-8")
            except OSError:
                self._send_bytes(b"index.html not found; set --web", "text/plain; charset=utf-8", 404)
        else:
            self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)


def main():
    ap = argparse.ArgumentParser(description="agentboard web backend")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AGENTBOARD_PORT", "8710")))
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--run", default=None)
    ap.add_argument("--interval", type=float, default=float(os.environ.get("AGENTBOARD_INTERVAL", "0.2")))
    ap.add_argument("--web", default=DEFAULT_WEB)
    args = ap.parse_args()

    store = BoardStore(args.root, args.run, args.interval)
    Handler.store = store
    Handler.web_file = os.path.abspath(args.web)

    import threading
    threading.Thread(target=store.run_loop, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"agentboard-web serving on http://127.0.0.1:{args.port} (run={store.scanner.run}, "
          f"interval={args.interval}s, root={args.root})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
