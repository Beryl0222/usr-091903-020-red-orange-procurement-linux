"""管护兑现引擎的领域规则测试。"""

import unittest
from datetime import datetime

from domain import (
    PLAN_ADJUST,
    Actor,
    Conflict,
    EvidenceKind,
    Forbidden,
    MilestoneStatus,
    MilestoneType,
    Outcome,
    Role,
    TreeStatus,
)
from guardian import GuardianEngine

NOW = datetime(2026, 3, 1, 9, 0, 0)

COOP = Actor("coop-1", Role.COOP)
AGRO = Actor("agro-1", Role.AGRONOMIST, frozenset({PLAN_ADJUST}))
AGRO_PLAIN = Actor("agro-2", Role.AGRONOMIST)
ENTERPRISE = Actor("ent-1", Role.ENTERPRISE)
PATROL = Actor("patrol-1", Role.PATROL)
TOWN = Actor("town-1", Role.TOWN)

AMOUNTS = {"pruning": 30000, "disease_control": 20000, "rejuvenation": 50000}


def make_engine():
    return GuardianEngine(clock=lambda: NOW)


class Scenario:
    """搭好一户一树一合同一方案（修剪/防病/复壮三个里程碑）。"""

    def __init__(self, engine, code="GX-001", amounts=None):
        self.engine = engine
        self.farmer = engine.register_farmer(COOP, "张三", "广兴镇幸福村3组12号", "幸福村")
        self.tree = engine.register_tree(
            COOP, code, 120, 30.123456, 120.654321, "幸福村",
            ["no_picking", "scion_limit"],
        )
        engine.assign(
            COOP, self.tree.tree_id, self.farmer.farmer_id,
            datetime(2026, 1, 1), "responsibility_change",
        )
        self.contract = engine.create_contract(
            COOP, 2026, "2026-v1", amounts or AMOUNTS, "保护价附加金条款"
        )
        self.plan = engine.create_plan(
            AGRO, self.tree.tree_id, 2026, self.contract.contract_id,
            [
                {"type": "pruning", "due_date": "2026-04-01",
                 "required_evidence": ["photo", "location"]},
                {"type": "disease_control", "due_date": "2026-06-01",
                 "required_evidence": ["photo", "diagnosis"]},
                {"type": "rejuvenation", "due_date": "2026-09-01",
                 "required_evidence": ["photo"]},
            ],
        )
        engine.activate_plan(COOP, self.plan.plan_id)
        self.m1, self.m2, self.m3 = self.plan.milestone_ids

    def accept(self, milestone_id, kinds=("photo", "location", "diagnosis")):
        milestone = self.engine.milestones[milestone_id]
        for kind in kinds:
            if kind in [k.value for k in milestone.required_evidence]:
                self.engine.submit_evidence(
                    PATROL, milestone_id, kind, f"{kind}-1", {"ref": kind}
                )
        return self.engine.review_milestone(AGRO, milestone_id, "accept", "符合要求")


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.scenario = Scenario(make_engine())

    def test_original_version_is_kept_when_resubmitting(self):
        engine = self.scenario.engine
        first = engine.submit_evidence(
            PATROL, self.scenario.m1, "photo", "修剪照", {"url": "a.jpg"}
        )
        again = engine.submit_evidence(
            PATROL, self.scenario.m1, "photo", "修剪照", {"url": "b.jpg"}
        )
        self.assertEqual(first.evidence_id, again.evidence_id)
        self.assertEqual(len(again.versions), 2)
        self.assertEqual(again.versions[0].payload, {"url": "a.jpg"})
        self.assertIsNone(again.versions[0].supersedes)
        self.assertEqual(again.versions[1].supersedes, 1)

    def test_accept_requires_all_required_evidence(self):
        engine = self.scenario.engine
        engine.submit_evidence(PATROL, self.scenario.m1, "photo", "p", {"url": "a"})
        with self.assertRaises(Conflict):
            engine.review_milestone(AGRO, self.scenario.m1, "accept", "缺定位")

    def test_decided_milestone_is_frozen(self):
        engine = self.scenario.engine
        self.scenario.accept(self.scenario.m1)
        with self.assertRaises(Conflict):
            engine.submit_evidence(
                PATROL, self.scenario.m1, "photo", "p", {"url": "x"}
            )
        with self.assertRaises(Conflict):
            engine.review_milestone(AGRO, self.scenario.m1, "reject", "改判")

    def test_rejection_is_recorded_and_resubmission_keeps_history(self):
        engine = self.scenario.engine
        engine.submit_evidence(PATROL, self.scenario.m1, "photo", "p", {"url": "a"})
        engine.submit_evidence(PATROL, self.scenario.m1, "location", "l", {"lat": 1})
        engine.review_milestone(AGRO, self.scenario.m1, "reject", "修剪不达标")
        engine.submit_evidence(PATROL, self.scenario.m1, "photo", "p", {"url": "b"})
        self.scenario.accept(self.scenario.m1)
        detail = engine.milestone_detail(self.scenario.m1)
        self.assertEqual([r.decision.value for r in detail["reviews"]],
                         ["reject", "accept"])


class AdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.scenario = Scenario(make_engine())

    def test_authorized_agronomist_adjusts_only_pending_milestones(self):
        engine = self.scenario.engine
        self.scenario.accept(self.scenario.m1)
        adjustment = engine.adjust_plan(
            AGRO, self.scenario.plan.plan_id, "postponement",
            [{"milestone_id": self.scenario.m2, "due_date": "2026-07-15"}],
            note="汛期延期",
        )
        self.assertEqual(
            engine.milestones[self.scenario.m2].due_date.isoformat(), "2026-07-15"
        )
        self.assertEqual(adjustment.changes[0]["before"]["due_date"], "2026-06-01")
        with self.assertRaises(Conflict):
            engine.adjust_plan(
                AGRO, self.scenario.plan.plan_id, "postponement",
                [{"milestone_id": self.scenario.m1, "due_date": "2026-05-01"}],
            )

    def test_adjust_requires_permission(self):
        with self.assertRaises(Forbidden):
            self.scenario.engine.adjust_plan(
                AGRO_PLAIN, self.scenario.plan.plan_id, "relocation",
                [{"milestone_id": self.scenario.m2, "due_date": "2026-07-01"}],
            )
        with self.assertRaises(Forbidden):
            self.scenario.engine.adjust_plan(
                COOP, self.scenario.plan.plan_id, "relocation",
                [{"milestone_id": self.scenario.m2, "due_date": "2026-07-01"}],
            )


class RosterChangeTest(unittest.TestCase):
    def test_roster_change_does_not_rewrite_decided_facts(self):
        engine = make_engine()
        scenario = Scenario(engine)
        scenario.accept(scenario.m1)  # 验收时刻为 NOW，责任人是张三
        newcomer = engine.register_farmer(COOP, "李四", "广兴镇幸福村8组3号", "幸福村")
        engine.assign(
            COOP, scenario.tree.tree_id, newcomer.farmer_id,
            datetime(2026, 7, 1), "relocation",
        )
        engine.inject_funding(ENTERPRISE, scenario.m1)
        distribution = engine.run_distribution(COOP, 2026, scenario.contract.contract_id)
        lines = [engine.lines[lid] for lid in distribution.line_ids]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].farmer_id, scenario.farmer.farmer_id)
        history = engine.tree_history(COOP, scenario.tree.tree_id)["assignments"]
        self.assertEqual(len(history), 2)
        self.assertIsNotNone(history[0].effective_to)
        self.assertIsNone(history[1].effective_to)


