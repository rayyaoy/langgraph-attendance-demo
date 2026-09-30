"""用例 4:注入假模型走 AI 分支 —— 验证分支切换、AI 初判挂到待确认项上、发给模型的内容
不含员工 userid/工号/姓名;以及模型返回字段类型不对时降级为规则结果、记日志、不抛异常。
不联网、不需要 key。"""

import json
import logging
import sqlite3

from conftest import node_runs
from demo import synth
from demo.graph import build_graph, open_checkpointer, resume, start


class FakeLLM:
    """按提示词第一行的"任务:"分辨是哪一个调用,返回格式正确的结果。"""

    def __init__(self):
        self.prompts = []

    def __call__(self, system, user, max_tokens=2000):
        self.prompts.append(system + "\n" + user)
        data = json.loads(user)
        if system.startswith("任务:请假理由分类"):
            return {"items": [{"id": i["id"], "category": "家庭", "type_conflict": False} for i in data]}
        if system.startswith("任务:打卡异常初步判断"):
            return {"items": [{"id": i["id"], "verdict": "可能旷工" if i["kind"] == "整天无卡" else "可能忘记打卡",
                               "confidence": 0.7, "reason": "假模型"} for i in data]}
        return {"sections": [{"title": "整体概况", "text": f"异常 {data['reconcile']['异常总数']} 条"}]}


def _rules_only_csv(tmp_dir):
    """同一份合成数据走纯规则分支、同样不做人工决定,返回月表字节。"""
    base = tmp_dir / "rules_only"
    base.mkdir()
    con = sqlite3.connect(base / "data.db")
    synth.generate(con)
    con.close()
    app_b = build_graph(open_checkpointer(base / "checkpoints.db"))
    start(app_b, "t-rules", data_db=str(base / "data.db"), out_dir=str(base / "output"),
          month=synth.MONTH, auto=False, llm=None)
    resume(app_b, "t-rules", decisions={})
    return (base / "output" / f"月表-{synth.MONTH}.csv").read_bytes()


def test_ai_branch_with_fake_llm(work, app):
    llm = FakeLLM()
    res = start(app, "t-ai", data_db=work["data_db"], out_dir=work["out_dir"],
                month=synth.MONTH, auto=False, llm=llm)
    items = res["__interrupt__"][0].value["items"]
    assert all("ai_verdict" in i for i in items)
    assert {i["ai_verdict"] for i in items if i["kind"] == "整天无卡"} == {"可能旷工"}

    final = resume(app, "t-ai", llm=llm, decisions={})
    assert final["use_ai"] is True
    runs = node_runs(work["data_db"])
    for n in ("ai_reasons", "ai_judge", "ai_report"):
        assert runs[n] == 1, n
    report = (work["dir"] / "output" / f"月报-{synth.MONTH}.md").read_text(encoding="utf-8")
    assert "正文来源:AI 撰写" in report

    sent = "\n".join(llm.prompts)
    assert len(llm.prompts) == 3
    con = sqlite3.connect(work["data_db"])
    ids = [x for row in con.execute("SELECT userid, code, name FROM employees") for x in row]
    con.close()
    assert len(ids) == 90
    leaked = [x for x in ids if x in sent]
    assert not leaked, leaked                       # 员工 userid / 工号 / 姓名都不能发出去
    assert "虚拟员工" not in sent

    # AI 只产出建议和正文:同样的人工决定下,月表和纯规则分支逐字节相同
    ai_csv = (work["dir"] / "output" / f"月表-{synth.MONTH}.csv").read_bytes()
    assert _rules_only_csv(work["dir"]) == ai_csv


class WrongTypesLLM:
    """返回的是 JSON,但字段类型不对。只有一条初判格式完全正确。"""

    def __init__(self):
        self.good_rid = None

    def __call__(self, system, user, max_tokens=2000):
        data = json.loads(user)
        if system.startswith("任务:请假理由分类"):
            return {"items": "家庭"}                              # items 不是列表
        if system.startswith("任务:打卡异常初步判断"):
            rids = [i["id"] for i in data]
            self.good_rid = rids[2]
            return {"items": [
                {"id": "abc", "verdict": "可能旷工", "confidence": 0.9, "reason": "x"},   # id 不是数字
                {"id": rids[0], "verdict": "可能旷工", "confidence": "high", "reason": "x"},  # confidence 是字符串
                {"id": rids[1], "verdict": "可能旷工", "confidence": 0.8, "reason": ["x"]},  # reason 是列表
                {"id": rids[2], "verdict": "可能忘记打卡", "confidence": 0.6, "reason": "只缺一次"},
                "不是对象",
            ]}
        return {"sections": "本月一切正常"}                        # sections 不是列表


def test_wrong_types_from_model_fall_back_to_rules(work, app, caplog):
    llm = WrongTypesLLM()
    with caplog.at_level(logging.WARNING, logger="demo.ai_nodes"):
        res = start(app, "t-bad", data_db=work["data_db"], out_dir=work["out_dir"],
                    month=synth.MONTH, auto=False, llm=llm)
        items = res["__interrupt__"][0].value["items"]
        final = resume(app, "t-bad", llm=llm, decisions={})

    # 只有格式正确的那一条挂上了初判,其余保持规则提示
    judged = [i for i in items if "ai_verdict" in i]
    assert [i["rid"] for i in judged] == [llm.good_rid]
    assert all("rule_hint" in i for i in items)

    # 分类整批丢弃 → 没有写分类表;月度小结降级为规则模板
    con = sqlite3.connect(work["data_db"])
    tables = {t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "ai_reason_class" not in tables
    assert "ai_reasons" not in final["stats"]
    report = (work["dir"] / "output" / f"月报-{synth.MONTH}.md").read_text(encoding="utf-8")
    assert "正文来源:规则模板" in report

    # 每一种坏数据都记了日志
    text = "\n".join(r.getMessage() for r in caplog.records)
    for needle in ("items 应为列表", "id 不是已知的整数编号", "confidence 不是 0-1 的数",
                   "reason 不是字符串", "不是对象", "sections 应为非空列表"):
        assert needle in text, needle

    # 月表不受影响
    csv_bytes = (work["dir"] / "output" / f"月表-{synth.MONTH}.csv").read_bytes()
    assert _rules_only_csv(work["dir"]) == csv_bytes
