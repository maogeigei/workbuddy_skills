# -*- coding: utf-8 -*-
"""guard —— **守护程序**：保证「协作程序」与「监督程序」**一直在运行**。

🔴 架构（2026-09-29 用户定案）：**监督程序与协作程序应该一直运行，用守护程序去保护** —— 本件即那个守护程序。
职责只有一件：**拉起 ＋ 看护 ＋ 挂了重拉 ＋ 单例**。

⛔ 边界（红线）：
  · ⛔ 不开新会话、⛔ 不派活（不写排期）—— 那是主会话／自动化的事
  · ⛔ 不读口令、⛔ 不联网、⛔ 不写宿主库、⛔ 不碰用户的会话
  · 全静默（只写自己的日志）；fail-safe：本程序异常 ⇒ 只退出自己，⛔ 不影响 WorkBuddy

⚠️ **起法（实测约束，⛔ 别从会话里起）**：
  · ⛔ 从 WorkBuddy 会话／工具调用里起的进程，**调用一结束就被回收**（2026-09-29 当场实测：心跳停在调用结束那一秒，ps 也查不到）
  · ⛔ `schtasks`（计划任务）被本机安全策略**硬拦**，且明令不得绕过
  · ✅ 正确起法：**在独立窗口里跑一次**（`python guard.py`，或用同目录的 `启动守护.cmd`），
       或把它的快捷方式放进「启动」文件夹（`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup`）⇒ 开机自启
  ⇒ 由**会话之外**起的进程**不挂在任何 WorkBuddy 会话下** ⇒ 不会被回收。

用法：
  python guard.py            # 常驻守护（前台；独立窗口里跑）
  python guard.py --status   # 只报当前状态
  python guard.py --stop     # 让守护程序退出（写停止标志；子程序由守护收尾时一并结束）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
def _cfg() -> dict:
    """读部署配置（与 collabd.py 同一份）—— 🔴 **inbox 必须两边一致**，否则两个程序不在同一个
    inbox 工作（自测挖出：guard 原先把它硬编码成 `tmp/supervise-inbox`，与配置里的 `inbox` 可能不同）。"""
    p = Path(os.environ.get("COLLABD_CONFIG") or (Path(__file__).resolve().parent / "collabd.config.json"))
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_C = _cfg()
_ws_env = os.environ.get("COLLABD_WS") or ""
WS = Path(_ws_env) if _ws_env else (Path(_C["workspace"]) if _C.get("workspace") else Path.cwd())
INBOX = (WS / str(_C.get("inbox") or "tmp/supervise-inbox"))
LOG = INBOX / "guard.log"
STOP = INBOX / "guard.stop"
PIDF = INBOX / "guard.json"
COLLABD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collabd.py")
PY = sys.executable
CHECK_EVERY = 15          # 秒：看护周期
GOALF = INBOX / "goal.json"           # 已确认的目标
GOALP = INBOX / "goal.pending.json"   # **待确认**的目标（用户还没点头）
GOAL_NAME = "目标守护进程"             # 对外正式名（文件名仍 guard.py，⛔ 不为改名掀引用）
STALE = 3 * CHECK_EVERY   # 秒：心跳过期 ⇒ 判前一个守护已死


def log(m: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), m))
    except Exception:
        pass


def _shutdown(kids, wait_s: int = 20) -> None:
    """**优雅收尾**：先让子程序**自己退出**（它们每轮看停止标志／守护心跳），等不到才兜底结束。"""
    for k in kids:
        for _ in range(wait_s):
            if not k.alive():
                break
            time.sleep(1)
        if k.alive():
            log("%s 未在 %d 秒内优雅退出 ⇒ 兜底结束" % (k.name, wait_s))
            try:
                k.p.terminate()
            except Exception:
                pass
        else:
            log("%s 已优雅退出" % k.name)


class Child:
    """一个被看护的子程序（同一个文件的不同模式）。"""

    def __init__(self, name: str, args: list):
        self.name, self.args, self.p = name, args, None

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def ensure(self) -> None:
        if self.alive():
            return
        try:
            self.p = subprocess.Popen([PY, "-u", COLLABD] + self.args, cwd=str(HERE),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      close_fds=True, creationflags=0x00000008,  # DETACHED_PROCESS
                                      env=dict(os.environ, COLLABD_GUARDED="1"))  # 打标：子程序据此做优雅退出
            log("%s 已拉起 pid=%s" % (self.name, getattr(self.p, "pid", "?")))
        except Exception as e:
            log("%s 拉起失败：%s" % (self.name, e))


def _stale() -> bool:
    """用"心跳是否过期"判前一个守护是否已死 —— ⛔ 不依赖任何进程 API、⛔ 不自造锁协议。"""
    try:
        d = json.loads(PIDF.read_text(encoding="utf-8"))
        return (time.time() - float(d.get("ts") or 0)) > STALE
    except Exception:
        return True


def main() -> int:
    if "--goal" in sys.argv:                 # 声明/改写**任务目标**（一句话）
        def _ga(k, dv=""):
            return sys.argv[sys.argv.index(k) + 1] if (k in sys.argv and sys.argv.index(k) + 1 < len(sys.argv)) else dv

        t = _ga("--goal").strip()
        if not t:
            print("用法：guard.py --goal \"<一句话任务目标>\"  （直接声明；推荐先 --propose 再 --confirm）")
            return 2
        try:
            cur = {}
            if (INBOX / "goal.json").exists():
                cur = json.loads((INBOX / "goal.json").read_text(encoding="utf-8")) or {}
            cur["title"] = t
            cur["declared_at"] = time.strftime("%Y-%m-%dT%H:%M")
            INBOX.mkdir(parents=True, exist_ok=True)
            (INBOX / "goal.json").write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print("写目标失败：%s" % e)
            return 2
        print("OK 已声明任务目标：%s" % t)
        return 0
    if "--propose" in sys.argv:              # ① 用户说明目标 ⇒ 落成**待确认**，并回述
        def _gp(k, dv=""):
            return sys.argv[sys.argv.index(k) + 1] if (k in sys.argv and sys.argv.index(k) + 1 < len(sys.argv)) else dv

        raw = _gp("--propose").strip()
        if not raw:
            print("用法：guard.py --propose \"<用户对目标的原话>\"")
            return 2
        rec = {"title": raw, "proposed_at": time.strftime("%Y-%m-%dT%H:%M"),
               "confirmed": False,
               "_下一步": "把这份理解回述给用户 ⇒ 用户点头后跑 --confirm（之后才允许启动）"}
        try:
            INBOX.mkdir(parents=True, exist_ok=True)
            GOALP.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print("写待确认目标失败：%s" % e)
            return 2
        print("⏳ 已记录**待确认**目标（%s）：\n    %s\n"
              "   ⇒ 请把理解回述给用户，用户确认后再跑： guard.py --confirm" % (GOAL_NAME, raw))
        return 0
    if "--confirm" in sys.argv:              # ② 用户点头 ⇒ 转正（**这一步之后才允许启动**）
        try:
            rec = json.loads(GOALP.read_text(encoding="utf-8"))
        except Exception:
            print("没有待确认的目标（`%s` 不存在）⇒ 先跑 --propose \"<用户原话>\"" % GOALP)
            return 2
        rec["confirmed"] = True
        rec["confirmed_at"] = time.strftime("%Y-%m-%dT%H:%M")
        rec["confirmed_by"] = "用户"
        rec.pop("_下一步", None)
        try:
            GOALF.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            GOALP.unlink()
        except Exception as e:
            print("写已确认目标失败：%s" % e)
            return 2
        print("✅ 目标已确认：%s\n   ⇒ 现在可以启动%s了（独立窗口跑 start-guard.cmd，或用宿主后台机制起）"
              % (rec.get("title"), GOAL_NAME))
        return 0
    if "--status" in sys.argv:
        try:
            print(json.dumps(json.loads(PIDF.read_text(encoding="utf-8")), ensure_ascii=False, indent=1))
        except Exception:
            print("守护程序未在运行（无 %s）" % PIDF)
        return 0
    if "--stop" in sys.argv:
        try:
            STOP.parent.mkdir(parents=True, exist_ok=True)
            STOP.write_text("stop", encoding="utf-8")
            print("已写停止标志：%s" % STOP)
        except Exception as e:
            print("写停止标志失败：%s" % e)
        return 0

    # 🔴 2026-09-29 用户定案（新逻辑）：**用户说明目标 ⇒ 机制理解并回述 ⇒ 与用户确认 ⇒ 才允许启动**。
    #    闸 ①：还有"待确认"的目标 ⇒ ⛔ 拒绝启动（不拿没确认过的东西当运行中心）
    if (INBOX / "goal.pending.json").exists():
        try:
            _p = json.loads(GOALP.read_text(encoding="utf-8"))
            _pt = str(_p.get("title") or "")
        except Exception:
            _pt = ""
        print("⛔ 拒绝启动：**目标还没和用户确认**（待确认内容：%s）\n"
              "   流程：把理解回述给用户 ⇒ 用户点头 ⇒ 跑 `guard.py --confirm` ⇒ 再启动。\n"
              "   ⛔ 未经确认的目标不得作为运行中心。" % (_pt[:80] or "(空)"))
        return 4
    # 🔴 闸 ②：**没有已确认目标就拒绝启动** —— 机制的一切判定（需求是否完成／派活／心跳）
    #    都围绕目标；没目标 ⇒ 不知道该做什么，宁可**不开**（⛔ 不空转）。
    try:
        _goal = json.loads((INBOX / "goal.json").read_text(encoding="utf-8")) if (INBOX / "goal.json").exists() else {}
    except Exception:
        _goal = {}
    if not str((_goal or {}).get("title") or "").strip():
        print("⛔ 拒绝启动：**没有任务目标** —— 这套机制围绕目标运行，无目标就不知道在为什么跑。\n"
              "   两种声明方式：\n"
              "     ① 一句话写入： <python> guard.py --goal \"<任务目标>\"\n"
              "     ② 手写文件：  %s\n"
              "        （至少要有 title；可另附 acceptance_doc / taskgraph / lines）" % (INBOX / "goal.json"))
        return 3
    if PIDF.exists() and not _stale():
        try:
            old = json.loads(PIDF.read_text(encoding="utf-8"))
        except Exception:
            old = {}
        print("已有守护程序在跑（pid=%s，心跳 %ss 前）⇒ 本进程退出"
              % (old.get("pid"), int(time.time() - float(old.get("ts") or 0))))
        return 0
    try:
        STOP.unlink()
    except Exception:
        pass

    info = {"pid": os.getpid(), "since": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": time.time(),
            "ws": str(WS)}
    kids = [Child("协作程序", []), Child("监督程序", ["--supervise"])]
    log("%s 启动 pid=%d ws=%s 目标=%s" % (GOAL_NAME, os.getpid(), WS, _goal.get("title")))
    # 启动对账：收编"队列开启前就已在跑的棒" ＋ 标出僵尸（不投递、幂等）
    # fail-safe：超时 30 秒、吞错、不显窗（CREATE_NO_WINDOW）
    try:
        subprocess.run([PY, "-u", COLLABD, "--reconcile"], cwd=str(HERE),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                       creationflags=0x08000000)
        log("启动对账已跑")
    except Exception as e:
        log("启动对账失败（不影响启动）：%s" % e)
    n = 0
    while True:
        if STOP.exists():
            log("收到停止标志 ⇒ 守护程序退出")
            break
        for k in kids:
            k.ensure()
        info["ts"] = time.time()
        info["children"] = {"%s" % k.name: ("alive" if k.alive() else "dead") for k in kids}
        try:
            PIDF.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        n += 1
        if n % 20 == 0:
            log("看护中 %s" % " | ".join("%s=%s" % (k.name, "活" if k.alive() else "死") for k in kids))
        try:
            time.sleep(CHECK_EVERY)
        except KeyboardInterrupt:              # Ctrl+C 也要走**优雅收尾**（⛔ 不能直接死掉丢下孩子）
            log("收到 Ctrl+C ⇒ 优雅收尾")
            break

    _shutdown(kids)                        # 🔴 守护关 ⇒ 两个程序**优雅退出**（等它们自己退，再兜底）
    try:
        PIDF.unlink()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        log("fatal %s" % e)
        sys.exit(0)
