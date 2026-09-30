"""故障注入(变异)检查:把 10 处代码或参数逐一改坏,确认测试至少有一个会失败。

每个变异在一份临时拷贝里做,不改动工作区。用法(项目根目录):

    .venv/bin/python eval/mutation_check.py

输出每个变异由哪些测试抓到;有任何变异没被抓到时退出码为 1。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (编号, 名称, 文件, 原文, 改坏后)
MUTATIONS = [
    ("M1", "加班门槛 30 分钟改成 15 分钟", "demo/policy.py",
     "OT_MIN_MINUTES = 30 ", "OT_MIN_MINUTES = 15 "),
    ("M2", "待批的请假也计入", "demo/policy.py",
     'LEAVE_COUNTED_STATUSES = ("已批准",)', 'LEAVE_COUNTED_STATUSES = ("已批准", "待批")'),
    ("M3", "加班不再按 15 分钟向下取整", "demo/policy.py",
     "OT_STEP_MIN = 15 ", "OT_STEP_MIN = 1 "),
    ("M4", "确认节点去掉 interrupt(),直接往下走", "demo/graph.py",
     "answer = interrupt({", "answer = ({"),
    ("M5", "ledger 后的条件边写反", "demo/graph.py",
     'return "ai_reasons" if s.get("use_ai") else "confirm"',
     'return "confirm" if s.get("use_ai") else "ai_reasons"'),
    ("M6", "命令行的 checkpointer 换成内存库(续跑无 checkpoint 可读)", "demo/run.py",
     'open_checkpointer(wd / "checkpoints.db")', 'open_checkpointer(":memory:")'),
    ("M7", "人工答复不再包 {\"decisions\": ...},空字典裸传", "demo/graph.py",
     'Command(resume={"decisions": decisions})', "Command(resume=decisions)"),
    ("M8", "脱敏失效:临时编号直接返回原 userid", "demo/ai_nodes.py",
     "        return self.by_user[uid]", "        return uid"),
    ("M9", "写错的答复编号静默吞掉", "demo/graph.py",
     'tally["无效答复"] = sorted(str(k) for k in raw if str(k) not in decisions)',
     'tally["无效答复"] = []'),
    ("M10", "AI 分支把\"可能旷工\"初判直接写进月表", "demo/graph.py",
     'table = rules.build_monthly(con, s["month"], s.get("decisions", {}))',
     'table = rules.build_monthly(con, s["month"], {**{str(i["rid"]): "确认异常" '
     'for i in s.get("review_items", []) if i.get("ai_verdict") == "可能旷工"}, '
     '**s.get("decisions", {})})'),
]


def _copy(dst: Path) -> None:
    for name in ("demo", "tests", "pytest.ini"):
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, dst / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst / name)


def _run_pytest(cwd: Path) -> tuple[int, list[str]]:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rf", "-p", "no:cacheprovider"],
                       cwd=cwd, capture_output=True, text=True, timeout=600)
    failed = sorted(set(re.findall(r"^FAILED tests/(\w+)\.py", r.stdout, re.M)))
    return r.returncode, failed


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        base.mkdir()
        _copy(base)
        code, failed = _run_pytest(base)
        if code != 0:
            print(f"未改动的代码测试就不过:{failed}")
            return 1
        missed = []
        for mid, name, rel, old, new in MUTATIONS:
            work = Path(tmp) / mid
            work.mkdir()
            _copy(work)
            f = work / rel
            text = f.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"{mid} 变异点在 {rel} 里找不到或不唯一:{old!r}")
                return 1
            f.write_text(text.replace(old, new), encoding="utf-8")
            code, failed = _run_pytest(work)
            caught = code != 0
            if not caught:
                missed.append(mid)
            print(f"{mid:4} {'抓到' if caught else '漏检'}  {name}  <- {', '.join(failed) or '-'}")
        print(f"\n{len(MUTATIONS) - len(missed)}/{len(MUTATIONS)} 个变异被测试抓到")
        return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
