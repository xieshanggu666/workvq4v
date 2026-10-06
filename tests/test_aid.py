# -*- coding: utf-8 -*-
"""地堡联盟援助协议测试：三方签约 → 托管 → 外部聚落签约审核 → 押运运输
（途中事件）→ 押运审核（检疫关卡）→ 交付，以及信誉/资源/医疗危机回写、
阶段锁、幂等回放与终局收敛。"""
import pytest

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession, Facility
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    AID_INCIDENTS,
    AID_PROPOSED,
    AID_ESCORTING,
    MED_REGISTERED,
    MED_TREATING,
    MED_RECOVERED,
    MEDICAL_CRISIS_GATE_FAIL,
    MEDICAL_CRISIS_OUTBREAK,
    MEDICAL_CRISIS_RELIEF,
    FOOD, WATER, POWER, OXY,
    RESOURCE_KEYS,
    FACILITY_ZH,
)
from tests.test_engine import make_session, FixedRand


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    Base.metadata.drop_all(bind=engine)


class AidRand:
    """可脚本化的援助随机：依次弹出 random() 值，choice 取事件池首项。"""

    def __init__(self, random_vals=()):
        self.vals = list(random_vals)

    def random(self):
        return self.vals.pop(0) if self.vals else 0.9

    def choice(self, seq):
        return seq[0]


def _add_clinic(gs, level=1):
    return Facility(
        session_id=gs.id, name=FACILITY_ZH["clinic"], category="clinic",
        level=level, status="active", built_day=1,
    )


def make_aid_session(db, residents=3, resources=None, clinic=False):
    gs = make_session(db, residents=residents, resources=resources)
    if clinic:
        db.add(_add_clinic(gs))
        db.commit()
        db.refresh(gs)
    return gs


def aid_request(eng, eta=None):
    board = eng.aid_board()
    if eta is not None:
        return next(q for q in board if q["eta"] == eta)
    return min(board, key=lambda q: q["eta"])


def assign_medics(gs, *indexes):
    for i in indexes:
        gs.residents[i].job = "medic"


# ---- 公告板 ----

def test_board_is_deterministic_within_day(db):
    gs = make_aid_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    b1 = eng.aid_board()
    b2 = eng.aid_board()
    assert [q["id"] for q in b1] == [q["id"] for q in b2]
    assert len(b1) == 3


def test_board_rotates_across_days(db):
    gs = make_aid_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    day1 = {q["id"] for q in eng.aid_board()}
    gs.day = 8
    day8 = {q["id"] for q in eng.aid_board()}
    assert day1.isdisjoint(day8)


# ---- 三方签约 ----

def test_propose_requires_medical_party(db):
    gs = make_aid_session(db)  # 无救治中心、无医护：不具备签约资质
    eng = BunkerEngine(db, gs, rand=FixedRand())
    req = aid_request(eng)
    with pytest.raises(BunkerEngineError):
        eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    # 有在任医护即可签约
    assign_medics(gs, 0)
    pact = eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    assert pact["status"] == AID_PROPOSED
    assert pact["signer_id"] == gs.residents[0].id


def test_propose_signer_must_be_medic(db):
    gs = make_aid_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    req = aid_request(eng)
    with pytest.raises(BunkerEngineError):
        # 救治中心虽在，但会签人不是在任医护
        eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    # 有未结病例的医护也不能会签
    assign_medics(gs, 0, 1)
    eng._open_case(gs.residents[0], infectious=False, reason="测试")
    with pytest.raises(BunkerEngineError):
        eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])


def test_propose_freezes_escrow_and_enters_proposed(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    req = aid_request(eng)
    before = dict(gs.resources)
    pact = eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    assert pact["status"] == AID_PROPOSED
    assert pact["escrow"] == req["escrow"]
    assert pact["cargo"] == req["cargo"]
    for k, v in req["escrow"].items():
        assert gs.resources[k] == round(before[k] - v, 1)
    # proposed 阶段押运队尚未离堡，仍参与地堡生产
    assert gs.residents[1].id not in eng._away_resident_ids()


def test_propose_validates_escorts(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    req = aid_request(eng)
    with pytest.raises(BunkerEngineError):
        eng.propose_aid(req["id"], gs.residents[0].id, [])
    with pytest.raises(BunkerEngineError):
        eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id] * 2)
    with pytest.raises(BunkerEngineError):
        eng.propose_aid(req["id"], gs.residents[0].id, [9999])


