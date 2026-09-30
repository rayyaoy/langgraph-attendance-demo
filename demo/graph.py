"""LangGraph 编排:8 个节点、2 条条件边、SQLite checkpoint、interrupt 人工确认。

    rules → reconcile → ledger ─┬─(有模型)→ ai_reasons → ai_judge → confirm ─→ ai_report → render
                                └─(无模型)─────────────────────→ confirm ─→ render

  1. 挂 SqliteSaver,每个节点跑完同步写一个 checkpoint(durability="sync")。进程被杀后,
     用同一个 thread_id 调 invoke(None) 就从最后一个 checkpoint 继续。
  2. 人工确认节点调用 interrupt():图停在 confirm,状态写进 checkpoint;可以隔天、在另一个
     进程里用 Command(resume=...) 给出决定,图接着往下走。
  3. 是否走 AI 分支由条件边按 state["use_ai"] 决定。没有模型时三个 AI 节点整段跳过,
     月度小结用字符串模板。

模型调用函数通过 runtime context 传入,不进 state,所以 checkpoint 里没有模型 client 对象和 key。
"""

from __future__ import annotations

import csv
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from . import rules
from .ai_nodes import (LLM, classify_reasons, employee_names, judge_anomalies, template_report,
                       write_report)

DECISIONS = ("确认异常", "核销")


@dataclass
class Ctx:
    llm: Optional[LLM] = None


class S(TypedDict, total=False):
    month: str
    data_db: str
    out_dir: str
    auto: bool            # True = 不停在人工确认,未决项写进备注
    use_ai: bool
    stats: dict           # 各节点的汇总数字(给月度小结和渲染)
    review_items: list    # 待人工确认的异常(规则提示 + 可选的 AI 初判)
    decisions: dict       # {rid: 确认异常|核销}
    confirmed: bool
    ai_sections: list
    out_paths: list


# ---------------------------------------------------------------- helpers
def _conn(s: S) -> sqlite3.Connection:
    return sqlite3.connect(s["data_db"])


def _enter(s: S, node: str) -> None:
    """节点入口:追加一条运行日志(测试靠它数每个节点被执行了几次),以及一个故障注入点。

    DEMO_SLOW_NODE=<节点名> 会让该节点睡 DEMO_SLOW_SECONDS 秒,测试趁它睡着时 kill -9 进程。
    """
    con = _conn(s)
    con.execute("CREATE TABLE IF NOT EXISTS node_runs(seq INTEGER PRIMARY KEY, node TEXT, pid INT, ts REAL)")
    con.execute("INSERT INTO node_runs(node, pid, ts) VALUES(?,?,?)", (node, os.getpid(), time.time()))
    con.commit()
    con.close()
    if os.environ.get("DEMO_SLOW_NODE") == node:
        time.sleep(float(os.environ.get("DEMO_SLOW_SECONDS", "30")))


def _stats(s: S, key: str, value: Any) -> dict:
    return {**s.get("stats", {}), key: value}


# ---------------------------------------------------------------- 规则节点
def n_rules(s: S) -> dict:
    _enter(s, "rules")
    con = _conn(s)
    # 先清掉上一轮的 AI 结果,避免纯规则模式或模型失败时,旧的判断挂到新数据上
    con.executescript("DROP TABLE IF EXISTS ai_reason_class; DROP TABLE IF EXISTS ai_anomaly_judgment;")
    st = rules.run_rules(con, s["month"])
    con.close()
    return {"stats": {"rules": st}}


def n_reconcile(s: S) -> dict:
    _enter(s, "reconcile")
    con = _conn(s)
    st, review = rules.run_reconcile(con, s["month"])
    con.close()
    return {"stats": _stats(s, "reconcile", st), "review_items": review}


def n_ledger(s: S) -> dict:
    _enter(s, "ledger")
    con = _conn(s)
    st = rules.build_leave_list(con)
    con.close()
    return {"stats": _stats(s, "leave", st)}


