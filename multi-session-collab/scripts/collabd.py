# -*- coding: utf-8 -*-
"""collabd —— **协作守护程序**（通用版 · 一个进程承担"派活之外的全部功能"）

配套：`multi-session-collab` 技能。配置见同目录 `collabd.config.json`（模板 `collabd.config.example.json`）。

**它承担**：① 通路/服务自愈（连续 N 次不通才动手·带冷却 ⇒ 防误杀）② 监控＋**证据分级**＋推进判定
③ 机械靶点判定 ④ **真空判定**（需求未完成 ∧ 无人执行） ⑤ 断链/中断告警 ⑥ **机制体检**（仅空闲）
⑦ **任务图**（可派/等待/关键路径/**可派未派＝浪费**） ⑧ 视图覆写（看板/摘要） ⑨ **单例**守护
⑩ **唤醒回路**（2026-09-29 立 · 用户拍板）：`NEXT.md` 出现且闸门开 ⇒ **把「有活」投给活会话**
   —— 走网关官方 `POST /api/v1/sessions/{id}/reply`（**投递不夺 ACP writer** · 桌面不受干扰）。

**⛔ 它不做**：**派活 / 开新会话** —— 那必须由会话做（且"新建自动化"受白名单＋确认制约束）。

边界：⛔ 读取宿主库只读 ·⛔ 不写宿主状态 ·⛔ 不碰别人的文件 · 全静默 · fail-open
      ⚠️ **唯一写操作 = ⑩ 的那一次 reply 投递**（口令**只进程内用**：⛔ 不落盘 ⛔ 不进日志 ⛔ 不回显）；
         四道闸门全满足才投（有 `NEXT.md` ∧ `gate=free` ∧ **内容哈希≠上次** ∧ 距上次 ≥`wake_min_gap` 秒），
         每次投递**追加登记** `inbox/wakeups.jsonl`（`{ts, port, sessionId, hash, http}` ⇒ 可列出/可删）。
         🔴 认口口径：同 cwd 可能有多个口，**取「第一个带活会话的」**（按 uptime 降序里第一个 `sessionId`
         非空者）—— ⛔ 不能只取 uptime 最大那个（实测它可能没有活会话 ⇒ 白等）。
用法（🔴 2026-09-30 定型 —— **投递＝宿主钩子唤起，⛔ 不靠排期、⛔ 不靠常驻**）：
    `--once`  协作程序：读库／判定／信号／看板（**⛔ 不投递**）
    `--tick`  监督程序：读队列 ⇒ **投给主会话**（单条＋握手／三条件心跳）—— 由**宿主钩子**唤起
    `--where` 路径自证
    （无参数 ＝ 常驻模式，**旧形态**，保留但不再是投递的前置）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "workspace": "",
    "inbox": "tmp/supervise-inbox",
    "live": "",                     # 视图（看板类产物）；空 ⇒ inbox/realtime.md
    "taskgraph": "",                # 任务图 json；空 ⇒ inbox/taskgraph.json
    "lines": {},                    # {"<目录名>": "<友好名>"}  用于靶点判定
    "goal_docs": [],                # [["显示名", "相对 workspace 的路径"], ...]
    "targets": {},                  # {"<目录名>": [["显示名","glob 相对该目录"], ...]}
    "host_db": "",                  # 宿主库路径；空 ⇒ $CODEBUDDY_CONFIG_DIR/workbuddy.db
    "shim_port": 0,                 # 服务端口探针（0 ⇒ 关闭）
    "shim_streak": 3,
    "client_entry": "",             # 端口不通时拉起的启动器（.mjs/.py/.exe）
    "client_runner": "",            # 用什么跑启动器（如 node 绝对路径）；空 ⇒ 直接执行
    "singleton_port": 20099,
    "interval": 10,
    "idle_min": 12,
    "stuck_min": 30,
    "vacuum_min": 5,
    "health_every": 300,
    "client_cooldown": 600,
    # ⑩ 唤醒回路（2026-09-29 立）。⚠️ wake_text 内**一律用「」**，⛔ 别写 ASCII 双引号（P11 引号坑）
    "wake_enable": True,
    "wake_min_gap": 180,          # 秒：同一件两次投递的最小间隔
    "wake_max_ports": 60,         # 最多探多少个 loopback 监听口（认网关口用）
    "wake_text": "读 tmp/supervise-inbox/NEXT.md 处理那一条；收尾时若 NEXT.md 还在才接续（一次只做这一条）",
}


# 🔴 部署配置**属于使用方**（用户 2026-09-30 定则：「技能就是技能 程序就是程序，
#    谁用产生的文件 放在他自己那里」）⇒ **技能目录里⛔ 不放生产配置**。
#    查找顺序（前面的赢）：
#      ① 环境变量 `COLLABD_CONFIG`                —— 钩子／启动器显式指定（首选）
#      ② `<工作区>/.workbuddy/collab/collabd.config.json` —— 使用方的**标准落点**
#      ③ 都没有 ⇒ 用 DEFAULTS ＋ `COLLABD_WORKSPACE`／cwd，并**明确记一条"未找到配置"**（⛔ 不静默）
CFG_USED = ""
CFG_MISSING = True


def _cfg_candidates() -> list:
    out = []
    env = os.environ.get("COLLABD_CONFIG")
    if env:
        out.append(Path(env))
    ws = os.environ.get("COLLABD_WORKSPACE") or os.getcwd()
    out.append(Path(ws) / ".workbuddy" / "collab" / "collabd.config.json")
    return out


def load_cfg() -> dict:
    global CFG_USED, CFG_MISSING
    cfg = dict(DEFAULTS)
    for p in _cfg_candidates():
        if p.is_file():
            try:
                cfg.update(json.loads(p.read_text(encoding="utf-8")))
                CFG_USED, CFG_MISSING = str(p), False
                break
            except Exception:
                pass
    if not cfg["workspace"]:
        cfg["workspace"] = os.environ.get("COLLABD_WORKSPACE") or os.getcwd()
    return cfg


C = load_cfg()
WS = Path(C["workspace"])
INBOX = WS / C["inbox"]
def _rp(rel: str, default: Path) -> Path:
    """🔴 把配置里的**相对路径解析到 workspace 下**（⛔ 别按脚本 CWD 解析 —— 踩过：视图被写进技能目录）"""
    p = Path(rel) if rel else None
    if p is None:
        return default
    return p if p.is_absolute() else (WS / p)


LIVE = _rp(C["live"], INBOX / "realtime.md")
TG = _rp(C["taskgraph"], INBOX / "taskgraph.json")
STALL, VACUUM, READY = INBOX / "STALL.md", INBOX / "VACUUM.md", INBOX / "READY.md"
HEALTH = INBOX / "体检报告.md"
STATE = INBOX / "collabd-state.json"
# ⛔ 日志**不进技能目录**（技能是只读的能力件）⇒ 默认落在使用方的工作区内
LOG = _rp(C.get("log") or "", INBOX / "_collabd.log")


def log(m: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)     # ⚠️ 日志落点在**使用方**，目录可能还没建
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), m))
    except Exception:
        pass


if CFG_MISSING:
    # ⛔ **找不到配置时不许落任何文件** —— 那一刻我们根本不知道"使用方的地方"在哪；
    #    旧写法 `log(...)` 会按默认落点写进 **cwd**，而 cwd 常常就是技能目录
    #    ⇒ 恰好又造出它要避免的污染（2026-09-30 实测：一跑就往技能里长出 `tmp/supervise-inbox/_collabd.log`）。
    #    ⇒ 只打 **stderr**（调用方若 devnull 掉了就当静默；人手动跑时看得见）。
    sys.stderr.write("⚠️ collabd：未找到部署配置（COLLABD_CONFIG 未设，且 %s 不存在）\n"
                     "   ⇒ 已拒跑，⛔ 不会往任何目录写文件。\n"
                     % (Path(os.environ.get("COLLABD_WORKSPACE") or os.getcwd())
                        / ".workbuddy" / "collab" / "collabd.config.json"))


def _mt(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except Exception:
        return 0.0


def _db():
    cfg_dir = os.environ.get("CODEBUDDY_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".workbuddy")
    db = Path(C["host_db"]) if C["host_db"] else Path(cfg_dir, "workbuddy.db")
    return sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True, timeout=4)


# ── 只读取数（宿主库） ───────────────────────────────────────
def fetch() -> dict:
    d = {"runs": [], "wm": 0, "pending": [], "upcoming": [], "soon": 0, "working": [],
         "self_working": False, "sessions": [], "locks": [], "due": [], "have": set()}
    now_ms = int(time.time() * 1000)
    try:
        con = _db()
        rows = list(con.execute("select rowid,status,thread_title,automation_id from automation_runs "
                                "order by rowid desc limit 8"))
        d["runs"] = [{"rowid": r[0], "status": r[1], "title": (r[2] or "").strip(), "aid": (r[3] or "")[:8]}
                     for r in rows]
        d["wm"] = max([r["rowid"] for r in d["runs"]], default=0)
        for r in con.execute("select id,name,cwds,schedule_type,next_run_at,scheduled_at from automations "
                             "where status='ACTIVE' and (deleted_at is null or deleted_at='')"):
            # 🔴 2026-09-29 修（P1·实测）：原实现把「ACTIVE ∧ next_run_at>now」全算 `pending`，
            #    而两条**周期**自动化（日报 / 体检）恒满足 ⇒ `pending` 恒非空 ⇒「没有会话在跑」
            #    那条分支**永远不可达** ⇒ `STALL.md` 从未生成（实测两日零生成）。⇒ 拆开：
            #      · `pending`  = **一次性**排活（刚建好、还没跑的下一棒 ⇒ 用于判「只在排活、未见成果」）
            #      · `upcoming` = **周期**自动化（⛔ 只展示，**不代表有人在跑**）
            if (r[4] or 0) > now_ms:
                if (r[3] or "").lower() == "recurring":
                    d["upcoming"].append(r)
                else:
                    d["pending"].append(r)
                if (r[4] or 0) <= now_ms + 60 * 60 * 1000:
                    # 🔴 「**未来 1 小时内**会不会有人自己动起来」—— 这才是判"确定性静默"的口径。
                    #    ⛔ 不是"全库有没有排期"：日报 / 体检那两条明天才跑，与**本线**毫无关系
                    #    （实测 16:34→20:07：全库有 2 条排期，本线 0 条，于是静默 4 小时）。
                    d["soon"] += 1
            if r[5]:
                d["due"].append((str(r[0]), r[1] or "", r[5]))
        # 🔴 2026-09-29 修（P1·实测）：**唯一可得的「有人在跑」信号 = `sessions.status='working'`**。
        #    ⛔ 不能用 `automation_runs.status`（实测 54 行**全 ACCEPTED**、零 IN_PROGRESS）
        #    ⛔ 不能用一次性棒的 `next_run_at`（实测**全是 0/None**，跑完即归零）
        for r in con.execute("select id, coalesce(nullif(custom_title,''),title,'(未命名)'), "
                             "       coalesce(last_activity_at, updated_at), coalesce(cwd,'') "
                             "from sessions where status='working'"):
            # 🔴 2026-09-29：**排除观察者自己**。否则「主会话自己在跑」也会被算成「有人在推进」
            #    ⇒ 停滞 / 真空永远发现不了（用户问「是不是又发呆了」答不出来的根因之一）。
            wid = str(r[0] or "")
            if SELF_SID and wid.lower().startswith(SELF_SID[:8].lower()):
                d["self_working"] = True
                continue
            d["working"].append({"id": wid, "name": str(r[1] or "")[:34], "at": r[2],
                                 "cwd": str(r[3] or "")})
        d["have"] = {str(r[0]) for r in con.execute("select distinct automation_id from automation_runs") if r[0]}
        # 🔴 2026-09-29（回架构·去自造状态源）：**「多久没成果」的权威在宿主库里** ——
        #    `automation_runs.updated_at`（实测有该列，毫秒 epoch，形如 '1790667377928'）。
        #    ⛔ 不要让程序自己维护 `last_progress_at` 这类时钟（那就是"第二状态源"：会漂、要清理、
        #    丢了就永久失真 —— 今天整天的毛病都长在这类自造状态上）。
        try:
            row = con.execute("select updated_at from automation_runs "
                              "where status='ACCEPTED' and coalesce(thread_title,'')<>'' "
                              "order by updated_at desc limit 1").fetchone()
            if row and row[0]:
                v = float(row[0])
                d["last_result_at"] = v / (1000.0 if v > 1e11 else 1.0)
        except Exception as e2:
            log("last_result_at 取失败 %s" % e2)
        # ⚠️ sessions 表没有 name 列（正确列：title/custom_title/last_activity_at/unread）——踩过一次
        for r in con.execute(
                "select id, coalesce(nullif(custom_title,''), title, '(未命名)'), "
                "       coalesce(last_activity_at, updated_at), status, unread from sessions "
                "order by coalesce(last_activity_at, updated_at) desc limit 10"):
            ts, t = r[2], 0.0
            if isinstance(ts, (int, float)):
                t = float(ts) / (1000.0 if float(ts) > 1e11 else 1.0)
            elif isinstance(ts, str) and ts:
                try:
                    t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    t = 0.0
            d["sessions"].append({"id": str(r[0] or "")[:8], "name": str(r[1] or "")[:36],
                                  "age": (time.time() - t) / 60 if t else 1e9,
                                  "tag": ("未读 %s" % r[4]) if r[4] else (r[3] or "")})
        con.close()
    except Exception as e:
        log("fetch err %s" % e)
    return d


def probe_port() -> bool:
    if not C["shim_port"]:
        return True
    try:
        s = socket.create_connection(("127.0.0.1", int(C["shim_port"])), timeout=1.5)
        s.close()
        return True
    except Exception:
        return False


def targets() -> tuple[bool, list[str]]:
    rows, ok_all = [], True
    for line, items in (C["targets"] or {}).items():
        for label, pat in items:
            base = Path(WS).parent / line if not Path(line).is_absolute() else Path(line)
            hits = sorted(base.glob(pat), key=_mt) if pat else []
            ok = bool(hits)
            ok_all = ok_all and ok
            rows.append("- %s **%s** %s" % ("✅" if ok else "⛔", line, label))
    return ok_all, rows


# ── 任务图（防干等） ────────────────────────────────────────
def taskgraph(cur: list, busy: bool) -> dict:
    try:
        g = json.loads(TG.read_text(encoding="utf-8"))
    except Exception:
        return {}
    ns = {n.get("id"): n for n in g.get("nodes", [])}
    out = {"ready": [], "waiting": [], "critical": list(g.get("critical_path") or []),
           "waste": False, "nodes_raw": g.get("nodes", [])}
    for n in g.get("nodes", []):
        if n.get("status") == "done":
            continue
        undone = [x for x in (n.get("deps") or []) if ns.get(x, {}).get("status") != "done"]
        (out["waiting"] if undone else out["ready"]).append(
            (n.get("id"), (n.get("title") or "")[:40], n.get("line"), undone) if undone
            else (n.get("id"), (n.get("title") or "")[:40], n.get("line"), n.get("status")))
    # 🔴 2026-09-29 加：**受阻件不算「可派」** —— 否则 `READY.md` 与机器摘要会把受损件当
    #    「可派未派（浪费）」反复喊，与 `queue.json` 的 blocked **自相矛盾**（实测：N9 被标受阻后，
    #    READY.md 仍在喊「N9 可派」）。
    try:
        blk = json.loads(BLOCKED.read_text(encoding="utf-8")) if BLOCKED.exists() else {}
        if not isinstance(blk, dict):
            blk = {}
    except Exception:
        blk = {}
    if blk:
        out["blocked"] = [(r[0], blk.get(r[0], "")) for r in out["ready"] if r[0] in blk]
        out["ready"] = [r for r in out["ready"] if r[0] not in blk]
    out["waste"] = bool(out["ready"]) and not cur and not busy
    return out


# ── 判定 ────────────────────────────────────────────────────
def verdicts(d: dict, up: bool, st: dict) -> dict:
    now = time.time()
    fresh = [r for r in d["runs"] if r["rowid"] > int(st.get("rowid") or 0)]
    done = [r for r in fresh if r["status"] == "ACCEPTED" and r["title"]]
    started = [r for r in fresh if r not in done]
    # 🔴 2026-09-29 修：原写成 `... if st.get("aids") else []` ⇒ `aids` 一旦为空（实测刚发生过）
    #    就**永远判不出「新增排活」** ⇒「只在排活、未见成果」这条警戒失效。改成对空集比较。
    newp = [r for r in d["pending"] if str(r[0]) not in set(st.get("aids") or [])]
    # 🔴 2026-09-29 修（P3·实测）：探针**翻转不是推进** —— 原实现任何翻转（含 通→不通）都重置
    #    `last_progress_at` ⇒ 探针每次抖动都把「无成果时长」清零 ⇒ 卡住 / 脱节告警**永不触发**。
    # 🔴 2026-09-29 再修（P3b · 20:07 实测）：**「恢复」也不得重置** —— 16:31→20:07 整段停滞里
    #    程序只跑过一轮（20:07），恰好赶上探针 不通→通 ⇒ idle 被清成 **1 分钟**，摘要写「1 分钟无
    #    成果」而不是「4.5 小时」⇒ **连喊一声都没有**。⇒ `idle` **只认真成果**；探针状态变化降级为
    #    后置说明（服务层，⛔ 不算成果、⛔ 不清零）。
    rec = (st.get("up") is False and up is True)
    lost = (st.get("up") is True and up is False)
    # 🔴 权威顺序：本轮有新成果 ⇒ now；否则 **宿主库最后一次有结论的 run**（`last_result_at`）；
    #    ⛔ 只有读库失败时才退回 `state.last_progress_at`（那是旧的自维护时钟，已降级为兜底，
    #    且一取到库值就被忽略）。
    src_pa = float(d.get("last_result_at") or 0)
    if done:
        pa = now
    elif src_pa:
        pa = src_pa
    else:
        pa = float(st.get("last_progress_at") or now)
    idle = (now - pa) / 60

    def _dur(m):
        return ("%.0f 分钟" % m) if m < 90 else ("%.1f 小时" % (m / 60.0))

    if done:
        v, ic = "🟢 **真成果** —— %d 条有结论的完成" % len(done), "ok"
    elif newp:
        v, ic = "🟡 **只在排活，未见新成果**（新增 %d 条接续任务·排期≠成果）" % len(newp), "wait"
    elif d["working"]:
        v, ic = "🟡 有会话在跑，暂无新成果（在跑 %d 个；%s 无成果）" % (
            len(d["working"]), _dur(idle)), "wait"
    else:
        v, ic = "🔴 **中断／脱节** —— 没有任何会话在跑，且 %s 无成果" % _dur(idle), "stall"
    if rec or lost:
        v += "；探针 %s（服务层变化，⛔ 不算成果、不清零）" % ("不通→通" if rec else "通→不通")
    vs = float(st.get("vacuum_since") or 0)
    vac = (not up) and not busy_(d)
    vs = (vs or now) if vac else 0.0
    # 🔴 2026-09-29（回架构）：**「零排期」是一等事实，不是背景噪音**。
    #    地基（顶层设计 §1/§2）：宿主不提供常驻 ⇒ 一切"等待"只能靠**宿主排期** ⇒
    #    **排期数 = 0 ⇒ 没人发消息时"确定性静默"**（实测 16:31→20:07 四小时零运行，根因即此）。
    #    ⇒ 单独成一档告警：不等 idle 攒够，直接喊。
    zero_sched = int(d.get("soon") or 0) == 0
    if zero_sched and not d["working"] and not done:
        v += "；⛔ **未来 1 小时内零排期** ⇒ 不等人发消息就是**确定性静默**"
        ic = "stall"
    alert = None
    if zero_sched and not d["working"] and not done:
        alert = "确定性静默（未来 1 小时内**零排期** ＋ 无人接活）"
    elif ic == "stall" and idle > float(C["idle_min"]):
        alert = "脱节（无活无人）" if not d["working"] else "中断（有活没人接）"
    elif ic == "wait" and idle > float(C["stuck_min"]):
        alert = "卡住（有会话在跑但 %.0f 分钟无成果%s）" % (idle, "，且探针不通" if (lost or not up) else "")
    return {"fresh": fresh, "done": done, "started": started, "newp": newp, "up": up,
            "busy": busy_(d), "verdict": v, "icon": ic, "idle": idle, "alert": alert,
            "vacuum": vac, "vdur": (time.time() - vs) / 60 if vs else 0.0, "vs": vs,
            "pa": pa, "wm": d["wm"], "cur": [str(r[0]) for r in d["pending"]]}


def busy_(d: dict) -> bool:
    """🔴 2026-09-29 修（实测）：原实现两个信号**恒为假** —— `d["locks"]` 从未被填充；
    `automation_runs.status` 实测 54 行**全 ACCEPTED**（零 IN_PROGRESS）⇒ `busy` 恒 False
    ⇒ ① 体检 `health()` 从不跳过 ② `真空` / `可派未派` 判定失真。
    改用实测唯一可得的「有人在跑」信号：`sessions.status='working'`（含本会话 ⇒ 语义正确：
    「有会话在执行」就不算真空）。"""
    return bool(d["locks"]) or bool(d.get("working"))


def health(d: dict, V: dict) -> dict:
    out = {"skipped": V["busy"], "issues": []}
    if V["busy"]:
        return out
    cut = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() - 15 * 60))
    for aid, nm, sat in d["due"]:
        s = (sat or "")[:16]
        if s and s < cut and aid not in d["have"]:
            out["issues"].append("**哑火**：`%s`（%s）到点未触发" % (aid[:8], nm[:30]))
    f = sum(1 for r in d["runs"] if "抢锁失败" in (r["title"] or ""))
    if f >= 3:
        out["issues"].append("近 %d 条里 %d 次抢锁失败（摩擦，非故障）" % (len(d["runs"]), f))
    if len([r for r in d["runs"] if r["status"] == "IN_PROGRESS"]) > 1:
        out["issues"].append("同时多个 IN_PROGRESS（疑似并发）")
    for nm, rel in (C["goal_docs"] or []):
        if not (WS / rel).exists():
            out["issues"].append("**关键件缺失**：%s" % nm)
    return out


# ── 自愈（带去抖） ───────────────────────────────────────────
QUEUE, QUEUE_MD = INBOX / "queue.json", INBOX / "queue.md"
CLAIMS, STALE = INBOX / "claims", INBOX / "claims-stale"
DOING_TTL = 20 * 60          # 秒：doing 超过多久没人续 ⇒ 判卡死并回退（⛔ 判活优先，见 _reap_dead）
BLOCKED = INBOX / "blocked.json"          # {"<节点id>": "<受阻原因·谁在等>"} —— 受阻件 ⛔ 不当队首
_SID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SID_DEAD = {"completed", "error", "archived"}   # 实测取自 sessions.status 取值域
SELF_SID = os.environ.get("CODEBUDDY_SESSION_ID", "") or ""   # 观察者自己 —— 判「有没有人在推进」时 ⛔ 必须排除



def _reap_dead(doing: dict) -> dict:
    """认领持有人**是否还在**（🔴 P5 治本：claims 与「人」绑定）。

    契约：`claims/<id>/holder` = `<会话名>@<线>@<完整会话id>`（第 3 段可省）。
    · holder 里有会话 id ⇒ 查宿主库；状态是**终结态**（completed/error/archived）⇒ 立即把该 claim
      移到 `claims-stale/`（⛔ 不删，可追溯）。实测病根：棒已终结、claim 还在 ⇒ 队首被僵尸占住；
      等 20 分钟 TTL 回退后队首又**复活** ⇒ 同一件被反复派（N17、N9 各复现一次）。
    · ⛔ 无会话 id / 读库失败 / 状态未知 ⇒ **一律不猜**，交给 TTL 兜底（行为＝修前）。
    """
    if not doing:
        return doing
    sids = {}
    for k, v in doing.items():
        m = _SID_RE.search(v or "")
        if m:
            sids[k] = m.group(0).lower()
    if not sids:
        return doing
    try:
        con = _db()
        rows = list(con.execute("select id, status from sessions"))
        con.close()
    except Exception as e:
        log("reap 跳过（宿主库读不到 ⇒ 回退 TTL）：%s" % e)
        return doing
    smap = {str(r[0] or "").lower(): str(r[1] or "").lower() for r in rows}
    for k, s in list(sids.items()):
        if smap.get(s) not in SID_DEAD:
            continue                      # 在跑 / 状态未知 ⇒ 不动
        try:
            STALE.mkdir(parents=True, exist_ok=True)
            p = CLAIMS / k
            if p.is_dir():
                p.rename(STALE / ("%s-持有会话已终结-%s" % (k, time.strftime("%H%M%S"))))
            doing.pop(k, None)
            log("claim 出队 %s（持有会话 %s = %s）" % (k, s[:8], smap.get(s)))
        except Exception as e:
            log("reap 移出失败 %s：%s" % (k, e))
    return doing


def queue_view(T: dict, d: dict | None = None) -> dict:
    """🔴 **严格队列**（2026-09-29 用户要求："严格用队列的方式处理，避免打架，处理好一个再处理下一个"）。

    语义（与"域锁"分工不同：**队列管"谁做下一件"；域锁管"能不能动这个资源"**）：
      ① **调度串行** —— 一次只呈现**一个队首**；取件必须**原子**（`mkdir claims/<id>` 成功者得）
      ② **同线互斥、跨线并行** —— 队列里标 `line`：同线已有 doing ⇒ 该线其他件**不呈现为队首**
      ③ **卡死回退** —— claim 目录超 `DOING_TTL` 未被续 ⇒ 移到 `claims-stale/`（⛔ 不删，可追溯）并记日志
      ④ ⛔ 本程序**不派活**：只"排队 + 呈现队首 + 兜卡死"；**取件与派活由会话做**。
    """
    try:
        CLAIMS.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    now = time.time()
    # ① 卡死回退（只看 claim 目录的 mtime）
    for c in list(CLAIMS.iterdir()) if CLAIMS.exists() else []:
        try:
            if c.is_dir() and now - c.stat().st_mtime > DOING_TTL:
                STALE.mkdir(parents=True, exist_ok=True)
                c.rename(STALE / c.name)
                log("claim stale -> %s（超 %d 分钟未续，已回退）" % (c.name, DOING_TTL // 60))
        except Exception:
            pass
    doing = {}
    for c in (CLAIMS.iterdir() if CLAIMS.exists() else []):
        if c.is_dir():
            h = c / "holder"
            doing[c.name] = (h.read_text(encoding="utf-8", errors="replace").strip()
                             if h.exists() else "?")
    # 🔴 2026-09-29 修（P5）：**持有人失活 ⇒ 立即出队**（⛔ 不再干等 20 分钟 TTL）——
    #    病根：claim 只记「有人取了」，不记「那个人还在不在」⇒ 队首被僵尸占住 / TTL 回退后复活。
    doing = _reap_dead(doing)
    # 🔴 2026-09-29 修（P5·`holder` 契约）：契约是 `<会话名>@<线>[@<会话id>]`（见 queue.md 模板）。
    #    原实现取 `split("@")[0]` = **会话名**，却拿去比 **线名** ⇒ 恒不相等 ⇒
    #    **「同线互斥」从未生效** ⇒ 同一条线可被同时派多件（打架）。取第 2 段才是线；
    #    缺第 2 段 ⇒ 回退按节点 id 反查它是哪条线。
    line_of = {n.get("id"): n.get("line") for n in (T.get("nodes_raw") or [])}
    # 🔴 2026-09-29（回架构 · 去自造状态源）：「哪条线被占」**由宿主库推导** ——
    #    `sessions.status='working'` 的 `cwd` 末段就是线名（与任务图 `line` 同构）。
    #    ⛔ 不再依赖解析自己写的 `holder` 文本（那正是"第二状态源"：会漂、要清理、今天整天的
    #    缺陷都长在这类自造状态上）。`claims` 降级为**仅给程序看的投影提示**，丢了不影响判定。
    busy_lines = set()
    for w in (d or {}).get("working") or []:
        seg = str(w.get("cwd") or "").replace("\\", "/").rstrip("/").split("/")[-1]
        if seg:
            busy_lines.add(seg)
    for cid, v in doing.items():                 # 兜底：宿主库取不到时才看自造件
        parts = [x.strip() for x in v.split("@")]
        busy_lines.add((parts[1] if len(parts) > 1 and parts[1] else "") or line_of.get(cid) or "")
    busy_lines.discard("")
    crit = list(T.get("critical") or [])
    # 🔴 优先级：**"在关键路径的上游闭包内"⇒ 0**（做它能解锁关键路径）；否则 1
    #    ⛔ 不能只看"是不是关键路径节点本身"（那样会漏掉它的前置，反而去干无关的活）
    crit_up = set(crit)
    changed = True
    while changed:                     # 迭代求上游闭包
        changed = False
        for n in (T.get("nodes_raw") or []):
            if n.get("id") in crit_up:
                continue
            if any(d in crit_up for d in (n.get("deps") or [])):
                crit_up.add(n.get("id"))
                changed = True
    pend, head = [], None
    for nid, title, line, status in (T.get("ready") or []):
        if nid in doing:
            continue
        num = int("".join(ch for ch in nid if ch.isdigit()) or 9999)   # N6 < N7 < N11
        pend.append({"id": nid, "title": title, "line": line, "status": status,
                     "prio": 0 if nid in crit_up else 1, "num": num})
    pend.sort(key=lambda x: (x["prio"], x["num"]))
    # 🔴 2026-09-29 加（P4·队列生命周期）：**受阻件不得当队首**。否则队首永远是它、`NEXT.md`
    #    永远是它 ⇒ 每次唤醒都白跑一遍同一个受阻件（实测：N9 被泄漏锁挡住后反复复活）。
    #    受阻清单由**会话**维护：`tmp/supervise-inbox/blocked.json` = {"<节点id>": "<原因·谁在等>"}
    #    （⛔ 不改任务图：这是"排队状态"，不是"任务状态"）
    try:
        blocked = json.loads(BLOCKED.read_text(encoding="utf-8")) if BLOCKED.exists() else {}
        if not isinstance(blocked, dict):
            blocked = {}
    except Exception:
        blocked = {}
    if blocked:
        pend = [p for p in pend if p["id"] not in blocked]
    for p in pend:                      # ② 同线互斥 ⇒ 只挑一个未被占线的作队首
        if p["line"] not in busy_lines:
            head = p
            break
    # 🔴 闸门（gate）：用**钩子事件**驱动"继续读下一条"，⛔ 不靠轮询
    #    规则：有 claim（有人在做）且 `gate-done.stamp` 不新于该 claim ⇒ gate=busy（处理期间不放行）；
    #          收尾钩子会更新 stamp ⇒ gate=free ⇒ 放行下一条（写 NEXT.md，一次只写一条）。
    GATE_DONE = INBOX / "gate-done.stamp"
    NEXT_MD = INBOX / "NEXT.md"
    gate = "free"
    try:
        claim_mt = max([c.stat().st_mtime for c in CLAIMS.iterdir() if c.is_dir()] or [0])
        done_mt = GATE_DONE.stat().st_mtime if GATE_DONE.exists() else 0
        if doing and done_mt <= claim_mt:
            gate = "busy"
    except Exception:
        pass

    out = {"head": head, "doing": doing, "pending": len(pend),
           "blocked_lines": sorted(busy_lines), "blocked": blocked, "gate": gate,
           "conflict": bool(len(doing) > 1)}
    try:
        if gate == "free" and head:
            NEXT_MD.write_text(
                "# 📌 下一条（**只此一条** · 处理期间闸门关闭）\n\n- 条目：**%s** %s\n- 线：`%s`\n- 状态：%s\n\n"
                "## 主会话该做的\n"
                "1. 抢锁（按此件涉及的文件声明域）\n"
                "2. `mkdir \"…/tmp/supervise-inbox/claims/%s\"` **原子取件**；建不成 ⇒ 别人取了 ⇒ ⛔ 停\n"
                "3. 立刻写 `claims/%s/holder` = `<会话名>@<线>@<本会话 id>`（**第 3 段别省**）—— 这是\n"
                "   「谁在做」的唯一权威；持有人失活程序会**立即出队**，⛔ 不写就只能等 20 分钟超时兜底\n"
                "4. 派活（白名单「派活」⇒ 免确认）**或**自己做完\n"
                "5. 更新任务图（**有可核对产物才标 done**）⇒ **删 `claims/%s` 出队**\n"
                "6. 收尾（你的 SessionEnd 钩子会自动通知程序放行下一条）\n"
                "7. 🔴 **收尾＝自己判本线缺口并接上**（顶层设计 §4「收尾自判」· 定稿 §4.2 必含条款）：\n"
                "   ⚠️ **派活命名规范**：新棒的自动化名必须用**两级前缀** `[协作]-[主题]-<具体>`\n"
                "       （主题取自 `goal.json` 的 `short`）—— 否则程序判「未按约定命名」并告警。\n"
                "   做完就判「我这条线还有没有缺口」⇒ 有 ⇒ **在收尾那一刻**写下一行一次性 `automations`\n"
                "   （排期 = 现在 + 5~8 分钟）—— ⛔ 不叫「监管会话」（监管棒这个**角色**已退役）。\n"
                "   ⚠️ 棒中途死掉时这步不会发生 ⇒ 链条会静默断掉 ⇒ 靠**每小时兜底心跳**接（定稿 §3②）。\n"
                "\n> ⛔ 一次只做这一条：**闸门 gate=%s**；遇阻 ⇒ 写 `NEED-USER.md` 并**明确喊「需用户介入」**，\n"
                "> 同时把该件写进 `blocked.json`（{id: 原因}）—— 否则它会一直当队首、每次唤醒都白跑。\n"
                % (head["id"], head["title"], head["line"], head["status"], head["id"], head["id"], head["id"], gate),
                encoding="utf-8")
        elif gate == "free" and blocked:
            # 🔴 2026-09-29 补（治「受阻即静默」）：**受阻不是没活** —— 只是没人能开工。
            #    原实现选不出队首就删 NEXT.md ⇒ 唤醒回路的触发条件不再满足 ⇒ 变成
            #    「有活 ∧ 谁都动不了 ∧ 程序一声不响」（实测 16:31→20:07 零投递）。
            #    ⛔ 内容**不得含时间戳**：唤醒回路按**内容哈希**去重，带时间戳会退化成每轮都投一遍。
            NEXT_MD.write_text(
                "# 🛑 队列受阻（**有活，但没人能开工** · 需人处理）\n\n"
                + "\n".join("- **%s** —— %s" % (k, v) for k, v in blocked.items())
                + "\n\n## 主会话该做的\n"
                "1. 读 `tmp/supervise-inbox/NEED-USER.md`（受阻原因与解除条件都在里面）\n"
                "2. **明确报告用户**：要哪一句授权、要哪个决定\n"
                "3. 用户放行后办掉，并**摘掉 `blocked.json` 里对应项** ⇒ 队首自动恢复\n"
                "4. ⛔ 不要因为受阻就硬派别的棒（那会变成「空转链条」）\n"
                "\n> ⛔ 一次只处理这一件；**受阻 ≠ 停滞** —— ⛔ 不抢锁、不接管。\n",
                encoding="utf-8")
        else:
            try:
                NEXT_MD.unlink()
            except Exception:
                pass
    except Exception:
        pass
    try:
        QUEUE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        QUEUE_MD.write_text("\n".join([
            "# 严格队列（一次一件 · 原子取件 · 同线互斥）", "",
            "- 生成于：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "- **队首（下一个该做）**：%s" % ("**%s** %s（线：%s，%s）" % (head["id"], head["title"], head["line"],
                                                                  "在关键路径上游·做它可解锁关键路径" if head["prio"] == 0 else "非关键路径")
                                          if head else "（无可派：都在做或等前置）"),
            "- **正在做**：%s" % ("、".join("%s→%s" % (k, v) for k, v in doing.items()) or "（无）"),
            "- **受阻（⛔ 不当队首）**：%s" % ("、".join("**%s**（%s）" % (k, v) for k, v in blocked.items()) or "（无）"),
            "- **待办**：%d 件%s" % (len(pend), "　⚠️ 同时在做多件（可能打架）" if out["conflict"] else ""), "",
            "## 取件规矩（**严格队列**）",
            "1. **只取队首那一件**；取件用**原子动作**：`mkdir tmp/supervise-inbox/claims/<id>` ⇒ **建不成就是别人取走了** ⇒ ⛔ 换队首／等待",
            "2. 取到后写 `claims/<id>/holder`（内容 `<会话名>@<线>@<完整会话id>`）⇒ 「谁在做」的唯一权威；**第 3 段别省**（持有人失活 ⇒ 程序立即出队；⛔ 不写只能等超时兜底）",
            "3. **做完 → 删掉 `claims/<id>`**（出队）⇒ 下一个才可能成为队首（⛔ 不删 ⇒ 该线一直被占）",
            "4. **同线互斥、跨线并行**：同一条线同时只允许一件；不同线可并行（队列按 `line` 判）",
            "5. **卡死**：claim 超 %d 分钟未续 ⇒ 程序自动移到 `claims-stale/`（可追溯）⇒ 队列自动放行"
            % (DOING_TTL // 60), "",
        ]), encoding="utf-8")
    except Exception:
        pass
    return out


# ── ⑩ 唤醒回路（程序把「有活」投给**活会话**） ───────────────────
# 🔴 2026-09-29 立（用户拍板）。事实依据（源码实读 ＋ 本机实测，⛔ 别再试错）：
#   · `POST /api/v1/sessions/{id}/reply` ＝ **官方投递语义：不夺 ACP writer**（桌面不被切换/打断）；
#     ⚠️ 只对**该网关的当前活会话**成立，其余会话会 409 ⇒ 故先 `GET /api/v1/sessions/live` 取 id。
#   · 鉴权头**只有** `x-access-token: <口令>` 通（`?password=` 在受保护路径上恒失效）；
#     口令从 `os.environ[CODEBUDDY_GATEWAY_PASSWORD]` 取（桥跑在 WorkBuddy 进程树内即自动可得）。
#   · 本机**并存多个实例**（各带一个网关）⇒ 认口必须加 **cwd == 本工作区** 这一条，
#     否则会把「有活」投进**别的工作区**的会话里。同 cwd 多口时取 **uptime 最大**（＝最稳的常驻实例）。
NEXT_MD = INBOX / "NEXT.md"
WAKEUPS = INBOX / "wakeups.jsonl"
NEED_USER = INBOX / "NEED-USER.md"
GW_TITLE_KEYS = ("CodeBuddy Gateway", "CodeBuddy Remote Control")
GW_HDR = "x-access-token"
GW_ENV = "CODEBUDDY_GATEWAY_PASSWORD"

# 🔴 2026-09-30 加：本进程**是不是由宿主钩子唤起**（`--tick`）——
#    用来区分两种"没口令"，两者的处置完全相反：
#      · 钩子唤起（进程在**宿主进程树**内）却仍没口令 ⇒ **真异常** ⇒ 必须落 `NEED-USER.md` 让人看见；
#      · 常驻进程（从「启动」文件夹/命令行起，**天然在宿主进程树外**）没口令 ⇒ **设计使然**（⛔ 不是故障）
#        ⇒ 只记日志，⛔ 不要每小时写一条 `NEED-USER.md` 把真问题淹掉。
FROM_HOOK = False


def _norm_p(p) -> str:
    return str(p or "").replace("\\", "/").rstrip("/").lower()


def _gw_http(port: int, method: str, path: str, obj, token: str, timeout: float = 6.0):
    """直调本机网关。⛔ 口令只作请求头使用：不写日志、不落盘、不回显、不进异常消息。"""
    h = {"Accept": "application/json"}
    data = None
    if obj is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if token:
        h[GW_HDR] = token
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (int(port), path), data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, str(e)


def _listen_ports(limit: int) -> list:
    """扫 `netstat -ano` 取 `127.0.0.1:<port> LISTENING` 候选（1024<p<65535），最多 limit 个。"""
    try:
        # 🔴 2026-09-29 修（用户："一会弹出来一会弹出来的，影响我操作电脑"）：
        #    `netstat` 是**控制台程序** —— 不加 CREATE_NO_WINDOW 就**每次都闪一个黑窗**
        #    （本函数每轮都被调 ⇒ 10~30 秒闪一次）。⇒ 所有子进程一律**不显窗**。
        r = subprocess.run(["netstat", "-ano"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           creationflags=0x08000000,   # CREATE_NO_WINDOW
                           timeout=40, errors="replace")
        out = r.stdout or ""
    except Exception as e:
        log("wake: netstat err %s" % e)
        return []
    ports = []
    for ln in out.splitlines():
        if "LISTENING" not in ln:
            continue
        m = re.search(r"127\.0\.0\.1:(\d+)\b", ln)
        if not m:
            continue
        p = int(m.group(1))
        if 1024 < p < 65535 and p not in ports:
            ports.append(p)
    return ports[:limit]


def discover_gateways() -> list:
    """认网关口：① `GET /` 200 且正文含 CodeBuddy Gateway／Remote Control ② `/api/v1/info.cwd`
    **等于本工作区**（多实例区分的关键）。返回按 uptime 降序 ⇒ `[0]` 即最稳的那个。"""
    tok = os.environ.get(GW_ENV) or ""
    hits = []
    for p in _listen_ports(int(C.get("wake_max_ports") or 60)):
        st, body = _gw_http(p, "GET", "/", None, "", timeout=3.0)
        if st != 200 or not any(k in body for k in GW_TITLE_KEYS):
            continue
        cwd, up = "", 0.0
        st2, t2 = _gw_http(p, "GET", "/api/v1/info", None, tok, timeout=5.0)
        try:
            dd = json.loads(t2).get("data") or {}
            cwd, up = dd.get("cwd") or "", float(dd.get("uptime") or 0.0)
        except Exception:
            pass
        if _norm_p(cwd) != _norm_p(WS):
            continue
        sid = ""
        st3, t3 = _gw_http(p, "GET", "/api/v1/sessions/live", None, tok, timeout=5.0)
        try:
            sid = (json.loads(t3).get("data") or {}).get("sessionId") or ""
        except Exception:
            pass
        hits.append({"port": p, "cwd": cwd, "uptime": up, "sessionId": sid, "live_http": st3})
    hits.sort(key=lambda x: -x["uptime"])
    return hits


def _wakeup_log(rec: dict) -> None:
    try:
        with open(WAKEUPS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log("wake: append wakeups err %s" % e)


def need_user(reason: str) -> None:
    """遇阻 ⇒ 落 `NEED-USER.md`（报告里必须**明确喊「需用户介入」**）。"""
    try:
        NEED_USER.parent.mkdir(parents=True, exist_ok=True)
        NEED_USER.write_text(
            "# 🔴 需用户介入\n\n- 时间：%s\n- 原因：%s\n\n## 喊话\n**需用户介入**\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), reason), encoding="utf-8")
    except Exception:
        pass


def wake_round(st: dict, T: dict, V: dict | None = None) -> dict:
    """⛔ **已停用（2026-09-29）**：投递已**唯一化到监督程序**（`supervise()`），本函数不再被任何路径调用；
    它依赖的 `NEXT.md` 旧队列一并退役。⛔ 本轮不删（删除要按"函数边界＋夹层常量"的规矩单独做，
    避免重演误删常量那次），列入 ④ 收敛清单。"""
    """闸门全满足才投**一次**（一份内容只投一次）。返回本轮判读（写进 state，供视图/排查用）。

    触发面（🔴 2026-09-29 扩）：① `NEXT.md` 存在且 `gate=free`（有活派）
    ② **`NEXT.md` 不存在但出现「停滞」或「可派未派」** —— ⛔ 原实现只认 ①
    ⇒「没人发消息 ⇒ 没人跑程序 ⇒ 永远发现不了停滞」的死循环。"""
    info = {"skipped": ""}
    if not C.get("wake_enable", True):
        info["skipped"] = "disabled"
        return info
    nodes = T.get("nodes_raw") or []
    if nodes and all((n.get("status") == "done") for n in nodes):
        info["skipped"] = "all-done"          # 终止：任务图全 done ⇒ 不再唤醒
        return info
    synth = ""
    if NEXT_MD.exists():
        try:
            txt = NEXT_MD.read_text(encoding="utf-8", errors="replace")
        except Exception:
            info["skipped"] = "next-unreadable"
            return info
        try:
            gate = (json.loads(QUEUE.read_text(encoding="utf-8")) or {}).get("gate")
        except Exception:
            gate = None
        if gate != "free":
            info["skipped"] = "gate=%s" % gate
            return info
    else:
        # 🔴 2026-09-29 补（治「没人发消息 ⇒ 彻底静默」）：NEXT.md 只是「有活派」的载体，
        #    **不是唯一该把人叫起来的理由**。**停滞**（无人接活 / 有活可派未派）同样必须投一次；
        #    否则「有活 ∧ 谁都动不了 ∧ 程序一声不响」—— 实测 16:31→20:07 整 4.5 小时零投递，
        #    用户看到的就是「一直在发呆」。
        #    ⚠️ 哈希必须用**粗粒度键**（种类 ＋ 空闲按 30 分钟取整）：若拿带时间戳的摘要正文去算，
        #    会退化成每 180 秒投一次（刷屏）。
        idle_v = float((V or {}).get("idle") or 0)
        alert = str((V or {}).get("alert") or "")
        if alert.startswith("确定性静默"):
            # ⛔ 这一档是「**要用户拍板**」的状态 ⇒ **只投一次**（用常量键、不带时间桶）：
            #    它不会自己好，每 30 分钟重复喊一遍只会变成噪音；状态一变（排期恢复 / 有人跑起来 /
            #    新结论落库）键自然变 ⇒ 会再投一次。
            synth = "确定性静默"
        elif alert:
            synth = "僵住|%d|%s" % (int(idle_v // 30), alert)
        elif T.get("waste"):
            synth = "可派未派|%d" % int(idle_v // 30)
        if not synth:
            info["skipped"] = "no-next"
            return info
        txt = synth
        info["trigger"] = synth
    h = hashlib.sha1(txt.encode("utf-8", "replace")).hexdigest()[:16]
    wtext = str(C.get("wake_text") or "")
    if synth:
        wtext = ("协作程序报警：%s。请读 tmp/supervise-inbox/digest.md 与 NEED-USER.md，判「该谁动」"
                 "——**有活就派、被卡就明确报给用户**；本轮只做这一件，做完即停。"
                 % ((V or {}).get("alert") or "有活可派但无人接"))
    W = st.get("wake") or {}
    if W.get("hash") == h:
        info["skipped"] = "same-item"
        return info
    if time.time() - float(W.get("ts") or 0.0) < float(C.get("wake_min_gap") or 180):
        info["skipped"] = "too-soon"
        return info
    tok = os.environ.get(GW_ENV) or ""
    if not tok:
        info["skipped"] = "no-token"          # ⛔ 口令缺失只跳过，绝不落盘/回显
        return info
    gws = discover_gateways()
    if not gws:
        info["skipped"] = "no-gateway"
        return info
    # 🔴 2026-09-29 补（实测驱动）：**必须挑「有活会话」的那个口**，⛔ 不能只取 `gws[0]`。
    #    实测（本机 13:24）：同 cwd 两个口 —— `:62213` uptime 更大但 `/sessions/live` 为空，
    #    有活会话的 `:56944` 排在后面 ⇒ 原逻辑取 `gws[0]` ⇒ 直接落 `no-live-session` 跳过，
    #    **「有活会话也投不出去」**。改法最小：沿用 uptime 降序，取**第一个带 sessionId 的**。
    g = next((x for x in gws if x["sessionId"]), None)
    if g is None:
        info["skipped"] = "no-live-session"
        return info
    if _session_status(str(g.get("sessionId") or "")) == "working":   # 同上：忙时不投
        log("延后投递（旧路径）：目标会话正在执行")
        info["skipped"] = "target-busy"
        return info
    stx, body = _gw_http(g["port"], "POST", "/api/v1/sessions/%s/reply" % g["sessionId"],
                         {"text": wtext}, tok, timeout=20.0)
    ok = stx in (200, 201, 202)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "epoch": round(time.time(), 1),
           "port": g["port"], "sessionId": g["sessionId"], "hash": h, "http": stx, "ok": ok}
    _wakeup_log(rec)
    st["wake"] = {"ts": time.time(), "hash": h, "port": g["port"], "sessionId": g["sessionId"], "http": stx}
    log("wake reply -> %s@%s http=%s hash=%s" % (str(g["sessionId"])[:8], g["port"], stx, h))
    if not ok:
        need_user("唤醒投递失败：网关口 %s 回 %s（%s）" % (g["port"], stx, str(body)[:140]))
        info["skipped"] = "deliver-failed"
    else:
        info["delivered"] = rec
    return info


# ── ⑪ 任务队列（🔴 架构定案 2026-09-29 用户）：**状态由协作会话上报，⛔ 不靠猜** ──────────
#   ① 协作会话**开始执行** ⇒ 告诉协作程序 ⇒ 条目置 `running`（记录执行者＋开始时刻）
#   ② 协作会话**处理完毕** ⇒ 告诉协作程序 ⇒ 条目置 `done`（记录产物＋结束时刻）
#   ③ **监督程序**逐条读队列 ⇒ 有新变化 ⇒ 告诉主会话跟进；
#      **一段时间没有「执行中／执行完毕」的队列**（＝静默）⇒ **发心跳**让主会话检查状态。
#   ⛔ 本程序只"读队列 ＋ 写通知"，⛔ 不派活、⛔ 不开会话。
TASKS = INBOX / "tasks.json"                 # 🔴 **唯一权威**（队列本体）
TASK_EVENTS = INBOX / "tasks-events.jsonl"   # 只作审计（append-only，⛔ 不参与判定）
TO_MAIN = INBOX / "TO-MAIN.md"               # 监督程序给主会话的通知（投影 · 供人/AI 直接读）
TASK_STATES = ("pending", "running", "done", "blocked")   # 需求台账四态：待执行/执行中/已完成/有阻碍
QUEUE_IDLE_MIN = 5                           # 心跳**限流桶**（分钟）：同一状态下最多每 **5** 分钟发一次（2026-09-30 用户拍板：30→10→**5**）；⛔ 触发条件是 supervise() 的**四条件**（不是计时器）
HEARTBEAT_MIN_IDLE = 15                      # 🔴 2026-09-30 加：**「真停滞」门槛**（分钟）——
                                             #    距**上次任何进展** ≥ 15 分钟才允许发心跳；**有进展 ⇒ 一条都不发**。
                                             #    为什么：心跳原只要求"三条件成立"，而"目标未完成"在长任务期长期成立
                                             #    ⇒ 变成定期噪音，且**每次投递都会唤起主会话跑一轮 ⇒ 用户发消息撞 busy ⇒ 体感"卡"**。


def _load_tasks() -> dict:
    try:
        t = json.loads(TASKS.read_text(encoding="utf-8"))
        return t if isinstance(t, dict) else {}
    except Exception:
        return {}


def _save_tasks(t: dict) -> None:
    try:
        TASKS.write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log("tasks 写失败 %s" % e)


def task_report(tid: str, state: str, by: str = "", artifact: str = "", reason: str = "",
                line: str = "") -> int:
    """协作会话**上报**状态转移（需求台账四态）。用法：
    `python collabd.py --report <需求id> --state pending|running|done|blocked [--by 会话名] [--artifact 产物] [--reason 阻碍原因]`
    ⚠️ `--state blocked` **必须**带 `--reason`（⛔ 不许只标"卡了"不说卡在哪、谁在等）。
    """
    tid = (tid or "").strip()
    state = (state or "").strip().lower()
    if not tid or state not in TASK_STATES:
        print("用法：--report <需求id> --state pending|running|done|blocked [--by 会话名] [--artifact 产物] [--reason 原因]")
        return 2
    t = _load_tasks()
    rec = dict(t.get(tid) or {})
    prev = str(rec.get("state") or "")
    rec.update({"state": state, "by": by or rec.get("by", ""), "t": time.time()})
    if line:
        rec["line"] = line          # 线归属**写进条目**（⛔ 不再从会话 cwd 反推）
    if artifact:
        rec["artifact"] = artifact
    if state == "blocked":
        rec["block_reason"] = reason or rec.get("block_reason", "")
    else:
        rec.pop("block_reason", None)      # 解除阻碍 ⇒ 原因一并清掉（⛔ 不留过期原因误导后续判断）
    if state == "running" and not rec.get("t_start"):
        rec["t_start"] = time.time()
    if state == "done":
        rec["t_end"] = time.time()
    t[tid] = rec
    # 🔴🔴 2026-09-30 改：**乐观重试**（治"多个主会话同时调用本 skill ⇒ 台账丢更新"）
    #    ⛔ **为什么不用文件锁**：实测在**隔离目录**里 OS 锁三项全绿（同进程反复 lock/unlock 0.01s；
    #       两进程真互斥 0.00s；释放后立刻拿到 0.00s），但**接进生产后** `--once` 与 6 个并发 `--report`
    #       **全部 rc=124 卡住不退出**（⚠️ 数据其实写成功了）⇒ **本机这个方案不能用** ⇒ 换**零锁**方案。
    #    ✅ 乐观重试：**写 → 回读核对 → 被覆盖 ⇒ 以磁盘为准重新合并自己这条**（最多 3 次）。
    #       适用性：本场景是"**低冲突、写小文件**" ⇒ 足够，且**没有任何卡死风险**。
    #    ⚠️ 实测登记：**8 并发 `--report` 在"无任何保护"的旧实现下也是 8/8 全成功**（没压出丢更新）
    #       ⇒ 说明这是**低概率事件**；本补丁是"真冲突时能自愈"的保险，⛔ 不是复现过的故障修复。
    for _try in range(3):
        _save_tasks(t)
        try:
            _back = _load_tasks()
        except Exception:
            _back = {}
        if str((_back.get(tid) or {}).get("state") or "") == state:
            break                                   # ✅ 自己这条已写进去
        if isinstance(_back, dict) and _back:
            t = dict(_back)                         # ⚠️ 被覆盖过 ⇒ 以磁盘为准，重新合并自己这条
            t[tid] = rec
        time.sleep(0.05)
    try:
        with open(TASK_EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "id": tid, "from": prev,
                                "to": state, "by": by, "artifact": artifact},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass
    log("task report %s: %s -> %s (%s)" % (tid, prev or "-", state, by or "?"))
    print("OK 已上报：%s %s -> %s" % (tid, prev or "(新)", state))
    return 0


def _deliver_str(text: str, key: str, st: dict) -> dict:
    """把一段文字投给**活会话**（网关官方 reply，不夺 ACP writer）。⛔ 口令只进程内用。"""
    info = {"skipped": ""}
    if not C.get("wake_enable", True):
        info["skipped"] = "disabled"
        return info
    # 🔴 2026-09-29 用户定案：**投递前先判目标会话是否在跑 —— 只有"没在跑"才投**。
    #    （正在 `working` 的会话，投进去会插进它当前的轮次里 ⇒ 等它空闲再投，与 §2.1 握手同源）
    _tgt = _main_sid(st)
    if _tgt and _session_status(_tgt) == "working":
        log("延后投递：主会话 %s 正在执行 ⇒ 等它空闲" % _tgt[:8])
        info["skipped"] = "target-busy"
        return info
    # 🔴🔴 2026-09-30 加：**快速否决（⛔ 不取锁、⛔ 不删锁）** —— 治"常驻被护栏杀掉"（实测真因）：
    #    `_deliver_str` 每轮尝试都会在 finally 里 `unlink(wake.lock)`；宿主有 **SafeDelete 批量删除护栏**
    #    （按"本轮删除次数"计数，达阈值即要求确认并**拒绝**）⇒ 常驻监督程序跑约 50 次后**被系统终止**。
    #    实测：后台任务 `Syz5DD` 跑 **48m43s** 后 `failed`，stdout 原文
    #    `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":50,"threshold":50,…targets:[…\wake.lock"]}`
    #    ⇒ **心跳的时钟就是这么断的**（用户当晚的关切正是"别回来还在发呆"）。
    #    修法：把 hash / 最小间隔的**预判提到取锁之前** —— 高频的 `same-item` / `too-soon` 路径
    #    （实测占绝大多数：日志里成片 "投递未成（too-soon）"）**根本不碰锁文件** ⇒ 删除次数降到"只在可能真投时"。
    #    ⚠️ 取舍：锁内仍会**重读 STATE 再判一次**（那是权威判据），所以竞态窗口由 `wake_min_gap` 兜底 ⇒ ⛔ 不会双投。
    _h0 = hashlib.sha1((key + "|" + text).encode("utf-8", "replace")).hexdigest()[:16]
    _W0 = st.get("wake") or {}
    if _W0.get("hash") == _h0:
        info["skipped"] = "same-item"
        return info
    if time.time() - float(_W0.get("ts") or 0.0) < float(C.get("wake_min_gap") or 300):
        info["skipped"] = "too-soon"
        return info
    # 🔴 2026-09-29 23:12 修（"卡 working / 发消息没反应"复盘）：
    #    **两个常驻进程（协作程序 ＋ 监督程序）各自投递、共用状态文件但无互斥** ⇒ 都读到旧哈希
    #    ⇒ **同一内容成对重复投递**（实测同一 hash 秒级出现两次：22:24:04/30、22:40:23/26、
    #    22:48:32/55、22:51:01×2）⇒ 每次都往主会话的会话队列里**压消息**（22:53 起该会话进入
    #    parkInQueue「只进不出」⇒ 用户随后发的消息排在后面 ⇒ 表现为"卡死、没反应"）。
    #    ⇒ 三招：**原子取锁（跨进程互斥）＋ 拿锁后重读状态 ＋ 投完立刻落盘**。
    _lk = INBOX / "wake.lock"
    _got = False
    try:
        _fd = os.open(str(_lk), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(_fd, str(os.getpid()).encode())
        os.close(_fd)
        _got = True
    except FileExistsError:
        try:
            if time.time() - _lk.stat().st_mtime > 120:          # 残锁 >120 秒 ⇒ 抢占
                _lk.unlink()
                _fd = os.open(str(_lk), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(_fd, str(os.getpid()).encode())
                os.close(_fd)
                _got = True
        except Exception:
            _got = False
    except Exception:
        _got = True                                              # 取锁本身出错 ⇒ ⛔ 不因它挡死投递
    if not _got:
        info["skipped"] = "locked"
        return info
    try:
        try:                                                     # 拿锁后**重读**状态（⛔ 不用陈旧内存哈希）
            _st2 = json.loads(STATE.read_text(encoding="utf-8")) or {}
            if isinstance(_st2, dict) and _st2.get("wake"):
                st["wake"] = _st2["wake"]
        except Exception:
            pass
        h = hashlib.sha1((key + "|" + text).encode("utf-8", "replace")).hexdigest()[:16]
        W = st.get("wake") or {}
        if W.get("hash") == h:
            info["skipped"] = "same-item"
            return info
        if time.time() - float(W.get("ts") or 0.0) < float(C.get("wake_min_gap") or 300):
            info["skipped"] = "too-soon"
            return info
    finally:
        try:
            _lk.unlink()
        except Exception:
            pass
    tok = os.environ.get(GW_ENV) or ""
    if not tok:
        # 🔴 投递不到 ≠ 可以沉默 —— 但**两种"没口令"要分开处置**（见 `FROM_HOOK` 处说明）：
        if FROM_HOOK:
            need_user("有内容要投给主会话，但**连宿主钩子进程里都拿不到网关口令**（%s）"
                      "—— 属异常（宿主版本/注册面变了？），⛔ 请勿当成常驻进程的正常现象" % key)
        else:
            log("投递跳过：本进程不在宿主进程树内（无 %s）—— **设计使然**（投递由宿主钩子唤起）" % GW_ENV)
        info["skipped"] = "no-token"
        return info
    gws = discover_gateways()
    # 🔴 2026-09-29 修（用户截图暴露）：**桌面上会有很多会话窗口** ⇒ 不能只取"第一个带会话 id 的口"，
    #    否则通知会**投错窗口**。⇒ 目标必须是**声明为主会话**的那个（`--declare --role main`）；
    #    没声明才回落到旧行为。
    _want = _main_sid(st)
    g = next((x for x in gws if x["sessionId"] and _want and str(x["sessionId"]) == _want), None) if gws else None
    if g is None:
        g = next((x for x in gws if x["sessionId"]), None) if gws else None
    if g is not None and _session_status(str(g.get("sessionId") or "")) == "working":
        log("延后投递：目标会话 %s 正在执行 ⇒ 等它空闲" % str(g.get("sessionId"))[:8])
        info["skipped"] = "target-busy"
        return info
    if g is None:
        need_user("有内容要投给主会话，但**没有活会话**可投（%s）—— 请打开任意工作区会话" % key)
        info["skipped"] = "no-live-session"
        return info
    stx, _body = _gw_http(g["port"], "POST", "/api/v1/sessions/%s/reply" % g["sessionId"],
                          {"text": text}, tok, timeout=20.0)
    ok = stx in (200, 201, 202)
    _wakeup_log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "epoch": round(time.time(), 1),
                 "port": g["port"], "sessionId": g["sessionId"], "hash": h, "http": stx, "ok": ok,
                 "kind": key})
    # 🔴 2026-09-30 加：投递成功**不等于**被消费 ⇒ 记下"期望"（下一轮回查该会话有没有真的动）。
    st["wake"] = {"ts": time.time(), "hash": h, "port": g["port"], "sessionId": g["sessionId"],
                  "http": stx, "expect": {"sid": g["sessionId"], "at": time.time()}}
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")   # 立刻落盘 ⇒ 另一进程读到新哈希
    except Exception:
        pass
    log("notify(%s) -> %s@%s http=%s hash=%s" % (key, str(g["sessionId"])[:8], g["port"], stx, h))
    info["delivered"] = {"http": stx, "kind": key}
    return info


def _session_status(sid: str) -> str:
    """只读宿主库拿某会话的状态（`working` ＝ 正在执行）。取不到 ⇒ 空串（⛔ 不猜）。"""
    if not sid:
        return ""
    try:
        con = _db()
        row = con.execute("select status from sessions where id=?", (sid,)).fetchone()
        con.close()
        return str(((row or [""])[0]) or "").lower()
    except Exception as e:
        log("session_status 读失败 %s" % e)
        return ""


# ── 🔴 2026-09-30 加：**宿主侧「消息卡住」探针**（用户给出窗口 22:55–23:20，实测定型） ────
#    指纹：工作区日志里 `PromptIterator … route=parkInQueue … hasWaiter=false`
#      ⇒ **消息进了队列、但没有消费者** ⇒ 界面一直转、发消息没反应。
#      真因＝**宿主客户端连接状态丢失**（同窗口 `sendToClient: No state found for connectionId`
#      303 次，其他小时 0 次）；实测队列 `queueLen` 0→1→2 逐条堆积。
#    🔴 **AI 侧修不了这个根因** —— 唯一解法是**让客户端重新挂上该会话**（切走再切回／重开窗口）。
#    ⇒ 我们能做的只有一件：**一发生就发现，并把"该点哪一下"直接写进 `NEED-USER.md`**。
#    详见 `references/pitfalls.md P0-5`。
PARK_TAIL = 400 * 1024        # 只读日志尾部（⛔ 不许整文件扫：工作区日志可达几十 MB）
PARK_FRESH = 30 * 60          # 秒：指纹超过这个时长就不算"正在卡"
PARK_STAMP = INBOX / "_park.stamp"   # 告警节流（⛔ 不每轮刷同一条）
PARK_GAP = 180                # 秒
CONSUME_GRACE = 240           # 秒：投递后多久没见目标会话活动 ⇒ 判「没被消费」


def _log_dirs() -> list:
    """候选工作区日志路径：`<配置根>/logs/<今天|昨天>/<本工作区名>__*.log`。
    ⚠️ 日志**跨日不换名**（实测：主会话 09-29 那份一直写到 23:59）⇒ 两个日期都试。
    """
    root = os.path.join(os.environ.get("CODEBUDDY_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".workbuddy"), "logs")
    name = os.path.basename(str(WS).replace("\\", "/").rstrip("/")) or ""
    out = []
    for d in (time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))):
        try:
            for fn in os.listdir(os.path.join(root, d)):
                if name and fn.startswith(name + "__") and fn.endswith(".log"):
                    out.append(os.path.join(root, d, fn))
        except Exception:
            continue
    return out


def _is_park_line(ln: str) -> bool:
    """判据：**只认真正的 AcpView 记录行**。
    🔴 **⛔ 不能只搜字符串**（2026-09-30 当场踩到）：取证命令（`grep parkInQueue …`）的输出
    会**被写进同一份工作区日志**（`SandboxShell ProcessOutput … content=`）⇒ 探针命中自己的回声 ⇒ **假阳性**。
    """
    return ("parkInQueue" in ln and "hasWaiter=false" in ln
            and "[AcpView][PromptIterator] received prompt" in ln
            and "ProcessOutput" not in ln and "content=" not in ln)


def probe_host_park() -> dict:
    """扫日志尾部找「parkInQueue ＋ hasWaiter=false」指纹。⛔ 只读、⛔ 不写任何东西。"""
    hit = {"n": 0, "last": "", "file": ""}
    for p in _log_dirs():
        try:
            sz = os.path.getsize(p)
            with open(p, "rb") as f:
                if sz > PARK_TAIL:
                    f.seek(sz - PARK_TAIL)
                buf = f.read().decode("utf-8", "replace")
        except Exception:
            continue
        for ln in buf.splitlines():
            if _is_park_line(ln):        # ⚠️ 判据见 `_is_park_line`（⛔ 不搜字符串，防"回声假阳性"）
                hit["n"] += 1
                hit["file"] = os.path.basename(p)
                m = re.search(r"\[(\d{4}/\d{1,2}/\d{1,2} (\d\d:\d\d:\d\d))", ln)
                if m:
                    hit["last"] = m.group(2)
    return hit


def _sess_activity(sid: str) -> float:
    """某会话最后活动时刻（epoch 秒）。⛔ 取不到 ⇒ 0（不猜）。"""
    if not sid:
        return 0.0
    try:
        con = _db()
        row = con.execute("select updated_at from sessions where id=?", (sid,)).fetchone()
        con.close()
        return float(((row or [0])[0]) or 0) / 1000.0
    except Exception:
        return 0.0


def check_delivery_consumed(st: dict) -> dict:
    """🔴 **「投出去」≠「它跑起来了」** —— 投递后必须**回查目标会话有没有真的动**。

    判据：目标会话 `updated_at` 是否**晚于**投递时刻。⛔ 不靠"网关回了 200"（那只证明对方收下了）。
    超 `CONSUME_GRACE` 秒仍未动 ⇒ 落 `NEED-USER.md`（含**用户唯一该做的那一下**），⛔ 不静默。
    """
    w = st.get("wake") or {}
    exp = w.get("expect") or None
    if not exp:
        return {}
    sid, at = str(exp.get("sid") or ""), float(exp.get("at") or 0)
    if not sid or not at:
        return {}
    act = _sess_activity(sid)
    if act and act > at:
        w.pop("expect", None)
        st["wake"] = w
        return {"consumed": True, "sid": sid[:8], "lag": round(act - at, 1)}
    if time.time() - at < CONSUME_GRACE:
        return {"waiting": True, "sid": sid[:8]}
    w.pop("expect", None)
    st["wake"] = w
    need_user(
        "投给会话 `%s` 的通知**没被消费**（投出后 %.1f 分钟该会话零活动）。\n"
        "典型的**宿主侧卡住**：`parkInQueue` ＋ `hasWaiter=false` —— 消息进了队列、**没有消费者**。\n"
        "🔴 **这一步 AI 侧无法自救**。请你**让客户端重新挂上该会话**：把窗口**切走再切回**，"
        "或**关掉再打开该会话窗口** —— 队列随即会被排空。⛔ 别反复发消息试探（只会在队尾再堆一条）。"
        % (sid[:8], (time.time() - at) / 60.0))
    return {"unconsumed": True, "sid": sid[:8]}


def _acc_short() -> list:
    """读 `goal.json.acceptance_state` ⇒ 返回**非 pass** 的验收项（供心跳正文直接列出"还差什么"）。
    ⚠️ 读不到 ⇒ 返回提示行（⛔ 不因它判"全过"）；`unknown` 视为**未过**。
    """
    try:
        _as = (json.loads((INBOX / "goal.json").read_text(encoding="utf-8")).get("acceptance_state") or {})
        rows = ["- **%s**：%s" % (k, v) for k, v in sorted(_as.items())
                if not str(k).startswith("_") and str(v or "").strip().lower() != "pass"]
        return rows or ["- （全部 pass —— 目标已达成，可收口）"]
    except Exception:
        return ["- ⚠️ 读不到 `goal.json.acceptance_state`（⛔ 不因此判完成）"]


def _goal_short() -> str:
    """本需求目标的 `short`（两级命名前缀里的第 2 级）。"""
    try:
        return str(json.loads((INBOX / "goal.json").read_text(encoding="utf-8")).get("short") or "")
    except Exception:
        return ""


def _in_project(sid: str, title: str, main_sid: str, short: str) -> bool:
    """🔴 **判定一个会话是否属于本需求目标项目**（2026-09-30 用户明示：「要明确哪些会话是属于某个需求目标项目的」）。

    **三级判据（任一命中即算）—— ⛔ 与 cwd 无关**
      ① **主会话**：`sid == main_sid`（＝ `--declare --role main` 显式声明出来的那个）
      ② **协作会话**：**标题含 `[<goal.short>]`** —— 即 §2.3 定的**两级命名前缀**（派活时由自动化名自动带进标题）
      ③ 其它会话：`--declare --goal <goalId>` 显式声明（⚠️ 尚未接线，留待需要时）
    ⛔ **不命中 ⇒ 不算本项目**（例：别的项目的会话、标题里没有本项目的主题前缀 `[<主题>]` ⇒ 归属为"否"）。
    🔴 **⛔ 刻意不用 cwd 推断** —— 架构 §2.3 明令「**绝不回落到 cwd 推断**」（我 2026-09-30 一度用 cwd 判，属违规，已改回）。
    """
    if main_sid and sid == main_sid:
        return True
    return bool(short) and ("[%s]" % short) in str(title or "")


def ready_next(st: dict) -> dict:
    """🔴 **收尾确认 —— 用它替代"盲等 5~8 分钟"**（用户 2026-09-30 说明其由来）：
    用户原话：「5-8 应该是之前出现 **前面还没执行完 就开始下一棒了**，
               **要是能避免这个问题 可以压缩时间**」
    ⇒ 5~8 分钟 = **用固定延迟赌"上一棒已收尾"**；既然能**真的确认**，就该压到"确认即起"。

    **三条判据（全满足 ⇒ 可立刻排下一棒，排期 = 现在 + 30~60 秒）**
      ① 除**主会话**外，**没有别的会话处于 `working`** —— 读**宿主状态**，⛔ 不是"它自己说完了"
      ② 台账里**没有 `running` 的条目** —— ⛔ 不认"它说做完了"，要终态（done/blocked）
      ③ 上一棒**没有留下待消费的投递**（`wake.expect` 已清）—— ⛔ 不等一个还没被取走的通知
    ⚠️ 任一不满足 ⇒ **⛔ 不排**，等下一次心跳/反馈再判（⛔ 不盲等、也⛔ 不硬上）。
    """
    out = {"ok": False, "why": [], "working_others": [], "running_tasks": []}
    # ① 除主会话外还有谁在跑？
    me = _main_sid(st)
    short = _goal_short()
    try:
        con = _db()
        for row in con.execute("select id, coalesce(nullif(custom_title,''),title,'') "
                               "from sessions where status='working'"):
            sid, ti = str(row[0] or ""), str(row[1] or "")
            if me and sid == me:
                continue                       # ⛔ 主会话自己不算（否则永远拦住）
            if not _in_project(sid, ti, me, short):
                # ⛔ **别的线的会话不算本项目在跑**（2026-09-30 修：曾把别的项目的会话算进来 ⇒ 误拦派棒）
                out.setdefault("ignored_others", []).append("%s(%s)" % (sid[:8], ti[:24]))
                continue
            out["working_others"].append("%s(%s)" % (sid[:8], ti[:28]))
        con.close()
    except Exception as e:
        out["why"].append("读宿主库失败：%s" % e)
    if out["working_others"]:
        out["why"].append("还有会话在跑：%s" % "、".join(out["working_others"]))
    # ② 台账里还有 running？
    for k, v in (_load_tasks() or {}).items():
        if str((v or {}).get("state") or "") == "running":
            out["running_tasks"].append(k)
    if out["running_tasks"]:
        out["why"].append("台账仍有执行中：%s" % "、".join(out["running_tasks"]))
    # ③ 上一棒的**单条反馈**是否还等着主会话处理？（握手没走完 ⇒ 本轮别急着重排）
    #    🔴 2026-09-30 修（**本命令上线 3 分钟就踩到**）：原实现查的是 `wake.expect`，
    #       而**心跳投递同样会留下 `expect`** ⇒ `--ready-next` 会被"心跳尚未被消费"拦死 ⇒
    #       实测输出「⛔ 先别排：上一棒投递尚未被消费」—— 而那条"未消费的投递"正是**刚发的心跳**，
    #       主会话正在处理它 ⇒ **假拦**（若不改，提速全部作废）。
    #    ⇒ 判据只认**握手等待**（`notify_awaiting`：反馈了一条、等主会话处理），⛔ 不看心跳的 expect。
    _aw = st.get("notify_awaiting") or None
    if _aw:
        out["why"].append("上一棒的单条反馈仍在等主会话处理（%s，阶段 %s）"
                          % (_aw.get("item"), _aw.get("phase")))
    out["ok"] = not out["why"]
    return out


def _guard_says_stop() -> bool:
    """🔴 **被守护拉起时**（`COLLABD_GUARDED=1`）：守护停下 ⇒ 本程序**优雅退出**。
    判据只有两个（⛔ 不猜）：① 停止标志 `guard.stop` ② 守护心跳过期（>90 秒 ⇒ 守护已死）。
    ⚠️ 手动跑（`--once`／钩子）没有该环境变量 ⇒ ⛔ 不受影响。
    """
    if os.environ.get("COLLABD_GUARDED") != "1":
        return False
    try:
        if (INBOX / "guard.stop").exists():
            return True
    except Exception:
        pass
    try:
        ts = float(json.loads((INBOX / "guard.json").read_text(encoding="utf-8")).get("ts") or 0)
        return (time.time() - ts) > 90
    except Exception:
        return False


GOAL_F = INBOX / "goal.json"


def load_goal() -> dict:
    """🔴 **任务目标**（整套机制的运行中心）。缺失/无 title ⇒ 返回空 dict
    —— 调用方**必须显式处理"未声明目标"**，⛔ 不得假装有目标。"""
    try:
        g = json.loads(GOAL_F.read_text(encoding="utf-8"))
        return g if isinstance(g, dict) and str(g.get("title") or "").strip() else {}
    except Exception:
        return {}


def goal_line() -> str:
    """摘要/通知里显示的一行目标。"""
    g = load_goal()
    if not g:
        return "⚠️ **未声明任务目标**（机制不知道该围绕什么跑）"
    sh = str(g.get("short") or "").strip()
    return "🎯 围绕目标：%s%s" % (g["title"], ("（主题前缀 `[%s]`）" % sh) if sh else "")


def pw_fingerprint() -> str:
    """网关口令的**指纹**（sha256 前 12 位）—— **只用于判"变没变"**。
    ⛔ 绝不落口令本身；指纹不可逆、**不能用于鉴权**（所以不算"口令落盘"）。"""
    tok = os.environ.get(GW_ENV) or ""
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()[:12] if tok else ""


def goals_open() -> bool:
    """**需求是否仍未完成**。
    🔴 判据（迁移期取两处**并集**，⛔ 不取任何"自述完成"；读不到 ⇒ 不因它判完成）：
      · **需求台账**（`tasks.json`）里有非 `done` 的条目
      · **任务图**里有非 `done` 的节点
      · 🔴 **验收判据**（`goal.json.acceptance_state`）里**任一非 `pass`**（2026-09-30 加）
    """
    open_ = False
    try:
        for _k, v in (_load_tasks() or {}).items():
            if str((v or {}).get("state") or "") != "done":
                open_ = True
    except Exception as e:
        log("goals_open 读台账失败 %s" % e)
    try:
        g = json.loads(TG.read_text(encoding="utf-8"))
        for n in (g.get("nodes") or []):
            if str(n.get("status") or "") != "done":
                open_ = True
    except Exception as e:
        log("goals_open 读任务图失败 %s" % e)
    # 🔴🔴 2026-09-30 加（用户点破「目标都还未达成，为什么没触发目标状态评估」）：
    #    **前两路测的是"活干完了吗"，⛔ 不是"目标达成了吗"** —— 铁证：任务图里 **N14 节点是 `done`，
    #    而 N14 判的正是「V1 ❌ 未过」**。⇒ 必须**再读一路"验收判据的真实状态"**。
    #    ⛔ 为什么不直接把前两路删掉：它们防的是「**漏待办**」（已规划但还没派棒 ⇒ 不在台账）；
    #       本路防的是「**误判完成**」；二者**互补**，取并集。
    #    ⚠️ `unknown` 视为**未过**（⛔ 不臆断为过）⇒ 宁可多提醒，⛔ 不漏。
    try:
        _gj = json.loads((INBOX / "goal.json").read_text(encoding="utf-8"))
        _as = _gj.get("acceptance_state") or {}
        for _k, _v in _as.items():
            if str(_k).startswith("_"):
                continue
            if str(_v or "").strip().lower() != "pass":
                open_ = True
    except Exception as e:
        log("goals_open 读 acceptance_state 失败 %s" % e)
    return open_


def parse_session_name(name: str) -> dict:
    """解析**两级前缀**命名：`[角色]-[主题]-<具体>`（如 `[协作]-[<主题>]-N9…`）。
    返回 `{"role": "main"/"worker"/"", "topic": str, "ok": bool}`。
    ⛔ 解析不出就返回空（⛔ 不猜）；`ok=False` 表示**没按约定命名**。"""
    nm = str(name or "").strip()
    if not nm.startswith("["):
        return {"role": "", "topic": "", "ok": False}
    parts = nm.split("-")
    r = parts[0].strip("[]").strip() if parts else ""
    role = {"主": "main", "协作": "worker"}.get(r, "")
    topic = parts[1].strip("[]").strip() if len(parts) > 1 and parts[1].strip().startswith("[") else ""
    return {"role": role, "topic": topic, "ok": bool(role and topic)}


def topic_of(st: dict, sid: str = "") -> str:
    """会话的**二级前缀＝"在协作什么"**：① 命名里的 `[主题]` ② 声明（`--declare --topic`）③ 空。"""
    if sid:
        try:
            con = _db()
            row = con.execute("select coalesce(nullif(custom_title,''), title, '') from sessions where id=?",
                              (sid,)).fetchone()
            con.close()
            tp = parse_session_name(str((row or [""])[0] or "")).get("topic") or ""
            if tp:
                return tp
        except Exception as e:
            log("topic_of 读标题失败 %s" % e)
    return str((st.get("topics") or {}).get(sid) or "")


def role_of(st: dict, sid: str = "") -> str:
    """会话角色 —— 🔴 **以「会话命名前缀」为准**（2026-09-29 用户定案）：
    **同工作区、跨工作区都适用**，因为前缀与 `cwd` 无关。

    优先级：**① 命名前缀（`[主]`／`[协作]`）② 显式声明（`--declare`）③ 未声明**
    · 命名前缀：**派活时给自动化命名加前缀** ⇒ 宿主把它带成会话标题（实测：会话标题＝自动化名）
    · 显式声明：会话自己跑 `collabd.py --declare --role main|worker`（sid 自动从环境取）——
      留给"主会话自己改名不方便／标题没前缀"的场合
    · ⛔ 都取不到 ⇒ 返回空串（＝**未声明**）——调用方必须按"未声明"处理，
      ⛔ **绝不回落到 cwd 推断**（那正是历史故障的来源）
    """
    if sid:
        try:
            con = _db()
            row = con.execute("select coalesce(nullif(custom_title,''), title, '') from sessions where id=?",
                              (sid,)).fetchone()
            con.close()
            t = str((row or [""])[0] or "")
            pr = parse_session_name(t)
            if pr["ok"]:
                return str(pr["role"])
            if t.startswith("[主]") or t.startswith("[协作]"):
                log("⚠️ 会话 %s 命名只有一级前缀（缺 `[主题]`）⇒ 建议改成 [角色]-[主题]-<具体>" % str(sid)[:8])
                return "main" if t.startswith("[主]") else "worker"
        except Exception as e:
            log("role_of 读标题失败 %s" % e)
    roles = dict(st.get("roles") or {})
    if sid and roles.get(sid):
        return str(roles[sid])
    if not sid:
        return ""
    return ""


def _main_sid(st: dict) -> str:
    """主会话的 session id：① 首选**声明为 main** 的那个 ② 退回最近一次投递到的会话
    ③ 都取不到 ⇒ 空串（⛔ 不猜）。"""
    for _sid, _r in (st.get("roles") or {}).items():
        if str(_r) == "main" and str(_sid).startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f")):
            return str(_sid)
    return str(((st.get("wake") or {}).get("sessionId")) or "")

def _main_state(st: dict) -> str:
    """主会话**当前状态**（`working` ⇒ 正在执行）。取不到 ⇒ 空串（⛔ 不猜）。"""
    sid = _main_sid(st)
    try:
        con = _db()
        if sid:
            row = con.execute("select status from sessions where id=?", (sid,)).fetchone()
        else:
            row = con.execute("select status from sessions where status='working' limit 1").fetchone()
        con.close()
        return str(((row or [""])[0]) or "").lower()
    except Exception as e:
        log("main_state 取失败 %s" % e)
        return ""


def reconcile(st: dict) -> dict:
    """启动对账（收编）—— 治「守护启动前已有协作会话在跑」：那些棒没进过台账 ⇒ 监督程序看不见
    ⇒ 主会话以为没人在跑 ⇒ 可能重复派活。
      收编：宿主库 working 会话（排除观察者自己）若台账没有 ⇒ 建条目 sess:<前8位>，state=running，
            _source=启动对账收编；线留空并标「待确认」（不从标题猜线）。
      僵尸：台账里 running 但除自己外无人跑、也无 IN_PROGRESS ⇒ 标「有阻碍 ⇒ 待主会话重派」。
    不投递、不改别人文件；幂等。
    """
    out = {"adopted": [], "zombie": [], "skip": ""}
    tk = _load_tasks()
    try:
        con = _db()
        allrun = [(str(r[0] or ""), str(r[1] or "")) for r in
                  con.execute("select id, coalesce(nullif(custom_title,''),title,'') "
                              "from sessions where status='working'")]
        inprog = int(con.execute("select count(*) from automation_runs where status='IN_PROGRESS'").fetchone()[0])
        con.close()
    except Exception as e:
        out["skip"] = "读宿主库失败：%s" % e
        return out
    me = (os.environ.get("CODEBUDDY_SESSION_ID") or "")[:8]
    others = [(s, ti) for s, ti in allrun if not (me and s[:8] == me)]
    for sid, title in others:
        key = "sess:" + sid[:8]
        if key in tk:
            continue
        tk[key] = {"state": "running", "by": (title[:40] or sid[:8]), "t": time.time(),
                   "title": title[:80], "line": "",
                   "_source": "启动对账收编（队列开启前就已在跑）", "_line": "待确认（不从标题猜线）"}
        out["adopted"].append(key)
    if not others and not inprog:
        for k, v in list(tk.items()):
            if str((v or {}).get("state")) == "running":
                nv = dict(v or {})
                nv["state"] = "blocked"
                nv["block_reason"] = "执行者已消失（除观察者外无在跑会话、无 IN_PROGRESS 运行）⇒ 待主会话重派"
                nv["t"] = time.time()
                tk[k] = nv
                out["zombie"].append(k)
    if out["adopted"] or out["zombie"]:
        _save_tasks(tk)
    log("reconcile adopted=%s zombie=%s" % (out["adopted"], out["zombie"]))
    return out


def supervise(st: dict, deliver: bool = True, mutate: bool = True) -> dict:
    """**监督程序职责** —— 🔴 **反馈协议 ＝ 单条 ＋ 握手**（2026-09-29 用户定案）：

    ① **一次只反馈一条**任务状态 ⇒ 退回等主会话处理；
    ② 反馈成功后进入**等待状态**：**定期监督主会话是否在执行**（`sessions.status='working'`）；
    ③ **主会话执行结束**（working 消失）⇒ 才反馈**下一条**；
    ④ 另：**一段时间没有「执行中／执行完毕」的队列** ⇒ 发心跳让主会话检查状态。
       ⚠️ 等待期间**不发心跳**（⛔ 不叠加消息，一条没处理完就不发第二条）。

    ⛔ 只读队列 ＋ 写通知；⛔ 不派活、⛔ 不开会话。
    ⚠️ 通知正文**不含秒级时间**（逐字稳定，否则内容哈希去重失效 ⇒ 刷屏）。
    ⛔ 兜底：反馈后 **5 分钟**（2026-09-30 由 20 分钟提速）仍未见主会话执行 ⇒ 放行下一条并记「需用户介入」，⛔ 不无限卡死。

    🔴 `mutate`（2026-09-30 加）—— **只有「投递方」才配推进队列**：
      · `deliver=True, mutate=True`（`--tick`，**宿主钩子唤起**）＝ **唯一的投递方 ⇒ 唯一的推进方**；
      · `deliver=False, mutate=False`（`--once`／协作程序）＝ **纯投影**：只算、只写 `TO_MAIN.md`，
        ⛔ **绝不**改 `notify_pending`／`notify_awaiting`／`notified`／`hb_since`／`q_sig`／`pw_fp`。
    ⚠️ 为什么必须分开（今天实测到的一型**静默丢件**）：钩子在 `UserPromptSubmit` 上**先**跑 `--once`
      （节流 3 分钟，谁发话都会跑）—— 若它也推进队列，就会**把待反馈项"消费"掉却不投递**
      ⇒ 紧接着的 `--tick` 看到队列已空 ⇒ **通知永远发不出去**（表现为"程序在跑，主会话什么也没收到"）。
    """
    t = _load_tasks()
    now = time.time()
    # 🔴 **口令指纹探针**（2026-09-29 用户问"口令会变化吗" ⇒ 不猜，装探针）：
    #    指纹变了 ⇒ 记日志 ＋ 落 `NEED-USER.md`（常驻进程需重启一次才能继续投递）。
    _fp = pw_fingerprint()
    _prev = str(st.get("pw_fp") or "")
    if mutate and _fp and _prev and _fp != _prev:
        log("⚠️ 网关口令指纹变化：%s -> %s" % (_prev, _fp))
        need_user("网关口令**已变化**（指纹 %s -> %s）⇒ 进程需重启一次才能继续投递" % (_prev, _fp))
    if _fp and mutate:
        st["pw_fp"] = _fp
    sig = "|".join("%s=%s" % (k, (v or {}).get("state")) for k, v in sorted(t.items()))
    if mutate and sig != str(st.get("q_sig") or ""):
        st["q_sig"] = sig
        st["q_transition_at"] = now
    notified = dict(st.get("notified") or {})
    info = {"n": len(t), "awaiting": "", "sent": "", "phase": "",
            "pw": {"have": bool(_fp), "fp": _fp}}

    # ① 待反馈序列：**按发生顺序**排队（⛔ 不一次倾倒）
    pend = list(st.get("notify_pending") or [])
    for k, v in sorted(t.items()):
        stt = str((v or {}).get("state") or "")
        if stt in ("running", "done") and notified.get(k) != stt and ("%s=%s" % (k, stt)) not in pend:
            pend.append("%s=%s" % (k, stt))
    if mutate:
        st["notify_pending"] = pend

    aw = st.get("notify_awaiting") or None
    if aw:
        # ②③ 等待状态：定期监督主会话是否在执行 ⇒ 执行结束才放行下一条
        ms = _main_state(st)
        phase = str(aw.get("phase") or "wait-start")
        if phase == "wait-start":
            if ms == "working":                 # 主会话**已在执行** ⇒ 进第二阶段
                if mutate:
                    aw["phase"], aw["t_working"] = "wait-done", now
                    log("握手：主会话已开始执行（%s）" % aw.get("item"))
            elif mutate and now - float(aw.get("t") or now) > 5 * 60:
                log("握手：等主会话 5 分钟未见执行（%s）⇒ 放行并记 NEED" % aw.get("item"))
                need_user("监督程序反馈后 5 分钟未见主会话执行：%s" % aw.get("item"))
                st.pop("notify_awaiting", None)
                aw = None
        if aw and mutate and str(aw.get("phase")) == "wait-done":
            if ms != "working":                 # **执行结束** ⇒ 放行下一条
                log("握手：主会话执行结束（%s，执行 %.0f 秒）⇒ 放行下一条"
                    % (aw.get("item"), now - float(aw.get("t_working") or now)))
                st.pop("notify_awaiting", None)
                aw = None
        if aw:
            info["awaiting"] = str(aw.get("item"))
            info["phase"] = str(aw.get("phase"))

    # ①′ 没有「等待中」才发**下一条**（单条 ＋ 握手）
    if not aw and pend:
        item = pend[0]
        kid, _, stt = item.partition("=")
        v = t.get(kid) or {}
        head = ["# 队列变化（监督程序 -> 主会话）", "",
                "本轮**只反馈这一条** —— 你处理完（我一看到你执行结束）才会发下一条：", "",
                "- **%s** -> %s%s%s" % (kid, stt,
                                       ("（%s）" % v.get("by")) if v.get("by") else "",
                                       ("　产物：%s" % v.get("artifact")) if v.get("artifact") else "")]
        if stt == "blocked":
            head += ["", "> 🔴 本条状态 = **有阻碍**：原因 —— %s"
                     % (str(v.get("block_reason") or "（未填原因，⛔ 属不合格上报）")),
                     "> ⇒ 除下面三步外，**必须明确向用户喊「需用户介入」**（谁在等、要哪一句话）。"]
        txt = "\n".join(head + [
            "", "## 主会话该做的", "1. 按产物核对是否真做完（不认自述）",
            "2. 需要就写一行排期（派活）", "3. ⛔ 不重派已在跑／已完成的件",
        ]) + "\n"
        try:
            TO_MAIN.write_text(txt, encoding="utf-8")
        except Exception:
            pass
        info["sent"] = item
        if not (deliver and mutate):         # 纯投影（`--once`／协作程序）：只写通知文件，⛔ 不消费队列
            info["deliver"] = {"skipped": "read-only"}
            return info
        _dv = _deliver_str(txt, "监督程序·单条", st)
        info["deliver"] = _dv
        # 🔴 2026-09-30 修（**静默丢件**）：⛔ **只有真投出去了才允许消费队列**。
        #    旧实现**无条件** `notified[kid] = stt` ⇒ 被 `target-busy`／`too-soon`／`locked`／
        #    `no-token` 挡下时，这一条**从此消失**（既没投到主会话、也不再重试）
        #    ＝ 又一型「程序在跑、主会话什么也没收到」。⇒ 未投出 ⇒ **队列原样保留**，下一轮再试。
        #    ⚠️ `same-item` 例外：内容哈希与上次**逐字相同** ⇒ 说明主会话本来就收到了 ⇒ 允许消费。
        if not (_dv.get("delivered") or _dv.get("skipped") == "same-item"):
            log("投递未成（%s）⇒ 保留 %s 在队首，下一轮重试"
                % (_dv.get("skipped") or "?", item))
            return info
        st["notify_awaiting"] = {"item": item, "t": now, "phase": "wait-start"}
        st["notify_pending"] = pend[1:]
        notified[kid] = stt
        st["notified"] = notified
        return info

    # ④ **心跳**（🔴 2026-09-29 用户定案 —— **三条件合取，⛔ 不是计时器**）：
    #      (a) 定期探测到主会话**未在处理**（不在 `working`）
    #    ∧ (b) 队列中**没有待反馈的任务**（无 awaiting ∧ 无 pending）
    #    ∧ (c) **需求仍未完成**（任务图里还有非 done 的节点）
    #    ⇒ 触发心跳，让主会话**核对需求推进状态**。
    #    ⚠️ 同一状态下**最多每 QUEUE_IDLE_MIN 分钟一次**：正文逐字稳定（⛔ 不带秒级时间）＋
    #       **持续时长按 30 分钟桶**写进正文 ⇒ 靠内容哈希天然限流，⛔ 不会每次探测都刷屏。
    main_busy = (_main_state(st) == "working")
    # ⚠️ 用**刚算出来的** `pend`，⛔ 不用 `st["notify_pending"]`：投影轮（mutate=False）不写 st，
    #    读 st 会拿到**陈旧值**（可能已空）⇒ 误判"无待反馈"。
    no_fb = (not aw) and (not pend)
    goal_open = goals_open()
    # 🔴🔴 2026-09-30 加第 4 个条件「**真停滞**」（**用户报"发消息卡"查出来的**）：
    #    现象：用户"发消息发不出去卡住，看上去发出去了、实际没有"。
    #    取证：① 05:20 后**全部会话** 14 条 prompt **全部 `resolveWaiter`、`park=0`** ⇒ **没丢消息**；
    #          ② 主会话 05:40 后 **`busy=true` 占 324/342（95%）**。
    #    因果：**每次"程序投递"都会唤起主会话跑一轮 ⇒ 计入 busy**；而心跳从 30 分钟改到 **5 分钟（×6）**
    #          ⇒ 主会话被唤起次数 ×6 ⇒ **用户发消息时更容易撞上 busy ⇒ 排队 ⇒ 体感"发不出去"**。
    #    根因（语义错）：心跳的触发只要求"三条件成立"，而**"目标未完成"在长任务期长期成立**
    #          ⇒ 心跳变成"**目标没完成就定期提醒**"（噪音），⛔ 而它本该表达"**异常停滞**"。
    #    ⇒ **加一条**：距**上次任何进展**（`last_progress_at`）≥ `HEARTBEAT_MIN_IDLE` 分钟才发。
    #      **有进展 ⇒ 一条都不发**；**真停滞 ⇒ 才发**（且保留 5 分钟桶）。⇒ 既快发现停滞，又不无事刷。
    _prog = float(st.get("last_progress_at") or 0)
    prog_age_min = ((now - _prog) / 60.0) if _prog else 1e9
    info["probe"] = {"main_busy": main_busy, "no_feedback": no_fb, "goal_open": goal_open,
                     "prog_age_min": round(prog_age_min, 1)}
    if (not main_busy) and no_fb and goal_open and prog_age_min >= HEARTBEAT_MIN_IDLE:
        hb_since = float(st.get("hb_since") or 0) or now
        if mutate:
            st["hb_since"] = hb_since
        held = (now - hb_since) / 60.0
        bucket = int(held // float(QUEUE_IDLE_MIN)) * int(QUEUE_IDLE_MIN)
        open_ids = [k for k, v in sorted(t.items()) if (v or {}).get("state") != "done"]
        info["heartbeat"] = {"held_min": round(held, 1), "bucket": bucket}
        txt = "\n".join(
            ["# 心跳：请核对需求推进状态（监督程序 -> 主会话）", "",
             goal_line(), "",
             "**触发条件同时成立**：主会话未在处理 · 队列无待反馈 · **需求仍未完成**"
             "（已持续 %d 分钟）。" % bucket, "",
             "## 请你做的",
             "1. 对着任务图核对：哪些还没做、哪些卡住了、下一步该派谁",
             "2. 需要用户拍板 -> 明确喊「需用户介入」", "",
             # 🔴 2026-09-30 加（用户「加快效率」）：**把"还差什么"直接写进心跳正文**，
             #    省掉主会话每轮自己去翻目标文件 —— 心跳一到手就知道该干什么。
             "## 目标验收：还差这些（非 pass 的项）"] + _acc_short() + [
             "", "## 队列现状（未完结 %d）" % len(open_ids)]
            + (["- **%s**：%s" % (k, (t[k] or {}).get("state")) for k in open_ids]
               or ["- （队列为空 —— 待办在任务图/旧队列里，尚未并入）"])
        ) + "\n"
        try:
            TO_MAIN.write_text(txt, encoding="utf-8")
        except Exception:
            pass
        info["notice"] = "心跳"
        # 🔴 2026-09-29 修（实测：18 秒内两次同 hash 投递 ⇒ 去重失效）：
        #    根因＝**两个进程并发读写同一个 state 文件、互相覆盖**（协作程序 one_round 与监督程序
        #    的 `--supervise` 循环都会投）。⇒ **投递只由专用监督进程做**（架构上"投递"本就是它的职责），
        #    另一处调用传 `deliver=False`（仍写通知文件，⛔ 不投）。
        if deliver and mutate:
            info["deliver"] = _deliver_str(txt, "监督程序·心跳", st)
        else:
            info["deliver"] = {"skipped": "read-only" if not deliver else "not-deliverer"}
    else:
        if mutate:
            st.pop("hb_since", None)
        if not aw:                 # ⚠️ 正在等主会话处理（aw 在身）时**别删**刚投出去的通知文件
            try:
                TO_MAIN.unlink()
            except Exception:
                pass
    return info


# ── 视图 ────────────────────────────────────────────────────
def render(d: dict, V: dict, H: dict, T: dict, tk: list) -> None:
    Q_ = queue_view(T, d)       # 🔴 严格队列（一次一件 · 原子取件）
    sh = []
    for s in d["sessions"]:
        a = s["age"]
        sh.append("- `%s` %-36s %s · %s · %s" % (
            s["id"], s["name"], "🟢 活跃" if a < 5 else ("🟡 可能停了" if a < 30 else "⚪ 闲"),
            ("%.0f 分钟前" % a) if a < 1e8 else "—", s["tag"]))
    md = "\n".join([
        "# 协作实时状态（collabd · 每轮覆写 · 无需任何会话在跑）", "",
        "- 生成于：**%s**（每 %ss）" % (time.strftime("%Y-%m-%d %H:%M:%S"), C["interval"]),
        "- 📥 反馈新增 **%d** 条（水位 →%s）" % (len(V["fresh"]), V["wm"]),
        "- 🔎 证据分级：真成果 **%d**｜新排期 **%d**（⛔非成果）｜在跑 **%d**" % (len(V["done"]), len(V["newp"]), len(V["cur"])),
        "- 🚀 推进判定：%s" % V["verdict"],
        "- 🕳 真空：%s" % ("🔴 **≥%.0f 分钟**（未完成 ∧ 无人执行）⇒ 需主会话接管" % V["vdur"]
                          if (V["vacuum"] and V["vdur"] >= float(C["vacuum_min"]))
                          else ("⚠️ 真空中（%.0f 分钟）" % V["vdur"] if V["vacuum"] else "✅ 无真空")),
        "- 🩺 体检：%s" % ("⏸ 跳过（忙）" if H["skipped"] else ("✅ 通过" if not H["issues"] else "⚠️ %d 项" % len(H["issues"]))),
        "- %s 告警：%s" % ("🔴" if V["alert"] else "✅", V["alert"] or "无"),
        "- 进程 pid **%d**（单例 :%s）· 零令牌 · 只读 · ⛔ 不派活" % (os.getpid(), C["singleton_port"]), "",
        "## 🎯 服务探针", "- %s 端口 `%s`" % ("✅" if V["up"] else "⛔", C["shim_port"] or "（未配置）"), "",
        "## 🧭 任务图（可派 / 等待 · **防干等**）",
        ("- 🔴 **可派未派（浪费）** ⇒ 立即派：" + "；".join("%s(%s)" % (r[0], r[2]) for r in T["ready"]))
        if T.get("waste") else ("- ⏳ 可派：" + "；".join("%s(%s)" % (r[0], r[2]) for r in T["ready"])
                                if T.get("ready") else "- ✅ 无可派"),
        *["- 🚧 等待：%s（等 %s）" % (w[0], ",".join(w[3] or [])) for w in (T.get("waiting") or [])[:6]],
        ("- 🎯 关键路径：%s" % " → ".join(T["critical"])) if T.get("critical") else "", "",
        "## 📋 严格队列（**一次一件** · 原子取件 · 同线互斥）",
        "- **队首（下一个该做）**：%s" % ("**%s** %s（线：%s，%s）" % (
            Q_["head"]["id"], Q_["head"]["title"], Q_["head"]["line"],
            "关键路径上游" if Q_["head"]["prio"] == 0 else "非关键路径") if Q_.get("head") else "（无可派）"),
        "- **正在做**：%s" % ("、".join("%s→%s" % (k, v) for k, v in (Q_.get("doing") or {}).items()) or "（无）"),
        "- **待办**：%d 件%s" % (Q_.get("pending", 0),
                                 "　⚠️ **同时在做多件（可能打架）**" if Q_.get("conflict") else ""),
        "- 规矩：只取队首；`mkdir claims/<id>` 原子取件；**做完删 claim 出队**；卡死 %d 分钟自动回退。"
        % (DOING_TTL // 60), "",
        "## ✅ 真成果", *(["- `[%s]` %s · %s" % (r["status"], r["aid"], r["title"][:140] or "（空）") for r in V["done"][:5]] or ["- （无）"]), "",
        "## 🟡 接续任务（⛔ 只是计划）", *(["- %s · %s" % (str(r[0])[:8], (r[1] or "")[:46]) for r in V["newp"][:6]] or ["- （无）"]), "",
        "## 🖥 会话状态", ("  **持锁**：" + "、".join(h[:30] for h in d["locks"])) if d["locks"] else "  **无锁**", "",
        *(sh or ["- （无）"]), "",
        "## 🧭 靶点", *(tk or ["- （未配置）"]), "",
        "## 🫀 在跑的棒（口径 = `sessions.status='working'` · ⛔ 不含本会话）",
        *(["- `%s` %s" % (w["id"][:8], w["name"]) for w in d["working"]]
          or ["- （无 —— 没有任何会话在做正事%s）"
              % ("；⚠️ 只有本会话（观察者）在跑" if d.get("self_working") else "")]), "",
        "> 本程序承担「派活之外的全部功能」；**⛔ 不派活**（派活须由会话做）。", "",
    ])
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    LIVE.write_text(md, encoding="utf-8")
    (INBOX / "digest.md").write_text(
        "# 机械摘要\n\n- %s\n- %s\n- 可派：%s\n- 等待：%s\n- 关键路径：%s\n"
        % (V["verdict"], goal_line(),
           "；".join("%s(%s)" % (r[0], r[2]) for r in T.get("ready", [])) or "无",
           "；".join("%s" % w[0] for w in T.get("waiting", [])) or "无",
           " → ".join(T.get("critical", [])) or "（未配置）"), encoding="utf-8")


def signals(V: dict, T: dict) -> None:
    def w(p, txt):
        try:
            p.write_text(txt, encoding="utf-8")
        except Exception:
            pass

    def rm(p):
        try:
            p.unlink()
        except Exception:
            pass

    if V["alert"]:
        w(STALL, "# ⚠️ %s\n\n- %s\n- 详见实时状态\n\n## 该谁做（本程序⛔ 做不到）\n"
                 "主会话抢**细粒度域锁** → 读摘要 → 按缺口**派下一棒**（属白名单「派活」）\n"
                 % (V["alert"], time.strftime("%Y-%m-%d %H:%M:%S")))
    else:
        rm(STALL)
    if V["vacuum"] and V["vdur"] >= float(C["vacuum_min"]):
        w(VACUUM, "# 🔴 真空：需求未完成 ∧ 没有会话在执行\n\n- 已持续 **%.0f 分钟**\n\n"
                  "## 主会话接管\n1. 抢域锁\n2. 读摘要/任务图 ⇒ 判缺口\n3. **派下一棒** 或自己做最靠前那步\n4. 记忆 → 释锁 → 停\n"
                  % V["vdur"])
    else:
        rm(VACUUM)
    if T.get("waste"):
        w(READY, "# 🔴 可派未派（浪费）：有能干的活，但没有任何棒在跑\n\n%s\n\n"
                 "## 怎么做\n按任务图**优先关键路径**派活；抢**细粒度域锁**（⛔ 禁用整工作区粗域）\n"
                 % "\n".join("- **%s** %s（线：%s）" % (r[0], r[1], r[2]) for r in T["ready"]))
    else:
        rm(READY)


def one_round(st: dict) -> dict:
    d = fetch()
    up = probe_port()
    V = verdicts(d, up, st)
    T = taskgraph(V["cur"], V["busy"])
    H = health(d, V)
    if not H["skipped"] and time.time() - float(st.get("health_at") or 0) > float(C["health_every"]):
        st["health_at"] = time.time()
        try:
            HEALTH.write_text("# 机制体检（空闲时执行）\n\n- %s\n\n## 结果：%s\n%s\n"
                              % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "✅ 通过" if not H["issues"] else "⚠️ %d 项" % len(H["issues"]),
                                 "\n".join("- %s" % i for i in H["issues"]) or "- 无异常"), encoding="utf-8")
        except Exception:
            pass
    tk_ok, tk = targets()
    render(d, V, H, T, tk)
    # 🔧 **兼容别名**：旧引用（`CODEBUDDY.md §1.5 D⑤`／SOP）读 `advance.md`。由**同一份内容**产出，
    #    ⛔ 不是第二真相（同源同内容，仅供旧读者过渡）。
    try:
        (INBOX / "advance.md").write_text(
            "# 机械推进快照（与 digest.md 同源）\n\n"
            + (INBOX / "digest.md").read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    signals(V, T)
    # ⑪ 读队列/写通知 —— 🔴 **纯投影**（`mutate=False`）：⛔ 绝不推进队列状态。
    #    **投递 ＋ 推进队列 = 「监督程序」的唯一职责**（`--tick`，由宿主钩子唤起）。
    #    ⚠️ 2026-09-30 修：旧版这里 `deliver=False` 但**仍然消费** `notify_pending`/`notified`
    #    ⇒ 钩子先跑 `--once`（节流 3 分钟，谁发话都会跑）**把待反馈项吃掉却不投递**，
    #    紧接着的 `--tick` 便看到空队列 ⇒ **通知永远发不出去**（＝"程序在跑、主会话没收到"）。
    st["queue_info"] = supervise(st, deliver=False, mutate=False)
    # 🔴 2026-09-29 职责纠正（用户定案："协作程序怎么会投递呢，应该是只维护协作队列，让监督程序来读"）：
    #    **投递（通知主会话）＝「监督程序」的职责**，⛔ 不是协作程序的。⇒ 这里**不再调用 `wake_round()`**
    #    （它与 `--supervise` 里的 `_deliver_str` 各判各的重 ⇒ 正是"同一内容成对重复投递"的真身）。
    #    ⇒ 协作程序只做：读库／判定／信号文件／看板／台账投影。
    #    ⚠️ 副作用提醒：**只起协作程序、不起监督程序 ⇒ 没人投递**（所以守护必须把两个都拉起来）。
    wi = st.get("wake_info") or {}          # 观测项保留旧值（投递台账现在只由监督程序写）
    st.update({"rowid": V["wm"], "aids": V["cur"], "last_progress_at": V["pa"],
               "vacuum_since": V["vs"], "up": up, "wake_info": wi})
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return st


def main() -> int:
    # 🔴 未知参数 ⇒ **拒绝并退出**（⛔ 不许落进常驻模式）——
    #    实测踩过：误传一个未处理的参数（`--reqs`）会**静默变成常驻进程**，很难发现。
    _KNOWN = {"--where", "--once", "--supervise", "--tick", "--ready-next", "--report", "--reqs", "--declare",
              "--state", "--by", "--artifact", "--reason", "--line", "--role", "--name", "--topic", "--reconcile"}
    _bad = [a for a in sys.argv[1:] if a.startswith("--") and a not in _KNOWN]
    if _bad:
        print("未知参数：%s\n用法：collabd.py [--once | --tick | --supervise | --reqs | "
              "--report <id> --state pending|running|done|blocked [--by ...] [--artifact ...] [--reason ...] | --where]"
              "\n⚠️ ⛔ 不带参数 ＝ 常驻模式（应由守护程序拉起）" % " ".join(_bad))
        return 2
    if "--where" in sys.argv:
        print("collabd.py =", Path(__file__).resolve())
        print("workspace =", WS, "\nLIVE =", LIVE, "\nTG =", TG, "\nINBOX =", INBOX)
        print("配置来源 =", CFG_USED or "⚠️ 未找到（在用 DEFAULTS）")
        return 0
    # 🔴🔴 **找不到使用方的部署配置 ⇒ 拒跑（fail-closed）**
    #    为什么必须拒：不配配置时 `workspace` 会回落到 **cwd**，而 cwd 常常就是**技能目录**
    #    ⇒ 会在技能里长出 `tmp/supervise-inbox/`、`_collabd.log` 等**使用方的产物**
    #    （2026-09-30 实测：跑一次 `--where` 就在技能里生成了 `tmp/supervise-inbox/_collabd.log`）。
    #    ⇒ 宁可**什么都不做并说明原因**，⛔ 也不把技能目录当使用方用。
    if CFG_MISSING:
        print("⛔ 未找到部署配置，**拒跑**（避免把技能目录当工作区用、在里面长出运行产物）。\n"
              "   查找顺序：① 环境变量 `COLLABD_CONFIG` ② `<COLLABD_WORKSPACE 或 cwd>"
              "/.workbuddy/collab/collabd.config.json`。\n"
              "   使用方请把配置放在 ② 那个位置（或由钩子／启动器传 ①）。\n"
              "   只想看路径 ⇒ 用 `--where`。")
        return 2
    INBOX.mkdir(parents=True, exist_ok=True)
    st = {}
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    if "--report" in sys.argv:             # 协作会话**上报**状态（执行中／执行完毕）
        def _arg(k, dv=""):
            return sys.argv[sys.argv.index(k) + 1] if (k in sys.argv and sys.argv.index(k) + 1 < len(sys.argv)) else dv

        rc = task_report(_arg("--report"), _arg("--state", "running"), _arg("--by"),
                         _arg("--artifact"), _arg("--reason"), _arg("--line"))
        if rc == 0:                        # 上报**只写台账**（⛔ 不投递）——
            # 🔴 2026-09-29 职责纠正：投递归监督程序；协作程序上报后**只是把状态落盘**，
            #    由**常驻的监督程序**在它自己的轮次里读到变化并投递（这样才有"单条＋握手"的顺序）。
            try:
                STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return rc
    if "--declare" in sys.argv:            # 会话**声明自己的角色**（sid 自动从环境取，⛔ 不用手填）
        def _d(k, dv=""):
            return sys.argv[sys.argv.index(k) + 1] if (k in sys.argv and sys.argv.index(k) + 1 < len(sys.argv)) else dv

        _role = _d("--role", "worker").strip().lower()
        if _role not in ("main", "worker"):
            print("--role 只能是 main / worker")
            return 2
        _sid = os.environ.get("CODEBUDDY_SESSION_ID") or ""
        _key = _sid or ("name:" + _d("--name", "?"))
        st["roles"] = dict(st.get("roles") or {})
        st["roles"][_key] = _role
        try:
            STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        _topic = _d("--topic", "").strip()
        if _topic:
            st["topics"] = dict(st.get("topics") or {})
            st["topics"][_key] = _topic
        print("OK 已声明角色：%s = %s%s%s" % (_key[:8], _role,
                                            ("，主题 = %s" % _topic) if _topic else "（⚠️ 未给 --topic ⇒ 二级前缀缺失）",
                                            "" if _sid else "（⛔ 环境里取不到会话 id ⇒ 用名字作键）"))
        return 0
    if "--ready-next" in sys.argv:         # 🔴 收尾确认（替代"盲等 5~8 分钟"）
        _r = ready_next(st)
        if _r["ok"]:
            print("✅ 可以排下一棒（上一棒已收尾）⇒ 排期建议 = 现在 + 30~60 秒")
        else:
            print("⛔ 先别排，原因：")
            for w in _r["why"]:
                print("   - %s" % w)
        return 0
    if "--reconcile" in sys.argv:
        _r = reconcile(st)
        print("OK 启动对账：收编 %d 条（%s）；僵尸 %d 条（%s）"
              % (len(_r["adopted"]), "、".join(_r["adopted"]) or "-",
                 len(_r["zombie"]), "、".join(_r["zombie"]) or "-"))
        if _r.get("skip"):
            print("   WARN: " + _r["skip"])
        return 0
    if "--reqs" in sys.argv:               # 打印**需求台账**（四态；协作程序持有）
        _t = _load_tasks()
        _n = {"pending": "待执行", "running": "执行中", "done": "已完成", "blocked": "有阻碍"}
        if not _t:
            print("（需求台账为空 —— 待办可能还在任务图里，尚未并入）")
        for _k, _v in sorted(_t.items()):
            _s = str((_v or {}).get("state") or "?")
            print("- %-16s %-6s %s%s%s" % (_k, _n.get(_s, _s),
                                           ("线 %s｜" % _v.get("line")) if _v.get("line") else "",
                                           ("执行者 %s " % _v.get("by")) if _v.get("by") else "",
                                           ("｜阻碍：%s" % _v.get("block_reason")) if _v.get("block_reason") else ""))
        return 0
    if "--supervise" in sys.argv:          # 监督程序**常驻**模式（由守护程序看护；⛔ 不派活）
        log("supervise loop start pid=%d" % os.getpid())
        while True:
            if _guard_says_stop():             # 守护停了 ⇒ 优雅退出
                log("守护已停 ⇒ 监督程序优雅退出")
                return 0
            try:
                st["queue_info"] = supervise(st)
                STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                log("supervise loop err %s" % e)
            time.sleep(float(C.get("supervise_interval") or 30))
    if "--tick" in sys.argv:               # 监督程序 · **宿主钩子唤起的一次性投递轮**
        # 🔴 2026-09-30 立（用户口径：「又给我整到自动任务去了」）——
        #    **投递 ⛔ 不靠自动任务排期、⛔ 不靠常驻进程**：由**宿主钩子**唤起本程序跑一轮。
        #    依据（源码 ＋ 本机实测，⛔ 非推断）：
        #      · 钩子是**宿主起的子进程** ⇒ ① 继承到网关口令 `CODEBUDDY_GATEWAY_PASSWORD`
        #        （2026-09-30 实测：本机会话内进程 `len=43` 有值）② ⛔ 不占任何会话 ③ 零 token；
        #      · `UserPromptSubmit` 两日 26 次 spawn 实测**会被投递**（见 pitfalls §钩子）。
        #      · ⇒ **监督程序 ＝ 被钩子唤起的一次性进程**（`--tick`），⛔ 不需要常驻、⛔ 不需要排期。
        #    **覆盖度（为什么这样就够）**：需要投递的时刻**全都是由某个会话在动产生的** ——
        #      协作会话上报＝一次 Bash 调用、主会话处理完＝一次工具调用 ⇒ 那一刻钩子必然响 ⇒ **自洽**。
        #      ⚠️ 唯一缺口＝「谁都没动」的**停滞心跳**（需要时钟）⇒ 由会话外守护**只落标记**
        #      （`STALL.md`），等下一次任意钩子触发时**补投**。⇒ ⛔ 常驻进程不再是投递的前置。
        #    ⚠️ 本路径**必须**在宿主树内被调起：否则拿不到口令 ⇒ `_deliver_str` 会落 `NEED-USER.md`
        #      （设计如此，⛔ 不静默失败）。手工在会话外跑 `--tick` 会看到 `no-token`，属预期。
        global FROM_HOOK
        FROM_HOOK = True                  # 本进程＝宿主钩子唤起 ⇒ 没口令就属**异常**（见 `_deliver_str`）
        try:
            st["queue_info"] = supervise(st, deliver=True)
            # ① 回查"上次投递有没有被消费"（投出去 ≠ 它跑起来了）
            _cc = check_delivery_consumed(st)
            # ② 宿主侧「消息卡住」指纹探针（⛔ AI 侧修不了，只能发现 + 告诉用户点哪一下；节流 10 分钟）
            _pk = {"n": 0, "last": ""}
            try:
                _pk = probe_host_park()
                _fresh = False
                if _pk["n"] and _pk["last"]:
                    try:
                        hh, mm, ss = [int(x) for x in _pk["last"].split(":")]
                        _fresh = ((time.time() - (hh * 3600 + mm * 60 + ss)) % 86400) < PARK_FRESH
                    except Exception:
                        _fresh = False
                _due = True
                try:
                    if PARK_STAMP.exists() and (time.time() - PARK_STAMP.stat().st_mtime) < PARK_GAP:
                        _due = False
                except Exception:
                    pass
                if _fresh and _due:
                    need_user(
                        "检测到**宿主侧「消息卡住」指纹**（%s，最近一次 %s）：`parkInQueue` ＋ `hasWaiter=false`"
                        " ⇒ 消息进了队列、**没有消费者**。\n🔴 **AI 侧无法自救**。请你**把那个会话窗口切走再切回**"
                        "（或关掉重开）—— 队列随即排空。⛔ 别反复发消息试探。" % (_pk["file"], _pk["last"]))
                    PARK_STAMP.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            except Exception as e:
                log("park 探针失败（已忽略）%s" % e)
            STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            _qi = st.get("queue_info") or {}
            _de = _qi.get("deliver") or {}
            _dd = _de.get("delivered") or {}
            print("tick: item=%s awaiting=%s phase=%s deliver=%s consume=%s park=%s"
                  % (_qi.get("sent") or _qi.get("notice") or "-",
                     _qi.get("awaiting") or "-", _qi.get("phase") or "-",
                     ("http=%s" % _dd.get("http")) if _dd else (_de.get("skipped") or "-"),
                     ("%s@%s" % (list(_cc.keys())[0], _cc.get("sid"))) if _cc else "-",
                     ("%d 次 最近 %s（%s）" % (_pk["n"], _pk["last"], _pk["file"])) if _pk["n"] else "-"))
        except Exception as e:
            log("tick err %s" % e)
            return 1
        return 0
    if "--once" in sys.argv:
        one_round(st)
        return 0
    try:                                   # 单例
        g = socket.socket()
        g.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        g.bind(("127.0.0.1", int(C["singleton_port"])))
        g.listen(1)
    except Exception as e:
        log("singleton bind failed (%s) => another instance runs" % e)
        return 0
    log("collabd start pid=%d ws=%s" % (os.getpid(), WS))
    n = 0
    while True:
        if _guard_says_stop():                 # 守护停了 ⇒ 优雅退出（⛔ 不硬杀、不成孤儿）
            log("守护已停 ⇒ 协作程序优雅退出")
            break
        n += 1
        try:
            st = one_round(st)
            # ⛔ **业务自愈已从协作机制移出**（2026-09-29 用户定案：「协作机制就是协作机制，
            #    手机控制 workbuddy 是另一回事」）⇒ 本程序只做 队列／判定／通知，⛔ 不探业务端口、
            #    ⛔ 不拉业务客户端。若某条业务线需要自愈 ⇒ 由**该线自己的件**做。

        except Exception as e:
            log("round %d err %s" % (n, e))
        if n % 30 == 0:
            log("alive rounds=%d" % n)
        time.sleep(float(C["interval"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log("fatal %s" % e)
        sys.exit(1)          # 🔴 不再 exit 0：**rc=0 必须是真成功**（今天栽过一次"假绿"——
                             #    异常被吞、rc=0、而产物根本没刷新，全链路静默失灵）
