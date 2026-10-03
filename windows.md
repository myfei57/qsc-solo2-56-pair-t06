# 门区检修窗口（Maintenance Window）

解决"检修时段和接车排班靠电话协调、修到一半要接车"的问题。按门区申请占用时段，
系统同时检测与**接车时刻表**、与**其他检修申请**的冲突，冲突时给出**让路方案**；
占用期间该门不能进入自动开门。

## 两个设计取舍（已定）

1. **冲突策略按站可配**，两种都说得通，因此不做全局一刀切：
   - `service_first`（保运营，缺省）：申请撞上接车时段 → **拒绝**，让路方案改时间
     （原门组平移到最近空档）或换门（同站替部门）。
   - `maintenance_first`（保检修）：申请撞上接车时段 → **批准**，让路方案是让排班
     改点（被撞车次顺延到窗口结束后，时刻表出新版本，需行车调度确认）。
   - 与**其他检修窗口**抢门永远是硬冲突；仅当本站 `allow_force=true` 且对方窗口
     **尚未到点**时，持 `force` 可顶让；已 `active`（现场有人）绝不允许顶。
2. **到期自动释放门 + 人工再确认销账**：控制周期在 `end + expiry_grace_s` 自动
   交还门体（解锁、恢复自动开门），但窗口进入 `expired_unconfirmed` 挂账态；
   值班人必须确认现场收工（`confirm`）才转入不可变历史。**挂账期间该门不能再提
   新申请**——避免"门自动放行了却没人知道现场到底收没收拾"。

## 数据与一致性

- 窗口事实是一条 **SHA-256 哈希链、只追加的 JSONL 流水**（`window-*.jsonl`）。
  每次业务操作的多个事件打成**一个事件批一次 fsync**提交：
  - 部分失败：提交前任一步失败则整批不写；顶让旧窗口用副本承载，失败不污染现场。
  - 历史不可变：`closed/cancelled/rejected` 只留在流水与历史表，任何篡改/缺段在
    `verify` 时立即报 `ChainBroken`。
  - 重启回放整条流水重建状态。
- **同一份占用**：门控、联锁、审计不各存标志，统一调
  `WindowService.occupancy_at(moment)` / `is_gate_held(gate)`。
- **并发**：服务级决策锁把"查冲突 → 改状态 → 提交"收成临界区，杜绝两笔申请都通过
  检测后双双落盘（已用 16 线程压测，同门重叠只批准 1 笔）。
- **跨天**：起止一律 Unix 秒（UTC）半开区间 `[start,end)`，跨零点窗口是普通区间，
  `MaintenanceWindow.spans_midnight()` 供展示识别。
- **接车时刻表版本化**：保检修改点写 revision+1，旧版本保留，解释"当时按哪份排班
  判的冲突"。

## CLI

```
make window-demo      # 可推进时钟跑全流程并自检（21 项）
make windows          # 查看当前占用/挂账/策略/哈希链
```

## HTTP 接口

| 方法 路径 | 说明 |
| --- | --- |
| `GET /api/windows?station_id=` | 当前窗口、占用映射、挂账、策略、哈希链校验 |
| `GET /api/windows/history` | 不可变历史窗口 |
| `GET /api/timetable/{stationID}` | 当前接车时刻表及版本号 |
| `POST /api/windows/apply` | 申请 `{station_id, gate_ids, start_at, end_at, requested_by, reason, force?}` |
| `POST /api/windows/activate` | 时段内手动置占用（正常由周期自动到点） |
| `POST /api/windows/extend` | 续窗（新止点冲突照常检测） |
| `POST /api/windows/cancel` | 取消未到点窗口 |
| `POST /api/windows/complete` | 提前完工：释放门体，仍需销账 |
| `POST /api/windows/confirm` | 人工销账 |
| `POST /api/windows/tick` | 手动触发一次到点扫描（周期也会自动触发） |
| `POST /api/windows/policy` | 改站策略 `{station_id, conflict_policy, expiry_grace_s?, allow_force?}` |
| `POST /api/timetable` | 调度推送下一版时刻表 |

申请被拒时响应固定为 `ok=false` + `yield_plan`（平移/换门/改点三选一或多个）+
`conflicts`（逐门逐时段说明撞了哪个窗口或哪班车）。

## 配置（config 的 `window` 节）

```json
"window": {
  "default_conflict_policy": "service_first",
  "expiry_grace_s": 0.0,
  "allow_force": false,
  "search_horizon_s": 3600.0,
  "station_policies": [["ST2", "maintenance_first"]]
}
```
