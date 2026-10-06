# -*- coding: utf-8 -*-
"""结局评分测试：总分同时反映医疗救治、难民安置、贸易援助的成败。

覆盖：
- 三项贡献分的计算口径（成功正贡献、失败负贡献）
- 撤销/驳回/逾期不计分，且同一终态不重复计分（幂等回放、直接重复收敛）
- 旧存档缺 mission_stats 列/字段时仍能正常结算（缺省按 0）
"""
import pytest

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession
from app.core.config import SURVIVAL_TARGET_DAY
from app.services.engine import (
    BunkerEngine,
    SCORE_MED_RECOVERED,
    SCORE_MED_CARE_DAY,
    SCORE_MED_DECEASED,
    SCORE_REFUGEE_ADMITTED,
    SCORE_REFUGEE_QUARANTINE_DEAD,
    SCORE_TRADE_RESCUE,
    SCORE_TRADE_PROCURE,
    SCORE_AID_DELIVERED,
    SCORE_TRADE_FAILED,
    SCORE_AID_FAILED,
    MED_TREATING,
    MED_RECOVERED,
    MED_DECEASED,
    FOOD, WATER, POWER, OXY,
)
from tests.test_engine import make_session, FixedRand
from tests.test_trade import TradeRand, rescue_offer
from tests.test_aid import AidRand, make_aid_session, assign_medics, aid_request


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def _finish(eng, gs, win=True):
    gs.day = SURVIVAL_TARGET_DAY
    eng._finish(win=win, reason="测试终局")
    return gs


# ---- 基础：无任何贡献时总分等于旧公式 ----

def test_score_without_contributions_equals_base_formula(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    gs.day = SURVIVAL_TARGET_DAY
    morale = eng.avg_morale()
    expected = int(gs.survivors * gs.day * (0.5 + morale / 200.0))
    _finish(eng, gs)
    parts = gs.outcome["score_parts"]
    assert parts["medical"] == 0
    assert parts["refugee"] == 0
    assert parts["trade_aid"] == 0
    assert gs.score == expected
    assert parts["total"] == expected
    assert gs.outcome["base_score"] == expected


# ---- 医疗救治贡献 ----

def test_score_reflects_medical_recovered_care_days_and_deceased(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 一康复（在治 3 床日）、一病亡
    r1, r2 = gs.residents[0], gs.residents[1]
    c1 = eng._build_case_dict(r1, infectious=False, reason="t", status=MED_RECOVERED)
    c1["days_cared"] = 3
    c1["close_day"] = 10
    c2 = eng._build_case_dict(r2, infectious=True, reason="t", status=MED_DECEASED)
    c2["close_day"] = 11
    eng._save_cases([c1, c2])
    _finish(eng, gs)
    med = gs.outcome["medical"]
    assert med["recovered"] == 1 and med["deceased"] == 1 and med["care_days"] == 3
    expected_med = (
        SCORE_MED_RECOVERED + 3 * SCORE_MED_CARE_DAY - SCORE_MED_DECEASED
    )
    parts = gs.outcome["score_parts"]
    assert parts["medical"] == expected_med
    assert gs.score == parts["base"] + expected_med


# ---- 难民安置贡献 ----

def test_score_reflects_refugee_admitted_and_quarantine_dead(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._bump_refugee_stats(admitted=4, quarantine_dead=1)
    _finish(eng, gs)
    parts = gs.outcome["score_parts"]
    assert parts["refugee"] == (
        4 * SCORE_REFUGEE_ADMITTED - SCORE_REFUGEE_QUARANTINE_DEAD
    )
    assert gs.outcome["refugee"]["admitted"] == 4
    assert gs.score == parts["base"] + parts["refugee"]


# ---- 贸易救援贡献 ----

def test_successful_rescue_and_procure_deliveries_add_mission_stats(db):
    gs = make_session(db)
    # 求援 eta2：审核通过(0.1)、day2 无事件(0.9)、抵达交付掷点(0.1 成功)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9]))
    offer = rescue_offer(eng)
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()
    eng.rand = TradeRand([0.1])
    eng.advance_day()
    stats = eng.mission_stats()
    assert stats["rescue_delivered"] == 1
    assert stats["procure_delivered"] == 0
    assert stats["trade_failed"] == 0


def test_failed_trade_delivery_counts_failure_once(db):
    gs = make_session(db)
    # 审核通过(0.1)、无事件(0.9)、交付掷点失败(0.9)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9, 0.9]))
    offer = rescue_offer(eng)
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    eng.advance_day()
    eng.advance_day()
    stats = eng.mission_stats()
    assert stats["trade_failed"] == 1
    assert stats["rescue_delivered"] == 0
    # 订单已清除：连点/并发落败的结算请求走幂等回放，不再二次计数
    detail, replayed = eng.reconcile_stale_trade(
        "settle", token=None, choice_key=None
    )
    assert replayed is True
    assert eng.mission_stats()["trade_failed"] == 1


def test_trade_cancel_and_review_rejection_do_not_count(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.99]))  # 审核驳回
    offer = rescue_offer(eng)
    order = eng.apply_trade(offer["id"], [gs.residents[0].id])
    # 主动撤单（审核阶段）：不计任何战绩
    eng.cancel_trade(token=order["token"])
    assert all(v == 0 for v in eng.mission_stats().values())
    # 再走一笔审核驳回：同样不计
    eng2_order = eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    eng.advance_day()  # 0.99 >= 通过率：驳回
    assert gs.trade_order is None
    assert all(v == 0 for v in eng.mission_stats().values())


def test_trade_contribution_scored_at_finish(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._bump_mission_stats(rescue_delivered=2, procure_delivered=1, trade_failed=1)
    _finish(eng, gs)
    parts = gs.outcome["score_parts"]
    assert parts["trade_aid"] == (
        2 * SCORE_TRADE_RESCUE + SCORE_TRADE_PROCURE - SCORE_TRADE_FAILED
    )
    ta = gs.outcome["trade_aid"]
    assert ta["rescue_delivered"] == 2 and ta["procure_delivered"] == 1
    assert ta["trade_failed"] == 1
    assert gs.score == parts["base"] + parts["trade_aid"]


# ---- 联盟援助贡献 ----

def test_aid_delivery_counts_once(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1]))  # 检疫关卡通过 → 交付
    _near_arrival_aid(gs, eng)
    eng.advance_day()
    assert gs.aid_pact is None
    stats = eng.mission_stats()
    assert stats["aid_delivered"] == 1
    assert stats["aid_failed"] == 0


