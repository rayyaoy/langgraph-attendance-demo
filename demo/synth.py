"""合成数据:30 名虚拟员工 × 2026-06 整月的打卡记录和申请单。

四张原始表:employees / calendar / clocks / applications。全部由固定种子生成,
不读取任何真实数据;姓名一律"虚拟员工NN",工号 E901–E930(userid 为小写 e9NN)。
规则参数见 policy.py(示例口径)。

刻意放进去的情况(规则层和人工确认节点都要能处理):
  - 工作日晚走 → 工作日加班(18:30 起,满 30 分钟才记,按 15 分钟向下取整);
    其中有一批 18:40–18:59 下班的,加班不足 30 分钟,不记
  - 周六、端午连休中的周末到岗 → 休息日加班;端午当天(6/19)到岗 → 节假日加班
  - 周日到岗 20 分钟(不足 30 分钟,不记)
  - 迟到、早退、上班未打卡、下班未打卡;e929 每天 09:01–09:05 到岗(在宽限内,不算迟到)
  - 上午半天假抵掉当天迟到;补卡申请批准后补上缺卡,被拒的不补
  - 待批的请假不计入(当天没打卡的照样送人工确认),被拒的请假不计
  - 整天无卡且没有请假 → 可能旷工,送人工确认(系统不自动认定旷工)
  - e930 按日计薪:计薪日按实际出勤日算
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3

MONTH = "2026-06"
SEED = 20260928

HOLIDAYS = {"2026-06-19": "端午节"}                                  # 周五,法定节假日
HOLIDAY_WEEKENDS = {"2026-06-20": "端午节", "2026-06-21": "端午节"}  # 连休中的周末按休息日算

DEPTS = [("运营部", "客服组"), ("技术部", "开发组"), ("市场部", "推广组"), ("行政部", "综合组")]

REASONS = {
    "事假": ["家里老人住院需要陪护", "回老家办理户口手续", "孩子学校开家长会", "房子交接需要到场"],
    "病假": ["发烧38度去医院", "肠胃炎输液", "牙疼拔牙", "身体不适在家休息"],
    "调休": ["用上月加班调休", "调休处理私事"],
    "年假": ["年假出游", "年假陪家人"],
}

# 整天无卡、也没有请假 → 可能旷工(人工确认节点里的两条埋点)
ABSENT = (("e915", "2026-06-11"), ("e916", "2026-06-23"))

# 下班未打卡后提交补卡:(员工, 日期, 补卡申请状态)
MAKEUP = (("e917", "2026-06-16", "已批准"), ("e918", "2026-06-18", "已批准"),
          ("e919", "2026-06-29", "已拒绝"))

GRACE_EMPLOYEE = "e929"     # 每天在宽限时间内到岗
DAILY_PAID = "e930"         # 按日计薪

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees(
  userid TEXT PRIMARY KEY, code TEXT, name TEXT, dept TEXT, team TEXT, title TEXT,
  daily_paid INT, active INT);
CREATE TABLE IF NOT EXISTS calendar(
  date TEXT PRIMARY KEY, day_type TEXT, holiday_name TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS clocks(
  userid TEXT, date TEXT, direction TEXT, time TEXT,
  PRIMARY KEY(userid, date, direction));
CREATE TABLE IF NOT EXISTS applications(
  app_id TEXT PRIMARY KEY, userid TEXT, app_type TEXT, leave_type TEXT,
  start_date TEXT, end_date TEXT, half_day TEXT, direction TEXT, time TEXT,
  hours REAL, status TEXT, reason TEXT);
"""


def month_days(month: str = MONTH) -> list[dt.date]:
    y, m = map(int, month.split("-"))
    d = dt.date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d)
        d += dt.timedelta(days=1)
    return out


