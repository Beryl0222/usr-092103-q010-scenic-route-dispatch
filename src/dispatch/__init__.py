"""景区分层线路调度后端。

模块划分：
- timeutil/events/store：时间与仅追加事件日志（双时间：occurred_at / recorded_at）
- network：线路版本、边（索道/天梯/步道段）、分区、会合点等静态网络定义
- projection：从事件日志重建的读模型
- planner：分层线路规划与可解释阻断
- service：调度应用服务（预约生命周期、重排、疏散、人工放行）
- views：游客 / 现场 / 管理三视图与复盘归因
"""
