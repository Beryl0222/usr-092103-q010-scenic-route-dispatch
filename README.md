# 景区分层线路调度

张家界分层线路调度后端。接入入口时段、设施容量、维护计划、气象风险、游客能力声明、
团队关系、无障碍需求、线路版本与救援覆盖，在实时约束下给出**可解释**的路线与入场窗口；
高风险活动须资格核验与人工放行；游客可拒绝个性化推荐。

## 核心规则

- **分层线路**：精品观光 `scic` / 摄影机位 `photo` / 长线徒步 `hiking` / 极限运动 `extreme`，
  各层有独立气象阈值（观光/摄影≤黄色2，徒步≤蓝色1，极限必须清零0）与能力、资格门槛。
- **容量核算**：索道、天梯、步道、入口均按 10 分钟格子统计，占位 `held` 与确认 `confirmed`
  都计入容量；门票有余量不代表运力可承载。
- **预约一致性**：占位 15 分钟 TTL，在 `hold → confirmed / timed_out / released` 三种结局间
  容量成对预留与释放，任何格子不重不漏。
- **高风险门**：极限线路资格未核验不可占位；资格齐备仍须现场人员人工放行才可确认。
- **只重排未走节点**：索道停运、步道封闭、气象升级时，已核销票段（`SEGMENT_USED`）原样保留，
  续程从当前位置图搜索安全路径；无可达安全路径时**不把游客送入风险区**，转现场人工。
- **团队拆分**：拆分事件记录分组、继承共同走过的票段、明确会合点与时间。
- **迟到数据不回溯**：事件双时间——业务时间 `occurred_at` 与入库时间 `recorded_at`，
  所有判断支持 `known_at` 上界；传感器迟到数据只影响其入库之后，绝不反向制造违规。
- **紧急疏散优先**：分区疏散时自动对在场团队排撤离线至集合点，穿越风险区的撤离命令
  优先于气象阈值与普通偏好，且不做个性化。
- **拒绝个性化**：团队任一成员关闭个性化即按客观规则排期。

## 模块

- `contracts/domain.schema.json`：公共事件信封、事件类型与聚合类型枚举。
- `src/validator.py`：公共信封校验（枚举与 schema 保持一致，有测试守护）。
- `src/dispatch/`
  - `events.py` 事件工厂；`store.py` 仅追加日志（双时间、版本/幂等约束）。
  - `network.py` 线路版本、边（索道/天梯/步道/徒步/极限）、设施、分层阈值、时间格子。
  - `projection.py` 从事件重建的全部读模型，查询支持 `known_at` 决策时刻语义。
  - `planner.py` 逐格约束仿真、入场窗口搜索、稳定阻断原因码、资格/人工门、疏散模式。
  - `service.py` 命令→事件：预约生命周期、容量预留/释放、改线绕行、拆分、疏散联动。
  - `views.py` 游客视图、当班现场视图、管理者复盘归因。
- `data/sample.json`：联调样例。
- `tests/`：契约一致性、规划约束、预约生命周期、改线/疏散/拆分/迟到数据、三视图与复盘。

## 事件目录

公共信封：`event_id, event_type, aggregate_type, aggregate_id, occurred_at, version, summary, payload?`，
存储层另加 `recorded_at`。

| 事件 | 聚合 | 语义 |
|---|---|---|
| ROUTE_PUBLISHED | route_revision | 发布某分层线路版本（节点/边/门槛/会合点），同 id 旧版本自动失效 |
| ENTRY_SLOT_OPENED | entry_slot | 开放入口时段、名额与可接待分层 |
| FACILITY_STATUS_CHANGED | facility | 索道/天梯/救援站或步道边的开放、停运、封闭 |
| WEATHER_RISK_ISSUED / CLEARED | weather_advisory | 分区气象风险等级发布/解除；RISK_CLEARED 为人工综合解除 |
| VISITOR_CAPABILITY_DECLARED | visitor | 能力等级、无障碍需求、资格申报、所属团队 |
| VISITOR_CONSENT_UPDATED | visitor | 个性化推荐同意/拒绝 |
| QUALIFICATION_VERIFIED | qualification | 资格核验结果、有效期、核验人 |
| PARTY_REGISTERED / PARTY_SPLIT | visitor_party | 团队登记与拆分（分组、会合点） |
| RESERVATION_PLACED / CONFIRMED / TIMED_OUT / RELEASED | reservation | 预约占位四态生命周期 |
| CAPACITY_RESERVED | capacity_window | 格子容量 held/confirmed/converted/released，带 reservation_id 明细 |
| SEGMENT_USED | reservation | 票段核销；已核销段在改线时保留 |
| MANUAL_APPROVAL_DECIDED | qualification | 高风险人工放行 approved/rejected |
| ROUTE_REROUTED | dispatch_decision | 只替换未走节点，带 reason_codes、reasons、分组、会合点、疏散标记 |
| EVACUATION_ORDERED / STOOD_DOWN | evacuation | 分区紧急疏散/解除（集合点、清空时限） |
| DISPATCH_NOTE_ISSUED | dispatch_decision | 面向 visitor/staff 的通知（改线原因、安全提示、人工介入） |
| INCIDENT_REVIEWED | dispatch_decision | 拥堵/救援复盘，容量/设施/气象/调度/迟到数据五类归因与证据 |

阻断原因码（游客、现场、复盘共用词汇）：`EVACUATION_ACTIVE`、`WEATHER_TIER_EXCEEDED`、
`FACILITY_CLOSED`、`EDGE_CLOSED`、`MAINTENANCE`、`CAPACITY_FULL`、`ENTRY_SLOT_FULL`、
`ENTRY_SLOT_TIER_MISMATCH`、`CAPABILITY_INSUFFICIENT`、`ACCESSIBILITY_UNMET`、
`QUALIFICATION_UNVERIFIED / PENDING / EXPIRED`、`RESCUE_UNCOVERED`、`PARTY_TOO_LARGE`、
`MANUAL_APPROVAL_REQUIRED`、`NO_ENTRY_WINDOW`、`NO_SAFE_CONTINUATION`。

## 三视图

- **游客**：当前路线、已用票段、后续节点、改线原因与安全提示、会合点；
  高风险时显示等待人工放行与占位剩余时间。
- **现场人员**：各分区气象/疏散/封闭边与设施/救援是否在位、待人工放行队列、
  即将超时占位、受影响在园预约、拆分团队与会合点、近期调度通知。
- **管理者复盘**：在事件时间窗内按容量/设施/气象/调度/迟到数据五类归因，
  每条结论引用具体事件证据，并单列“决策时刻之后才到达”的迟到数据，
  明确双时间日志下不可能用迟到数据回溯制造违规。

## 本地检查

```bash
python3 -m unittest discover -s tests -v
```

仅使用 Python 标准库（>=3.11）。
