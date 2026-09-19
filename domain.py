"""老红橘古树管护兑现的领域模型与业务错误。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class DomainError(Exception):
    """业务规则拒绝；status 供 HTTP 层映射。"""

    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(DomainError):
    status = 404


class Forbidden(DomainError):
    status = 403


class Conflict(DomainError):
    status = 409


class Role(str, enum.Enum):
    FARMER = "farmer"  # 果农
    PATROL = "patrol"  # 护树队
    AGRONOMIST = "agronomist"  # 农技员
    ENTERPRISE = "enterprise"  # 企业
    COOP = "coop"  # 合作社
    TOWN = "town"  # 镇里
    PUBLIC = "public"  # 公众


# 调整未来里程碑所需的授权（普通农技员只有验收权）。
PLAN_ADJUST = "plan_adjust"


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: Role
    permissions: frozenset = frozenset()


class TreeStatus(str, enum.Enum):
    ALIVE = "alive"  # 存活
    REJUVENATING = "rejuvenating"  # 复壮中
    DEAD = "dead"  # 死亡
    EXITED = "exited"  # 合理退出


class Restriction(str, enum.Enum):
    NO_PICKING = "no_picking"  # 禁止采摘
    SCION_LIMIT = "scion_limit"  # 接穗限制


class MilestoneType(str, enum.Enum):
    PRUNING = "pruning"  # 修剪
    DISEASE_CONTROL = "disease_control"  # 防病
    REJUVENATION = "rejuvenation"  # 低产树复壮
    INSPECTION = "inspection"  # 巡查


class MilestoneStatus(str, enum.Enum):
    PENDING = "pending"  # 待执行
    SUBMITTED = "submitted"  # 待验收
    ACCEPTED = "accepted"  # 通过
    REJECTED = "rejected"  # 驳回
    CANCELLED = "cancelled"  # 取消（死亡/退出等）


class PlanStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"


class EvidenceKind(str, enum.Enum):
    PHOTO = "photo"  # 照片
    LOCATION = "location"  # 定位
    DIAGNOSIS = "diagnosis"  # 病害诊断
    REVIEW_OPINION = "review_opinion"  # 复核意见


class AdjustReason(str, enum.Enum):
    RELOCATION = "relocation"  # 换地
    RESPONSIBILITY_CHANGE = "responsibility_change"  # 责任人变更
    EXTREME_DAMAGE = "extreme_damage"  # 极端损伤
    POSTPONEMENT = "postponement"  # 方案延期


class ReviewDecision(str, enum.Enum):
    ACCEPT = "accept"
    REJECT = "reject"


class Outcome(str, enum.Enum):
    SURVIVED = "survived"  # 真实存活
    REJUVENATED = "rejuvenated"  # 复壮
    EXITED = "exited"  # 合理退出
    NEGLECTED = "neglected"  # 漏管


class DisputeStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


@dataclass
class Farmer:
    farmer_id: str
    name: str
    address: str  # 私密，仅内部使用，公众视图不得暴露
    village: str
    active: bool = True


@dataclass
class Tree:
    tree_id: str
    code: str  # 挂牌编号
    age_years: int
    lat: float  # 精确坐标，仅内部使用
    lng: float
    village: str
    restrictions: tuple = ()  # tuple[Restriction]
    status: TreeStatus = TreeStatus.ALIVE
    status_note: str = ""
    status_changed_at: datetime | None = None
    status_event_id: str | None = None  # 导致状态变更的现场事件


@dataclass
class Assignment:
    """责任农户关系；变更只新增版本，不改写历史。"""

    assignment_id: str
    tree_id: str
    farmer_id: str
    effective_from: datetime
    effective_to: datetime | None  # None 表示现任
    reason: AdjustReason
    recorded_by: str
    recorded_at: datetime


@dataclass
class ContractVersion:
    contract_id: str
    year: int
    version: str  # 如 "2026-v1"
    milestone_amounts: dict  # MilestoneType -> 附加金（分）
    terms: str
    active: bool = True


@dataclass
class Milestone:
    milestone_id: str
    plan_id: str
    seq: int
    mtype: MilestoneType
    due_date: date
    required_evidence: tuple = ()  # tuple[EvidenceKind]
    status: MilestoneStatus = MilestoneStatus.PENDING
    decided_at: datetime | None = None
    cancel_reason: str = ""


@dataclass
class Adjustment:
    adjustment_id: str
    plan_id: str
    reason: AdjustReason
    changes: list  # [{milestone_id, before, after}]
    operator_id: str
    operated_at: datetime
    note: str = ""


@dataclass
class AnnualPlan:
    plan_id: str
    tree_id: str
    year: int
    contract_id: str
    status: PlanStatus = PlanStatus.DRAFT
    milestone_ids: list = field(default_factory=list)
    adjustment_ids: list = field(default_factory=list)


@dataclass
class EvidenceVersion:
    version: int
    submitted_by: str
    submitted_at: datetime
    payload: dict
    supersedes: int | None  # 被本版本取代的版本号；原始版本恒为 None


@dataclass
class EvidenceItem:
    """同一逻辑证据（里程碑/事件 + 类型 + 标签）的版本链，原始版本永远保留。"""

    evidence_id: str
    milestone_id: str | None
    event_id: str | None
    kind: EvidenceKind
    label: str
    versions: list = field(default_factory=list)  # list[EvidenceVersion]


@dataclass
class Review:
    """验收/复核记录，一经登记不可改写。"""

    review_id: str
    milestone_id: str
    decision: ReviewDecision
    opinion: str
    reviewer_id: str
    reviewed_at: datetime


@dataclass
class Report:
    """一次原始上报（护树队离线巡查或部门报送）。"""

    report_id: str
    source: str
    reporter_id: str
    tree_id: str
    observed_at: datetime
    lat: float
    lng: float
    content: str
    offline_id: str | None  # 离线客户端去重键
    received_at: datetime
    event_id: str


@dataclass
class FieldEvent:
    """合并后的现场事件：同一时间地点窗口内的多源上报只算一次。"""

    event_id: str
    tree_id: str
    occurred_at: datetime
    lat: float
    lng: float
    report_ids: list = field(default_factory=list)
    merged: bool = False


@dataclass
class FundInjection:
    injection_id: str
    milestone_id: str
    tree_id: str
    contract_id: str
    amount_cents: int
    enterprise_id: str
    injected_at: datetime


@dataclass
class AllocationLine:
    line_id: str
    farmer_id: str | None
    injection_id: str
    amount_cents: int
    superseded_by: str | None = None  # 异议更正后指向新分配线
    corrects: str | None = None  # 本线更正的原分配线


@dataclass
class Distribution:
    distribution_id: str
    year: int
    contract_id: str
    line_ids: list = field(default_factory=list)
    run_by: str = ""
    run_at: datetime | None = None


@dataclass
class Dispute:
    dispute_id: str
    line_id: str
    filed_by: str
    reason: str
    status: DisputeStatus = DisputeStatus.OPEN
    resolution: str = ""
    resolved_by: str | None = None
    resolved_at: datetime | None = None
