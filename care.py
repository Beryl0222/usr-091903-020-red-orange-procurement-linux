"""老红橘古树管护兑现领域服务。

核心约束：

* 所有业务动作只追加事件（event ledger），查询由事件重放得到，
  因此已验收、已驳回、原始证据等事实不会被后续名单改写。
* 现场记录、农技意见、复核意见都作为不可变证据保存，只允许新增版本。
* 护树队离线巡查、多部门重复上报通过幂等键与同树同日同类规则合并为
  一次现场事件（incident）。
* 农技人员只能调整尚未裁决的未来里程碑；换地、责任人变更通过
  责任版本生效，不溯及既往。
* 附加金只注入“已通过且当季仍符合保护条件、且未拨付过”的里程碑，
  拨付单按当时合同版本快照金额与责任农户快照生成。
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# 常量与错误
# ---------------------------------------------------------------------------

ROLE_GUARDIAN = "guardian"        # 护树队
ROLE_AGRONOMIST = "agronomist"    # 农技员
ROLE_COOP = "coop"                # 合作社
ROLE_ENTERPRISE = "enterprise"    # 企业
ROLE_TOWN = "town"                # 镇里
ROLE_ADMIN = "admin"

PERM_ADJUST_MILESTONE = "milestone.adjust"  # 有权调整未来里程碑的农技人员

# 年度方案里程碑类型
STAGE_PRUNE = "PRUNE"                # 修剪
STAGE_DISEASE = "DISEASE"            # 防病
STAGE_REJUVENATE = "REJUVENATE"      # 低产树复壮
STAGE_TYPES = (STAGE_PRUNE, STAGE_DISEASE, STAGE_REJUVENATE)

# 证据类型（原始版本，一律不可改）
EV_PHOTO = "PHOTO"                  # 照片
EV_GPS = "GPS"                      # 定位
EV_DIAGNOSIS = "DIAGNOSIS"          # 病害诊断（农技员原始意见）
EV_REVIEW = "REVIEW"                # 复核意见
EV_OTHER = "OTHER"
EVIDENCE_KINDS = (EV_PHOTO, EV_GPS, EV_DIAGNOSIS, EV_REVIEW, EV_OTHER)

# 现场事件类型
INC_INSPECTION = "INSPECTION"            # 常规巡查
INC_CARE = "CARE"                        # 管护作业（修剪/复壮等）
INC_DISEASE = "DISEASE"                  # 病情上报
INC_TREE_DEATH = "TREE_DEATH"            # 树木死亡
INC_EXTREME_DAMAGE = "EXTREME_DAMAGE"    # 极端损伤
INC_BREACH = "BREACH"                    # 违反禁止采摘/接穗限制
INCIDENT_KINDS = (
    INC_INSPECTION,
    INC_CARE,
    INC_DISEASE,
    INC_TREE_DEATH,
    INC_EXTREME_DAMAGE,
    INC_BREACH,
)

# 保护状态裁决
STATUS_PROTECTED = "PROTECTED"
STATUS_DEAD = "DEAD"                          # 死亡，退出保护
STATUS_EXITED_DAMAGE = "EXITED_DAMAGE"        # 极端损伤后经评估退出
STATUS_UNDER_RECOVERY = "UNDER_RECOVERY"      # 极端损伤后留养观察

SUBMIT_PENDING = "PENDING"
SUBMIT_APPROVED = "APPROVED"
SUBMIT_REJECTED = "REJECTED"

# 年度结果四分类
YEAR_REAL_ALIVE = "REAL_ALIVE"        # 真实存活
YEAR_REJUVENATED = "REJUVENATED"      # 复壮
YEAR_REASONABLE_EXIT = "REASONABLE_EXIT"  # 合理退出
YEAR_NEGLECTED = "NEGLECTED"          # 漏管

# 合理退出的上报宽限：死亡/损伤发生后，跨越不超过一个年度仍属合理
REPORT_GRACE_SEASONS = 1


class DomainError(Exception):
    """业务规则错误，code 用于 HTTP 层映射状态码。"""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class NotFound(DomainError):
    def __init__(self, what: str):
        super().__init__("not_found", f"{what}不存在", 404)


class AuthzError(DomainError):
    def __init__(self, message: str = "无权限执行该操作"):
        super().__init__("forbidden", message, 403)


# ---------------------------------------------------------------------------
# 参与者与存储
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    id: str
    name: str
    role: str
    permissions: frozenset[str] = frozenset()

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise AuthzError(f"该操作需要角色：{'、'.join(roles)}")

    def require_permission(self, permission: str) -> None:
        if permission not in self.permissions:
            raise AuthzError("缺少权限：" + permission)


@dataclass
class Store:
    """仅追加事件台账。events 之外不保存任何可变状态。"""

    events: list[dict] = field(default_factory=list)
    _seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def append(self, type_: str, actor: Actor | None, payload: dict) -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "id": f"ev_{self._seq:06d}_{uuid.uuid4().hex[:8]}",
                "type": type_,
                "at": today().isoformat(),
                "actor_id": actor.id if actor else None,
                "actor_name": actor.name if actor else None,
                "actor_role": actor.role if actor else None,
                "payload": payload,
            }
            self.events.append(event)
            return event

    def to_json(self) -> str:
        return json.dumps({"events": self.events, "_seq": self._seq}, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Store":
        data = json.loads(raw)
        store = cls(events=data.get("events", []), _seq=data.get("_seq", 0))
        return store


# 允许测试与服务注入固定日期
_clock: Callable[[], date] = date.today


def today() -> date:
    return _clock()


def set_clock(fn: Callable[[], date]) -> None:
    global _clock
    _clock = fn


def _d(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


# ---------------------------------------------------------------------------
# 投影（从事件重放）
# ---------------------------------------------------------------------------


@dataclass
class State:
    trees: dict[str, dict] = field(default_factory=dict)
    farmers: dict[str, dict] = field(default_factory=dict)
    # tree_id -> 责任版本列表（按生效季升序）
    responsibility: dict[str, list[dict]] = field(default_factory=dict)
    # (tree_id, season) -> 方案及里程碑（含调整后的当前形态）
    plans: dict[tuple[str, str], dict] = field(default_factory=dict)
    incidents: dict[str, dict] = field(default_factory=dict)
    dedup_index: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, dict] = field(default_factory=dict)
    submissions: dict[str, dict] = field(default_factory=dict)
    # tree,season,stage -> 最新一次提交
    submission_index: dict[tuple[str, str, str], str] = field(default_factory=dict)
    decisions: dict[str, dict] = field(default_factory=dict)   # tree -> 最新保护状态裁决
    breaches: list[dict] = field(default_factory=list)
    contracts: dict[tuple[str, int], dict] = field(default_factory=dict)
    contract_versions: dict[str, list[int]] = field(default_factory=dict)
    batches: dict[str, dict] = field(default_factory=dict)
    paid_milestones: set[str] = field(default_factory=set)
    allocations: dict[str, list[dict]] = field(default_factory=dict)
    disputes: dict[str, dict] = field(default_factory=dict)


def replay(events: Iterable[dict]) -> State:
    st = State()
    for e in events:
        p = e["payload"]
        t = e["type"]
        if t == "TREE_REGISTERED":
            st.trees[p["tree_id"]] = {
                "id": p["tree_id"],
                "public_code": p["public_code"],
                "age_years": p["age_years"],
                "village": p.get("village"),
                # plot_hint 是可公开的模糊位置（如村片区），不含住址
                "plot_hint": p.get("plot_hint"),
                # exact_location 为精确坐标，仅授权角色可见
                "exact_location": p.get("exact_location"),
                "registered_season": p["season"],
            }
        elif t == "FARMER_REGISTERED":
            st.farmers[p["farmer_id"]] = {"id": p["farmer_id"], "name": p["name"]}
        elif t == "RESPONSIBILITY_ASSIGNED":
            st.responsibility.setdefault(p["tree_id"], []).append(
                {
                    "farmer_id": p["farmer_id"],
                    "effective_season": p["effective_season"],
                    "reason": p.get("reason", ""),
                    "event_id": e["id"],
                    "assigned_at": e["at"],
                }
            )
        elif t == "CONTRACT_PUBLISHED":
            key = (p["season"], p["version"])
            st.contracts[key] = dict(p)
            versions = st.contract_versions.setdefault(p["season"], [])
            if p["version"] not in versions:
                versions.append(p["version"])
        elif t == "PLAN_PUBLISHED":
            st.plans[(p["tree_id"], p["season"])] = {
                "tree_id": p["tree_id"],
                "season": p["season"],
                "stages": [dict(s) for s in p["stages"]],
                "restrictions": [dict(r) for r in p.get("restrictions", [])],
                "published_event": e["id"],
                "adjustments": [],
            }
        elif t == "MILESTONE_ADJUSTED":
            plan = st.plans.get((p["tree_id"], p["season"]))
            if plan:
                for change in p["changes"]:
                    for stage in plan["stages"]:
                        if stage["code"] == change["stage_code"]:
                            if "new_scheduled_at" in change:
                                stage["scheduled_at"] = change["new_scheduled_at"]
                            if "new_requirement" in change:
                                stage["requirement"] = change["new_requirement"]
                            stage["adjusted"] = True
                plan["adjustments"].append(dict(p, event_id=e["id"], at=e["at"]))
        elif t == "INCIDENT_REPORTED":
            st.incidents[p["incident_id"]] = {
                "id": p["incident_id"],
                "tree_id": p["tree_id"],
                "kind": p["kind"],
                "season": p["season"],
                "occurred_at": p["occurred_at"],
                "note": p.get("note", ""),
                "sources": [dict(p["source"])],
                "evidence_ids": [],
                "created_at": e["at"],
            }
            st.dedup_index[p["dedup_key"]] = p["incident_id"]
        elif t == "INCIDENT_REPORT_APPENDED":
            inc = st.incidents.get(p["incident_id"])
            if inc:
                inc["sources"].append(dict(p["source"]))
                if p.get("note") and p["note"] not in inc["note"]:
                    inc["note"] = (inc["note"] + " | " + p["note"]).strip(" |")
        elif t == "EVIDENCE_ADDED":
            st.evidence[p["evidence_id"]] = {
                "id": p["evidence_id"],
                "incident_id": p["incident_id"],
                "kind": p["kind"],
                "sha256": p["sha256"],
                "captured_at": p["captured_at"],
                "uploaded_at": e["at"],
                "uploader_id": e["actor_id"],
                "metadata": p.get("metadata", {}),
                "event_id": e["id"],
            }
            inc = st.incidents.get(p["incident_id"])
            if inc and p["evidence_id"] not in inc["evidence_ids"]:
                inc["evidence_ids"].append(p["evidence_id"])
        elif t == "MILESTONE_SUBMITTED":
            st.submissions[p["submission_id"]] = {
                "id": p["submission_id"],
                "tree_id": p["tree_id"],
                "season": p["season"],
                "stage_code": p["stage_code"],
                "incident_id": p["incident_id"],
                "evidence_ids": list(p["evidence_ids"]),
                "farmer_snapshot": p["farmer_snapshot"],
                "submitted_by": e["actor_id"],
                "submitted_at": e["at"],
                "verdict": SUBMIT_PENDING,
                "review": None,
            }
            st.submission_index[(p["tree_id"], p["season"], p["stage_code"])] = p["submission_id"]
        elif t == "MILESTONE_REVIEWED":
            sub = st.submissions.get(p["submission_id"])
            if sub:
                sub["verdict"] = p["verdict"]
                sub["review"] = {
                    "note": p.get("note", ""),
                    "review_evidence_id": p.get("review_evidence_id"),
                    "reviewer_id": e["actor_id"],
                    "reviewed_at": e["at"],
                    "event_id": e["id"],
                }
        elif t == "PROTECTION_STATUS_DECIDED":
            st.decisions[p["tree_id"]] = dict(p, decided_event=e["id"], decided_at=e["at"])
        elif t == "BREACH_CONFIRMED":
            st.breaches.append(dict(p, event_id=e["id"], confirmed_at=e["at"]))
        elif t == "DISBURSEMENT_CREATED":
            st.batches[p["batch_id"]] = {
                "id": p["batch_id"],
                "season": p["season"],
                "contract_version": p["contract_version"],
                "total": p["total"],
                "lines": [dict(x) for x in p["lines"]],
                "status": "DRAFT",
                "created_at": e["at"],
                "confirmed_at": None,
            }
            for line in p["lines"]:
                st.paid_milestones.add(line["submission_id"])
        elif t == "DISBURSEMENT_CONFIRMED":
            batch = st.batches.get(p["batch_id"])
            if batch:
                batch["status"] = "CONFIRMED"
                batch["confirmed_at"] = e["at"]
        elif t == "ALLOCATION_MADE":
            st.allocations.setdefault(p["batch_id"], []).extend(dict(x) for x in p["lines"])
        elif t == "DISPUTE_OPENED":
            st.disputes[p["dispute_id"]] = {
                "id": p["dispute_id"],
                "subject": p["subject"],
                "opened_by": e["actor_id"],
                "note": p["note"],
                "status": "OPEN",
                "resolution": None,
                "opened_at": e["at"],
            }
        elif t == "DISPUTE_RESOLVED":
            dispute = st.disputes.get(p["dispute_id"])
            if dispute:
                dispute["status"] = "RESOLVED"
                dispute["resolution"] = p["resolution"]
                dispute["resolved_at"] = e["at"]
    return st


def state(store: Store) -> State:
    return replay(store.events)


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------


def _tree(st: State, tree_id: str) -> dict:
    tree = st.trees.get(tree_id)
    if not tree:
        raise NotFound("保护树")
    return tree


def _plan(st: State, tree_id: str, season: str) -> dict:
    plan = st.plans.get((tree_id, season))
    if not plan:
        raise NotFound(f"{season2(season)}年度管护方案")
    return plan


def season2(season: str) -> str:
    return season


def responsible_farmer(st: State, tree_id: str, season: str) -> dict | None:
    """某树某季当前（重放后）责任农户。"""
    current = None
    for v in st.responsibility.get(tree_id, []):
        if v["effective_season"] <= season:
            current = v
    return st.farmers.get(current["farmer_id"]) if current else None


def _farmer_snapshot(st: State, tree_id: str, season: str) -> dict | None:
    farmer = responsible_farmer(st, tree_id, season)
    return {"farmer_id": farmer["id"], "farmer_name": farmer["name"]} if farmer else None


def is_protected_in_season(st: State, tree_id: str, season: str) -> tuple[bool, str]:
    """该树在指定年度是否仍受保护。返回 (是否受保护, 原因)。"""
    decision = st.decisions.get(tree_id)
    if decision and decision["effective_season"] <= season:
        if decision["status"] in (STATUS_DEAD, STATUS_EXITED_DAMAGE):
            return False, {
                STATUS_DEAD: "树木已死亡，保护自{es}年度起终止",
                STATUS_EXITED_DAMAGE: "极端损伤评估退出，保护自{es}年度起终止",
            }[decision["status"]].format(es=decision["effective_season"])
    return True, ""


def has_confirmed_breach(st: State, tree_id: str, season: str) -> dict | None:
    for b in st.breaches:
        if b["tree_id"] == tree_id and b["season"] == season:
            return b
    return None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 命令：基础档案
# ---------------------------------------------------------------------------


def register_tree(
    store: Store,
    actor: Actor,
    public_code: str,
    age_years: int,
    season: str,
    village: str | None = None,
    plot_hint: str | None = None,
    exact_location: dict | None = None,
) -> dict:
    """登记一棵保护树。exact_location 为精确坐标，公开视图永不输出。"""
    actor.require_role(ROLE_TOWN, ROLE_ADMIN, ROLE_COOP)
    if age_years <= 0:
        raise DomainError("bad_input", "树龄必须为正整数")
    st = state(store)
    if any(t["public_code"] == public_code for t in st.trees.values()):
        raise DomainError("duplicate", "古树编号已存在", 409)
    tree_id = _new_id("tree")
    payload = {
        "tree_id": tree_id,
        "public_code": public_code,
        "age_years": age_years,
        "season": season,
        "village": village,
        "plot_hint": plot_hint,
        "exact_location": exact_location,
    }
    store.append("TREE_REGISTERED", actor, payload)
    return payload


def register_farmer(store: Store, actor: Actor, name: str) -> dict:
    actor.require_role(ROLE_COOP, ROLE_TOWN, ROLE_ADMIN)
    farmer_id = _new_id("farmer")
    payload = {"farmer_id": farmer_id, "name": name}
    store.append("FARMER_REGISTERED", actor, payload)
    return payload


def assign_responsibility(
    store: Store,
    actor: Actor,
    tree_id: str,
    farmer_id: str,
    effective_season: str,
    reason: str = "",
) -> dict:
    """变更责任农户（换地、责任人变更）。

    生成新的责任版本，仅对 effective_season 及以后生效；已形成的提交、
    验收与拨付快照中的责任农户保持不变。
    """
    actor.require_role(ROLE_COOP, ROLE_TOWN, ROLE_ADMIN)
    st = state(store)
    _tree(st, tree_id)
    if farmer_id not in st.farmers:
        raise NotFound("农户")
    latest = max(
        (v["effective_season"] for v in st.responsibility.get(tree_id, [])),
        default=None,
    )
    if latest is not None and effective_season <= latest:
        raise DomainError(
            "bad_input",
            f"新责任版本生效年度必须晚于现有版本（{latest}），历史责任不可改写",
        )
    payload = {
        "tree_id": tree_id,
        "farmer_id": farmer_id,
        "effective_season": effective_season,
        "reason": reason,
    }
    store.append("RESPONSIBILITY_ASSIGNED", actor, payload)
    return payload


def publish_contract(
    store: Store,
    actor: Actor,
    season: str,
    stage_amounts: dict[str, int],
    tree_ids: list[str],
    note: str = "",
) -> dict:
    """企业发布某年度保护价合同。版本自动递增；拨付按出账时的有效版本快照。"""
    actor.require_role(ROLE_ENTERPRISE, ROLE_ADMIN)
    bad = set(stage_amounts) - set(STAGE_TYPES)
    if bad:
        raise DomainError("bad_input", f"未知里程碑类型：{sorted(bad)}")
    if any(int(a) < 0 for a in stage_amounts.values()):
        raise DomainError("bad_input", "保护附加金金额不能为负")
    st = state(store)
    for tid in tree_ids:
        _tree(st, tid)
    version = max(st.contract_versions.get(season, []), default=0) + 1
    payload = {
        "season": season,
        "version": version,
        "stage_amounts": {k: int(v) for k, v in stage_amounts.items()},
        "tree_ids": list(tree_ids),
        "note": note,
    }
    store.append("CONTRACT_PUBLISHED", actor, payload)
    return payload


def active_contract(st: State, season: str) -> dict | None:
    versions = st.contract_versions.get(season)
    if not versions:
        return None
    return st.contracts[(season, max(versions))]


# ---------------------------------------------------------------------------
# 命令：年度方案与未来里程碑调整
# ---------------------------------------------------------------------------


def publish_plan(
    store: Store,
    actor: Actor,
    tree_id: str,
    season: str,
    stages: list[dict],
    restrictions: list[dict] | None = None,
) -> dict:
    """农技员为一棵树编制年度方案：分阶段里程碑 + 禁止采摘/接穗限制。"""
    actor.require_role(ROLE_AGRONOMIST, ROLE_ADMIN)
    st = state(store)
    _tree(st, tree_id)
    ok, why = is_protected_in_season(st, tree_id, season)
    if not ok:
        raise DomainError("tree_not_protected", why)
    if (tree_id, season) in st.plans:
        raise DomainError("duplicate", "该树该年度方案已存在，应通过里程碑调整变更", 409)
    norm = _normalize_stages(stages)
    for r in restrictions or []:
        if r.get("type") not in ("NO_HARVEST", "NO_SCION"):
            raise DomainError("bad_input", "限制类型只能是 NO_HARVEST 或 NO_SCION")
    payload = {
        "tree_id": tree_id,
        "season": season,
        "stages": norm,
        "restrictions": restrictions or [],
    }
    store.append("PLAN_PUBLISHED", actor, payload)
    return payload


def _normalize_stages(stages: list[dict]) -> list[dict]:
    if not stages:
        raise DomainError("bad_input", "年度方案至少包含一个里程碑")
    out = []
    seen = set()
    for s in stages:
        code = s.get("code")
        if code not in STAGE_TYPES:
            raise DomainError("bad_input", f"未知里程碑类型：{code}")
        if code in seen:
            raise DomainError("bad_input", f"里程碑重复：{code}")
        seen.add(code)
        scheduled = s.get("scheduled_at")
        _d(scheduled)  # 校验日期格式
        out.append(
            {
                "code": code,
                "name": s.get("name") or code,
                "scheduled_at": scheduled,
                "requirement": s.get("requirement", ""),
            }
        )
    return out


def adjust_future_milestones(
    store: Store,
    actor: Actor,
    tree_id: str,
    season: str,
    changes: list[dict],
    reason: str,
) -> dict:
    """调整未来里程碑（换地衔接、方案延期、极端损伤后改期）。

    仅允许：

    * 调整人具备 milestone.adjust 权限的农技人员；
    * 里程碑尚未到/尚未裁决（已验收或已驳回的不可改）；
    * 调整只改变未来排期与要求，不改写任何既有事实。
    """
    actor.require_role(ROLE_AGRONOMIST)
    actor.require_permission(PERM_ADJUST_MILESTONE)
    if not reason.strip():
        raise DomainError("bad_input", "调整必须注明原因")
    st = state(store)
    plan = _plan(st, _tree(st, tree_id)["id"], season)
    stage_map = {s["code"]: s for s in plan["stages"]}
    norm_changes = []
    for change in changes:
        code = change.get("stage_code")
        stage = stage_map.get(code)
        if not stage:
            raise DomainError("bad_input", f"方案中不存在里程碑：{code}")
        sid = st.submission_index.get((tree_id, season, code))
        if sid:
            verdict = st.submissions[sid]["verdict"]
            if verdict in (SUBMIT_APPROVED, SUBMIT_REJECTED):
                raise DomainError(
                    "fact_locked",
                    f"里程碑{code}已{ '验收通过' if verdict == SUBMIT_APPROVED else '驳回'}，"
                    "事实不可改写；如需补救请编制新年度方案",
                )
        new_date = change.get("new_scheduled_at", stage["scheduled_at"])
        _d(new_date)
        if _d(new_date) < today():
            raise DomainError("bad_input", f"里程碑{code}新排期不能早于今天，只能调整未来里程碑")
        norm_changes.append(
            {
                "stage_code": code,
                "new_scheduled_at": new_date,
                "new_requirement": change.get(
                    "new_requirement", stage["requirement"]
                ),
            }
        )
    payload = {
        "tree_id": tree_id,
        "season": season,
        "changes": norm_changes,
        "reason": reason,
    }
    store.append("MILESTONE_ADJUSTED", actor, payload)
    return payload


# ---------------------------------------------------------------------------
# 命令：现场事件（离线、多部门合并）与不可变证据
# ---------------------------------------------------------------------------


def report_incident(
    store: Store,
    actor: Actor,
    tree_id: str,
    kind: str,
    occurred_at: str | date,
    *,
    dedup_key: str,
    season: str,
    note: str = "",
    dept: str | None = None,
    channel: str = "ONLINE",
    stage_code: str | None = None,
) -> dict:
    """上报一次现场事件。

    护树队离线巡查可用 channel=OFFLINE，recorded_at 晚于 occurred_at。
    去重合并规则（满足任一即合并为同一次现场事件）：

    1. dedup_key 相同（客户端幂等键，断网重试安全）；
    2. 同树、同日、同事件类型（不同部门重复上报）。
    """
    actor.require_role(
        ROLE_GUARDIAN, ROLE_AGRONOMIST, ROLE_COOP, ROLE_TOWN, ROLE_ADMIN
    )
    if kind not in INCIDENT_KINDS:
        raise DomainError("bad_input", f"未知现场事件类型：{kind}")
    _d(occurred_at)
    st = state(store)
    _tree(st, tree_id)
    source = {
        "reporter_id": actor.id,
        "reporter_name": actor.name,
        "dept": dept or actor.role,
        "channel": channel,  # OFFLINE 表示离线补报
        "recorded_at": today().isoformat(),
    }

    existing_id = st.dedup_index.get(dedup_key)
    if not existing_id:
        for inc in st.incidents.values():
            if (
                inc["tree_id"] == tree_id
                and inc["kind"] == kind
                and inc["occurred_at"] == _d(occurred_at).isoformat()
            ):
                existing_id = inc["id"]
                break
    if existing_id:
        payload = {
            "incident_id": existing_id,
            "source": source,
            "note": note,
            "merged_dedup_key": dedup_key,
        }
        store.append("INCIDENT_REPORT_APPENDED", actor, payload)
        return {"incident_id": existing_id, "merged": True, **payload}

    incident_id = _new_id("inc")
    payload = {
        "incident_id": incident_id,
        "dedup_key": dedup_key,
        "tree_id": tree_id,
        "kind": kind,
        "season": season,
        "occurred_at": _d(occurred_at).isoformat(),
        "note": note,
        "source": source,
        "stage_code": stage_code,
    }
    store.append("INCIDENT_REPORTED", actor, payload)
    return {"incident_id": incident_id, "merged": False, **payload}


def add_evidence(
    store: Store,
    actor: Actor,
    incident_id: str,
    kind: str,
    sha256: str,
    captured_at: str | date,
    metadata: dict | None = None,
) -> dict:
    """为现场事件追加原始证据。证据只能新增，不能修改或删除。

    * 照片/定位：护树队等现场角色可提交；
    * 病害诊断：仅农技员提交（保留其原始意见版本）；
    * 复核意见：由里程碑复核动作自动留存。
    """
    if kind not in EVIDENCE_KINDS:
        raise DomainError("bad_input", f"未知证据类型：{kind}")
    if not sha256:
        raise DomainError("bad_input", "证据必须提供内容哈希 sha256")
    _d(captured_at)
    st = state(store)
    inc = st.incidents.get(incident_id)
    if not inc:
        raise NotFound("现场事件")
    if kind == EV_DIAGNOSIS:
        actor.require_role(ROLE_AGRONOMIST)
    else:
        actor.require_role(
            ROLE_GUARDIAN, ROLE_AGRONOMIST, ROLE_COOP, ROLE_TOWN, ROLE_ADMIN
        )
    evidence_id = _new_id("evd")
    payload = {
        "evidence_id": evidence_id,
        "incident_id": incident_id,
        "kind": kind,
        "sha256": sha256,
        "captured_at": _d(captured_at).isoformat(),
        "metadata": metadata or {},
    }
    store.append("EVIDENCE_ADDED", actor, payload)
    return payload


# ---------------------------------------------------------------------------
# 命令：里程碑提交、复核
# ---------------------------------------------------------------------------


def submit_milestone(
    store: Store,
    actor: Actor,
    tree_id: str,
    season: str,
    stage_code: str,
    incident_id: str,
    evidence_ids: list[str],
) -> dict:
    """护树队以一次现场事件及其原始证据申报某个阶段管护完成。"""
    actor.require_role(ROLE_GUARDIAN, ROLE_COOP)
    st = state(store)
    _tree(st, tree_id)
    # 先判定保护资格：已死亡/退出的树不得以“无方案”名义掩盖
    ok, why = is_protected_in_season(st, tree_id, season)
    if not ok:
        raise DomainError("tree_not_protected", why)
    plan = _plan(st, tree_id, season)
    if stage_code not in {s["code"] for s in plan["stages"]}:
        raise DomainError("bad_input", f"{season}年度方案不含里程碑：{stage_code}")
    inc = st.incidents.get(incident_id)
    if not inc:
        raise NotFound("现场事件")
    if inc["tree_id"] != tree_id:
        raise DomainError("bad_input", "现场事件与保护树不匹配")
    if not evidence_ids:
        raise DomainError("bad_input", "申报必须附带至少一份原始证据")
    for eid in evidence_ids:
        ev = st.evidence.get(eid)
        if not ev:
            raise NotFound("证据")
        if ev["incident_id"] != incident_id:
            raise DomainError("bad_input", f"证据{eid}不属于该现场事件")
    sid = st.submission_index.get((tree_id, season, stage_code))
    if sid and st.submissions[sid]["verdict"] == SUBMIT_APPROVED:
        raise DomainError("fact_locked", "该里程碑已验收通过，不得重复申报", 409)
    if sid and st.submissions[sid]["verdict"] == SUBMIT_PENDING:
        raise DomainError("duplicate", "该里程碑已申报，等待农技员复核", 409)
    # 驳回后允许重新申报：生成一条全新的提交事实，旧驳回原样保留
    submission_id = _new_id("sub")
    payload = {
        "submission_id": submission_id,
        "tree_id": tree_id,
        "season": season,
        "stage_code": stage_code,
        "incident_id": incident_id,
        "evidence_ids": list(evidence_ids),
        "farmer_snapshot": _farmer_snapshot(st, tree_id, season),
    }
    store.append("MILESTONE_SUBMITTED", actor, payload)
    return payload


def review_milestone(
    store: Store,
    actor: Actor,
    submission_id: str,
    verdict: str,
    note: str = "",
    review_sha256: str = "",
) -> dict:
    """农技员复核：验收通过或驳回。复核意见作为原始证据永久留存。

    复核结论只追加，不支持修改；驳回后责任方可重新申报新一轮。
    """
    actor.require_role(ROLE_AGRONOMIST)
    if verdict not in (SUBMIT_APPROVED, SUBMIT_REJECTED):
        raise DomainError("bad_input", "复核结论只能是 APPROVED 或 REJECTED")
    st = state(store)
    sub = st.submissions.get(submission_id)
    if not sub:
        raise NotFound("里程碑申报")
    if sub["verdict"] != SUBMIT_PENDING:
        raise DomainError("fact_locked", "该申报已有复核结论，结论不可改写", 409)
    if verdict == SUBMIT_REJECTED and not note.strip():
        raise DomainError("bad_input", "驳回必须填写原因")

    review_evidence_id = None
    if review_sha256:
        ev_payload = {
            "evidence_id": _new_id("evd"),
            "incident_id": sub["incident_id"],
            "kind": EV_REVIEW,
            "sha256": review_sha256,
            "captured_at": today().isoformat(),
            "metadata": {"submission_id": submission_id, "verdict": verdict},
        }
        store.append("EVIDENCE_ADDED", actor, ev_payload)
        review_evidence_id = ev_payload["evidence_id"]

    payload = {
        "submission_id": submission_id,
        "verdict": verdict,
        "note": note,
        "review_evidence_id": review_evidence_id,
    }
    store.append("MILESTONE_REVIEWED", actor, payload)
    return payload


# ---------------------------------------------------------------------------
# 命令：死亡、极端损伤、违约退出
# ---------------------------------------------------------------------------


def decide_protection_status(
    store: Store,
    actor: Actor,
    tree_id: str,
    status: str,
    effective_season: str,
    incident_id: str,
    note: str = "",
) -> dict:
    """农技员依据现场事件裁决保护状态（死亡/极端损伤退出/留养观察）。

    死亡或退出从 effective_season（通常为下一季）起终止保护；
    当季已通过的里程碑事实保留，是否可拨付由资格规则判定。
    """
    actor.require_role(ROLE_AGRONOMIST, ROLE_TOWN)
    if status not in (STATUS_DEAD, STATUS_EXITED_DAMAGE, STATUS_UNDER_RECOVERY):
        raise DomainError("bad_input", "未知保护状态裁决")
    st = state(store)
    _tree(st, tree_id)
    inc = st.incidents.get(incident_id)
    if not inc:
        raise NotFound("现场事件")
    if inc["tree_id"] != tree_id:
        raise DomainError("bad_input", "现场事件与保护树不匹配")
    if status == STATUS_DEAD and inc["kind"] != INC_TREE_DEATH:
        raise DomainError("bad_input", "死亡裁决必须依据 TREE_DEATH 现场事件")
    if status in (STATUS_EXITED_DAMAGE, STATUS_UNDER_RECOVERY) and inc["kind"] != INC_EXTREME_DAMAGE:
        raise DomainError("bad_input", "极端损伤裁决必须依据 EXTREME_DAMAGE 现场事件")
    payload = {
        "tree_id": tree_id,
        "status": status,
        "effective_season": effective_season,
        "incident_id": incident_id,
        "note": note,
    }
    store.append("PROTECTION_STATUS_DECIDED", actor, payload)
    return payload


def confirm_breach(
    store: Store,
    actor: Actor,
    tree_id: str,
    season: str,
    restriction: str,
    incident_id: str,
    note: str = "",
) -> dict:
    """农技员确认违反禁止采摘或接穗限制。当季里程碑丧失附加金资格。"""
    actor.require_role(ROLE_AGRONOMIST)
    if restriction not in ("NO_HARVEST", "NO_SCION"):
        raise DomainError("bad_input", "限制类型只能是 NO_HARVEST 或 NO_SCION")
    st = state(store)
    _tree(st, tree_id)
    inc = st.incidents.get(incident_id)
    if not inc or inc["tree_id"] != tree_id or inc["kind"] != INC_BREACH:
        raise DomainError("bad_input", "违约确认必须依据该树的 BREACH 现场事件")
    payload = {
        "tree_id": tree_id,
        "season": season,
        "restriction": restriction,
        "incident_id": incident_id,
        "note": note,
    }
    store.append("BREACH_CONFIRMED", actor, payload)
    return payload


# ---------------------------------------------------------------------------
# 命令：企业拨付与合作社分配、异议
# ---------------------------------------------------------------------------


def milestone_eligibility(st: State, sub: dict) -> tuple[bool, str]:
    """已通过里程碑在当前是否仍可拨付附加金。"""
    if sub["verdict"] != SUBMIT_APPROVED:
        return False, "里程碑未验收通过"
    tree_id, season = sub["tree_id"], sub["season"]
    ok, why = is_protected_in_season(st, tree_id, season)
    if not ok:
        return False, why
    breach = has_confirmed_breach(st, tree_id, season)
    if breach:
        return False, f"当季存在已确认的违约（{breach['restriction']}），不予附加金"
    contract = active_contract(st, season)
    if not contract:
        return False, f"{season}年度无有效保护价合同"
    if tree_id not in contract["tree_ids"]:
        return False, "该树不在保护价合同名单内"
    amount = contract["stage_amounts"].get(sub["stage_code"])
    if amount is None:
        return False, f"合同版本v{contract['version']}未约定{sub['stage_code']}附加金"
    if sub["id"] in st.paid_milestones:
        return False, "里程碑已包含在拨付单中，不可重复拨付"
    if not sub.get("farmer_snapshot"):
        return False, "申报时缺少责任农户快照，无法确定受款人"
    return True, ""


def create_disbursement(
    store: Store, actor: Actor, season: str, submission_ids: list[str] | None = None
) -> dict:
    """企业按季生成附加金拨付单。

    默认汇总该季全部“已通过且仍符合保护条件”的里程碑；
    显式指定名单时逐一校验——死亡树次季申报、违约季、重复拨付都会被拒绝，
    拨付单金额按出账时有效合同版本、农户按验收事实中的快照固定。
    """
    actor.require_role(ROLE_ENTERPRISE)
    st = state(store)
    contract = active_contract(st, season)
    if not contract:
        raise DomainError("no_contract", f"{season}年度无有效保护价合同")

    if submission_ids is None:
        candidates = [
            s
            for s in st.submissions.values()
            if s["season"] == season and s["verdict"] == SUBMIT_APPROVED
        ]
        chosen = [s for s in candidates if milestone_eligibility(st, s)[0]]
    else:
        chosen = []
        for sid in submission_ids:
            sub = st.submissions.get(sid)
            if not sub:
                raise NotFound("里程碑申报")
            if sub["season"] != season:
                raise DomainError("bad_input", f"{sid}不属于{season}年度")
            ok, why = milestone_eligibility(st, sub)
            if not ok:
                raise DomainError("ineligible_milestone", f"里程碑不可拨付：{why}")
            chosen.append(sub)

    lines = []
    for sub in sorted(chosen, key=lambda s: (s["tree_id"], s["stage_code"])):
        lines.append(
            {
                "submission_id": sub["id"],
                "tree_id": sub["tree_id"],
                "stage_code": sub["stage_code"],
                "farmer_snapshot": sub["farmer_snapshot"],
                "contract_version": contract["version"],
                "amount": int(contract["stage_amounts"][sub["stage_code"]]),
            }
        )
    if not lines:
        raise DomainError("empty_disbursement", "没有符合拨付条件的里程碑")
    batch_id = _new_id("batch")
    payload = {
        "batch_id": batch_id,
        "season": season,
        "contract_version": contract["version"],
        "total": sum(x["amount"] for x in lines),
        "lines": lines,
    }
    store.append("DISBURSEMENT_CREATED", actor, payload)
    return payload


def confirm_disbursement(store: Store, actor: Actor, batch_id: str) -> dict:
    """企业确认附加金已注入。"""
    actor.require_role(ROLE_ENTERPRISE)
    st = state(store)
    batch = st.batches.get(batch_id)
    if not batch:
        raise NotFound("拨付单")
    if batch["status"] != "DRAFT":
        raise DomainError("fact_locked", "拨付单已确认，不可重复确认", 409)
    payload = {"batch_id": batch_id}
    store.append("DISBURSEMENT_CONFIRMED", actor, payload)
    return payload


def record_allocation(
    store: Store, actor: Actor, batch_id: str, lines: list[dict]
) -> dict:
    """合作社按合同版本把到账附加金分配到农户。分配总额必须与拨付单一致。"""
    actor.require_role(ROLE_COOP)
    st = state(store)
    batch = st.batches.get(batch_id)
    if not batch:
        raise NotFound("拨付单")
    if batch["status"] != "CONFIRMED":
        raise DomainError("bad_input", "拨付单未经企业确认到账，不能分配")
    total = 0
    norm = []
    for line in lines:
        fid = line.get("farmer_id")
        if fid not in st.farmers:
            raise NotFound("农户")
        amount = int(line.get("amount", 0))
        if amount < 0:
            raise DomainError("bad_input", "分配金额不能为负")
        total += amount
        norm.append(
            {
                "farmer_id": fid,
                "amount": amount,
                "contract_version": line.get("contract_version", batch["contract_version"]),
                "note": line.get("note", ""),
            }
        )
    if total != batch["total"]:
        raise DomainError(
            "bad_input",
            f"分配总额{total}与拨付单总额{batch['total']}不一致",
        )
    payload = {"batch_id": batch_id, "lines": norm}
    store.append("ALLOCATION_MADE", actor, payload)
    return payload


def open_dispute(
    store: Store,
    actor: Actor,
    subject: dict,
    note: str,
) -> dict:
    """农户/合作社对验收或分配提出异议。异议不改变任何已裁决事实。"""
    actor.require_role(ROLE_COOP, ROLE_TOWN, ROLE_GUARDIAN, ROLE_AGRONOMIST)
    st = state(store)
    kind = subject.get("type")
    rid = subject.get("id")
    if kind == "SUBMISSION" and rid not in st.submissions:
        raise NotFound("里程碑申报")
    if kind == "BATCH" and rid not in st.batches:
        raise NotFound("拨付单")
    if kind not in ("SUBMISSION", "BATCH"):
        raise DomainError("bad_input", "异议对象只能是 SUBMISSION 或 BATCH")
    dispute_id = _new_id("dsp")
    payload = {"dispute_id": dispute_id, "subject": dict(subject), "note": note}
    store.append("DISPUTE_OPENED", actor, payload)
    return payload


def resolve_dispute(
    store: Store, actor: Actor, dispute_id: str, resolution: str
) -> dict:
    """镇里/合作社裁定异议。处理结果记录在案，台账事实仍不回写。"""
    actor.require_role(ROLE_TOWN, ROLE_COOP)
    if not resolution.strip():
        raise DomainError("bad_input", "裁定结果不能为空")
    st = state(store)
    if dispute_id not in st.disputes:
        raise NotFound("异议")
    payload = {"dispute_id": dispute_id, "resolution": resolution}
    store.append("DISPUTE_RESOLVED", actor, payload)
    return payload


# ---------------------------------------------------------------------------
# 查询：年度结果分类与公众视图
# ---------------------------------------------------------------------------


def _latest_submission(st: State, tree_id: str, season: str, code: str) -> dict | None:
    sid = st.submission_index.get((tree_id, season, code))
    return st.submissions.get(sid) if sid else None


def classify_tree(st: State, tree_id: str, season: str) -> dict:
    """将一棵树在某一年度归入：真实存活 / 复壮 / 合理退出 / 漏管。"""
    tree = st.trees[tree_id]
    decision = st.decisions.get(tree_id)
    plan = st.plans.get((tree_id, season))

    # 1) 已在本年或更早裁定死亡/损伤退出
    if decision and decision["effective_season"] <= season and decision["status"] in (
        STATUS_DEAD,
        STATUS_EXITED_DAMAGE,
    ):
        inc = st.incidents.get(decision["incident_id"])
        # 合理退出：死亡/损伤被及时发现上报（事发年度与裁决年度相隔不超过宽限）
        timely = (
            inc is not None
            and int(season) - int(inc["season"]) <= REPORT_GRACE_SEASONS
            and int(inc["season"]) <= int(season)
        )
        if timely:
            category = YEAR_REASONABLE_EXIT
        else:
            category = YEAR_NEGLECTED
        return {
            "tree_id": tree_id,
            "category": category,
            "status": decision["status"],
            "reason": decision.get("note", ""),
            "passed_stages": [],
        }

    # 2) 仍受保护（含留养观察）：以年度方案里程碑完成情况判定
    if plan:
        passed, pending_or_failed = [], []
        for stage in plan["stages"]:
            sub = _latest_submission(st, tree_id, season, stage["code"])
            if sub and sub["verdict"] == SUBMIT_APPROVED:
                passed.append(stage["code"])
            else:
                pending_or_failed.append(stage["code"])
        if not pending_or_failed:
            rejuv = _latest_submission(st, tree_id, season, STAGE_REJUVENATE)
            if rejuv and rejuv["verdict"] == SUBMIT_APPROVED:
                category = YEAR_REJUVENATED
            else:
                category = YEAR_REAL_ALIVE
            return {
                "tree_id": tree_id,
                "category": category,
                "status": decision["status"] if decision else STATUS_PROTECTED,
                "passed_stages": passed,
            }
        return {
            "tree_id": tree_id,
            "category": YEAR_NEGLECTED,
            "status": decision["status"] if decision else STATUS_PROTECTED,
            "passed_stages": passed,
            "missing_stages": pending_or_failed,
        }

    # 3) 既无退出裁决又无年度方案：未纳入管护 = 漏管
    return {
        "tree_id": tree_id,
        "category": YEAR_NEGLECTED,
        "status": decision["status"] if decision else STATUS_PROTECTED,
        "passed_stages": [],
        "missing_stages": [s for s in STAGE_TYPES],
    }


def annual_report(st: State, season: str) -> dict:
    """镇里视角：逐树分类 + 明细 + 附加金汇总。"""
    rows = []
    for tree_id in sorted(st.trees):
        tree = st.trees[tree_id]
        # 该年度之后才登记的树不计入
        if tree["registered_season"] > season:
            continue
        result = classify_tree(st, tree_id, season)
        farmer = responsible_farmer(st, tree_id, season)
        result.update(
            {
                "public_code": tree["public_code"],
                "farmer": farmer,
                "village": tree["village"],
                "plot_hint": tree["plot_hint"],
                "breach": has_confirmed_breach(st, tree_id, season),
            }
        )
        rows.append(result)
    categories = {
        YEAR_REAL_ALIVE: 0,
        YEAR_REJUVENATED: 0,
        YEAR_REASONABLE_EXIT: 0,
        YEAR_NEGLECTED: 0,
    }
    for r in rows:
        categories[r["category"]] += 1
    paid_total = sum(
        b["total"] for b in st.batches.values() if b["season"] == season
    )
    return {
        "season": season,
        "summary": categories,
        "trees": rows,
        "disbursement_total": paid_total,
    }


def public_report(st: State, season: str) -> dict:
    """公众视角：古树保护进展。

    只输出古树编号、树龄、模糊片区、分类与通过的阶段数；
    不输出农户姓名/住址、精确坐标、现场记录原文与病害细节。
    """
    trees = []
    for tree_id in sorted(st.trees):
        tree = st.trees[tree_id]
        if tree["registered_season"] > season:
            continue
        result = classify_tree(st, tree_id, season)
        trees.append(
            {
                "public_code": tree["public_code"],
                "age_years": tree["age_years"],
                "plot_hint": tree["plot_hint"],
                "category": result["category"],
                "passed_stage_count": len(result["passed_stages"]),
            }
        )
    return {"season": season, "trees": trees}
