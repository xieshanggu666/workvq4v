# -*- coding: utf-8 -*-
"""轻量建表后迁移：为旧版数据库补齐新增列并归一化历史快照。

项目没有引入 Alembic，这里用 ADD COLUMN 做前向兼容（对已是最新结构的库为幂等无操作）：
- pending_crisis / last_resolution / last_expedition / expedition：状态机快照列，旧行补 NULL
- trade_order / last_trade：贸易救援订单与幂等凭据列，旧行补 NULL
- aid_pact / last_aid：地堡联盟援助协议与幂等凭据列，旧行补 NULL
- refugee_intake / last_refugee / refugee_stats：跨聚落难民安置快照、幂等凭据与累计统计，旧行补 NULL
- mission_stats：贸易救援/联盟援助累计战绩列，旧行补 NULL 后由归一化从事件日志回填
- medical_cases：医疗救治中心病例簿列，旧行补 NULL
- medical_crisis：地堡医疗危机表，旧行统一从 0 开始
- reputation：对外信誉，旧行统一从初始值 50 开始
- row_version：乐观锁版本号，旧行统一从 1 开始

补列后再做一次“旧存档归一化”（reconcile_old_saves），保证历史行加载进新引擎后
状态机可以唯一收敛。四类待决事件（危机/探索遭遇/贸易途中事件/联盟援助途中事件）
在引擎侧共用同一套"待决事件管线"（见 services/engine.py 的 _PENDING_SLOTS），这里的
归一化也走同一份快照校验（_clean_pending_snapshot）：
- 已结束档案上悬而未决的危机/探索队/贸易订单/援助协议/难民安置一律清除，统一收敛到 ended
- 损坏/悬空的快照（成员全部不在档、目标居民失踪、JSON 残缺）不阻塞每日推进
- 难民安置快照结构残缺或已无存活难民时清除，避免卡在无法接纳/遣返的死状态
- 病例簿中结构残缺或居民已不在档的活跃病例收敛/剔除，终态履历同步校正
- 贸易救援/联盟援助战绩（mission_stats）旧行缺失时从事件日志按终态结算日志回填：
  仅成功交付与押运/交付失败各计一次，撤单/撤约/驳回/逾期日志天然不计入
- 已结束档案的旧结局缺少 base_score / score_parts / trade_aid 时按现存统计补齐
  （不改写历史总分），旧存档在新结局页上也能看到各项贡献说明
- survivors 与实际存活居民数漂移时以居民表为准校正
"""
import json

from sqlalchemy import inspect, text


def _existing_columns(conn, table):
    try:
        return {c["name"] for c in inspect(conn).get_columns(table)}
    except Exception:
        return set()


def ensure_schema(engine):
    with engine.begin() as conn:
        columns = _existing_columns(conn, "game_sessions")
        if not columns:
            # 表尚未创建，create_all 会按最新模型建表，无需迁移
            return
        if "pending_crisis" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN pending_crisis JSON"))
        if "last_resolution" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_resolution JSON"))
        if "expedition" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN expedition JSON"))
        if "last_expedition" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_expedition JSON"))
        if "trade_order" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN trade_order JSON"))
        if "last_trade" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_trade JSON"))
        if "medical_cases" not in columns:
            # 医疗救治中心病例簿：旧档案从未开展救治，从空病历开始
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN medical_cases JSON"))
        if "medical_crisis" not in columns:
            # NOT NULL + 常量默认值：旧档案从未承受医疗危机压力，从 0 开始
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN medical_crisis INTEGER NOT NULL DEFAULT 0")
            )
        if "aid_pact" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN aid_pact JSON"))
        if "last_aid" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_aid JSON"))
        if "refugee_intake" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN refugee_intake JSON"))
        if "last_refugee" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN last_refugee JSON"))
        if "refugee_stats" not in columns:
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN refugee_stats JSON"))
        if "mission_stats" not in columns:
            # 贸易救援/联盟援助累计战绩：旧档案补列后由 reconcile 从事件日志回填
            conn.execute(text("ALTER TABLE game_sessions ADD COLUMN mission_stats JSON"))
        if "reputation" not in columns:
            # NOT NULL + 常量默认值：旧档案从未开展贸易，信誉从初始值 50 开始
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN reputation INTEGER NOT NULL DEFAULT 50")
            )
        if "row_version" not in columns:
            # NOT NULL + 常量默认值，存量行全部初始化为 1
            conn.execute(
                text("ALTER TABLE game_sessions ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1")
            )
    # DDL 提交后再做数据归一化（SQLite 不允许在同一事务里混用 DDL 与行更新）
    reconcile_old_saves(engine)