def test_propose_rejects_expired_request(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    req = aid_request(eng)
    gs.day = 9
    with pytest.raises(BunkerEngineError):
        eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])


def test_only_one_aid_pact_at_a_time(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.propose_aid(aid_request(eng)["id"], gs.residents[0].id, [gs.residents[1].id])
    with pytest.raises(BunkerEngineError):
        eng.propose_aid(aid_request(eng)["id"], gs.residents[0].id, [gs.residents[2].id])


def test_aid_mutually_exclusive_with_trade_and_expedition(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.propose_aid(aid_request(eng)["id"], gs.residents[0].id, [gs.residents[1].id])
    # 协议在谈期间不能派探索队，也不能申请贸易订单
    with pytest.raises(BunkerEngineError):
        eng.send_expedition([gs.residents[2].id], {FOOD: 5, WATER: 5})
    from tests.test_trade import rescue_offer
    with pytest.raises(BunkerEngineError):
        eng.apply_trade(rescue_offer(eng)["id"], [gs.residents[2].id])


def test_signer_and_escort_medic_job_is_locked(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng.propose_aid(aid_request(eng)["id"], gs.residents[0].id, [gs.residents[1].id])
    with pytest.raises(BunkerEngineError):
        eng.set_job(gs.residents[0].id, "general")  # 医护负责人锁定
    with pytest.raises(BunkerEngineError):
        eng.set_job(gs.residents[1].id, "general")  # 押运队医护锁定
    # 无协议职责的医护可正常调岗
    eng.set_job(gs.residents[2].id, "medic")
    eng.set_job(gs.residents[2].id, "general")


# ---- 外部聚落签约审核 ----

def test_signing_rejection_refunds_and_penalizes(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs, rand=AidRand([0.99]))  # 通过率 0.80：0.99 被拒
    req = aid_request(eng)
    before = dict(gs.resources)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()
    assert gs.aid_pact is None
    assert gs.reputation == 47  # -3
    for k, v in req["escrow"].items():
        assert gs.resources[k] >= before[k] - v  # 托管全额带回（当日生产另计）


def test_signing_approval_starts_escorting(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.9]))  # 通过、无事件
    req = aid_request(eng)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()
    assert gs.aid_pact is not None
    assert gs.aid_pact["status"] == AID_ESCORTING
    assert gs.aid_pact["travel_days"] == 1
    assert gs.residents[1].id in eng._away_resident_ids()


def test_signing_lapses_when_signer_unqualified(db):
    """签约日前医护负责人伤亡/调岗：协议逾期关闭，全额退款且不罚信誉。"""
    gs = make_aid_session(db)
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.9]))
    req = aid_request(eng)
    before = dict(gs.resources)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    gs.residents[0].job = "general"  # 医护负责人在签约前被调离
    eng.advance_day()
    assert gs.aid_pact is None
    assert gs.reputation == 50  # 逾期不罚信誉
    for k, v in req["escrow"].items():
        assert gs.resources[k] >= before[k] - v


# ---- 撤约 ----

def test_cancel_during_proposed_refunds_fully(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    before = dict(gs.resources)
    pact = eng.propose_aid(aid_request(eng)["id"], gs.residents[0].id, [gs.residents[1].id])
    detail, replayed = eng.cancel_aid(token=pact["token"])
    assert replayed is False
    assert gs.aid_pact is None
    assert gs.resources == before
    # 连点幂等回放
    detail2, replayed2 = eng.cancel_aid(token=pact["token"])
    assert replayed2 is True
    assert detail2 == detail
    assert gs.resources == before  # 没有第二次退款


def test_cancel_blocked_after_escorting_starts(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.9]))
    pact = eng.propose_aid(aid_request(eng)["id"], gs.residents[0].id, [gs.residents[1].id])
    token = pact["token"]
    eng.advance_day()  # 签约通过，进入押运
    with pytest.raises(BunkerEngineConflict):
        eng.cancel_aid(token=token)


