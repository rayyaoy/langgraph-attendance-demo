"""可选的模型调用:请假理由分类、待确认异常的初步判断、月度小结。

这个模块是整个 demo 里唯一会调用模型的地方。设计约束:

  - 模型由调用方以 `llm(system, user, max_tokens) -> dict | None` 的形式传进来
    (经 LangGraph 的 runtime context),测试里可以换成假模型。
  - 发给模型之前,员工 userid 换成临时编号 P01、P02……,对照表只放在内存里;
    请假理由里出现的员工姓名换成"某员工"。
  - 模型的输出一律当作不可信输入逐项检查。调用出错、返回的不是 JSON 对象、
    字段类型不对,都记一条 warning 日志,然后丢掉这一条(或整批),上层照规则结果继续。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

LLM = Callable[[str, str, int], Optional[dict]]

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def llm_from_env() -> Optional[LLM]:
    """设置了 LLM_API_KEY 才返回模型调用函数,否则返回 None(图走纯规则分支)。

    key 只交给 openai SDK 的 client 对象,不打印、不写文件、不进 state 和 checkpoint。
    """
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None
    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)

    def call(system: str, user: str, max_tokens: int = 2000) -> Optional[dict]:
        from openai import OpenAI     # 只有走 AI 分支才需要安装 openai
        client = OpenAI(api_key=key, base_url=base_url)
        resp = client.chat.completions.create(
            model=model, temperature=0.2, max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        try:
            return json.loads(resp.choices[0].message.content)
        except (json.JSONDecodeError, TypeError):
            return None

    return call


# ---------------------------------------------------------------- 脱敏
class Pseudonyms:
    """userid → 临时编号(P01、P02……)。对照表只在内存里,不落盘。"""

    def __init__(self) -> None:
        self.by_user: dict[str, str] = {}
        self.by_code: dict[str, str] = {}

    def anon(self, uid: str) -> str:
        if uid not in self.by_user:
            code = f"P{len(self.by_user) + 1:02d}"
            self.by_user[uid] = code
            self.by_code[code] = uid
        return self.by_user[uid]

    def real(self, code: str) -> str:
        return self.by_code.get(code, code)


def mask_names(text: str, names: list[str]) -> str:
    """把文本里出现的员工全名换成"某员工"。"""
    for name in names:
        if name and name in text:
            text = text.replace(name, "某员工")
    return text


def employee_names(conn: sqlite3.Connection) -> list[str]:
    return [n for (n,) in conn.execute("SELECT name FROM employees WHERE length(name) >= 2")]


# ---------------------------------------------------------------- 输出检查
def _ask(llm: LLM, task: str, system: str, user: Any, max_tokens: int) -> Optional[dict]:
    """调用模型并确认返回的是 JSON 对象;其余情况记日志并返回 None。"""
    try:
        out = llm(system, json.dumps(user, ensure_ascii=False), max_tokens)
    except Exception as exc:                     # 网络、鉴权、限流等
        log.warning("%s: 模型调用失败(%s),按没有 AI 结果处理", task, type(exc).__name__)
        return None
    if out is None:
        log.warning("%s: 模型没有返回可解析的 JSON,按没有 AI 结果处理", task)
        return None
    if not isinstance(out, dict):
        log.warning("%s: 返回值应为 JSON 对象,实际是 %s,整批丢弃", task, type(out).__name__)
        return None
    return out


def _item_list(task: str, out: dict) -> list:
    items = out.get("items")
    if not isinstance(items, list):
        log.warning("%s: items 应为列表,实际是 %s,整批丢弃", task, type(items).__name__)
        return []
    return items


def _drop(task: str, item: Any, why: str) -> None:
    log.warning("%s: 丢弃一条模型输出(%s):%.120r", task, why, item)


# ---------------------------------------------------------------- (1) 请假理由分类
CATEGORIES = ("健康", "家庭", "私事", "未写明", "其他")

CLASSIFY_PROMPT = (
    "任务:请假理由分类。\n"
    "输入是一组请假记录,每条有 id、假期类型 type、天数 days 和请假理由 reason。\n"
    "请给每条理由选一个类别,只能从这几个里选:" + "、".join(CATEGORIES) + "。\n"
    "如果理由和假期类型对不上(例如请的是病假,理由却是出去玩),type_conflict 写 true,否则写 false。\n"
    "只输出一个 JSON 对象,格式:"
    '{"items": [{"id": "记录的 id", "category": "类别", "type_conflict": false}]}'
)


def classify_reasons(llm: Optional[LLM], leaves: list[dict], names: list[str]) -> Optional[dict]:
    """leaves: [{app_id, userid, leave_type, days, reason}] → {app_id: {category, type_conflict}}"""
    if llm is None or not leaves:
        return None
    task = "请假理由分类"
    pseud = Pseudonyms()
    payload = [{"id": lv["app_id"], "who": pseud.anon(lv["userid"]), "type": lv["leave_type"],
                "days": lv.get("days", 0),
                "reason": mask_names((lv.get("reason") or "")[:100], names)} for lv in leaves]
    out = _ask(llm, task, CLASSIFY_PROMPT, payload, 2000)
    if out is None:
        return None
    known = {lv["app_id"] for lv in leaves}
    res: dict = {}
    for it in _item_list(task, out):
        if not isinstance(it, dict):
            _drop(task, it, "不是对象")
            continue
        app_id, cat, conflict = it.get("id"), it.get("category"), it.get("type_conflict", False)
        if not isinstance(app_id, str) or app_id not in known:
            _drop(task, it, "id 不认识")
            continue
        if not isinstance(conflict, bool):
            _drop(task, it, "type_conflict 不是布尔值")
            continue
        res[app_id] = {"category": cat if cat in CATEGORIES else "其他", "type_conflict": conflict}
    return res or None


# ---------------------------------------------------------------- (2) 待确认异常初步判断
VERDICTS = ("可能忘记打卡", "可能漏提请假", "可能旷工", "可能设备故障", "信息不足")

JUDGE_PROMPT = (
    "任务:打卡异常初步判断。\n"
    "输入里每条是某位员工某一天的打卡异常,这些异常没有对应的请假单或补卡单。"
    "另附这位员工本月补过几次卡(makeups_this_month)、本月一共几条异常(anomalies_this_month)。\n"
    "请为每条给一个初步判断,只能从这几项里选:" + "、".join(VERDICTS) + "。\n"
    "可以这样考虑:只少打一次卡、本月别的问题也少的人,多半是忘了打;"
    "一整天都没有打卡的,可能是请假没提单,也可能是旷工;拿不准就选「信息不足」。\n"
    "confidence 写 0 到 1 之间的数;reason 用一句话说明依据。"
    "这只是给审核人的参考,最后由人来决定。\n"
    "只输出一个 JSON 对象,格式:"
    '{"items": [{"id": 1, "verdict": "判断", "confidence": 0.5, "reason": "一句话"}]}'
)


def _as_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v)
    return None


def judge_anomalies(llm: Optional[LLM], anoms: list[dict]) -> Optional[dict]:
    """anoms: [{rid, userid, date, kind, makeups, anomalies}] → {rid: {verdict, confidence, reason}}。
    结果只是建议,是否计旷工由人工确认节点决定。"""
    if llm is None or not anoms:
        return None
    task = "打卡异常初判"
    pseud = Pseudonyms()
    payload = [{"id": a["rid"], "who": pseud.anon(a["userid"]), "date": a["date"], "kind": a["kind"],
                "makeups_this_month": a.get("makeups", 0),
                "anomalies_this_month": a.get("anomalies", 0)} for a in anoms]
    out = _ask(llm, task, JUDGE_PROMPT, payload, 4000)
    if out is None:
        return None
    known = {a["rid"] for a in anoms}
    res: dict = {}
    for it in _item_list(task, out):
        if not isinstance(it, dict):
            _drop(task, it, "不是对象")
            continue
        rid = _as_int(it.get("id"))
        if rid is None or rid not in known:
            _drop(task, it, "id 不是已知的整数编号")
            continue
        conf = it.get("confidence")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
            _drop(task, it, "confidence 不是 0-1 的数")
            continue
        reason = it.get("reason", "")
        if not isinstance(reason, str):
            _drop(task, it, "reason 不是字符串")
            continue
        verdict = it.get("verdict")
        res[rid] = {"verdict": verdict if verdict in VERDICTS else "信息不足",
                    "confidence": float(conf), "reason": reason[:80]}
    return res or None


# ---------------------------------------------------------------- (3) 月度小结
REPORT_PROMPT = (
    "任务:月度考勤小结。\n"
    "输入是本月考勤的统计数字(JSON)。请写一份给管理层看的简短小结,分几段,每段有标题和正文。\n"
    "要求:正文里出现的每个数字都必须能在输入里找到原值,不要自己加减乘除或估算;"
    "不写任何员工的姓名或工号;用平实的语气。\n"
    "建议的段落:整体概况、异常和处理、请假情况、值得留意的地方。\n"
    "只输出一个 JSON 对象,格式:"
    '{"sections": [{"title": "标题", "text": "正文"}]}'
)


def write_report(llm: Optional[LLM], stats: dict) -> Optional[list[dict]]:
    """stats: 各规则节点的汇总数字 → [{title, text}]。"""
    if llm is None:
        return None
    task = "月度小结"
    out = _ask(llm, task, REPORT_PROMPT, stats, 3000)
    if out is None:
        return None
    sections = out.get("sections")
    if not isinstance(sections, list) or not sections:
        log.warning("%s: sections 应为非空列表,改用规则模板", task)
        return None
    for sec in sections:
        if not (isinstance(sec, dict) and isinstance(sec.get("title"), str)
                and isinstance(sec.get("text"), str)):
            log.warning("%s: 段落格式不对(%.120r),整份改用规则模板", task, sec)
            return None
    return [{"title": s["title"], "text": s["text"]} for s in sections]


def template_report(stats: dict) -> list[dict]:
    """纯规则分支的月度小结:用字符串模板填数字,不经过模型。"""
    r, c, lv = stats.get("rules", {}), stats.get("reconcile", {}), stats.get("leave", {})
    cf = stats.get("confirm", {})
    return [
        {"title": "整体概况", "text": f"本月纳入核算 {r.get('员工数', 0)} 人,生成每日记录 "
                                  f"{r.get('每日行数', 0)} 行,加班记录 {r.get('加班记录', 0)} 条。"},
        {"title": "异常和处理", "text": f"原始异常 {c.get('异常总数', 0)} 条,被申请单抵消 "
                                   f"{c.get('申请单抵消', 0)} 条(抵消率 {c.get('抵消率', 0):.1%}),"
                                   f"按次计入 {c.get('按次计入', 0)} 条,送人工确认 "
                                   f"{c.get('待人工确认', 0)} 条。"},
        {"title": "请假情况", "text": f"请假单 {lv.get('总单数', 0)} 张,状态分布 "
                                  f"{lv.get('状态分布', {})};计入的合计 {lv.get('计入总天数', 0)} 天。"},
        {"title": "人工确认", "text": f"确认异常 {cf.get('确认异常', 0)} 条,核销 {cf.get('核销', 0)} 条,"
                                  f"未决 {cf.get('未决', 0)} 条(未决项不计旷工,写进备注待人工复核)。"},
    ]