def _loads(raw):
    """SQLite JSON 列读回可能是 str/dict/None；解析失败一律按损坏快照处理。"""
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def reconcile_old_saves(engine):
    """旧存档快照归一化：见模块文档字符串。幂等，可重复执行。

    只有“确实发生了变化”的行才会被 UPDATE：快照本就规范时零写入，
    不会无谓 bump 乐观锁版本号。
    """
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    if "game_sessions" not in table_names:
        return 0
    resident_columns = _existing_columns(engine, "residents")
    session_columns = _existing_columns(engine, "game_sessions")
    has_event_logs = "event_logs" in table_names
    # 旧表可能缺少部分后加列：只 SELECT 实际存在的列，缺失列按 None 处理
    wanted = [
        "id", "status", "pending_crisis", "expedition", "trade_order",
        "aid_pact", "medical_cases", "survivors", "refugee_intake",
        "mission_stats", "outcome", "day", "score", "medical_crisis",
        "refugee_stats",
    ]
    selected = [c for c in wanted if c in session_columns]
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"SELECT {', '.join(selected)} FROM game_sessions")
        ).all()
        updates = []
        for row in rows:
            data = dict(zip(selected, row))
            sid = data.get("id")
            status = data.get("status")
            crisis_raw = data.get("pending_crisis")
            exp_raw = data.get("expedition")
            trade_raw = data.get("trade_order")
            aid_raw = data.get("aid_pact")
            med_raw = data.get("medical_cases")
            survivors = data.get("survivors")
            refugee_raw = data.get("refugee_intake")
            mission_raw = data.get("mission_stats")
            outcome_raw = data.get("outcome")
            day = data.get("day")
            crisis_val = data.get("medical_crisis")
            ref_stats_raw = data.get("refugee_stats")
            old_crisis = _loads(crisis_raw)
            old_exp = _loads(exp_raw)
            old_trade = _loads(trade_raw)
            old_aid = _loads(aid_raw)
            old_med = _loads(med_raw)
            old_refugee = _loads(refugee_raw)
            old_mission = _loads(mission_raw)
            old_outcome = _loads(outcome_raw)
            alive_count = None
            if {"id", "alive", "session_id"} <= resident_columns:
                alive_count = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM residents WHERE session_id = :sid AND alive = 1"
                    ),
                    {"sid": sid},
                ).scalar()

            if status == "running":
                # 运行中：只清理无法再被状态机处理的损坏/悬空快照
                new_crisis, crisis_changed = _clean_pending_crisis(conn, sid, old_crisis)
                new_exp, exp_changed = _clean_expedition(old_exp, conn, sid)
                new_trade, trade_changed = _clean_trade_order(old_trade, conn, sid)
                new_aid, aid_changed = _clean_aid_pact(old_aid, conn, sid)
                new_refugee, refugee_changed = _clean_refugee_intake(old_refugee)
            else:
                # 已结束：危机/探索队/贸易订单/援助协议/难民安置快照一律清空，收敛到 ended
                new_crisis, crisis_changed = None, old_crisis is not None
                new_exp, exp_changed = None, old_exp is not None
                new_trade, trade_changed = None, old_trade is not None
                new_aid, aid_changed = None, old_aid is not None
                new_refugee, refugee_changed = None, old_refugee is not None
            # 病例簿归一化与档案阶段无关：活跃病例绑定的居民必须仍在档且状态一致
            new_med, med_changed = _clean_medical_cases(old_med, conn, sid)

            # 战绩回填：旧档案没有 mission_stats 列（补列后为 NULL）时，从事件
            # 日志里唯一的终态结算日志重建。只认成功交付与失败回退两类标题，
            # 撤单·/撤约·/审核驳回/签约逾期天然不计；同一终态只写一条日志，
            # 故撤销或失败不会被重复计分。回填一次后不再覆盖新引擎写入的值。
            mission_changed = False
            new_mission = old_mission
            if old_mission is None and has_event_logs:
                new_mission = _reconstruct_mission_stats(conn, sid)
                if any(new_mission.values()):
                    mission_changed = True
                else:
                    new_mission = None  # 无战绩：保持 NULL，零写入

            # 已结束的旧结局补齐新评分字段：按现存病例/难民/战绩重算各项贡献
            # 与总分（旧公式是新公式 base 分量的子集），使旧存档在新结局页也能
            # 正常展示贡献明细；已是新结构（含 score_parts）的结局不动。
            outcome_changed = False
            new_outcome = old_outcome
            if status != "running" and isinstance(old_outcome, dict) \
                    and "score_parts" not in old_outcome:
                # 终局快照里的 day/survivors 才是结算时刻的值，优先于当前列
                # （历史行的列可能与结局漂移）；缺省时再退回列值
                end_day = old_outcome.get("day") or day
                end_survivors = old_outcome.get("survivors")
                if end_survivors is None:
                    end_survivors = alive_count if alive_count is not None else survivors
                new_outcome = _enrich_legacy_outcome(
                    old_outcome,
                    cases=(new_med or []),
                    refugee_stats=_loads(ref_stats_raw) or {},
                    mission_stats=new_mission or {},
                    survivors=end_survivors,
                    day=end_day,
                    crisis=(crisis_val or 0) if crisis_val is not None else 0,
                )
                outcome_changed = new_outcome != old_outcome

            if (crisis_changed or exp_changed or trade_changed or aid_changed
                    or med_changed or refugee_changed or mission_changed
                    or outcome_changed
                    or (alive_count is not None and alive_count != survivors)):
                updates.append(
                    {
                        "sid": sid,
                        "crisis": json.dumps(new_crisis, ensure_ascii=False)
                        if new_crisis is not None
                        else None,
                        "expedition": json.dumps(new_exp, ensure_ascii=False)
                        if new_exp is not None
                        else None,
                        "trade": json.dumps(new_trade, ensure_ascii=False)
                        if new_trade is not None
                        else None,
                        "aid": json.dumps(new_aid, ensure_ascii=False)
                        if new_aid is not None
                        else None,
                        "medical": json.dumps(new_med, ensure_ascii=False)
                        if new_med is not None
                        else None,
                        "refugee": json.dumps(new_refugee, ensure_ascii=False)
                        if new_refugee is not None
                        else None,
                        "survivors": alive_count if alive_count is not None else survivors,
                        "mission": json.dumps(new_mission, ensure_ascii=False)
                        if new_mission is not None
                        else None,
                        "outcome": json.dumps(new_outcome, ensure_ascii=False)
                        if new_outcome is not None
                        else None,
                        # 旧结局补齐贡献字段时同步把总分重算为新口径（各分量之和），
                        # 保证结局页展示的总分等于 score 列；其余行不动历史分数
                        "score": new_outcome["recomputed_score"]
                        if outcome_changed and isinstance(new_outcome, dict)
                        else None,
                        "has_mission_col": "mission_stats" in session_columns,
                        "has_outcome_col": "outcome" in session_columns,
                        "has_score_col": "score" in session_columns,
                    }
                )
        for u in updates:
            sets = [
                "pending_crisis = :crisis",
                "expedition = :expedition",
                "trade_order = :trade",
                "aid_pact = :aid",
                "medical_cases = :medical",
                "refugee_intake = :refugee",
            ]
            params = {k: v for k, v in u.items()
                      if k not in ("has_mission_col", "has_outcome_col", "has_score_col")}
            # survivors/score 列在所有建表版本中都存在，但仍按实际列集合兜底
            if "survivors" in session_columns:
                sets.append("survivors = :survivors")
            else:
                params.pop("survivors", None)
            if u["has_score_col"] and u["score"] is not None:
                sets.append("score = :score")
            else:
                params.pop("score", None)
            if u["has_mission_col"]:
                sets.append("mission_stats = :mission")
            if u["has_outcome_col"]:
                sets.append("outcome = :outcome")
            conn.execute(
                text(f"UPDATE game_sessions SET {', '.join(sets)} WHERE id = :sid"),
                params,
            )
    return len(updates)