# ---- 押运运输与途中事件 ----

def test_incident_pends_and_locks_phase(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.1]))
    eng.propose_aid(aid_request(eng, eta=2)["id"], gs.residents[0].id, [gs.residents[1].id])
    incident = eng.advance_day()
    assert incident is not None
    assert incident["event"] == AID_INCIDENTS[0]["key"]
    assert eng.phase == "aid"
    with pytest.raises(BunkerEngineError):
        eng.advance_day()
    with pytest.raises(BunkerEngineError):
        eng.build_facility("med")


def test_incident_cargo_loss_reduces_delivery(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.1]))
    req = aid_request(eng, eta=2)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    incident = eng.advance_day()
    # 分出部分物资安抚：货损 20%，信誉 +3
    eng.resolve_aid_incident("share_rations", token=incident["token"])
    assert gs.aid_pact["cargo_ratio"] == 0.8
    assert gs.reputation == 53
    # 抵达日：检疫关卡通过、交付
    eng.rand = AidRand([0.1])
    eng.advance_day()
    assert gs.aid_pact is None


def test_incident_targeted_choice_binds_member(db):
    gs = make_aid_session(db, residents=3)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.1]))
    eng.propose_aid(aid_request(eng, eta=2)["id"], gs.residents[0].id,
                    [gs.residents[1].id, gs.residents[2].id])
    incident = eng.advance_day()
    assert incident["needs_target"]
    eng.resolve_aid_incident("stand_guard", token=incident["token"])
    target = next(r for r in gs.residents if r.id == incident["target_id"])
    assert target.health < 90
    for r in gs.residents:
        if r.id != incident["target_id"]:
            assert r.health == 90


def test_resolved_incident_is_idempotent(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.1]))
    eng.propose_aid(aid_request(eng, eta=2)["id"], gs.residents[0].id, [gs.residents[1].id])
    incident = eng.advance_day()
    d1, rp1 = eng.resolve_aid_incident("share_rations", token=incident["token"])
    assert rp1 is False
    d2, rp2 = eng.resolve_aid_incident("share_rations", token=incident["token"])
    assert rp2 is True
    assert d2 == d1
    assert gs.aid_pact["cargo_ratio"] == 0.8  # 货损只施加一次


def test_quarantine_incident_registers_infectious_case(db):
    """检疫暴露（具传染性）受伤队员由医疗救治中心承接为传染病例（随队冻结）。"""
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.9]))
    req = aid_request(eng, eta=2)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()  # 签约通过，进入押运
    # 在押运中的协议上构造一场检疫暴露事件（事件池第二项）
    event = next(e for e in AID_INCIDENTS if e["key"] == "quarantine")
    pending = eng._build_aid_incident(event, gs.aid_pact)
    gs.aid_pact["pending_incident"] = pending
    gs.residents[1].health = 30  # 受伤后跌破自动登记线
    eng.resolve_aid_incident("field_decon", token=pending["token"])
    case = next(c for c in gs.medical_cases if c["resident_id"] == gs.residents[1].id)
    assert case["infectious"] is True
    # 病例随在途押运队冻结，不占在堡床位
    assert eng.med_summary()["frozen_away"] == 1


def test_abort_settles_pact_immediately(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.1]))
    req = aid_request(eng, eta=2)
    before = dict(gs.resources)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    incident = eng.advance_day()
    detail, _ = eng.resolve_aid_incident("abandon", token=incident["token"])
    assert gs.aid_pact is None
    assert gs.reputation == 40  # -10
    assert gs.medical_crisis == MEDICAL_CRISIS_OUTBREAK
    # 弃货时货损为 0：托管全额带回
    for k, v in req["escrow"].items():
        assert gs.resources[k] >= before[k] - v
    # 疫情反扑：在堡最虚弱者立为传染病例
    assert any(c.get("infectious") for c in (gs.medical_cases or []))
    # 同一事件请求重放：不再二次结算
    detail2, replayed = eng.resolve_aid_incident("abandon", token=incident["token"])
    assert replayed is True
    assert detail2 == detail


