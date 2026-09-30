"""用例 2:图停在人工确认节点;换一个图实例(模拟第二天另开进程)回答后继续跑完,决定写进月表。"""

from conftest import cfg, node_runs, read_monthly
from demo import synth
from demo.graph import build_graph, open_checkpointer, resume, start


def test_interrupt_then_human_resume(work, app):
    res = start(app, "t-hitl", data_db=work["data_db"], out_dir=work["out_dir"],
                month=synth.MONTH, auto=False, llm=None)

    # 1) 图停住了,等人
    assert "__interrupt__" in res
    payload = res["__interrupt__"][0].value
    items = payload["items"]
    assert payload["options"] == ["确认异常", "核销"]
    whole_day = {(i["userid"], i["date"]): i["rid"] for i in items if i["kind"] == "整天无卡"}
    # 两条"整天无卡、没有请假"的埋点都被送上来;请假还没批的两天同样要人工决定
    assert set(whole_day) == set(synth.ABSENT) | {("e911", "2026-06-25"), ("e912", "2026-06-26")}
    assert app.get_state(cfg("t-hitl")).next == ("confirm",)
    assert not (work["dir"] / "output").exists()           # 没确认前不出月表

    # 2) 新的 checkpointer 连接 + 新的图对象 = 另一个进程里恢复
    app2 = build_graph(open_checkpointer(work["ckpt"]))
    rid_915 = whole_day[synth.ABSENT[0]]
    rid_916 = whole_day[synth.ABSENT[1]]
    final = resume(app2, "t-hitl", decisions={str(rid_915): "确认异常", str(rid_916): "核销",
                                              "999999": "确认异常"})   # 不存在的 rid 必须被丢掉
    assert "__interrupt__" not in final
    assert app2.get_state(cfg("t-hitl")).next == ()
    assert final["decisions"] == {str(rid_915): "确认异常", str(rid_916): "核销"}
    assert final["stats"]["confirm"] == {"确认异常": 1, "核销": 1, "未决": len(items) - 2,
                                         "无效答复": ["999999"]}     # 写错的编号要被点名,不能静默吞掉

    # 3) 决定写进月表:确认的计旷工并扣计薪日,核销的不计,没回答的留在备注
    m = read_monthly(work["out_dir"])
    assert m["E915"]["旷工日"] == "1" and float(m["E915"]["计薪日"]) == 20
    assert m["E916"]["旷工日"] == "0" and float(m["E916"]["计薪日"]) == 21
    unanswered = [i for i in items if i["rid"] not in (rid_915, rid_916)]
    for i in unanswered:
        assert f"{i['date'][5:]}{i['kind']}待人工复核" in m[i["userid"].upper()]["备注"]

    # 4) interrupt 恢复时 confirm 节点从头再执行一次,前面的节点不重跑
    runs = node_runs(work["data_db"])
    assert runs["confirm"] == 2
    assert runs["rules"] == runs["reconcile"] == runs["ledger"] == runs["render"] == 1
