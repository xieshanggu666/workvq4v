# -*- coding: utf-8 -*-
"""结局贡献评分测试：生存基础分之外，医疗救治、难民安置、贸易援助按终局
成败计入总分；撤销/驳回/逾期不计分；成功/失败只计一次（幂等回放不重复）；
旧存档（无 mission_stats / 旧 outcome 口径）仍能正常加载与结算。"""
import json

import pytest
from sqlalchemy import text

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession, Facility
from app.core.migration import ensure_schema
from app.services.engine import (
    BunkerEngine,
    FOOD, WATER, POWER, OXY,
    MED_RECOVERED, MED_DECEASED, MED_TREATING,
    SCORE_MED_PER_RECOVERED, SCORE_MED_PER_CARE_DAY, SCORE_MED_PER_DECEASED,
    SCORE_REFUGEE_PER_ADMITTED,
    SCORE_TRADE_PER_DELIVERED, SCORE_TRADE_PER_FAILED,
    SCORE_AID_PER_DELIVERED, SCORE_AID_PER_FAILED,
)
from tests.test_engine import make_session, FixedRand
from tests.test_trade import TradeRand, rescue_offer


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


def _finish(eng):
    """直接触发终局结算（抵达目标日口径），返回 outcome。"""
    eng.session.day = eng.session.target_day
    eng._check_end()
    assert eng.session.status in ("win", "over")
    return eng.session.outcome


# ---- 基础分与贡献分合计 ----

def test_score_equals_base_plus_contributions(db):
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    outcome = _finish(eng)
    assert outcome["score"] == gs.score
    assert outcome["score_base"] > 0
    # 无任何外部贡献时总分等于基础分
    assert outcome["contributions"]["total_bonus"] == 0
    assert gs.score == outcome["score_base"]


