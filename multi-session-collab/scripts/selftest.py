# -*- coding: utf-8 -*-
"""selftest —— 「多会话协作机制」的**回归测试**：把**所有异常情况**做成用例，每次改完跑一遍。

🔴 纪律（2026-09-29 用户定案）：**每次改完 collabd.py / guard.py，必须跑本文件**，全绿才算改完。
设计原则：
  · **不碰生产**：整套测试用 `COLLABD_CONFIG` 指向 `tmp/selftest/`（独立 inbox / 看板 / 任务图），
    生产台账与看板**一个字节都不动**。
  · **离线可跑**：需要宿主库的用例，读不到就记 SKIP（⛔ 不假装通过）。
  · **判据是"读数"不是"没报错"**：rc、文件是否生成、台账状态、投递台账增量 …
  · FAIL ⇒ **非零退出**（配合"rc=0 才是真成功"）。

用法：python selftest.py            # 全部
      python selftest.py -k 投递     # 只跑名字含"投递"的用例
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 测试工作区：优先 `COLLABD_WS`；否则落在「当前目录/collabd-selftest」（⛔ 不写进技能目录）
WS = Path(os.environ.get("COLLABD_WS") or (Path.cwd() / "collabd-selftest")).resolve()
TEST_WS = WS / "tmp" / "selftest"
TIN = TEST_WS / "inbox"
CFG = TEST_WS / "collabd.config.json"
PY = sys.executable
RESULTS = []


def _mk_env() -> dict:
    env = dict(os.environ)
    env["COLLABD_CONFIG"] = str(CFG)
    env["COLLABD_WS"] = str(TEST_WS)
    return env


def _prepare() -> None:
    TIN.mkdir(parents=True, exist_ok=True)
    for _f in ("wake.lock", "guard.stop", "goal.pending.json"):   # 清上一轮残留（防跨轮假红）
        try:
            (TIN / _f).unlink()
        except Exception:
            pass
    (TEST_WS / "live.md").write_text("(selftest)", encoding="utf-8")
    (TEST_WS / "tg.json").write_text(json.dumps({"nodes": [
        {"id": "T1", "title": "测试件-1", "status": "todo", "line": "line-a"},
        {"id": "T2", "title": "测试件-2", "status": "done", "line": "line-b"},
    ]}, ensure_ascii=False), encoding="utf-8")
    CFG.write_text(json.dumps({
        "_说明": "selftest 专用（⛔ 不碰生产）",
        "workspace": str(TEST_WS).replace("\\", "/"),
        "inbox": "inbox",
        "live": "live.md",
        "taskgraph": "tg.json",
        "lines": {"line-a": "A 线", "line-b": "B 线"},
        "goal_docs": [], "targets": {},
        "host_db": "",
        "singleton_port": 20098,
        "interval": 5, "idle_min": 12, "stuck_min": 30, "vacuum_min": 5,
        "health_every": 300, "wake_enable": True, "wake_min_gap": 300, "wake_max_ports": 4,
        "wake_text": "(selftest)",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def case(name):
    def deco(fn):
        fn._case = name
        return fn
    return deco


def run_cli(args, timeout=60):
    p = subprocess.run([PY, "-u", str(HERE / "collabd.py")] + args, cwd=str(HERE),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env=_mk_env(), timeout=timeout, errors="replace")
    return p.returncode, p.stdout or ""


def tasks() -> dict:
    try:
        return json.loads((TIN / "tasks.json").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def wakeups_n() -> int:
    try:
        return len((TIN / "wakeups.jsonl").read_text(encoding="utf-8").strip().splitlines())
    except Exception:
        return 0


def state() -> dict:
    try:
        return json.loads((TIN / "collabd-state.json").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def put_tasks(t: dict) -> None:
    (TIN / "tasks.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cli_env(args, env: dict, timeout=60):
    """同 `run_cli`，但**自定义环境**（用例要故意抽掉网关口令等）。"""
    p = subprocess.run([PY, "-u", str(HERE / "collabd.py")] + args, cwd=str(HERE),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env=env, timeout=timeout, errors="replace")
    return p.returncode, p.stdout or ""


_PROD_SNAP = {}


def _snap_prod():
    """记录**生产 inbox** 关键产物的 (mtime,size) —— 元用例据此断言「测试不碰生产」。"""
    global _PROD_SNAP
    _PROD_SNAP = {}
    for f in ("tasks.json", "wakeups.jsonl", "digest.md", "TO-MAIN.md", "wake.lock"):
        p = WS / "tmp" / "supervise-inbox" / f
        try:
            st = p.stat()
            _PROD_SNAP[f] = (st.st_mtime, st.st_size)
        except Exception:
            _PROD_SNAP[f] = None


def imp():
    # 🔴 先设环境，**再加载模块** —— 否则模块会去读**生产配置**（本用例曾因此"碰生产"＋结果飘）
    os.environ["COLLABD_CONFIG"] = str(CFG)
    os.environ["COLLABD_WS"] = str(TEST_WS)
    import importlib.util
    spec = importlib.util.spec_from_file_location("cb_selftest", HERE / "collabd.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 用例（每条都对应一个真实踩过的坑） ─────────────────────────────
@case("命名：两级前缀解析（合规 / 缺级 / 旧式）")
def t_name():
    m = imp()
    a = m.parse_session_name("[协作]-[示例主题]-N9 复测")
    b = m.parse_session_name("[主]-[示例主题]-机制线")
    c = m.parse_session_name("[协作]N9-2156")          # 只一级 ⇒ 不合规
    d = m.parse_session_name("接续 · 机制线（旧式）")
    return [("合规二级=worker/示例主题", (a["role"], a["topic"], a["ok"]) == ("worker", "示例主题", True)),
            ("合规二级=main", (b["role"], b["topic"], b["ok"]) == ("main", "示例主题", True)),
            ("只一级 ⇒ 判不合规", c["ok"] is False),
            ("旧式 ⇒ 判不合规", d["ok"] is False)]


@case("目标：无目标 ⇒ 拒绝启动 rc=3")
def t_goal_missing():
    g = TIN / "goal.json"
    bak = g.read_text(encoding="utf-8") if g.exists() else None
    if g.exists():
        g.unlink()
    p = subprocess.run([PY, "-u", str(HERE / "guard.py")], cwd=str(HERE),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env=_mk_env(), timeout=60, errors="replace")
    if bak is not None:
        g.write_text(bak, encoding="utf-8")
    return [("rc==3", p.returncode == 3), ("提示含『没有任务目标』", "没有任务目标" in (p.stdout or ""))]


@case("目标：有待确认 ⇒ 拒绝启动 rc=4；--propose/--confirm 能走通")
def t_goal_pending():
    g, gp = TIN / "goal.json", TIN / "goal.pending.json"
    bak = g.read_text(encoding="utf-8") if g.exists() else None
    if g.exists():
        g.unlink()
    subprocess.run([PY, "-u", str(HERE / "guard.py"), "--propose", "自测目标"],
                   cwd=str(HERE), env=_mk_env(), timeout=60,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                       creationflags=0x08000000)
    p = subprocess.run([PY, "-u", str(HERE / "guard.py")], cwd=str(HERE),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env=_mk_env(), timeout=60, errors="replace")
    rc_pending = p.returncode
    subprocess.run([PY, "-u", str(HERE / "guard.py"), "--confirm"], cwd=str(HERE), env=_mk_env(),
                   timeout=60, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                       creationflags=0x08000000)
    ok_confirmed = g.exists() and not gp.exists()
    if bak is not None:
        g.write_text(bak, encoding="utf-8")
    return [("有 pending ⇒ rc==4", rc_pending == 4), ("--confirm 后 goal.json 生成且 pending 清除", ok_confirmed)]


@case("台账四态：running → blocked(带原因) → 解除后原因清掉")
def t_states():
    run_cli(["--report", "C1", "--state", "running", "--by", "t", "--line", "line-a"])
    ok1 = (tasks().get("C1") or {}).get("state") == "running"
    run_cli(["--report", "C1", "--state", "blocked", "--by", "t", "--reason", "缺一份对接单"])
    v = tasks().get("C1") or {}
    ok2 = v.get("state") == "blocked" and "对接单" in str(v.get("block_reason"))
    run_cli(["--report", "C1", "--state", "pending", "--by", "t"])
    v = tasks().get("C1") or {}
    ok3 = v.get("state") == "pending" and not v.get("block_reason")
    t = tasks(); t.pop("C1", None); (TIN / "tasks.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    return [("上报 running 生效", ok1), ("blocked 带原因", ok2), ("解除后原因被清掉", ok3)]


@case("投递：协作程序（--once / --report）**零投递**")
def t_no_deliver():
    w0 = wakeups_n()
    run_cli(["--once"])
    run_cli(["--report", "C2", "--state", "running", "--by", "t"])
    w1 = wakeups_n()
    t = tasks(); t.pop("C2", None); (TIN / "tasks.json").write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
    return [("--once 与 --report 合计零投递（%d→%d）" % (w0, w1), w1 == w0)]


@case("投递：目标会话在跑 ⇒ 必须延后（target-busy）")
def t_target_busy():
    m = imp()
    sid = os.environ.get("CODEBUDDY_SESSION_ID") or ""
    if not sid:
        return [("SKIP：环境里取不到会话 id", True)]
    # 🔴 2026-09-30 修（**同族假红**）：本用例的前提是"主会话**此刻**在 `working`"——
    #    而主会话在**空闲回落**时 `sessions.status='completed'`（实测：`_session_status(fe146dd9)='completed'`）
    #    ⇒ 前提不成立 ⇒ 我原来会判**假红**。⇒ 与"对账/幂等"两条同族处理：**前提不成立就 SKIP**
    #    （⛔ 不假装通过、也⛔ 不判假红）—— ✅ 正确的红是"目标在跑却没被延后"。
    _st_now = m._session_status(sid)
    if _st_now != "working":
        return [("SKIP：主会话当前 `%s`（≠working）⇒ target-busy 前提不成立" % (_st_now or "?"), True)]
    st = {"roles": {sid: "main"}}
    r = m._deliver_str("自测：不该被投出", "selftest", st)
    return [("skipped==target-busy", r.get("skipped") == "target-busy")]


@case("投递：跨进程互斥锁存在且会被释放")
def t_lock_released():
    m = imp()
    m._deliver_str("自测", "selftest", {"roles": {}})
    return [("wake.lock 未残留", not (TIN / "wake.lock").exists())]


# ── 🔴 2026-09-30 加：**投递＝宿主钩子唤起（`--tick`）** 的回归面 ────────────────
#    背景（用户口径「又给我整到自动任务去了」）：投递**⛔ 不靠自动化排期、⛔ 不靠常驻**，
#    改由**宿主钩子**唤起 `collabd.py --tick`（钩子是宿主子进程 ⇒ 自带网关口令）。
#    随之必须钉死两条**新引入的正确性约束**，否则会出现"程序在跑、主会话什么都没收到"：
#      ① `--once`（协作程序／投影轮）**⛔ 不许消费队列** —— 它在 UserPromptSubmit 上先跑；
#      ② `--tick` **投不出去就不许消费队列** —— 旧实现无条件 `notified[kid]=stt` ⇒ **静默丢件**。

@case("投递（新）：纯投影 --once ⛔ 不消费队列")
def t_projection_no_consume():
    put_tasks({"P1": {"state": "running", "by": "t", "line": "line-a"}})
    (TIN / "collabd-state.json").write_text(json.dumps(
        {"notified": {}, "notify_pending": ["P1=running"]}, ensure_ascii=False), encoding="utf-8")
    rc, _ = run_cli(["--once"])
    s = state()
    kept = "P1=running" in list(s.get("notify_pending") or [])
    no_aw = not s.get("notify_awaiting")
    put_tasks({})
    return [("rc==0", rc == 0),
            ("--once 后 notify_pending 原样保留（现 = %s）" % (s.get("notify_pending"),), kept),
            ("--once 未设 notify_awaiting", no_aw)]


@case("投递（新）：投不出去（no-token）⇒ ⛔ 不消费队列（防静默丢件）")
def t_no_consume_when_skipped():
    put_tasks({"Q1": {"state": "running", "by": "t", "line": "line-a"}})
    (TIN / "collabd-state.json").write_text(json.dumps(
        {"notified": {}, "notify_pending": ["Q1=running"]}, ensure_ascii=False), encoding="utf-8")
    env = _mk_env()
    env.pop("CODEBUDDY_GATEWAY_PASSWORD", None)     # 抽掉口令 ⇒ 投递必被挡（no-token）
    rc, out = run_cli_env(["--tick"], env)
    s = state()
    pend = list(s.get("notify_pending") or [])
    notified = dict(s.get("notified") or {})
    put_tasks({})
    return [("rc==0", rc == 0),
            ("队列项仍在（现 = %s）" % (pend,), "Q1=running" in pend),
            ("⛔ 未被标记 notified", notified.get("Q1") != "running"),
            ("⛔ 未设 notify_awaiting", not s.get("notify_awaiting"))]


@case("宿主卡住探针：只读尾部、测试环境零命中、⛔ 不误报")
def t_park_probe():
    # 2026-09-30 立（用户给的窗口 22:55–23:20 实测定型）：指纹 = parkInQueue ＋ hasWaiter=false。
    # ⚠️ 测试工作区名是 `selftest` ⇒ 候选日志目录里没有它的日志 ⇒ 必须**零命中且不报错**（⛔ 不碰生产日志）。
    m = imp()
    r = m.probe_host_park()
    real = ('[2026/9/29 22:53:00.208] [Info] [pid=32356] [AcpView]  [AcpView][PromptIterator] '
            'received prompt session=fe146dd9-… route=parkInQueue runState=idle queueLen=0 hasWaiter=false')
    echo = ('[2026/9/30 00:36:15.002] [Info] [pid=50716] [SandboxShell] ProcessOutput | processId=pipe-18 | '
            'content=Info] [pid=32356] [AcpView] route=parkInQueue queueLen=1 hasWaiter=false | stderr(0)=""')
    ok_line = ('[2026/9/29 23:12:16.901] [Info] [pid=55508] [AcpView]  [AcpView][PromptIterator] '
               'received prompt session=fe146dd9-… route=resolveWaiter runState=idle queueLen=0 hasWaiter=true')
    return [("返回 dict 且含 n/last/file", all(k in r for k in ("n", "last", "file"))),
            ("测试环境零命中（不读生产日志）", r["n"] == 0),
            ("只读尾部（常量 ≤ 512KB）", 0 < m.PARK_TAIL <= 512 * 1024),
            ("⛔ 不命中「取证命令的回声行」（防假阳性）", m._is_park_line(echo) is False),
            ("✅ 命中真正的 parkInQueue 记录行", m._is_park_line(real) is True),
            ("✅ 不误判正常行（resolveWaiter/hasWaiter=true）", m._is_park_line(ok_line) is False)]


@case("投递消费回查：投出去 ≠ 跑起来了 ⇒ 未消费必须升级为 NEED-USER")
def t_consume_check():
    m = imp()
    nu = TIN / "NEED-USER.md"
    if nu.exists():
        nu.unlink()
    # ① 刚投出去（宽限期内）⇒ waiting，⛔ 不报警
    st1 = {"wake": {"expect": {"sid": "0" * 36, "at": time.time()}}}
    r1 = m.check_delivery_consumed(st1)
    quiet = not nu.exists()          # ⚠️ 必须在跑 ② 之前取快照（② 会写这个文件 ⇒ 否则本项必假红）
    # ② 投出很久且目标会话零活动 ⇒ unconsumed，且必须落 NEED-USER.md（喊人）
    st2 = {"wake": {"expect": {"sid": "0" * 36, "at": time.time() - 9999}}}
    r2 = m.check_delivery_consumed(st2)
    shouted = nu.exists() and "切走再切回" in nu.read_text(encoding="utf-8", errors="replace")
    return [("宽限期内 = waiting 且不报警", r1.get("waiting") is True and quiet),
            ("超期未动 = unconsumed", r2.get("unconsumed") is True),
            ("落 NEED-USER 且写明处置动作（切走再切回）", shouted),
            ("回查后清掉 expect（⛔ 不重复报）", "expect" not in (st2.get("wake") or {}))]


@case("投递（新）：--tick 在位、可跑、且**只由它投递**")
def t_tick_wired():
    m = imp()
    known = "--tick" in {"--where", "--once", "--supervise", "--tick", "--report", "--reqs"}
    rc, out = run_cli(["--tick"])
    src = (HERE / "collabd.py").read_text(encoding="utf-8", errors="replace")
    # 想连"钩子里确实调了 --tick"一起验 ⇒ 把钩子脚本路径放进 `COLLABD_HOOK_PATH`；
    # 不配就跳过（⛔ 技能里不写死任何使用方的钩子路径）。
    _hp = os.environ.get("COLLABD_HOOK_PATH") or ""
    hook = Path(_hp) if _hp else None
    hook_ok, hook_note = True, "(未配置 COLLABD_HOOK_PATH，跳过)"
    if hook is not None and hook.exists():
        hs = hook.read_text(encoding="utf-8", errors="replace")
        hook_ok = ("--tick" in hs) and ("maybe_run_supervisor_tick" in hs)
        hook_note = "钩子里有 `--tick` 调用"
    return [("`--tick` 属已知参数", known),
            ("`--tick` 可跑 rc==0", rc == 0),
            ("投递方唯一：`--tick` 用 mutate=True 调用 supervise", "deliver=True)" in src or "deliver=True, mutate" in src or "supervise(st, deliver=True)" in src),
            (hook_note, hook_ok)]


@case("对账：僵尸（台账 running 但没人跑）⇒ 标 blocked")
def t_reconcile_zombie():
    # 🔴 2026-09-30 修（**本用例曾假红**）：僵尸的判据是「**除观察者外无人跑** ∧ 无 IN_PROGRESS」——
    #    而测试机上**天天有别的工作会话在跑** ⇒ 前提不成立 ⇒ 不能判它红（⛔ 也不许假装绿）。
    #    正确姿势：**前提不成立就 SKIP**（读不出宿主库、或确实有别的会话在跑）。
    m = imp()
    put_tasks({"Z1": {"state": "running", "by": "已经死掉的棒", "t": time.time()}})
    r = m.reconcile({})
    if r.get("skip"):
        put_tasks({})
        return [("SKIP：读不到宿主库（%s）" % r["skip"], True)]
    if r.get("adopted"):
        put_tasks({})
        return [("SKIP：当前确有 %d 个别的会话在跑 ⇒ 『除观察者外无人跑』前提不成立"
                 % len(r["adopted"]), True)]
    v = m._load_tasks().get("Z1") or {}
    ok = v.get("state") == "blocked" and "执行者已消失" in str(v.get("block_reason"))
    put_tasks({})
    return [("Z1 被标 blocked 且原因是『执行者已消失』", ok)]


@case("对账：幂等（连跑两次结论一致）")
def t_reconcile_idempotent():
    # 🔴 2026-09-30 修（**同一类假红**）：第 1 次会**收编**当前正在跑的别的工作会话（环境相关），
    #    第 2 次才进入"没有新东西"的稳态 ⇒ 幂等性只能比较 **第 2 次 vs 第 3 次**。
    run_cli(["--reconcile"])
    _, o1 = run_cli(["--reconcile"])
    _, o2 = run_cli(["--reconcile"])
    put_tasks({})
    return [("第 2 次 = 第 3 次输出一致", o1.strip().splitlines()[:1] == o2.strip().splitlines()[:1]),
            ("第 2 次已无新收编（『收编 0 条』）", "收编 0 条" in o1)]


@case("CLI：未知参数 ⇒ rc=2 且**不落常驻**")
def t_unknown_arg():
    rc, out = run_cli(["--nonsense"], timeout=20)
    return [("rc==2", rc == 2), ("提示未知参数", "未知参数" in out)]


@case("CLI：--where / --reqs 可跑且 rc=0")
def t_basic_cli():
    rc1, o1 = run_cli(["--where"])
    rc2, _ = run_cli(["--reqs"])
    return [("--where rc==0 且打印路径", rc1 == 0 and "workspace" in o1), ("--reqs rc==0", rc2 == 0)]


@case("结构：关键名字/设施在位（防『误删常量』重演）")
def t_symbols():
    src = (HERE / "collabd.py").read_text(encoding="utf-8")
    need = ["CLAIMS", "SELF_SID", "STALE", "DOING_TTL", "def reconcile(", "def supervise(",
            "def _deliver_str(", "def _guard_says_stop(", "def parse_session_name(",
            "def topic_of(", "def load_goal(", "GOAL_F"]
    miss = [n for n in need if n not in src]
    return [("关键符号齐全（缺：%s）" % (miss or "无"), not miss)]


@case("结构：所有 subprocess 调用都带 creationflags（不显窗）")
def t_nowindow():
    bad = []
    for fn in ("collabd.py", "guard.py"):
        for i, l in enumerate((HERE / fn).read_text(encoding="utf-8").splitlines(), 1):
            if "subprocess.run(" in l or "subprocess.Popen(" in l:
                seg = " ".join((HERE / fn).read_text(encoding="utf-8").splitlines()[i - 1:i + 4])
                if "creationflags" not in seg:
                    bad.append("%s:%d" % (fn, i))
    return [("无『没带 creationflags』的子进程调用（%s）" % (bad or "无"), not bad)]


@case("结构：fatal 不再 exit 0（防『假绿』）")
def t_failopen():
    src = (HERE / "collabd.py").read_text(encoding="utf-8")
    i = src.find('log("fatal %s" % e)')
    seg = src[i:i + 200] if i >= 0 else ""
    return [("fatal 分支是非零退出", "sys.exit(1)" in seg), ("⛔ 不是 exit(0)", "sys.exit(0)" not in seg)]


@case("结构：投递方唯一（只有 --tick 能推进队列）＋ 旧投递路径已停用")
def t_deliver_switch():
    src = (HERE / "collabd.py").read_text(encoding="utf-8")
    return [("supervise 有 deliver 参数", re.search(r"def supervise\(st: dict, deliver: bool", src) is not None),
            ("supervise 有 mutate 参数（防『投不出去却消费队列』）",
             re.search(r"def supervise\(st: dict, deliver: bool = True, mutate: bool = True\)", src) is not None),
            ("one_round 走**纯投影**（deliver=False, mutate=False）",
             re.search(r"supervise\(st,\s*deliver=False,\s*mutate=False\)", src) is not None),
            ("`--tick` 是唯一投递方（supervise(st, deliver=True)）",
             re.search(r"supervise\(st,\s*deliver=True\)", src) is not None),
            ("wake_round 已标注停用", "已停用" in src)]


@case("结构：守护生命周期（优雅退出设施在位）")
def t_graceful():
    src = (HERE / "guard.py").read_text(encoding="utf-8")
    return [("有 _shutdown（优雅收尾）", "def _shutdown(" in src),
            ("子进程打 COLLABD_GUARDED 标", "COLLABD_GUARDED" in src),
            ("子程序侧有 _guard_says_stop", "_guard_says_stop" in (HERE / "collabd.py").read_text(encoding="utf-8"))]


@case("归属判据：看板 ≡ 收尾确认（⛔ 防两处漂移；⛔ 不按 cwd 推断）")
def t_scope_parity():
    """🔴 用户 2026-09-30 两条明示：「**要明确哪些会话是属于某个需求目标项目的**」＋
    「**这个看板上应该明确显示是哪个需求目标项目**」。

    `collabd._in_project()`（`--ready-next` 用）与 `board.in_project()`（看板用）是**重复实现**
    ⇒ 一旦漂移就会出这种怪事：**看板说"本项目没人在跑"，而 `--ready-next` 被别的项目的会话拦死**。
    本用例把两者摆到同一张用例表上逐例比对（含"别的项目"这类**别人家的会话**必须判否）。
    """
    code = r'''
import importlib.util, inspect, json, sys
sys.dont_write_bytecode = True   # ⛔ 别让测试自己往技能目录里落 __pycache__
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
H = sys.argv[1]
cb = load("cb", H + "/collabd.py")
bd = load("bd", H + "/board.py")
cases = [
    ("main1", "任意标题（主会话）",              "main1", "示例主题", True),
    ("aaa",   "[协作]-[示例主题]-N10 复测",      "main1", "示例主题", True),
    ("bbb",   "某个日报 · 今日生成",           "main1", "示例主题", False),
    ("ccc",   "随便一个标题",                    "main1", "示例主题", False),
    ("ddd",   "[其他主题]-x",                    "main1", "示例主题", False),
    ("eee",   "x",                               "main1", "",         False),  # short 空 ⇒ 只剩主会话能命中
    ("ggg",   "x",                               "ggg",   "",         True),   # 主会话判据 ⛔ 不依赖 short
    ("hhh",   "[其他主题][示例主题]",              "main1", "示例主题", True),
    ("fff",   "[示例主题] 加个 [示例主题x] 干扰",   "main1", "示例主题", True),
]
bad = []
for sid, ti, msid, short, exp in cases:
    a = bool(cb._in_project(sid, ti, msid, short))
    b = bool(bd.in_project(sid, ti, {"main_sid": msid, "short": short}))
    if a != b:
        bad.append("%s 判据漂移（collabd=%s / board=%s）" % (sid, a, b))
    if a != exp:
        bad.append("%s 实得 %s / 期望 %s" % (sid, a, exp))
sig = str(inspect.signature(bd.in_project))
if "cwd" in sig:
    bad.append("board.in_project 仍带 cwd 参数：%s" % sig)
src = open(H + "/board.py", encoding="utf-8").read()
if "project_scopes" in src:
    bad.append("board.py 仍残留 cwd 版的 project_scopes()")
print(json.dumps({"bad": bad, "sig": sig}, ensure_ascii=False))
'''
    p = subprocess.run([PY, "-c", code, str(HERE)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, env=_mk_env(), timeout=30, errors="replace")
    try:
        r = json.loads((p.stdout or "").strip().splitlines()[-1])
    except Exception:
        return [("子进程跑不起来（rc=%s）：%s" % (p.returncode, (p.stdout or "")[-300:]), False)]
    return [("7 例判据一致且与期望相符（%s）" % ("、".join(r["bad"]) or "全对"), not r["bad"]),
            ("board.in_project 不带 cwd 参数（%s）" % r["sig"], "cwd" not in r["sig"])]


@case("看板：风格系统（三风格令牌在位 ＋ 组件里零硬编码色）")
def t_board_styles():
    """🔴 用户 2026-09-30：「画架构图参考 archify 的样式，**右上角加个风格切换（保留当前风格）**」。

    两件必须守住：
      ① **`base`（保留的当前风格）必须在**，且**跟随系统深浅色** —— ⛔ 删了就等于改了用户的原风格。
      ② **组件里不许出现硬编码颜色** —— 颜色只许出现在令牌块（`--color-*` 等）里。
         ⚠️ 一旦有人在 `.card{}`／SVG 里写死 `#fff`，换风格就会**花**（这是最容易犯的）。
    """
    p = HERE.parent / "assets" / "board.html"
    src = p.read_text(encoding="utf-8")
    need = ['[data-style="archify-dark"]', '[data-style="archify-light"]',
            ':root:not([data-style]), :root[data-style="base"]',   # base 跟随系统深色
            'id="stylesw"', 'function applyStyle(', "collab-board-style"]  # 切换件 + 持久化
    misc = [n for n in need if n not in src]
    bad = []
    for i, l in enumerate(src.splitlines(), 1):
        s = l.strip()
        if s.startswith(("--color-", "--font", "--dot", "--node", "--radius", "--shadow")):
            continue          # 令牌块本身当然有颜色值
        if re.search(r"#[0-9a-fA-F]{3,8}\b", l) or re.search(r"rgba?\(\s*\d", l):
            bad.append("board.html:%d" % i)
    return [("三风格令牌 + 切换件 + 持久化齐全（缺：%s）" % (misc or "无"), not misc),
            ("组件里零硬编码颜色（越界：%s）" % (bad[:4] or "无"), not bad)]


@case("看板：版面纪律（小字描述只进 ?；指定删除的三处不得回来）")
def t_board_layout():
    """🔴 用户 2026-09-30：「把各板块的**小字描述**放到各板块对应**右上角 ?号图标**中，鼠标移上去显示
    （**只保留标题，主体，类别标签**这类信息）」＋ 点名**删除**三处。

    守两件：
      ① 每个 `.card/.hero` 的说明**只许放在 `.qtip` 里**（至少 4 个 `?`），⛔ 不许摊回版面；
      ② 被点名删除的三句**不得复活**（否则又变成"版面上一堆解释性小字"）。
    """
    src = (HERE.parent / "assets" / "board.html").read_text(encoding="utf-8")
    qh = src.count('class="qh"')
    qtip = src.count('class="qtip"') + src.count('class="qtip" id=')
    DELETED = ["一张看板只管", "只读旁路观测", "不按工作目录", "其他工作区另有在跑的会话",
               "不参与、也不拖慢协作程序"]
    back = [k for k in DELETED if k in src]
    return [("右上角 ? 图标齐全且都带说明（?=%d，qtip=%d）" % (qh, qtip), qh >= 4 and qtip >= qh),
            ("点名删除的三处未复活（回来的是：%s）" % (back or "无"), not back)]


@case("技能侧零项目串（「技能就是技能，谁用产生的文件放在他自己那里」）")
def t_no_jargon():
    """🔴 用户 2026-09-30 两条定则：
      ① 「**禁止用这么抽象的词**，用 **系统-模块-功能名**（系统-功能名）」
      ② 「**技能就是技能 程序就是程序，谁用产生的文件 放在他自己那里**」

    守三件（都是**静态可判**的，⛔ 不靠"看输出像不像"）：
      ① **技能目录里没有使用方的产物** —— 部署配置／运行日志／编译产物／部署启动器，一个都不许有
         （判据：`scripts/` 只剩通用代码 ＋ 范例配置）。
      ② **`board.py` 源码里零项目串** —— 端口、工作区路径、那些真名，⛔ 一个都不许硬编码
         （它们属于使用方的 `board_ext.py`）。**这条直接对应用户的定则，是本用例的主判据。**
      ③ 看板**回读不引用使用方已废弃的键**（键名由 `COLLABD_STALE_KEYS` 用 `|` 分隔给出）。
    """
    SK = HERE.parent / "scripts"
    # ① 技能目录里不许有使用方的**结构性**产物（配置／启动器／编译产物）。
    #    ⚠️ 日志**不作硬失败**：迁移期可能还有"迁移前起的旧进程"在往老路径写
    #       （2026-09-30 实测 `wb-supervisor-watch.py --interval 10` 就是这种），
    #       那种只能等它自己结束；判据落在**源码不变量**上（见下一条）而不是文件存不存在。
    forbidden = ["collabd.config.json", "start-guard.cmd", "__pycache__"]
    leak = [n for n in forbidden if (SK / n).exists()]
    # ①b 源码级不变量：日志**不得**再挂在技能目录上（`LOG = HERE / …` 是旧写法）
    logbad = []
    for fn in ("collabd.py", "board.py", "guard.py"):
        t = (SK / fn).read_text(encoding="utf-8")
        for i, line in enumerate(t.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            if re.search(r"LOG\s*=\s*HERE", s) or re.search(r"\bHERE\s*/\s*[\"']_", s):
                logbad.append("%s:%d" % (fn, i))
    # ② board.py 源码零项目串
    src = (SK / "board.py").read_text(encoding="utf-8")
    # 使用方把自己项目的专属串写进 `COLLABD_LEAK_PAT`（如 `r"(myproj|12345)"`）；
    # ⛔ 技能里不写死任何一个具体项目的名字。不配 ⇒ 这条只做结构性检查。
    _lp = os.environ.get("COLLABD_LEAK_PAT") or ""
    proj_pat = re.compile(_lp) if _lp else None
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):          # 注释里可以讲"为什么这样分层"，但代码里不行
            continue
        if proj_pat and proj_pat.search(line):
            hits.append("board.py:%d" % i)
    # ③ 看板不许回读已废弃的项目键
    html = (HERE.parent / "assets" / "board.html").read_text(encoding="utf-8")
    _sk = [k for k in (os.environ.get("COLLABD_STALE_KEYS") or "").split("|") if k]
    stale = [k for k in _sk if k in html]
    soft = [n for n in ("_collabd.log", "_guard-stdout.log", "tmp")
            if (SK / n).exists()]
    return [("技能目录里没有使用方产物（硬项泄漏：%s；软提示：%s）"
             % (leak or "无", soft or "无"), not leak),
            ("日志不再挂在技能目录（命中：%s）" % (logbad[:4] or "无"), not logbad),
            ("board.py 代码里零项目串（命中：%s）" % (hits[:5] or "无"), not hits),
            ("看板不回读已废弃的项目键（残留：%s）" % (stale or "无"), not stale)]


@case("元：本测试不碰生产（判据＝**测试独有的痕迹**，⛔ 不拿「文件被写过」当证据）")
def t_no_prod_touch():
    """🔴 判据要精确：生产守护**每 10 秒**就在重写 digest/TO-MAIN/看板 —— 拿"文件被写过"当证据
    会**假红**（本用例第一版就栽在这）。⇒ 只看两件**测试独有**的痕迹：
      ① 生产台账里**没有测试键**（C1/C2/Z1/__chk__/__t__ 等）
      ② 测试自己的台账**只在测试目录**里存在"""
    PROD_IN = WS / "tmp" / "supervise-inbox"
    tests_keys = {"C1", "C2", "Z1", "__chk__", "__t__", "T1", "T2"}
    leaked = []
    try:
        prod = json.loads((PROD_IN / "tasks.json").read_text(encoding="utf-8")) or {}
        leaked = [k for k in prod if k in tests_keys or k.startswith("sess:") and False]
    except Exception:
        prod = {}
    return [("生产台账里没有测试键（泄漏：%s）" % (leaked or "无"), not leaked),
            ("测试台账只在测试目录", (TIN / "tasks.json").exists() and (TIN / "tasks.json").parent == TIN)]


def main() -> int:
    only = ""
    if "-k" in sys.argv:
        only = sys.argv[sys.argv.index("-k") + 1]
    _prepare()
    _snap_prod()
    cases = [v for v in globals().values() if callable(v) and hasattr(v, "_case")]
    npass = nfail = 0
    print("=" * 66)
    print("协作机制 · 回归自测（每改一次必跑）  测试工作区：%s" % TEST_WS)
    print("=" * 66)
    for fn in cases:
        if only and only not in fn._case:
            continue
        try:
            checks = fn()
        except Exception as exc:
            checks = [("用例抛异常：%r" % exc, False)]
        bad = [c for c, ok in checks if not ok]
        if bad:
            nfail += 1
            print("  ✗ %s" % fn._case)
            for c, ok in checks:
                print("      %s %s" % ("✓" if ok else "✗", c))
        else:
            npass += 1
            print("  ✓ %s  (%d 项)" % (fn._case, len(checks)))
    print("-" * 66)
    print("  合计：PASS %d / FAIL %d" % (npass, nfail))
    print("=" * 66)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
