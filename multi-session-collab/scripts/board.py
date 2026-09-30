#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""board.py —— 生成**看板快照** `board.json`（供 `assets/board.html` 实时渲染）。

设计原则：
 · **只读**：只读台账/状态/投递台账/宿主库（三张只读表）。（组件探测属**使用方**，见 board_ext）⛔ 不写任何账本。
 · **零依赖**：只用标准库。⛔ 不引第三方。
 · **快**：一轮 < 0.1 s（socket 探测 0.3 s 超时上限；宿主库只读一条 SQL）。
🔴 **总则：看板不能影响程序执行**（用户 2026-09-30 明令）。三条落地：
  ① **解耦**：协作程序/守护程序**一行都不引用本文件**（已核）；本文件**从不写任何账本**。
  ② **异步**：`--serve` 由**后台线程**按 `--interval` 秒产快照，**请求线程只吐内存缓存**
     ⇒ 请求路径 ⛔ 不碰 DB／⛔ 不读文件 ⇒ 开多少标签页都不增加宿主负载。
  ③ **降级不静默**：任何一块读不到 ⇒ 记进 `warn` 并在界面显示，⛔ 不伪装成"0 个会话"。

用法：
  python board.py                  # 产一次 board.json（默认写到 inbox/board.json）
  python board.py --out <路径>
  python board.py --serve [端口] [--interval 秒]   # 起本地只读看板（默认 8788 / 3 秒）