# ---------------------------------------------------------------- AI 节点
def n_ai_reasons(s: S, runtime: Runtime[Ctx]) -> dict:
    _enter(s, "ai_reasons")
    con = _conn(s)
    cols = ["app_id", "userid", "leave_type", "days", "reason"]
    leaves = [dict(zip(cols, r)) for r in con.execute(
        "SELECT app_id, userid, leave_type, days, reason FROM leave_list WHERE reason != ''")]
    res = classify_reasons(runtime.context.llm, leaves, employee_names(con))
    update: dict = {}
    if res:
        con.execute("DROP TABLE IF EXISTS ai_reason_class")
        con.execute("CREATE TABLE ai_reason_class(app_id TEXT PRIMARY KEY, ai_category TEXT, "
                    "ai_type_conflict INT)")
        con.executemany("INSERT INTO ai_reason_class VALUES(?,?,?)",
                        [(k, v["category"], int(v["type_conflict"])) for k, v in res.items()])
        dist: dict[str, int] = {}
        for v in res.values():
            dist[v["category"]] = dist.get(v["category"], 0) + 1
        update["stats"] = _stats(s, "ai_reasons", {"分类分布": dist,
                                                   "类型不符": sum(v["type_conflict"] for v in res.values())})
    con.commit()
    con.close()
    return update


def n_ai_judge(s: S, runtime: Runtime[Ctx]) -> dict:
    _enter(s, "ai_judge")
    items = s.get("review_items", [])
    if not items:
        return {}
    con = _conn(s)
    makeup = dict(con.execute("SELECT userid, COUNT(*) FROM applications WHERE app_type='补卡' GROUP BY 1"))
    anom = dict(con.execute("SELECT userid, COUNT(*) FROM reconciled WHERE scope != 'cleared' GROUP BY 1"))
    payload = [{**it, "makeups": makeup.get(it["userid"], 0),
                "anomalies": anom.get(it["userid"], 0)} for it in items]
    res = judge_anomalies(runtime.context.llm, payload)
    if not res:
        con.close()
        return {}
    con.execute("DROP TABLE IF EXISTS ai_anomaly_judgment")
    con.execute("CREATE TABLE ai_anomaly_judgment(rid INT PRIMARY KEY, verdict TEXT, confidence REAL, "
                "reason TEXT, review_status TEXT)")
    con.executemany("INSERT INTO ai_anomaly_judgment VALUES(?,?,?,?,?)",
                    [(rid, v["verdict"], v["confidence"], v["reason"], "待人工复核") for rid, v in res.items()])
    con.commit()
    con.close()
    enriched = [{**it, **{f"ai_{k}": v for k, v in res.get(it["rid"], {}).items()}} for it in items]
    dist: dict[str, int] = {}
    for v in res.values():
        dist[v["verdict"]] = dist.get(v["verdict"], 0) + 1
    return {"review_items": enriched, "stats": _stats(s, "ai_judge", {"判断分布": dist, "有判断的条数": len(res)})}


# ---------------------------------------------------------------- 人工确认节点
def n_confirm(s: S) -> dict:
    """人工确认。interrupt 恢复时 LangGraph 会把本节点从头再执行一遍,
    所以 interrupt() 之前只做读操作,写库放在拿到决定之后。"""
    _enter(s, "confirm")
    items = s.get("review_items", [])
    if not items:
        return {"decisions": {}, "confirmed": True, "stats": _stats(s, "confirm", {"未决": 0})}
    if s.get("auto"):
        return {"decisions": {}, "confirmed": False,
                "stats": _stats(s, "confirm", {"确认异常": 0, "核销": 0, "未决": len(items)})}

    answer = interrupt({
        "question": "下面这些异常没有申请单能抵消,请逐条决定:确认异常(整天无卡的计旷工)或核销(不计)。"
                    "没有回答的条目保持'待人工复核'。",
        "reply_format": {"decisions": {"<rid>": "确认异常|核销"}},
        "options": list(DECISIONS),
        "items": items,
    })

    # 答复外面包一层 {"decisions": ...}:Command(resume={}) 会被当成"没有答复"而再次 interrupt,
    # 而"这批全部先不决定"传的正是空字典,不包一层图会一直停在这里。
    raw = (answer or {}).get("decisions") or {}
    valid = {str(it["rid"]) for it in items}
    decisions = {str(k): v for k, v in raw.items() if str(k) in valid and v in DECISIONS}
    con = _conn(s)
    con.execute("CREATE TABLE IF NOT EXISTS review_decisions(rid TEXT PRIMARY KEY, decision TEXT, ts REAL)")
    con.execute("DELETE FROM review_decisions")
    con.executemany("INSERT INTO review_decisions VALUES(?,?,?)",
                    [(k, v, time.time()) for k, v in decisions.items()])
    has_ai = con.execute("SELECT 1 FROM sqlite_master WHERE name='ai_anomaly_judgment'").fetchone()
    if has_ai and decisions:                     # 纯规则分支没有这张表
        con.executemany("UPDATE ai_anomaly_judgment SET review_status='已人工决定' WHERE rid=?",
                        [(int(k),) for k in decisions])
    con.commit()
    con.close()
    tally = {d: sum(1 for v in decisions.values() if v == d) for d in DECISIONS}
    tally["未决"] = len(items) - len(decisions)
    # 编号或选项写错时要点名,不能悄悄忽略,否则回答的人以为处理过了,其实那条还挂着
    tally["无效答复"] = sorted(str(k) for k in raw if str(k) not in decisions)
    return {"decisions": decisions, "confirmed": True, "stats": _stats(s, "confirm", tally)}