def test_medical_contribution_counts_recovered_care_days_and_deaths(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    # 构造履历：居民0 康复（3 床日）、居民1 病亡（2 床日）、居民2 无病例
    cases = [
        {**eng._build_case_dict(gs.residents[0], False, "测试康复", status=MED_TREATING),
         "status": MED_RECOVERED, "days_cared": 3, "close_day": 10},
        {**eng._build_case_dict(gs.residents[1], True, "测试病亡", status=MED_TREATING),
         "status": MED_DECEASED, "days_cared": 2, "close_day": 11},
    ]
    eng._save_cases(cases)
    outcome = _finish(eng)
    med = outcome["contributions"]["medical"]
    expected = (1 * SCORE_MED_PER_RECOVERED + 5 * SCORE_MED_PER_CARE_DAY
                + 1 * SCORE_MED_PER_DECEASED)
    assert med["points"] == expected
    assert outcome["medical"]["recovered"] == 1
    assert outcome["medical"]["deceased"] == 1
    assert outcome["medical"]["care_days"] == 5
    assert gs.score == outcome["score_base"] + expected


# ---- 贸易/援助：成败计数与撤销/驳回不计 ----

def _deliver_rescue(eng, gs):
    """走通一笔求援订单（eta=2）：审核通过当日走首个在途日，次日抵达交付。"""
    offer = rescue_offer(eng)
    # day2：审核通过(0.1)、在途事件判定不触发(0.9)、地堡危机不触发(0.9)
    # day3：抵达交付成功(0.1)（掷点后默认 rand 0.9 不触发地堡危机）
    eng.rand = TradeRand([0.1, 0.9, 0.9, 0.1])
    eng.apply_trade(offer["id"], [gs.residents[0].id])
    pending = eng.advance_day()   # 审核通过 + 在途日（未抵达）
    assert pending is None
    assert gs.trade_order is not None
    pending = eng.advance_day()   # 抵达交付成功
    assert pending is None
    assert gs.trade_order is None
    assert gs.pending_crisis is None


def test_trade_delivery_counts_once_and_replay_does_not_double_count(db):
    gs = make_session(db)
    eng = BunkerEngine(db, gs, rand=TradeRand())
    _deliver_rescue(eng, gs)
    assert eng.mission_stats()["trade_delivered"] == 1
    # 终局后成功交付计入贡献
    outcome = _finish(eng)
    assert outcome["mission"]["trade_delivered"] == 1
    assert outcome["contributions"]["mission"]["points"] == SCORE_TRADE_PER_DELIVERED
    assert gs.score == outcome["score_base"] + SCORE_TRADE_PER_DELIVERED


def test_trade_failure_counts_negative_once(db):
    gs = make_session(db)
    # day2：审核通过(0.1)、在途无事件(0.9)
    # day3：抵达交付失败（0.9 >= 0.75 成功率）、当日不触发地堡危机(0.9)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9, 0.9, 0.9]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    eng.advance_day()
    assert gs.trade_order is not None
    eng.advance_day()
    assert gs.trade_order is None
    assert eng.mission_stats()["trade_failed"] == 1
    outcome = _finish(eng)
    assert outcome["mission"]["trade_failed"] == 1
    assert outcome["mission"]["trade_delivered"] == 0
    assert outcome["contributions"]["mission"]["points"] == SCORE_TRADE_PER_FAILED


def test_review_rejection_and_cancel_do_not_score(db):
    """审核驳回与审核中撤单均未形成援助结果，不进入终局统计。"""
    gs = make_session(db)
    # 审核驳回：random 0.99 >= 通过率
    eng = BunkerEngine(db, gs, rand=TradeRand([0.99]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    eng.advance_day()
    assert gs.trade_order is None
    # 第二笔：审核中主动撤单（每日公告板轮换，取当日任一求援报价）
    eng.rand = TradeRand()
    offer2 = next(o for o in eng.trade_market() if o["type"] == "rescue")
    order = eng.apply_trade(offer2["id"], [gs.residents[1].id])
    eng.cancel_trade(token=order["token"])
    assert gs.trade_order is None
    assert eng.mission_stats() == {
        "trade_delivered": 0, "trade_failed": 0, "aid_delivered": 0, "aid_failed": 0,
    }
    outcome = _finish(eng)
    assert outcome["contributions"]["mission"]["points"] == 0


def test_settlement_replay_does_not_bump_stats_again(db):
    """交付/失败的幂等回放不得把同一笔终局二次计分。"""
    gs = make_session(db)
    # day2：审核通过(0.1)、在途无事件(0.9)；day3：交付失败(0.9)、不触发危机(0.9)
    eng = BunkerEngine(db, gs, rand=TradeRand([0.1, 0.9, 0.9, 0.9]))
    eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[0].id])
    eng.advance_day()
    eng.advance_day()  # 抵达交付失败，订单收敛
    assert gs.trade_order is None
    assert eng.mission_stats()["trade_failed"] == 1
    # 用结算凭据安全回放：统计不增加
    rec = eng._read_credential("incident")
    detail, replayed = eng.reconcile_stale_trade("settle", token=rec.get("host_token"))
    assert replayed is True
    assert eng.mission_stats()["trade_failed"] == 1


# ---- 难民安置贡献 ----

def test_refugee_admission_contributes(db):
    """通过真实安置链路接纳难民：终局贡献按接纳人数计分。"""
    from tests.test_refugee import make_ref_session, accept_first, ticks_to_admit

    gs = make_ref_session(db, residents=4,
                          resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, _intake = accept_first(eng)
    ticks_to_admit(eng)
    detail, _ = eng.admit_refugees()
    admitted = eng.refugee_stats()["admitted"]
    assert admitted == app["count"]
    outcome = _finish(eng)
    ref = outcome["contributions"]["refugee"]
    assert ref["points"] == admitted * SCORE_REFUGEE_PER_ADMITTED
    assert outcome["refugee"]["admitted"] == admitted
    assert gs.score == outcome["score_base"] + outcome["contributions"]["total_bonus"]


# ---- 联盟援助贡献（成功 + 检疫未过失败）----

def _aid_session_engine(db, gs_kwargs=None):
    from tests.test_aid import make_aid_session, assign_medics, aid_request

    gs = make_aid_session(db, clinic=True, **(gs_kwargs or {}))
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs)
    req = aid_request(eng)
    return gs, eng, req


def test_aid_delivery_counts_in_contributions(db):
    from tests.test_aid import AidRand

    gs, eng, req = _aid_session_engine(
        db, {"resources": {FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999}}
    )
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    # 签约日同时走首个在途日：签约通过(0.1)、在途无事件(0.9)
    eng.rand = AidRand([0.1, 0.9])
    pending = eng.advance_day()  # 签约 + 在途日
    assert pending is None
    # 抵达日：检疫关卡通过(0.1)、交付成功，当日不触发地堡危机(0.9)
    eng.rand = AidRand([0.1, 0.1, 0.9])
    pending = eng.advance_day()  # 检疫通过并交付
    assert pending is None
    assert gs.aid_pact is None
    assert eng.mission_stats()["aid_delivered"] == 1
    outcome = _finish(eng)
    assert outcome["mission"]["aid_delivered"] == 1
    assert outcome["contributions"]["mission"]["points"] == SCORE_AID_PER_DELIVERED


def test_aid_gate_failure_counts_negative_and_cancel_does_not(db):
    from tests.test_aid import AidRand

    gs, eng, req = _aid_session_engine(
        db, {"resources": {FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999}}
    )
    pact = eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    # 撤约：不计分
    detail, replayed = eng.cancel_aid(token=pact["token"])
    assert not replayed
    assert eng.mission_stats()["aid_delivered"] == 0
    assert eng.mission_stats()["aid_failed"] == 0
    # 再来一份（eta=2）：签约通过 → 在途无事件
    req2 = next(q for q in eng.aid_board() if q["eta"] == 2)
    eng.propose_aid(req2["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.rand = AidRand([0.1, 0.9])
    pending = eng.advance_day()
    assert pending is None
    # 抵达日：检疫关卡未过（0.99）判失败，当日不触发地堡危机(0.9)
    eng.rand = AidRand([0.99, 0.9])
    pending = eng.advance_day()
    assert pending is None
    assert gs.aid_pact is None
    assert eng.mission_stats()["aid_failed"] == 1
    outcome = _finish(eng)
    assert outcome["contributions"]["mission"]["points"] == SCORE_AID_PER_FAILED


# ---- 旧存档兼容 ----

def test_old_save_without_mission_stats_settles_normally(db):
    """缺 mission_stats 列的旧库迁移后：旧运行档案可正常结算，贡献从零计。"""
    gs = make_session(db, resources={FOOD: 9999, WATER: 9999, POWER: 9999, OXY: 9999})
    db.commit()
    db.expire_all()
    # 模拟旧版表结构：移除 mission_stats 列（SQLite 经表重建）
    cols = [
        "id", "row_version", "name", "day", "target_day", "status", "resources",
        "survivors", "pending_crisis", "last_resolution", "expedition",
        "last_expedition", "trade_order", "last_trade", "aid_pact", "last_aid",
        "medical_crisis", "refugee_intake", "last_refugee", "refugee_stats",
        "medical_cases", "reputation", "outcome", "score", "created_at", "updated_at",
    ]
    collist = ", ".join(cols)
    db.execute(text("ALTER TABLE game_sessions RENAME TO gs_old"))
    db.execute(text(
        "CREATE TABLE game_sessions AS SELECT " + collist + " FROM gs_old"
    ))
    db.execute(text("DROP TABLE gs_old"))
    db.commit()

    ensure_schema(engine)
    db.expire_all()
    loaded = db.get(GameSession, gs.id)
    eng = BunkerEngine(db, loaded, rand=FixedRand())
    outcome = _finish(eng)
    assert loaded.status == "win"
    assert outcome["mission"] == {
        "trade_delivered": 0, "trade_failed": 0, "aid_delivered": 0, "aid_failed": 0,
    }
    assert outcome["contributions"]["total_bonus"] == 0
    assert loaded.score == outcome["score_base"]


def test_already_ended_old_outcome_loads_and_keeps_legacy_score(db):
    """迁移前已结束的旧档案：旧 outcome 无 contributions 仍可只读加载，
    分数不被重算（终局幂等，不因版本升级二次结算）。"""
    gs = make_session(db)
    legacy_outcome = {
        "win": True,
        "reason": "旧版结局",
        "survivors": 3,
        "day": 120,
        "avg_health": 88.0,
        "medical": {"total": 0, "active": 0, "recovered": 0, "deceased": 0,
                    "care_days": 0, "medical_crisis": 0},
        "refugee": {"applications": 0, "accepted": 0, "admitted": 0,
                    "rejected": 0, "repatriated": 0, "quarantine_dead": 0},
    }
    gs.status = "win"
    gs.outcome = legacy_outcome
    gs.score = 12345
    db.commit()

    ensure_schema(engine)
    db.expire_all()
    loaded = db.get(GameSession, gs.id)
    assert loaded.status == "win"
    assert loaded.score == 12345
    assert loaded.outcome.get("contributions") is None  # 旧口径无贡献分项，保持不变
    eng = BunkerEngine(db, loaded)
    assert eng.phase == "ended"
