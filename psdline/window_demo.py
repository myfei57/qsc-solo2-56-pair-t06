"""门区检修窗口全流程演示与交付自检。

用可推进时钟按真实现场顺序跑一遍，每个步骤同时给出"系统做了什么"和可供
``window-demo`` 命令判定的布尔检查：

1. 保运营站 ST1：申请撞接车 → 拒绝并给让路方案；按方案改到空档 → 批准；
2. 占用期间这樘门不能进自动开门（批量开门跳过、门区联动直接拒绝）；
3. 保检修站 ST2：申请撞接车 → 批准，接车被顺延（时刻表出新版本，旧版保留）；
4. 到点自动释放 → 挂账；未销账不能再申请；人工销账后恢复；
5. 并发申请同一樘门：只有一个赢，另一个拿到让路方案；
6. 部分失败注入：提交前夕故障 → 整批回滚，流水与占用不留痕；
7. 跨天窗口：跨零点申请正常占用；
8. 历史不可变：终结窗口只在历史里，篡改流水文件后哈希链校验立即报错。
"""

from __future__ import annotations

import json
import os

from .clock import ManualClock
from .errors import InterlockError, RailError
from .window import MAINTENANCE_FIRST, StationWindowPolicy


def run_window_demo(platform, clock: ManualClock) -> dict:
    """在给定平台上执行演示，返回步骤记录与自检结果。"""

    steps: list[dict] = []
    checks: list[tuple[str, bool, str]] = []

    def record(name: str, payload: dict) -> None:
        steps.append({"step": name, **payload})

    platform.start()
    gate_st1 = platform.gates.by_station("ST1")[0].id
    rollback_gate_st2 = platform.gates.by_station("ST2")[-1].id
    gate_st2 = platform.gates.by_station("ST2")[0].id
    other_gate_st1 = platform.gates.by_station("ST1")[1].id
    now = clock.now()

    # ST2 设为保检修站，允许在演示里顶让未到点窗口。
    platform.set_station_window_policy("ST2", MAINTENANCE_FIRST, allow_force=True)

    # 1. ST1 保运营：申请时段覆盖下一班接车（播种后约 10 分钟），应被拒并给方案。
    clash_start = now + 600
    rejected = platform.apply_window(
        "ST1", [gate_st1], clash_start, clash_start + 1200, "crew-01", "门机异响"
    )
    record("保运营站撞接车", {"ok": rejected["ok"], "violations": rejected["violations"],
                             "plan": rejected["yield_plan"]})
    checks.append(("保运营撞排班被拒", rejected["ok"] is False, str(rejected["violations"])))
    plan = rejected["yield_plan"]
    checks.append(("拒绝时给出让路方案", plan["feasible"], plan["headline"]))

    # 按让路方案里的平移时段重新申请。
    shift = next(option for option in plan["options"] if option["kind"] == "shift")
    shifted = platform.apply_window(
        "ST1", [gate_st1], shift["start_at"], shift["end_at"], "crew-01", "门机异响"
    )
    record("按让路方案平移后批准", {"ok": shifted["ok"], "window_id": shifted["window"]["id"]})
    checks.append(("平移后批准", shifted["ok"] is True, shifted["window"]["id"]))
    window_id = shifted["window"]["id"]

    # 2. 推进到窗口内：自动上锁，门控跳过这樘门。
    clock.advance(shift["start_at"] - clock.now())
    tick_in = platform.window_tick()
    batch = platform.batch_open("ST1")
    record("占用期间自动开门被闸", {
        "activated": tick_in["activated"],
        "locked": platform.lockouts.is_locked(gate_st1),
        "skipped": batch["skipped"],
    })
    checks.append(("窗口到点自动上锁", platform.lockouts.is_locked(gate_st1), window_id))
    checks.append((
        "批量开门跳过占用门",
        batch["skipped"].get(_group_of(platform, gate_st1)) == "maintenance_occupied",
        str(batch["skipped"]),
    ))
    blocked = False
    try:
        from .door import open_group

        open_group(platform.doors, platform.mappings, "ST1", _group_of(platform, gate_st1))
    except InterlockError:
        blocked = True
    checks.append(("门区联动拒绝占用门", blocked, "InterlockError"))

    # 3. ST2 保检修：撞接车也批准，排班改点。
    before_rev = platform.timetables.latest("ST2").revision
    clash2 = platform.apply_window(
        "ST2", [gate_st2], now + 700, now + 900, "crew-02", "门槛胶条更换"
    )
    after_rev = platform.timetables.latest("ST2").revision
    record("保检修站撞接车改点批准", {
        "ok": clash2["ok"],
        "displaced": [item["train_id"] for item in clash2["schedule_displacements"]],
        "revision": after_rev,
    })
    checks.append(("保检修站批准", clash2["ok"] is True, str(clash2["violations"])))
    checks.append(("时刻表出新版本且旧版保留", after_rev == before_rev + 1, f"{before_rev}->{after_rev}"))
    checks.append(("接车被顺延", bool(clash2["schedule_displacements"]), "无改点"))

    # 4. 到点自动释放 + 挂账 + 销账闸。
    window = platform.windows.require(window_id)
    clock.advance(window.end_at - clock.now())
    tick_end = platform.window_tick()
    record("到期自动释放挂账", {"expired": tick_end["expired"], "locked": platform.lockouts.is_locked(gate_st1)})
    checks.append(("到期自动释放门锁", platform.lockouts.is_locked(gate_st1) is False, window_id))
    unconfirmed_message = ""
    try:
        platform.apply_window(
            "ST1", [gate_st1], now + 10_000, now + 10_600, "crew-03", "复检"
        )
    except RailError as error:
        unconfirmed_message = error.message
    checks.append((
        "未销账不能再申请",
        "销账" in unconfirmed_message,
        unconfirmed_message or "竟然放行了",
    ))
    confirm = platform.confirm_window(window_id, "dispatcher-01")
    after_confirm = platform.apply_window(
        "ST1", [gate_st1], now + 10_000, now + 10_600, "crew-03", "复检"
    )
    checks.append(("销账后恢复申请", after_confirm["ok"] is True, str(after_confirm["violations"])))
    record("人工销账后恢复", {"confirm": confirm["ok"], "reopened": after_confirm["ok"]})

    # 5. 并发：同一时刻两笔申请抢同一段同一樘门（串行化后只有一笔赢）。
    concurrent_start = now + 20_000
    first = platform.apply_window(
        "ST1", [other_gate_st1], concurrent_start, concurrent_start + 600, "A", "并发甲"
    )
    second = platform.apply_window(
        "ST1", [other_gate_st1], concurrent_start, concurrent_start + 600, "B", "并发乙"
    )
    record("并发申请同门同时段", {"first": first["ok"], "second": second["ok"]})
    checks.append(("并发只批准一笔", first["ok"] is True and second["ok"] is False, f"{first['ok']}/{second['ok']}"))
    checks.append(("输家拿到让路方案", second["yield_plan"] is not None, "无方案"))

    # 6. 部分失败回滚：注入提交前故障，连锁顶让也必须整体撤回。
    rollback_start = now + 30_000
    victim = platform.apply_window(
        "ST2", [rollback_gate_st2], rollback_start, rollback_start + 600, "C", "将被顶让"
    )
    before_history = len(platform.windows.history())
    platform.windows.fault_on_commit = True
    rolled_back = None
    try:
        rolled_back = platform.apply_window(
            "ST2", [rollback_gate_st2], rollback_start + 60, rollback_start + 600, "D", "故障申请", force=True
        )
    except RuntimeError as error:
        rolled_back = {"ok": False, "error": str(error)}
    victim_after = platform.windows.get(victim["window"]["id"])
    chain = platform.windows.verify()
    record("注入故障整批回滚", {
        "result": rolled_back,
        "victim_status": victim_after.status if victim_after else None,
        "history_delta": len(platform.windows.history()) - before_history,
    })
    checks.append(("故障申请未生效", isinstance(rolled_back, dict) and rolled_back.get("ok") is False, str(rolled_back)[:120]))
    checks.append(("被顶让窗口原样保留", victim_after is not None and victim_after.status == "approved",
                   str(victim_after.status if victim_after else None)))
    checks.append(("回滚后哈希链一致", chain["consistent"] is True, str(chain)))

    # 7. 跨天窗口（跨 UTC 零点）。
    day_boundary = (clock.now() // 86_400 + 1) * 86_400
    cross = platform.apply_window(
        "ST1", [other_gate_st1], day_boundary - 600, day_boundary + 600, "E", "跨天检修"
    )
    spans = cross["ok"] and platform.windows.require(cross["window"]["id"]).spans_midnight()
    record("跨天窗口", {"ok": cross["ok"], "window_id": cross["window"]["id"] if cross["ok"] else None})
    checks.append(("跨零点窗口被识别为跨天", bool(spans), str(cross["ok"])))

    # 8. 历史不可变：终态窗口只在历史；篡改流水文件后校验报错。
    history_before = [item.id for item in platform.windows.history()]
    backup, segment = _tamper_journal(platform.window_store.journal)
    verify_after_tamper = {"detected": False}
    try:
        platform.window_store.verify_chain()
    except RailError as error:
        verify_after_tamper = {"detected": True, "message": error.message}
    os.remove(segment)
    os.replace(backup, segment)
    record("篡改审计流水", verify_after_tamper)
    checks.append(("篡改历史被哈希链检出", verify_after_tamper["detected"], str(verify_after_tamper)))
    restored = platform.windows.verify()
    checks.append(("恢复文件后校验通过", restored["consistent"] is True, str(restored)))
    checks.append(("终态窗口只进历史", bool(history_before), f"{len(history_before)} 条"))

    return {
        "steps": steps,
        "history_count": len(platform.windows.history()),
        "live_count": len(platform.windows.windows()),
        "checks": checks,
    }


def _group_of(platform, gate_id: str) -> str:
    gate = platform.gates.require(gate_id)
    return gate.group


def _tamper_journal(journal) -> tuple[str, str]:
    """在不改变记录数的前提下，就地篡改最早一段的一条记录。

    返回 ``(备份路径, 段路径)``：原始段改名备份，篡改段写回原路径。
    """

    segment = journal.segments()[0]
    backup = segment + ".bak"
    with open(segment, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    os.replace(segment, backup)
    record = json.loads(lines[-1])
    record["at"] = float(record.get("at", 0)) + 1
    lines[-1] = json.dumps(record, ensure_ascii=False) + "\n"
    with open(segment, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return backup, segment