# ---- 押运审核（检疫关卡）与交付 ----

def _near_arrival_pact(gs, escrow=None, cargo=None, ratio=1.0):
    gs.aid_pact = {
        "token": "pact-tok", "request_id": "x", "partner": "valley",
        "partner_name": "幽谷定居点", "eta": 1, "status": AID_ESCORTING,
        "proposed_day": gs.day, "signer_id": gs.residents[0].id,
        "signer_name": gs.residents[0].name, "escorts": [gs.residents[1].id],
        "escrow": escrow or {POWER: 20}, "cargo": cargo or {FOOD: 30},
        "cargo_ratio": ratio, "travel_days": 0, "incidents_resolved": 0,
        "pending_incident": None,
    }


def test_gate_failure_refunds_and_raises_crisis(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.95]))  # 关卡通过率 0.90：0.95 未过
    _near_arrival_pact(gs)
    eng.advance_day()
    assert gs.aid_pact is None
    assert gs.reputation == 44  # -6
    assert gs.medical_crisis == MEDICAL_CRISIS_GATE_FAIL


def test_successful_delivery_grants_cargo_relief_and_reputation(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    # 布置一名在堡在治病例，健康贴近康复线，验证援助救治回写
    victim = gs.residents[2]
    victim.health = 60
    eng = BunkerEngine(db, gs, rand=AidRand([0.1]))  # 关卡通过 → 交付
    eng._open_case(victim, infectious=False, reason="测试伤病", status=MED_TREATING)
    _near_arrival_pact(gs)
    hp_before = {r.id: r.health for r in gs.residents}
    eng.advance_day()
    assert gs.aid_pact is None
    assert gs.reputation == 58  # +8
    # 医疗危机缓解（初始为 0，缓解后仍为 0）
    assert gs.medical_crisis == 0
    # 全员获得援助治疗
    for r in gs.residents:
        assert r.health > hp_before[r.id]
    # 贴近康复线的病例经援助治疗后结案为康复
    case = next(c for c in gs.medical_cases if c["resident_id"] == victim.id)
    assert case["status"] == MED_RECOVERED
    # 回礼入库（当日生产另计，只断言食物严格增加了一笔）
    assert gs.resources[FOOD] >= 30


def test_delivery_relief_reduces_existing_crisis(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0, 1)
    gs.medical_crisis = 80
    eng = BunkerEngine(db, gs, rand=AidRand([0.1]))
    _near_arrival_pact(gs)
    # 当日医疗结算先施压（1 名在堡病例都没有：不施压），交付再缓解 35
    eng.advance_day()
    assert gs.medical_crisis == 80 - MEDICAL_CRISIS_RELIEF


def test_failed_outbreak_without_clinic_only_damages_health(db):
    """未建救治中心时援助失败：无病例体系承接，仅在堡健康损伤，不建档。"""
    gs = make_aid_session(db, clinic=False)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.1]))
    req = aid_request(eng, eta=2)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    incident = eng.advance_day()
    eng.resolve_aid_incident("abandon", token=incident["token"])
    assert gs.aid_pact is None
    assert gs.medical_crisis == MEDICAL_CRISIS_OUTBREAK
    assert not gs.medical_cases


# ---- 医疗危机回写：每日压力与高压士气 ----