# 终态结算日志标题 → 战绩计数键。撤单·/撤约·/签约被拒/签约逾期等非终态
# （未实际押运或未完成交付）的标题不在表中，日志重建时自然不计分。
_TRADE_SUCCESS_TITLES = ("救援送达·", "采购到货·")
_TRADE_FAIL_TITLE = "订单失败·"
_AID_SUCCESS_TITLE = "援助交付·"
_AID_FAIL_TITLES = ("援助失败·", "押运审核未过·")


def _reconstruct_mission_stats(conn, sid):
    """从事件日志重建贸易/援助累计战绩（旧存档回填，一次性）。

    每条订单/协议的终态收敛恰好写一条结算日志，故按标题计数即等价于
    成功交付 / 失败回退次数，且撤单撤约绝不计入。
    """
    stats = {
        "rescue_delivered": 0,
        "procure_delivered": 0,
        "aid_delivered": 0,
        "trade_failed": 0,
        "aid_failed": 0,
    }
    rows = conn.execute(
        text("SELECT title FROM event_logs WHERE session_id = :sid AND event_type IN ('trade', 'aid')"),
        {"sid": sid},
    ).all()
    for (title,) in rows:
        if not isinstance(title, str):
            continue
        if title.startswith("救援送达·"):
            stats["rescue_delivered"] += 1
        elif title.startswith("采购到货·"):
            stats["procure_delivered"] += 1
        elif title.startswith(_TRADE_FAIL_TITLE):
            stats["trade_failed"] += 1
        elif title.startswith(_AID_SUCCESS_TITLE):
            stats["aid_delivered"] += 1
        elif title.startswith(_AID_FAIL_TITLES):
            stats["aid_failed"] += 1
    return stats