def test_aid_gate_failure_counts_as_failure(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    # 关卡掷点失败（< chance 才通过）
    eng = BunkerEngine(db, gs, rand=AidRand([0.99]))
    _near_arrival_aid(gs, eng)
    eng.advance_day()
    assert gs.aid_pact is None
    assert eng.mission_stats()["aid_failed"] == 1
    assert eng.mission_stats()["aid_delivered"] == 0


def test_aid_cancel_reject_and_lapse_do_not_count(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    req = aid_request(eng)
    pact = eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    # proposed 阶段主动撤约：不计分
    eng.cancel_aid(token=pact["token"])
    assert all(v == 0 for v in eng.mission_stats().values())
    # 会签医护失去资质 → 签约逾期关闭：不计分
    req2 = aid_request(eng)
    pact2 = eng.propose_aid(req2["id"], gs.residents[0].id, [gs.residents[1].id])
    gs.residents[0].job = "general"
    eng.advance_day()
    assert gs.aid_pact is None
    assert all(v == 0 for v in eng.mission_stats().values())
    # 签约审核被拒：不计分（0.99 极高随机必被拒）
    gs.residents[0].job = "medic"
    eng.rand = AidRand([0.99])
    req3 = aid_request(eng)
    eng.propose_aid(req3["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()
    assert gs.aid_pact is None
    assert all(v == 0 for v in eng.mission_stats().values())


def test_aid_contribution_scored_at_finish(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._bump_mission_stats(aid_delivered=1, aid_failed=2)
    _finish(eng, gs)
    parts = gs.outcome["score_parts"]
    assert parts["trade_aid"] == SCORE_AID_DELIVERED - 2 * SCORE_AID_FAILED
    assert gs.outcome["trade_aid"]["aid_delivered"] == 1
    assert gs.outcome["trade_aid"]["aid_failed"] == 2


# ---- 终局幂等：贡献统计/分数不重复结算 ----

def test_finish_idempotent_keeps_contribution_score(db):
    from app.models import EventLog
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng._bump_mission_stats(rescue_delivered=1)
    _finish(eng, gs)
    score1, outcome1 = gs.score, dict(gs.outcome)
    stats1 = dict(gs.mission_stats)
    # 重复收敛/重复 _finish：分数、战绩、结局日志都不重复
    eng._finish(win=False, reason="不应覆盖")
    eng._check_end()
    assert gs.score == score1
    assert gs.outcome == outcome1
    assert gs.mission_stats == stats1
    db.flush()
    end_logs = db.query(EventLog).filter(
        EventLog.session_id == gs.id, EventLog.title == "结局贡献结算"
    ).count()
    assert end_logs == 1


# ---- 旧存档兼容：缺战绩列/统计缺省时按 0 正常结算 ----

def test_old_save_without_mission_stats_settles_normally(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 模拟旧档案：从未写过战绩列
    assert gs.mission_stats is None
    assert eng.mission_stats() == {
        "rescue_delivered": 0, "procure_delivered": 0, "aid_delivered": 0,
        "trade_failed": 0, "aid_failed": 0,
    }
    gs.day = SURVIVAL_TARGET_DAY
    morale = eng.avg_morale()
    expected = int(gs.survivors * gs.day * (0.5 + morale / 200.0))
    _finish(eng, gs)
    assert gs.score == expected  # 无战绩：贡献分 0，与旧口径一致
    assert gs.outcome["trade_aid"] == {
        "rescue_delivered": 0, "procure_delivered": 0, "aid_delivered": 0,
        "trade_failed": 0, "aid_failed": 0,
    }
    assert gs.outcome["score_parts"]["trade_aid"] == 0


# ---- 辅助：把协议直接布置到抵达前一天 ----

def _near_arrival_aid(gs, eng):
    req = aid_request(eng, eta=2)
    pact = eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    pact["status"] = "escorting"
    pact["travel_days"] = pact["eta"] - 1
    gs.aid_pact = dict(pact)
    return pact
