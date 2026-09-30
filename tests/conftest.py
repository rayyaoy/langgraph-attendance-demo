import csv
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from demo import synth
from demo.graph import build_graph, open_checkpointer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def work(tmp_path):
    """一个干净的工作目录:合成数据库 + checkpoint 库 + 输出目录。"""
    data_db = tmp_path / "data.db"
    con = sqlite3.connect(data_db)
    synth.generate(con)
    con.close()
    return {"dir": tmp_path, "data_db": str(data_db), "ckpt": tmp_path / "checkpoints.db",
            "out_dir": str(tmp_path / "output")}


@pytest.fixture
def app(work):
    return build_graph(open_checkpointer(work["ckpt"]))


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def node_runs(data_db) -> Counter:
    con = sqlite3.connect(data_db)
    try:
        return Counter(n for (n,) in con.execute("SELECT node FROM node_runs ORDER BY seq"))
    except sqlite3.OperationalError:
        return Counter()
    finally:
        con.close()


def read_monthly(out_dir, month=synth.MONTH) -> dict[str, dict]:
    with (Path(out_dir) / f"月表-{month}.csv").open(encoding="utf-8-sig") as fh:
        return {r["工号"]: r for r in csv.DictReader(fh)}
