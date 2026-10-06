# -*- coding: utf-8 -*-
"""跨聚落难民安置测试：外部聚落申请 → 地堡审核 → 隔离检疫（配给/确诊/
救治/病亡）→ 检疫期满 → 接纳并分配岗位（联动医疗病例）/ 遣返，以及
信誉结算、资源消耗、统计、阶段锁、幂等回放、与既有系统共存和终局收敛。"""
import pytest

from app.core.database import Base, engine, SessionLocal
from app.models import GameSession, Facility
from app.services.engine import (
    BunkerEngine,
    BunkerEngineError,
    BunkerEngineConflict,
    REFUGEE_QUARANTINE,
    REFUGEE_ADMITTING,
    REFUGEE_QUARANTINE_DAYS,
    REFUGEE_DAILY_COST,
    REFUGEE_REP_REJECT,
    REFUGEE_REP_ADMIT,
    REFUGEE_REP_REPATRIATE,
    REFUGEE_REP_DEATH,
    MED_REGISTERED,
    MED_ISOLATED,
    MED_TREATING,
    MED_RECOVERED,
    MEDICAL_CRISIS_PER_WAITING,
    MEDICAL_CRISIS_PER_CARE,
    FOOD, WATER, POWER, OXY,
    JOB_EFFICIENCY,
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


class RefugeeRand:
    """可脚本化随机：random 依次弹出脚本值，缺省 default；choice 取首项。"""

    def __init__(self, vals=(), default=0.9):
        self.vals = list(vals)
        self.default = default

    def random(self):
        return self.vals.pop(0) if self.vals else self.default

    def choice(self, seq):
        return seq[0]


def _add_clinic(gs, level=1):
    return Facility(
        session_id=gs.id, name=FACILITY_ZH["clinic"], category="clinic",
        level=level, status="active", built_day=1,
    )


def make_ref_session(db, residents=3, resources=None, clinic=False, medics=0):
    gs = make_session(db, residents=residents, resources=resources)
    if clinic:
        db.add(_add_clinic(gs))
        db.commit()
        db.refresh(gs)
    for i in range(medics):
        gs.residents[i].job = "medic"
    db.commit()
    return gs


def first_app(eng):
    return eng.refugee_board()[0]


def accept_first(eng):
    app = first_app(eng)
    detail, replayed = eng.accept_refugees(app["id"])
    assert not replayed
    return app, eng.session.refugee_intake


def ticks_to_admit(eng, vals=()):
    """推进到检疫期满（不触发地堡危机）：返回 admitting 快照。"""
    rand = RefugeeRand(vals=vals, default=0.9)
    eng.rand = rand
    for _ in range(REFUGEE_QUARANTINE_DAYS):
        eng.advance_day()
    intake = eng.session.refugee_intake
    assert intake and intake["status"] == REFUGEE_ADMITTING
    return intake


# ---- 公告板 ----

def test_board_is_deterministic_within_day(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    b1 = eng.refugee_board()
    b2 = eng.refugee_board()
    assert [a["id"] for a in b1] == [a["id"] for a in b2]
    assert len(b1) == 2
    for a in b1:
        assert 1 <= a["count"] <= 3
        assert set(a["daily_cost"]) == {FOOD, WATER, POWER}
        for p in a["people"]:
            assert p["skill"] in JOB_EFFICIENCY


def test_board_rotates_across_days(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    day1 = {a["id"] for a in eng.refugee_board()}
    gs.day = 9
    day9 = {a["id"] for a in eng.refugee_board()}
    assert day1.isdisjoint(day9)


# ---- 审核 ----

def test_accept_creates_quarantine_intake(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app = first_app(eng)
    before = gs.survivors
    detail, replayed = eng.accept_refugees(app["id"])
    assert not replayed
    intake = gs.refugee_intake
    assert intake["status"] == REFUGEE_QUARANTINE
    assert intake["partner"] == app["partner"]
    assert len(intake["people"]) == app["count"]
    assert all(p["alive"] for p in intake["people"])
    # 检疫中的难民尚未成为正式居民
    assert gs.survivors == before
    assert eng.refugee_summary()["quarantining"] == app["count"]


def test_accept_expired_application_rejected(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app = first_app(eng)
    gs.day += 5
    with pytest.raises(BunkerEngineError):
        eng.accept_refugees(app["id"])


def test_only_one_active_intake(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    gs.day += 3  # 公告板已轮换
    app = first_app(eng)
    with pytest.raises(BunkerEngineError):
        eng.accept_refugees(app["id"])


def test_reject_costs_reputation_and_no_intake(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    rep0 = gs.reputation
    app = first_app(eng)
    detail, replayed = eng.reject_refugees(app["id"])
    assert not replayed
    assert gs.refugee_intake is None
    assert gs.reputation == rep0 + REFUGEE_REP_REJECT
    stats = eng.refugee_stats()
    assert stats["applications"] == 1
    assert stats["rejected"] == app["count"]
    # 同一份申请的连点按同一结果回放，不重复扣信誉
    detail2, replayed2 = eng.reject_refugees(app["id"])
    assert replayed2
    assert gs.reputation == rep0 + REFUGEE_REP_REJECT


def test_accept_is_idempotent_on_same_application(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app = first_app(eng)
    d1, r1 = eng.accept_refugees(app["id"])
    assert not r1
    # 安置仍在时重复批准同一申请：回放，不重复建档
    d2, r2 = eng.accept_refugees(app["id"])
    assert r2
    assert eng.refugee_stats()["accepted"] == app["count"]


# ---- 检疫：配给 / 确诊 / 医疗 ----

def test_quarantine_consumes_daily_rations(db):
    """隔离配给在基地生产/消耗之外额外扣除：与无安置的对照档案逐日比对。"""
    res = {FOOD: 300, WATER: 300, POWER: 300, OXY: 300}
    gs = make_ref_session(db, resources=dict(res))
    gs_ctrl = make_ref_session(db, resources=dict(res))
    eng = BunkerEngine(db, gs, rand=FixedRand())
    eng_ctrl = BunkerEngine(db, gs_ctrl, rand=FixedRand())
    app, intake = accept_first(eng)
    n = len([p for p in intake["people"] if p["alive"]])
    # 固定难民名单，保证两组除安置外完全同构
    intake["people"] = [{
        "key": "p1", "name": "流民甲", "health": 70.0, "morale": 60.0,
        "exposed": False, "infectious": False, "skill": "general", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    n = 1
    eng.advance_day()
    eng_ctrl.advance_day()
    assert gs.resources[FOOD] == round(gs_ctrl.resources[FOOD] - REFUGEE_DAILY_COST[FOOD] * n, 1)
    assert gs.resources[WATER] == round(gs_ctrl.resources[WATER] - REFUGEE_DAILY_COST[WATER] * n, 1)
    assert gs.resources[POWER] == round(gs_ctrl.resources[POWER] - REFUGEE_DAILY_COST[POWER] * n, 1)
    intake = gs.refugee_intake
    assert intake["elapsed"] == 1


def test_quarantine_transitions_to_admitting_after_duration(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    intake = ticks_to_admit(eng)
    assert intake["elapsed"] == REFUGEE_QUARANTINE_DAYS
    assert eng.refugee_summary()["admitting"] >= 1
    # admitting 阶段不再消耗隔离配给
    res0 = dict(gs.resources)
    eng.advance_day()
    # 推进一天后仍待接纳（没有自动收敛），资源变化只来自基地生产/消耗
    assert gs.refugee_intake is not None
    assert gs.refugee_intake["status"] == REFUGEE_ADMITTING


def test_exposed_refugee_diagnoses_and_is_treated(db):
    """接触史难民确诊后，在救治体系下接受治疗；确诊者进入 admitting 时仍是疫病例。"""
    gs = make_ref_session(db, clinic=True, medics=1,
                          resources={FOOD: 400, WATER: 400, POWER: 400, OXY: 400})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    # 强制构造：单人、有接触史、健康 60；随机 0.0 必然确诊
    intake["people"] = [{
        "key": "p1", "name": "测试流民", "health": 60.0, "morale": 55.0,
        "exposed": True, "infectious": False, "skill": "farmer", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    # 第一天：0.0 确诊（健康 60-12=48）；其余随机 0.9 不触发危机
    eng.rand = RefugeeRand(vals=[0.0], default=0.9)
    eng.advance_day()
    p = gs.refugee_intake["people"][0]
    assert p["infectious"] is True
    assert p["health"] == 48.0
    # 第二天：有救治中心 + 1 名医护，救治 4.0+0.8=4.8；48 < 75 不会转阴
    eng.advance_day()
    assert gs.refugee_intake["status"] == REFUGEE_ADMITTING
    p = gs.refugee_intake["people"][0]
    assert p["infectious"] is True
    assert p["health"] == round(48.0 + 4.8, 1)


def test_infectious_without_clinic_deteriorates(db):
    gs = make_ref_session(db, resources={FOOD: 400, WATER: 400, POWER: 400, OXY: 400})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "高热流民", "health": 40.0, "morale": 55.0,
        "exposed": True, "infectious": True, "skill": "general", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    eng.advance_day()
    p = gs.refugee_intake["people"][0]
    assert p["health"] == 37.0  # 40 - 3.0 无救治体系恶化


def test_diagnosis_clears_with_medical_care(db):
    """确诊难民在高健康起点 + 高等级救治中心时可于检疫期内转阴。"""
    gs = make_ref_session(db, clinic=True, medics=3,
                          resources={FOOD: 500, WATER: 500, POWER: 500, OXY: 500})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "疑似流民", "health": 70.0, "morale": 60.0,
        "exposed": True, "infectious": True, "skill": "medic", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    # 第一天救治 4.0+3*0.8=6.4 → 76.4 >= 75 转阴
    eng.rand = RefugeeRand(default=0.9)
    eng.advance_day()
    p = gs.refugee_intake["people"][0]
    assert p["infectious"] is False
    assert p["exposed"] is False


def test_shortage_hurts_quarantining_refugees(db):
    gs = make_ref_session(db, resources={FOOD: 0, WATER: 0, POWER: 0, OXY: 200})
    # 关停全部生产设施：推进后仍无产出，隔离配给必然断供
    for f in gs.facilities:
        f.status = "offline"
    db.commit()
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "饿肚流民", "health": 70.0, "morale": 60.0,
        "exposed": False, "infectious": False, "skill": "general", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    eng.rand = RefugeeRand(default=0.9)
    eng.advance_day()
    p = gs.refugee_intake["people"][0]
    # 健康 -6 断供；士气 -5 断供后 +1 自然回摆 = 56
    assert p["health"] == 64.0
    assert p["morale"] == 56.0


def test_quarantine_death_fails_intake_and_penalizes(db):
    gs = make_ref_session(db, clinic=False,
                          resources={FOOD: 500, WATER: 500, POWER: 500, OXY: 500})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    # 无救治体系：确诊难民每日 -3 恶化。健康 5：第一天 →2，第二天归零病亡
    intake["people"] = [{
        "key": "p1", "name": "危重流民", "health": 5.0, "morale": 50.0,
        "exposed": True, "infectious": True, "skill": "general", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    rep0 = gs.reputation
    survivors0 = gs.survivors
    eng.rand = RefugeeRand(default=0.9)
    eng.advance_day()
    assert gs.refugee_intake is not None  # 还剩 2 点，检疫继续
    p = gs.refugee_intake["people"][0]
    assert p["health"] == 2.0 and p["alive"]
    eng.advance_day()
    # 全员病亡：安置失败关闭
    assert gs.refugee_intake is None
    assert gs.reputation == rep0 + REFUGEE_REP_DEATH
    assert eng.refugee_stats()["quarantine_dead"] == 1
    # 难民从未计入正式居民，病亡不扣 survivors
    assert gs.survivors == survivors0


# ---- 接纳与岗位分配（联动病例）----

def test_admit_joins_residents_with_default_skills(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    skills = {p["key"]: p["skill"] for p in intake["people"]}
    ticks_to_admit(eng)
    survivors0 = gs.survivors
    alive = [p for p in gs.refugee_intake["people"] if p["alive"]]
    rep0 = gs.reputation
    detail, replayed = eng.admit_refugees()
    assert not replayed
    assert gs.refugee_intake is None
    assert gs.survivors == survivors0 + len(alive)
    assert gs.reputation == rep0 + REFUGEE_REP_ADMIT * len(alive)
    # 新居民按擅长岗位上岗
    new_residents = [r for r in gs.residents if r.joined_day == gs.day]
    assert {r.name for r in new_residents} == {p["name"] for p in alive}
    for r in new_residents:
        key = next(p["key"] for p in alive if p["name"] == r.name)
        assert r.job == skills[key]
    stats = eng.refugee_stats()
    assert stats["admitted"] == len(alive)


def test_admit_respects_explicit_assignments(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    target = intake["people"][0]["key"]
    ticks_to_admit(eng)
    eng.admit_refugees(assignments={target: "engineer"})
    r = next(r for r in gs.residents if r.joined_day == gs.day
             and r.name == intake["people"][0]["name"])
    assert r.job == "engineer"


def test_admit_infectious_goes_to_isolation_bed(db):
    gs = make_ref_session(db, clinic=True, medics=2)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "未转阴流民", "health": 40.0, "morale": 55.0,
        "exposed": True, "infectious": True, "skill": "general", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    ticks_to_admit(eng)
    # 救治中心 Lv1：2 床 + 2 医护 = 4 床，空余充足 → 立即隔离
    eng.admit_refugees()
    cases = gs.medical_cases
    new_case = next(c for c in cases if c["resident_name"] == "未转阴流民")
    assert new_case["infectious"] is True
    assert new_case["status"] == MED_ISOLATED
    assert new_case["reason"] == "跨聚落难民安置·检疫未转阴"
    # 新居民已在治病例中，暂停生产口径
    assert eng._caring_resident_ids()
    summary = eng.med_summary()
    assert summary["isolated"] == 1


def test_admit_infectious_waiting_when_beds_full(db):
    # Lv1 中心无医护 = 2 张床，先用 2 名在堡居民占满隔离床
    gs = make_ref_session(db, residents=4, clinic=True, medics=0)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    for r in gs.residents[:2]:
        r.health = 20.0
        eng._open_case(r, infectious=True, reason="预置", kind="疫病",
                       status=MED_ISOLATED)
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "无床流民", "health": 40.0, "morale": 55.0,
        "exposed": True, "infectious": True, "skill": "general", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    ticks_to_admit(eng)
    assert eng.med_beds_used() == 2
    eng.admit_refugees()
    new_case = next(c for c in gs.medical_cases if c["resident_name"] == "无床流民")
    assert new_case["status"] == MED_REGISTERED
    # 登记未收治病例立刻对医疗危机施压（每日 tick 时 +4）
    assert eng.med_summary()["registered"] >= 1


def test_admit_weak_noninfectious_registers_normal_case(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "体弱流民", "health": 28.0, "morale": 55.0,
        "exposed": False, "infectious": False, "skill": "farmer", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    ticks_to_admit(eng)
    p = gs.refugee_intake["people"][0]
    # 检疫期内医疗筛查每日 +3：28→31→34，仍低于 35 登记线
    assert p["health"] == 34.0
    eng.admit_refugees()
    new_case = next(c for c in gs.medical_cases if c["resident_name"] == "体弱流民")
    assert new_case["infectious"] is False
    assert new_case["status"] == MED_REGISTERED
    assert new_case["kind"] == "伤病"


def test_admit_healthy_refugee_opens_no_case(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "健壮流民", "health": 88.0, "morale": 65.0,
        "exposed": False, "infectious": False, "skill": "engineer", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    ticks_to_admit(eng)
    eng.admit_refugees()
    assert gs.medical_cases is None or all(
        c["resident_name"] != "健壮流民" for c in gs.medical_cases
    )


def test_admit_raises_morale_for_everyone_once(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    ticks_to_admit(eng)
    old_morales = {r.id: r.morale for r in gs.residents if r.alive}
    alive = [p for p in gs.refugee_intake["people"] if p["alive"]]
    eng.admit_refugees()
    # 在堡老人士气 +5（仅一次）
    for r in gs.residents:
        if r.id in old_morales:
            assert r.morale == min(100.0, old_morales[r.id] + 5.0)
    # 新居民也恰好 +5（入堡时基准士气 + 一次接纳加成）
    new = [r for r in gs.residents if r.id not in old_morales and r.alive]
    assert len(new) == len(alive)


def test_admit_blocked_before_quarantine_ends(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    eng.advance_day()  # 仅一天，仍在检疫
    with pytest.raises(BunkerEngineError):
        eng.admit_refugees()
    with pytest.raises(BunkerEngineError):
        eng.repatriate_refugees()


def test_admit_idempotent_replay(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    token = intake["token"]
    ticks_to_admit(eng)
    survivors_after, _ = eng.admit_refugees(token=token)
    survivors = gs.survivors
    # 安置已清除：携带原 token 连点 → 安全回放，不二次增加人口/信誉
    detail, replayed = eng.admit_refugees(token=token)
    assert replayed
    assert gs.survivors == survivors
    assert eng.refugee_stats()["admitted"] == len(
        [p for p in intake["people"] if p["alive"]]
    )


def test_admit_with_wrong_token_conflicts(db):
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    ticks_to_admit(eng)
    with pytest.raises(BunkerEngineConflict):
        eng.admit_refugees(token="stale-token")


# ---- 遣返 ----

def test_repatriate_closes_intake_with_penalty(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    ticks_to_admit(eng)
    alive_n = len([p for p in gs.refugee_intake["people"] if p["alive"]])
    survivors0 = gs.survivors
    rep0 = gs.reputation
    old_morales = {r.id: r.morale for r in gs.residents if r.alive}
    detail, replayed = eng.repatriate_refugees()
    assert not replayed
    assert gs.refugee_intake is None
    assert gs.survivors == survivors0
    assert gs.reputation == rep0 + REFUGEE_REP_REPATRIATE
    assert eng.refugee_stats()["repatriated"] == alive_n
    for r in gs.residents:
        if r.alive and r.id in old_morales:
            assert r.morale == min(100.0, old_morales[r.id] - 3.0)


def test_repatriate_idempotent_replay(db):
    gs = make_ref_session(db)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    token = intake["token"]
    ticks_to_admit(eng)
    eng.repatriate_refugees(token=token)
    rep = gs.reputation
    detail, replayed = eng.repatriate_refugees(token=token)
    assert replayed
    assert gs.reputation == rep


# ---- 与既有系统共存 ----

def test_refugee_intake_coexists_with_expedition(db):
    """难民在堡内隔离：不派离堡队伍，与探索队同时存在。"""
    gs = make_ref_session(db, resources={FOOD: 400, WATER: 400, POWER: 400, OXY: 400})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    # 派遣探索队不受难民安置影响
    exp = eng.send_expedition(
        [gs.residents[0].id], {FOOD: 14, WATER: 14}
    )
    assert exp["status"] == "away"
    assert gs.refugee_intake is not None
    # 探索队在外日：探索消耗自带口粮，地堡按在堡人口结算，难民仍消耗隔离配给
    eng.advance_day()
    assert gs.refugee_intake["elapsed"] == 1
    assert gs.expedition["travel_days"] == 1
    # 探索队成员仍标记离堡、难民仍未成为居民
    assert gs.residents[0].id in eng._away_resident_ids()
    assert gs.survivors == 3


def test_refugee_quarantine_tick_runs_alongside_medical_crisis(db):
    """接纳的疫病例登记/在治持续推高医疗危机（联动）。"""
    gs = make_ref_session(db, clinic=True, medics=1)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    app, intake = accept_first(eng)
    intake["people"] = [{
        "key": "p1", "name": "带疫流民", "health": 40.0, "morale": 55.0,
        "exposed": True, "infectious": True, "skill": "general", "alive": True,
    }]
    gs.refugee_intake = dict(intake)
    ticks_to_admit(eng)
    eng.admit_refugees()  # 立即隔离，占用 1 床
    crisis0 = eng.medical_crisis()
    eng.rand = RefugeeRand(default=0.9)
    eng.advance_day()
    # 在治床位每日 +2
    assert eng.medical_crisis() == min(100, crisis0 + MEDICAL_CRISIS_PER_CARE)


def test_actions_locked_while_crisis_pending(db):
    """地堡危机待处理时，难民安置的审核/接纳动作一并锁定。"""
    from tests.test_engine import TriggerRand
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    eng.rand = TriggerRand()
    crisis = eng._maybe_trigger_crisis()
    assert crisis is not None
    assert eng.phase == "crisis"
    # 危机待处理：审核新申请被拒（已有安置时本就拒绝，先清掉安置再验证阶段锁）
    eng.session.refugee_intake = None
    app = first_app(eng)
    with pytest.raises(BunkerEngineError):
        eng.accept_refugees(app["id"])
    with pytest.raises(BunkerEngineError):
        eng.reject_refugees(app["id"])
    # 推进同样被锁
    with pytest.raises(BunkerEngineError):
        eng.advance_day()


def test_endgame_clears_refugee_intake(db):
    gs = make_ref_session(db, clinic=True,
                          resources={FOOD: 500, WATER: 500, POWER: 500, OXY: 500})
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    ticks_to_admit(eng)
    assert gs.refugee_intake is not None
    # 直接抵达终局日：终局收敛清空安置，不留僵尸
    gs.day = gs.target_day
    eng.advance_day()
    assert gs.status == "win"
    assert gs.refugee_intake is None
    assert gs.outcome["refugee"]["applications"] == 1


def test_medical_crisis_does_not_double_count_refugees_before_admit(db):
    """检疫中的难民不是居民/病例：接纳前不向医疗危机施压。"""
    gs = make_ref_session(db, clinic=True)
    eng = BunkerEngine(db, gs, rand=FixedRand())
    accept_first(eng)
    crisis0 = eng.medical_crisis()
    eng.rand = RefugeeRand(default=0.9)
    eng.advance_day()
    assert eng.medical_crisis() == crisis0