class FieldEventMergeTest(unittest.TestCase):
    def test_offline_and_department_reports_merge_into_one_event(self):
        engine = make_engine()
        scenario = Scenario(engine)
        patrol = engine.submit_report(
            PATROL, "护树队", scenario.tree.tree_id,
            datetime(2026, 5, 1, 8, 0), 30.1234, 120.6543, "巡查正常",
            offline_id="patrol-2026-05-01-01",
        )
        department = engine.submit_report(
            TOWN, "镇农业服务中心", scenario.tree.tree_id,
            datetime(2026, 5, 1, 20, 0), 30.1235, 120.6544, "部门复查",
        )
        self.assertEqual(patrol.event_id, department.event_id)
        event = engine.events[patrol.event_id]
        self.assertTrue(event.merged)
        self.assertEqual(len(event.report_ids), 2)

    def test_offline_resync_is_idempotent(self):
        engine = make_engine()
        scenario = Scenario(engine)
        first = engine.submit_report(
            PATROL, "护树队", scenario.tree.tree_id,
            datetime(2026, 5, 1, 8, 0), 30.1234, 120.6543, "巡查",
            offline_id="patrol-x",
        )
        resent = engine.submit_report(
            PATROL, "护树队", scenario.tree.tree_id,
            datetime(2026, 5, 1, 8, 0), 30.1234, 120.6543, "巡查",
            offline_id="patrol-x",
        )
        self.assertEqual(first.report_id, resent.report_id)
        self.assertEqual(len(engine.reports), 1)

    def test_distant_or_late_report_starts_new_event(self):
        engine = make_engine()
        scenario = Scenario(engine)
        first = engine.submit_report(
            PATROL, "护树队", scenario.tree.tree_id,
            datetime(2026, 5, 1, 8, 0), 30.1234, 120.6543, "巡查",
        )
        far = engine.submit_report(
            TOWN, "镇里", scenario.tree.tree_id,
            datetime(2026, 5, 1, 9, 0), 30.1334, 120.6643, "另一处",
        )
        late = engine.submit_report(
            TOWN, "镇里", scenario.tree.tree_id,
            datetime(2026, 5, 10, 9, 0), 30.1234, 120.6543, "隔周复查",
        )
        self.assertNotEqual(first.event_id, far.event_id)
        self.assertNotEqual(first.event_id, late.event_id)
        self.assertEqual(len(engine.events), 3)


class FundingTest(unittest.TestCase):
    def setUp(self):
        self.scenario = Scenario(make_engine())

    def test_funding_follows_contract_version_and_is_one_off(self):
        engine = self.scenario.engine
        self.scenario.accept(self.scenario.m1)
        injection = engine.inject_funding(ENTERPRISE, self.scenario.m1)
        self.assertEqual(injection.amount_cents, 30000)
        self.assertEqual(injection.contract_id, self.scenario.contract.contract_id)
        with self.assertRaises(Conflict):
            engine.inject_funding(ENTERPRISE, self.scenario.m1)

    def test_funding_requires_acceptance_and_enterprise_role(self):
        engine = self.scenario.engine
        with self.assertRaises(Conflict):
            engine.inject_funding(ENTERPRISE, self.scenario.m1)
        self.scenario.accept(self.scenario.m1)
        with self.assertRaises(Forbidden):
            engine.inject_funding(COOP, self.scenario.m1)

    def test_dead_tree_cannot_claim_next_season(self):
        engine = self.scenario.engine
        self.scenario.accept(self.scenario.m1)
        engine.record_death(
            AGRO, self.scenario.tree.tree_id, datetime(2026, 5, 20),
            {"cause": "雷击致主干劈裂", "diagnosed_by": "agro-1"},
        )
        tree = self.scenario.tree
        self.assertEqual(tree.status, TreeStatus.DEAD)
        # 未验收的里程碑被取消，下一季无法继续申领
        self.assertEqual(
            engine.milestones[self.scenario.m2].status, MilestoneStatus.CANCELLED
        )
        with self.assertRaises(Conflict):
            engine.submit_evidence(
                PATROL, self.scenario.m2, "photo", "p", {"url": "x"}
            )
        # 死亡前已通过的里程碑也不再符合保护条件
        with self.assertRaises(Conflict):
            engine.inject_funding(ENTERPRISE, self.scenario.m1)
        # 死亡诊断作为事件证据留痕
        diagnoses = [
            e for e in engine.evidence.values()
            if e.kind == EvidenceKind.DIAGNOSIS and e.event_id == tree.status_event_id
        ]
        self.assertEqual(len(diagnoses), 1)


