"""老红橘古树管护兑现引擎：责任、方案、证据、验收、资金与年度结果的业务规则。

核心不变量：
- 证据（照片/定位/病害诊断/复核意见）原始版本永远保留，更正只追加新版本。
- 已通过或已驳回的里程碑事实不随责任人名单或方案调整而改写。
- 换地、责任人变更、极端损伤、方案延期只能由获授权的农技人员调整未来里程碑。
- 护树队离线上报与多部门重复上报合并为一次现场事件。
- 企业只对"已通过且树仍符合保护条件"的里程碑注入附加金。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import asin, cos, radians, sin, sqrt

from domain import (
    PLAN_ADJUST,
    Actor,
    AdjustReason,
    Adjustment,
    AllocationLine,
    AnnualPlan,
    Assignment,
    Conflict,
    ContractVersion,
    Dispute,
    DisputeStatus,
    Distribution,
    DomainError,
    EvidenceItem,
    EvidenceKind,
    EvidenceVersion,
    Farmer,
    FieldEvent,
    Forbidden,
    FundInjection,
    Milestone,
    MilestoneStatus,
    MilestoneType,
    NotFound,
    Outcome,
    PlanStatus,
    Report,
    Restriction,
    Review,
    ReviewDecision,
    Role,
    Tree,
    TreeStatus,
)

# 现场事件合并窗口：同一棵树 48 小时内、150 米以内的上报视为同一次现场。
MERGE_WINDOW = timedelta(hours=48)
MERGE_RADIUS_M = 150.0

# 仍符合保护条件、可注入附加金的树状态。
FUNDABLE_STATUSES = (TreeStatus.ALIVE, TreeStatus.REJUVENATING)


def _distance_m(lat1, lng1, lat2, lng2):
    """haversine 球面距离（米）。"""
    radius = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * radius * asin(sqrt(a))


class GuardianEngine:
    """管护兑现后端的状态与规则入口（内存仓储，便于联调与测试）。"""

    def __init__(self, clock=None):
        self._clock = clock or datetime.now
        self._seq = {}
        self.farmers = {}
        self.trees = {}
        self.assignments = {}
        self.contracts = {}
        self.plans = {}
        self.milestones = {}
        self.evidence = {}
        self.reviews = {}
        self.adjustments = {}
        self.reports = {}
        self.events = {}
        self.injections = {}
        self.distributions = {}
        self.lines = {}
        self.disputes = {}
        self._offline_index = {}  # (source, offline_id) -> report_id
        self._evidence_index = {}  # (scope, kind, label) -> evidence_id
        self._inj_by_milestone = {}  # milestone_id -> injection_id
        self._dist_index = {}  # (year, contract_id) -> distribution_id

    # ---------- 基础工具 ----------

    def _now(self):
        return self._clock()

    def _next(self, prefix):
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}-{self._seq[prefix]:04d}"

    def _get(self, repo, key, what):
        try:
            return repo[key]
        except KeyError:
            raise NotFound(f"{what}不存在: {key}") from None

    def get_tree(self, tree_id):
        return self._get(self.trees, tree_id, "保护树")

    def get_plan(self, plan_id):
        return self._get(self.plans, plan_id, "年度方案")

    def get_milestone(self, milestone_id):
        return self._get(self.milestones, milestone_id, "里程碑")

    def get_event(self, event_id):
        return self._get(self.events, event_id, "现场事件")

    def get_line(self, line_id):
        return self._get(self.lines, line_id, "分配线")

    def get_dispute(self, dispute_id):
        return self._get(self.disputes, dispute_id, "异议")

    @staticmethod
    def _require(actor, *roles):
        if actor.role not in roles:
            raise Forbidden(f"角色 {actor.role.value} 无权执行该操作")

    # ---------- 农户与保护树 ----------

    def register_farmer(self, actor, name, address, village):
        self._require(actor, Role.COOP, Role.TOWN)
        farmer = Farmer(self._next("farmer"), name, address, village)
        self.farmers[farmer.farmer_id] = farmer
        return farmer

    def register_tree(self, actor, code, age_years, lat, lng, village, restrictions=()):
        self._require(actor, Role.COOP, Role.AGRONOMIST, Role.TOWN)
        tree = Tree(
            tree_id=self._next("tree"),
            code=code,
            age_years=int(age_years),
            lat=float(lat),
            lng=float(lng),
            village=village,
            restrictions=tuple(Restriction(r) for r in restrictions),
        )
        self.trees[tree.tree_id] = tree
        return tree

    # ---------- 责任农户（名单变更只追加版本） ----------

    def assign(self, actor, tree_id, farmer_id, effective_from, reason):
        """指定或变更责任农户；历史责任关系保留，已发生的事实仍归属当时的责任人。"""
        self._require(actor, Role.COOP, Role.AGRONOMIST)
        tree = self.get_tree(tree_id)
        farmer = self._get(self.farmers, farmer_id, "农户")
        reason = AdjustReason(reason)
        current = self.current_assignment(tree.tree_id)
        if current is not None:
            if effective_from <= current.effective_from:
                raise Conflict("生效时间必须晚于现任责任关系的生效时间")
            current.effective_to = effective_from
        assignment = Assignment(
            assignment_id=self._next("assign"),
            tree_id=tree.tree_id,
            farmer_id=farmer.farmer_id,
            effective_from=effective_from,
            effective_to=None,
            reason=reason,
            recorded_by=actor.actor_id,
            recorded_at=self._now(),
        )
        self.assignments[assignment.assignment_id] = assignment
        return assignment

    def current_assignment(self, tree_id):
        return self.responsible_at(tree_id, self._now())

    def responsible_at(self, tree_id, when):
        """某时点（如验收通过时刻）的责任农户，用于分配归属。"""
        for assignment in self.assignments.values():
            if assignment.tree_id != tree_id:
                continue
            if assignment.effective_from <= when and (
                assignment.effective_to is None or when < assignment.effective_to
            ):
                return assignment
        return None

    def tree_history(self, actor, tree_id):
        self._require(actor, Role.COOP, Role.AGRONOMIST, Role.TOWN, Role.ENTERPRISE)
        tree = self.get_tree(tree_id)
        history = [a for a in self.assignments.values() if a.tree_id == tree.tree_id]
        history.sort(key=lambda a: a.effective_from)
        return {"tree": tree, "assignments": history}

    # ---------- 合同版本与年度方案 ----------

    def create_contract(self, actor, year, version, milestone_amounts, terms):
        self._require(actor, Role.COOP)
        amounts = {MilestoneType(k): int(v) for k, v in milestone_amounts.items()}
        contract = ContractVersion(
            contract_id=self._next("contract"),
            year=int(year),
            version=version,
            milestone_amounts=amounts,
            terms=terms,
        )
        self.contracts[contract.contract_id] = contract
        return contract

    def create_plan(self, actor, tree_id, year, contract_id, milestones):
        self._require(actor, Role.COOP, Role.AGRONOMIST)
        tree = self.get_tree(tree_id)
        if tree.status in (TreeStatus.DEAD, TreeStatus.EXITED):
            raise Conflict("树已死亡或退出，不能新建年度方案")
        contract = self._get(self.contracts, contract_id, "合同版本")
        if contract.year != int(year):
            raise Conflict("合同版本年份与方案年份不一致")
        if not milestones:
            raise DomainError("年度方案至少需要一个里程碑")
        plan = AnnualPlan(
            plan_id=self._next("plan"),
            tree_id=tree.tree_id,
            year=int(year),
            contract_id=contract.contract_id,
        )
        for seq, spec in enumerate(milestones, start=1):
            milestone = Milestone(
                milestone_id=self._next("ms"),
                plan_id=plan.plan_id,
                seq=seq,
                mtype=MilestoneType(spec["type"]),
                due_date=self._as_date(spec["due_date"]),
                required_evidence=tuple(
                    EvidenceKind(k) for k in spec.get("required_evidence", ())
                ),
            )
            self.milestones[milestone.milestone_id] = milestone
            plan.milestone_ids.append(milestone.milestone_id)
        self.plans[plan.plan_id] = plan
        return plan

    def activate_plan(self, actor, plan_id):
        self._require(actor, Role.COOP, Role.AGRONOMIST)
        plan = self.get_plan(plan_id)
        if plan.status != PlanStatus.DRAFT:
            raise Conflict("只有草稿方案可以生效")
        plan.status = PlanStatus.ACTIVE
        return plan

    def adjust_plan(self, actor, plan_id, reason, changes, note=""):
        """获授权农技人员调整未来里程碑；已通过/已驳回的事实一律不改写。"""
        self._require(actor, Role.AGRONOMIST)
        if PLAN_ADJUST not in actor.permissions:
            raise Forbidden("该农技员未获方案调整授权")
        plan = self.get_plan(plan_id)
        reason = AdjustReason(reason)
        applied = []
        for change in changes:
            milestone = self.get_milestone(change["milestone_id"])
            if milestone.plan_id != plan.plan_id:
                raise DomainError("里程碑不属于该方案")
            if milestone.status != MilestoneStatus.PENDING:
                raise Conflict(
                    f"里程碑 {milestone.milestone_id} 已{milestone.status.value}，事实不可改写"
                )
            before = {
                "due_date": milestone.due_date.isoformat(),
                "required_evidence": [k.value for k in milestone.required_evidence],
            }
            if "due_date" in change:
                milestone.due_date = self._as_date(change["due_date"])
            if "required_evidence" in change:
                milestone.required_evidence = tuple(
                    EvidenceKind(k) for k in change["required_evidence"]
                )
            after = {
                "due_date": milestone.due_date.isoformat(),
                "required_evidence": [k.value for k in milestone.required_evidence],
            }
            applied.append(
                {"milestone_id": milestone.milestone_id, "before": before, "after": after}
            )
        if not applied:
            raise DomainError("调整内容为空")
        adjustment = Adjustment(
            adjustment_id=self._next("adj"),
            plan_id=plan.plan_id,
            reason=reason,
            changes=applied,
            operator_id=actor.actor_id,
            operated_at=self._now(),
            note=note,
        )
        self.adjustments[adjustment.adjustment_id] = adjustment
        plan.adjustment_ids.append(adjustment.adjustment_id)
        return adjustment

    # ---------- 证据（原始版本保留）与验收 ----------

    def _add_evidence(self, actor, scope, kind, label, payload):
        kind = EvidenceKind(kind)
        key = (scope, kind, label)
        evidence_id = self._evidence_index.get(key)
        if evidence_id is None:
            evidence_id = self._next("ev")
            milestone_id = scope[1] if scope[0] == "m" else None
            event_id = scope[1] if scope[0] == "e" else None
            self.evidence[evidence_id] = EvidenceItem(
                evidence_id=evidence_id,
                milestone_id=milestone_id,
                event_id=event_id,
                kind=kind,
                label=label,
            )
            self._evidence_index[key] = evidence_id
        item = self.evidence[evidence_id]
        version = EvidenceVersion(
            version=len(item.versions) + 1,
            submitted_by=actor.actor_id,
            submitted_at=self._now(),
            payload=dict(payload),
            supersedes=len(item.versions) or None,
        )
        item.versions.append(version)
        return item

    def submit_evidence(self, actor, milestone_id, kind, label, payload):
        """提交分阶段证据；重复提交只追加版本，原始版本不覆盖。"""
        if actor.role == Role.PUBLIC:
            raise Forbidden("公众角色不能提交证据")
        milestone = self.get_milestone(milestone_id)
        plan = self.get_plan(milestone.plan_id)
        if plan.status != PlanStatus.ACTIVE:
            raise Conflict("方案未生效，不能提交证据")
        if milestone.status in (MilestoneStatus.ACCEPTED, MilestoneStatus.CANCELLED):
            raise Conflict(f"里程碑已{milestone.status.value}，不能再提交证据")
        item = self._add_evidence(actor, ("m", milestone_id), kind, label, payload)
        milestone.status = MilestoneStatus.SUBMITTED
        return item

    def _milestone_evidence(self, milestone_id, kind):
        for item in self.evidence.values():
            if item.milestone_id == milestone_id and item.kind == kind and item.versions:
                return True
        return False

    def review_milestone(self, actor, milestone_id, decision, opinion):
        """农技员验收；通过/驳回一经登记即冻结，驳回可补证再报但记录保留。"""
        self._require(actor, Role.AGRONOMIST)
        milestone = self.get_milestone(milestone_id)
        if milestone.status != MilestoneStatus.SUBMITTED:
            raise Conflict("只有待验收的里程碑可以验收")
        decision = ReviewDecision(decision)
        if decision == ReviewDecision.ACCEPT:
            missing = [
                k.value
                for k in milestone.required_evidence
                if not self._milestone_evidence(milestone_id, k)
            ]
            if missing:
                raise Conflict(f"缺少必需证据: {', '.join(missing)}")
        review = Review(
            review_id=self._next("rev"),
            milestone_id=milestone_id,
            decision=decision,
            opinion=opinion,
            reviewer_id=actor.actor_id,
            reviewed_at=self._now(),
        )
        self.reviews[review.review_id] = review
        self._add_evidence(
            actor,
            ("m", milestone_id),
            EvidenceKind.REVIEW_OPINION,
            f"验收-{review.review_id}",
            {"decision": decision.value, "opinion": opinion},
        )
        milestone.status = (
            MilestoneStatus.ACCEPTED
            if decision == ReviewDecision.ACCEPT
            else MilestoneStatus.REJECTED
        )
        milestone.decided_at = review.reviewed_at
        return review

    def milestone_detail(self, milestone_id):
        milestone = self.get_milestone(milestone_id)
        items = [
            item
            for item in self.evidence.values()
            if item.milestone_id == milestone_id
        ]
        items.sort(key=lambda i: i.evidence_id)
        reviews = [
            r for r in self.reviews.values() if r.milestone_id == milestone_id
        ]
        reviews.sort(key=lambda r: r.reviewed_at)
        return {"milestone": milestone, "evidence": items, "reviews": reviews}

    # ---------- 现场事件：离线上报去重 + 多源合并 ----------

    def submit_report(
        self, actor, source, tree_id, observed_at, lat, lng, content, offline_id=None
    ):
        """护树队/部门上报；离线键幂等，同树同时同地的上报合并为一次现场事件。"""
        self._require(actor, Role.PATROL, Role.AGRONOMIST, Role.COOP, Role.TOWN)
        tree = self.get_tree(tree_id)
        if offline_id is not None:
            seen = self._offline_index.get((source, offline_id))
            if seen is not None:
                return self.reports[seen]
        event = self._find_event(tree.tree_id, observed_at, lat, lng)
        if event is None:
            event = FieldEvent(
                event_id=self._next("evt"),
                tree_id=tree.tree_id,
                occurred_at=observed_at,
                lat=float(lat),
                lng=float(lng),
            )
            self.events[event.event_id] = event
        report = Report(
            report_id=self._next("rep"),
            source=source,
            reporter_id=actor.actor_id,
            tree_id=tree.tree_id,
            observed_at=observed_at,
            lat=float(lat),
            lng=float(lng),
            content=content,
            offline_id=offline_id,
            received_at=self._now(),
            event_id=event.event_id,
        )
        self.reports[report.report_id] = report
        event.report_ids.append(report.report_id)
        event.merged = len(event.report_ids) > 1
        if offline_id is not None:
            self._offline_index[(source, offline_id)] = report.report_id
        return report

    def _find_event(self, tree_id, observed_at, lat, lng):
        best = None
        for event in self.events.values():
            if event.tree_id != tree_id:
                continue
            if abs(observed_at - event.occurred_at) > MERGE_WINDOW:
                continue
            if _distance_m(event.lat, event.lng, lat, lng) > MERGE_RADIUS_M:
                continue
            if best is None or abs(observed_at - event.occurred_at) < abs(
                observed_at - best.occurred_at
            ):
                best = event
        return best

    # ---------- 极端损伤、死亡与合理退出 ----------

    def record_death(self, actor, tree_id, observed_at, diagnosis, note=""):
        """登记树体死亡：留存病害诊断证据，取消未来里程碑，阻断后续申领。"""
        self._require(actor, Role.AGRONOMIST)
        tree = self.get_tree(tree_id)
        if tree.status in (TreeStatus.DEAD, TreeStatus.EXITED):
            raise Conflict("树已死亡或退出")
        event = FieldEvent(
            event_id=self._next("evt"),
            tree_id=tree.tree_id,
            occurred_at=observed_at,
            lat=tree.lat,
            lng=tree.lng,
        )
        self.events[event.event_id] = event
        self._add_evidence(
            actor, ("e", event.event_id), EvidenceKind.DIAGNOSIS, "死亡诊断", diagnosis
        )
        self._close_tree(tree, TreeStatus.DEAD, note or "树体死亡", event.event_id)
        return event

    def approve_exit(self, actor, tree_id, reason, observed_at=None):
        """合理退出（如不可恢复的极端损伤），留痕后取消未来里程碑。"""
        self._require(actor, Role.AGRONOMIST)
        tree = self.get_tree(tree_id)
        if tree.status in (TreeStatus.DEAD, TreeStatus.EXITED):
            raise Conflict("树已死亡或退出")
        event = FieldEvent(
            event_id=self._next("evt"),
            tree_id=tree.tree_id,
            occurred_at=observed_at or self._now(),
            lat=tree.lat,
            lng=tree.lng,
        )
        self.events[event.event_id] = event
        self._add_evidence(
            actor,
            ("e", event.event_id),
            EvidenceKind.REVIEW_OPINION,
            "退出复核",
            {"reason": reason},
        )
        self._close_tree(tree, TreeStatus.EXITED, reason, event.event_id)
        return event

    def _close_tree(self, tree, status, note, event_id):
        tree.status = status
        tree.status_note = note
        tree.status_changed_at = self._now()
        tree.status_event_id = event_id
        for plan in self.plans.values():
            if plan.tree_id != tree.tree_id or plan.status != PlanStatus.ACTIVE:
                continue
            for milestone_id in plan.milestone_ids:
                milestone = self.milestones[milestone_id]
                if milestone.status in (
                    MilestoneStatus.PENDING,
                    MilestoneStatus.SUBMITTED,
                ):
                    milestone.status = MilestoneStatus.CANCELLED
                    milestone.cancel_reason = note

    # ---------- 企业附加金：只认"已通过且仍符合保护条件" ----------

    def inject_funding(self, actor, milestone_id):
        self._require(actor, Role.ENTERPRISE)
        milestone = self.get_milestone(milestone_id)
        if milestone.status != MilestoneStatus.ACCEPTED:
            raise Conflict("里程碑未通过验收，不能注入附加金")
        plan = self.get_plan(milestone.plan_id)
        tree = self.get_tree(plan.tree_id)
        if tree.status not in FUNDABLE_STATUSES:
            raise Conflict(f"树当前状态为 {tree.status.value}，不再符合保护条件")
        if milestone.milestone_id in self._inj_by_milestone:
            raise Conflict("该里程碑已注入附加金")
        contract = self._get(self.contracts, plan.contract_id, "合同版本")
        amount = contract.milestone_amounts.get(milestone.mtype)
        if amount is None:
            raise Conflict("合同版本未约定该类型里程碑的附加金")
        injection = FundInjection(
            injection_id=self._next("inj"),
            milestone_id=milestone.milestone_id,
            tree_id=tree.tree_id,
            contract_id=contract.contract_id,
            amount_cents=amount,
            enterprise_id=actor.actor_id,
            injected_at=self._now(),
        )
        self.injections[injection.injection_id] = injection
        self._inj_by_milestone[milestone.milestone_id] = injection.injection_id
        return injection

    # ---------- 合作社分配与异议 ----------

    def run_distribution(self, actor, year, contract_id):
        """按合同版本对当年已注入的附加金进行分配；归属验收时刻的责任农户。"""
        self._require(actor, Role.COOP)
        contract = self._get(self.contracts, contract_id, "合同版本")
        key = (int(year), contract.contract_id)
        if key in self._dist_index:
            return self.distributions[self._dist_index[key]]
        distribution = Distribution(
            distribution_id=self._next("dist"),
            year=int(year),
            contract_id=contract.contract_id,
            run_by=actor.actor_id,
            run_at=self._now(),
        )
        for injection in self.injections.values():
            if injection.contract_id != contract.contract_id:
                continue
            milestone = self.milestones[injection.milestone_id]
            plan = self.plans[milestone.plan_id]
            if plan.year != int(year):
                continue
            assignment = self.responsible_at(plan.tree_id, milestone.decided_at)
            line = AllocationLine(
                line_id=self._next("line"),
                farmer_id=assignment.farmer_id if assignment else None,
                injection_id=injection.injection_id,
                amount_cents=injection.amount_cents,
            )
            self.lines[line.line_id] = line
            distribution.line_ids.append(line.line_id)
        self.distributions[distribution.distribution_id] = distribution
        self._dist_index[key] = distribution.distribution_id
        return distribution

    def file_dispute(self, actor, line_id, reason):
        if actor.role in (Role.PUBLIC, Role.ENTERPRISE):
            raise Forbidden("该角色不能发起异议")
        line = self.get_line(line_id)
        dispute = Dispute(
            dispute_id=self._next("disp"),
            line_id=line.line_id,
            filed_by=actor.actor_id,
            reason=reason,
        )
        self.disputes[dispute.dispute_id] = dispute
        return dispute

    def resolve_dispute(self, actor, dispute_id, uphold, note="", adjust_to_farmer_id=None):
        """合作社处理异议；更正只追加新分配线，原分配线保留并标注被取代。"""
        self._require(actor, Role.COOP)
        dispute = self.get_dispute(dispute_id)
        if dispute.status != DisputeStatus.OPEN:
            raise Conflict("异议已处理")
        line = self.get_line(dispute.line_id)
        if not uphold:
            if adjust_to_farmer_id is None:
                raise DomainError("支持异议时必须指定更正后的农户")
            farmer = self._get(self.farmers, adjust_to_farmer_id, "农户")
            new_line = AllocationLine(
                line_id=self._next("line"),
                farmer_id=farmer.farmer_id,
                injection_id=line.injection_id,
                amount_cents=line.amount_cents,
                corrects=line.line_id,
            )
            self.lines[new_line.line_id] = new_line
            line.superseded_by = new_line.line_id
            for distribution in self.distributions.values():
                if line.line_id in distribution.line_ids:
                    distribution.line_ids.append(new_line.line_id)
        dispute.status = DisputeStatus.RESOLVED
        dispute.resolution = note
        dispute.resolved_by = actor.actor_id
        dispute.resolved_at = self._now()
        return dispute

    # ---------- 镇里年度结果与公众视图 ----------

    def annual_outcomes(self, actor, year):
        """按树区分真实存活、复壮、合理退出和漏管。"""
        self._require(actor, Role.TOWN, Role.COOP, Role.AGRONOMIST)
        outcomes = []
        for plan in self.plans.values():
            if plan.year != int(year):
                continue
            tree = self.trees[plan.tree_id]
            milestones = [self.milestones[mid] for mid in plan.milestone_ids]
            if tree.status in (TreeStatus.DEAD, TreeStatus.EXITED):
                outcome = Outcome.EXITED
                detail = tree.status_note
            else:
                active = [
                    m for m in milestones if m.status != MilestoneStatus.CANCELLED
                ]
                unfinished = [
                    m for m in active if m.status != MilestoneStatus.ACCEPTED
                ]
                if not unfinished:
                    rejuvenated = any(
                        m.mtype == MilestoneType.REJUVENATION for m in active
                    )
                    outcome = Outcome.REJUVENATED if rejuvenated else Outcome.SURVIVED
                    detail = ""
                else:
                    outcome = Outcome.NEGLECTED
                    detail = "未完成里程碑: " + ", ".join(
                        f"{m.milestone_id}({m.status.value})" for m in unfinished
                    )
            outcomes.append(
                {
                    "tree_id": tree.tree_id,
                    "code": tree.code,
                    "year": int(year),
                    "outcome": outcome,
                    "detail": detail,
                }
            )
        outcomes.sort(key=lambda o: o["tree_id"])
        return outcomes

    def public_progress(self):
        """公众视图：只给古树保护进展，不暴露农户姓名、住址与精确坐标。"""
        progress = []
        for tree in self.trees.values():
            years = []
            for plan in self.plans.values():
                if plan.tree_id != tree.tree_id:
                    continue
                milestones = [self.milestones[mid] for mid in plan.milestone_ids]
                years.append(
                    {
                        "year": plan.year,
                        "milestones_total": len(milestones),
                        "milestones_accepted": sum(
                            1
                            for m in milestones
                            if m.status == MilestoneStatus.ACCEPTED
                        ),
                    }
                )
            years.sort(key=lambda y: y["year"])
            progress.append(
                {
                    "code": tree.code,
                    "village": tree.village,
                    "approx_lat": round(tree.lat, 2),
                    "approx_lng": round(tree.lng, 2),
                    "age_years": tree.age_years,
                    "status": tree.status.value,
                    "restrictions": [r.value for r in tree.restrictions],
                    "years": years,
                }
            )
        progress.sort(key=lambda p: p["code"])
        return progress

    # ---------- 解析辅助 ----------

    @staticmethod
    def _as_date(value):
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)