# ---------------------------------------------------------------- 月度小结 + 渲染
def n_ai_report(s: S, runtime: Runtime[Ctx]) -> dict:
    _enter(s, "ai_report")
    sec = write_report(runtime.context.llm, s.get("stats", {}))
    return {"ai_sections": sec} if sec else {}


def n_render(s: S) -> dict:
    _enter(s, "render")
    out = Path(s["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    con = _conn(s)
    table = rules.build_monthly(con, s["month"], s.get("decisions", {}))
    con.close()
    csv_path = out / f"月表-{s['month']}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=rules.MONTHLY_COLS)
        w.writeheader()
        w.writerows(table)
    sections = s.get("ai_sections") or template_report(s.get("stats", {}))
    source = "AI 撰写(要求只引用输入数字)" if s.get("ai_sections") else "规则模板"
    md = [f"# 考勤月报 {s['month']}(合成数据)", "", f"> 正文来源:{source}", ""]
    for sec in sections:
        md += [f"## {sec.get('title', '')}", "", str(sec.get("text", "")), ""]
    md_path = out / f"月报-{s['month']}.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    return {"out_paths": [str(csv_path), str(md_path)]}


# ---------------------------------------------------------------- 图
def after_ledger(s: S) -> str:
    return "ai_reasons" if s.get("use_ai") else "confirm"


def after_confirm(s: S) -> str:
    return "ai_report" if s.get("use_ai") else "render"


def build_graph(checkpointer=None):
    g = StateGraph(S, context_schema=Ctx)
    for name, fn in [("rules", n_rules), ("reconcile", n_reconcile), ("ledger", n_ledger),
                     ("ai_reasons", n_ai_reasons), ("ai_judge", n_ai_judge), ("confirm", n_confirm),
                     ("ai_report", n_ai_report), ("render", n_render)]:
        g.add_node(name, fn)
    g.add_edge(START, "rules")
    g.add_edge("rules", "reconcile")
    g.add_edge("reconcile", "ledger")
    g.add_conditional_edges("ledger", after_ledger, ["ai_reasons", "confirm"])
    g.add_edge("ai_reasons", "ai_judge")
    g.add_edge("ai_judge", "confirm")
    g.add_conditional_edges("confirm", after_confirm, ["ai_report", "render"])
    g.add_edge("ai_report", "render")
    g.add_edge("render", END)
    return g.compile(checkpointer=checkpointer)


def open_checkpointer(path: str | Path) -> SqliteSaver:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn)


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def start(app, thread_id: str, *, data_db: str, out_dir: str, month: str, auto: bool = False,
          llm: Optional[LLM] = None, no_ai: bool = False) -> dict:
    """从头跑一次。传了 llm 且没有 no_ai 才走 AI 分支;这个选择写进 state,续跑时不变。"""
    use_ai = llm is not None and not no_ai
    init: S = {"month": month, "data_db": data_db, "out_dir": out_dir, "auto": auto,
               "use_ai": use_ai, "stats": {}}
    return app.invoke(init, _cfg(thread_id), context=Ctx(llm=llm if use_ai else None), durability="sync")


def resume(app, thread_id: str, *, llm: Optional[LLM] = None, decisions: Optional[dict] = None) -> dict:
    """续跑。decisions=None:崩溃后接着跑;给了 decisions:回答人工确认。"""
    payload = Command(resume={"decisions": decisions}) if decisions is not None else None
    return app.invoke(payload, _cfg(thread_id), context=Ctx(llm=llm), durability="sync")
