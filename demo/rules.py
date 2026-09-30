"""规则层:纯确定性计算,不调用模型。

所有规则参数来自 policy.py(示例口径,可按实际制度修改),这里只写计算逻辑。

每个函数都先删掉再重建自己负责的表:节点可能因为续跑或 interrupt 恢复被重复执行,
结果必须和只执行一次完全一样(幂等)。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from . import policy as P


def _m(hhmm: str | None) -> int | None:
    if not hhmm:
        return None
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


START = _m(P.WORK_START)
END = _m(P.WORK_END)
LATE_AFTER = START + P.LATE_GRACE_MIN
OT_FROM = END + P.OT_GAP_MIN


def ot_hours(minutes: int) -> float:
    """加班分钟 → 小时:不足门槛记 0,其余按步长向下取整。"""
    if minutes < P.OT_MIN_MINUTES:
        return 0.0
    return round(minutes // P.OT_STEP_MIN * P.OT_STEP_MIN / 60, 2)


# ---------------------------------------------------------------- rules
def run_rules(conn: sqlite3.Connection, month: str) -> dict:
    """打卡 → 每人每日一行 + 原始异常 + 加班。这一步不看申请单(那是 reconcile 的事)。"""
    conn.executescript("""
        DROP TABLE IF EXISTS daily; DROP TABLE IF EXISTS anomalies; DROP TABLE IF EXISTS ot_rows;
        CREATE TABLE daily(userid TEXT, date TEXT, day_type TEXT, first_in TEXT, last_out TEXT,
                           PRIMARY KEY(userid, date));
        CREATE TABLE anomalies(aid INTEGER PRIMARY KEY, userid TEXT, date TEXT, kind TEXT, minutes INT);
        CREATE TABLE ot_rows(userid TEXT, date TEXT, ot_type TEXT, minutes INT, hours REAL,
                             PRIMARY KEY(userid, date, ot_type));
    """)
    emps = [u for (u,) in conn.execute("SELECT userid FROM employees WHERE active=1 ORDER BY userid")]
    cal = conn.execute("SELECT date, day_type FROM calendar WHERE date LIKE ? ORDER BY date",
                       (month + "%",)).fetchall()
    clocks: dict[tuple, dict] = defaultdict(dict)
    for u, d, direction, tm in conn.execute(
            "SELECT userid, date, direction, time FROM clocks WHERE date LIKE ?", (month + "%",)):
        clocks[(u, d)][direction] = tm

    anomalies, ot, daily = [], [], []
    for uid in emps:
        for d, dtype in cal:
            p = clocks.get((uid, d), {})
            tin, tout = _m(p.get("in")), _m(p.get("out"))
            if dtype == "workday":
                if tin is None and tout is None:
                    anomalies.append((uid, d, "整天无卡", 0))
                else:
                    if tin is None:
                        anomalies.append((uid, d, "上班未打卡", 0))
                    elif tin > LATE_AFTER:
                        anomalies.append((uid, d, "迟到", tin - START))
                    if tout is None:
                        anomalies.append((uid, d, "下班未打卡", 0))
                    elif tout < END:
                        anomalies.append((uid, d, "早退", END - tout))
                # 工作日加班只看下班卡
                if tout is not None and tout > OT_FROM:
                    h = ot_hours(tout - OT_FROM)
                    if h:
                        ot.append((uid, d, "workday", tout - OT_FROM, h))
            elif tin is not None and tout is not None and tout > tin:
                h = ot_hours(tout - tin)
                if h:
                    ot.append((uid, d, dtype, tout - tin, h))    # restday / holiday
            if p or dtype == "workday":
                daily.append((uid, d, dtype, p.get("in"), p.get("out")))

    conn.executemany("INSERT INTO daily VALUES(?,?,?,?,?)", daily)
    conn.executemany("INSERT INTO anomalies(userid,date,kind,minutes) VALUES(?,?,?,?)", anomalies)
    conn.executemany("INSERT INTO ot_rows VALUES(?,?,?,?,?)", ot)
    conn.commit()
    kinds: dict[str, int] = defaultdict(int)
    for a in anomalies:
        kinds[a[2]] += 1
    return {"员工数": len(emps), "每日行数": len(daily), "原始异常": len(anomalies),
            "异常分布": dict(sorted(kinds.items())), "加班记录": len(ot)}


# ---------------------------------------------------------------- reconcile
def run_reconcile(conn: sqlite3.Connection, month: str) -> tuple[dict, list[dict]]:
    """用申请单抵掉异常。返回 (统计, 待人工确认清单)。

    整天假抵掉当天全部异常;上午半天假抵掉迟到和上班未打卡;批准的补卡抵掉对应的缺卡。
    抵不掉的"整天无卡/未打卡"进待确认清单(scope=judge);
    抵不掉的迟到、早退直接按次数计(scope=count),不打扰人工。
    """
    conn.executescript("""
        DROP TABLE IF EXISTS reconciled;
        CREATE TABLE reconciled(aid INTEGER PRIMARY KEY, userid TEXT, date TEXT, kind TEXT,
                                minutes INT, cleared_by TEXT, scope TEXT);
    """)
    workdays = {d for (d,) in conn.execute(
        "SELECT date FROM calendar WHERE day_type='workday' AND date LIKE ?", (month + "%",))}
    leave_cover: dict[tuple, tuple] = {}
    makeup: dict[tuple, str] = {}
    for app_id, uid, app_type, start, end, half, direction, status in conn.execute(
            "SELECT app_id, userid, app_type, start_date, end_date, half_day, direction, status "
            "FROM applications"):
        if app_type == "请假" and status in P.LEAVE_COUNTED_STATUSES:
            for d in workdays:
                if start <= d <= end:
                    leave_cover[(uid, d)] = (app_id, half or "full")
        elif app_type == "补卡" and status == "已批准":
            kind = "下班未打卡" if direction == "out" else "上班未打卡"
            makeup[(uid, start, kind)] = app_id

    rows, review = [], []
    for aid, uid, d, kind, mins in conn.execute(
            "SELECT aid, userid, date, kind, minutes FROM anomalies ORDER BY aid"):
        by = None
        lv = leave_cover.get((uid, d))
        if lv and (lv[1] == "full" or (lv[1] == "am" and kind in ("迟到", "上班未打卡"))):
            by = lv[0]
        elif (uid, d, kind) in makeup:
            by = makeup[(uid, d, kind)]
        if by:
            scope = "cleared"
        elif kind in ("整天无卡", "上班未打卡", "下班未打卡"):
            scope = "judge"
            review.append({"rid": aid, "userid": uid, "date": d, "kind": kind,
                           "rule_hint": "可能旷工" if kind == "整天无卡" else "可能漏打卡"})
        else:
            scope = "count"
        rows.append((aid, uid, d, kind, mins, by, scope))
    conn.executemany("INSERT INTO reconciled VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    cleared = sum(1 for r in rows if r[6] == "cleared")
    stats = {"异常总数": len(rows), "申请单抵消": cleared,
             "抵消率": round(cleared / len(rows), 3) if rows else 0.0,
             "按次计入": sum(1 for r in rows if r[6] == "count"), "待人工确认": len(review)}
    return stats, review


# ---------------------------------------------------------------- leave list
def build_leave_list(conn: sqlite3.Connection) -> dict:
    conn.executescript("""
        DROP TABLE IF EXISTS leave_list;
        CREATE TABLE leave_list(app_id TEXT PRIMARY KEY, userid TEXT, leave_type TEXT, status TEXT,
                                hours REAL, days REAL, reason TEXT);
    """)
    rows = [(a, u, lt, st, h, round(h / P.HOURS_PER_DAY, 2), reason or "")
            for a, u, lt, st, h, reason in conn.execute(
                "SELECT app_id, userid, leave_type, status, hours, reason FROM applications "
                "WHERE app_type='请假' ORDER BY app_id")]
    conn.executemany("INSERT INTO leave_list VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    status: dict[str, int] = defaultdict(int)
    cnt: dict[str, int] = defaultdict(int)
    days: dict[str, float] = defaultdict(float)
    for r in rows:
        status[r[3]] += 1
        if r[3] in P.LEAVE_COUNTED_STATUSES:
            cnt[r[2]] += 1
            days[r[2]] += r[5]
    return {"总单数": len(rows), "状态分布": dict(status), "计入的按类型次数": dict(cnt),
            "计入的按类型天数": {k: round(v, 1) for k, v in days.items()},
            "计入总天数": round(sum(days.values()), 1)}


# ---------------------------------------------------------------- monthly
MONTHLY_COLS = ["工号", "姓名", "部门", "岗位", "应出勤日", "出勤日", "工作日加班h", "休息日加班h",
                "节假日加班h", "调休h", "带薪假日", "事假日", "病假日", "旷工日", "计薪日",
                "迟到", "早退", "补卡", "缺卡待核", "备注"]


def build_monthly(conn: sqlite3.Connection, month: str, decisions: dict[str, str]) -> list[dict]:
    """月表。decisions: {rid: "确认异常"|"核销"};没有决定的项不计旷工,只写进备注。"""
    n_work = conn.execute("SELECT COUNT(*) FROM calendar WHERE day_type='workday' AND date LIKE ?",
                          (month + "%",)).fetchone()[0]
    ot: dict = defaultdict(lambda: defaultdict(float))
    for u, t, h in conn.execute("SELECT userid, ot_type, hours FROM ot_rows"):
        ot[u][t] += h
    marks = ",".join("?" * len(P.LEAVE_COUNTED_STATUSES))
    leave: dict = defaultdict(lambda: defaultdict(float))
    for u, lt, h in conn.execute(
            f"SELECT userid, leave_type, hours FROM applications WHERE app_type='请假' "
            f"AND status IN ({marks})", P.LEAVE_COUNTED_STATUSES):
        leave[u][lt] += h
    attend = dict(conn.execute("SELECT userid, COUNT(*) FROM daily WHERE day_type='workday' "
                               "AND (first_in IS NOT NULL OR last_out IS NOT NULL) GROUP BY userid"))
    makeups = dict(conn.execute("SELECT userid, COUNT(*) FROM applications WHERE app_type='补卡' "
                                "AND status='已批准' GROUP BY userid"))
    notes: dict = defaultdict(list)
    for u, start, lt in conn.execute(
            "SELECT userid, start_date, leave_type FROM applications WHERE app_type='请假' "
            "AND status='待批' ORDER BY app_id"):
        notes[u].append(f"{start[5:]}{lt}待批")
    counts: dict = defaultdict(lambda: defaultdict(int))
    for aid, u, d, kind, scope in conn.execute(
            "SELECT aid, userid, date, kind, scope FROM reconciled ORDER BY aid"):
        if scope == "count":
            counts[u][kind] += 1
        elif scope == "judge":
            dec = decisions.get(str(aid))
            if dec == "确认异常":
                counts[u]["旷工" if kind == "整天无卡" else "缺卡"] += 1
            elif dec is None:
                notes[u].append(f"{d[5:]}{kind}待人工复核")

    def days_of(lv: dict, types) -> float:
        return round(sum(lv.get(t, 0) for t in types) / P.HOURS_PER_DAY, 2)

    out = []
    for uid, code, name, dept, title, daily_paid in conn.execute(
            "SELECT userid, code, name, dept, title, daily_paid FROM employees "
            "WHERE active=1 ORDER BY userid"):
        lv = leave[uid]
        paid = days_of(lv, P.PAID_LEAVE_TYPES)
        unpaid = days_of(lv, P.UNPAID_LEAVE_TYPES)
        sick = days_of(lv, (P.SICK_LEAVE_TYPE,))
        absent = counts[uid]["旷工"]
        present = attend.get(uid, 0)
        if daily_paid:                       # 按日计薪:出勤一天算一天,请假不再另扣
            pay = float(present)
        else:
            pay = round(n_work - unpaid - sick * P.SICK_DEDUCT_RATIO - absent, 2)
        out.append({
            "工号": code, "姓名": name, "部门": dept, "岗位": title,
            "应出勤日": n_work, "出勤日": present,
            "工作日加班h": round(ot[uid]["workday"], 2), "休息日加班h": round(ot[uid]["restday"], 2),
            "节假日加班h": round(ot[uid]["holiday"], 2), "调休h": round(lv.get(P.COMP_LEAVE_TYPE, 0), 2),
            "带薪假日": paid, "事假日": unpaid, "病假日": sick, "旷工日": absent, "计薪日": pay,
            "迟到": counts[uid]["迟到"], "早退": counts[uid]["早退"], "补卡": makeups.get(uid, 0),
            "缺卡待核": counts[uid]["缺卡"], "备注": ";".join(notes[uid]),
        })
    return out