def test_medical_crisis_rises_with_active_cases(db):
    gs = make_aid_session(db, clinic=True)
    assign_medics(gs, 0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    victim = gs.residents[1]
    victim.health = 30
    eng._open_case(victim, infectious=False, reason="测试", status=MED_REGISTERED)
    eng.advance_day()
    # 登记未收治病例每日 +4 压力
    assert gs.medical_crisis == 4
    victim.health = 40  # 保持在康复线以下，避免收治当日即康复结案
    eng.admit_case(victim.id, MED_TREATING)
    eng.advance_day()
    # 在治病例每日 +2 压力（病例仍活跃）
    assert gs.medical_crisis == 6
    assert eng._has_active_case(victim.id)


def test_high_crisis_drains_morale(db):
    gs = make_aid_session(db)
    gs.medical_crisis = 70
    eng = BunkerEngine(db, gs, rand=FixedRand())
    morale_before = gs.residents[0].morale
    eng.advance_day()
    # 高压阈值下在堡全员每日额外 -3 士气（自然回落 -0.3，净降超过 2）
    assert gs.residents[0].morale < morale_before - 2


# ---- 离堡口径 ----

def test_escorting_aid_excludes_escorts_from_consumption(db):
    gs_ref = make_aid_session(db)
    eng_ref = BunkerEngine(db, gs_ref, rand=AidRand([0.9]))
    eng_ref.advance_day()
    ref = gs_ref.resources[FOOD]
    eng_ref.advance_day()
    delta_ref = gs_ref.resources[FOOD] - ref

    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.9]))  # 通过、无事件
    req = aid_request(eng, eta=4)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()  # 签约 + 第一个在途日
    food = gs.resources[FOOD]
    eng.rand = AidRand([0.9])
    eng.advance_day()  # 完整在途日
    delta_aid = gs.resources[FOOD] - food
    # 唯一差别：1 名押运队员离堡不消耗地堡口粮（少消耗 1.5）
    assert round(delta_aid - delta_ref, 1) == 1.5


def test_aid_escorts_skip_bunker_crisis_effects(db):
    """在途援助押运队员不参与地堡危机的全体效果（口径同探索/贸易）。"""
    from app.services.engine import CRISIS_POOL
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.9]))
    eng.propose_aid(aid_request(eng, eta=4)["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()
    assert gs.residents[1].id in eng._away_resident_ids()
    crisis = eng._build_crisis(next(e for e in CRISIS_POOL if e["key"] == "mutiny"))
    gs.pending_crisis = crisis
    before = gs.residents[1].morale
    eng.resolve_crisis("mutiny", "suppress")
    assert gs.residents[1].morale == before  # 离堡队员不受地堡危机影响


# ---- 终局收敛 ----

def test_endgame_clears_escorting_pact(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.9]))
    eng.propose_aid(aid_request(eng, eta=4)["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()  # 已在途
    gs.day = gs.target_day
    eng.advance_day()
    assert gs.status == "win"
    assert gs.aid_pact is None


def test_proposed_pact_on_endgame_is_cancelled_with_refund(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0)
    gs.day = gs.target_day
    eng = BunkerEngine(db, gs, rand=FixedRand())
    req = aid_request(eng)
    before = dict(gs.resources)
    eng.propose_aid(req["id"], gs.residents[0].id, [gs.residents[1].id])
    eng.advance_day()  # 终局日：不再签约，撤约退款
    assert gs.status == "win"
    assert gs.aid_pact is None
    for k, v in req["escrow"].items():
        assert gs.resources[k] >= before[k] - v


def test_delivery_gain_does_not_rescue_collapse(db):
    """抵达交付当日地堡已全线枯竭：回礼入库不得把败局"救回"。"""
    gs = make_aid_session(db, resources={k: 0 for k in RESOURCE_KEYS})
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1]))
    _near_arrival_pact(gs, cargo={FOOD: 40, WATER: 40, OXY: 40})
    eng.advance_day()
    assert gs.status == "over"
    assert gs.aid_pact is None


# ---- 快照持久化 ----

def test_pact_persisted_and_recoverable_after_reload(db):
    gs = make_aid_session(db)
    assign_medics(gs, 0, 1)
    eng = BunkerEngine(db, gs, rand=AidRand([0.1, 0.1]))
    eng.propose_aid(aid_request(eng, eta=2)["id"], gs.residents[0].id, [gs.residents[1].id])
    incident = eng.advance_day()
    token = incident["token"]
    db.commit()
    sid = gs.id

    db2 = SessionLocal()
    try:
        gs2 = db2.get(GameSession, sid)
        eng2 = BunkerEngine(db2, gs2, rand=FixedRand())
        assert eng2.phase == "aid"
        assert gs2.aid_pact["pending_incident"]["token"] == token
        eng2.resolve_aid_incident("share_rations", token=token)
        db2.commit()
    finally:
        db2.close()