def day_type(d: dt.date) -> str:
    if d.isoformat() in HOLIDAYS:
        return "holiday"
    if d.weekday() >= 5:
        return "restday"
    return "workday"


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def generate(conn: sqlite3.Connection, month: str = MONTH, seed: int = SEED) -> dict:
    """生成并写入四张原始表。重复调用结果完全一致(先清空再写)。"""
    rng = random.Random(seed)
    conn.executescript(SCHEMA)
    for t in ("employees", "calendar", "clocks", "applications"):
        conn.execute(f"DELETE FROM {t}")

    days = month_days(month)
    for d in days:
        s = d.isoformat()
        name = HOLIDAYS.get(s) or HOLIDAY_WEEKENDS.get(s)
        conn.execute("INSERT INTO calendar VALUES(?,?,?,?)", (s, day_type(d), name, "synthetic"))

    emps = []
    for i in range(1, 31):
        uid = f"e9{i:02d}"
        dept, team = DEPTS[(i - 1) % len(DEPTS)]
        daily_paid = int(uid == DAILY_PAID)
        title = "经理" if i <= 2 else "组长" if i <= 5 else "兼职助理" if daily_paid else "专员"
        emps.append((uid, uid.upper(), f"虚拟员工{i:02d}", dept, team, title, daily_paid, 1))
    conn.executemany("INSERT INTO employees VALUES(?,?,?,?,?,?,?,?)", emps)

    workdays = [d.isoformat() for d in days if day_type(d) == "workday"]
    apps: list[tuple] = []
    off_days: dict[tuple[str, str], str] = {}   # (uid, date) -> "full"/"am":当天不来或上午不来

    def next_id() -> str:
        return f"AP{len(apps) + 1:04d}"

    def add_leave(uid, ltype, start, end, half, status):
        span = [w for w in workdays if start <= w <= end]
        hours = 4.0 if half else 8.0 * len(span)
        apps.append((next_id(), uid, "请假", ltype, start, end, half, None, None,
                     hours, status, rng.choice(REASONS[ltype])))
        if status != "已拒绝":                  # 待批的请假本人也没来,只是单子还没批
            for w in span:
                off_days[(uid, w)] = half or "full"

    add_leave("e906", "事假", "2026-06-03", "2026-06-03", None, "已批准")
    add_leave("e907", "病假", "2026-06-08", "2026-06-09", None, "已批准")
    add_leave("e908", "调休", "2026-06-12", "2026-06-12", None, "已批准")
    add_leave("e909", "事假", "2026-06-15", "2026-06-15", "am", "已批准")   # 上午假抵迟到
    add_leave("e910", "年假", "2026-06-22", "2026-06-24", None, "已批准")
    add_leave("e911", "事假", "2026-06-25", "2026-06-25", None, "待批")     # 待批:不计入
    add_leave("e912", "病假", "2026-06-26", "2026-06-26", None, "待批")
    add_leave("e913", "事假", "2026-06-10", "2026-06-10", None, "已拒绝")   # 被拒:当天照常打卡
    add_leave("e914", "调休", "2026-06-17", "2026-06-17", "am", "已批准")
    add_leave("e930", "事假", "2026-06-05", "2026-06-05", None, "已批准")   # 按日计薪人员请假

    makeup = {(u, d): st for u, d, st in MAKEUP}
    rows = []
    for (uid, *_rest) in emps:
        heavy_ot = uid <= "e908"                    # 前 8 人加班多一些
        for w in workdays:
            if (uid, w) in ABSENT or off_days.get((uid, w)) == "full":
                continue
            half = off_days.get((uid, w))
            t_in = rng.randint(8 * 60 + 35, 9 * 60 + 5)            # 正常到岗(含宽限内)
            t_out = rng.randint(18 * 60, 18 * 60 + 25)
            r = rng.random()
            if uid == GRACE_EMPLOYEE:
                t_in = rng.randint(9 * 60 + 1, 9 * 60 + 5)         # 每天都在宽限内
            elif half == "am":
                t_in = rng.randint(13 * 60 + 30, 14 * 60)          # 上午请假,下午到
            elif r < 0.06:
                t_in = rng.randint(9 * 60 + 6, 9 * 60 + 50)        # 迟到
            elif r < 0.10:
                t_out = rng.randint(17 * 60, 17 * 60 + 59)         # 早退
            if r >= 0.10 and rng.random() < (0.4 if heavy_ot else 0.08):
                t_out = rng.randint(18 * 60 + 40, 21 * 60)         # 晚走;18:40-18:59 不足 30 分钟
            miss = rng.random()
            if (uid, w) in makeup:
                rows.append((uid, w, "in", _hm(t_in)))            # 下班未打卡,之后提交补卡
                apps.append((next_id(), uid, "补卡", None, w, w, None, "out", _hm(t_out),
                             0.0, makeup[(uid, w)], "忘记打下班卡"))
                continue
            if miss >= 0.02:
                rows.append((uid, w, "in", _hm(t_in)))
            if miss < 0.02 or miss >= 0.04:
                rows.append((uid, w, "out", _hm(t_out)))

    rows += [("e901", "2026-06-13", "in", "09:02"), ("e901", "2026-06-13", "out", "15:53"),  # 周六
             ("e902", "2026-06-20", "in", "08:55"), ("e902", "2026-06-20", "out", "12:14"),  # 连休中的周末
             ("e903", "2026-06-19", "in", "08:40"), ("e903", "2026-06-19", "out", "17:05"),  # 端午当天
             ("e920", "2026-06-19", "in", "09:00"), ("e920", "2026-06-19", "out", "13:31"),  # 端午当天
             ("e921", "2026-06-27", "in", "09:10"), ("e921", "2026-06-27", "out", "11:30"),  # 周六
             ("e922", "2026-06-28", "in", "10:00"), ("e922", "2026-06-28", "out", "10:20")]  # 周日,不足 30 分钟

    conn.executemany("INSERT OR REPLACE INTO clocks VALUES(?,?,?,?)", rows)
    conn.executemany("INSERT INTO applications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", apps)
    conn.commit()
    return {"employees": len(emps), "clocks": len(rows), "applications": len(apps),
            "workdays": len(workdays)}