class DistributionTest(unittest.TestCase):
    def test_distribution_is_idempotent_and_dispute_keeps_original_line(self):
        engine = make_engine()
        scenario = Scenario(engine)
        scenario.accept(scenario.m1)
        scenario.accept(scenario.m2)
        engine.inject_funding(ENTERPRISE, scenario.m1)
        engine.inject_funding(ENTERPRISE, scenario.m2)
        distribution = engine.run_distribution(COOP, 2026, scenario.contract.contract_id)
        again = engine.run_distribution(COOP, 2026, scenario.contract.contract_id)
        self.assertEqual(distribution.distribution_id, again.distribution_id)
        self.assertEqual(len(distribution.line_ids), 2)

        other = engine.register_farmer(COOP, "王五", "广兴镇幸福村5组9号", "幸福村")
        dispute = engine.file_dispute(
            Actor(scenario.farmer.farmer_id, Role.FARMER),
            distribution.line_ids[0], "责任期认定有误",
        )
        with self.assertRaises(Forbidden):
            engine.resolve_dispute(ENTERPRISE, dispute.dispute_id, True)
        engine.resolve_dispute(
            COOP, dispute.dispute_id, False,
            note="更正归属", adjust_to_farmer_id=other.farmer_id,
        )
        original = engine.lines[distribution.line_ids[0]]
        self.assertIsNotNone(original.superseded_by)
        corrected = engine.lines[original.superseded_by]
        self.assertEqual(corrected.farmer_id, other.farmer_id)
        self.assertEqual(corrected.corrects, original.line_id)
        self.assertEqual(dispute.status.value, "resolved")


class OutcomeTest(unittest.TestCase):
    def test_annual_outcomes_distinguish_four_categories(self):
        engine = make_engine()
        survived = Scenario(engine, code="GX-101")
        for milestone_id in survived.plan.milestone_ids[:2]:
            survived.accept(milestone_id)
        survived.engine.milestones[survived.m3].status = MilestoneStatus.CANCELLED
        survived.engine.milestones[survived.m3].cancel_reason = "树体状况良好无需复壮"

        rejuvenated = Scenario(engine, code="GX-102")
        for milestone_id in rejuvenated.plan.milestone_ids:
            rejuvenated.accept(milestone_id)

        exited = Scenario(engine, code="GX-103")
        engine.approve_exit(AGRO, exited.tree.tree_id, "台风倒伏不可恢复")

        neglected = Scenario(engine, code="GX-104")

        outcomes = {
            item["code"]: item for item in engine.annual_outcomes(TOWN, 2026)
        }
        self.assertEqual(outcomes["GX-101"]["outcome"], Outcome.SURVIVED)
        self.assertEqual(outcomes["GX-102"]["outcome"], Outcome.REJUVENATED)
        self.assertEqual(outcomes["GX-103"]["outcome"], Outcome.EXITED)
        self.assertEqual(outcomes["GX-104"]["outcome"], Outcome.NEGLECTED)
        self.assertIn("ms-", outcomes["GX-104"]["detail"])
        with self.assertRaises(Forbidden):
            engine.annual_outcomes(ENTERPRISE, 2026)


class PublicViewTest(unittest.TestCase):
    def test_public_progress_hides_farmer_and_precise_location(self):
        engine = make_engine()
        scenario = Scenario(engine)
        scenario.accept(scenario.m1)
        progress = engine.public_progress()
        self.assertEqual(len(progress), 1)
        view = progress[0]
        self.assertEqual(view["code"], "GX-001")
        self.assertEqual(view["approx_lat"], 30.12)
        self.assertEqual(view["years"][0]["milestones_accepted"], 1)
        forbidden_keys = {"address", "farmer_id", "name", "lat", "lng"}
        self.assertTrue(forbidden_keys.isdisjoint(view.keys()))


if __name__ == "__main__":
    unittest.main()
