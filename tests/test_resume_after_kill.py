"""用例 1:运行到一半 kill -9,换一个进程从 checkpoint 续跑,结果和一口气跑完的完全一样。"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from conftest import ROOT, cfg, node_runs
from demo import synth
from demo.graph import build_graph, open_checkpointer, start

import sqlite3


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("LLM_API_KEY", "DEMO_SLOW_NODE")}
    env.update(extra)
    return env


def test_kill_mid_run_then_resume(tmp_path):
    cmd = [sys.executable, "-m", "demo.run", "--thread", "t-kill", "--workdir", str(tmp_path), "--auto"]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=_env(DEMO_SLOW_NODE="ledger", DEMO_SLOW_SECONDS="120"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    data_db = tmp_path / "data.db"

    # 等到 ledger 节点已经开始执行(正在睡),再 SIGKILL —— 模拟机器断电/进程被杀
    deadline = time.time() + 60
    while node_runs(data_db)["ledger"] < 1:
        assert proc.poll() is None, proc.stderr.read().decode()
        assert time.time() < deadline, "ledger 节点 60 秒内没启动"
        time.sleep(0.2)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL

    # checkpoint 停在 ledger 之前:rules、reconcile 的结果已落盘,月表还没生成
    app = build_graph(open_checkpointer(tmp_path / "checkpoints.db"))
    snap = app.get_state(cfg("t-kill"))
    assert snap.next == ("ledger",)
    assert "reconcile" in snap.values["stats"]
    assert not (tmp_path / "output").exists()

    # 新进程续跑(不带故障注入)
    r = subprocess.run(cmd[:-1] + ["--resume"], cwd=ROOT, env=_env(), capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    assert app.get_state(cfg("t-kill")).next == ()

    # 已完成的节点没有重跑;只有被杀时正在跑的 ledger 跑了两次
    runs = node_runs(data_db)
    assert runs == {"rules": 1, "reconcile": 1, "ledger": 2, "confirm": 1, "render": 1}

    # 和一口气跑完的基线逐字节一致
    base = tmp_path / "baseline"
    base.mkdir()
    con = sqlite3.connect(base / "data.db")
    synth.generate(con)
    con.close()
    app_b = build_graph(open_checkpointer(base / "checkpoints.db"))
    start(app_b, "t-base", data_db=str(base / "data.db"), out_dir=str(base / "output"),
          month=synth.MONTH, auto=True, llm=None)
    for name in (f"月表-{synth.MONTH}.csv", f"月报-{synth.MONTH}.md"):
        assert (tmp_path / "output" / name).read_bytes() == (base / "output" / name).read_bytes(), name
