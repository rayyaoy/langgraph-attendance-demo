"""命令行入口(在项目根目录用 .venv/bin/python -m demo.run 运行)。

  --thread june                    造合成数据并从头跑;有 LLM_API_KEY 走 AI 分支,否则纯规则
  --thread june --no-ai            强制纯规则分支
  --thread june --auto             不停在人工确认,未决项写进备注
  --thread june --resume           进程被杀后,从最后一个 checkpoint 续跑
  --thread june --status           看这个线程停在哪个节点、在等人确认什么
  --thread june --decide '{"123":"确认异常","456":"核销"}'   回答人工确认,图继续往下走
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from . import synth
from .ai_nodes import llm_from_env
from .graph import build_graph, open_checkpointer, resume, start


def _brief(result: dict) -> dict:
    """只挑能公开打印的字段:节点产物路径、统计、待确认项。"""
    out = {"out_paths": result.get("out_paths"), "use_ai": result.get("use_ai"),
           "stats": result.get("stats")}
    if "__interrupt__" in result:
        out["waiting_for_human"] = [i.value for i in result["__interrupt__"]]
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="LangGraph 考勤核算 demo(只用合成数据)")
    ap.add_argument("--thread", required=True)
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--month", default=synth.MONTH)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--auto", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--resume", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--decide")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    wd = Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    data_db, out_dir = str(wd / "data.db"), str(wd / "output")
    app = build_graph(open_checkpointer(wd / "checkpoints.db"))
    llm = llm_from_env()
    cfg = {"configurable": {"thread_id": a.thread}}

    if a.status:
        snap = app.get_state(cfg)
        waiting = [i.value for t in snap.tasks for i in t.interrupts]
        print(json.dumps({"next": list(snap.next), "waiting_for_human": waiting},
                         ensure_ascii=False, indent=2))
        return
    if a.resume:
        result = resume(app, a.thread, llm=llm)
    elif a.decide:
        result = resume(app, a.thread, llm=llm, decisions=json.loads(a.decide))
    else:
        con = sqlite3.connect(data_db)
        print("合成数据:", json.dumps(synth.generate(con, a.month), ensure_ascii=False), file=sys.stderr)
        con.close()
        result = start(app, a.thread, data_db=data_db, out_dir=out_dir, month=a.month,
                       auto=a.auto, llm=llm, no_ai=a.no_ai)
    bad = (result.get("stats") or {}).get("confirm", {}).get("无效答复")
    if bad:
        print(f"警告:以下答复编号不存在或选项不合法,已忽略:{bad}", file=sys.stderr)
    print(json.dumps(_brief(result), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