def _medical_stats_from_cases(cases):
    """与引擎 _medical_stats 同口径的纯函数：登记/康复/病亡/累计床日。"""
    active_statuses = {"registered", "treating", "isolated"}
    return {
        "total": len(cases),
        "active": sum(1 for c in cases if c.get("status") in active_statuses),
        "recovered": sum(1 for c in cases if c.get("status") == "recovered"),
        "deceased": sum(1 for c in cases if c.get("status") == "deceased"),
        "care_days": sum(int(c.get("days_cared", 0) or 0) for c in cases),
    }


def _enrich_legacy_outcome(outcome, cases, refugee_stats, mission_stats,
                           survivors, day, crisis):
    """旧结局缺 base_score/score_parts/trade_aid 时按现存统计补齐并重算总分。

    旧档案的 score 只含基础生存分；新公式在其上叠加三项贡献分。重算后
    各项明细之和等于最终总分，旧存档在新结局页看到的口径与新档案完全一致。
    仅补缺失字段，不覆盖既有 reason/survivors/day/avg_health 等历史值。
    """
    from ..services import engine as engine_mod

    # 医疗履历优先取旧结局自带的统计（终局后病例簿可能已无存）；
    # 旧结局没有时才从归一化后的病例簿重建
    legacy_med = outcome.get("medical")
    if isinstance(legacy_med, dict):
        med = {
            "total": int(legacy_med.get("total", 0) or 0),
            "active": int(legacy_med.get("active", 0) or 0),
            "recovered": int(legacy_med.get("recovered", 0) or 0),
            "deceased": int(legacy_med.get("deceased", 0) or 0),
            "care_days": int(legacy_med.get("care_days", 0) or 0),
            "medical_crisis": int(
                legacy_med.get("medical_crisis", crisis) or 0
            ),
        }
    else:
        med = _medical_stats_from_cases(cases)
        med["medical_crisis"] = crisis
    # 难民统计同样优先取旧结局自带值，其次读 refugee_stats 列
    legacy_ref = outcome.get("refugee")
    if isinstance(legacy_ref, dict):
        ref_source = legacy_ref
    else:
        ref_source = refugee_stats
    ref = {
        "applications": int(ref_source.get("applications", 0) or 0),
        "accepted": int(ref_source.get("accepted", 0) or 0),
        "admitted": int(ref_source.get("admitted", 0) or 0),
        "rejected": int(ref_source.get("rejected", 0) or 0),
        "repatriated": int(ref_source.get("repatriated", 0) or 0),
        "quarantine_dead": int(ref_source.get("quarantine_dead", 0) or 0),
    }
    mission = {
        "rescue_delivered": int(mission_stats.get("rescue_delivered", 0) or 0),
        "procure_delivered": int(mission_stats.get("procure_delivered", 0) or 0),
        "aid_delivered": int(mission_stats.get("aid_delivered", 0) or 0),
        "trade_failed": int(mission_stats.get("trade_failed", 0) or 0),
        "aid_failed": int(mission_stats.get("aid_failed", 0) or 0),
    }
    morale = outcome.get("avg_morale")
    if morale is None:
        # 旧结局没有记录士气：基础分量无法精确复现，按中性士气 60 估算
        morale = 60.0
    base = int(survivors * day * (0.5 + morale / 200.0))
    med_part = (
        med["recovered"] * engine_mod.SCORE_MED_RECOVERED
        + med["care_days"] * engine_mod.SCORE_MED_CARE_DAY
        - med["deceased"] * engine_mod.SCORE_MED_DECEASED
    )
    ref_part = (
        ref["admitted"] * engine_mod.SCORE_REFUGEE_ADMITTED
        - ref["quarantine_dead"] * engine_mod.SCORE_REFUGEE_QUARANTINE_DEAD
    )
    mission_part = (
        mission["rescue_delivered"] * engine_mod.SCORE_TRADE_RESCUE
        + mission["procure_delivered"] * engine_mod.SCORE_TRADE_PROCURE
        + mission["aid_delivered"] * engine_mod.SCORE_AID_DELIVERED
        - mission["trade_failed"] * engine_mod.SCORE_TRADE_FAILED
        - mission["aid_failed"] * engine_mod.SCORE_AID_FAILED
    )
    parts = {
        "base": base,
        "medical": int(med_part),
        "refugee": int(ref_part),
        "trade_aid": int(mission_part),
    }
    parts["total"] = max(0, base + parts["medical"] + parts["refugee"] + parts["trade_aid"])
    enriched = dict(outcome)
    enriched.setdefault("base_score", base)
    enriched["score_parts"] = parts
    # medical/ref 已按“旧结局履历优先、档案统计兜底”重建，统一回写
    enriched["medical"] = med
    enriched.setdefault("refugee", ref)
    enriched["trade_aid"] = mission
    enriched["recomputed_score"] = parts["total"]
    return enriched


