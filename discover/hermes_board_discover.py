#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hermes_board_discover.py — agentboard.sh 自动发现数据源(纯 stdlib,零依赖)

把"系统里所有 agent"自动物化成看板条目,写入 $BOARD_DIR/__discovered__/。
每次运行先清空 __discovered__ 再重建 → 幂等、不留过期条目。
agentboard.sh 的 view/live 打开时自动调用本脚本(python3 探测 jobs.json + /proc)。

覆盖三类 agent(对应"Herres 相关全部"范围):
  1. cron 任务(调度 agent): 读 ~/.hermes/cron/jobs.json → 写 <cron_<id>>.cron 元数据卡
  2. 运行中的 hermes 会话 + gateway 守护进程(进程卡)
  3. 委派出去的子 agent 进程 codex / claude / opencode(进程卡)

board 文件约定(与 agentboard.sh 一致):
  进程卡:  __discovered__/<type>-<pid>.pid/.start/.cmd/.log    (走现有 card() 进程渲染)
  cron 卡: __discovered__/<cron_<id>>.cron                     (走 card() 的 .cron 分支)

从 /proc/*/cmdline 探测进程,避免 pgrep -f 自匹配。
BOARD_ROOT / AGENTBOARD_RUN 环境变量与 agentboard.sh 相同。
"""

import json
import os
import re
import time

BOARD_ROOT = os.environ.get("AGENTBOARD_ROOT", os.path.expanduser("~/.hermes/agent-board"))
RUN = os.environ.get("AGENTBOARD_RUN", time.strftime("%Y%m%d"))
OUT_DIR = os.path.join(BOARD_ROOT, RUN, "__discovered__")

CRON_JSON = os.path.expanduser("~/.hermes/cron/jobs.json")

# 时钟 HZ(通常 100)
try:
    HZ = os.sysconf("SC_CLK_TCK")
except (ValueError, OSError, AttributeError):
    HZ = 100

# ---- 进程探测(读 /proc,返回 {class: [(pid, start_epoch, cmdline)]}) ----
def scan_processes():
    found = {}
    here = os.getpid()
    # 系统启动时刻(epoch),来自 /proc/stat 的 btime
    boot_time = None
    try:
        with open("/proc/stat") as f:
            m = re.search(r"btime\s+(\d+)", f.read())
            boot_time = int(m.group(1)) if m else None
    except (OSError, AttributeError):
        boot_time = None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == here:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            with open(f"/proc/{pid}/comm", "rb") as f:
                pcomm = f.read().decode("utf-8", "replace").strip()
        except FileNotFoundError:
            continue
        except (PermissionError, ProcessLookupError):
            continue
        if not raw:
            continue
        # 进程启动 ticks(field 22 = starttime,位于 comm ')' 之后)
        # /proc/<pid>/stat: pid (comm) state ppid ...;comm 可能含空格/括号,
        # 取最后一个 ')' 之后按空白切分 → tokens[0]=state(field3) ... tokens[19]=starttime(field22)
        # comm 字段允许包含空格和括号，必须从最后一个 ')' 之后解析。
        after_rparen = stat.rsplit(")", 1)[-1] if ")" in stat else ""
        toks = after_rparen.split()
        try:
            start_ticks = int(toks[19])
        except (ValueError, IndexError):
            start_ticks = 0
        # ---- 分类 ----
        # cm 为规范化命令名(优先 comm,其次 cmdline 第一个 token basename)
        first_tok = raw.split()[0].split("/")[-1] if raw.split() else ""
        cm = pcomm or first_tok
        cls = None
        if "hermes_cli.main gateway run" in raw:
            cls = "gateway"
        elif "venv/bin/hermes" in raw and "gateway" not in raw:
            cls = "hermes"          # hermes CLI 会话(由活跃会话卡覆盖,见 write_hermes_session_cards)
        elif cm in ("codex", "codexjs") or re.search(r"(\s|/)codex(\s|$)", raw):
            cls = "codex"
        elif cm in ("claude",) or re.search(r"(\s|/)claude(\s|$)", raw):
            cls = "claude"
        elif cm == "opencode" or re.search(r"(\s|/)opencode(\s|$)", raw):
            cls = "opencode"
        elif cm == "pi" or re.search(r"(\s|/)pi([\s-]|$)", raw):
            cls = "pi"
        elif cm == "dsh" or re.search(r"(\s|/)dsh(\s|$)", raw):
            cls = "dsh"
        elif cm == "prime-agent" or re.search(r"(\s|/)prime-agent(\s|$)", raw):
            cls = "prime"
        if not cls:
            continue
        found.setdefault(cls, []).append((pid, start_ticks, raw, boot_time))
    return found

PROC_LABEL = {"gateway": "gateway", "codex": "codex", "claude": "claude",
              "opencode": "opencode", "pi": "pi", "dsh": "dsh", "prime": "prime"}

# ── 思维链提取(.think 文件),每个 agent 独立的来源 ──
#   hermes: 以 state.db "活跃 cli 会话"为 agent —— 每个会话读【它自己】的最近推理/输出(真正独立)
#   codex:  只读 codex_reasoning_items(自己的), 没有就没有
#   cron:   读各自 ~/.hermes/cron/output/<jobid> 的最近产出
STATE_DB = os.path.expanduser("~/.hermes/state.db")

def _connect_db():
    import sqlite3
    return sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=2)

def _session_think(session_id, limit=2):
    """某个会话自己的最近 2 条思维(有 reasoning 列优先,否则最新 assistant 内容)"""
    out = []
    try:
        con = _connect_db()
        cur = con.cursor()
        rows = cur.execute("""
            SELECT timestamp, reasoning, reasoning_content, content
            FROM messages
            WHERE session_id=? AND role='assistant'
              AND ( (reasoning IS NOT NULL AND length(reasoning)>0)
                    OR (reasoning_content IS NOT NULL AND length(reasoning_content)>0)
                    OR (content IS NOT NULL AND length(content)>0) )
            ORDER BY timestamp DESC LIMIT ?
        """, (session_id, limit * 2)).fetchall()
        for ts, r, rc, c in rows:
            txt = (r or rc or c or "").strip()
            if not txt:
                continue
            hms = time.strftime("%H:%M:%S", time.localtime(float(ts)))
            out.append((hms, txt))
            if len(out) >= limit:
                break
        con.close()
    except Exception:
        pass
    return out

def _active_cli_sessions():
    """state.db 中活跃的 cli 会话(未结束 + 近 2h 有活动)→ [(id, cwd, model, started_at, last_act, title, repo)]"""
    out = []
    try:
        con = _connect_db()
        cur = con.cursor()
        now = time.time()
        rows = cur.execute("""
            SELECT id, cwd, model, started_at, last_activity_at, COALESCE(title,''), COALESCE(git_repo_root,'')
            FROM sessions
            WHERE source='cli' AND (ended_at IS NULL OR ended_at='')
              AND last_activity_at > ?
            ORDER BY last_activity_at DESC
        """, (now - 7200,)).fetchall()
        for r in rows:
            out.append(r)
        con.close()
    except Exception:
        pass
    return out

def _first_lines(txt, n=2, maxlen=90):
    """取文本前 n 个非空行,每行截到 maxlen"""
    lines = []
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            continue
        lines.append(s[:maxlen])
        if len(lines) >= n:
            break
    return lines

def write_think_file(base, entries):
    """entries: [(hms, text)...]; 写成 H:M:S|text 每行, 最多 5 行(撑满卡片 5 行内容)"""
    with open(base + ".think", "w", encoding="utf-8") as f:
        for hms, txt in entries[:5]:
            for ln in _first_lines(txt, n=1):
                flat = " ".join(ln.split())  # 压空白
                f.write(f"{hms}|{flat[:160]}\n")

def write_hermes_session_cards():
    """hermes agent = 活跃 cli 会话;每个会话一张卡,思维链各自独立"""
    n = 0
    for sid, cwd, model, started_at, last_act, title, repo in _active_cli_sessions():
        short = sid.split("_", 2)[-1][:8] or sid
        base = os.path.join(OUT_DIR, f"hermes-{short}")
        now = time.time()
        # pid=0: 会话存活态(无真实进程); .start=会话开始时间 → 展示会话已进行时长
        with open(base + ".pid", "w") as f:
            f.write("0")
        with open(base + ".start", "w") as f:
            f.write(str(int(started_at if isinstance(started_at, (int, float)) else now)))
        # cmd: cwd / repo / model(可读标签)
        loc = (repo or cwd or "")
        label = title.strip() or os.path.basename(loc or "") or short
        with open(base + ".cmd", "w") as f:
            f.write(f"cwd={loc} model={model}")
        # 会话自己的思维链(独立!)
        write_think_file(base, _session_think(sid, 5))
        # 事件行: 名称用 title/cwd 标签, 便于识别
        labelf = label.replace("|", "/")
        with open(base + ".log", "w", encoding="utf-8") as f:
            ts = time.strftime("%H:%M:%S")
            f.write(f"{ts}|running|running|{labelf[:80]}|full_marker\n")
        n += 1
    return n

def _pi_think(pid, limit=2):
    """pi: 读 ~/.pi/agent/sessions/<slug>/ 下最新 JSONL 的 assistant 文本(按进程 cwd 匹配项目)"""
    out = []
    try:
        # 进程 cwd → slug
        cwd = os.readlink(f"/proc/{pid}/cwd")
        slug = "--" + cwd.lstrip("/").rstrip("/").replace("/", "-") + "--"
        sdir = os.path.expanduser(f"~/.pi/agent/sessions/{slug}")
        if not os.path.isdir(sdir):
            return out
        files = [os.path.join(sdir, x) for x in os.listdir(sdir) if x.endswith(".jsonl")]
        if not files:
            return out
        newest = max(files, key=os.path.getmtime)
        items = []
        with open(newest, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "message":
                    continue
                m = o.get("message") or {}
                if m.get("role") != "assistant":
                    continue
                ts = o.get("timestamp") or m.get("timestamp") or 0
                # 提取 content 数组里的 text
                txt = ""
                c = m.get("content")
                if isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text":
                            txt += part.get("text", "") + " "
                elif isinstance(c, str):
                    txt = c
                txt = txt.strip()
                if not txt:
                    continue
                try:
                    hms = time.strftime("%H:%M:%S", time.localtime(float(ts)))
                except Exception:
                    hms = ""
                items.append((hms, txt))
        for hms, txt in items[-limit:][::-1]:
            out.append((hms or "", txt))
    except Exception:
        pass
    return out[:limit]

def _prime_think(pid, limit=2):
    """prime: 按 pid 从 ~/.prime/agent/session-leases/*/ 找到它自己的 sessionPath,
    只读该会话 JSONL 的 assistant 文本 —— 每个 prime 进程(worker daemon)独立显示自己的思维链."""
    out = []
    try:
        path = _prime_session_for_pid(pid)
        if not path or not os.path.isfile(path):
            return out
        items = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") != "message":
                        continue
                    m = o.get("message") or {}
                    if m.get("role") != "assistant":
                        continue
                    ts = o.get("timestamp") or m.get("timestamp") or 0
                    txt = ""
                    c = m.get("content")
                    if isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                                txt += part.get("text", "") + " "
                    elif isinstance(c, str):
                        txt = c
                    txt = txt.strip()
                    if not txt:
                        continue
                    try:
                        hms = time.strftime("%H:%M:%S", time.localtime(float(ts)))
                        sortkey = float(ts)
                    except Exception:
                        hms = ts
                        sortkey = 0
                    items.append((sortkey, hms, txt))
        except OSError:
            pass
        for _, hms, txt in sorted(items, key=lambda x: x[0])[-limit:][::-1]:
            out.append((hms or "", txt))
    except Exception:
        pass
    return out[:limit]


def _prime_session_for_pid(pid):
    """从 ~/.prime/agent/session-leases/*/*.lock 里反查该 pid 对应的 sessionPath。
    每个 prime 进程持有一个锁文件(supervisor 132 线程之一),里面记录了它服务的会话。"""
    try:
        base = os.path.expanduser("~/.prime/agent/session-leases")
        if not os.path.isdir(base):
            return None
        picks = []
        for lockdir in os.listdir(base):
            d = os.path.join(base, lockdir)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        o = json.load(fh)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                if o.get("pid") != pid:
                    continue
                sp = o.get("sessionPath")
                if sp:
                    # 多个锁同 pid: 优先 paths 含 /sessions/ 的主会话, 否则取最新的
                    picks.append(("sessions/" in sp, o.get("createdAt") or "", sp))
        if not picks:
            return None
        picks.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return os.path.expanduser(picks[0][2])
    except Exception:
        return None


def _parse_prime_session(path):
    """解析单个 prime 会话 JSONL, 返回 dict:
    session_id / start / task(来自 custom_message 的 [task from parent]) /
    think([(hms,text)...]) / task_state(最近 agent_status) / last_hms
    """
    info = {"session_id": "", "start": None, "task": "", "think": [], "task_state": "", "last_hms": ""}
    items = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                typ = o.get("type")
                if typ == "session":
                    info["session_id"] = o.get("id") or info["session_id"]
                    if info["start"] is None:
                        try:
                            info["start"] = float(o.get("timestamp") or 0)
                        except Exception:
                            pass
                elif typ == "custom_message":
                    content = o.get("content") or ""
                    if "[task from parent]" in content and not info["task"]:
                        info["task"] = content.replace("[task from parent]", "").strip()
                elif typ == "agent_status":
                    st = (o.get("status") or {}).get("taskState") or ""
                    if st:
                        info["task_state"] = st
                        latest_ts = o.get("timestamp") or ""
                        try:
                            info["last_hms"] = time.strftime("%H:%M:%S",
                                time.localtime(float(o.get("timestamp") or 0)))
                        except Exception:
                            pass
                elif typ == "message":
                    m = o.get("message") or {}
                    if m.get("role") != "assistant":
                        continue
                    txt = ""
                    c = m.get("content")
                    if isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                                txt += part.get("text", "") + " "
                    elif isinstance(c, str):
                        txt = c
                    txt = txt.strip()
                    if not txt:
                        continue
                    try:
                        hms = time.strftime("%H:%M:%S", time.localtime(float(o.get("timestamp") or 0)))
                        sortkey = float(o.get("timestamp") or 0)
                    except Exception:
                        hms, sortkey = "", 0
                    items.append((sortkey, hms, txt))
    except OSError:
        pass
    items.sort(key=lambda x: x[0])
    info["think"] = [(h, t) for _, h, t in items[-5:][::-1]]
    return info


def _prime_subagents():
    """扫 ~/.prime/agent/session-artifacts/<parent>/sub-* 下所有并行子 agent 会话,
    返回 [{agent, pid, start, task, think, task_state, last_hms}, ...] —— 每个子 agent 一张独立卡.
    """
    out = []
    base = os.path.expanduser("~/.prime/agent/session-artifacts")
    if not os.path.isdir(base):
        return out
    try:
        for parent in sorted(os.listdir(base)):
            pdir = os.path.join(base, parent)
            if not os.path.isdir(pdir):
                continue
            for sub in sorted(os.listdir(pdir)):
                if not sub.startswith("sub-"):
                    continue
                sdir = os.path.join(pdir, sub)
                if not os.path.isdir(sdir):
                    continue
                jsonl = [os.path.join(sdir, fn) for fn in os.listdir(sdir)
                         if fn.endswith(".jsonl")]
                if not jsonl:
                    continue
                # 一个 sub 目录可能有多份历史 JSONL，只取最新文件，避免同名卡互相覆盖。
                session_file = max(jsonl, key=lambda p: os.path.getmtime(p))
                info = _parse_prime_session(session_file)
                if not info["session_id"]:
                    continue
                # 权威完成态: rlm-subagent.json 的 status
                meta_status = ""
                meta_path = os.path.join(sdir, "rlm-subagent.json")
                if os.path.isfile(meta_path):
                    try:
                        with open(meta_path, encoding="utf-8", errors="replace") as fh:
                            meta_status = (json.load(fh).get("status") or "")
                    except Exception:
                        pass
                # 归属的父 daemon pid: 仅作为诊断信息保留，卡片本身使用虚拟 PID。
                parent_pid = _prime_pid_for_session(parent)
                short = info["session_id"][-8:]
                # 状态归一: 已完成→done; 等待输入→waiting; 其余→running
                meta_status = str(meta_status or "").strip().lower()
                task_state = str(info["task_state"] or "").strip().lower()
                fin = meta_status in ("completed", "done", "success", "finished") or \
                    task_state in ("completed", "done", "success", "finished")
                if fin:
                    status = "done"
                elif task_state in ("needs_input", "waiting", "awaiting"):
                    status = "waiting"
                else:
                    status = "running"
                out.append({
                    "agent": f"prime-sub-{short}",
                    "pid": parent_pid,
                    "start": info["start"],
                    "task": info["task"],
                    "think": info["think"],
                    "status": status,
                    "task_state": task_state,
                    "last_hms": info["last_hms"],
                })
    except Exception:
        pass
    return out


def _prime_pid_for_session(session_id_prefix):
    """从 session-leases 反查: 给定父会话 id(前缀)返回它所属的 daemon pid(找不到返回 None)."""
    try:
        ldir = os.path.expanduser("~/.prime/agent/session-leases")
        if not os.path.isdir(ldir):
            return None
        for lockdir in os.listdir(ldir):
            d = os.path.join(ldir, lockdir)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                try:
                    o = json.load(open(os.path.join(d, fn), encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                sp = o.get("sessionPath") or ""
                if session_id_prefix in sp:
                    return o.get("pid")
    except Exception:
        pass
    return None


def write_prime_subagent_cards():
    """为每个 prime 并行子 agent 生成独立看板卡,各自显示自己的任务与思维链."""
    n = 0
    for s in _prime_subagents():
        base = os.path.join(OUT_DIR, s["agent"])
        now = time.time()
        # 子 agent 没有可可靠关联的独立 OS PID，使用虚拟会话 PID 0，
        # 不能再因为 lease 反查失败而整张卡消失。
        with open(base + ".pid", "w") as f:
            f.write("0")
        start_epoch = int(s["start"]) if s["start"] else int(now)
        with open(base + ".start", "w") as f:
            f.write(str(start_epoch))
        # cmd: 取任务里「任务：」那行作为简短可区分的描述, 否则回流式前几行
        title = ""
        for ln in (s["task"] or "").splitlines():
            st = ln.strip()
            if st.startswith("任务"):
                title = st.split("：", 1)[-1] if "：" in st else st
                break
        if not title:
            tf = _first_lines(s["task"] or "(prime sub-agent)", n=1, maxlen=200)
            title = tf[0] if tf else "(prime sub-agent)"
        cmd = " ".join(title.split())[:120]
        with open(base + ".cmd", "w") as f:
            f.write(cmd)
        # log: 最新活动时间 + 真实状态(已完成→done/等待→waiting/运行中→running)
        status_word = s["status"] or "running"
        done_mark = " [completed]" if status_word == "done" else ""
        if s["last_hms"]:
            with open(base + ".log", "w") as f:
                f.write(f"{s['last_hms']}|active|{status_word}|{cmd[:90]}{done_mark}\n")
        else:
            ts = time.strftime("%H:%M:%S")
            with open(base + ".log", "w") as f:
                f.write(f"{ts}|launched|{status_word}|{cmd[:90]}{done_mark}\n")
        if s["think"]:
            write_think_file(base, s["think"])
        n += 1
    return n


def _codex_think(limit=2):
    """codex: 读 ~/.codex/sessions/ 下最新 rollout JSONL 的 assistant 文本"""
    import ast
    out = []
    try:
        base = os.path.expanduser("~/.codex/sessions")
        files = []
        for root, _, fns in os.walk(base):
            for fn in fns:
                if fn.endswith(".jsonl"):
                    files.append(os.path.join(root, fn))
        if not files:
            return out
        newest = max(files, key=os.path.getmtime)
        items = []
        with open(newest, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "response_item":
                    continue
                try:
                    pl = ast.literal_eval(o["payload"]) if isinstance(o["payload"], str) else o["payload"]
                except Exception:
                    continue
                if not isinstance(pl, dict) or pl.get("type") != "message":
                    continue
                content = pl.get("content")
                if not isinstance(content, list):
                    continue
                txt = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")).strip()
                if not txt:
                    continue
                try:
                    hms = time.strftime("%H:%M:%S", time.localtime(float(o["timestamp"])))
                except Exception:
                    hms = ""
                items.append((hms, txt))
        for hms, txt in items[-limit:][::-1]:
            out.append((hms, txt))
    except Exception:
        pass
    return out[:limit]


def _codex_rollout_for_cwd(cwd):
    """在 ~/.codex/sessions/<year>/<mon>/ 下找 cwd 与给定路径最匹配、且最新的 rollout 会话文件。
    返回 (path, session_id) 或 (None, None)."""
    try:
        if not cwd:
            return None, None
        cwd = os.path.realpath(cwd)
        base = os.path.expanduser("~/.codex/sessions")
        best = None
        best_id = None
        best_score = -1
        best_mtime = 0
        for root, _, fns in os.walk(base):
            for fn in fns:
                if not fn.endswith(".jsonl") or not fn.startswith("rollout-"):
                    continue
                path = os.path.join(root, fn)
                sid = None
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            try:
                                o = json.loads(line)
                            except Exception:
                                continue
                            if o.get("type") == "session_meta":
                                sid = (o.get("payload") or {}).get("session_id")
                                break
                        fh.seek(0)
                        rcwd = None
                        for line in fh:
                            try:
                                o = json.loads(line)
                            except Exception:
                                continue
                            if o.get("type") == "session_meta":
                                rcwd = (o.get("payload") or {}).get("cwd") or ""
                                break
                except OSError:
                    continue
                if not rcwd:
                    continue
                rpath = os.path.realpath(rcwd)
                # 完全一致优先; 否则路径前缀匹配, 越深越优
                if rpath == cwd:
                    score = 1000
                elif cwd.startswith(rpath + os.sep) or rpath.startswith(cwd + os.sep):
                    score = min(len(rpath), len(cwd))
                else:
                    continue
                # 同分数下取最新
                mtime = os.path.getmtime(path)
                if score > best_score or (score == best_score and mtime > best_mtime):
                    best, best_id, best_score, best_mtime = path, sid, score, mtime
        return best, best_id
    except Exception:
        return None, None


def _codex_exec_think(pid, limit=5):
    """codex exec(只读规划/实施)会话: 按进程 cwd 匹配它自己的 rollout, 读该会话 assistant 思维链.
    与全局 app-server 日志解耦 —— 不同 codex 会话各显示各的."""
    import ast
    out = []
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        path, _sid = _codex_rollout_for_cwd(cwd)
        if not path:
            return out
        items = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "response_item":
                    continue
                try:
                    pl = ast.literal_eval(o["payload"]) if isinstance(o["payload"], str) else o["payload"]
                except Exception:
                    continue
                if not isinstance(pl, dict) or pl.get("type") != "message":
                    continue
                if pl.get("role") != "assistant":
                    continue
                content = pl.get("content")
                if not isinstance(content, list):
                    continue
                txt = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")).strip()
                if not txt:
                    continue
                try:
                    hms = time.strftime("%H:%M:%S", time.localtime(float(o["timestamp"])))
                    sk = float(o["timestamp"])
                except Exception:
                    hms, sk = "", 0
                items.append((sk, hms, txt))
        for _, hms, txt in sorted(items)[-limit:][::-1]:
            out.append((hms, txt))
    except Exception:
        pass
    return out[:limit]


def _codex_server_activity(pid=None, limit=2):
    """codex app-server(常驻服务): 读 ~/.codex/logs_2.sqlite 的 logs 表, 取最新活动时间 + 有内容的日志行.

    普通 codex rollout 会话没有 logs_2.sqlite 写入时返回空; 存在则说明是常驻 app-server.
    返回 (last_hms, [(hms, text)...]) —— last_hms 用于刷新卡片 .log 的"最后事件时间",
    think 条目填最近有实质性内容的日志行, 让看板卡片能实时反映这个服务的活动.
    """
    db = os.path.expanduser("~/.codex/logs_2.sqlite")
    if not os.path.exists(db):
        return None, []
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        cur = con.cursor()
        # 最新活动时间(整表最大 ts)
        row = cur.execute("SELECT MAX(ts) FROM logs").fetchone()
        last_hms = None
        if row and row[0]:
            try:
                last_hms = time.strftime("%H:%M:%S", time.localtime(float(row[0])))
            except Exception:
                last_hms = None
        # 最近有实质内容的 INFO/非遥测日志行作为思维/事件
        items = []
        try:
            rows = cur.execute(
                "SELECT ts, feedback_log_body FROM logs "
                "WHERE level='INFO' AND feedback_log_body IS NOT NULL "
                "  AND trim(feedback_log_body) != '' "
                "  AND target NOT LIKE '%otel%' AND target NOT LIKE '%feedback_tags%' "
                "  AND target NOT LIKE '%custom_ca%' "
                "ORDER BY ts DESC LIMIT 20"
            ).fetchall()
        except Exception:
            rows = []
        con.close()
        seen = set()
        for ts, body in rows:
            txt = (body or "").strip()
            if not txt:
                continue
            key = txt[:60]
            if key in seen:
                continue
            seen.add(key)
            try:
                hms = time.strftime("%H:%M:%S", time.localtime(float(ts))) if ts else ""
            except Exception:
                hms = ""
            # 压空白、截断、去控制字符
            flat = " ".join(txt.split())[:160]
            if flat:
                items.append((hms, flat))
            if len(items) >= limit:
                break
    except Exception:
        return None, []
    return last_hms, items


def _opencode_think(pid, limit=2):
    """opencode: 读 opencode.db 的 part 表 reasoning 类型(按进程 cwd 匹配最近会话)"""
    out = []
    db = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.exists(db):
        return out
    try:
        import sqlite3
        cwd = os.readlink(f"/proc/{pid}/cwd")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        cur = con.cursor()
        # 最近会话: 目录==cwd 或 cwd 是会话目录的子目录(取最接近的)
        rows = cur.execute(
            "SELECT id, directory, time_updated FROM session "
            "WHERE time_archived IS NULL ORDER BY time_updated DESC LIMIT 15"
        ).fetchall()
        best, best_len = None, -1
        for sid, sdir, _t in rows:
            sdir = (sdir or "").rstrip("/")
            if not sdir:
                continue
            if cwd == sdir or cwd.startswith(sdir + "/"):
                L = len(sdir)
                if L > best_len:
                    best, best_len = sid, L
        if not best:
            con.close()
            return out
        # 该会话最近的 reasoning parts
        day = 86400 * 2  # 只取近 2 天
        rows = cur.execute(
            "SELECT p.time_created, p.data FROM part p "
            "WHERE p.session_id=? AND json_extract(p.data,'$.type')='reasoning' "
            "  AND json_extract(p.data,'$.text') IS NOT NULL AND length(json_extract(p.data,'$.text'))>0 "
            "  AND p.time_created > ? "
            "ORDER BY p.time_created DESC LIMIT ?",
            (best, int(time.time() * 1000) - day * 1000, limit * 3),
        ).fetchall()
        con.close()
        # 某些 opencode 版本把"推理"存成 text part 而非 reasoning part:
        # 没有 reasoning 时回退到该会话最近的 assistant text 输出(同样是思维过程)
        if not rows:
            out = _opencode_text_think(best, limit)
            return out
        for ts, data in rows:
            try:
                o = json.loads(data)
                txt = (o.get("text") or "").strip()
            except Exception:
                continue
            if not txt:
                continue
            hms = time.strftime("%H:%M:%S", time.localtime(ts / 1000))
            out.append((hms, txt))
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out[:limit]


def _opencode_text_think(session_id, limit=2):
    """回退: 读 opencode 会话里最近的 assistant text 输出(部分版本思考即 text)。"""
    out = []
    db = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if not os.path.exists(db):
        return out
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        cur = con.cursor()
        day = 86400 * 2
        rows = cur.execute(
            "SELECT p.time_created, p.data FROM part p "
            "WHERE p.session_id=? AND json_extract(p.data,'$.type')='text' "
            "  AND json_extract(p.data,'$.text') IS NOT NULL AND length(json_extract(p.data,'$.text'))>20 "
            "  AND p.time_created > ? "
            "ORDER BY p.time_created DESC LIMIT ?",
            (session_id, int(time.time() * 1000) - day * 1000, limit),
        ).fetchall()
        con.close()
        for ts, data in rows:
            try:
                o = json.loads(data)
                txt = (o.get("text") or "").strip()
            except Exception:
                continue
            if not txt:
                continue
            hms = time.strftime("%H:%M:%S", time.localtime(ts / 1000))
            out.append((hms, txt))
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out[:limit]

def write_process_cards(procs):
    """codex/claude/opencode/pi 进程卡(各自独立思维源); gateway 不显示"""
    if not procs:
        return 0
    n = 0
    now = time.time()
    for cls, items in procs.items():
        if cls in ("hermes", "gateway"):
            continue  # hermes 由活跃会话卡覆盖; gateway 用户要求不显示
        label = PROC_LABEL[cls]
        for pid, start_ticks, raw, boot_time in items:
            # codex node 启动器(node 包装 codex exec): 真实 codex 二进制卡已代表同一会话, 跳过不重复显示
            if cls == "codex" and raw.split() and raw.split()[0].split("/")[-1] in ("node",):
                continue
            is_app_server = cls == "codex" and "app-server" in raw
            base = os.path.join(OUT_DIR, f"{label}-{pid}")
            if boot_time is not None:
                start_epoch = int(boot_time + start_ticks / HZ)
            else:
                start_epoch = int(now)
            with open(base + ".pid", "w") as f:
                f.write(str(pid))
            with open(base + ".start", "w") as f:
                f.write(str(start_epoch))
            cmd = " ".join(raw.split())[:200]
            with open(base + ".cmd", "w") as f:
                f.write(cmd)
            # codex: app-server 从 sqlite 同步活动; exec 会话用自己 rollout 的思维
            last_hms = None
            server_ents = []
            exec_ents = []
            if cls == "codex" and is_app_server:
                last_hms, server_ents = _codex_server_activity(pid, 3)
            elif cls == "codex":
                exec_ents = _codex_exec_think(pid, 5)
            with open(base + ".log", "w") as f:
                if cls == "codex" and is_app_server and last_hms:
                    # 先写启动兜底，再写真实活动；读取端取最后一条事件。
                    f.write(f"{time.strftime('%H:%M:%S')}|launched|running|pid={pid} :: {cmd[:90]}\n")
                    act_msg = server_ents[0][1] if server_ents else "(server busy / no INFO log)"
                    f.write(f"{last_hms}|active|running|{act_msg[:130]}\n")
                elif cls == "codex" and exec_ents:
                    # exec 会话: 用最近一条思维做活动事件；它必须压过启动兜底事件。
                    f.write(f"{time.strftime('%H:%M:%S')}|launched|running|pid={pid} :: codex exec{(' · '+cmd.split('codex')[-1][:60]) if 'codex' in cmd else ''}\n")
                    f.write(f"{exec_ents[0][0]}|active|running|{exec_ents[0][1][:130]}\n")
                else:
                    ts = time.strftime("%H:%M:%S")
                    f.write(f"{ts}|launched|running|pid={pid} :: {cmd[:90]}\n")
            # 各自的思维链(独立来源)
            ents = []
            if cls == "codex":
                # app-server: 自带日志; exec 会话: 自己的 rollout 思维链
                ents = server_ents if is_app_server else exec_ents
            elif cls == "pi":
                ents = _pi_think(pid, 5)
            elif cls == "prime":
                ents = _prime_think(pid, 5)
            elif cls == "opencode":
                ents = _opencode_think(pid, 5)
            if ents:
                write_think_file(base, ents)
            n += 1
    return n

def write_cron_cards(jobs):
    if not jobs:
        return 0
    n = 0
    for j in jobs:
        jid = j.get("id", "?")
        base = os.path.join(OUT_DIR, f"cron_{jid}")
        # 元数据卡: state|last_status|schedule|next_run|last_run|error(截断,tab 换行转义)
        state = j.get("state", "?")
        last_status = j.get("last_status", "-") or "-"
        sched = (j.get("schedule") or {}).get("expr", "?") if isinstance(j.get("schedule"), dict) else "?"
        next_run = (j.get("next_run_at") or "-")
        last_run = (j.get("last_run_at") or "-")
        err = (j.get("last_error") or "").replace("\n", " ").replace("|", "/")[:110] if j.get("last_error") else ""
        # 名称里可能含 | / ,做安全化(文件名)
        name = (j.get("name") or jid).replace("/", "_").replace("|", "-")
        with open(base + ".cron", "w", encoding="utf-8") as f:
            f.write(f"{name}|{state}|{last_status}|{sched}|{next_run}|{last_run}|{err}\n")
        # 思维链: 读该 cron 最近一次产出报告的前几行
        odir = os.path.expanduser(f"~/.hermes/cron/output/{jid}/")
        entries = []
        try:
            files = [os.path.join(odir, x) for x in os.listdir(odir) if x.endswith(".md")]
            if files:
                newest = max(files, key=os.path.getmtime)
                with open(newest, encoding="utf-8", errors="replace") as f:
                    txt = f.read()
                # 时间用文件的 mtime
                hms = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(newest)))
                for ln in _first_lines(txt, n=2):
                    entries.append((hms, " ".join(ln.split())))
        except Exception:
            pass
        write_think_file(base, entries)
        n += 1
    return n

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 清空旧生成条目(只清 __discovered__ 内,不碰用户手动登记的)
    for fn in os.listdir(OUT_DIR):
        try:
            os.remove(os.path.join(OUT_DIR, fn))
        except OSError:
            pass
    procs = scan_processes()
    np_ = write_process_cards(procs)
    ns_ = write_hermes_session_cards()
    nsub_ = write_prime_subagent_cards()
    # 历史任务已按用户要求移除,不再生成 cron 卡(保留函数,需要时启用)
    nc = 0
    # 汇总(供 agentboard.sh 显示/调试)
    counts = {k: len(v) for k, v in procs.items() if k != "hermes"}
    print(f"discovered: sessions={ns_} procs={np_} subagents={nsub_} ({', '.join(f'{k}:{v}' for k,v in counts.items()) or 'none'}) → {OUT_DIR}")

if __name__ == "__main__":
    main()