"""
from __future__ import annotations

import glob
import json
import os
import re
import socket
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG_P = Path(os.environ.get("COLLABD_CONFIG") or (HERE / "collabd.config.json"))
WS = Path(os.environ.get("COLLABD_WS") or "").resolve() if os.environ.get("COLLABD_WS") else None


def _cfg() -> dict:
    try:
        return json.loads(CFG_P.read_text(encoding="utf-8"))
    except Exception:
        return {}


C = _cfg()
if WS is None:
    WS = Path(str(C.get("workspace") or "")).resolve()
INBOX = WS / str(C.get("inbox") or "tmp/supervise-inbox")


def _j(p: Path, dv=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return dv


def _tail_jsonl(p: Path, n: int = 10) -> list:
    try:
        with open(p, "rb") as f:
            f.seek(max(0, os.path.getsize(p) - 65536))
            ls = f.read().decode("utf-8", "replace").strip().splitlines()
        out = []
        for ln in ls[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out
    except Exception:
        return []


def _host_db() -> Path | None:
    p = str(C.get("host_db") or "")
    if p and os.path.isfile(p):
        return Path(p)
    d = os.environ.get("CODEBUDDY_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".workbuddy")
    q = Path(d) / "workbuddy.db"
    return q if q.is_file() else None


def _main_sid(st: dict) -> str:
    """主会话 sid。🔴 **与 `collabd.py::_main_sid()` 必须逐字同款**（判据只此一处权威：
    ① 声明为 `main` 的角色 ② 退回最近一次投递到的会话 ③ 都取不到 ⇒ 空串，⛔ 不猜）。"""
    for _sid, _r in (st.get("roles") or {}).items():
        if str(_r) == "main" and str(_sid).startswith(tuple("0123456789abcdef")):
            return str(_sid)
    return str(((st.get("wake") or {}).get("sessionId")) or "")


def project_scope() -> dict:
    """🔴 **本需求目标项目的身份 ＋ 归属判据**（用户 2026-09-30 明示两件事：
      ① 「**要明确哪些会话是属于某个需求目标项目的**」
      ② 「**这个看板上应该明确显示是哪个需求目标项目**」）。

    🔴 **判据与 `collabd.py::_in_project()` 同款** ⇒ 看板列的"在跑会话" ≡ `--ready-next` 认的"在跑会话"
       （⛔ 否则两边各说各话：看板显示没人跑、收尾确认却被拦）。
    ⛔ **刻意不用 cwd 推断** —— 架构 §2.3 明令「绝不回落到 cwd 推断」。
    """
    g = _goal()
    st = _j(INBOX / "collabd-state.json", {}) or {}
    msid = _main_sid(st)
    short = str(g.get("short") or "")
    gid = str(g.get("id") or "")
    crit = []                                          # 给人读的判据清单（看板上原样展示）
    if msid:
        crit.append("主会话 %s" % msid[:8])
    if short:
        crit.append("标题含 [%s]" % short)
    if gid:
        crit.append("显式声明 --declare --goal %s" % gid)
    return {"goal_id": gid, "short": short, "title": str(g.get("title") or ""),
            "main_sid": msid, "workspace": str(WS), "criteria": crit}


def in_project(sid: str, title: str, sc: dict) -> bool:
    """🔴 **一个会话是否属于本项目**。⛔ **与 `collabd.py::_in_project()` 同款 —— 改一处必须改两处**。"""
    if sc["main_sid"] and sid == sc["main_sid"]:
        return True
    return bool(sc["short"]) and ("[%s]" % sc["short"]) in str(title or "")


def _goal() -> dict:
    return _j(INBOX / "goal.json", {}) or {}


def _labor(tasks: dict, sess: list, goal: dict) -> list:
    """🔴 **分工板**（用户 2026-09-30 两条明示：
      ① 「协作会话**不是历史记录**，是**展示分工**的板块」
      ② 「每个分工板块可以**展示最近的协作任务**」）。

    **分工位 = 线**（`goal.lines` ∪ 台账里出现过的线）—— 线是本机制里真实存在的分工维度。
    每格给四样：**最近的协作任务**／**当前承接会话**／**件汇总**／**状态色**。

    🔴 **两类"归属"别混（2026-09-30 修 · 用户报「桌面线有会话在跑、看板却说无协作任务」）**：
      · **会话 → 本项目**：按 §2.3 的三级判据（主会话 sid ／ 标题含 `[主题]` ／ `--declare`），
        ⛔ **绝不回落到 cwd**。这个判据在 `project_scope()` 里，本函数**收进来的 sess 已经过它筛**。
      · **会话 → 哪条线**：**线就是用工作区定义的**（`goal.lines` 里就是工作区名）⇒
        用会话 `cwd_tail ∈ lines` 判**线归属**是自然的，⚠️ 与上面那条不是一回事。
        ⇒ 这里**允许**用 cwd 判线，但**只用于"归到哪条线"**。

    🔴 **修掉的两种误报**（旧版只看台账里的件 ⇒ 会漏）：
      ① 「（该线暂无协作任务）」：台账没件 **≠** 该线没活 —— 该线正在跑棒时这话是错的。
         ⇒ 台账无件时，**回落到"该线最近的一个会话"**当"最近的协作任务"。
      ② 「当前无会话在跑」：旧版只认"在跑会话标题里出现**台账件 id**" ⇒
         棒在做台账里还没有的件（如 V6）时**匹配不到** ⇒ 明明在跑却说没人跑。
         ⇒ 承接判据改成：**cwd_tail == 线** ∨ 标题命中该线的台账件。
    🔴 **状态色三值**：`busy`＝该线有会话在跑｜`gap`＝**该线还有未完成的件却没人在跑**｜`idle`＝没活也没人跑。
    """
    lines: list = []
    for _ln in (goal.get("lines") or []):
        _ln = str(_ln or "")
        if _ln and _ln not in lines:
            lines.append(_ln)
    for _v in (tasks or {}).values():
        _ln = str((_v or {}).get("line") or "")
        if _ln and _ln not in lines:
            lines.append(_ln)
    if not lines:
        lines = ["（未标注线）"]

    _running = [s for s in (sess or []) if str(s.get("status")) == "working"]

    def _item_in_title(title: str) -> str:
        for _tid in sorted((tasks or {}).keys(), key=len, reverse=True):
            if _tid and re.search(r"(?<![0-9A-Za-z])%s(?![0-9A-Za-z])" % re.escape(str(_tid)),
                                  str(title or "")):
                return str(_tid)
        return ""

    _now = time.time()
    out = []
    for ln in lines:
        items = {k: (v or {}) for k, v in (tasks or {}).items()
                 if str((v or {}).get("line") or "") == ln}
        done = [k for k, v in items.items() if str(v.get("state")) == "done"]
        n_open = len(items) - len(done)
        _latest = None
        for k, v in items.items():
            _ts = float(v.get("t_end") or v.get("t") or 0)
            if _latest is None or _ts > _latest[0]:
                _latest = (_ts, k, v)
        # 承接会话：① 会话就在这条线的工作区里（cwd_tail == 线） ② 或标题命中该线的台账件
        holders = []
        for s in _running:
            _ct = str(s.get("cwd_tail") or "")
            _tid = _item_in_title(s.get("title"))
            if _ct == ln or (_tid and _tid in items):
                holders.append({"id8": str(s.get("id8") or ""),
                                "what": (_tid or str(s.get("title") or ""))[:26]})

        # 最近的协作任务：台账有件 ⇒ 用最新那件；**台账无件 ⇒ 回落到该线最近的会话**
        #   （⛔ 别因为"台账没件"就说"暂无协作任务" —— 该线可能正在跑一件还没上报的活）
        latest = None
        if _latest:
            latest = {"item": _latest[1], "state": str(_latest[2].get("state") or "?"),
                      "by": str(_latest[2].get("by") or ""),
                      "age_min": round((_now - _latest[0]) / 60.0, 1)}
        else:
            _cand = [s for s in (sess or []) if str(s.get("cwd_tail") or "") == ln
                     and str(s.get("role")) != "主会话"]
            _cand.sort(key=lambda x: float(x.get("age_min") or 1e9))
            if _cand:
                _s0 = _cand[0]
                latest = {"item": str(_s0.get("title") or "")[:28],
                          "state": ("working" if str(_s0.get("status")) == "working"
                                    else str(_s0.get("status") or "?")),
                          "by": str(_s0.get("id8") or ""),
                          "age_min": float(_s0.get("age_min") or 0), "from_session": True}

        out.append({
            "line": ln,
            "name": str((C.get("lines") or {}).get(ln) or ln),
            "items": {k: str(items[k].get("state") or "?") for k in sorted(items)},
            "done": len(done), "total": len(items), "open": n_open,
            "latest": latest,
            "running": holders,
            "state": ("busy" if holders else ("gap" if n_open else "idle")),
        })
    return out


def _sessions(limit: int = 8) -> dict:
    """返回 `{"mine": [...], "others_running": n, "err": ""}` —— **只把本项目的会话列进看板**。
    `mine` 里**主会话排最前**（看板第一眼要能看到"哪个是主会话"）。

    🔴 **不许拖慢宿主**（用户 2026-09-30：「看板不能影响程序执行」）：
      · 只读连接（`mode=ro`）⇒ WAL 下**读者不阻塞写者**，宿主的写事务该多快还是多快
      · `busy_timeout=300` ⇒ 万一撞上写锁，**0.3 秒就放弃**，⛔ 不排队、⛔ 不长时间占着
      · 失败 ⇒ 返回 `err` 让界面显示「宿主库暂不可读」，⛔ **不静默伪装成"0 个会话"**（那是假情报）
    """
    db = _host_db()
    if not db:
        return {"mine": [], "others_running": 0, "err": ""}
    sc = project_scope()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True, timeout=0.3)
        con.row_factory = sqlite3.Row
        try:
            con.execute("pragma busy_timeout=300")
        except Exception:
            pass
        rows = con.execute(
            "select id,title,custom_title,status,cwd,updated_at,last_activity_at "
            "from sessions order by updated_at desc limit 60").fetchall()
        con.close()
        now = time.time()
        main, work, others = [], [], 0
        for r in rows:
            title = str(r["custom_title"] or "") or str(r["title"] or "")
            cwd = str(r["cwd"] or "")
            st = str(r["status"] or "")
            sid = str(r["id"] or "")
            if not in_project(sid, title, sc):
                if st == "working":
                    others += 1                      # ⚠️ 别的线在跑 ⇒ **只计数**，⛔ 不混进列表
                continue
            rec = {
                "id8": sid[:8],
                "title": title[:60],
                "status": st,
                "role": "主会话" if (sc["main_sid"] and sid == sc["main_sid"]) else "协作会话",
                "cwd_tail": cwd.replace("\\", "/").rstrip("/").split("/")[-1],
                "age_min": round((now - float(r["updated_at"] or 0) / 1000.0) / 60.0, 1),
            }
            (main if rec["role"] == "主会话" else work).append(rec)
        mine = (main + work)[:limit]
        return {"mine": mine, "others_running": others, "err": ""}
    except Exception as e:
        return {"mine": [], "others_running": 0, "err": "宿主库暂不可读：%s" % e}


# ══════════════════════════════════════════════════════════════════════════════
# 🔴 职责边界（用户 2026-09-30 定则）：
#   「**技能就是技能 程序就是程序，谁用产生的文件 放在他自己那里**」
#   ＋「**禁止用抽象词，用 系统-模块-功能名**」
#
# ⇒ **本文件（技能侧）⛔ 不含任何项目的路径、端口、真名、历史**。
#   凡是"某个项目要看哪些组件、它们叫什么真名、端到端怎么算通过"这类**项目知识**，
#   一律由**使用方自己的扩展文件**提供，本文件只负责**把它读进来并渲染**：
#
#     配置项 `board_ext`（工作区相对路径，默认 `.workbuddy/collab/board_ext.py`）
#     ⇒ 那个文件暴露 `build(ws: str) -> dict`（契约见其文件头）
#
#   ⛔ 没配 / 读不到 / 抛异常 ⇒ **降级**（这一块不显示，看板照常）＋ 记进 `warn`，
#      ⛔ 绝不让使用方的脚本把看板搞挂。
# ══════════════════════════════════════════════════════════════════════════════

EXT_CACHE: dict = {}          # ⚠️ **只缓存"模块"，⛔ 绝不缓存"结果"**（见下）


def _ext(ws: Path, warn: list) -> dict:
    """🔴 读**使用方自己的**看板扩展（⛔ 技能侧不含任何项目知识）。

    契约：`build(ws) -> {"title","tag","chips":[{label,up,ok,bad,tip}],"paragraphs":[{text,tone}],"tip",
                        "triggers":[{name,detail,detail2,label,up,edge,tip}]}`。

    ⚠️ `triggers` 为**可选**：表示**外部触发源（时间驱动）** ⇒ 画进架构图 R5 行右半。
       （用户 2026-09-30：「把心跳的节点也放到看板协作架构图中」。）空或缺 ⇒ 退回"只有钩子子进程"的老版面。
    任何异常都吞掉并降级 —— 看板**不能因为使用方的脚本坏了就打不开**。

    🔴🔴 **⛔ 不许缓存 `build()` 的结果**（2026-09-30 我自己踩的坑）：
       第一版把**结果 dict** 缓存了 ⇒ 扩展里的**探针只跑了一次** ⇒ 前置状态**永久冻结在服务启动那一刻**，
       而且**不报错**（表现为"看板说该端口未监听，而 netstat 明明确有 LISTENING"）。
       ⇒ 只缓存**模块对象**（省一次 import），**每次快照都重新调用 `build(ws)`**。
       （⛔ 本文件的注释与 docstring 里也**不写任何项目的端口／路径** —— 有静态用例守着。）
    """
    rel = str(C.get("board_ext") or ".workbuddy/collab/board_ext.py")
    p = (ws / rel)
    out: dict = {"title": "前置", "tag": "", "chips": [], "paragraphs": [], "tip": ""}
    if not p.is_file():
        out["_missing"] = "未配看板扩展（board_ext）：%s" % p
        return out
    try:
        key = str(p)
        mod = EXT_CACHE.get(key)
        if mod is None:                       # 只缓存模块；⚠️ 文件改了要重启服务才会重载
            import importlib.util
            spec = importlib.util.spec_from_file_location("_board_ext_%d" % abs(hash(key)), str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            EXT_CACHE[key] = mod
        got = mod.build(str(ws))              # 🔴 每次都快照都重跑探针
        if isinstance(got, dict):
            out.update(got)
    except Exception as e:
        out["_err"] = str(e)
        warn.append("看板扩展加载失败（已降级）：%s" % e)
    return out


def _queue(tasks: dict, st: dict) -> dict:
    """🔴 **队列计数**（用户 2026-09-30：「协作程序 也要显示**当前待验收队列数量**」）。

    🔴 **队列 ＝ 需求台账 `tasks.json`**（`architecture.md` §迭代记录明载），**四态**：
       `pending 待执行` / `running 执行中` / `done 已完成` / `blocked 有阻碍`。
    ⚠️ **机制里没有「待验收」这个态**（⛔ 不臆造一个数字出来）⇒ 这里如实给**四态明细 ＋ 未完结数**：
       `open`（未完结）＝ 待执行 ＋ 执行中 ＋ 有阻碍 ＋ 其它；`done` ＝ 已完成。
    ⚠️ 另附 `notify_n`（= `queue_info.n`，**待主会话反馈**的通知条数）—— 它与台账是两码事，⛔ 别混。
    """
    by = {"pending": 0, "running": 0, "done": 0, "blocked": 0, "other": 0}
    for v in (tasks or {}).values():
        s = str((v or {}).get("state") or "").strip().lower()
        by[s if s in by else "other"] += 1
    qi = st.get("queue_info") or {}
    try:
        _nn = int(qi.get("n") or 0)
    except Exception:
        _nn = 0
    return {"total": len(tasks or {}), "by": by,
            "open": by["pending"] + by["running"] + by["blocked"] + by["other"],
            "notify_n": _nn}


# ⛔ 「监督守护为什么停」是**某个项目的历史事实** ⇒ 归**使用方**（写在其 `board_ext.py` 里，
#    经 `ext["guard_stop_reason"]` 取回）；技能侧⛔ 不落任何项目的止损史。


def _age_min(p: Path):
    """文件 mtime 距今多少分钟。取不到 ⇒ None（⛔ 不拿 0 冒充"刚更新"）。"""
    try:
        return round((time.time() - p.stat().st_mtime) / 60.0, 1)
    except Exception:
        return None


def _runtime() -> dict:
    """🔴 **协作程序 / 监督程序的实时状态**（用户 2026-09-30 要求架构图「**要能展示实时状态**」）。
    取的全是**真痕迹**（状态戳 mtime），⛔ 不猜、⛔ 不按"配置里写着要常驻"就当它活着：
      · **协作程序**：`_tick.stamp`（宿主钩子唤起的一次性投递轮）／`collabd-once.stamp`（投影轮）取更新时间
      · **投递**（旧名「监督程序」）：**不是一个该常驻的进程** —— 它就是**宿主钩子唤起的一次性
        `--tick`**。所以这里报的是「**这条链通不通**」（看 `--tick` 戳的新鲜度），⛔ 不是"启动没启动"。
    ⚠️ 教训仍在：图上必须能一眼看出**谁其实没在跑** —— 但"没在跑"得先说清**它本来该不该跑**。
    """
    tick = _age_min(INBOX / "_tick.stamp")
    proj = _age_min(INBOX / "collabd-once.stamp")
    glog = _age_min(INBOX / "guard.log")
    stopped = (INBOX / "guard.stop").exists()

    _a = [a for a in (tick, proj) if a is not None]
    prog_up = bool(_a) and min(_a) < 15.0                    # 15 分钟内有轮 ⇒ 算在线
    prog_age = min(_a) if _a else None
    prog_by = ""
    if prog_up:
        prog_by = "钩子 --tick" if (tick is not None and (proj is None or tick <= proj)) else "投影轮 --once"

    guard_up = (not stopped) and (glog is not None and glog < 5.0)
    return {
        "prog": {"up": prog_up, "age_min": prog_age, "by": prog_by,
                 "tick_age_min": tick, "proj_age_min": proj,
                 "label": (("在线 · %s · %s 分钟前" % (prog_by, prog_age)) if prog_up else "已停")},
        # 🔴🔴 **「投递」这个词取代了旧的「监督程序」**（2026-09-30 改口径 · 用户连问四次
        #    "监督程序为什么打不开 / 它一直停着能起什么作用 / 还需要保留吗 / 跟它有关系吗"）：
        #      · 「监督程序」是**投递这条职责的旧名** —— 它从来不是一个"该常驻的进程"；
        #      · ⛔ **别再报"已停"** —— 那会让人以为有东西坏了，而实际上**没有东西该在跑**；
        #      · 真正该报的是「**投递这条链通不通**」⇒ 判据用 `--tick` 戳的新鲜度（投递轮就是它）。
        #    🔴 **必须原样保留的**：投递方唯一性（只有 `--tick` 能推进队列）—— 22:53 事故的修法。
        "deliver": {"up": (tick is not None and tick < 15), "stopped": stopped, "age_min": tick,
                    "label": (("就绪 · 由宿主钩子唤起 · %s 分钟前跑过" % tick)
                              if (tick is not None and tick < 15) else "长时间未触发（钩子没被唤起）"),
                    "reason": ""},          # ⛔ 不再给"停因"——那个问题已经不存在了
    }


# ⛔ 上一版这里还有 `_parse_relay_line` / `_overlay_node` / `_end_to_end_verified` 三个函数 ——
#    它们解析的是**某个项目的中继客户端日志**（glob 路径、字段名、`streams>0` 判据）。
#    按「技能就是技能，谁用产生的文件放在他自己那里」，**已整体搬到使用方的
#    `.workbuddy/collab/board_ext.py`**；技能侧只保留「读扩展并渲染」这一层（见 `_ext()`）。


def build() -> dict:
    """🔴 **生成一份只读快照**。保证：**永不抛异常**（任何一处读不到 ⇒ 记进 `warn` 并降级），
    ⇒ 调用方永远拿得到一份可渲染的数据，⛔ 不会因为看板读不到某个文件而连累别人。
    """
    warn = []
    goal = _j(INBOX / "goal.json", {}) or {}
    tasks = _j(INBOX / "tasks.json", {}) or {}
    st = _j(INBOX / "collabd-state.json", {}) or {}
    if not goal:
        warn.append("读不到 goal.json")
    if not st:
        warn.append("读不到 collabd-state.json")
    qi = st.get("queue_info") or {}
    we = st.get("wake") or {}
    acc = {k: v for k, v in (goal.get("acceptance_state") or {}).items() if not str(k).startswith("_")}
    _ext_d = _ext(WS, warn)                      # 🔴 使用方自己的扩展（⛔ 技能侧不含项目知识）
    _rt = _runtime()       # ⚠️ 「投递」不再需要"停因"（那个问题已随改名消失）⇒ 不回填 reason
    _ext_d.pop("guard_stop_reason", None)   # 使用方若还留着这个键 ⇒ 不渲染（避免又把旧问题带回来）
    try:
        _ss = _sessions()
    except Exception as e:
        _ss = {"mine": [], "others_running": 0, "err": "会话读取异常：%s" % e}
    if _ss.get("err"):
        warn.append(_ss["err"])
    _sc = project_scope()
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": round(time.time(), 1),
        "goal": {"title": goal.get("title") or "", "short": goal.get("short") or "", "acceptance": acc},
        # 🔴 项目身份：看板顶部「本项目」区用它 ⇒ 一眼看清"这个看板是哪个需求目标项目的"
        "project": {"id": _sc["goal_id"], "short": _sc["short"], "title": _sc["title"],
                    "main_sid8": (_sc["main_sid"] or "")[:8], "criteria": _sc["criteria"],
                    "workspace": _sc["workspace"]},
        "others_running": _ss["others_running"],
        "tasks": {k: {"state": str((v or {}).get("state") or "?"),
                      "line": str((v or {}).get("line") or ""),
                      "by": str((v or {}).get("by") or ""),
                      "artifact": str((v or {}).get("artifact") or "")} for k, v in tasks.items()},
        "sessions": _ss["mine"],
        # 🔴 分工板（架构图「协作会话」层用它渲染）—— ⛔ 不是历史会话列表
        "labor": _labor(tasks, _ss["mine"], goal),
        "wakeups": [{"ts": w.get("ts"), "kind": w.get("kind") or "-", "http": w.get("http"), "ok": w.get("ok")}
                    for w in _tail_jsonl(INBOX / "wakeups.jsonl", 10)],
        # 🔴 「前置」整块由**使用方**提供（见 `board_ext`）—— 技能侧只负责把它渲染出来。
        #    ⛔ 技能里不许出现任何项目的端口／路径／真名／历史。
        "front": _ext_d,
        "runtime": _rt,                              # 🔴 实时状态（架构图用）
        "queue": _queue(tasks, st),                  # 🔴 队列计数（协作程序节点用）
        "progress": {"last_progress_at": st.get("last_progress_at"),
                     "goals_open": bool(qi.get("probe", {}).get("goal_open")),
                     "main_busy": bool(qi.get("probe", {}).get("main_busy")),
                     "prog_age_min": qi.get("probe", {}).get("prog_age_min")},
        "notify": {"awaiting": qi.get("awaiting") or "", "phase": qi.get("phase") or "",
                   "sent": qi.get("sent") or "", "notice": qi.get("notice") or ""},
        "warn": warn,                                # ⛔ 降级不静默：界面要显示"哪一块没读到"
        "meta": {"supervise_interval": C.get("supervise_interval", 30),
                 "queue_idle_min": 5, "wake_min_gap": C.get("wake_min_gap", 300),
                 "workspace": str(WS), "inbox": str(INBOX)},
    }


def serve(port: int = 8788, interval: float = 3.0) -> int:
    """🔴 本地看板服务（**只绑 127.0.0.1**）。⛔ 不引第三方、⛔ 不开对外端口、⛔ 不写任何账本。

    🔴🔴 **总则：看板不能影响程序执行**（用户 2026-09-30 明令：「看板不能影响程序执行，可以**异步**、
    可以**延迟**」）。据此，请求路径与数据生产**彻底解耦**：
      · **一个后台线程**每 `interval` 秒生成一次快照 → 存进**内存缓存**
      · **请求线程只吐缓存字节** —— ⛔ 不碰 DB、⛔ 不读文件、⛔ 不做任何可能阻塞的事
        ⇒ 开 10 个标签页 = 10 次内存读；宿主库的查询频率**恒定**为 `1/interval`，与页面数无关
      · 刷新失败 ⇒ **保留上一份快照**（界面显示"延迟 N 秒"）⇒ ⛔ 绝不 500、⛔ 绝不给空板
      · `--interval` 可取大（实时性换零负担）；页面按同一 `interval` 自取，⛔ 不自行加频
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    html_p = HERE.parent / "assets" / "board.html"
    cache = {"bytes": None, "at": 0.0, "err": "", "n": 0}

    def _refresh_loop():
        """唯一的生产者（后台守护线程）。⛔ 它出任何事都只影响"数据新不新"，⛔ 不影响服务存活。"""
        while True:
            try:
                d = build()
                d["board"] = {"refresh_interval": interval, "generated_at": round(time.time(), 1),
                              "readonly": True}
                cache["bytes"] = json.dumps(d, ensure_ascii=False, indent=1).encode("utf-8")
                cache["at"] = time.time()
                cache["err"] = ""
                cache["n"] += 1
            except Exception as e:                    # ⛔ 不清旧快照：宁可给"旧的"也不给"空的"
                cache["err"] = str(e)
            time.sleep(max(0.5, float(interval)))

    threading.Thread(target=_refresh_loop, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body: bytes, ctype: str):
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception:                          # 客户端提前断开 ⇒ 静默（⛔ 不刷日志）
                pass

        def do_GET(self):                                        # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/board.html", "/index.html"):
                try:
                    self._send(200, html_p.read_bytes(), "text/html; charset=utf-8")
                except Exception as e:
                    self._send(500, ("board.html 读不到：%s" % e).encode("utf-8"),
                               "text/plain; charset=utf-8")
            elif path == "/board.json":
                body = cache["bytes"]
                if body is None:                        # 冷启动：第一份还没出来
                    body = json.dumps({"warming": True,
                                       "board": {"refresh_interval": interval}}, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/healthz":
                self._send(200, json.dumps({"ok": True, "snapshots": cache["n"],
                                            "age": round(time.time() - cache["at"], 1),
                                            "err": cache["err"]}, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, *a):                               # ⛔ 静默（不刷屏、不唤醒宿主）
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", int(port)), H)
    srv.daemon_threads = True
    print("看板已起：http://127.0.0.1:%d/  （只绑回环 · 每 %ss 异步快照 · 请求零阻塞）" % (int(port), interval))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def main() -> int:
    if "--serve" in sys.argv:
        i = sys.argv.index("--serve")
        p = 8788
        if i + 1 < len(sys.argv) and sys.argv[i + 1].isdigit():
            p = int(sys.argv[i + 1])
        iv = 3.0
        if "--interval" in sys.argv:                     # 想更省 ⇒ 调大（实时性换零负担）
            j = sys.argv.index("--interval")
            if j + 1 < len(sys.argv):
                try:
                    iv = max(0.5, float(sys.argv[j + 1]))
                except Exception:
                    iv = 3.0
        return serve(p, iv)
    out = INBOX / "board.json"
    if "--out" in sys.argv:
        i = sys.argv.index("--out")
        if i + 1 < len(sys.argv):
            out = Path(sys.argv[i + 1])
    INBOX.mkdir(parents=True, exist_ok=True)
    d = build()
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("板快照已写：%s（%d 字节）" % (out, out.stat().st_size))
    print("  目标：%s ｜ 验收非 pass：%s" % (d["goal"]["title"][:40],
                                        [k for k, v in d["goal"]["acceptance"].items() if str(v).lower() != "pass"] or "无"))
    _f = d.get("front") or {}
    _ch = _f.get("chips") or []
    print("  前置（%s）：%s" % (
        _f.get("title") or "—",
        " ｜ ".join("%s=%s" % (c.get("label"), "上线" if c.get("up") else "离线") for c in _ch)
        or (_f.get("_missing") or "（使用方未配 board_ext）")))
    print("  台账：%s" % {k: v["state"] for k, v in d["tasks"].items()})
    _r = d.get("runtime") or {}
    print("  实时：协作程序=%s ｜ 投递=%s" % ((_r.get("prog") or {}).get("label"),
                                            (_r.get("deliver") or {}).get("label")))
    if d.get("warn"):
        print("  ⚠️ 降级（不静默）：%s" % "；".join(d["warn"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
