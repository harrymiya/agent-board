# agent-board

一个轻量、零第三方依赖的 **agent 实时监控看板**。它周期性地扫描本机上所有正在运行的 AI agent
（prime / codex / pi / opencode / hermes 等），把每个 agent 的**进程状态**、**思维链(thinking)**、
**状态色(运行/出错/等待/思考/完成)** 落盘成小文件，再用一个纯 Python 标准库写的 HTTP 服务
把数据推送/轮询到浏览器，形成一张自动刷新、带丝滑动效的看板。

- **纯标准库**：服务端只依赖 Python 3.8+ 标准库（`http.server`/`threading`/`sqlite3`/`json`），
  前端是单个 `index.html`，无 npm / 无构建。
- **自动发现**：从 `/proc` 探测进程、读 codex/prime/opencode 的会话与 sqlite，无需手动登记。
- **实时推送**：WebSocket 优先，失败自动回退轮询。

```
agent-board/
├── server/agentboard_server.py    # HTTP/WebSocket 看板后端(唯一需运行的进程)
├── web/index.html                 # 前端单页(自动被后端读取并提供)
├── discover/
│   └── hermes_board_discover.py   # 自动发现脚本:扫描 /proc + 各 agent 会话 → 落盘数据
└── README.md
```

---

## 快速开始

```bash
cd /home/musk/code/agent-board

# 1) 启动看板后端(默认 127.0.0.1:8710)
python3 server/agentboard_server.py --port 8710

# 2) 浏览器打开
open http://127.0.0.1:8710
```

服务启动后：
- 后端会周期调用 `discover/hermes_board_discover.py`（默认每 0.2s 一次），把当前所有
  agent 状态物化到数据目录 `<root>/<run>/__discovered__/`；
- 浏览器通过 WebSocket 实时收到快照，页面自动刷新各卡片；
- 支持深浅主题（右上角按钮，记忆在 localStorage）。

---

## 命令行与配置

```bash
python3 server/agentboard_server.py \
  --port 8710 \          # 监听端口(默认 8710, 也可用环境变量 AGENTBOARD_PORT)
  --root ~/.hermes/agent-board \   # 看板数据根目录(默认见下)
  --run 20260825 \       # 指定日期 run 子目录(默认取最近有数据的日期目录)
  --interval 0.2 \       # 发现/刷新间隔秒数
  --web web/index.html   # 前端文件路径(默认取仓库内 web/index.html)
```

### 环境变量

| 变量 | 作用 | 默认 |
|------|------|------|
| `AGENTBOARD_PORT` | 端口 | `8710` |
| `AGENTBOARD_ROOT` | 数据根目录 | `~/.hermes/agent-board` |
| `AGENTBOARD_RUN`  | 固定 run 子目录（如要固定日期） | 最近日期目录 |
| `AGENTBOARD_INTERVAL` | 刷新间隔秒 | `0.2` |
| `HERMES_SCRIPTS` | discover 脚本备选搜索目录 | `~/.hermes/scripts` |

> 数据根目录 `--root` 会生成 `YYYYMMDD/__discovered__/` 存放每次扫描结果，可安全清理；
> 手动登记的卡放在 `--root/<run>/` 下（`.pid/.log/.cmd/.think` 等），不会被自动发现清除。

### 仅手动跑一次发现（不启动服务）

```bash
AGENTBOARD_ROOT=~/.hermes/agent-board python3 discover/hermes_board_discover.py
# → discovered: sessions=0 procs=8 subagents=6 (prime:5, codex:2, pi:1) → .../__discovered__
```

---

## 数据来源与自动发现

`discover/hermes_board_discover.py` 会扫描三类 agent：

| 类别 | 数据源 |
|------|--------|
| **进程卡** | 从 `/proc/*/cmdline` 探测 codex / pi / prime-agent / opencode 等运行进程 |
| **prime 会话/子 agent** | `~/.prime/agent/session-leases/*/owner.json`、`~/.prime/agent/session-artifacts/<parent>/sub-*/`（每个并行子 agent 生成独立卡，各自显示自己的思维链与完成态）|
| **codex 会话** | `~/.codex/sessions/2026/.../rollout-*.jsonl`、`~/.codex/logs_2.sqlite`（按进程 cwd 匹配自己的 rollout）|
| **opencode** | `~/.local/share/opencode/opencode.db` |
| **hermes 会话** | `~/.hermes/state.db` |
| **cron 卡** | `~/.hermes/cron/jobs.json`（默认关闭，函数保留可启用）|

> 外部这些家目录数据是**运行时输入**，不属于本仓库代码；仓库本身 3 个文件即可独立运行
>（无这些数据时看板仍能显示 /proc 探测到的进程）。

---

## 状态模型

| 徽章 | 含义 | 触发状态词 |
|------|------|-----------|
| **RUNNING** (绿) | 运行中 / 成功 | `running, working, started...` |
| **DONE** (灰) | 已完成(终态,非运行) | `done, completed, success, finished` |
| **ERROR** (红) | 出错 / 已退出 | `error, failed, blocked, exited...` |
| **THINKING** (紫) | 思考中 | `think, thinking` |
| **WAITING** (黄) | 等待输入 / 阻塞 | `waiting, needs_input, pending, queued...` |
| **IDLE** (灰) | 空闲 / 其它 | 以上均不匹配的兜底 |

进程**存活**时为 RUNNING（除非最后事件是等待/思考/错误/完成等更细子状态）；
进程**退出**且未标记完成 → ERROR。

---

## 卡片动效（前端）

`web/index.html` 使用 **FLIP 增量更新**，不整页重绘：
- **出现**：`cardIn` 缩放淡入；
- **消失**：`leaving` 缩放淡出后移除；
- **移动/换位**：FLIP transform 平滑滑到新位置（`cubic-bezier(.22,.8,.28,1)`）；
- 内容仅在签名变化时原位刷新，避免每帧闪烁。

---

## 架构说明

```
浏览器 --WebSocket/轮询--> agentboard_server.py --周期调用--> hermes_board_discover.py
   ^                            |                                   |
   | 渲染卡片                     | 读取 __discovered__/ 文件           | 扫描 /proc + 各 agent 会话
   卡片状态/思维链 <--- 内存快照 <--                                    v
                                                            <run>/__discovered__/*.pid|.log|.cmd|.think
```

1. **discover** 把"系统里所有 agent"物化成 `__discovered__/*` 小文件（幂等、每次清空重建）。
2. **server** 周期重扫这些文件 → 内存快照 → 每次 diff 后推给前端。
3. **前端** 用 FLIP 增量渲染，保持页面丝滑。

---

## 常见问题

- **端口被占用**：`lsof -i :8710` 找到旧进程后 `kill` 掉再启动。
- **想固定查看某一天的数据**：`--run 20260825`。
- **卡显示 RUNNING 但已结束**：取决于最后写入的状态词；进程已退出且未写 `done` 会转 ERROR，
  已完成的 prime 子 agent 会标为 DONE。
- **不想让某些进程上板**：可在 `discover/hermes_board_discover.py` 的 `scan_processes()`
  里按 `comm`/`cmdline` 过滤。

## License

MIT（见 LICENSE）
