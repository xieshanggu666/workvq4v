# -*- coding: utf-8 -*-
"""末日地堡生存核心引擎。

资源守恒循环：
  每日净变化 = 设施产出 - 人口消耗 - 运营损耗
  产出受设施等级 + 人力资源(工程师/农夫加成) + 士气系数影响
"""

from sqlalchemy.orm import Session

from ..models import GameSession, Resident, Facility, EventLog
from ..core.config import INITIAL_RESOURCES, SURVIVAL_TARGET_DAY

import uuid

# 资源键
FOOD, WATER, POWER, OXY = "food", "water", "power", "oxygen"
RESOURCE_KEYS = [FOOD, WATER, POWER, OXY]

# 每日人均基础消耗
BASE_CONSUME = {FOOD: 1.5, WATER: 1.3, POWER: 1.0, OXY: 0.8}

# 设施基础产出（等级1）
FACILITY_OUTPUT = {
    "farm": {FOOD: 6.0, POWER: -1.5},   # 菜园产食物，耗电
    "water": {WATER: 7.0, POWER: -1.0}, # 净水器产水，耗电
    "power": {POWER: 8.0},              # 发电机产电
    "oxygen": {OXY: 6.0, POWER: -1.0},  # 水培/制氧耗电产氧
    "med": {},                          # 医疗舱：全体被动回复健康，微耗电
    "clinic": {POWER: -2.0},            # 医疗救治中心：病例收治，运营耗电（随等级放大）
    "storage": {},                      # 仓库：降低损耗
}
FACILITY_LEVEL_SCALE = 1.6  # 升级产出按比例放大
FACILITY_COST = {  # 建造/升级消耗 builder cost
    1: {FOOD: 20, WATER: 10, POWER: 15},
    2: {FOOD: 35, WATER: 18, POWER: 28},
    3: {FOOD: 60, WATER: 30, POWER: 45},
}

# 岗位
JOB_EFFICIENCY = {"engineer": 1.25, "farmer": 1.3, "general": 1.0, "medic": 1.0}

# 危机事件概率
CRISIS_DAY_CHANCE = 0.45

# 探索队系统
EXPEDITION_SUPPLY_PER_DAY = {FOOD: 1.0, WATER: 1.0}  # 每人每日消耗自带物资
EXPEDITION_MAX_DAYS = 7       # 最长探索天数，期满强制返程
EXPEDITION_ENCOUNTER_CHANCE = 0.85  # 每日行军遭遇概率
EXPEDITION_MAX_MEMBERS = 4    # 每支探索队上限

# 贸易救援系统
TRADE_REVIEWING, TRADE_TRANSPORTING = "reviewing", "transporting"
TRADE_DELIVERED, TRADE_FAILED = "delivered", "failed"
TRADE_REJECTED, TRADE_CANCELLED = "rejected", "cancelled"
TRADE_TYPES = ("rescue", "procure")
TRADE_INITIAL_REPUTATION = 50      # 新档案初始信誉
TRADE_MAX_ESCORTS = 3              # 每笔订单押运队上限
TRADE_INCIDENT_CHANCE = 0.55       # 每个在途日触发途中事件的概率
TRADE_REP_MIN, TRADE_REP_MAX = 0, 100

# 地堡联盟援助协议系统：管理者（玩家）发起、医护负责人会签、外部聚落签约，
# 托管医援物资、组建押运队，经对方签约审核 → 押运运输（途中事件）→
# 抵达疫区的押运审核（检疫关卡）→ 交付，回写信誉、资源与地堡医疗危机。
AID_PROPOSED, AID_ESCORTING = "proposed", "escorting"
AID_DELIVERED, AID_FAILED = "delivered", "failed"
AID_REJECTED, AID_LAPSED, AID_CANCELLED = "rejected", "lapsed", "cancelled"
AID_MAX_ESCORTS = 3                # 每支援助押运队上限
AID_INCIDENT_CHANCE = 0.55         # 每个在途日触发途中事件的概率
# 外部聚落签约审核通过率：0.50-0.90 随信誉，医护负责人资质再加成
AID_SIGN_BASE = 0.50
AID_SIGN_REP_DIV = 250.0
AID_SIGN_MEDIC_BONUS = 0.10        # 会签医护在岗（存活且仍任医护）的加成
# 抵达疫区后的押运审核（检疫关卡）：0.55-0.95 随信誉，押运队编入医护再加成
AID_GATE_BASE = 0.55
AID_GATE_REP_DIV = 250.0
AID_GATE_MEDIC_BONUS = 0.15
# 医疗危机表：0-100，随在堡活跃病例累积，联盟援助成功大幅缓解，失败则激化
MEDICAL_CRISIS_MIN, MEDICAL_CRISIS_MAX = 0, 100
MEDICAL_CRISIS_HIGH = 70                       # 高压阈值：越过则在堡全员士气受挫
MEDICAL_CRISIS_PER_CARE = 2                    # 每张在治床位每日压力
MEDICAL_CRISIS_PER_WAITING = 4                 # 每名登记未收治病例每日压力
MEDICAL_CRISIS_RELIEF = 35                     # 援助成功交付：危机缓解
MEDICAL_CRISIS_GATE_FAIL = 10                  # 押运审核未过：危机小升（疫区未获援）
MEDICAL_CRISIS_OUTBREAK = 25                   # 交付失败：危机激化
MEDICAL_CRISIS_HIGH_MORALE_HIT = 3.0           # 高压下在堡全员每日士气惩罚
AID_RELIEF_HEAL = 18.0                         # 援助成功对每名幸存者的基础治疗
AID_OUTBREAK_DAMAGE = 10.0                     # 援助失败激化疫情时的在堡健康损伤
AID_REP_DELIVER = 8                            # 成功交付信誉提升
AID_REP_REJECT = -3                            # 签约审核被拒信誉小挫
AID_REP_GATE_FAIL = -6                         # 押运审核未过信誉扣减
AID_REP_FAIL = -10                             # 交付失败（弃货/全损/失联）重罚

# 医疗救治中心系统：病例状态链 登记(registered) → 治疗(treating)/隔离(isolated)
# → 康复(recovered)/病亡(deceased)。终态病例保留在档案中作为救治履历与健康结算依据。
MED_REGISTERED, MED_TREATING, MED_ISOLATED = "registered", "treating", "isolated"
MED_RECOVERED, MED_DECEASED = "recovered", "deceased"
MED_CARE_STATUSES = (MED_TREATING, MED_ISOLATED)
MED_ACTIVE_STATUSES = (MED_REGISTERED, MED_TREATING, MED_ISOLATED)
MED_MANUAL_REGISTER_HEALTH = 70.0   # 手动登记的健康上限：高于此值视为健康无需登记
MED_AUTO_REGISTER_HEALTH = 35.0     # 每日巡检/事件后自动登记病例的健康线
MED_RECOVER_HEALTH = 75.0           # 康复出院线：在治病例每日结算后跨过此线即康复
MED_HEAL = {MED_TREATING: 6.0, MED_ISOLATED: 4.0}          # 每日基础救治回复
MED_MEDIC_HEAL = {MED_TREATING: 1.0, MED_ISOLATED: 0.8}   # 每名当值医护的额外回复
MED_DECAY = {True: 4.0, False: 1.5}  # 登记未收治病例每日恶化：{传染: 4, 普通: 1.5}
# 每张在治病床的每日资源消耗（治疗/隔离两档）
MED_CARE_COST = {
    MED_TREATING: {FOOD: 0.6, WATER: 0.5, POWER: 0.8},
    MED_ISOLATED: {FOOD: 0.8, WATER: 0.7, POWER: 1.2},
}
MED_BEDS_PER_LEVEL = 2             # 救治中心每级提供的床位（另 + 当值医护人数）
MED_SPREAD_CHANCE = 0.35           # 未隔离传染病例每日传播基础概率
MED_SPREAD_MEDIC_REDUCE = 0.05     # 每名当值医护降低的传播概率
MED_SPREAD_MIN_CHANCE = 0.10       # 传播概率下限（医护再多也不归零）
MED_SPREAD_DAMAGE = 8.0            # 传播对被感染者造成的即时健康损伤
# 各类抉择事件中具备传染性的事件 key：受袭者健康跌到登记线以下即立为传染病例
MED_INFECTIOUS_CRISIS_EVENTS = {"sick"}
MED_INFECTIOUS_ENCOUNTER_EVENTS = {"weather"}
MED_INFECTIOUS_INCIDENT_EVENTS = {"duststorm"}
# 联盟援助途中事件中具传染性的事件（检疫暴露）：受袭者健康跌破登记线即立为传染病例
MED_INFECTIOUS_AID_EVENTS = {"quarantine"}


# 跨聚落难民安置系统：外部聚落提交安置申请，地堡审核后进入隔离检疫，
# 检疫期满经"接纳 + 岗位分配"转为正式居民（联动医疗病例/资源消耗/信誉结算）。
# 安置不派离堡队伍：难民在堡内隔离舱中消耗地堡物资，不与探索队/贸易/援助互斥。
REFUGEE_QUARANTINE, REFUGEE_ADMITTING = "quarantine", "admitting"
REFUGEE_REJECTED, REFUGEE_REPATRIATED = "rejected", "repatriated"
REFUGEE_FAILED = "failed"          # 检疫期满无存活者：安置失败
REFUGEE_MAX_APPLICANTS = 3         # 每份申请难民人数上限
REFUGEE_QUARANTINE_DAYS = 2        # 隔离检疫天数
REFUGEE_DAILY_COST = {FOOD: 0.8, WATER: 0.7, POWER: 0.5}  # 每人每日检疫消耗
REFUGEE_SHORTAGE_HEALTH = 6.0      # 物资不足配给：每名难民每日健康损失
REFUGEE_SHORTAGE_MORALE = 5.0      # 物资不足配给：每名难民每日士气损失
REFUGEE_EXPOSE_CHANCE = 0.25       # 接触史（exposed）难民每日确诊基础概率
REFUGEE_EXPOSE_MEDIC_REDUCE = 0.05 # 每名当值医护降低的确诊概率
REFUGEE_EXPOSE_MIN_CHANCE = 0.10   # 确诊概率下限（医护再多也不归零）
REFUGEE_EXPOSE_DAMAGE = 12.0       # 确诊时的即时健康损伤
REFUGEE_INFECT_HEAL = 4.0          # 已确诊难民在有救治体系时每日基础救治
REFUGEE_INFECT_MEDIC_HEAL = 0.8    # 每名当值医护对确诊难民的额外救治
REFUGEE_INFECT_NOCLINIC = 3.0      # 无救治体系时确诊难民每日额外恶化
REFUGEE_WOUNDED_HEAL = 3.0         # 体弱（健康低于巡检登记线）未确诊难民每日回复
REFUGEE_NEGATIVE_HEALTH = 75.0     # 确诊难民健康跨过此线即解除确诊（检疫康复）
REFUGEE_MORALE_DRIFT = 1.0         # 检疫期士气每日向基准 60 自然恢复
REFUGEE_REP_REJECT = -2            # 拒绝外部聚落安置申请：信誉小挫
REFUGEE_REP_ADMIT = 4              # 每名成功接纳并分配岗位的难民：信誉提升
REFUGEE_REP_REPATRIATE = -5        # 检疫期满后遣返全部难民：人道代价
REFUGEE_REP_DEATH = -2             # 检疫期每名病亡难民的信誉代价
REFUGEE_ADMIT_MORALE = 5.0         # 接纳难民：在堡全员（含新居民）士气提升
REFUGEE_REPATRIATE_MORALE = -3.0   # 遣返难民：在堡全员士气受挫
REFUGEE_JOIN_HEALTH = 70.0         # 危机事件直接加入的幸存者健康（既有口径）

# 结局贡献评分：在"幸存者 × 天数 × 士气系数"的生存基础分之外，
# 医疗救治、难民安置、贸易援助三类外部贡献按终局成败计入总分。
# 只奖励/惩罚真正收敛的终局（交付成功/确认失败）：撤约、撤单、审核被拒、
# 逾期关闭等未形成援助结果的动作一律不计分，且每条终局订单/协议只结算一次
# （交付/失败路径自带幂等凭据，统计在同一次收敛内完成，回放不重复计数）。
SCORE_MED_PER_RECOVERED = 20       # 每名康复病例
SCORE_MED_PER_CARE_DAY = 2         # 每个累计救治床日
SCORE_MED_PER_DECEASED = -10       # 每名病亡病例（救治失败的代价）
SCORE_REFUGEE_PER_ADMITTED = 25    # 每名成功接纳的难民
SCORE_REFUGEE_PER_DEAD = -12       # 每名检疫病亡难民
SCORE_REFUGEE_PER_REJECTED = -4    # 每名被拒绝的难民
SCORE_REFUGEE_PER_REPATRIATED = -8  # 每名被遣返的难民
SCORE_TRADE_PER_DELIVERED = 40     # 每笔成功交付的贸易/救援订单
SCORE_TRADE_PER_FAILED = -15       # 每笔确认失败的贸易/救援订单
SCORE_AID_PER_DELIVERED = 50       # 每份成功交付的联盟援助协议
SCORE_AID_PER_FAILED = -20         # 每份确认失败的联盟援助协议


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _rng():
    """简单投影式随机数，便于测试时可注入 seed。"""
    import random
    return random.Random()


class BunkerEngineError(Exception):
    pass


class BunkerEngineConflict(BunkerEngineError):
    """并发冲突（乐观锁版本不匹配），HTTP 层映射为 409。"""


# 档案状态机阶段：
#   daily  —— 每日阶段，可建造/升级/调岗，可推进一天
#   crisis —— 危机阶段，存在待处理危机，除结算危机外拒绝一切推进与经营动作
#   ended  —— 终局（win/over），拒绝任何状态变更
PHASE_DAILY, PHASE_CRISIS, PHASE_ENDED = "daily", "crisis", "ended"
# 探索阶段：探索队在外且存在待处理遭遇，状态机拒绝一切经营/推进动作
PHASE_EXPEDITION = "expedition"
# 贸易阶段：押运队在途且存在待处理途中事件，状态机拒绝一切经营/推进动作
PHASE_TRADE = "trade"
# 联盟援助阶段：援助押运队在途且存在待处理途中事件（与贸易同口径互斥）
PHASE_AID = "aid"

# 待决事件种类：地堡危机 / 探索遭遇 / 押运途中事件 / 联盟援助途中事件。
# 四类挂起抉择共用同一套"统一待决事件管线"（见 _PENDING_SLOTS 与引擎内
# "待决事件统一管线"一节）：同构快照落库恢复、同机幂等凭据回放、
# 同一互斥状态机与终局收敛，差异仅在宿主与效果结算。
PENDING_CRISIS, PENDING_ENCOUNTER, PENDING_INCIDENT = "crisis", "encounter", "incident"
PENDING_AID_INCIDENT = "aid_incident"
PENDING_KINDS = (PENDING_CRISIS, PENDING_ENCOUNTER, PENDING_INCIDENT, PENDING_AID_INCIDENT)