def _resident_ids(conn, sid):
    return {
        row[0]
        for row in conn.execute(
            text("SELECT id FROM residents WHERE session_id = :sid"), {"sid": sid}
        ).all()
    }


def _prune_member_ids(raw_members, known):
    """剔除悬空/重复编号，保持首次出现顺序。返回 (有效编号列表, 是否发生变化)。"""
    members, seen = [], set()
    for m in raw_members:
        if m in known and m not in seen:
            seen.add(m)
            members.append(m)
    return members, members != raw_members


def _clean_pending_snapshot(pending, valid_target_ids):
    """待决事件快照归一化：危机/探索遭遇/押运途中事件共用同一快照结构。

    - 非 dict（结构损坏）：丢弃
    - 绑定目标已不在有效集合（居民表/队伍编制）：丢弃——无法再结算单体效果

    返回 (归一化快照, 是否发生变化)。调用方决定丢弃快照后是清空整个字段
    （危机）还是仅摘除快照、保留宿主（探索队/贸易订单）。
    """
    if pending is None:
        return None, False
    if not isinstance(pending, dict):
        return None, True
    target_id = pending.get("target_id")
    if target_id is not None and target_id not in valid_target_ids:
        return None, True
    return pending, False


def _clean_pending_crisis(conn, sid, crisis):
    """待处理危机快照结构残缺或绑定目标已不在档：清空以解除每日阶段的死锁。

    返回 (归一化快照, 是否发生变化)。
    """
    if crisis is None:
        return None, False
    if not isinstance(crisis, dict):
        return None, True
    if not crisis.get("event") or not isinstance(crisis.get("choices"), list):
        return None, True
    return _clean_pending_snapshot(crisis, _resident_ids(conn, sid))


