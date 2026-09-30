"""用例 3:没有 API key → 条件边走纯规则分支,AI 节点一个都不执行,月表数字和独立重算的一致。

"独立重算"= 测试里不调用 demo.rules、也不读 demo.policy,直接从原始打卡/申请表按示例口径
再算一遍(口径数值在下面写死),再加上合成数据里埋点的手算期望值(每一条都能对着 synth.py 核)。
"""

import sqlite3
from collections import defaultdict

from conftest import node_runs, read_monthly
from demo import synth
from demo.ai_nodes import llm_from_env
from demo.graph import start

WORK_START, WORK_END = 9 * 60, 18 * 60     # 09:00-18:00
LATE_AFTER = WORK_START + 5                # 宽限 5 分钟
OT_FROM = WORK_END + 30                    # 18:30 起算
OT_MIN, OT_STEP = 30, 15                   # 满 30 分钟才记,按 15 分钟向下取整


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _independent_workday_ot(con):
    """工作日下班卡晚于 18:30 的部分:不足 30 分钟记 0,否则按 15 分钟向下取整折小时,逐日求和。"""
    out = defaultdict(float)
    for uid, t in con.execute(
            "SELECT c.userid, c.time FROM clocks c JOIN calendar d ON d.date=c.date "
            "WHERE d.day_type='workday' AND c.direction='out'"):
        extra = _mins(t) - OT_FROM
        if extra >= OT_MIN:
            out[uid.upper()] += round(extra // OT_STEP * OT_STEP / 60, 2)
    return {k: round(v, 2) for k, v in out.items()}


def _independent_late(con):
    """工作日上班卡晚于 09:05,且当天没有已批准的整天假或上午半天假。"""
    covered = set()
    for uid, b, e in con.execute(
            "SELECT userid, start_date, end_date FROM applications "
            "WHERE app_type='请假' AND status='已批准'"):
        for (d,) in con.execute("SELECT date FROM calendar WHERE date BETWEEN ? AND ?", (b, e)):
            covered.add((uid, d))
    out = defaultdict(int)
    for uid, d, t in con.execute(
            "SELECT c.userid, c.date, c.time FROM clocks c JOIN calendar k ON k.date=c.date "
            "WHERE k.day_type='workday' AND c.direction='in'"):
        if _mins(t) > LATE_AFTER and (uid, d) not in covered:
            out[uid.upper()] += 1
    return out


def test_no_key_goes_rule_branch_and_numbers_are_right(work, app, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    llm = llm_from_env()
    assert llm is None

    final = start(app, "t-nokey", data_db=work["data_db"], out_dir=work["out_dir"],
                  month=synth.MONTH, auto=True, llm=llm)

    # 分支:走的是纯规则,AI 节点零执行,库里没有任何 AI 产物
    assert final["use_ai"] is False
    runs = node_runs(work["data_db"])
    assert runs == {"rules": 1, "reconcile": 1, "ledger": 1, "confirm": 1, "render": 1}
    con = sqlite3.connect(work["data_db"])
    tables = {t for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"ai_reason_class", "ai_anomaly_judgment"}
    report = (work["dir"] / "output" / f"月报-{synth.MONTH}.md").read_text(encoding="utf-8")
    assert "正文来源:规则模板" in report

    m = read_monthly(work["out_dir"])
    assert len(m) == 30

    # 独立重算:工作日加班、迟到次数,30 人逐个比
    ot = _independent_workday_ot(con)
    late = _independent_late(con)
    assert sum(1 for v in ot.values() if v) >= 10          # 数据里确实有足够多的加班可比
    for code, row in m.items():
        assert float(row["工作日加班h"]) == ot.get(code, 0.0), code
        assert int(row["迟到"]) == late.get(code, 0), code

    # 应出勤:2026-06 共 21 个工作日(端午 6/19 为节假日)
    for code, row in m.items():
        assert row["应出勤日"] == "21", code

    f = lambda code, col: float(m[code][col])  # noqa: E731
    expected = {
        ("E906", "事假日"): 1, ("E906", "计薪日"): 20,
        ("E907", "病假日"): 2, ("E907", "计薪日"): 20,              # 病假每天扣 0.5
        ("E908", "调休h"): 8, ("E908", "计薪日"): 21,               # 调休不扣
        ("E909", "事假日"): 0.5, ("E909", "计薪日"): 20.5,          # 上午半天假
        ("E910", "带薪假日"): 3, ("E910", "计薪日"): 21,            # 年假不扣
        ("E911", "事假日"): 0, ("E911", "计薪日"): 21,              # 待批的请假不计入
        ("E912", "病假日"): 0,
        ("E913", "事假日"): 0, ("E913", "计薪日"): 21,              # 被拒的请假不计入
        ("E914", "调休h"): 4,
        ("E930", "出勤日"): 20, ("E930", "计薪日"): 20,             # 按日计薪:只看出勤日
        ("E930", "事假日"): 1,
        ("E929", "迟到"): 0,                                          # 每天 09:01-09:05 到,在宽限内
        ("E903", "节假日加班h"): 8.25,  # 08:40-17:05 = 505 分钟 → 495
        ("E920", "节假日加班h"): 4.5,   # 09:00-13:31 = 271 分钟 → 270
        ("E901", "休息日加班h"): 6.75,  # 09:02-15:53 = 411 分钟 → 405
        ("E902", "休息日加班h"): 3.25,  # 端午放假期间的周六按休息日:199 分钟 → 195
        ("E921", "休息日加班h"): 2.25,  # 140 分钟 → 135
        ("E922", "休息日加班h"): 0,     # 20 分钟,不足 30 分钟
        ("E917", "补卡"): 1, ("E919", "补卡"): 0,                   # 被拒的补卡不算
        ("E915", "旷工日"): 0, ("E916", "旷工日"): 0,               # 没人确认 → 系统不认定旷工
    }
    for (code, col), want in expected.items():
        assert f(code, col) == want, (code, col, m[code][col])
    assert "06-11整天无卡待人工复核" in m["E915"]["备注"]
    assert "06-29下班未打卡待人工复核" in m["E919"]["备注"]
    assert "06-25事假待批" in m["E911"]["备注"]
    assert "06-25整天无卡待人工复核" in m["E911"]["备注"]
    con.close()