class BunkerEngine:
    def __init__(self, db: Session, session: GameSession, rand=None):
        self.db = db
        self.session = session
        self.rand = rand or _rng()

    # ---- 状态机 ----
    @property
    def phase(self):
        if self.session.status != "running":
            return PHASE_ENDED
        kind, _ = self.current_pending_event()
        return self._slot(kind)["phase"] if kind else PHASE_DAILY

    def _require_phase(self, phase, message):
        if self.phase != phase:
            raise BunkerEngineError(message)

    # ---- 待决事件统一管线 ----
    # 四类挂起抉择（危机/遭遇/贸易途中事件/援助途中事件）共用同一生命周期：
    #   触发 → 同构快照落库（刷新/重开后恢复同一场抉择）→ 结算 → 写档案级
    #   幂等凭据（连点/并发落败安全回放）→ 清除快照（互斥：同一时刻至多一个
    #   待决事件）→ 终局一律清空（收敛到 ended，不留悬而未决的抉择）。
    # 槽位差异（宿主列/事件池/凭据列/阶段）在模块底部 _PENDING_SLOTS 声明。

    def _slot(self, kind):
        return _PENDING_SLOTS[kind]

    def _pending_host(self, kind):
        """待决事件的宿主快照：危机宿主即档案本身（返回 None 表示不嵌套）。"""
        attr = self._slot(kind)["host_attr"]
        return getattr(self.session, attr) if attr else None

    def _host_active(self, kind):
        """宿主是否处于可挂起待决事件的状态（队伍在外/订单在途）。"""
        slot = self._slot(kind)
        if slot["host_attr"] is None:
            return True
        host = self._pending_host(kind)
        return bool(host) and host.get("status") == slot["host_status"]

    def _get_pending(self, kind):
        """读取某类待决事件快照（持久化位置由槽位声明，恢复口径四类一致）。"""
        slot = self._slot(kind)
        if slot["host_attr"] is None:
            return getattr(self.session, slot["field"])
        host = self._pending_host(kind)
        return host.get(slot["field"]) if host else None

    def _write_pending(self, kind, value):
        """写入/清除待决快照；嵌套快照整体回写宿主，确保 JSON 列变更被追踪落库。"""
        slot = self._slot(kind)
        if slot["host_attr"] is None:
            setattr(self.session, slot["field"], value)
            return
        host = dict(self._pending_host(kind) or {})
        host[slot["field"]] = value
        setattr(self.session, slot["host_attr"], host)

    def current_pending_event(self):
        """当前挂起的待决事件 (kind, snapshot)；无则 (None, None)。

        事件互斥由状态机保证：同一时刻全档案至多一个待决事件，
        按危机 → 遭遇 → 途中事件的固定优先级检出。
        """
        for kind in PENDING_KINDS:
            if not self._host_active(kind):
                continue
            snap = self._get_pending(kind)
            if snap:
                return kind, snap
        return None, None

    def _clear_all_pending(self):
        """终局收敛：清除全部待决快照与离堡任务宿主，不留悬而未决的抉择。"""
        for kind in PENDING_KINDS:
            slot = self._slot(kind)
            if slot["host_attr"] is None:
                self._write_pending(kind, None)
            else:
                setattr(self.session, slot["host_attr"], None)

    # ---- 资源查询 ----
    def get_resources(self):
        return self.session.resources or {k: 0 for k in RESOURCE_KEYS}

    def _set_resource(self, key, val):
        # 复制后整体回写，确保 JSON 列的变更被 SQLAlchemy 追踪并落库
        res = dict(self.session.resources or {k: 0 for k in RESOURCE_KEYS})
        res[key] = round(max(0.0, val), 1)
        self.session.resources = res

    def _add_resource(self, key, delta):
        res = self.session.resources or {k: 0 for k in RESOURCE_KEYS}
        cur = res.get(key, 0.0)
        nxt = max(0.0, cur + delta)
        new_res = dict(res)
        new_res[key] = round(nxt, 1)
        self.session.resources = new_res
        return nxt

    # ---- 设施 ----
    def facility_output(self, facility: Facility):
        base = FACILITY_OUTPUT.get(facility.category, {})
        mult = FACILITY_LEVEL_SCALE ** (facility.level - 1)
        out = {k: v * mult for k, v in base.items()}
        # 农夫/工程师提升产出设施
        if facility.category in ("farm", "oxygen") and self.job_count("farmer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("farmer")
        if facility.category == "power" and self.job_count("engineer") > 0:
            for k in list(out):
                if out[k] > 0:
                    out[k] *= 1 + 0.05 * self.job_count("engineer")
        return out

    def job_count(self, job):
        away = self._away_resident_ids()
        caring = self._caring_resident_ids()
        # 在治病床上的治疗/隔离病例暂停生产：既不算在堡劳动力，也不再产出岗位加成
        return sum(
            1 for r in self.session.residents
            if r.alive and r.job == job and r.id not in away and r.id not in caring
        )

    def active_facilities(self):
        return [f for f in self.session.facilities if f.status == "active"]

    # ---- 离堡成员追踪（探索队 + 贸易押运队 + 联盟援助押运队）----
    def _away_resident_ids(self):
        """当前离堡居民编号（无论生死）：探索队编制 + 在途贸易/援助押运队。

        订单处于 reviewing/proposed（审核中）时押运队尚未出发，仍在堡内正常
        生产/消耗；仅 transporting/escorting（在途）才按离堡口径结算。
        """
        ids = set()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            ids.update(exp.get("members", []))
        order = self.session.trade_order
        if order and order.get("status") == TRADE_TRANSPORTING:
            ids.update(order.get("escorts", []))
        pact = self.session.aid_pact
        if pact and pact.get("status") == AID_ESCORTING:
            ids.update(pact.get("escorts", []))
        return ids

    def _away_residents(self):
        """探索队编制内的全部居民（含已阵亡，用于返程结算）。"""
        ids = set()
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            ids.update(exp.get("members", []))
        return [r for r in self.session.residents if r.id in ids]

    def _trade_escorts(self, order=None):
        """当前贸易押运队的全部居民（含已阵亡，用于交付/回退结算）。"""
        order = order or self.session.trade_order
        if not order:
            return []
        ids = set(order.get("escorts", []))
        return [r for r in self.session.residents if r.id in ids]

    def _aid_escorts(self, pact=None):
        """当前联盟援助押运队的全部居民（含已阵亡，用于交付/回退结算）。"""
        pact = pact or self.session.aid_pact
        if not pact:
            return []
        ids = set(pact.get("escorts", []))
        return [r for r in self.session.residents if r.id in ids]

    def _in_bunker_residents(self):
        """地堡内存活居民（排除探索队成员）。"""
        away = self._away_resident_ids()
        return [r for r in self.session.residents if r.alive and r.id not in away]

    def _in_bunker_count(self):
        return len(self._in_bunker_residents())

    # ---- 每日推进 ----
    def advance_day(self):
        # 终局或存在待处理抉择（危机/探索遭遇）时都不能推进：抉择不可被"再点一天"跳过
        self._require_phase(PHASE_DAILY, "存在待处理抉择，必须先完成才能推进")
        self.session.day += 1
        # 在任何产出/探索队结算之前快照当日终局裁决：抵达目标日立即胜利；
        # 若推进前已全线枯竭，随后的当日产出或探索队带回的战利品/余粮
        # 都不得把败局“救回”——终局在当天只收敛一次
        pre_verdict = self._end_verdict()
        self._apply_production_and_consumption()
        self._apply_health_morale()
        # 医疗救治中心每日健康结算：病例治疗/隔离消耗、传染扩散、康复病亡收敛。
        # 离堡成员的病例随队伍冻结，只结算在堡病例，与产出/口粮同一口径
        self._apply_medical_tick()
        # 跨聚落难民安置每日检疫结算：隔离物资配给、接触史确诊、确诊救治/病亡、
        # 检疫期满转入待接纳。难民尚未计入居民表，独立消耗隔离舱配给
        self._apply_refugee_tick(pre_verdict=pre_verdict)
        exp = self.session.expedition
        if exp and exp.get("status") == "away":
            # 探索队在外出差：地堡按在堡人口结算，探索队消耗自带物资、行军并触发遭遇
            self._apply_expedition_travel(exp, pre_verdict=pre_verdict)
            # 强制返程（补给耗尽/期满/全员失联）会清除探索队状态：
            # 此时不得再用旧 exp 触发遭遇，否则会把已结算的队伍恢复成"在外"
            if self.session.expedition is None:
                self._check_end(forced_verdict=pre_verdict)
                return None
            # 终局优先：抵达目标日胜利，或地堡因在堡匮乏/人口归零失败时，
            # 在外队伍先安全返程（战利品入库、剩余物资归还、幸存者归队），
            # 再统一收敛到 ended——绝不在 ended 档案上留下无法处理的"僵尸队伍"
            if pre_verdict is not None or self._end_conditions_met():
                self._settle_expedition(
                    self.session.expedition, reason="终局已至，探索队返程"
                )
                self._check_end(forced_verdict=pre_verdict)
                return None
            # 探索队行军中：触发遭遇（替代地堡危机），遭遇挂起后进入 expedition 阶段
            return self._maybe_trigger_expedition_encounter(self.session.expedition)
        order = self.session.trade_order
        if order and order.get("status") in (TRADE_REVIEWING, TRADE_TRANSPORTING):
            # 贸易订单推进：审核（reviewing→transporting/rejected）或在途运输
            # （travel_days 累加 → 途中事件 / 抵达交付 / 失败回退）。
            # 与探索队同一口径：终局裁决先快照，成功入库的回礼也不得复活败局
            incident = self._progress_trade_order(order, pre_verdict=pre_verdict)
            if self.session.trade_order is None:
                # 订单已收敛（驳回/交付/失败回退）
                self._check_end(forced_verdict=pre_verdict)
                return None
            if incident is not None:
                # 途中事件挂起：进入 trade 阶段，替代当日地堡危机
                return incident
            if pre_verdict is not None or self._end_conditions_met():
                # 在途订单遇终局：强制安全交付（回礼/退款先入库、押运队归队），
                # 再统一收敛到 ended，不留"僵尸订单"
                self._deliver_trade_order(
                    self.session.trade_order, reason="终局已至，押运队返程",
                    forced_verdict=pre_verdict, force_success=True,
                )
                self._check_end(forced_verdict=pre_verdict)
                return None
        pact = self.session.aid_pact
        if pact and pact.get("status") in (AID_PROPOSED, AID_ESCORTING):
            # 联盟援助协议推进：外部聚落签约（proposed→escorting/rejected）
            # 或在途押运（travel_days 累加 → 途中事件 / 抵达押运审核 / 交付）。
            # 与贸易订单同一口径：终局裁决先快照，成功入库的医援也不得复活败局
            incident = self._progress_aid_pact(pact, pre_verdict=pre_verdict)
            if self.session.aid_pact is None:
                # 协议已收敛（拒签/逾期/押运审核未过/交付/失败回退）
                self._check_end(forced_verdict=pre_verdict)
                return None
            if incident is not None:
                # 援助途中事件挂起：进入 aid 阶段，替代当日地堡危机
                return incident
            if pre_verdict is not None or self._end_conditions_met():
                # 在途援助协议遇终局：强制安全交付（医援入库、押运队归队），
                # 再统一收敛到 ended，不留"僵尸协议"
                self._deliver_aid_pact(
                    self.session.aid_pact, reason="终局已至，援助押运队返程",
                    forced_verdict=pre_verdict, force_success=True,
                )
                self._check_end(forced_verdict=pre_verdict)
                return None
        # 终局优先：抵达目标日或全面崩溃直接结算结局，不再凭空挂起一个
        # 永远无法处理的危机（统一每日推进 → 危机处理 → 终局的流转）
        if self._check_end(forced_verdict=pre_verdict):
            return None
        return self._maybe_trigger_crisis()

    def _end_conditions_met(self):
        """只判定终局条件、不写终局状态（用于终局前的探索队返程收敛）。"""
        return self._end_verdict() is not None

    def _end_verdict(self):
        """当前状态对应的终局裁决：返回 None（未终局）或 (win, reason)。

        纯判定、不写状态。返程结算在战利品/余粮入库前先快照一次裁决，
        保证“全线枯竭”的败局不会被随后入库的战利品抬过阈值而“复活”，
        终局状态只收敛一次且与结算路径（主动返程/强制返程/遭遇收敛）无关。
        """
        if self.session.day >= self.session.target_day:
            return True, f"坚持到第{self.session.day}天，末日阴影散去，幸存者们走向了新生。"
        if self.session.survivors <= 0:
            return False, "所有幸存者都已逝去，地堡陷入永恒的寂静。"
        res = self.get_resources()
        if all(res.get(k, 0) <= 1 for k in RESOURCE_KEYS):
            return False, "食物、水源、电力和氧气全线枯竭，地堡无法再维系生命。"
        return None

    def _apply_production_and_consumption(self):
        # 离堡人员不消耗地堡物资（吃自带口粮），地堡消耗只计在堡人口
        pop = self._in_bunker_count()
        # 士气系数(在堡人员平均士气)：低士气降低产出；探索队在外不参与地堡生产
        avg_morale = self.avg_morale(in_bunker_only=True)
        morale_factor = 0.6 + 0.4 * (avg_morale / 100.0)

        # 消耗
        consume = {}
        for k in RESOURCE_KEYS:
            consume[k] = BASE_CONSUME[k] * pop

        # 产出（累计设施净产）
        prod = {k: 0.0 for k in RESOURCE_KEYS}
        for f in self.active_facilities():
            for k, v in self.facility_output(f).items():
                prod[k] += v * morale_factor

        # 应用净变化（消耗优先，产出后）
        for k in RESOURCE_KEYS:
            net = prod.get(k, 0.0) - consume[k]
            self._add_resource(k, net)

        # 日志
        self._log(
            "update",
            f"第{self.session.day}天 · 生存更新",
            f"人口{pop}，食物净变{round(consume[FOOD]-prod[FOOD],1):+}、水{round(consume[WATER]-prod[WATER],1):+}、电力{round(consume[POWER]-prod[POWER],1):+}、氧气{round(consume[OXY]-prod[OXY],1):+}",
            decision="例行更新",
        )

    def _apply_health_morale(self):
        res = self.get_resources()
        away = self._away_resident_ids()
        # 在治病例（治疗/隔离中）的健康由医疗救治中心每日结算统一处理，
        # 这里跳过医疗舱被动回复，避免一份伤势回两次血
        caring = self._caring_resident_ids()
        # 资源不足影响（仅作用于在堡居民；探索队吃自带物资，不受地堡短缺波及）
        for r in self.session.residents:
            if not r.alive:
                continue
            if r.id in away:
                continue
            morale = r.morale
            # 资源不足影响
            for k, name in ((FOOD, "食物"), (WATER, "水源"), (OXY, "氧气"), (POWER, "电力")):
                if res.get(k, 0) <= 15:
                    morale -= 2.0
            # 医疗站回复 + 保持士气（在治病例的健康改由医疗结算处理）
            if self.has_category("med") and r.id not in caring:
                if r.health < 100:
                    r.health = _clamp(r.health + 1.2)
            # 医疗危机高压：伤病满舱时在堡全员士气持续受挫（离堡人员不受波及）
            if self.medical_crisis() >= MEDICAL_CRISIS_HIGH:
                morale -= MEDICAL_CRISIS_HIGH_MORALE_HIT
            # 低健康拖累士气
            if r.health < 30:
                morale -= 3.0
            # 士气自然衰减/恢复向基准 75
            if morale < 75:
                morale += 0.5
            elif morale > 80:
                morale -= 0.3
            r.morale = _clamp(morale)
        # 去除最严重短缺导致的死亡
        self._apply_starvation_deaths()

    def has_category(self, cat):
        return any(f.category == cat and f.status == "active" for f in self.session.facilities)

    def _apply_starvation_deaths(self):
        res = self.get_resources()
        critical = [k for k in RESOURCE_KEYS if res.get(k, 0) <= 0]
        if not critical:
            return
        # 每日最多因匮乏死 1 人，依次从在堡最弱居民开始（探索队不在堡内，不参与地堡匮乏判定）
        alive = self._in_bunker_residents()
        if not alive:
            return
        weakest = min(alive, key=lambda r: r.health)
        weakest.alive = 0
        weakest.health = 0
        self.session.survivors -= 1
        self._log("crisis", "生存危机：资源耗尽", f"{weakest.name} 因匮乏失去生命。", decision="自然事件")

    def avg_morale(self, in_bunker_only=False):
        """平均士气。

        in_bunker_only=True（地堡设施产出加成）只统计在堡存活居民：
        探索队在外时其士气不参与地堡生产结算；终局评分等全局口径仍统计全体存活者。
        """
        if in_bunker_only:
            alive = self._in_bunker_residents()
        else:
            alive = [r for r in self.session.residents if r.alive]
        if not alive:
            return 0.0
        return sum(r.morale for r in alive) / len(alive)

    def _log(self, etype, title, detail, decision=None):
        self.db.add(
            EventLog(
                session_id=self.session.id,
                day=self.session.day,
                event_type=etype,
                title=title,
                detail=detail,
                decision=decision,
            )
        )

    # ---- 危机轮盘 ----

    @staticmethod
    def _effect_scope(effect):
        """健康/士气效果的作用域：'single' 仅目标本人，'all' 全体存活者。

        数字简写默认为全体；单体效果须显式声明
        {"value": -5, "target": "single"}。
        """
        if isinstance(effect, dict):
            return effect.get("target", "all")
        return "all"

    @staticmethod
    def _effect_value(effect):
        return effect["value"] if isinstance(effect, dict) else effect

    def _maybe_trigger_crisis(self):
        if self.rand.random() > CRISIS_DAY_CHANCE:
            return None
        event = self.rand.choice(CRISIS_POOL)
        crisis = self._build_crisis(event)
        # 待处理危机整体写入存档：事件、目标、选项与一次性 token 一起绑定，
        # 刷新页面后凭档案即可恢复同一个决策
        self._write_pending(PENDING_CRISIS, crisis)
        return crisis

    def _build_pending_event(self, event, target_pool):
        """构造待决事件快照：四类抉择（危机/遭遇/贸易途中事件/援助途中事件）共用同一结构。

        快照携带一次性 token、事件 key、发生日与绑定目标，随档案整体落库；
        刷新/重开后凭快照恢复同一场抉择，重复/并发提交凭 token 识别。
        仅当事件存在单体效果的决策时才从候选池抽取目标（危机=在堡居民，
        遭遇/途中事件=队内成员），全体事件不产生目标，前端也无从回传。
        """
        needs_target = any(self._choice_targeted(c) for c in event["choices"])
        target = self.rand.choice(target_pool) if needs_target and target_pool else None
        return {
            "token": uuid.uuid4().hex,  # 本次待决事件的一次性凭据
            "event": event["key"],
            "day": self.session.day,
            "title": event["title"],
            "desc": event["desc"],
            "needs_target": needs_target,
            "target_id": target.id if target else None,
            "target_name": target.name if target else None,
            "choices": [
                {
                    "key": c["key"],
                    "label": c["label"],
                    "hint": c.get("hint", ""),
                    "targeted": self._choice_targeted(c),
                }
                for c in event["choices"]
            ],
        }

    def _build_crisis(self, event):
        # 目标只从"在堡存活居民"中抽取：探索队/押运队外出期间不受地堡危机波及
        return self._build_pending_event(event, self._in_bunker_residents())

    @classmethod
    def _choice_targeted(cls, choice):
        """该决策是否含只作用于目标本人的健康/士气效果。"""
        effects = choice.get("effects", {})
        return any(
            cls._effect_scope(effects[stat]) == "single"
            for stat in ("health", "morale")
            if stat in effects
        )

    def _ensure_running(self):
        """结算边界：游戏结束后拒绝一切状态变更。"""
        if self.session.status != "running":
            raise BunkerEngineError("游戏已结束，无法执行该操作")

    def _require_daily_phase(self, action):
        """经营/推进类动作只允许在每日阶段执行：任一待决事件挂起即锁定。"""
        self._ensure_running()
        kind, _ = self.current_pending_event()
        if kind:
            raise BunkerEngineError(
                f"存在待处理{self._slot(kind)['zh']}，必须先完成抉择才能{action}"
            )

    def _require_no_mission(self, action):
        """离堡任务互斥：同一时间只允许一支在外探索队/一笔在谈贸易订单/一份在谈援助协议。"""
        if self.session.expedition:
            raise BunkerEngineError(f"已有探索队在外，无法同时{action}")
        if self.session.trade_order:
            raise BunkerEngineError(f"已有在谈/在途贸易订单，无法同时{action}")
        if self.session.aid_pact:
            raise BunkerEngineError(f"已有在谈/在途联盟援助协议，无法同时{action}")

    def _pending_event_def(self, kind, pending):
        """取出待决快照对应的事件定义；存档损坏（事件池中不存在）时视为无法结算。"""
        event = next(
            (e for e in self._slot(kind)["pool"] if e["key"] == pending.get("event")),
            None,
        )
        if event is None:
            raise BunkerEngineError(
                f"待处理{self._slot(kind)['zh']}已失效，请刷新档案后重试"
            )
        return event

    @staticmethod
    def _event_choice(event, choice_key):
        """在事件定义中定位所选决策；未知选项一律拒绝。"""
        choice = next((c for c in event["choices"] if c["key"] == choice_key), None)
        if not choice:
            raise BunkerEngineError("未知决策选项")
        return choice

    def _locate_pending_decision(self, kind, token):
        """定位当前待决事件：结算必须命中档案里唯一的待决快照。

        适用于宿主嵌套类（遭遇/途中事件；危机宿主即档案，走 resolve_crisis
        自身的定位流程）。返回 (host, pending)。宿主缺失/已前进或快照已被
        处理时：携带凭据的请求按 409（状态已变化）拒绝，无凭据请求按 400；
        快照 token 不符（过期/串档）一律 409。不能凭空伪造，也不能在状态
        前进后重复结算。
        """
        slot = self._slot(kind)
        host = self._pending_host(kind)
        if host is None or not self._host_active(kind):
            if token:
                raise BunkerEngineConflict(f"{slot['host_zh']}状态已变化，请刷新后重试")
            raise BunkerEngineError(f"当前没有{slot['host_absent_zh']}")
        pending = host.get(slot["field"])
        if not pending:
            if token:
                raise BunkerEngineConflict(f"该{slot['zh']}已被处理，请刷新后重试")
            raise BunkerEngineError(f"当前没有待处理的{slot['zh']}")
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict(f"该{slot['zh']}决策已过期，请刷新后重试")
        return host, pending

    def _bound_target(self, pending, choice, pool, member_zh):
        """单体效果目标校验（在施加任何效果前完成，失败不留部分变更）。

        目标必须与待决快照绑定的目标一致，且在候选池（队内成员）内存活；
        全体/资源类决策返回 None，效果按整个候选池结算。
        """
        if not self._choice_targeted(choice):
            return None
        bound_id = pending.get("target_id")
        if bound_id is None:
            raise BunkerEngineError(f"该决策需要指定一名{member_zh}作为目标")
        target = next((r for r in pool if r.id == bound_id), None)
        if not target or not target.alive:
            raise BunkerEngineError(f"目标{member_zh}不在队中或已故，无法作为效果目标")
        return target

    def _apply_stat_effects(self, effects, target, pool, pool_zh, detail_parts):
        """健康/士气效果：single 只作用于目标本人，all 作用于整个候选池。"""
        for stat, zh in (("health", "健康"), ("morale", "士气")):
            if stat not in effects:
                continue
            spec = effects[stat]
            val = self._effect_value(spec)
            if self._effect_scope(spec) == "single":
                targets, scope = [target], f"仅{target.name}"
            else:
                targets, scope = pool, pool_zh
            for r in targets:
                setattr(r, stat, _clamp(getattr(r, stat) + val))
            detail_parts.append(f"{zh} {val:+.0f}（{scope}）")

    def _sweep_casualties(self, members, casualties):
        """统一收敛伤亡：健康归零即阵亡。单体/全体效果（以及之前已濒死、
        被本次效果带过零点的成员）在同一次扫描中处理，保证人口只扣一次、
        伤亡名单不重不漏。"""
        for r in members:
            if r.health <= 0 and r.alive:
                r.alive = 0
                r.health = 0
                if r.id not in casualties:
                    casualties.append(r.id)
                    self.session.survivors = max(0, self.session.survivors - 1)
        return casualties

    # ---- 幂等凭据（并发重放）----
    # 每类待决事件在档案上各有一列凭据（last_resolution / last_expedition /
    # last_trade，槽位声明），只保留最近一次结算。宿主随后被清除（返程/交付/
    # 撤单）或继续在外都不影响：凭据独立保存在档案上，并发落败/连点请求凭
    # token 命中即安全回放，绝不二次结算。
    @staticmethod
    def _credential_matches(rec, require=None, **optional):
        """通用幂等凭据匹配。

        require 中的键必须逐字相等（强标识：action/event/choice）；
        optional 中的键只要请求值与凭据值都非空就必须相等——凭据缺字段
        （旧存档）或请求未携带该凭据（旧客户端无 token）时不误判冲突。
        """
        if not rec:
            return False
        for key, expected in (require or {}).items():
            if rec.get(key) != expected:
                return False
        for key, expected in optional.items():
            if expected is None:
                continue
            actual = rec.get(key)
            if actual is not None and actual != expected:
                return False
        return True

    @classmethod
    def _matches_resolution(cls, rec, event_key, choice_key, target_id, day=None):
        """判断落败/重试请求是否就是上一次已完成的那次危机结算（幂等回放）。

        除事件/选项/目标外还核对危机发生日，避免不同天的同类型危机被误重放；
        day 为 None（调用方拿不到上下文）时退化为不校验天数。
        """
        if not cls._credential_matches(
            rec, require={"event": event_key, "choice": choice_key}, day=day
        ):
            return False
        return (rec.get("target_id") or None) == (target_id or None)

    def _read_credential(self, kind):
        return getattr(self.session, self._slot(kind)["credential_attr"])

    def _write_credential(self, kind, rec):
        setattr(self.session, self._slot(kind)["credential_attr"], rec)

    def _remember_credential(self, kind, detail, action=None, token=None,
                             host_token=None, choice=None, day=None, extra=None):
        """把已完成的抉择/动作写入档案级幂等凭据（每类一列，只留最近一次）。"""
        rec = {
            "token": token,
            "choice": choice,
            "day": day if day is not None else self.session.day,
            "detail": detail,
        }
        if action is not None:
            rec["action"] = action
        host_key = self._slot(kind)["host_token_key"]
        if host_key:
            rec[host_key] = host_token
        if extra:
            rec.update(extra)
        self._write_credential(kind, rec)

    def _mission_credential_replay(self, kind, action, token=None,
                                   host_token=None, choice=None):
        """命中档案级幂等凭据则返回 (detail, True)，否则返回 (None, False)。"""
        rec = self._read_credential(kind)
        optional = {"token": token, "choice": choice}
        host_key = self._slot(kind)["host_token_key"]
        if host_key:
            optional[host_key] = host_token
        if self._credential_matches(rec, require={"action": action}, **optional):
            return rec.get("detail", ""), True
        return None, False

    def _converged_settlement_replay(self, kind, settle_action, token, choice=None):
        """抉择直接触发宿主收敛（返程/失败回退）时，凭事件 token 回放那次结算。

        事件结算后若宿主当场收敛（补给耗尽/全员失联/弃货/终局），档案级凭据
        会被收敛记录覆盖，但该记录仍挂着本次事件的一次性 token：并发落败或
        连点凭 token 命中这里，回放收敛明细，绝不二次结算。
        """
        rec = self._read_credential(kind)
        if not token or not rec or rec.get("action") != settle_action:
            return None, False
        if rec.get("token") != token:
            return None, False
        if choice is not None and rec.get("choice") is not None and choice != rec["choice"]:
            return None, False
        return rec.get("detail", ""), True

    def _resolve_target(self, target_id, required):
        """统一解析目标居民。

        - required=True（所选决策含单体效果）：必须显式给出目标，且目标归属
          当前档案并存活；跨档案编号、不存在、已故或缺席一律报错。
        - required=False（全体/资源类决策）：忽略客户端传入的目标，返回 None，
          效果按全体结算，前端回传谁都不会把全体效果收窄成单体。
        """
        if not required:
            return None
        if target_id is None:
            raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
        target = next((r for r in self.session.residents if r.id == target_id), None)
        if target is None:
            raise BunkerEngineError("目标居民不存在或不属于当前档案")
        if not target.alive:
            raise BunkerEngineError("目标居民已故，无法作为效果目标")
        return target

    def resolve_crisis(self, event_key, choice_key, target_id=None, token=None):
        """结算待处理危机。

        结算必须命中档案里唯一的待处理危机：事件、选项、单体目标都与存档绑定，
        既不能凭空伪造一场危机（无待处理危机时拒绝），也不能重复结算
        （结算后待处理危机被清除并留下幂等凭据，重放只返回上次结果）。
        返回 (detail, replayed)：replayed=True 表示这是重复请求，未再次施加效果。
        """
        self._ensure_running()
        pending = self._get_pending(PENDING_CRISIS)
        # 存档损坏（事件池外的事件）时直接判失效，不进入任何结算/回放分支
        event = self._pending_event_def(PENDING_CRISIS, pending) if pending else None

        # 已有同一危机（事件/选项/目标/发生日一致）的结算记录：
        # 重复提交（含并发落败方）只回放，不二次结算
        pending_day = pending.get("day") if pending else None
        rec = self._read_credential(PENDING_CRISIS)
        if self._matches_resolution(rec, event_key, choice_key, target_id, day=pending_day):
            return rec.get("detail", ""), True

        if pending is None:
            raise BunkerEngineError("当前没有待处理的危机，无法结算")

        # 事件必须与存档中的待处理危机一致：不能用 A 事件的请求去结算 B
        if event_key != pending.get("event"):
            raise BunkerEngineError("危机事件与当前待处理事件不符")
        # token 用于区分“同一危机上一次的旧点击”与刷新后恢复的当前决策；
        # 旧客户端/旧档案没有 token 时退化为仅按事件匹配
        if token is not None and pending.get("token") and token != pending["token"]:
            raise BunkerEngineConflict("该危机决策已过期，请按当前危机重新选择")

        choice = self._event_choice(event, choice_key)
        effects = choice.get("effects", {})

        # 作用域由所选决策的效果声明决定，客户端传入的 target_id 不能改变它：
        # 单体效果必须携带有效目标，全体效果一律忽略客户端目标
        targeted = self._choice_targeted(choice)
        if targeted:
            # 目标与待处理危机绑定：不能用任意/其他居民编号替换事件目标
            bound_id = pending.get("target_id")
            if target_id is None:
                raise BunkerEngineError("该决策需要指定一名幸存者作为目标")
            if bound_id is not None and target_id != bound_id:
                raise BunkerEngineError("目标居民与本次危机指定的幸存者不符")
        # 在应用任何效果前完成目标校验，保证失败时档案状态不发生部分变更
        target = self._resolve_target(target_id, required=targeted)

        detail_parts = []

        # 资源效果
        for k, v in effects.get("resources", {}).items():
            self._add_resource(k, v)
            detail_parts.append(f"{RESOURCE_ZH.get(k,k)} {v:+.0f}")
        # 健康/士气效果：single 只作用于目标本人，all 作用于在堡全体存活者
        # （探索队/押运队外出期间不参与地堡危机结算，与每日短缺/生产口径一致）
        self._apply_stat_effects(effects, target, self._in_bunker_residents(), "全体", detail_parts)
        if "add_resident" in effects:
            self._add_resident(effects["add_resident"])
            detail_parts.append(f"加入新幸存者 {effects['add_resident']}")
        if "reputation" in effects:
            rep = self._add_reputation(effects["reputation"])
            detail_parts.append(f"信誉 {effects['reputation']:+d}（现 {rep}）")
        if effects.get("trap"):
            detail_parts.append("（不良后果）")

        # 危机后健康结算：受伤/染疫居民由医疗救治中心承接为病例（未建中心则忽略）
        if "health" in effects:
            spec = effects["health"]
            affected = [target] if self._effect_scope(spec) == "single" else self._in_bunker_residents()
            self._settle_health_aftermath(
                affected, infectious_event=event["key"] in MED_INFECTIOUS_CRISIS_EVENTS,
                reason=event["title"], force=True,
            )

        # 日志与实际结算同一作用域：单体写名，全体写明“全体幸存者”
        scope_zh = f"（目标：{target.name}）" if targeted else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", event["title"], f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])

        # 清除待处理危机并记下幂等凭据——无论后续是否终局，本危机都已结算
        self._write_pending(PENDING_CRISIS, None)
        self._remember_credential(
            PENDING_CRISIS, detail,
            token=pending.get("token"), choice=choice["key"], day=pending.get("day"),
            extra={"event": event["key"], "target_id": target.id if targeted else None},
        )
        self._check_end()
        return detail, False

    def reconcile_stale_resolution(self, event_key, choice_key, target_id, token=None):
        """并发落败（版本冲突）后核对：若对方提交的是同一次结算则安全回放。

        返回 (detail, replayed)；请求与任何已知结算都对不上时抛 409，
        由调用方提示“危机状态已变化”，杜绝并发重复结算。
        """
        rec = self._read_credential(PENDING_CRISIS)
        if self._matches_resolution(rec, event_key, choice_key, target_id) and (
            token is None or not rec.get("token") or token == rec.get("token")
        ):
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("危机状态已被其他请求更新，请刷新后重试")

    def _add_resident(self, name):
        r = Resident(
            session_id=self.session.id,
            name=name,
            job="general",
            health=70.0,
            morale=60.0,
            alive=1,
            joined_day=self.session.day,
        )
        # 挂到关系集合：residents 已加载时仅 add+flush 不会让新成员出现在内存集合
        self.session.residents.append(r)
        self.db.add(r)
        self.session.survivors += 1

    # ---- 探索队 ----
    def _random_survivor_name(self):
        import random
        surnames = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许")
        givens = list("伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英华玉萍红斌")
        return random.choice(surnames) + random.choice(givens)

    def send_expedition(self, member_ids, supplies):
        """派遣探索队：选择在堡居民并分配自带物资，队伍出发后暂停地堡生产。

        离堡人员不参与设施产出、不消耗地堡口粮；行军消耗自带物资，
        途中遭遇由玩家抉择，返程时统一结算战利品与伤亡。
        """
        self._ensure_running()
        if self.phase != PHASE_DAILY:
            raise BunkerEngineError("当前状态无法派遣探索队")
        self._require_no_mission("派遣探索队")
        if not member_ids:
            raise BunkerEngineError("必须选择至少一名居民参加探索队")
        if len(set(member_ids)) != len(member_ids):
            raise BunkerEngineError("同一名居民不能重复编入探索队")
        if len(member_ids) > EXPEDITION_MAX_MEMBERS:
            raise BunkerEngineError(f"探索队最多 {EXPEDITION_MAX_MEMBERS} 人")
        # 校验队员：必须是在堡存活居民
        members = []
        for mid in member_ids:
            r = next((x for x in self.session.residents if x.id == mid), None)
            if not r or not r.alive:
                raise BunkerEngineError("队员不存在或已故，无法参加探索队")
            if r.id in self._away_resident_ids():
                raise BunkerEngineError(f"{r.name} 已在探索队中")
            if self._has_active_case(r.id):
                raise BunkerEngineError(f"{r.name} 有未结病例，无法离堡参加探索队")
            members.append(r)
        # 校验并扣除自带物资
        supply_cost = {}
        for k, v in (supplies or {}).items():
            if k not in RESOURCE_KEYS:
                raise BunkerEngineError(f"未知物资 {k}")
            if v < 0:
                raise BunkerEngineError("物资数量不能为负")
            supply_cost[k] = float(v)
        if not self._can_afford(supply_cost):
            raise BunkerEngineError("物资不足，无法派遣")
        for k, v in supply_cost.items():
            self._add_resource(k, -v)
        # 写入探索队快照（含一次性 token，刷新后恢复同一支队伍）
        exp = {
            "token": uuid.uuid4().hex,
            "status": "away",
            "started_day": self.session.day,
            "members": [r.id for r in members],
            "supplies": dict(supply_cost),
            "travel_days": 0,
            "encounters_resolved": 0,
            "pending_encounter": None,
            "loot": {},
            "casualties": [],
        }
        self.session.expedition = dict(exp)
        names = "、".join(r.name for r in members)
        self._log("system", "探索队出发", f"{names} 携带物资外出探索。", decision="派遣探索队")
        return exp

    def _apply_expedition_travel(self, exp, pre_verdict=None):
        """探索队每日行军：消耗自带物资、累计天数，触发强制返程判定。

        pre_verdict 为当日推进开始时快照的终局裁决（如已全线枯竭），
        透传给返程结算，避免行军/入库把当日败局“救回”。
        """
        alive_members = [r for r in self._away_residents() if r.alive]
        if not alive_members:
            # 全员失联：强制返程（无人生还）
            self._settle_expedition(exp, reason="探索队全员失联", pre_verdict=pre_verdict)
            return
        exp["travel_days"] = exp.get("travel_days", 0) + 1
        # 消耗自带口粮（按存活人数；阵亡者不再消耗）
        n = len(alive_members)
        supplies = exp.get("supplies", {})
        for k in (FOOD, WATER):
            cost = EXPEDITION_SUPPLY_PER_DAY[k] * n
            supplies[k] = round(max(0.0, supplies.get(k, 0.0) - cost), 1)
        exp["supplies"] = supplies
        # 物资耗尽或达到最长探索天数：当日强制返程，补给不得出现负值快照
        if supplies.get(FOOD, 0) <= 0 or supplies.get(WATER, 0) <= 0:
            self._settle_expedition(
                exp, reason="补给耗尽，探索队被迫返程", pre_verdict=pre_verdict
            )
            return
        if exp["travel_days"] >= EXPEDITION_MAX_DAYS:
            self._settle_expedition(
                exp, reason="探索期满，探索队返程", pre_verdict=pre_verdict
            )
            return
        # 整体回写，确保 JSON 列变更被追踪并落库
        self.session.expedition = dict(exp)

    def _maybe_trigger_expedition_encounter(self, exp):
        """每日行军后概率触发遭遇；已有待处理遭遇时不重复触发。"""
        if exp.get("pending_encounter"):
            return exp["pending_encounter"]
        if self.rand.random() > EXPEDITION_ENCOUNTER_CHANCE:
            return None
        event = self.rand.choice(EXPEDITION_ENCOUNTERS)
        encounter = self._build_expedition_encounter(event, exp)
        self._write_pending(PENDING_ENCOUNTER, encounter)
        return encounter

    def _build_expedition_encounter(self, event, exp):
        # 目标只从队内存活队员中抽取（与危机/途中事件同一快照结构）
        alive_members = [r for r in self._away_residents() if r.alive]
        return self._build_pending_event(event, alive_members)

    # 探索队动作类型（档案级幂等凭据 last_expedition 的 action 取值）
    _EXP_ACT_ENCOUNTER = "encounter"
    _EXP_ACT_RETURN = "return"

    def resolve_expedition_encounter(self, choice_key, token=None):
        """处理探索队途中遭遇：抉择影响队员健康/士气、物资与战利品。

        结算必须命中央档案里唯一的待处理遭遇：事件、选项、单体目标都与存档绑定，
        token 用于识别过期/重复请求；结算后待处理遭遇被清除。
        返回 (detail, replayed)：replayed=True 表示重复/并发落败请求，未再次施加效果。
        """
        self._ensure_running()
        # 幂等回放优先：遭遇结算后、下一个探索队动作前的连点/并发落败只回放。
        # 若档案级凭据已被后续动作（如返程）覆盖，说明遭遇所属状态已前进，
        # 落到下方的“无在外队伍/无待处理遭遇”分支并按 409 拒绝
        replay = self._mission_credential_replay(
            PENDING_ENCOUNTER, self._EXP_ACT_ENCOUNTER, token, choice=choice_key
        )
        if replay[0] is not None:
            return replay
        # 遭遇已直接触发队伍收敛（补给耗尽/全员阵亡/终局）：凭据已被返程记录
        # 覆盖，但记录上仍挂着本次遭遇 token，命中则回放返程明细而非 409
        converged = self._converged_settlement_replay(
            PENDING_ENCOUNTER, self._EXP_ACT_RETURN, token, choice=choice_key
        )
        if converged[0] is not None:
            return converged
        exp, pending = self._locate_pending_decision(PENDING_ENCOUNTER, token)
        event = self._pending_event_def(PENDING_ENCOUNTER, pending)
        choice = self._event_choice(event, choice_key)
        effects = choice.get("effects", {})
        # 单体目标校验：必须是队内存活队员，且与待处理遭遇绑定
        target = self._bound_target(pending, choice, self._away_residents(), "队员")
        # 在应用任何效果前完成校验，保证失败时档案状态不发生部分变更
        detail_parts = []
        exp.setdefault("casualties", [])
        alive_members = [r for r in self._away_residents() if r.alive]
        # 战利品（单独累计，返程时统一入库）
        loot = exp.get("loot", {})
        for k, v in effects.get("loot", {}).items():
            loot[k] = round(loot.get(k, 0.0) + v, 1)
            detail_parts.append(f"战利品 {RESOURCE_ZH.get(k, k)} +{v:g}")
        exp["loot"] = loot
        # 物资损失（从探索队自带物资中扣除，不为负）
        supplies = exp.get("supplies", {})
        for k, v in effects.get("supply_loss", {}).items():
            supplies[k] = round(max(0.0, supplies.get(k, 0.0) - v), 1)
            detail_parts.append(f"物资损失 {RESOURCE_ZH.get(k, k)} -{v:g}")
        exp["supplies"] = supplies
        # 健康/士气：单体作用于目标队员，全体作用于队内存活者
        self._apply_stat_effects(effects, target, alive_members, "全体队员", detail_parts)
        # 全部效果施加完毕后统一收敛伤亡：健康归零即阵亡
        exp["casualties"] = self._sweep_casualties(alive_members, exp.get("casualties", []))
        # 健康结算：受伤队员登记为病例，随队伍冻结，回堡后续治
        if "health" in effects:
            spec = effects["health"]
            affected = [target] if self._effect_scope(spec) == "single" else [
                r for r in alive_members if r.alive
            ]
            self._settle_health_aftermath(
                affected, infectious_event=event["key"] in MED_INFECTIOUS_ENCOUNTER_EVENTS,
                reason=f"探索遭遇·{event['title']}", force=True,
            )
        # 偶遇幸存者加入队伍
        if effects.get("add_resident"):
            name = self._random_survivor_name()
            self.db.flush()
            r = Resident(
                session_id=self.session.id, name=name, job="general",
                health=60.0, morale=50.0, alive=1, joined_day=self.session.day,
            )
            self.session.residents.append(r)
            self.db.add(r)
            self.db.flush()  # 取得新居民 id
            exp["members"].append(r.id)
            self.session.survivors += 1
            detail_parts.append(f"新幸存者 {name} 加入队伍")
        # 日志与实际结算同一作用域
        scope_zh = f"（目标：{target.name}）" if target else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", f"探索遭遇·{event['title']}", f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])
        # 清除待处理遭遇、写入档案级幂等凭据，队伍继续在外行军
        enc_token = pending.get("token")
        exp["pending_encounter"] = None
        exp["encounters_resolved"] = exp.get("encounters_resolved", 0) + 1
        self.session.expedition = dict(exp)
        self._remember_credential(
            PENDING_ENCOUNTER, detail, action=self._EXP_ACT_ENCOUNTER,
            token=enc_token, host_token=exp.get("token"), choice=choice["key"],
        )
        # 遭遇结算后立即收敛，不把“零补给 / 全员阵亡 / 人口归零”的队伍留给下一步：
        #   1) 全员阵亡 —— 无人生还的队伍不能继续行军（僵尸队伍）
        #   2) 自带补给耗尽 —— 无需再等一次“推进一天”，当场被迫返程
        #   3) 人口归零/抵达目标日等终局 —— 先安全返程再收敛到 ended
        # 返程结算内部会在战利品入库前快照终局裁决，收敛只会发生一次
        alive_after = [r for r in self._away_residents() if r.alive]
        supplies_after = exp.get("supplies", {})
        supplies_out = supplies_after.get(FOOD, 0) <= 0 or supplies_after.get(WATER, 0) <= 0
        settle_reason = None
        if not alive_after:
            settle_reason = "探索队全员失联"
        elif supplies_out:
            settle_reason = "补给耗尽，探索队被迫返程"
        elif self._end_conditions_met():
            settle_reason = "终局已至，探索队返程"
        if settle_reason is not None:
            return_detail, _ = self._settle_expedition(
                self.session.expedition, reason=settle_reason,
                enc_token=enc_token, enc_choice=choice["key"],
            )
            # 遭遇响应同时承载遭遇效果与当场返程结算；返程凭据记录同一份
            # 明细，保证该遭遇的连点/并发落败回放结果逐字一致
            detail = f"{detail}；队伍返程：{return_detail}"
            rec = self._read_credential(PENDING_ENCOUNTER)
            rec["detail"] = detail
            self._write_credential(PENDING_ENCOUNTER, rec)
            return detail, False
        return detail, False

    def reconcile_stale_expedition(self, action, token=None, choice_key=None, exp_token=None):
        """并发落败（版本冲突）后核对：若对方提交的是同一次探索队动作则安全回放。

        对不上任何已知结算时抛 409，由调用方提示刷新，杜绝并发重复结算。
        """
        if action == self._EXP_ACT_ENCOUNTER:
            replay = self._mission_credential_replay(
                PENDING_ENCOUNTER, action, token, choice=choice_key
            )
            if replay[0] is not None:
                return replay
            # 遭遇已直接触发队伍收敛（返程凭据覆盖了遭遇凭据）：
            # 凭遭遇 token 回放那次返程结算，落败方同样拿到 200 而非 409；
            # 对不上任何已知结算（token/选项不符）则落入统一的 409
            converged = self._converged_settlement_replay(
                PENDING_ENCOUNTER, self._EXP_ACT_RETURN, token, choice=choice_key
            )
            if converged[0] is not None:
                return converged
        else:
            replay = self._mission_credential_replay(
                PENDING_ENCOUNTER, action, token, host_token=exp_token
            )
            if replay[0] is not None:
                return replay
        raise BunkerEngineConflict("探索队状态已被其他请求更新，请刷新后重试")

    def return_expedition(self, token=None):
        """玩家主动召回探索队：结算战利品入库、伤亡扣减、剩余物资归还。

        返程必须命中央档案里唯一的在外探索队；队伍 token 用于识别过期/重复请求。
        结算后探索队状态被清除并在档案上留下幂等凭据，重复提交只回放。
        返回 (detail, replayed)。
        """
        self._ensure_running()
        # 幂等回放优先：返程后队伍已清除，凭据仍在档案上可识别连点/并发落败请求
        replay = self._mission_credential_replay(
            PENDING_ENCOUNTER, self._EXP_ACT_RETURN, host_token=token
        )
        if replay[0] is not None:
            return replay
        # 返程属于地堡经营动作：危机/遭遇待处理阶段一律锁定（幂等回放除外）
        self._require_daily_phase("召回探索队")
        exp = self.session.expedition
        if not exp or exp.get("status") != "away":
            # 队伍已不在外：通常是上一次返程已完成。携带不匹配 token 的请求
            # 属于过期/串档，明确报 409；完全无凭据时才按“无队伍”处理
            rec = self._read_credential(PENDING_ENCOUNTER)
            if token and rec and rec.get("action") == self._EXP_ACT_RETURN:
                raise BunkerEngineConflict("探索队状态已过期，请刷新后重试")
            raise BunkerEngineError("当前没有在外的探索队")
        if exp.get("pending_encounter"):
            raise BunkerEngineError("探索队还有未处理的遭遇，无法返程")
        if token is not None and exp.get("token") and token != exp["token"]:
            raise BunkerEngineConflict("探索队状态已过期，请刷新后重试")
        return self._settle_expedition(exp, reason="探索队安全返程")

    def _settle_expedition(self, exp, reason, enc_token=None, enc_choice=None, pre_verdict=None):
        """结算探索队返程：战利品入库、剩余自带物资归还、伤亡扣减。

        幂等：以队伍 token 为凭据写入档案级 last_expedition，重复调用只回放，
        不二次发放战利品。enc_token/enc_choice 非空表示本次返程由某次遭遇
        抉择直接触发（补给耗尽/全员失联/终局收敛），返程凭据同时挂住该
        遭遇的一次性 token，使该遭遇的连点/并发落败请求也能安全回放。
        pre_verdict 为状态变更前快照的终局裁决（如行军日推进开始时已枯竭），
        优先于本方法内部快照。返回 (detail, replayed)。
        """
        exp_token = exp.get("token")
        replay = self._mission_credential_replay(
            PENDING_ENCOUNTER, self._EXP_ACT_RETURN, host_token=exp_token
        )
        if replay[0] is not None:
            return replay
        members = self._away_residents()
        dead_members = [r for r in members if not r.alive]
        # 终局裁决在战利品/余粮入库前快照：全线枯竭的败局不得被随后入库的
        # 战利品抬过阈值而“复活”，终局状态与本结算只收敛一次（主动返程、
        # 补给耗尽强制返程、遭遇收敛各路径口径一致）；调用方（行军日推进）
        # 在产出前快照的更早裁决同样优先
        verdict = pre_verdict if pre_verdict is not None else self._end_verdict()
        # 战利品入库
        loot = exp.get("loot", {})
        loot_parts = [f"{RESOURCE_ZH.get(k, k)} +{v:g}" for k, v in loot.items() if v > 0]
        for k, v in loot.items():
            if v > 0:
                self._add_resource(k, v)
        # 剩余自带物资归还地堡（行军消耗已先行扣减，只归还正值余额）
        supplies = exp.get("supplies", {})
        supply_parts = [f"剩余{RESOURCE_ZH.get(k, k)} +{round(v, 1):g}" for k, v in supplies.items() if v > 0]
        for k, v in supplies.items():
            if v > 0:
                self._add_resource(k, v)
        # 伤亡（阵亡队员已在遭遇结算时扣减过 survivors，此处不再重复扣减）
        casualty_names = [r.name for r in dead_members]
        # 组装日志
        detail_parts = []
        if loot_parts:
            detail_parts.append("战利品：" + "、".join(loot_parts))
        if supply_parts:
            detail_parts.append("归还物资：" + "、".join(supply_parts))
        if casualty_names:
            detail_parts.append(f"殉职：{'、'.join(casualty_names)}")
        else:
            detail_parts.append("全员平安归来")
        detail = "；".join(detail_parts)
        self._log("system", f"探索队返程（{reason}）", detail, decision="返程结算")
        # 先写档案级幂等凭据，再清除探索队状态：凭据在队伍消失后依然可查
        self._remember_credential(
            PENDING_ENCOUNTER, detail, action=self._EXP_ACT_RETURN,
            token=enc_token, host_token=exp_token, choice=enc_choice,
        )
        self.session.expedition = None
        # 用入库前快照收敛终局；无预设败局时再按结算后状态正常判定
        self._check_end(forced_verdict=verdict)
        return detail, False


    # ---- 贸易救援 ----
    # 订单状态链：
    #   reviewing    申请已提交，等待外部聚落审核（托管物资已冻结）
    #     ├─ rejected 审核驳回：全额退还托管，订单关闭
    #     └─ transporting 审核通过：押运队离堡在途（成员按离堡口径结算）
    #          ├─ delivered 按期抵达并交付：回礼/采购入库，信誉与士气上升
    #          ├─ failed    途中弃货/全损/全员失联或交付失败：剩余货物回退、降信誉
    #          └─ 途中事件挂起（trade 阶段）：抉择后继续运输或当场收敛为 failed
    #   cancelled 玩家在审核阶段主动撤单：全额退还托管
    def _add_reputation(self, delta):
        rep = int(_clamp(
            (self.session.reputation if self.session.reputation is not None else TRADE_INITIAL_REPUTATION)
            + delta, TRADE_REP_MIN, TRADE_REP_MAX,
        ))
        self.session.reputation = rep
        return rep

    def trade_market(self):
        """生成当日外部聚落的贸易/救援报价（确定性，无副作用）。

        以"日期 + 序号"为种子：同一天内重复打开市场报价一致，跨天自动轮换，
        不随玩家刷新页面变化；申请时引擎重新生成当日市场并核对 offer_id，
        过期（跨天/被轮换掉）的报价无法下单。

        两类报价：
          rescue  聚落求援：地堡押运 escrow 物资前往，成功交付后对方回礼 cargo + 信誉
          procure 地堡采购：地堡预付 escrow，对方在交付时运来 cargo（风险共担，
                  在途损失按比例退款）
        """
        import random
        rng = random.Random(f"bunker-trade-market-day-{self.session.day}")
        partners = list(TRADE_PARTNERS)
        rng.shuffle(partners)
        offers = []
        # 前两个聚落发出求援，后两个聚落开放采购，四个交易对手互不重复
        for i, p in enumerate(partners[:4]):
            kind = "rescue" if i < 2 else "procure"
            others = [k for k in RESOURCE_KEYS if k != p["favor"]]
            if kind == "rescue":
                want = rng.choice(others)
                amount = rng.randint(26, 48)
                # 回礼按物资相对价值折算（对方出产的 favor 物资计价），含商谈浮动
                factor = rng.uniform(0.95, 1.25)
                reward = max(6, round(amount * TRADE_VALUE[want] / TRADE_VALUE[p["favor"]] * factor))
                offers.append({
                    "id": f"r-{p['key']}-{self.session.day}",
                    "type": "rescue",
                    "partner": p["key"],
                    "partner_name": p["name"],
                    "eta": p["distance"],
                    "escrow": {want: float(amount)},
                    "cargo": {p["favor"]: float(reward)},
                    "hint": f"{p['name']} 急缺{RESOURCE_ZH[want]}，愿以{RESOURCE_ZH[p['favor']]}回礼，押运约 {p['distance']} 天",
                })
            else:
                give = p["favor"]
                cost_res = rng.choice(others)
                qty = rng.randint(15, 34)
                markup = rng.uniform(1.05, 1.3)
                cost_amt = max(8, round(qty * TRADE_VALUE[give] / TRADE_VALUE[cost_res] * markup) + 4)
                offers.append({
                    "id": f"p-{p['key']}-{self.session.day}",
                    "type": "procure",
                    "partner": p["key"],
                    "partner_name": p["name"],
                    "eta": p["distance"],
                    "escrow": {cost_res: float(cost_amt)},
                    "cargo": {give: float(qty)},
                    "hint": f"向{p['name']}采购{RESOURCE_ZH[give]}，预付{RESOURCE_ZH[cost_res]}，押运约 {p['distance']} 天",
                })
        return offers

    def _find_offer(self, offer_id):
        return next((o for o in self.trade_market() if o["id"] == offer_id), None)

    def apply_trade(self, offer_id, escort_ids):
        """提交贸易/救援订单申请：冻结托管物资、组建押运队，进入 reviewing。"""
        self._require_daily_phase("申请贸易订单")
        self._require_no_mission("办理贸易订单")
        offer = self._find_offer(offer_id)
        if offer is None:
            raise BunkerEngineError("报价已过期或不存在（市场每日轮换），请重新打开市场")
        # 押运队校验：在堡存活居民，人数 1-3，不可重复
        if not escort_ids:
            raise BunkerEngineError("必须指定至少一名押运队员")
        if len(set(escort_ids)) != len(escort_ids):
            raise BunkerEngineError("同一名居民不能重复编入押运队")
        if len(escort_ids) > TRADE_MAX_ESCORTS:
            raise BunkerEngineError(f"押运队最多 {TRADE_MAX_ESCORTS} 人")
        for mid in escort_ids:
            r = next((x for x in self.session.residents if x.id == mid), None)
            if not r or not r.alive:
                raise BunkerEngineError("押运队员不存在或已故")
            if r.id in self._away_resident_ids():
                raise BunkerEngineError(f"{r.name} 已离堡，无法参加押运")
            if self._has_active_case(r.id):
                raise BunkerEngineError(f"{r.name} 有未结病例，无法参加押运")
        if not self._can_afford(offer["escrow"]):
            raise BunkerEngineError("托管物资不足，无法申请该订单")
        # 冻结托管物资（审核驳回/撤单/失败回退时按规则退还）
        for k, v in offer["escrow"].items():
            self._add_resource(k, -v)
        order = {
            "token": uuid.uuid4().hex,
            "offer_id": offer["id"],
            "type": offer["type"],
            "partner": offer["partner"],
            "partner_name": offer["partner_name"],
            "eta": offer["eta"],
            "status": TRADE_REVIEWING,
            "applied_day": self.session.day,
            "escorts": list(escort_ids),
            "escrow": dict(offer["escrow"]),      # 已冻结的托管物资
            "cargo": dict(offer["cargo"]),        # 成功交付时地堡应得物资
            "cargo_ratio": 1.0,                   # 在途货物残存比例（途中事件损耗）
            "travel_days": 0,
            "incidents_resolved": 0,
            "pending_incident": None,
        }
        self.session.trade_order = dict(order)
        names = "、".join(r.name for r in self._trade_escorts(order))
        kind_zh = "救援申请" if offer["type"] == "rescue" else "采购申请"
        self._log(
            "trade", f"{kind_zh}·{offer['partner_name']}",
            f"{names} 组成押运队，托管物资已冻结，等待对方审核。",
            decision="提交申请",
        )
        return order

    def cancel_trade(self, token=None):
        """审核阶段主动撤单：全额退还托管。进入运输后不可撤单。"""
        self._ensure_running()
        # 幂等回放优先：撤单后订单已清除，凭据仍在档案上可识别连点
        replay = self._mission_credential_replay(PENDING_INCIDENT, self._TRADE_ACT_CANCEL, token)
        if replay[0] is not None:
            return replay
        self._require_daily_phase("撤销贸易订单")
        order = self.session.trade_order
        if not order:
            if token and self._read_credential(PENDING_INCIDENT):
                raise BunkerEngineConflict("贸易订单状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有在谈的贸易订单")
        if token is not None and order.get("token") and token != order["token"]:
            raise BunkerEngineConflict("贸易订单状态已过期，请刷新后重试")
        if order.get("status") != TRADE_REVIEWING:
            # 审核已通过、订单进入运输：撤单窗口关闭，按状态过期处理（409）
            raise BunkerEngineConflict("订单已进入运输阶段，无法撤销，请刷新后重试")
        detail = self._refund_escrow(order, ratio=1.0, label="撤单退还")
        self._log("trade", f"撤单·{order['partner_name']}", detail, decision="撤销申请")
        detail = detail or "托管物资已全额退还"
        self._remember_credential(PENDING_INCIDENT, detail, action=self._TRADE_ACT_CANCEL, token=order.get("token"))
        self.session.trade_order = None
        return detail, False

    def _refund_escrow(self, order, ratio, label="退还"):
        """按残存比例退还托管物资，返回明细文本。"""
        parts = []
        for k, v in order.get("escrow", {}).items():
            amt = round(v * ratio, 1)
            if amt > 0:
                self._add_resource(k, amt)
                parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
        return f"{label}：" + "、".join(parts) if parts else ""

    # -- 每日推进：审核 / 在途运输 / 抵达交付 --
    def _progress_trade_order(self, order, pre_verdict=None):
        """推进贸易订单一天。返回挂起的途中事件（或 None）。

        - reviewing：审核日。终局已锁定时直接取消（全额退款）；否则掷审核，
          通过则当日出发并立刻走第一个在途日
        - transporting：在途日累加，途中可能挂起事件；抵达 eta 则交付/回退
        """
        if order.get("status") == TRADE_REVIEWING:
            # 终局日不再进行审核：撤单退款后随档案收敛到 ended
            if pre_verdict is not None:
                detail = self._refund_escrow(order, ratio=1.0, label="终局撤单退还")
                self._log("trade", f"撤单·{order['partner_name']}", detail or "终局已至，申请撤销", decision="终局撤单")
                self._remember_credential(
                    PENDING_INCIDENT, detail or "终局已至，申请撤销",
                    action=self._TRADE_ACT_CANCEL, token=order.get("token"),
                )
                self.session.trade_order = None
                return None
            if not self._review_trade_order(order):
                return None  # 审核驳回：订单已关闭
            # 审核通过：当日出发，继续走第一个在途日
        # transporting（含当日刚通过审核的订单）
        return self._tick_trade_transport(self.session.trade_order, pre_verdict=pre_verdict)

    def _review_trade_order(self, order):
        """外部聚落审核：信誉越高越容易通过。返回是否通过。"""
        rep = self.session.reputation or TRADE_INITIAL_REPUTATION
        # 求援方更看重地堡过往信誉（0.50-1.00）；采购方对陌生地堡更谨慎（0.40-0.80）
        chance = 0.50 + rep / 200.0 if order["type"] == "rescue" else 0.40 + rep / 250.0
        if self.rand.random() >= chance:
            detail = self._refund_escrow(order, ratio=1.0, label="全额退还")
            self._log(
                "trade", f"审核驳回·{order['partner_name']}",
                (detail + "；" if detail else "") + "对方回绝了本次申请，托管物资已退回。",
                decision="审核驳回",
            )
            self.session.trade_order = None
            return False
        order["status"] = TRADE_TRANSPORTING
        self.session.trade_order = dict(order)
        names = "、".join(r.name for r in self._trade_escorts(order) if r.alive)
        self._log(
            "trade", f"审核通过·{order['partner_name']}",
            f"{order['partner_name']} 接受申请，{names} 押运物资出发。",
            decision="审核通过",
        )
        return True

    def _tick_trade_transport(self, order, pre_verdict=None):
        """在途运输一天：累计行程、判定抵达或触发途中事件。"""
        alive_escorts = [r for r in self._trade_escorts(order) if r.alive]
        if not alive_escorts:
            # 押运队全员失联（理论上事件结算时即收敛，这里兜底不留僵尸订单）
            self._fail_trade_order(order, "押运队全员失联", forced_verdict=pre_verdict)
            return None
        order["travel_days"] += 1
        # 抵达日：直接交付/回退，不再触发途中事件
        if order["travel_days"] >= order["eta"]:
            self._deliver_trade_order(order, reason="押运队抵达聚落", forced_verdict=pre_verdict)
            return None
        if self.rand.random() <= TRADE_INCIDENT_CHANCE:
            event = self.rand.choice(TRADE_INCIDENTS)
            incident = self._build_trade_incident(event, order)
            self._write_pending(PENDING_INCIDENT, incident)
            return incident
        self.session.trade_order = dict(order)
        return None

    def _build_trade_incident(self, event, order):
        """构造途中事件快照（与危机/探索遭遇同一结构，刷新后可恢复抉择）。"""
        alive_escorts = [r for r in self._trade_escorts(order) if r.alive]
        return self._build_pending_event(event, alive_escorts)

    # 贸易动作类型（档案级幂等凭据 last_trade 的 action 取值）
    _TRADE_ACT_INCIDENT = "incident"
    _TRADE_ACT_SETTLE = "settle"   # 订单收敛（交付成功 / 失败回退 / 撤单退款）
    _TRADE_ACT_CANCEL = "cancel"

    def resolve_trade_incident(self, choice_key, token=None):
        """结算押运途中的事件抉择。

        效果键：cargo_loss（在途货物损耗比例）、delay（延误天数）、
        reputation（信誉变化）、health/morale（押运队员，single/all）、
        abort（弃货撤回：效果结清后当场按失败回退收敛）。
        返回 (detail, replayed)。
        """
        self._ensure_running()
        replay = self._mission_credential_replay(
            PENDING_INCIDENT, self._TRADE_ACT_INCIDENT, token, choice=choice_key
        )
        if replay[0] is not None:
            return replay
        # 事件抉择直接触发失败收敛：凭据已被失败记录覆盖，但仍挂着事件 token
        converged = self._converged_settlement_replay(
            PENDING_INCIDENT, self._TRADE_ACT_SETTLE, token, choice=choice_key
        )
        if converged[0] is not None:
            return converged
        order, pending = self._locate_pending_decision(PENDING_INCIDENT, token)
        event = self._pending_event_def(PENDING_INCIDENT, pending)
        choice = self._event_choice(event, choice_key)
        effects = choice.get("effects", {})
        target = self._bound_target(pending, choice, self._trade_escorts(order), "押运队员")
        # 校验全部完成后再施加效果，失败不留部分变更
        detail_parts = []
        alive_escorts = [r for r in self._trade_escorts(order) if r.alive]
        if effects.get("cargo_loss"):
            loss = float(effects["cargo_loss"])
            order["cargo_ratio"] = round(max(0.0, order.get("cargo_ratio", 1.0) * (1.0 - loss)), 3)
            detail_parts.append(f"货物损耗 {int(loss * 100)}%（残存 {int(order['cargo_ratio'] * 100)}%）")
        if effects.get("delay"):
            d = int(effects["delay"])
            order["eta"] += d
            detail_parts.append(f"行程延误 {d} 天")
        if effects.get("reputation"):
            rep = self._add_reputation(int(effects["reputation"]))
            detail_parts.append(f"信誉 {int(effects['reputation']):+d}（现 {rep}）")
        self._apply_stat_effects(effects, target, alive_escorts, "全体押运队员", detail_parts)
        if effects.get("abort"):
            detail_parts.append("弃货撤回")
        # 统一收敛押运伤亡（与探索遭遇同一口径，人口只扣一次）
        order["casualties"] = self._sweep_casualties(
            alive_escorts, order.get("casualties", [])
        )
        # 健康结算：受伤押运队员登记为病例，随押运队冻结，归队后续治
        if "health" in effects:
            spec = effects["health"]
            affected = [target] if self._effect_scope(spec) == "single" else [
                r for r in alive_escorts if r.alive
            ]
            self._settle_health_aftermath(
                affected, infectious_event=event["key"] in MED_INFECTIOUS_INCIDENT_EVENTS,
                reason=f"途中事件·{event['title']}", force=True,
            )
        scope_zh = f"（目标：{target.name}）" if target else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", f"途中事件·{event['title']}", f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])
        inc_token = pending.get("token")
        order["pending_incident"] = None
        order["incidents_resolved"] += 1
        self.session.trade_order = dict(order)
        self._remember_credential(
            PENDING_INCIDENT, detail, action=self._TRADE_ACT_INCIDENT,
            token=inc_token, host_token=order.get("token"), choice=choice["key"],
        )
        # 与探索遭遇一致：事件结算后立即收敛，不把零货物/全员阵亡的队伍留给下一步
        alive_after = [r for r in self._trade_escorts(order) if r.alive]
        settle_reason = None
        if not alive_after:
            settle_reason = "押运队全员失联"
        elif order["cargo_ratio"] <= 0:
            settle_reason = "货物全部损失，押运队空车返程"
        elif effects.get("abort"):
            settle_reason = "押运队弃货撤回"
        elif self._end_conditions_met():
            # 人口归零/全线枯竭等终局：强制安全交付后再收敛到 ended
            self._deliver_trade_order(
                self.session.trade_order, reason="终局已至，押运队返程", force_success=True,
            )
            rec = self._read_credential(PENDING_INCIDENT)
            return_detail = rec.get("detail", "") if rec else ""
            detail = f"{detail}；订单结算：{return_detail}" if return_detail else detail
            return detail, False
        if settle_reason is not None:
            return_detail, _ = self._fail_trade_order(
                self.session.trade_order, settle_reason,
                inc_token=inc_token, inc_choice=choice["key"],
            )
            detail = f"{detail}；订单回退：{return_detail}"
            rec = self._read_credential(PENDING_INCIDENT)
            rec["detail"] = detail
            self._write_credential(PENDING_INCIDENT, rec)
            return detail, False
        return detail, False

    def reconcile_stale_trade(self, action, token=None, choice_key=None):
        """并发落败后核对贸易动作：同一次抉择/结算则安全回放，否则 409。"""
        if action == self._TRADE_ACT_INCIDENT:
            replay = self._mission_credential_replay(
                PENDING_INCIDENT, action, token, choice=choice_key
            )
            if replay[0] is not None:
                return replay
            # 事件抉择直接触发订单收敛（结算凭据覆盖了事件凭据）：
            # 凭事件 token 回放那次结算，落败方同样拿到 200 而非 409
            converged = self._converged_settlement_replay(
                PENDING_INCIDENT, self._TRADE_ACT_SETTLE, token, choice=choice_key
            )
            if converged[0] is not None:
                return converged
        else:
            replay = self._mission_credential_replay(PENDING_INCIDENT, action, token)
            if replay[0] is not None:
                return replay
        raise BunkerEngineConflict("贸易订单状态已被其他请求更新，请刷新后重试")

    def _trade_rep_penalty(self, order):
        """失败回退的信誉扣减：求援订单失信代价更高。"""
        return -8 if order["type"] == "rescue" else -5

    def _deliver_trade_order(self, order, reason, forced_verdict=None, force_success=False):
        """抵达交付结算：成功则回礼/采购入库，失败则剩余货物回退。

        forced_verdict 为推进开始前快照的终局裁决，优先于本方法内部快照，
        保证入库的回礼不会复活当日已成立的败局（与探索队返程同一口径）。
        """
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        rep = self.session.reputation or TRADE_INITIAL_REPUTATION
        # 成功概率：求援 0.55-0.95，采购 0.60-1.00，均随信誉提高
        chance = (0.55 + rep / 250.0) if order["type"] == "rescue" else (0.60 + rep / 250.0)
        success = force_success or self.rand.random() < chance
        ratio = order.get("cargo_ratio", 1.0)
        if not success:
            return self._fail_trade_order(
                order, f"{reason}，但交易失败", inc_token=None, forced_verdict=verdict,
            )
        parts = []
        if order["type"] == "rescue":
            # 求援：援助物资已送达对方，原则上不退回；仅在途损耗的残份（ratio<1）
            # 随车带回；对方按实际送达比例回礼
            if ratio < 1.0:
                refund = self._refund_escrow(order, ratio=1.0 - ratio, label="未送达的援助物资带回")
                if refund:
                    parts.append(refund)
            gain_parts = []
            for k, v in order["cargo"].items():
                amt = round(v * ratio, 1)
                if amt > 0:
                    self._add_resource(k, amt)
                    gain_parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
            if gain_parts:
                parts.append("对方回礼：" + "、".join(gain_parts))
            rep_now = self._add_reputation(6)
            for r in self._in_bunker_residents():
                r.morale = _clamp(r.morale + 8)
            for r in self._trade_escorts(order):
                if r.alive:
                    r.morale = _clamp(r.morale + 10)
            parts.append(f"信誉 +6（现 {rep_now}），在堡全员士气 +8")
            title = f"救援送达·{order['partner_name']}"
        else:
            # 采购：按残存比例到货，损失部分对应托管按比例退还（风险共担）
            gain_parts = []
            for k, v in order["cargo"].items():
                amt = round(v * ratio, 1)
                if amt > 0:
                    self._add_resource(k, amt)
                    gain_parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
            parts.append("采购到货：" + "、".join(gain_parts))
            refund = self._refund_escrow(order, ratio=1.0 - ratio, label="损失部分退款")
            if refund:
                parts.append(refund)
            rep_now = self._add_reputation(3)
            for r in self._in_bunker_residents():
                r.morale = _clamp(r.morale + 5)
            for r in self._trade_escorts(order):
                if r.alive:
                    r.morale = _clamp(r.morale + 7)
            parts.append(f"信誉 +3（现 {rep_now}），在堡全员士气 +5")
            title = f"采购到货·{order['partner_name']}"
        detail = "；".join(parts)
        self._log("trade", title, f"{reason}。{detail}", decision="交付结算")
        self._remember_credential(
            PENDING_INCIDENT, detail, action=self._TRADE_ACT_SETTLE,
            host_token=order.get("token"),
        )
        # 终局统计：仅成功交付计入贡献（撤单/审核被拒不进入本路径）。
        # 凭据回放早于本方法返回，重复请求不会再次计数
        self._bump_mission_stats(trade_delivered=1)
        # 用交付前快照收敛终局
        self.session.trade_order = None
        self._check_end(forced_verdict=verdict)
        return detail, False

    def _fail_trade_order(self, order, reason, inc_token=None, inc_choice=None, forced_verdict=None):
        """失败回退：未送出的托管物资退回、扣信誉、押运队士气受挫。

        求援订单：援助未送达（交易失败/弃货撤回），托管物资按残存比例全额带回；
        采购订单：货到不了，预付托管按残存比例退回（其余视为共同损失）。
        inc_token 非空表示本次回退由某条途中事件抉择直接触发（弃货/全损/
        全员失联），回退凭据同时挂住该事件一次性 token，供连点/并发落败回放。
        返回 (detail, False)。
        """
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        ratio = order.get("cargo_ratio", 1.0)
        parts = []
        label = "未送达援助物资带回" if order["type"] == "rescue" else "预付物资退回"
        refund = self._refund_escrow(order, ratio=ratio, label=label)
        if refund:
            parts.append(refund)
        penalty = self._trade_rep_penalty(order)
        rep_now = self._add_reputation(penalty)
        morale_hit = -8 if order["type"] == "rescue" else -5
        for r in self._trade_escorts(order):
            if r.alive:
                r.morale = _clamp(r.morale + morale_hit)
        dead = [r.name for r in self._trade_escorts(order) if not r.alive]
        parts.append(f"信誉 {penalty:+d}（现 {rep_now}），押运队员士气 {morale_hit:+d}")
        if dead:
            parts.append(f"殉职：{'、'.join(dead)}")
        detail = "；".join(parts)
        self._log("trade", f"订单失败·{order['partner_name']}", f"{reason}。{detail}", decision="失败回退")
        self._remember_credential(
            PENDING_INCIDENT, detail, action=self._TRADE_ACT_SETTLE,
            token=inc_token, host_token=order.get("token"), choice=inc_choice,
        )
        # 终局统计：确认失败计入负贡献（审核驳回/主动撤单不走本路径，不扣分）
        self._bump_mission_stats(trade_failed=1)
        self.session.trade_order = None
        self._check_end(forced_verdict=verdict)
        return detail, False


    # ---- 地堡联盟援助协议 ----
    # 协议状态链（三方签约 + 托管 + 押运审核 + 交付）：
    #   proposed    管理者发起且医护负责人会签，医援物资已托管冻结，
    #               等待外部聚落按地堡信誉 + 会签资质掷签约审核
    #     ├─ rejected 对方拒签：全额退还托管，信誉小挫，协议关闭
    #     └─ escorting 签约通过：援助押运队离堡在途（成员按离堡口径结算）
    #          ├─ 抵达 eta 当日先过押运审核（疫区检疫关卡）：
    #          │    ├─ 通过 → 交付：医援入库、全员救治、医疗危机缓解、信誉士气上升
    #          │    └─ 未过 → failed：托管退回、押运审核触发疫情反扑、信誉下降
    #          ├─ failed    途中弃货/全损/全员失联：剩余货物回退、危机激化、重罚信誉
    #          └─ 途中事件挂起（aid 阶段）：抉择后继续运输或当场收敛为 failed
    #   lapsed  签约审核逾期（会签医护已不具备资质）：全额退还，不罚信誉
    #   cancelled 管理者在 proposed 阶段主动撤回：全额退还托管
    def aid_board(self):
        """生成当日联盟公告板的医疗援助请求（确定性，无副作用）。

        以"日期"为种子：同一天内重复打开公告板请求一致，跨天自动轮换；
        发起时引擎重新生成当日公告板并核对 request_id，过期（跨天/被轮换掉）
        的请求无法签约。各联盟聚落遭遇疫情/医疗挤兑，以紧缺物资为托管、
        回礼为其出产物资，eta 即押运天数（抵达日还要过一次检疫关卡）。
        """
        import random
        rng = random.Random(f"bunker-aid-board-day-{self.session.day}")
        partners = list(AID_PARTNERS)
        rng.shuffle(partners)
        requests = []
        for p in partners[:3]:
            others = [k for k in RESOURCE_KEYS if k != p["favor"]]
            want = rng.choice(others)
            amount = rng.randint(24, 42)
            factor = rng.uniform(1.0, 1.3)
            reward = max(8, round(amount * TRADE_VALUE[want] / TRADE_VALUE[p["favor"]] * factor))
            requests.append({
                "id": f"a-{p['key']}-{self.session.day}",
                "partner": p["key"],
                "partner_name": p["name"],
                "eta": p["distance"],
                "escrow": {want: float(amount)},
                "cargo": {p["favor"]: float(reward)},
                "hint": (
                    f"{p['name']} 的隔离营爆发疫情，急缺{RESOURCE_ZH[want]}；"
                    f"押运约 {p['distance']} 天，抵达后还须通过检疫关卡，"
                    f"对方愿以{RESOURCE_ZH[p['favor']]}回礼并签署联盟互助条款"
                ),
            })
        return requests

    def _find_aid_request(self, request_id):
        return next((q for q in self.aid_board() if q["id"] == request_id), None)

    def _aid_party_eligible(self):
        """签约前置：已建医疗救治中心或在堡在任医护，二者居其一。"""
        return self._active_clinic() is not None or self.medic_count() > 0

    def propose_aid(self, request_id, signer_id, escort_ids):
        """管理者发起联盟援助协议：医护负责人会签、托管冻结、组建押运队。"""
        self._require_daily_phase("发起联盟援助协议")
        self._require_no_mission("发起联盟援助协议")
        req = self._find_aid_request(request_id)
        if req is None:
            raise BunkerEngineError("援助请求已过期或不存在（公告板每日轮换），请重新查看")
        if not self._aid_party_eligible():
            raise BunkerEngineError("尚未建立医疗体系（救治中心或在任医护），不具备联盟签约资质")
        # 医护负责人会签：必须是在堡存活、在岗（medic）、无未结病例的居民
        signer = next((x for x in self.session.residents if x.id == signer_id), None)
        if signer is None or not signer.alive:
            raise BunkerEngineError("医护负责人不存在或已故，无法会签")
        if signer.job != "medic":
            raise BunkerEngineError(f"{signer.name} 不是在任医护，不能作为医护负责人会签")
        if signer.id in self._away_resident_ids():
            raise BunkerEngineError(f"{signer.name} 已离堡，无法会签")
        if self._has_active_case(signer.id):
            raise BunkerEngineError(f"{signer.name} 有未结病例，不能作为医护负责人会签")
        # 押运队校验：在堡存活居民，人数 1-3，不可重复；可与医护负责人重合
        if not escort_ids:
            raise BunkerEngineError("必须指定至少一名押运队员")
        if len(set(escort_ids)) != len(escort_ids):
            raise BunkerEngineError("同一名居民不能重复编入援助押运队")
        if len(escort_ids) > AID_MAX_ESCORTS:
            raise BunkerEngineError(f"援助押运队最多 {AID_MAX_ESCORTS} 人")
        for mid in escort_ids:
            r = next((x for x in self.session.residents if x.id == mid), None)
            if not r or not r.alive:
                raise BunkerEngineError("押运队员不存在或已故")
            if r.id in self._away_resident_ids():
                raise BunkerEngineError(f"{r.name} 已离堡，无法参加援助押运")
            if self._has_active_case(r.id):
                raise BunkerEngineError(f"{r.name} 有未结病例，无法参加援助押运")
        if not self._can_afford(req["escrow"]):
            raise BunkerEngineError("托管物资不足，无法签署该援助协议")
        for k, v in req["escrow"].items():
            self._add_resource(k, -v)
        pact = {
            "token": uuid.uuid4().hex,
            "request_id": req["id"],
            "partner": req["partner"],
            "partner_name": req["partner_name"],
            "eta": req["eta"],
            "status": AID_PROPOSED,
            "proposed_day": self.session.day,
            # 医护负责人会签凭据：签约审核与押运审核的资质加成均按发起时绑定
            "signer_id": signer.id,
            "signer_name": signer.name,
            "escorts": list(escort_ids),
            "escrow": dict(req["escrow"]),       # 已冻结的托管医援物资
            "cargo": dict(req["cargo"]),         # 成功交付时地堡应得回礼
            "cargo_ratio": 1.0,                  # 在途货物残存比例（途中事件损耗）
            "travel_days": 0,
            "incidents_resolved": 0,
            "pending_incident": None,
        }
        self.session.aid_pact = dict(pact)
        names = "、".join(r.name for r in self._aid_escorts(pact))
        self._log(
            "aid", f"联盟援助协议·{req['partner_name']}",
            f"管理者发起、医护负责人 {signer.name} 会签：{names} 组成援助押运队，"
            f"托管医援物资已冻结，等待 {req['partner_name']} 签署确认。",
            decision="三方签约",
        )
        return pact

    def cancel_aid(self, token=None):
        """proposed 阶段管理者主动撤回协议：全额退还托管。进入押运后不可撤回。"""
        self._ensure_running()
        replay = self._mission_credential_replay(PENDING_AID_INCIDENT, self._AID_ACT_CANCEL, token)
        if replay[0] is not None:
            return replay
        self._require_daily_phase("撤回联盟援助协议")
        pact = self.session.aid_pact
        if not pact:
            if token and self._read_credential(PENDING_AID_INCIDENT):
                raise BunkerEngineConflict("援助协议状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有在谈的联盟援助协议")
        if token is not None and pact.get("token") and token != pact["token"]:
            raise BunkerEngineConflict("援助协议状态已过期，请刷新后重试")
        if pact.get("status") != AID_PROPOSED:
            raise BunkerEngineConflict("协议已进入押运阶段，无法撤回，请刷新后重试")
        detail = self._refund_aid_escrow(pact, ratio=1.0, label="撤约退还")
        self._log("aid", f"撤约·{pact['partner_name']}", detail or "托管物资已全额退还", decision="撤回协议")
        detail = detail or "托管物资已全额退还"
        self._remember_credential(PENDING_AID_INCIDENT, detail,
                                  action=self._AID_ACT_CANCEL, token=pact.get("token"))
        self.session.aid_pact = None
        return detail, False

    def _refund_aid_escrow(self, pact, ratio, label="退还"):
        """按残存比例退还援助托管物资，返回明细文本。"""
        parts = []
        for k, v in pact.get("escrow", {}).items():
            amt = round(v * ratio, 1)
            if amt > 0:
                self._add_resource(k, amt)
                parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
        return f"{label}：" + "、".join(parts) if parts else ""

    def _signer_qualified(self, pact):
        """会签医护是否仍具资质（存活、仍任医护、无未结病例）。"""
        signer = next((x for x in self.session.residents if x.id == pact.get("signer_id")), None)
        if signer is None or not signer.alive or signer.job != "medic":
            return False
        return not self._has_active_case(signer.id)

    def _escort_has_medic(self, pact):
        """援助押运队是否编入在任医护（押运审核检疫加成）。"""
        return any(
            r.alive and r.job == "medic"
            for r in self._aid_escorts(pact)
        )

    # -- 每日推进：签约审核 / 在途押运 / 押运审核 / 交付 --
    def _progress_aid_pact(self, pact, pre_verdict=None):
        """推进联盟援助协议一天。返回挂起的途中事件（或 None）。

        - proposed：签约审核日。终局已锁定时直接撤回（全额退款）；会签医护
          失去资质则逾期撤约；否则外部聚落按信誉 + 会签资质掷签约审核，
          通过当日出发并立刻走第一个在途日
        - escorting：在途日累加，途中可能挂起事件；抵达 eta 当日先过押运
          审核（检疫关卡），通过再交付，未过直接按失败回退
        """
        if pact.get("status") == AID_PROPOSED:
            if pre_verdict is not None:
                detail = self._refund_aid_escrow(pact, ratio=1.0, label="终局撤约退还")
                self._log("aid", f"撤约·{pact['partner_name']}",
                          detail or "终局已至，协议撤回", decision="终局撤约")
                self._remember_credential(
                    PENDING_AID_INCIDENT, detail or "终局已至，协议撤回",
                    action=self._AID_ACT_CANCEL, token=pact.get("token"),
                )
                self.session.aid_pact = None
                return None
            if not self._signer_qualified(pact):
                # 会签医护伤亡/调岗/病倒：签约审核无法继续，逾期撤约、全额退款
                detail = self._refund_aid_escrow(pact, ratio=1.0, label="逾期退还")
                self._log(
                    "aid", f"签约逾期·{pact['partner_name']}",
                    (detail + "；" if detail else "")
                    + f"医护负责人 {pact.get('signer_name')} 已不具备会签资质，协议逾期关闭。",
                    decision="签约逾期",
                )
                self._remember_credential(
                    PENDING_AID_INCIDENT, detail or "协议逾期关闭",
                    action=self._AID_ACT_SETTLE, host_token=pact.get("token"),
                )
                self.session.aid_pact = None
                return None
            if not self._review_aid_signing(pact):
                return None  # 拒签：协议已关闭
            # 签约通过：当日出发，继续走第一个在途日
        return self._tick_aid_transport(self.session.aid_pact, pre_verdict=pre_verdict)

    def _review_aid_signing(self, pact):
        """外部聚落签约审核：信誉 + 会签医护资质共同决定通过率。返回是否通过。"""
        rep = self.session.reputation or TRADE_INITIAL_REPUTATION
        chance = AID_SIGN_BASE + rep / (AID_SIGN_REP_DIV * 100.0)
        if self._signer_qualified(pact):
            chance += AID_SIGN_MEDIC_BONUS
        if self.rand.random() >= chance:
            detail = self._refund_aid_escrow(pact, ratio=1.0, label="全额退还")
            rep_now = self._add_reputation(AID_REP_REJECT)
            self._log(
                "aid", f"签约被拒·{pact['partner_name']}",
                (detail + "；" if detail else "")
                + f"对方未通过本次联盟签约，托管物资已退回，信誉 {AID_REP_REJECT:+d}（现 {rep_now}）。",
                decision="签约审核驳回",
            )
            self._remember_credential(
                PENDING_AID_INCIDENT, detail or "签约被拒",
                action=self._AID_ACT_SETTLE, host_token=pact.get("token"),
            )
            self.session.aid_pact = None
            return False
        pact["status"] = AID_ESCORTING
        self.session.aid_pact = dict(pact)
        names = "、".join(r.name for r in self._aid_escorts(pact) if r.alive)
        self._log(
            "aid", f"签约通过·{pact['partner_name']}",
            f"{pact['partner_name']} 签署联盟援助协议，{names} 押运医援物资出发，"
            "抵达后须通过疫区检疫关卡。",
            decision="外部聚落签约",
        )
        return True

    def _tick_aid_transport(self, pact, pre_verdict=None):
        """在途押运一天：累计行程、抵达日先过押运审核（检疫关卡）或触发途中事件。"""
        alive_escorts = [r for r in self._aid_escorts(pact) if r.alive]
        if not alive_escorts:
            self._fail_aid_pact(pact, "援助押运队全员失联", forced_verdict=pre_verdict)
            return None
        pact["travel_days"] += 1
        if pact["travel_days"] >= pact["eta"]:
            # 抵达日：先过疫区检疫关卡（押运审核），通过才交付；不再触发途中事件
            if not self._review_aid_gate(pact, forced_verdict=pre_verdict):
                return None
            self._deliver_aid_pact(self.session.aid_pact,
                                   reason="援助押运队抵达疫区并通过检疫",
                                   forced_verdict=pre_verdict)
            return None
        if self.rand.random() <= AID_INCIDENT_CHANCE:
            event = self.rand.choice(AID_INCIDENTS)
            incident = self._build_aid_incident(event, pact)
            self._write_pending(PENDING_AID_INCIDENT, incident)
            return incident
        self.session.aid_pact = dict(pact)
        return None

    def _review_aid_gate(self, pact, forced_verdict=None):
        """抵达疫区的押运审核（检疫关卡）：信誉 + 随队医护决定通过率。

        未通过即判协议失败：托管全额带回、信誉扣减、医疗危机因疫区未获援
        而反扑小升。返回是否通过。
        """
        rep = self.session.reputation or TRADE_INITIAL_REPUTATION
        chance = AID_GATE_BASE + rep / 250.0
        if self._escort_has_medic(pact):
            chance += AID_GATE_MEDIC_BONUS
        if self.rand.random() < chance:
            names = "、".join(r.name for r in self._aid_escorts(pact) if r.alive)
            self._log(
                "aid", f"检疫通过·{pact['partner_name']}",
                f"{names} 通过疫区检疫关卡，医援物资获准交付。",
                decision="押运审核通过",
            )
            return True
        # 押运审核未过：当场收敛为失败（托管全额退回，不叠加在途货损）
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        parts = []
        refund = self._refund_aid_escrow(pact, ratio=1.0, label="未入关托管带回")
        if refund:
            parts.append(refund)
        rep_now = self._add_reputation(AID_REP_GATE_FAIL)
        crisis_now = self._add_medical_crisis(MEDICAL_CRISIS_GATE_FAIL)
        for r in self._aid_escorts(pact):
            if r.alive:
                r.morale = _clamp(r.morale - 6)
        parts.append(
            f"信誉 {AID_REP_GATE_FAIL:+d}（现 {rep_now}），"
            f"疫区未获援、医疗危机 +{MEDICAL_CRISIS_GATE_FAIL}（现 {crisis_now}）"
        )
        detail = "；".join(parts)
        self._log("aid", f"押运审核未过·{pact['partner_name']}",
                  f"援助押运队抵达疫区但未通过检疫关卡。{detail}", decision="押运审核驳回")
        self._remember_credential(
            PENDING_AID_INCIDENT, detail, action=self._AID_ACT_SETTLE,
            host_token=pact.get("token"),
        )
        # 终局统计：押运审核未过等同援助失败（医援未送达疫区），计入负贡献
        self._bump_mission_stats(aid_failed=1)
        self.session.aid_pact = None
        self._check_end(forced_verdict=verdict)
        return False

    def _build_aid_incident(self, event, pact):
        """构造援助途中事件快照（与危机/遭遇/贸易途中事件同一结构）。"""
        alive_escorts = [r for r in self._aid_escorts(pact) if r.alive]
        return self._build_pending_event(event, alive_escorts)

    # 联盟援助动作类型（档案级幂等凭据 last_aid 的 action 取值）
    _AID_ACT_INCIDENT = "incident"
    _AID_ACT_SETTLE = "settle"   # 协议收敛（交付成功 / 押运审核未过 / 失败回退 / 撤回）
    _AID_ACT_CANCEL = "cancel"

    def resolve_aid_incident(self, choice_key, token=None):
        """结算援助押运途中的事件抉择。

        效果键与贸易途中事件一致：cargo_loss（在途货物损耗比例）、delay
        （延误天数）、reputation（信誉变化）、health/morale（押运队员，
        single/all）、abort（弃货撤回：当场按失败回退收敛）。
        返回 (detail, replayed)。
        """
        self._ensure_running()
        replay = self._mission_credential_replay(
            PENDING_AID_INCIDENT, self._AID_ACT_INCIDENT, token, choice=choice_key
        )
        if replay[0] is not None:
            return replay
        converged = self._converged_settlement_replay(
            PENDING_AID_INCIDENT, self._AID_ACT_SETTLE, token, choice=choice_key
        )
        if converged[0] is not None:
            return converged
        pact, pending = self._locate_pending_decision(PENDING_AID_INCIDENT, token)
        event = self._pending_event_def(PENDING_AID_INCIDENT, pending)
        choice = self._event_choice(event, choice_key)
        effects = choice.get("effects", {})
        target = self._bound_target(pending, choice, self._aid_escorts(pact), "援助押运队员")
        detail_parts = []
        alive_escorts = [r for r in self._aid_escorts(pact) if r.alive]
        if effects.get("cargo_loss"):
            loss = float(effects["cargo_loss"])
            pact["cargo_ratio"] = round(max(0.0, pact.get("cargo_ratio", 1.0) * (1.0 - loss)), 3)
            detail_parts.append(f"货物损耗 {int(loss * 100)}%（残存 {int(pact['cargo_ratio'] * 100)}%）")
        if effects.get("delay"):
            d = int(effects["delay"])
            pact["eta"] += d
            detail_parts.append(f"行程延误 {d} 天")
        if effects.get("reputation"):
            rep = self._add_reputation(int(effects["reputation"]))
            detail_parts.append(f"信誉 {int(effects['reputation']):+d}（现 {rep}）")
        self._apply_stat_effects(effects, target, alive_escorts, "全体援助押运队员", detail_parts)
        if effects.get("abort"):
            detail_parts.append("弃货撤回")
        pact["casualties"] = self._sweep_casualties(
            alive_escorts, pact.get("casualties", [])
        )
        if "health" in effects:
            spec = effects["health"]
            affected = [target] if self._effect_scope(spec) == "single" else [
                r for r in alive_escorts if r.alive
            ]
            self._settle_health_aftermath(
                affected, infectious_event=event["key"] in MED_INFECTIOUS_AID_EVENTS,
                reason=f"援助途中·{event['title']}", force=True,
            )
        scope_zh = f"（目标：{target.name}）" if target else ""
        detail = "，".join(detail_parts) if detail_parts else "无显著变化"
        self._log("crisis", f"援助途中·{event['title']}",
                  f"选择「{choice['label']}」{scope_zh}：{detail}", decision=choice["label"])
        inc_token = pending.get("token")
        pact["pending_incident"] = None
        pact["incidents_resolved"] += 1
        self.session.aid_pact = dict(pact)
        self._remember_credential(
            PENDING_AID_INCIDENT, detail, action=self._AID_ACT_INCIDENT,
            token=inc_token, host_token=pact.get("token"), choice=choice["key"],
        )
        # 与贸易途中事件同一口径：事件结算后立即收敛，不留零货物/全员阵亡的队伍
        alive_after = [r for r in self._aid_escorts(pact) if r.alive]
        settle_reason = None
        if not alive_after:
            settle_reason = "援助押运队全员失联"
        elif pact["cargo_ratio"] <= 0:
            settle_reason = "医援物资全部损失，押运队空车返程"
        elif effects.get("abort"):
            settle_reason = "援助押运队弃货撤回"
        elif self._end_conditions_met():
            # 人口归零/全线枯竭等终局：强制安全交付后再收敛到 ended
            self._deliver_aid_pact(
                self.session.aid_pact, reason="终局已至，援助押运队返程", force_success=True,
            )
            rec = self._read_credential(PENDING_AID_INCIDENT)
            return_detail = rec.get("detail", "") if rec else ""
            detail = f"{detail}；协议结算：{return_detail}" if return_detail else detail
            return detail, False
        if settle_reason is not None:
            return_detail, _ = self._fail_aid_pact(
                self.session.aid_pact, settle_reason,
                inc_token=inc_token, inc_choice=choice["key"],
            )
            detail = f"{detail}；协议回退：{return_detail}"
            rec = self._read_credential(PENDING_AID_INCIDENT)
            rec["detail"] = detail
            self._write_credential(PENDING_AID_INCIDENT, rec)
            return detail, False
        return detail, False

    def reconcile_stale_aid(self, action, token=None, choice_key=None):
        """并发落败后核对联盟援助动作：同一次抉择/结算则安全回放，否则 409。"""
        if action == self._AID_ACT_INCIDENT:
            replay = self._mission_credential_replay(
                PENDING_AID_INCIDENT, action, token, choice=choice_key
            )
            if replay[0] is not None:
                return replay
            converged = self._converged_settlement_replay(
                PENDING_AID_INCIDENT, self._AID_ACT_SETTLE, token, choice=choice_key
            )
            if converged[0] is not None:
                return converged
        else:
            replay = self._mission_credential_replay(PENDING_AID_INCIDENT, action, token)
            if replay[0] is not None:
                return replay
        raise BunkerEngineConflict("联盟援助协议状态已被其他请求更新，请刷新后重试")

    def _aid_relief_care(self):
        """援助成功的医疗救治回写：对每名存活幸存者实施救治。

        只负责健康与病例康复；士气奖励由交付结算统一发放，避免一份援助提振两次。
        在堡病例与归队押运病例（离堡冻结中）都受益；已病亡者不复活。
        被治愈病例的健康抬过康复线即当场结案为康复。返回 (救治人数, 康复病例名)。
        """
        cases = self._cases()
        case_of = {c.get("resident_id"): c for c in cases
                   if c.get("status") in MED_ACTIVE_STATUSES}
        healed = 0
        recovered_names = []
        for r in self.session.residents:
            if not r.alive:
                continue
            r.health = _clamp(r.health + AID_RELIEF_HEAL)
            healed += 1
            c = case_of.get(r.id)
            if c is not None and r.health >= MED_RECOVER_HEALTH:
                c["status"] = MED_RECOVERED
                c["close_day"] = self.session.day
                recovered_names.append(r.name)
        if recovered_names:
            self._save_cases(cases)
            self._log(
                "medical", "联盟医援治愈",
                f"随援助物资抵达的医疗方案使 {('、'.join(recovered_names))} 康复结案。",
                decision="联盟援助救治",
            )
        return healed, recovered_names

    def _aid_outbreak(self):
        """援助失败的医疗危机回写：疫情反扑。

        已建救治中心：在堡最虚弱的健康居民立为传染病例（已有活跃病例者跳过）；
        未建中心：无病例体系承接，仅施加在堡健康损伤。
        """
        in_bunker = self._in_bunker_residents()
        if self._active_clinic() is not None:
            cases = self._cases()
            active = {c.get("resident_id") for c in cases
                      if c.get("status") in MED_ACTIVE_STATUSES}
            pool = [r for r in in_bunker if r.id not in active]
            if pool:
                victim = min(pool, key=lambda r: r.health)
                victim.health = _clamp(victim.health - AID_OUTBREAK_DAMAGE)
                victim.morale = _clamp(victim.morale - 6)
                self._open_case(
                    victim, infectious=True,
                    reason="联盟援助失败、疫区疫情反扑", kind="疫病",
                )
                self._log(
                    "medical", "疫情反扑",
                    f"援助未送达，{victim.name} 在疫情反扑中染疫（健康 -{AID_OUTBREAK_DAMAGE:g}）。",
                    decision="医疗危机激化",
                )
                return victim.name
        else:
            for r in in_bunker:
                r.health = _clamp(r.health - AID_OUTBREAK_DAMAGE / 2)
        return None

    def _deliver_aid_pact(self, pact, reason, forced_verdict=None, force_success=False):
        """交付结算（押运审核通过后）：医援回礼入库、全员救治、医疗危机缓解。

        forced_verdict 为推进开始前快照的终局裁决，优先于本方法内部快照，
        保证入库的回礼不会复活当日已成立的败局（与贸易交付同一口径）。
        """
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        ratio = pact.get("cargo_ratio", 1.0)
        parts = []
        # 医援物资已送抵疫区：在途损耗的残份随车带回；对方按实际送达比例回礼
        if ratio < 1.0:
            refund = self._refund_aid_escrow(pact, ratio=1.0 - ratio, label="未送达的医援物资带回")
            if refund:
                parts.append(refund)
        gain_parts = []
        for k, v in pact["cargo"].items():
            amt = round(v * ratio, 1)
            if amt > 0:
                self._add_resource(k, amt)
                gain_parts.append(f"{RESOURCE_ZH.get(k, k)} +{amt:g}")
        if gain_parts:
            parts.append("联盟回礼：" + "、".join(gain_parts))
        # 医疗救治回写：全员治疗、活跃病例康复
        healed, recovered_names = self._aid_relief_care()
        # 医疗危机缓解
        crisis_now = self._add_medical_crisis(-MEDICAL_CRISIS_RELIEF)
        rep_now = self._add_reputation(AID_REP_DELIVER)
        for r in self._in_bunker_residents():
            r.morale = _clamp(r.morale + 8)
        for r in self._aid_escorts(pact):
            if r.alive:
                r.morale = _clamp(r.morale + 10)
        parts.append(f"医援救治覆盖 {healed} 名幸存者，医疗危机 -{MEDICAL_CRISIS_RELIEF}（现 {crisis_now}）")
        if recovered_names:
            parts.append(f"康复结案：{'、'.join(recovered_names)}")
        parts.append(f"信誉 {AID_REP_DELIVER:+d}（现 {rep_now}），在堡全员士气 +8")
        detail = "；".join(parts)
        self._log("aid", f"援助交付·{pact['partner_name']}", f"{reason}。{detail}", decision="联盟援助交付")
        self._remember_credential(
            PENDING_AID_INCIDENT, detail, action=self._AID_ACT_SETTLE,
            host_token=pact.get("token"),
        )
        # 终局统计：仅成功交付计入贡献（撤约/签约被拒/逾期不进入本路径）
        self._bump_mission_stats(aid_delivered=1)
        self.session.aid_pact = None
        self._check_end(forced_verdict=verdict)
        return detail, False

    def _fail_aid_pact(self, pact, reason, inc_token=None, inc_choice=None, forced_verdict=None):
        """失败回退：未送出的托管医援物资退回、信誉重罚、医疗危机激化。

        inc_token 非空表示本次回退由某条途中事件抉择直接触发（弃货/全损/
        全员失联），回退凭据同时挂住该事件一次性 token，供连点/并发落败回放。
        返回 (detail, False)。
        """
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        ratio = pact.get("cargo_ratio", 1.0)
        parts = []
        refund = self._refund_aid_escrow(pact, ratio=ratio, label="未送出医援物资带回")
        if refund:
            parts.append(refund)
        # 医疗危机回写：疫情反扑（在堡最虚弱者染疫；无救治中心时仅健康损伤）
        victim = self._aid_outbreak()
        crisis_now = self._add_medical_crisis(MEDICAL_CRISIS_OUTBREAK)
        rep_now = self._add_reputation(AID_REP_FAIL)
        for r in self._aid_escorts(pact):
            if r.alive:
                r.morale = _clamp(r.morale - 8)
        dead = [r.name for r in self._aid_escorts(pact) if not r.alive]
        outbreak_txt = f"，{victim} 染疫" if victim else ""
        parts.append(
            f"医疗危机 +{MEDICAL_CRISIS_OUTBREAK}（现 {crisis_now}）{outbreak_txt}；"
            f"信誉 {AID_REP_FAIL:+d}（现 {rep_now}），押运队员士气 -8"
        )
        if dead:
            parts.append(f"殉职：{'、'.join(dead)}")
        detail = "；".join(parts)
        self._log("aid", f"援助失败·{pact['partner_name']}", f"{reason}。{detail}", decision="援助失败回退")
        self._remember_credential(
            PENDING_AID_INCIDENT, detail, action=self._AID_ACT_SETTLE,
            token=inc_token, host_token=pact.get("token"), choice=inc_choice,
        )
        # 终局统计：确认失败计入负贡献（撤约/签约被拒/逾期不进入本路径）
        self._bump_mission_stats(aid_failed=1)
        self.session.aid_pact = None
        self._check_end(forced_verdict=verdict)
        return detail, False


    # ---- 跨聚落难民安置 ----
    # 状态链（难民在堡内隔离舱安置，不派离堡队伍，不与探索/贸易/援助互斥）：
    #   外部聚落公告板每日发布难民安置申请（确定性轮换）
    #     ├─ 拒绝（reject）：申请关闭，信誉小挫
    #     └─ 批准（accept）：quarantine 隔离检疫开始
    #          每日推进：隔离配给（食物/水/电）→ 接触史确诊 → 确诊救治/恶化/病亡
    #          ├─ 检疫期满无人存活：failed，按病亡人数扣信誉，安置关闭
    #          └─ 检疫期满有幸存者：admitting 待接纳（暂停消耗）
    #               ├─ 接纳并分配岗位（admit）：每人结算信誉，未确诊疫病例登记/隔离，
    #               │                         弱智者登记普通病例，新居民加入地堡
    #               └─ 遣返（repatriate）：repatriated，信誉与士气受挫，安置关闭
    def refugee_board(self):
        """生成当日外部聚落的难民安置申请（确定性，无副作用）。

        以"日期"为种子：同一天内重复查看申请一致，跨天自动轮换；批准时引擎
        重新生成当日公告板并核对 application_id，过期（跨天/被轮换掉）无法批准。
        每份申请来自一个聚落，含 1-3 名难民（健康/士气/接触史/擅长岗位各异）。
        """
        import random
        rng = random.Random(f"bunker-refugee-board-day-{self.session.day}")
        partners = list(REFUGEE_PARTNERS)
        rng.shuffle(partners)
        surnames = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜")
        givens = list("伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英华玉萍红斌晨雪辉宁")
        applications = []
        for p in partners[:2]:
            n = rng.randint(1, REFUGEE_MAX_APPLICANTS)
            people = []
            for _ in range(n):
                # 难民健康普遍偏弱：多数人体弱，少数人已出现高热（确诊疫病）
                roll = rng.random()
                if roll < 0.20:
                    health = rng.randint(22, 34)      # 高热病患：直接确诊
                    exposed, infectious = True, True
                elif roll < 0.60:
                    health = rng.randint(38, 62)      # 体弱：可能带接触史
                    exposed, infectious = rng.random() < 0.5, False
                else:
                    health = rng.randint(63, 80)      # 基本健康
                    exposed, infectious = rng.random() < 0.3, False
                skill_roll = rng.random()
                if skill_roll < 0.08:
                    skill = "medic"
                elif skill_roll < 0.28:
                    skill = "engineer"
                elif skill_roll < 0.53:
                    skill = "farmer"
                else:
                    skill = "general"
                people.append({
                    "key": uuid.uuid4().hex[:8],
                    "name": rng.choice(surnames) + rng.choice(givens),
                    "health": float(health),
                    "morale": float(rng.randint(45, 70)),
                    "exposed": bool(exposed),          # 疫区接触史：检疫期可能确诊
                    "infectious": bool(infectious),    # 抵达时已确诊
                    "skill": skill,                    # 擅长岗位（接纳时预填）
                    "alive": True,
                })
            total = len(people)
            sick = sum(1 for x in people if x["infectious"] or x["exposed"])
            applications.append({
                "id": f"rf-{p['key']}-{self.session.day}",
                "partner": p["key"],
                "partner_name": p["name"],
                "count": total,
                "people": people,
                "daily_cost": {k: round(v * total, 1) for k, v in REFUGEE_DAILY_COST.items()},
                "quarantine_days": REFUGEE_QUARANTINE_DAYS,
                "hint": (
                    f"{p['name']} 有 {total} 名流民请求安置，其中 {sick} 人有疫区接触史"
                    f"或已出现发热症状；须隔离检疫 {REFUGEE_QUARANTINE_DAYS} 天，"
                    f"每人每日消耗隔离配给。"
                ),
            })
        return applications

    def _find_refugee_application(self, application_id):
        return next((a for a in self.refugee_board() if a["id"] == application_id), None)

    def refugee_stats(self):
        """难民安置累计统计（缺列旧档案从零开始）。"""
        stats = self.session.refugee_stats or {}
        return {
            "applications": int(stats.get("applications", 0)),
            "accepted": int(stats.get("accepted", 0)),
            "admitted": int(stats.get("admitted", 0)),
            "rejected": int(stats.get("rejected", 0)),
            "repatriated": int(stats.get("repatriated", 0)),
            "quarantine_dead": int(stats.get("quarantine_dead", 0)),
        }

    def _bump_refugee_stats(self, **deltas):
        from sqlalchemy.orm.attributes import flag_modified
        stats = dict(self.session.refugee_stats or {})
        for k, v in deltas.items():
            stats[k] = int(stats.get(k, 0)) + v
        self.session.refugee_stats = stats
        flag_modified(self.session, "refugee_stats")

    # ---- 贸易/援助终局累计统计（结局贡献评分用）----
    MISSION_STAT_KEYS = (
        "trade_delivered", "trade_failed", "aid_delivered", "aid_failed",
    )

    def mission_stats(self):
        """贸易/援助终局累计统计（缺列旧档案从零开始）。

        只统计真正收敛的终局：交付成功 / 确认失败。撤约、撤单、审核被拒、
        签约逾期等未形成援助结果的动作不计入。
        """
        stats = self.session.mission_stats or {}
        return {k: int(stats.get(k, 0)) for k in self.MISSION_STAT_KEYS}

    def _bump_mission_stats(self, **deltas):
        """累加终局统计。仅在订单/协议的终局收敛路径调用一次，
        收敛本身由档案级幂等凭据保护（连点/并发落败安全回放），
        故统计随终局幂等，同一条订单/协议不会重复计分。"""
        from sqlalchemy.orm.attributes import flag_modified
        stats = dict(self.session.mission_stats or {})
        for k, v in deltas.items():
            if k not in self.MISSION_STAT_KEYS:
                continue
            stats[k] = int(stats.get(k, 0)) + v
        self.session.mission_stats = stats
        flag_modified(self.session, "mission_stats")

    def refugee_summary(self):
        """难民安置面板概览（无副作用）：当前安置进度、在检/待接纳人数与累计统计。"""
        intake = self.session.refugee_intake
        summary = {
            "active": intake is not None,
            "status": intake.get("status") if intake else None,
            "partner_name": intake.get("partner_name") if intake else None,
            "quarantine_days": REFUGEE_QUARANTINE_DAYS,
            "elapsed": intake.get("elapsed", 0) if intake else 0,
            "quarantining": 0,
            "admitting": 0,
            "dead": intake.get("dead", []) if intake else [],
            "daily_cost": {},
            "stats": self.refugee_stats(),
        }
        if intake:
            alive = [p for p in intake.get("people", []) if p.get("alive")]
            if intake.get("status") == REFUGEE_QUARANTINE:
                summary["quarantining"] = len(alive)
                summary["daily_cost"] = {
                    k: round(v * len(alive), 1) for k, v in REFUGEE_DAILY_COST.items()
                }
            else:
                summary["admitting"] = len(alive)
        return summary

    # 难民安置动作类型（档案级幂等凭据 last_refugee 的 action 取值）
    _REF_ACT_ACCEPT = "accept"
    _REF_ACT_REJECT = "reject"
    _REF_ACT_ADMIT = "admit"
    _REF_ACT_REPATRIATE = "repatriate"
    _REF_ACT_SETTLE = "settle"   # 安置自动收敛（检疫失败）

    def _refugee_replay(self, action, token=None, application_id=None):
        """命中央档级幂等凭据则返回 (detail, True)，否则 (None, False)。

        token（接纳/遣返绑定安置 token）或 application_id（批准/拒绝绑定当日
        申请）任一非空都必须与凭据逐字相等，避免把 A 申请的结果误回放给 B 申请。
        """
        rec = self.session.last_refugee
        if not rec or rec.get("action") != action:
            return None, False
        if token and rec.get("token") and rec.get("token") != token:
            return None, False
        if application_id is not None and rec.get("application_id") != application_id:
            return None, False
        return rec.get("detail", ""), True

    def _remember_refugee(self, detail, action, token=None, application_id=None):
        rec = {
            "action": action, "token": token,
            "day": self.session.day, "detail": detail,
        }
        if application_id is not None:
            rec["application_id"] = application_id
        self.session.last_refugee = rec

    def accept_refugees(self, application_id):
        """地堡审核通过外部聚落的安置申请：建立隔离检疫安置。"""
        self._require_daily_phase("批准难民安置申请")
        replay = self._refugee_replay(
            self._REF_ACT_ACCEPT, application_id=application_id
        )
        if replay[0] is not None:
            return replay
        if self.session.refugee_intake:
            raise BunkerEngineError("已有一批难民正在安置中，无法同时接收新申请")
        app = self._find_refugee_application(application_id)
        if app is None:
            raise BunkerEngineError("安置申请已过期或不存在（公告板每日轮换），请重新查看")
        alive_people = [p for p in app["people"] if p.get("alive", True)]
        if not alive_people:
            raise BunkerEngineError("该批难民已无人存活，无法接收")
        intake = {
            "token": uuid.uuid4().hex,
            "application_id": app["id"],
            "partner": app["partner"],
            "partner_name": app["partner_name"],
            "status": REFUGEE_QUARANTINE,
            "applied_day": self.session.day,
            "elapsed": 0,
            "people": [dict(p) for p in alive_people],
            "dead": [],
        }
        self.session.refugee_intake = dict(intake)
        self._bump_refugee_stats(applications=1, accepted=len(alive_people))
        names = "、".join(p["name"] for p in alive_people)
        self._log(
            "refugee", f"难民安置·{app['partner_name']}",
            f"地堡审核通过 {app['partner_name']} 的安置申请，{names}（共 {len(alive_people)} 人）"
            f"进入隔离舱，开始 {REFUGEE_QUARANTINE_DAYS} 天检疫。",
            decision="批准安置",
        )
        detail = f"{len(alive_people)} 名难民进入隔离检疫"
        self._remember_refugee(
            detail, self._REF_ACT_ACCEPT, token=intake["token"], application_id=app["id"]
        )
        return detail, False

    def reject_refugees(self, application_id):
        """地堡拒绝外部聚落的安置申请：不消耗物资，信誉小挫。"""
        self._require_daily_phase("拒绝难民安置申请")
        replay = self._refugee_replay(
            self._REF_ACT_REJECT, application_id=application_id
        )
        if replay[0] is not None:
            return replay
        app = self._find_refugee_application(application_id)
        if app is None:
            raise BunkerEngineError("安置申请已过期或不存在（公告板每日轮换），请重新查看")
        if self.session.refugee_intake:
            raise BunkerEngineError("已有一批难民正在安置中，无法处理新申请")
        rep_now = self._add_reputation(REFUGEE_REP_REJECT)
        self._bump_refugee_stats(applications=1, rejected=app["count"])
        self._log(
            "refugee", f"拒绝安置·{app['partner_name']}",
            f"地堡回绝了 {app['partner_name']} 的 {app['count']} 人安置申请，"
            f"信誉 {REFUGEE_REP_REJECT:+d}（现 {rep_now}）。",
            decision="拒绝安置",
        )
        detail = f"拒绝 {app['count']} 名难民入境，信誉 {REFUGEE_REP_REJECT:+d}（现 {rep_now}）"
        # 拒绝不产生持续宿主：仍写幂等凭据（绑定当日申请 id），连点/并发落败按同一结果回放
        self._remember_refugee(detail, self._REF_ACT_REJECT, application_id=app["id"])
        return detail, False

    def _apply_refugee_tick(self, pre_verdict=None):
        """每日检疫结算。admitting 阶段难民等待接纳，不再消耗物资。"""
        intake = self.session.refugee_intake
        if not intake or intake.get("status") != REFUGEE_QUARANTINE:
            return
        # 终局日（抵达目标日/全线枯竭已锁定）不再检疫消耗：随 _finish 清空安置
        if pre_verdict is not None:
            return
        alive = [p for p in intake["people"] if p.get("alive")]
        if not alive:
            self._fail_refugee_intake(intake, reason="隔离检疫开始前难民已无人存活")
            return
        # 1) 隔离配给：任一物资不足则当日断供，全员健康/士气受挫
        n = len(alive)
        need = {k: round(v * n, 1) for k, v in REFUGEE_DAILY_COST.items()}
        res = self.get_resources()
        rationed = all(res.get(k, 0) >= v for k, v in need.items())
        if rationed:
            for k, v in need.items():
                self._add_resource(k, -v)
        else:
            for p in alive:
                p["health"] = _clamp(p["health"] - REFUGEE_SHORTAGE_HEALTH)
                p["morale"] = _clamp(p["morale"] - REFUGEE_SHORTAGE_MORALE)

        # 2) 医疗筛查：确诊者救治/恶化，接触史者掷确诊，体弱者缓慢恢复
        clinic = self._active_clinic()
        medics = self.medic_count()
        newly_diagnosed = []
        parts = []
        for p in alive:
            if p["infectious"]:
                if clinic is not None:
                    heal = REFUGEE_INFECT_HEAL + REFUGEE_INFECT_MEDIC_HEAL * medics
                    p["health"] = _clamp(p["health"] + heal)
                    if p["health"] >= REFUGEE_NEGATIVE_HEALTH:
                        p["infectious"] = False
                        p["exposed"] = False
                        parts.append(f"{p['name']} 检疫康复")
                else:
                    p["health"] = _clamp(p["health"] - REFUGEE_INFECT_NOCLINIC)
            elif p["exposed"]:
                chance = max(
                    REFUGEE_EXPOSE_MIN_CHANCE,
                    REFUGEE_EXPOSE_CHANCE - REFUGEE_EXPOSE_MEDIC_REDUCE * medics,
                )
                if self.rand.random() < chance:
                    p["infectious"] = True
                    p["health"] = _clamp(p["health"] - REFUGEE_EXPOSE_DAMAGE)
                    newly_diagnosed.append(p["name"])
            elif p["health"] < MED_AUTO_REGISTER_HEALTH and clinic is not None:
                # 体弱但未接触疫区：医疗筛查帮助其缓慢恢复
                p["health"] = _clamp(p["health"] + REFUGEE_WOUNDED_HEAL)
            # 检疫期士气向基准 60 缓慢恢复
            if p["morale"] < 60:
                p["morale"] = _clamp(p["morale"] + REFUGEE_MORALE_DRIFT)

        # 3) 病亡收敛
        dead_names = []
        for p in alive:
            if p["health"] <= 0:
                p["alive"] = False
                p["health"] = 0
                dead_names.append(p["name"])
                intake.setdefault("dead", []).append({"name": p["name"], "day": self.session.day})
        for name in newly_diagnosed:
            self._log("medical", "难民确诊", f"隔离筛查中 {name} 确诊疫病（健康 -{REFUGEE_EXPOSE_DAMAGE:g}）。",
                      decision="隔离检疫")
        for name in dead_names:
            self._log("refugee", "难民检疫病亡", f"{name} 在隔离检疫中病亡。", decision="检疫病亡")

        intake["elapsed"] = intake.get("elapsed", 0) + 1
        self.session.refugee_intake = dict(intake)

        survivors = [p for p in intake["people"] if p.get("alive")]
        if not survivors:
            self._fail_refugee_intake(
                self.session.refugee_intake, reason="隔离检疫期间难民全部病亡"
            )
            return
        if intake["elapsed"] >= REFUGEE_QUARANTINE_DAYS:
            intake = self.session.refugee_intake
            intake["status"] = REFUGEE_ADMITTING
            self.session.refugee_intake = dict(intake)
            sick_n = sum(1 for p in survivors if p["infectious"])
            self._log(
                "refugee", "检疫期满",
                f"{intake['partner_name']} 的 {len(survivors)} 名难民检疫期满"
                + (f"，其中 {sick_n} 人仍为确诊疫病例须隔离收治" if sick_n else "，无人确诊疫病")
                + "，等待接纳与岗位分配。",
                decision="检疫结束",
            )
            return
        # 检疫中日志（聚合一行）
        tick_parts = []
        cost_txt = "、".join(f"{RESOURCE_ZH[k]} -{v:g}" for k, v in need.items())
        tick_parts.append(f"配给 {n} 人（{cost_txt}）" if rationed else f"物资不足，{n} 人断供")
        if newly_diagnosed:
            tick_parts.append(f"确诊：{'、'.join(newly_diagnosed)}")
        if dead_names:
            tick_parts.append(f"病亡：{'、'.join(dead_names)}")
        if parts:
            tick_parts.append("、".join(parts))
        self._log("refugee", f"第{self.session.day}天 · 隔离检疫",
                  "，".join(tick_parts), decision="例行检疫")

    def _fail_refugee_intake(self, intake, reason):
        """检疫失败（无幸存者）：按病亡人数扣信誉并清空安置。返回 (detail, False)。"""
        verdict = self._end_verdict()
        dead = intake.get("dead", [])
        penalty = REFUGEE_REP_DEATH * max(1, len(dead))
        rep_now = self._add_reputation(penalty)
        self._bump_refugee_stats(quarantine_dead=len(dead))
        detail = f"{reason}，信誉 {penalty:+d}（现 {rep_now}）"
        self._log("refugee", "难民安置失败", f"{detail}。", decision="安置失败")
        self._remember_refugee(detail, self._REF_ACT_SETTLE, token=intake.get("token"))
        self.session.refugee_intake = None
        self._check_end(forced_verdict=verdict)
        return detail, False

    def admit_refugees(self, assignments=None, token=None):
        """接纳检疫期满的难民并分配岗位：结算信誉、联动医疗病例、新居民加入地堡。

        assignments 为 {难民 key: 岗位}；缺省按难民擅长岗位（skill）分配。
        未确诊的确诊疫病例须有救治中心床位承接（立即隔离，床位不足则登记待收治）；
        体弱（低于巡检登记线）的非疫病例登记普通病例。无救治中心时确诊者
        不建档（病例体系无承接），仅按当前健康加入。
        """
        self._require_daily_phase("接纳难民")
        intake = self.session.refugee_intake
        if not intake:
            replay = self._refugee_replay(self._REF_ACT_ADMIT, token=token)
            if replay[0] is not None:
                return replay
            # 安置已被并发的接纳/遣返清除：携带 token 的请求按状态过期处理（409），
            # HTTP 层据此走 reconcile 安全回放；无凭据请求按"无待接纳难民"（400）
            if token:
                raise BunkerEngineConflict("难民安置状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有待接纳的难民")
        if token is not None and intake.get("token") and token != intake["token"]:
            raise BunkerEngineConflict("难民安置状态已过期，请刷新后重试")
        replay = self._refugee_replay(self._REF_ACT_ADMIT, token=intake.get("token"))
        if replay[0] is not None:
            return replay
        if intake.get("status") != REFUGEE_ADMITTING:
            raise BunkerEngineError("难民仍在隔离检疫中，检疫期满后才能接纳")
        assignments = assignments or {}
        alive = [p for p in intake.get("people", []) if p.get("alive")]
        if not alive:
            # 兜底：无幸存者直接按失败收敛，不留僵尸安置
            return self._fail_refugee_intake(intake, reason="已无存活难民可接纳")
        for p in alive:
            job = assignments.get(p["key"])
            if job is None:
                job = p.get("skill", "general")
            if job not in JOB_EFFICIENCY:
                raise BunkerEngineError(f"{p['name']} 的分配岗位未知：{job}")
            p["assigned_job"] = job

        # 接纳前快照终局裁决（与贸易/援助交付同一口径）
        verdict = self._end_verdict()
        clinic = self._active_clinic()
        cases = self._cases()
        admitted_names = []
        admitted_infectious = []
        admitted_weak = []
        # 先创建居民并 flush 取得 id，再按床位余量决定疫病例隔离/登记
        new_residents = []
        for p in alive:
            r = Resident(
                session_id=self.session.id,
                name=p["name"],
                job=p["assigned_job"],
                health=_clamp(p["health"]),
                morale=_clamp(p["morale"]),
                alive=1,
                joined_day=self.session.day,
            )
            self.session.residents.append(r)
            self.db.add(r)
            new_residents.append((p, r))
        self.db.flush()  # 取得新居民 id
        # 病例承接（病例簿在接纳时一次性写入）：确诊→优先隔离，床位不足转登记；
        # 体弱非疫→登记普通病例。无救治中心则无病例体系承接
        away = self._away_resident_ids()
        for p, r in new_residents:
            if clinic is None:
                continue
            if p["infectious"]:
                # 已使用床位按当前病例簿实时计算（只计在堡病例，口径同 med_beds_used），
                # 本批新加入的隔离病例逐人占用
                used = sum(
                    1 for c in cases
                    if c.get("status") in MED_CARE_STATUSES
                    and c.get("resident_id") not in away
                )
                status = MED_ISOLATED if used < self.med_bed_capacity() else MED_REGISTERED
                case = self._build_case_dict(
                    r, infectious=True, reason="跨聚落难民安置·检疫未转阴",
                    kind="疫病", status=status,
                )
                cases.append(case)
                admitted_infectious.append(r.name)
            elif r.health < MED_AUTO_REGISTER_HEALTH:
                cases.append(self._build_case_dict(
                    r, infectious=False, reason="跨聚落难民安置·体弱", kind="伤病",
                ))
                admitted_weak.append(r.name)
        if clinic is not None:
            self._save_cases(cases)

        n = len(new_residents)
        self.session.survivors += n
        rep_gain = REFUGEE_REP_ADMIT * n
        rep_now = self._add_reputation(rep_gain)
        for r in self.session.residents:
            if r.alive:
                r.morale = _clamp(r.morale + REFUGEE_ADMIT_MORALE)
        self._bump_refugee_stats(admitted=n)
        admitted_names = [r.name for _, r in new_residents]
        parts = [
            f"{n} 名难民正式加入地堡：{'、'.join(admitted_names)}",
            f"信誉 {rep_gain:+d}（现 {rep_now}），全员士气 {REFUGEE_ADMIT_MORALE:+.0f}",
        ]
        if admitted_infectious:
            parts.append(f"疫病例：{'、'.join(admitted_infectious)}")
        if admitted_weak:
            parts.append(f"体弱登记：{'、'.join(admitted_weak)}")
        detail = "；".join(parts)
        self._log("refugee", f"接纳难民·{intake['partner_name']}", detail, decision="接纳并分配岗位")
        self._remember_refugee(detail, self._REF_ACT_ADMIT, token=intake.get("token"))
        self.session.refugee_intake = None
        self._check_end(forced_verdict=verdict)
        return detail, False

    def repatriate_refugees(self, token=None):
        """检疫期满后遣返全部待接纳难民：人道代价，信誉与在堡士气受挫。"""
        self._require_daily_phase("遣返难民")
        intake = self.session.refugee_intake
        if not intake:
            replay = self._refugee_replay(self._REF_ACT_REPATRIATE, token=token)
            if replay[0] is not None:
                return replay
            if token:
                raise BunkerEngineConflict("难民安置状态已变化，请刷新后重试")
            raise BunkerEngineError("当前没有待处置的难民")
        if token is not None and intake.get("token") and token != intake["token"]:
            raise BunkerEngineConflict("难民安置状态已过期，请刷新后重试")
        replay = self._refugee_replay(
            self._REF_ACT_REPATRIATE, token=intake.get("token")
        )
        if replay[0] is not None:
            return replay
        if intake.get("status") != REFUGEE_ADMITTING:
            raise BunkerEngineError("隔离检疫期间无法遣返，请待检疫结束后处置")
        alive = [p for p in intake.get("people", []) if p.get("alive")]
        names = "、".join(p["name"] for p in alive)
        rep_now = self._add_reputation(REFUGEE_REP_REPATRIATE)
        for r in self._in_bunker_residents():
            r.morale = _clamp(r.morale + REFUGEE_REPATRIATE_MORALE)
        self._bump_refugee_stats(repatriated=len(alive))
        detail = (
            f"遣返 {names}（共 {len(alive)} 人），信誉 {REFUGEE_REP_REPATRIATE:+d}"
            f"（现 {rep_now}），在堡全员士气 {REFUGEE_REPATRIATE_MORALE:+.0f}"
        )
        self._log("refugee", f"遣返难民·{intake['partner_name']}", detail, decision="遣返")
        self._remember_refugee(detail, self._REF_ACT_REPATRIATE, token=intake.get("token"))
        self.session.refugee_intake = None
        return detail, False

    def reconcile_stale_refugee(self, action, token=None):
        """并发落败后核对难民安置动作：同一次动作安全回放，否则 409。"""
        replay = self._refugee_replay(action, token=token)
        if replay[0] is not None:
            return replay
        # 接纳/遣返并发时落败方的安置已被对方动作（或检疫失败自动收敛）清除：
        # 只要档案级凭据仍挂着同一个安置 token，就回放那次处置明细，
        # 落败方同样拿到 200，绝不二次结算
        rec = self.session.last_refugee
        if token and rec and rec.get("token") == token and rec.get("action") in (
            self._REF_ACT_ADMIT, self._REF_ACT_REPATRIATE, self._REF_ACT_SETTLE,
        ):
            return rec.get("detail", ""), True
        raise BunkerEngineConflict("难民安置状态已被其他请求更新，请刷新后重试")

    # ---- 医疗救治中心 ----
    # 病例状态链：
    #   registered 登记：病例已建档但尚未收治（无床位/未安排），健康持续恶化；
    #                    传染病例未收治时每天可能在堡内扩散
    #     ├─ treating  治疗：占用救治床位，消耗物资，医护在岗时回复更快
    #     └─ isolated  隔离：消耗更高，但切断传染扩散；对传染病例是推荐处置
    #   recovered 康复（在治期间健康跨过康复线）/ deceased 病亡（健康归零）
    #   离堡成员（探索队/押运队）的病例随队伍冻结：不恶化、不扩散、不消耗床位
    def _cases(self):
        # 浅拷贝列表：元素仍是存档病例字典的同一引用，内部可原地修改病例状态；
        # 持久化时由 _save_cases 深拷贝后整体回写，保证 JSON 列追踪变更
        return list(self.session.medical_cases or [])

    def _save_cases(self, cases):
        # 深拷贝后整体回写：medical_cases 是 JSON 列，病例状态常以原地改字典方式
        # 变更（admit_case 等）。除赋一份全新 list+dict 外再 flag_modified，
        # 确保工作单元在跨请求/刷新场景下一定把病例簿刷入数据库
        from sqlalchemy.orm.attributes import flag_modified
        self.session.medical_cases = [dict(c) for c in cases]
        flag_modified(self.session, "medical_cases")

    def _resident_case_map(self):
        """居民编号 → 未终态病例（每名居民至多一份活跃病例）。"""
        return {c.get("resident_id"): c for c in self._cases()
                if c.get("status") in MED_ACTIVE_STATUSES}

    def _caring_resident_ids(self):
        """当前正在救治床位上（治疗/隔离）的居民编号，含离堡冻结者。"""
        return {c.get("resident_id") for c in self._cases()
                if c.get("status") in MED_CARE_STATUSES}

    def _has_active_case(self, resident_id):
        """该居民是否存在未终态病例（登记/治疗/隔离）。"""
        return resident_id in self._resident_case_map()

    def _active_clinic(self):
        """当前生效的医疗救治中心：取等级最高的一座（无则 None）。"""
        clinics = [f for f in self.session.facilities
                   if f.category == "clinic" and f.status == "active"]
        return max(clinics, key=lambda f: f.level, default=None)

    def medic_count(self):
        """当值医护（medic）人数：在堡、存活、本人未躺上病床。"""
        return self.job_count("medic")

    def med_bed_capacity(self):
        """救治床位上限：每级救治中心 2 张 + 每名当值医护 1 张；无中心则无床位。"""
        clinic = self._active_clinic()
        if clinic is None:
            return 0
        return MED_BEDS_PER_LEVEL * clinic.level + self.medic_count()

    def med_beds_used(self):
        """已占用床位：只计在堡病例（离堡冻结者不占地堡床位）。"""
        away = self._away_resident_ids()
        return sum(
            1 for c in self._cases()
            if c.get("status") in MED_CARE_STATUSES and c.get("resident_id") not in away
        )

    def med_summary(self):
        """医疗面板概览（无副作用）：中心等级、医护、床位与各状态病例数。"""
        cases = self._cases()
        counts = {s: 0 for s in (MED_REGISTERED, MED_TREATING, MED_ISOLATED,
                                 MED_RECOVERED, MED_DECEASED)}
        for c in cases:
            s = c.get("status")
            if s in counts:
                counts[s] += 1
        away = self._away_resident_ids()
        # 离堡冻结病例单独标注，便于前端提示"回堡后继续救治"
        frozen = sum(1 for c in cases
                     if c.get("status") in MED_ACTIVE_STATUSES and c.get("resident_id") in away)
        return {
            "has_clinic": self._active_clinic() is not None,
            "clinic_level": self._active_clinic().level if self._active_clinic() else 0,
            "medics": self.medic_count(),
            "bed_capacity": self.med_bed_capacity(),
            "beds_used": self.med_beds_used(),
            "registered": counts[MED_REGISTERED],
            "treating": counts[MED_TREATING],
            "isolated": counts[MED_ISOLATED],
            "recovered": counts[MED_RECOVERED],
            "deceased": counts[MED_DECEASED],
            "frozen_away": frozen,
            "total_registered": len(cases),
            # 地堡医疗危机表（高压阈值随接口下发，前端据此提示/置灰）
            "medical_crisis": self.medical_crisis(),
            "medical_crisis_high": MEDICAL_CRISIS_HIGH,
        }

    def register_case(self, resident_id, infectious=False, reason="手动登记"):
        """为居民登记新病例（仅每日阶段、已建救治中心、本人无活跃病例）。

        手动登记要求健康低于登记线；事件后的强制建档（force 路径）走
        _open_case，不受健康线限制。
        """
        self._require_daily_phase("登记病例")
        if self._active_clinic() is None:
            raise BunkerEngineError("尚未建造医疗救治中心，无法登记病例")
        r = self._find_in_bunker_resident(resident_id)
        if r.health >= MED_MANUAL_REGISTER_HEALTH:
            raise BunkerEngineError(f"{r.name} 健康状况良好（≥{MED_MANUAL_REGISTER_HEALTH:g}），无需登记")
        if r.id in self._away_resident_ids():
            raise BunkerEngineError(f"{r.name} 已离堡，回堡后再登记病例")
        if self._has_active_case(r.id):
            raise BunkerEngineError(f"{r.name} 已有在档病例，请勿重复登记")
        self._open_case(r, infectious=infectious, reason=reason, kind=("疫病" if infectious else "伤病"))
        self._log(
            "medical", "病例登记",
            f"{r.name} 因{reason}登记为{('传染病例，需尽快隔离' if infectious else '伤病病例')}，等待收治。",
            decision="登记病例",
        )

    def admit_case(self, resident_id, mode):
        """把已登记病例收治上床：mode 为 treating（治疗）或 isolated（隔离）。

        已在治病例可在治疗/隔离之间切换（不重复占床）；床位不足时拒绝。
        离堡成员病例冻结，回堡前无法收治。
        """
        self._require_daily_phase("收治病例")
        if mode not in MED_CARE_STATUSES:
            raise BunkerEngineError("未知收治方式")
        if self._active_clinic() is None:
            raise BunkerEngineError("尚未建造医疗救治中心，无法收治")
        r = self._find_in_bunker_resident(resident_id)
        # 在同一份病例列表上找到目标并原地修改，避免"从 A 拷贝找字典、回写 B 拷贝"
        cases = self._cases()
        case = next((c for c in cases
                     if c.get("resident_id") == r.id and c.get("status") in MED_ACTIVE_STATUSES), None)
        if case is None:
            raise BunkerEngineError(f"{r.name} 没有待收治的病例")
        if r.id in self._away_resident_ids():
            raise BunkerEngineError(f"{r.name} 已离堡，病例随队伍冻结，回堡后再收治")
        if case["status"] != mode and case["status"] == MED_REGISTERED \
                and self.med_beds_used() >= self.med_bed_capacity():
            raise BunkerEngineError("救治床位已满，可升级医疗救治中心或增派医护")
        old_status = case["status"]
        case["status"] = mode
        case["admitted_day"] = self.session.day
        self._save_cases(cases)
        mode_zh = "隔离" if mode == MED_ISOLATED else "治疗"
        if old_status == MED_REGISTERED:
            self._log(
                "medical", "病例收治",
                f"{r.name} 转入{mode_zh}床位，医护开始按日救治。",
                decision=f"收治·{mode_zh}",
            )
        else:
            old_zh = "隔离" if old_status == MED_ISOLATED else "治疗"
            self._log(
                "medical", "调整救治方案",
                f"{r.name} 由{old_zh}改为{mode_zh}处置。",
                decision=f"转·{mode_zh}",
            )

    def _find_in_bunker_resident(self, resident_id):
        r = next((x for x in self.session.residents if x.id == resident_id), None)
        if not r or not r.alive:
            raise BunkerEngineError("居民不存在或已故")
        return r

    def _build_case_dict(self, resident, infectious, reason, kind="伤病",
                         status=MED_REGISTERED):
        """构造一份新病例字典（默认登记态）。"""
        return {
            "id": f"med-{uuid.uuid4().hex[:12]}",
            "resident_id": resident.id,
            "resident_name": resident.name,
            "status": status,
            "infectious": bool(infectious),
            "kind": kind,
            "reason": reason,
            "open_day": self.session.day,
            "admitted_day": self.session.day if status in MED_CARE_STATUSES else None,
            "close_day": None,
            "days_cared": 0,
        }

    def _open_case(self, resident, infectious, reason, kind="伤病",
                   status=MED_REGISTERED):
        """内部建档：不校验阶段/健康线/是否在堡（事件结算、每日巡检均可用）。

        要求本人无活跃病例。返回新病例字典并立即整体回写。
        """
        cases = self._cases()
        for c in cases:
            if c.get("resident_id") == resident.id and c.get("status") in MED_ACTIVE_STATUSES:
                return c  # 已有活跃病例：事件损伤叠加在既有病例上，不重复建档
        case = self._build_case_dict(resident, infectious, reason, kind, status=status)
        cases.append(case)
        self._save_cases(cases)
        return case

    def _settle_health_aftermath(self, affected, infectious_event, reason, force=False):
        """危机/遭遇/途中事件健康结算：把受伤幸存者登记进医疗救治中心。

        affected 为已施加健康效果后的存活居民列表；infectious_event 表示本次
        抉择具传染性（疫病/恶劣天气）。health 低于自动登记线即建档；force 时
        （被点名的传染目标）即使尚未跌破线也强制建档，避免漏网传播。
        未建造救治中心时无医疗体系承接，仅返回（健康损失已由事件本身生效）。
        """
        if self._active_clinic() is None:
            return
        cases = self._cases()
        for r in affected:
            if not r.alive:
                continue
            already = any(
                c.get("resident_id") == r.id and c.get("status") in MED_ACTIVE_STATUSES
                for c in cases
            )
            if already:
                continue
            forced = force and infectious_event
            if forced or r.health < MED_AUTO_REGISTER_HEALTH:
                self._open_case(
                    r, infectious=infectious_event, reason=reason,
                    kind=("疫病" if infectious_event else "伤病"),
                )

    def _apply_medical_tick(self):
        """每日医疗结算：在治病例消耗/回复、未收治病例恶化与扩散、康复病亡收敛、
        每日巡检自动登记。仅作用于在堡病例；离堡病例冻结，等回堡后续治。
        """
        # 在存档病例簿（同一批字典引用）上结算，过程中所有 _open_case 与
        # days_cared 等原地修改都落在同一份列表，最后整体深拷贝回写一次落库
        cases = self._cases()
        away = self._away_resident_ids()
        residents = {r.id: r for r in self.session.residents}

        # 1) 收敛已死亡居民的悬空活跃病例（匮乏死亡等）
        for c in list(cases):
            if c.get("status") in MED_ACTIVE_STATUSES:
                r = residents.get(c.get("resident_id"))
                if r is None or not r.alive:
                    c["status"] = MED_DECEASED
                    c["close_day"] = self.session.day

        # 2) 床位物资配给：在堡治疗/隔离病例按日消耗；任一物资不足则当日停诊
        in_care = [c for c in cases
                   if c.get("status") in MED_CARE_STATUSES and c.get("resident_id") not in away]
        need = {FOOD: 0.0, WATER: 0.0, POWER: 0.0}
        for c in in_care:
            for k, v in MED_CARE_COST[c["status"]].items():
                need[k] += v
        need = {k: round(v, 1) for k, v in need.items()}
        res = self.get_resources()
        rationed = bool(in_care) and all(res.get(k, 0) >= v for k, v in need.items())
        if in_care:
            if rationed:
                for k, v in need.items():
                    self._add_resource(k, -v)
            else:
                # 停诊：物资缺口下全体在治病例士气受挫，当日无救治回复
                for c in in_care:
                    r = residents.get(c.get("resident_id"))
                    if r:
                        r.morale = _clamp(r.morale - 5)

        clinic = self._active_clinic()
        level_scale = FACILITY_LEVEL_SCALE ** (clinic.level - 1) if clinic else 1.0
        medics = self.medic_count()
        recovered, deceased = [], []
        spreading = []

        # 3) 逐病例每日进展（在治回复 / 未收治恶化，离堡病例冻结）
        for c in list(cases):
            if c.get("status") not in MED_ACTIVE_STATUSES:
                continue
            r = residents.get(c.get("resident_id"))
            if r is None or not r.alive:
                continue
            if r.id in away:
                continue  # 离堡冻结：不恶化、不扩散、不消耗
            status = c["status"]
            if status in MED_CARE_STATUSES:
                if rationed:
                    heal = MED_HEAL[status] * level_scale + MED_MEDIC_HEAL[status] * medics
                    r.health = _clamp(r.health + heal)
                    r.morale = _clamp(r.morale + 0.5)
                    c["days_cared"] = c.get("days_cared", 0) + 1
                if r.health >= MED_RECOVER_HEALTH:
                    c["status"] = MED_RECOVERED
                    c["close_day"] = self.session.day
                    recovered.append(r.name)
                    continue
            else:
                # 登记未收治：伤病每日缓慢恶化，传染病恶化更快、且可能扩散
                r.health = _clamp(r.health - MED_DECAY[bool(c.get("infectious"))])
                r.morale = _clamp(r.morale - (3 if c.get("infectious") else 2))
                if c.get("infectious"):
                    spreading.append(c)
            if r.health <= 0:
                r.alive = 0
                r.health = 0
                self.session.survivors = max(0, self.session.survivors - 1)
                c["status"] = MED_DECEASED
                c["close_day"] = self.session.day
                deceased.append(r.name)

        # 康复/病亡日志（在统一回写前记录）
        for name in recovered:
            self._log("medical", "病例康复", f"{name} 康复出院，重返岗位。", decision="康复出院")
        for name in deceased:
            self._log("medical", "病例病亡", f"{name} 不治。", decision="病亡")

        # 4) 传染扩散：未收治/未隔离的传染病例掷传播（在治病例被隔离阻断）
        if spreading:
            chance = max(
                MED_SPREAD_MIN_CHANCE,
                MED_SPREAD_CHANCE - MED_SPREAD_MEDIC_REDUCE * medics,
            )
            active_map = {c.get("resident_id"): c for c in cases
                          if c.get("status") in MED_ACTIVE_STATUSES}
            in_bunker = self._in_bunker_residents()
            # 只结算当日已在册的传染源：巡检/本次扩散新建病例当日不再继续扩散，
            # 避免一次推进里链式传染全堡
            for c in list(spreading):
                if c.get("status") != MED_REGISTERED:
                    continue
                if self.rand.random() > chance:
                    continue
                pool = [r for r in in_bunker if r.id not in active_map and r.alive]
                if not pool:
                    break
                victim = self.rand.choice(pool)
                victim.health = _clamp(victim.health - MED_SPREAD_DAMAGE)
                victim.morale = _clamp(victim.morale - 4)
                if victim.health < MED_AUTO_REGISTER_HEALTH:
                    new_case = self._build_case_dict(
                        victim, infectious=True, reason="接触未隔离疫病", kind="疫病",
                    )
                    cases.append(new_case)
                    active_map[victim.id] = new_case
                self._log(
                    "medical", "疫病扩散",
                    f"未隔离病例 {c.get('resident_name')} 传染了 {victim.name}（健康 -{MED_SPREAD_DAMAGE:g}）。",
                    decision="疫情蔓延",
                )

        # 5) 每日巡检：在堡、健康跌破登记线且无活跃病例者自动建档（普通伤病）
        if clinic is not None:
            active_map = {c.get("resident_id"): c for c in cases
                          if c.get("status") in MED_ACTIVE_STATUSES}
            for r in self._in_bunker_residents():
                if r.id in away or r.id in active_map:
                    continue
                if r.health < MED_AUTO_REGISTER_HEALTH:
                    cases.append(self._build_case_dict(
                        r, infectious=False, reason="每日巡检发现", kind="伤病",
                    ))
                    active_map[r.id] = cases[-1]

        # 5.5) 医疗危机压力：在治床位/待收治病例每日抬高地堡医疗危机表，
        # 离堡冻结病例不施压；联盟援助的交付回写（缓解/激化）在协议结算处处理
        pressure = 0
        for c in cases:
            if c.get("status") not in MED_ACTIVE_STATUSES:
                continue
            if c.get("resident_id") in away:
                continue
            pressure += (MEDICAL_CRISIS_PER_WAITING
                         if c.get("status") == MED_REGISTERED
                         else MEDICAL_CRISIS_PER_CARE)
        if pressure:
            self._add_medical_crisis(pressure)

        # 6) 深拷贝整体回写，确保 JSON 列追踪到病例字典的原地变更
        self._save_cases(cases)

        # 7) 当日医疗日志（聚合一行，无变化则不刷屏）
        parts = []
        if in_care:
            if rationed:
                cost_txt = "、".join(f"{RESOURCE_ZH[k]} -{v:g}" for k, v in need.items() if v > 0)
                parts.append(f"在治 {len(in_care)} 例（消耗{cost_txt}）")
            else:
                parts.append(f"物资不足，{len(in_care)} 例停诊")
        if recovered:
            parts.append(f"康复：{'、'.join(recovered)}")
        if deceased:
            parts.append(f"病亡：{'、'.join(deceased)}")
        if pressure:
            parts.append(f"医疗危机 +{pressure}（现 {self.medical_crisis()}）")
        if parts:
            self._log("medical", f"第{self.session.day}天 · 医疗结算", "，".join(parts), decision="例行救治")

    def medical_crisis(self):
        """地堡医疗危机表 0-100（旧档案无该列时从 0 开始）。

        随在堡活跃病例每日累积，越过高压阈值（MEDICAL_CRISIS_HIGH）后在堡
        全员士气持续受挫；联盟援助成功交付大幅缓解、失败则激化。
        """
        return int(_clamp(
            self.session.medical_crisis or 0,
            MEDICAL_CRISIS_MIN, MEDICAL_CRISIS_MAX,
        ))

    def _add_medical_crisis(self, delta):
        val = int(_clamp(self.medical_crisis() + delta,
                         MEDICAL_CRISIS_MIN, MEDICAL_CRISIS_MAX))
        self.session.medical_crisis = val
        return val

    def _medical_stats(self):
        """危机后健康结算用的全周期救治统计（写入终局 outcome）。"""
        cases = self._cases()
        active = [c for c in cases if c.get("status") in MED_ACTIVE_STATUSES]
        recovered = sum(1 for c in cases if c.get("status") == MED_RECOVERED)
        deceased = sum(1 for c in cases if c.get("status") == MED_DECEASED)
        care_days = sum(c.get("days_cared", 0) for c in cases)
        return {
            "total": len(cases),
            "active": len(active),
            "recovered": recovered,
            "deceased": deceased,
            "care_days": care_days,
            "medical_crisis": self.medical_crisis(),
        }

    # ---- 扩建 ----
    def build_facility(self, category):
        self._require_daily_phase("建造设施")
        cost = FACILITY_COST[1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法建造")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f = Facility(
            session_id=self.session.id,
            name=FACILITY_ZH.get(category, category),
            category=category,
            level=1,
            status="active",
            built_day=self.session.day,
        )
        # 同时挂到关系集合：若 facilities 已被加载进身份映射，仅设外键 + flush
        # 不会让新设施出现在内存集合中，显式挂关系保证随后查询立即可见
        self.session.facilities.append(f)
        self.db.add(f)
        self.db.flush()  # 让新设施立即反映到 session.facilities 集合
        self._log("system", "设施扩建", f"建造了{FACILITY_ZH.get(category, category)}。", decision="扩建")
        return f

    def upgrade_facility(self, facility_id):
        self._require_daily_phase("升级设施")
        f = next((x for x in self.session.facilities if x.id == facility_id), None)
        if not f:
            raise BunkerEngineError("设施不存在")
        if f.level >= max(FACILITY_COST.keys()):
            raise BunkerEngineError("已达最高等级")
        cost = FACILITY_COST[f.level + 1]
        if not self._can_afford(cost):
            raise BunkerEngineError("资源不足，无法升级")
        for k, v in cost.items():
            self._add_resource(k, -v)
        f.level += 1
        self._log("system", "设施升级", f"{FACILITY_ZH.get(f.category, f.category)} 提升到 Lv.{f.level}。", decision="升级")
        return f

    def _can_afford(self, cost):
        res = self.get_resources()
        return all(res.get(k, 0) >= v for k, v in cost.items())

    # ---- 任务分配（重分配岗位）----
    def set_job(self, resident_id, job):
        self._require_daily_phase("调整岗位")
        if job not in JOB_EFFICIENCY:
            raise BunkerEngineError("未知岗位")
        r = next((x for x in self.session.residents if x.id == resident_id), None)
        if not r or not r.alive:
            raise BunkerEngineError("居民不存在或已故")
        if r.id in self._away_resident_ids():
            raise BunkerEngineError("离堡队伍中的居民无法调整岗位")
        # 联盟援助协议在谈/在途期间：医护负责人与押运队员岗位锁定——
        # 会签资质与押运检疫加成在发起时即绑定，不允许中途调岗架空协议
        pact = self.session.aid_pact
        if pact and r.job == "medic" and job != "medic" and (
            pact.get("signer_id") == r.id or r.id in pact.get("escorts", [])
        ):
            raise BunkerEngineError(f"{r.name} 承担着联盟援助协议职责，协议结束前不能调离医护岗位")
        r.job = job

    # ---- 结局判定 ----
    def _check_end(self, forced_verdict=None):
        """判定并落终局状态。

        forced_verdict 为状态变更（如返程战利品入库）前快照的裁决：一旦在
        变更前已满足终局（尤其是全线枯竭），即便变更后资源回升也照样收敛，
        保证终局判定单调、不被中途入库的物资“救回”。返回是否处于终局。
        """
        if self.session.status != "running":
            return True
        verdict = forced_verdict if forced_verdict is not None else self._end_verdict()
        if verdict is None:
            return False
        win, reason = verdict
        self._finish(win=win, reason=reason)
        return True

    def _finish(self, win, reason):
        # 幂等：终局只结算一次。重复调用（多路径收敛）直接返回，
        # 不重算分数、不重复写结局日志
        if self.session.status != "running":
            return
        self.session.status = "win" if win else "over"
        # 进入终局后不存在悬而未决的抉择/在外队伍/在途订单，状态机统一收敛到 ended
        self._clear_all_pending()
        # 在检/待接纳难民随终局关闭：终局只统计正式居民，不留僵尸安置
        self.session.refugee_intake = None
        alive = [r for r in self.session.residents if r.alive]
        # 计分：生存基础分（幸存者 * 天数 * 士气系数）
        #       + 医疗救治 / 难民安置 / 贸易援助三类外部贡献分（按终局成败）
        morale = self.avg_morale()
        base_score = int(self.session.survivors * self.session.day * (0.5 + morale / 200.0))
        # 危机后健康结算：全周期医疗救治履历随终局归档（登记/康复/病亡/累计救治日）
        med_stats = self._medical_stats()
        avg_health = round(sum(r.health for r in alive) / len(alive), 1) if alive else 0.0
        contributions = self._contribution_breakdown(med_stats)
        score = max(0, base_score + int(contributions["total_bonus"]))
        self.session.score = score
        self.session.outcome = {
            "win": win,
            "reason": reason,
            "survivors": len(alive),
            "day": self.session.day,
            "avg_health": avg_health,
            "score_base": base_score,
            "score": score,
            "contributions": contributions,
            "medical": med_stats,
            "refugee": self.refugee_stats(),
            "mission": self.mission_stats(),
        }
        if med_stats["total"]:
            self._log(
                "medical", "医疗救治总结算",
                f"全周期登记 {med_stats['total']} 例：康复 {med_stats['recovered']} 人、"
                f"病亡 {med_stats['deceased']} 人、累计救治 {med_stats['care_days']} 床日，"
                f"幸存者平均健康 {avg_health}。",
                decision="健康结算",
            )
        if contributions["total_bonus"]:
            self._log(
                "system", "外部贡献结算",
                f"医疗救治 {contributions['medical']['points']:+d} 分、"
                f"难民安置 {contributions['refugee']['points']:+d} 分、"
                f"贸易援助 {contributions['mission']['points']:+d} 分；"
                f"生存基础分 {base_score}，合计 {score} 分。",
                decision="贡献结算",
            )
        self._log("system", "游戏结束", reason, decision="结局")

    def _contribution_breakdown(self, med_stats):
        """结局贡献分项：医疗救治 / 难民安置 / 贸易援助（含联盟援助）。

        每项给出可在结局页展示的明细（数量 × 分值）与小计；贡献可负。
        医疗按全周期病例履历（康复/床日/病亡），难民按累计安置统计，
        贸易援助按终局收敛统计——撤销/驳回/逾期等未形成结果的动作不在统计内，
        每条订单/协议的终局只落一次，故撤销或失败都不会重复计分。
        """
        refugee = self.refugee_stats()
        mission = self.mission_stats()
        med_items = [
            {"label": f"康复 {med_stats['recovered']} 人",
             "count": med_stats["recovered"], "per": SCORE_MED_PER_RECOVERED,
             "points": med_stats["recovered"] * SCORE_MED_PER_RECOVERED},
            {"label": f"救治 {med_stats['care_days']} 床日",
             "count": med_stats["care_days"], "per": SCORE_MED_PER_CARE_DAY,
             "points": med_stats["care_days"] * SCORE_MED_PER_CARE_DAY},
            {"label": f"病亡 {med_stats['deceased']} 人",
             "count": med_stats["deceased"], "per": SCORE_MED_PER_DECEASED,
             "points": med_stats["deceased"] * SCORE_MED_PER_DECEASED},
        ]
        med_points = sum(item["points"] for item in med_items)
        refugee_items = [
            {"label": f"接纳 {refugee['admitted']} 人",
             "count": refugee["admitted"], "per": SCORE_REFUGEE_PER_ADMITTED,
             "points": refugee["admitted"] * SCORE_REFUGEE_PER_ADMITTED},
            {"label": f"检疫病亡 {refugee['quarantine_dead']} 人",
             "count": refugee["quarantine_dead"], "per": SCORE_REFUGEE_PER_DEAD,
             "points": refugee["quarantine_dead"] * SCORE_REFUGEE_PER_DEAD},
            {"label": f"拒绝 {refugee['rejected']} 人",
             "count": refugee["rejected"], "per": SCORE_REFUGEE_PER_REJECTED,
             "points": refugee["rejected"] * SCORE_REFUGEE_PER_REJECTED},
            {"label": f"遣返 {refugee['repatriated']} 人",
             "count": refugee["repatriated"], "per": SCORE_REFUGEE_PER_REPATRIATED,
             "points": refugee["repatriated"] * SCORE_REFUGEE_PER_REPATRIATED},
        ]
        refugee_points = sum(item["points"] for item in refugee_items)
        mission_items = [
            {"label": f"贸易援助成功 {mission['trade_delivered']} 笔",
             "count": mission["trade_delivered"], "per": SCORE_TRADE_PER_DELIVERED,
             "points": mission["trade_delivered"] * SCORE_TRADE_PER_DELIVERED},
            {"label": f"贸易援助失败 {mission['trade_failed']} 笔",
             "count": mission["trade_failed"], "per": SCORE_TRADE_PER_FAILED,
             "points": mission["trade_failed"] * SCORE_TRADE_PER_FAILED},
            {"label": f"联盟援助成功 {mission['aid_delivered']} 份",
             "count": mission["aid_delivered"], "per": SCORE_AID_PER_DELIVERED,
             "points": mission["aid_delivered"] * SCORE_AID_PER_DELIVERED},
            {"label": f"联盟援助失败 {mission['aid_failed']} 份",
             "count": mission["aid_failed"], "per": SCORE_AID_PER_FAILED,
             "points": mission["aid_failed"] * SCORE_AID_PER_FAILED},
        ]
        mission_points = sum(item["points"] for item in mission_items)
        return {
            "total_bonus": med_points + refugee_points + mission_points,
            "medical": {"points": med_points, "items": med_items},
            "refugee": {"points": refugee_points, "items": refugee_items},
            "mission": {"points": mission_points, "items": mission_items},
        }



RESOURCE_ZH = {"food": "食物", "water": "水源", "power": "电力", "oxygen": "氧气"}
FACILITY_ZH = {"farm": "穹顶菜园", "water": "净水器", "power": "发电机", "oxygen": "水培制氧", "med": "医疗舱", "clinic": "医疗救治中心", "storage": "仓储区"}

# 物资跨类别折算的相对价值：食物/水更稀缺昂贵，电力次之，氧气最便宜
TRADE_VALUE = {FOOD: 1.2, WATER: 1.1, POWER: 0.9, OXY: 0.8}

# 外部聚落：distance 为单程在途天数（审核通过后），favor 为其出产/偏好物资
TRADE_PARTNERS = [
    {"key": "ridge",   "name": "岭上镇",   "distance": 2, "favor": FOOD},
    {"key": "dock",    "name": "旧港码头", "distance": 3, "favor": WATER},
    {"key": "station", "name": "变电站营地", "distance": 2, "favor": POWER},
    {"key": "dome",    "name": "七号穹顶", "distance": 4, "favor": OXY},
]

# 地堡联盟成员聚落（医疗援助协议的合作方）：distance 为押运单程天数，
# favor 为其回礼出产物资。联盟援助须经三方签约 + 疫区检疫关卡，故路程略长
AID_PARTNERS = [
    {"key": "haven",   "name": "避风港医疗站", "distance": 3, "favor": OXY},
    {"key": "valley",  "name": "幽谷定居点",   "distance": 2, "favor": FOOD},
    {"key": "foundry", "name": "铸铁要塞",     "distance": 3, "favor": POWER},
    {"key": "spring",  "name": "甘泉营地",     "distance": 4, "favor": WATER},
]

# 申请难民安置的外部聚落（难民来源地，多为疫区周边的流民中转站）。
# 安置在堡内隔离舱完成，无押运路程：公告板每日轮换，批准即开始检疫
REFUGEE_PARTNERS = [
    {"key": "ridge",   "name": "岭北流民站"},
    {"key": "dock",    "name": "旧港难民营"},
    {"key": "station", "name": "变电站收容点"},
    {"key": "dome",    "name": "七号穹顶外缘"},
]


# ============ 危机事件池（决策树） ============
CRISIS_POOL = [
    {
        "key": "radstorm",
        "title": "辐射风暴来袭",
        "desc": "一场强辐射风暴正在逼近地堡。派工程师抢修屏蔽层，或让所有人避难并停电。",
        "choices": [
            {
                "key": "shield_repair",
                "label": "抢修屏蔽层",
                "hint": "消耗少量电力，成功则平安，失败有人员受伤",
                "effects": {"resources": {"power": -8}},
            },
            {
                "key": "shutdown",
                "label": "全员断电避难",
                "hint": "所有设施停摆一天，电力下降，无人员风险",
                "effects": {"resources": {"power": -15, "food": -5, "water": -4}},
            },
        ],
    },
    {
        "key": "mutiny",
        "title": "地堡内讧",
        "desc": "因食物分配不公，一部分人情绪失控，要求重新分配口粮。",
        "choices": [
            {
                "key": "double_ration",
                "label": "加倍发放食物",
                "hint": "士气+20，但食物储备大减",
                "effects": {"resources": {"food": -20}, "morale": 20},
            },
            {
                "key": "suppress",
                "label": "严令镇压",
                "hint": "食物不变，但士气大降",
                "effects": {"morale": -15},
            },
        ],
    },
    {
        "key": "leak",
        "title": "氧气泄漏",
        "desc": "水培舱密封圈老化，氧气正在泄漏。",
        "choices": [
            {
                "key": "emergency_repair",
                "label": "紧急封堵",
                "hint": "消耗食物与电力，防止气体外泄",
                "effects": {"resources": {"food": -6, "power": -6}},
            },
            {
                "key": "vent",
                "label": "先泄压再修",
                "hint": "氧气大降但更省资源",
                "effects": {"resources": {"oxygen": -20, "power": -3}},
            },
        ],
    },
    {
        "key": "sick",
        "title": "疫病袭来",
        "desc": "一名幸存者出现不明高热，可能是污染引发的疾病。",
        "choices": [
            {
                "key": "quarantine",
                "label": "隔离治疗",
                "hint": "该居民卸下工作，健康缓慢回复",
                "effects": {"resources": {"food": -4}, "health": {"value": -5, "target": "single"}},
            },
            {
                "key": "public_health",
                "label": "全员消毒",
                "hint": "消耗电力与水源消毒，保护大家",
                "effects": {"resources": {"power": -6, "water": -8}},
            },
        ],
    },
    {
        "key": "raid",
        "title": "盗匪袭扰",
        "desc": "地堡外传来敲击声，一伙流民试图破门而入抢夺物资。",
        "choices": [
            {
                "key": "defend",
                "label": "武装抵抗",
                "hint": "能耗物资，可能有人受伤，但守住粮食",
                "effects": {"resources": {"food": -2, "power": -4}, "health": {"value": -8, "target": "single"}},
            },
            {
                "key": "bribe",
                "label": "分粮和解",
                "hint": "交出部分食物换取平安",
                "effects": {"resources": {"food": -18}},
            },
        ],
    },
    {
        "key": "scavenge",
        "title": "发现物资舱",
        "desc": "侦察队在地堡深处发现一间废弃补给舱，但已部分损坏。",
        "choices": [
            {
                "key": "crack_open",
                "label": "强制开启",
                "hint": "可能获得大量补给，也可能毁坏",
                "effects": {"resources": {"food": 12, "water": 8}},
            },
            {
                "key": "careful",
                "label": "小心拆解",
                "hint": "稳定获得少量补给",
                "effects": {"resources": {"food": 6, "water": 5, "power": 3}},
            },
        ],
    },
    {
        "key": "blizzard",
        "title": "暴雪封门",
        "desc": "极寒暴雪掩盖了地堡入口，通风与采能都受影响。",
        "choices": [
            {
                "key": "burn_fuel",
                "label": "燃烧燃料保温",
                "hint": "消耗食物(燃料)维持温度",
                "effects": {"resources": {"food": -10}},
            },
            {
                "key": "huddle",
                "label": "集中避寒",
                "hint": "士气下降，但省下燃料",
                "effects": {"morale": -10},
            },
        ],
    },
    {
        "key": "caravan_help",
        "title": "路过商队求助",
        "desc": "一支外部商队在地堡附近抛锚，请求分享补给与维修零件。出手相助或许能换来口碑。",
        "choices": [
            {
                "key": "aid",
                "label": "慷慨接济",
                "hint": "消耗食物与电力，对外信誉与士气提升",
                "effects": {"resources": {"food": -10, "power": -6}, "reputation": 8, "morale": 6},
            },
            {
                "key": "trade_part",
                "label": "等价交换",
                "hint": "以物资换取对方的水源，信誉小升",
                "effects": {"resources": {"food": -6, "water": 8}, "reputation": 3},
            },
            {
                "key": "refuse",
                "label": "闭门不纳",
                "hint": "物资无损，但口碑与士气下降",
                "effects": {"reputation": -6, "morale": -5},
            },
        ],
    },
]


# ============ 探索队遭遇池（外出探索途中的遭遇决策树） ============
# 与地堡危机相互独立：探索队在外时，每日行军触发的是探索遭遇而非地堡危机。
# 效果键：
#   loot       —— 战利品，单独累计，返程时统一入库
#   supply_loss —— 从探索队自带物资中扣除
#   health/morale —— 队员健康/士气（single 仅作用于目标队员，all 作用于全体队员）
#   add_resident —— 有幸存者加入队伍
EXPEDITION_ENCOUNTERS = [
    {
        "key": "cache",
        "title": "废弃补给点",
        "desc": "探索队在一处废墟中发现半埋的废弃补给箱，外观尚可辨认。",
        "choices": [
            {
                "key": "search_carefully",
                "label": "仔细搜索",
                "hint": "耗时但可能获得更多物资",
                "effects": {"loot": {FOOD: 8, WATER: 6}},
            },
            {
                "key": "grab_quickly",
                "label": "快速搜刮",
                "hint": "安全但收获有限",
                "effects": {"loot": {FOOD: 4, WATER: 3}},
            },
        ],
    },
    {
        "key": "beast",
        "title": "异兽袭击",
        "desc": "一头变异巨兽从废墟中窜出，挡住了去路。",
        "choices": [
            {
                "key": "fight",
                "label": "武装驱赶",
                "hint": "可能有人受伤，但能保住物资并缴获战利品",
                "effects": {"health": {"value": -12, "target": "single"}, "loot": {FOOD: 5}},
            },
            {
                "key": "flee",
                "label": "绕道撤退",
                "hint": "损失部分物资，但无人受伤",
                "effects": {"supply_loss": {FOOD: 6, WATER: 4}, "morale": -5},
            },
        ],
    },
    {
        "key": "weather",
        "title": "恶劣天气",
        "desc": "辐射尘暴骤起，能见度极低，探索队被迫寻找掩体。",
        "choices": [
            {
                "key": "take_shelter",
                "label": "就地躲避",
                "hint": "消耗一日物资，士气下降",
                "effects": {"supply_loss": {FOOD: 3, WATER: 3}, "morale": -8},
            },
            {
                "key": "push_through",
                "label": "冒雨前进",
                "hint": "可能生病，但不耽误行程",
                "effects": {"health": -6, "morale": -3},
            },
        ],
    },
    {
        "key": "survivors",
        "title": "偶遇幸存者",
        "desc": "探索队遇到一群流离失所的幸存者，他们请求加入地堡。",
        "choices": [
            {
                "key": "accept",
                "label": "接纳加入",
                "hint": "新增一名幸存者，但消耗更多补给",
                "effects": {"add_resident": True, "supply_loss": {FOOD: 4, WATER: 3}},
            },
            {
                "key": "trade",
                "label": "交换物资",
                "hint": "用自带物资换取情报与小份补给",
                "effects": {"supply_loss": {FOOD: 3}, "loot": {POWER: 5}, "morale": 3},
            },
            {
                "key": "refuse",
                "label": "拒绝并离开",
                "hint": "保持警惕，安然离开",
                "effects": {"morale": -2},
            },
        ],
    },
    {
        "key": "ruins",
        "title": "废墟探索",
        "desc": "一座保存较完整的废弃建筑矗立在眼前，隐约有物资的气息。",
        "choices": [
            {
                "key": "deep_explore",
                "label": "深入探索",
                "hint": "高风险高回报，可能有重大伤亡",
                "effects": {"loot": {FOOD: 12, WATER: 8, POWER: 6}, "health": {"value": -15, "target": "single"}},
            },
            {
                "key": "outer_search",
                "label": "外围搜索",
                "hint": "安全获得少量物资",
                "effects": {"loot": {FOOD: 5, WATER: 4}},
            },
        ],
    },
    {
        "key": "lost",
        "title": "迷路",
        "desc": "复杂的废墟巷道让探索队迷失了方向，补给在不知不觉中消耗。",
        "choices": [
            {
                "key": "retrace",
                "label": "凭记忆折返",
                "hint": "消耗额外物资寻找归路",
                "effects": {"supply_loss": {FOOD: 5, WATER: 4}, "morale": -5},
            },
            {
                "key": "climb_high",
                "label": "登高辨认",
                "hint": "冒险登高，可能有意外收获",
                "effects": {"loot": {FOOD: 3}, "health": -4, "morale": 2},
            },
        ],
    },
    {
        "key": "airdrop",
        "title": "空投补给",
        "desc": "一架老旧的运输机残骸旁，探索队发现了未被开启的空投舱。",
        "choices": [
            {
                "key": "open_carefully",
                "label": "小心开启",
                "hint": "稳定获得补给",
                "effects": {"loot": {FOOD: 6, WATER: 6, POWER: 4, OXY: 4}},
            },
            {
                "key": "force_open",
                "label": "强行破开",
                "hint": "可能获得更多，也可能损坏物资",
                "effects": {"loot": {FOOD: 10, WATER: 8, POWER: 6}, "supply_loss": {OXY: 3}},
            },
        ],
    },
    {
        "key": "trap",
        "title": "陷阱",
        "desc": "探索队触发了一处老旧的捕兽夹，一名队员被夹住。",
        "choices": [
            {
                "key": "free_carefully",
                "label": "小心解救",
                "hint": "可能加重伤势，但能保全物资",
                "effects": {"health": {"value": -10, "target": "single"}},
            },
            {
                "key": "force_free",
                "label": "强行挣脱",
                "hint": "伤势更重，但不耽误行程",
                "effects": {"health": {"value": -18, "target": "single"}, "supply_loss": {FOOD: 2}},
            },
        ],
    },
]

# ============ 贸易押运途中事件池 ============
# 押运队（reviewing 通过后离堡）在每个在途日可能遭遇；事件挂起时进入 trade 阶段，
# 替代当日地堡危机。效果键（区别于地堡危机/探索遭遇）：
#   cargo_loss  —— 在途货物/托管残存比例乘法折损（0-1）
#   delay       —— 行程延误天数（eta 增加）
#   reputation  —— 地堡信誉变化
#   health/morale —— 押运队员健康/士气（single 仅目标，all 全体押运队员）
#   abort       —— 弃货撤回：效果结清后当场按失败回退收敛
TRADE_INCIDENTS = [
    {
        "key": "ambush",
        "title": "流民截道",
        "desc": "一伙武装流民在隘口设下路障，要求押运队留下货物买路。",
        "choices": [
            {
                "key": "fight_through",
                "label": "强行突围",
                "hint": "可能有人受伤、损失部分货物，但保住大部分订单",
                "effects": {"health": {"value": -14, "target": "single"}, "cargo_loss": 0.2, "morale": -4},
            },
            {
                "key": "pay_toll",
                "label": "缴纳货物买路",
                "hint": "折损三成货物，无人受伤",
                "effects": {"cargo_loss": 0.3, "morale": -3},
            },
            {
                "key": "abandon",
                "label": "弃货撤回",
                "hint": "放弃订单保命，剩余货物随车退回，订单判失败",
                "effects": {"abort": True, "cargo_loss": 0.0, "morale": -6},
            },
        ],
    },
    {
        "key": "duststorm",
        "title": "辐射沙暴",
        "desc": "灰黄色的辐射沙暴横扫荒原，能见度几乎为零。",
        "choices": [
            {
                "key": "shelter",
                "label": "就地掩蔽等待",
                "hint": "无人受伤，但行程延误 1 天且轻微货损",
                "effects": {"delay": 1, "cargo_loss": 0.1},
            },
            {
                "key": "push",
                "label": "冒沙暴赶路",
                "hint": "不延误，但队员可能病倒",
                "effects": {"health": -8, "morale": -3},
            },
        ],
    },
    {
        "key": "patrol",
        "title": "聚落巡逻队盘查",
        "desc": "一支陌生聚落的巡逻队持枪拦下押运车，怀疑你们是走私者。",
        "choices": [
            {
                "key": "papers",
                "label": "出示交易凭据交涉",
                "hint": "顺利放行，信誉在各聚落间传开",
                "effects": {"reputation": 4},
            },
            {
                "key": "bribe",
                "label": "分货打点",
                "hint": "交出两成货物换取放行",
                "effects": {"cargo_loss": 0.2},
            },
            {
                "key": "detour",
                "label": "绕开巡逻线",
                "hint": "行程延误，队员疲惫",
                "effects": {"delay": 2, "morale": -5},
            },
        ],
    },
    {
        "key": "breakdown",
        "title": "运输车故障",
        "desc": "老旧运输车在半路趴窝，货物还散落在辐射尘中。",
        "choices": [
            {
                "key": "repair",
                "label": "就地抢修",
                "hint": "消耗队员体力，大部分货物可救回",
                "effects": {"health": {"value": -8, "target": "single"}, "cargo_loss": 0.15},
            },
            {
                "key": "haul",
                "label": "人力拖拽前进",
                "hint": "全员疲惫、货损较多，但不延误",
                "effects": {"health": -5, "cargo_loss": 0.25},
            },
            {
                "key": "abandon",
                "label": "弃车撤回",
                "hint": "放弃订单，剩余货物撤回，订单判失败",
                "effects": {"abort": True, "cargo_loss": 0.0, "morale": -5},
            },
        ],
    },
]


# ============ 联盟援助押运途中事件池 ============
# 援助押运队（proposed 经外部聚落签约通过后离堡）在每个在途日可能遭遇；
# 事件挂起时进入 aid 阶段，替代当日地堡危机。效果键与贸易途中事件一致
# （cargo_loss / delay / reputation / health / morale / abort），
# 另含联盟特色的"检疫暴露"事件（具传染性，伤者由医疗救治中心承接为疫病例）。
AID_INCIDENTS = [
    {
        "key": "ambush",
        "title": "疫区流民围堵",
        "desc": "押运车临近疫区时被一群逃难流民围住，他们乞求车上的医援物资。",
        "choices": [
            {
                "key": "stand_guard",
                "label": "鸣枪护卫物资",
                "hint": "可能有人受伤、折损部分物资，但保住大部分医援",
                "effects": {"health": {"value": -12, "target": "single"}, "cargo_loss": 0.15, "morale": -4},
            },
            {
                "key": "share_rations",
                "label": "分出部分物资安抚",
                "hint": "折损两成医援，无人受伤，口碑流传",
                "effects": {"cargo_loss": 0.2, "reputation": 3, "morale": 2},
            },
            {
                "key": "abandon",
                "label": "弃货撤回",
                "hint": "放弃援助保命，剩余物资随车退回，协议判失败",
                "effects": {"abort": True, "cargo_loss": 0.0, "morale": -6},
            },
        ],
    },
    {
        "key": "quarantine",
        "title": "检疫关卡暴露",
        "desc": "穿越疫区外围缓冲区时，一名队员接触了污染耗材，出现发热征兆。",
        "choices": [
            {
                "key": "field_decon",
                "label": "随车医护现场洗消",
                "hint": "该队员仍可能染疫，但物资无损、行程不延误",
                "effects": {"health": {"value": -10, "target": "single"}},
            },
            {
                "key": "seal_supply",
                "label": "封存疑似污染货箱",
                "hint": "无人受伤，但销毁三成可能被污染的医援物资",
                "effects": {"cargo_loss": 0.3, "morale": -3},
            },
            {
                "key": "push_contaminated",
                "label": "不做处理继续赶路",
                "hint": "不损失物资、不延误，但队员可能重症",
                "effects": {"health": {"value": -20, "target": "single"}, "morale": -4},
            },
        ],
    },
    {
        "key": "duststorm",
        "title": "辐射尘暴",
        "desc": "灰黄色的辐射尘暴横扫荒原，能见度几乎为零。",
        "choices": [
            {
                "key": "shelter",
                "label": "就地掩蔽等待",
                "hint": "无人受伤，但行程延误 1 天且轻微货损",
                "effects": {"delay": 1, "cargo_loss": 0.1},
            },
            {
                "key": "push",
                "label": "冒尘暴赶路",
                "hint": "不延误，但队员可能病倒",
                "effects": {"health": -8, "morale": -3},
            },
        ],
    },
    {
        "key": "breakdown",
        "title": "押运车故障",
        "desc": "老旧押运车在半路趴窝，医援货箱还散落在辐射尘中。",
        "choices": [
            {
                "key": "repair",
                "label": "就地抢修",
                "hint": "消耗队员体力，大部分物资可救回",
                "effects": {"health": {"value": -8, "target": "single"}, "cargo_loss": 0.15},
            },
            {
                "key": "haul",
                "label": "人力拖拽前进",
                "hint": "全员疲惫、货损较多，但不延误",
                "effects": {"health": -5, "cargo_loss": 0.25},
            },
            {
                "key": "abandon",
                "label": "弃车撤回",
                "hint": "放弃援助，剩余物资撤回，协议判失败",
                "effects": {"abort": True, "cargo_loss": 0.0, "morale": -5},
            },
        ],
    },
]


# ============ 待决事件槽位注册表 ============
# 四类挂起抉择（地堡危机/探索遭遇/贸易途中事件/联盟援助途中事件）的统一声明：
# 引擎的"待决事件统一管线"（快照构造/落库恢复/幂等凭据/互斥状态机/终局收敛）
# 全部按本表驱动，新增一类待决事件只需在此登记一个槽位。
#
#   host_attr / host_status  宿主快照列与可挂起状态（None 表示宿主即档案本身）
#   field                    待决快照所在字段（档案列或宿主快照内的键）
#   pool                     事件池（结算时按快照 event 键回查定义）
#   credential_attr          档案级幂等凭据列（并发落败/连点安全回放）
#   phase                    挂起时状态机进入的阶段（互斥：锁定经营与推进）
#   host_token_key           幂等凭据中宿主 token 的键名（危机宿主即档案，无）
#   zh / host_zh / host_absent_zh  统一报错文案用词
_PENDING_SLOTS = {
    PENDING_CRISIS: {
        "host_attr": None,
        "host_status": None,
        "field": "pending_crisis",
        "pool": CRISIS_POOL,
        "credential_attr": "last_resolution",
        "phase": PHASE_CRISIS,
        "host_token_key": None,
        "zh": "危机",
        "host_zh": "档案",
        "host_absent_zh": "待处理的危机",
    },
    PENDING_ENCOUNTER: {
        "host_attr": "expedition",
        "host_status": "away",
        "field": "pending_encounter",
        "pool": EXPEDITION_ENCOUNTERS,
        "credential_attr": "last_expedition",
        "phase": PHASE_EXPEDITION,
        "host_token_key": "exp_token",
        "zh": "探索遭遇",
        "host_zh": "探索队",
        "host_absent_zh": "在外的探索队",
    },
    PENDING_INCIDENT: {
        "host_attr": "trade_order",
        "host_status": TRADE_TRANSPORTING,
        "field": "pending_incident",
        "pool": TRADE_INCIDENTS,
        "credential_attr": "last_trade",
        "phase": PHASE_TRADE,
        "host_token_key": "order_token",
        "zh": "途中事件",
        "host_zh": "贸易订单",
        "host_absent_zh": "在途的贸易订单",
    },
    PENDING_AID_INCIDENT: {
        "host_attr": "aid_pact",
        "host_status": AID_ESCORTING,
        "field": "pending_incident",
        "pool": AID_INCIDENTS,
        "credential_attr": "last_aid",
        "phase": PHASE_AID,
        "host_token_key": "pact_token",
        "zh": "援助途中事件",
        "host_zh": "联盟援助协议",
        "host_absent_zh": "在途的联盟援助协议",
    },
}