def _clean_expedition(exp, conn, sid):
    """探索队快照结构残缺或成员全部不在档：清空（队伍无法恢复）。

    成员中夹杂已删除/重复编号时剔除；剔除后无人则整队清除。
    返回 (归一化快照, 是否发生变化)。
    """
    if exp is None:
        return None, False
    if not isinstance(exp, dict) or exp.get("status") != "away":
        return None, True
    raw_members = exp.get("members")
    if not isinstance(raw_members, list):
        return None, True
    members, changed = _prune_member_ids(raw_members, _resident_ids(conn, sid))
    if not members:
        return None, True
    cleaned = dict(exp)
    cleaned["members"] = members
    # 待处理遭遇绑定的目标若已不在队中，丢弃该遭遇（无法再结算单体效果）
    pending, pending_changed = _clean_pending_snapshot(
        cleaned.get("pending_encounter"), members
    )
    cleaned["pending_encounter"] = pending
    return cleaned, changed or pending_changed


# 贸易订单合法状态链
_TRADE_STATUSES = {"reviewing", "transporting", "delivered", "failed", "rejected", "cancelled"}


def _clean_trade_order(order, conn, sid):
    """贸易订单快照归一化。

    - 结构残缺 / 非法状态：清空（无法恢复的订单）
    - 已收敛的终态（delivered/failed/rejected/cancelled）残留在列上：清空，
      统一以"无在谈订单"开始（结算结果早已回写资源/信誉）
    - 押运成员夹杂悬空/重复编号：剔除；剔除后无人且已在途则整单清除
    - 在途中事件绑定目标已不在队：丢弃该事件（无法再结算单体效果），
      订单本身保留，下一次推进正常运输/交付

    返回 (归一化快照, 是否发生变化)。
    """
    if order is None:
        return None, False
    if not isinstance(order, dict):
        return None, True
    status = order.get("status")
    if status not in _TRADE_STATUSES:
        return None, True
    if status in ("delivered", "failed", "rejected", "cancelled"):
        return None, True
    raw_escorts = order.get("escorts")
    if not isinstance(raw_escorts, list) or not order.get("token"):
        return None, True
    escorts, changed = _prune_member_ids(raw_escorts, _resident_ids(conn, sid))
    if not escorts:
        return None, True
    cleaned = dict(order)
    cleaned["escorts"] = escorts
    pending, pending_changed = _clean_pending_snapshot(
        cleaned.get("pending_incident"), escorts
    )
    cleaned["pending_incident"] = pending
    return cleaned, changed or pending_changed


# 联盟援助协议合法状态链
_AID_STATUSES = {"proposed", "escorting", "delivered", "failed", "rejected", "lapsed", "cancelled"}


def _clean_aid_pact(pact, conn, sid):
    """联盟援助协议快照归一化（口径同 _clean_trade_order）。

    - 结构残缺 / 非法状态：清空（无法恢复的协议）
    - 已收敛终态（delivered/failed/rejected/lapsed/cancelled）残留：清空
    - 押运成员夹杂悬空/重复编号：剔除；剔除后无人则整份协议清除
    - 在途中事件绑定目标已不在队：丢弃该事件，协议本身保留，下一次推进正常运输
    - proposed 阶段会签医护编号悬空：整份协议清除（三方签约凭据不完整）

    返回 (归一化快照, 是否发生变化)。
    """
    if pact is None:
        return None, False
    if not isinstance(pact, dict):
        return None, True
    status = pact.get("status")
    if status not in _AID_STATUSES:
        return None, True
    if status in ("delivered", "failed", "rejected", "lapsed", "cancelled"):
        return None, True
    raw_escorts = pact.get("escorts")
    if not isinstance(raw_escorts, list) or not pact.get("token"):
        return None, True
    known = _resident_ids(conn, sid)
    if pact.get("signer_id") not in known:
        return None, True
    escorts, changed = _prune_member_ids(raw_escorts, known)
    if not escorts:
        return None, True
    cleaned = dict(pact)
    cleaned["escorts"] = escorts
    pending, pending_changed = _clean_pending_snapshot(
        cleaned.get("pending_incident"), escorts
    )
    cleaned["pending_incident"] = pending
    return cleaned, changed or pending_changed


# 病例合法状态：登记/治疗/隔离（活跃）与康复/病亡（终态）
_MED_STATUSES = {"registered", "treating", "isolated", "recovered", "deceased"}
_MED_ACTIVE = {"registered", "treating", "isolated"}


# 难民安置合法状态链
_REFUGEE_STATUSES = {"quarantine", "admitting"}


def _clean_refugee_intake(intake):
    """跨聚落难民安置快照归一化。

    - 结构残缺 / 非法状态 / 无一次性 token：清空（无法恢复的安置）
    - people 不是列表或已无存活难民：清空（无法继续检疫/接纳）
    - 单条难民记录残缺（无 key/name）：剔除；剔除后无人则整份安置清除

    返回 (归一化快照, 是否发生变化)。
    """
    if intake is None:
        return None, False
    if not isinstance(intake, dict):
        return None, True
    if intake.get("status") not in _REFUGEE_STATUSES or not intake.get("token"):
        return None, True
    raw_people = intake.get("people")
    if not isinstance(raw_people, list):
        return None, True
    people = []
    changed = False
    for p in raw_people:
        if not isinstance(p, dict) or not p.get("key") or not p.get("name"):
            changed = True
            continue
        # 规范化布尔健康字段的残缺值
        if not isinstance(p.get("alive", True), bool) and p.get("alive") not in (0, 1):
            p = dict(p)
            p["alive"] = True
            changed = True
        people.append(p)
    alive = [p for p in people if p.get("alive", True)]
    if not alive:
        return None, True
    if changed:
        cleaned = dict(intake)
        cleaned["people"] = people
        return cleaned, True
    return intake, False


def _clean_medical_cases(cases, conn, sid):
    """医疗病例簿归一化。

    - 整体结构损坏（非 list）：清空，巡检会重新建档
    - 单条病例字段残缺（无 id/resident_id/合法状态）：剔除
    - 活跃病例绑定居民不在档：整份病例剔除（悬空病例无法再结算）
    - 活跃病例绑定居民已死亡（alive=0）：收敛为 deceased，保持病亡履历一致
    - 同一居民多条活跃病例：仅保留最早建档的一条，其余重复病例剔除

    返回 (归一化病例簿, 是否发生变化)。
    """
    if cases is None:
        return None, False
    if not isinstance(cases, list):
        return None, True
    known = _resident_ids(conn, sid)
    dead_ids = {
        row[0]
        for row in conn.execute(
            text("SELECT id FROM residents WHERE session_id = :sid AND alive = 0"),
            {"sid": sid},
        ).all()
    }
    cleaned = []
    changed = False
    active_owners = set()
    for c in cases:
        if not isinstance(c, dict) or not c.get("id"):
            changed = True
            continue
        rid = c.get("resident_id")
        status = c.get("status")
        if rid is None or status not in _MED_STATUSES:
            changed = True
            continue
        if rid not in known:
            # 居民已不在档：活跃/终态病历都无法对应到人，一并剔除
            changed = True
            continue
        c = dict(c)
        if status in _MED_ACTIVE:
            if rid in dead_ids:
                # 居民死亡但病例仍挂活跃态：收敛为病亡
                c["status"] = "deceased"
                c["close_day"] = c.get("close_day")
                changed = True
                cleaned.append(c)
                continue
            if rid in active_owners:
                # 同一居民重复的活跃病例：保留最早一条
                changed = True
                continue
            active_owners.add(rid)
        cleaned.append(c)
    if not cleaned:
        # 归一化后无任何病历：回 None（等价空病历），减少无意义 JSON 落库
        return None, True
    return cleaned, changed
