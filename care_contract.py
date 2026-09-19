"""管护兑现领域规则测试。

覆盖需求中的关键承诺：
1. 照片/定位/诊断/复核意见保留原始版本；
2. 责任人变更只对未来生效，已验收/驳回事实与拨付快照不被名单改写；
3. 已验收或驳回的里程碑不可调整，未来里程碑调整需专门权限；
4. 死树次季不能再申报、不能再拨付；当季已通过事实保留；
5. 离线重试与多部门重复上报合并为一次现场事件；
6. 企业只拨付已通过且仍符合保护条件、未拨付过的里程碑，按合同版本出账；
7. 异议不回写事实；
8. 年度结果区分真实存活/复壮/合理退出/漏管；
9. 公众视图不暴露农户住址与精确坐标。
"""

import unittest
from datetime import date, timedelta

import care
from care import (
    PERM_ADJUST_MILESTONE,
    ROLE_ADMIN,
    ROLE_AGRONOMIST,
    ROLE_COOP,
    ROLE_ENTERPRISE,
    ROLE_GUARDIAN,
    ROLE_TOWN,
    STAGE_DISEASE,
    STAGE_PRUNE,
    STAGE_REJUVENATE,
    STATUS_DEAD,
    SUBMIT_APPROVED,
    SUBMIT_PENDING,
    SUBMIT_REJECTED,
    INC_CARE,
    INC_EXTREME_DAMAGE,
    INC_INSPECTION,
    INC_TREE_DEATH,
    INC_BREACH,
    EV_DIAGNOSIS,
    EV_GPS,
    EV_PHOTO,
    YEAR_NEGLECTED,
    YEAR_REAL_ALIVE,
    YEAR_REASONABLE_EXIT,
    YEAR_REJUVENATED,
    Actor,
    DomainError,
    Store,
    active_contract,
    add_evidence,
    adjust_future_milestones,
    annual_report,
    assign_responsibility,
    classify_tree,
    confirm_breach,
    confirm_disbursement,
    create_disbursement,
    decide_protection_status,
    open_dispute,
    public_report,
    publish_contract,
    publish_plan,
    record_allocation,
    register_farmer,
    register_tree,
    report_incident,
    resolve_dispute,
    review_milestone,
    set_clock,
    state,
    submit_milestone,
)


class Clock:
    def __init__(self, day: date):
        self.day = day

    def __call__(self) -> date:
        return self.day

    def advance(self, days: int):
        self.day += timedelta(days=days)


CLOCK = Clock(date(2026, 3, 1))


def actors():
    return {
        "admin": Actor("u_admin", "管理员", ROLE_ADMIN),
        "ag": Actor("u_ag", "李农技", ROLE_AGRONOMIST, frozenset({PERM_ADJUST_MILESTONE})),
        "ag_readonly": Actor("u_ag2", "王农技", ROLE_AGRONOMIST),
        "guard": Actor("u_guard", "张护树", ROLE_GUARDIAN),
        "guard2": Actor("u_guard3", "赵护树", ROLE_GUARDIAN),
        "coop": Actor("u_coop", "合作社小陈", ROLE_COOP),
        "ent": Actor("u_ent", "企业代表", ROLE_ENTERPRISE),
        "town": Actor("u_town", "镇干部", ROLE_TOWN),
    }


AMOUNTS = {STAGE_PRUNE: 100, STAGE_DISEASE: 120, STAGE_REJUVENATE: 200}


def stages():
    return [
        {"code": STAGE_PRUNE, "name": "春季修剪", "scheduled_at": "2026-03-10"},
        {"code": STAGE_DISEASE, "name": "防病", "scheduled_at": "2026-05-10"},
        {"code": STAGE_REJUVENATE, "name": "低产树复壮", "scheduled_at": "2026-09-10"},
    ]


def new_tree(store, a, code, season="2026", farmer_name="果农甲", with_plan=True, stage_list=None):
    tree = register_tree(
        store, a["town"], code, 105, season,
        village="广兴镇红光村", plot_hint="河东老院子片区",
        exact_location={"lat": 30.1, "lng": 106.2},
    )
    farmer = register_farmer(store, a["coop"], farmer_name)
    assign_responsibility(
        store, a["coop"], tree["tree_id"], farmer["farmer_id"], season, reason="初始承包"
    )
    if with_plan:
        publish_plan(
            store, a["ag"], tree["tree_id"], season,
            stage_list or stages(),
            restrictions=[{"type": "NO_HARVEST"}, {"type": "NO_SCION"}],
        )
    return tree, farmer


def contract(store, a, season, tree_ids, amounts=None, version_note=""):
    return publish_contract(
        store, a["ent"], season, amounts or AMOUNTS, tree_ids, note=version_note
    )


def approve_stage(store, a, tree_id, stage, season="2026", *, diagnosis=False, when="2026-03-05"):
    inc = report_incident(
        store, a["guard"], tree_id, INC_CARE, when,
        dedup_key=f"{tree_id}-{season}-{stage}", season=season, dept="护树队",
    )
    inc_id = inc["incident_id"]
    photo = add_evidence(store, a["guard"], inc_id, EV_PHOTO, f"sha-photo-{tree_id}-{stage}", when)
    gps = add_evidence(store, a["guard"], inc_id, EV_GPS, f"sha-gps-{tree_id}-{stage}", when)
    evidence_ids = [photo["evidence_id"], gps["evidence_id"]]
    if diagnosis:
        diag = add_evidence(
            store, a["guard2"] if False else a["ag"], inc_id, EV_DIAGNOSIS,
            f"sha-diag-{tree_id}-{stage}", when, metadata={"diagnosis": "炭疽病轻度"},
        )
        evidence_ids.append(diag["evidence_id"])
    sub = submit_milestone(
        store, a["guard"], tree_id, season, stage, inc_id, evidence_ids
    )
    review_milestone(store, a["ag"], sub["submission_id"], SUBMIT_APPROVED, note="现场达标")
    return sub, inc_id


class CareDomainTest(unittest.TestCase):
    def setUp(self):
        set_clock(CLOCK)
        CLOCK.day = date(2026, 3, 1)

    # -- 1. 完整兑现链路 ----------------------------------------------------

    def test_happy_path_disbursement_and_allocation(self):
        store = Store()
        a = actors()
        tree, farmer = new_tree(store, a, "HX-001")
        contract(store, a, "2026", [tree["tree_id"]])

        sub, _ = approve_stage(store, a, tree["tree_id"], STAGE_PRUNE, diagnosis=True)
        st = state(store)
        self.assertEqual(st.submissions[sub["submission_id"]]["verdict"], SUBMIT_APPROVED)

        batch = create_disbursement(store, a["ent"], "2026")
        self.assertEqual(len(batch["lines"]), 1)
        line = batch["lines"][0]
        self.assertEqual(line["amount"], 100)
        self.assertEqual(line["contract_version"], 1)
        self.assertEqual(line["farmer_snapshot"]["farmer_id"], farmer["farmer_id"])
        confirm_disbursement(store, a["ent"], batch["batch_id"])
        record_allocation(
            store, a["coop"], batch["batch_id"],
            [{"farmer_id": farmer["farmer_id"], "amount": 100}],
        )
        st = state(store)
        self.assertEqual(st.batches[batch["batch_id"]]["status"], "CONFIRMED")

    # -- 2. 原始证据版本不可改 ---------------------------------------------

    def test_evidence_original_versions_are_append_only(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-002")
        inc = report_incident(
            store, a["guard"], tree["tree_id"], INC_INSPECTION, "2026-03-02",
            dedup_key="HX-002-inspection", season="2026",
        )
        add_evidence(store, a["guard"], inc["incident_id"], EV_PHOTO, "sha-v1", "2026-03-02")
        add_evidence(store, a["guard"], inc["incident_id"], EV_PHOTO, "sha-v2", "2026-03-03")
        with self.assertRaises(AuthzOrInput(DomainError)):
            add_evidence(store, a["guard"], inc["incident_id"], EV_DIAGNOSIS, "sha-x", "2026-03-03")

        photo_events = [
            e for e in store.events
            if e["type"] == "EVIDENCE_ADDED" and e["payload"]["kind"] == EV_PHOTO
        ]
        self.assertEqual([e["payload"]["sha256"] for e in photo_events], ["sha-v1", "sha-v2"])
        st = state(store)
        self.assertEqual(len(st.incidents[inc["incident_id"]]["evidence_ids"]), 2)

    # -- 3. 责任人变更不溯及既往 -------------------------------------------

    def test_responsibility_change_does_not_rewrite_facts_or_snapshots(self):
        store = Store()
        a = actors()
        tree, farmer_a = new_tree(store, a, "HX-003", farmer_name="农户甲")
        contract(store, a, "2026", [tree["tree_id"]])
        sub, _ = approve_stage(store, a, tree["tree_id"], STAGE_PRUNE)

        farmer_b = register_farmer(store, a["coop"], "农户乙")
        # 果农换地：乙自 2027 年起接管
        assign_responsibility(
            store, a["coop"], tree["tree_id"], farmer_b["farmer_id"],
            "2027", reason="果农换地",
        )
        # 历史责任版本不能被覆盖
        with self.assertRaises(DomainError):
            assign_responsibility(
                store, a["coop"], tree["tree_id"], farmer_b["farmer_id"], "2026"
            )

        batch = create_disbursement(store, a["ent"], "2026")
        # 拨付快照仍指向验收事实形成时的责任人甲，尽管当前责任人已是乙
        self.assertEqual(batch["lines"][0]["farmer_snapshot"]["farmer_id"], farmer_a["farmer_id"])

        st = state(store)
        self.assertEqual(
            care.responsible_farmer(st, tree["tree_id"], "2026")["id"], farmer_a["farmer_id"]
        )
        self.assertEqual(
            care.responsible_farmer(st, tree["tree_id"], "2027")["id"], farmer_b["farmer_id"]
        )
        # 原始提交事实中的快照未被改写
        self.assertEqual(
            st.submissions[sub["submission_id"]]["farmer_snapshot"]["farmer_id"],
            farmer_a["farmer_id"],
        )

    # -- 4. 未来里程碑调整权限与事实锁 -------------------------------------

    def test_future_milestone_adjustment_requires_permission_and_respects_locks(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-004")

        # 无调整权限的农技员不能改
        with self.assertRaises(DomainError) as cm:
            adjust_future_milestones(
                store, a["ag_readonly"], tree["tree_id"], "2026",
                [{"stage_code": STAGE_PRUNE, "new_scheduled_at": "2026-04-01"}],
                reason="春寒延期",
            )
        self.assertEqual(cm.exception.code, "forbidden")

        # 有权限但缺原因 / 改到过去 都被拒绝
        with self.assertRaises(DomainError):
            adjust_future_milestones(
                store, a["ag"], tree["tree_id"], "2026",
                [{"stage_code": STAGE_PRUNE, "new_scheduled_at": "2026-04-01"}], reason="  ",
            )
        with self.assertRaises(DomainError):
            adjust_future_milestones(
                store, a["ag"], tree["tree_id"], "2026",
                [{"stage_code": STAGE_PRUNE, "new_scheduled_at": "2026-02-01"}], reason="延期",
            )

        adjust_future_milestones(
            store, a["ag"], tree["tree_id"], "2026",
            [{"stage_code": STAGE_PRUNE, "new_scheduled_at": "2026-04-01",
              "new_requirement": "花后复剪一次"}],
            reason="春寒延期",
        )
        st = state(store)
        plan = st.plans[(tree["tree_id"], "2026")]
        prune = next(s for s in plan["stages"] if s["code"] == STAGE_PRUNE)
        self.assertEqual(prune["scheduled_at"], "2026-04-01")
        self.assertEqual(prune["requirement"], "花后复剪一次")
        self.assertEqual(len(plan["adjustments"]), 1)

        # 已验收的里程碑不可再调
        approve_stage(store, a, tree["tree_id"], STAGE_PRUNE, when="2026-04-02")
        with self.assertRaises(DomainError) as cm:
            adjust_future_milestones(
                store, a["ag"], tree["tree_id"], "2026",
                [{"stage_code": STAGE_PRUNE, "new_scheduled_at": "2026-05-01"}],
                reason="再延一次",
            )
        self.assertEqual(cm.exception.code, "fact_locked")

    def test_rejected_milestone_is_locked_but_resubmission_keeps_history(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-005")
        contract(store, a, "2026", [tree["tree_id"]])
        inc = report_incident(
            store, a["guard"], tree["tree_id"], INC_CARE, "2026-03-05",
            dedup_key="hx5-prune", season="2026",
        )
        photo = add_evidence(store, a["guard"], inc["incident_id"], EV_PHOTO, "sha-bad", "2026-03-05")
        sub1 = submit_milestone(
            store, a["guard"], tree["tree_id"], "2026", STAGE_PRUNE,
            inc["incident_id"], [photo["evidence_id"]],
        )
        review_milestone(
            store, a["ag"], sub1["submission_id"], SUBMIT_REJECTED, note="剪口不符规范"
        )
        # 驳回结论本身不可改写
        with self.assertRaises(DomainError):
            review_milestone(store, a["ag"], sub1["submission_id"], SUBMIT_APPROVED)
        # 驳回里程碑也不能被“调整”抹掉
        with self.assertRaises(DomainError):
            adjust_future_milestones(
                store, a["ag"], tree["tree_id"], "2026",
                [{"stage_code": STAGE_PRUNE, "new_scheduled_at": "2026-04-01"}],
                reason="想改判",
            )
        # 整改后重新申报：新事实，旧驳回原样保留
        photo2 = add_evidence(store, a["guard"], inc["incident_id"], EV_PHOTO, "sha-good", "2026-03-20")
        sub2 = submit_milestone(
            store, a["guard"], tree["tree_id"], "2026", STAGE_PRUNE,
            inc["incident_id"], [photo2["evidence_id"]],
        )
        review_milestone(store, a["ag"], sub2["submission_id"], SUBMIT_APPROVED, note="整改达标")
        st = state(store)
        self.assertEqual(st.submissions[sub1["submission_id"]]["verdict"], SUBMIT_REJECTED)
        self.assertEqual(st.submissions[sub2["submission_id"]]["verdict"], SUBMIT_APPROVED)

    # -- 5. 死树：当季事实保留，次季全面拦截 --------------------------------

    def test_dead_tree_keeps_current_season_fact_but_blocks_next_season(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-100")
        contract(store, a, "2026", [tree["tree_id"]])
        # 2026 年三个阶段全部完成并通过
        for stage, when in (
            (STAGE_PRUNE, "2026-03-05"),
            (STAGE_DISEASE, "2026-05-05"),
            (STAGE_REJUVENATE, "2026-09-05"),
        ):
            approve_stage(store, a, tree["tree_id"], stage, when=when)

        # 2026 年底树死亡，裁决自 2027 年起终止保护
        death = report_incident(
            store, a["guard"], tree["tree_id"], INC_TREE_DEATH, "2026-12-20",
            dedup_key="hx100-death", season="2026",
        )
        decide_protection_status(
            store, a["ag"], tree["tree_id"], STATUS_DEAD, "2027",
            death["incident_id"], note="主干枯死",
        )

        # 当季（2026）已通过里程碑仍可拨付
        batch = create_disbursement(store, a["ent"], "2026")
        self.assertEqual(len(batch["lines"]), 3)

        # 次季：不能编方案、不能申报
        with self.assertRaises(DomainError) as cm:
            publish_plan(store, a["ag"], tree["tree_id"], "2027", stages())
        self.assertEqual(cm.exception.code, "tree_not_protected")
        fake_inc = report_incident(
            store, a["guard"], tree["tree_id"], INC_CARE, "2027-03-05",
            dedup_key="hx100-fake", season="2027",
        )
        photo = add_evidence(store, a["guard"], fake_inc["incident_id"], EV_PHOTO, "x", "2027-03-05")
        with self.assertRaises(DomainError) as cm:
            submit_milestone(
                store, a["guard"], tree["tree_id"], "2027", STAGE_PRUNE,
                fake_inc["incident_id"], [photo["evidence_id"]],
            )
        self.assertEqual(cm.exception.code, "tree_not_protected")

        # 年度分类：2026 三阶段（含复壮）全通过=复壮，2027 合理退出
        st = state(store)
        self.assertEqual(classify_tree(st, tree["tree_id"], "2026")["category"], YEAR_REJUVENATED)
        self.assertEqual(
            classify_tree(st, tree["tree_id"], "2027")["category"], YEAR_REASONABLE_EXIT
        )

    def test_death_reported_too_late_counts_as_neglected(self):
        # 2025 年就死了，2027 年才上报并裁决：不是合理退出，是漏管
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-101", season="2025")
        death = report_incident(
            store, a["guard"], tree["tree_id"], INC_TREE_DEATH, "2025-08-01",
            dedup_key="hx101-death", season="2025", channel="OFFLINE",
        )
        decide_protection_status(
            store, a["ag"], tree["tree_id"], STATUS_DEAD, "2027", death["incident_id"]
        )
        st = state(store)
        self.assertEqual(classify_tree(st, tree["tree_id"], "2027")["category"], YEAR_NEGLECTED)

    # -- 6. 现场事件合并 ----------------------------------------------------

    def test_offline_retry_and_cross_department_reports_merge(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-200", with_plan=False)

        first = report_incident(
            store, a["guard"], tree["tree_id"], INC_INSPECTION, "2026-03-08",
            dedup_key="client-uuid-1", season="2026", dept="护树队",
        )
        self.assertFalse(first["merged"])
        # 护树队断网重试：同一幂等键，离线补报
        retry = report_incident(
            store, a["guard2"], tree["tree_id"], INC_INSPECTION, "2026-03-08",
            dedup_key="client-uuid-1", season="2026", dept="护树队", channel="OFFLINE",
        )
        self.assertTrue(retry["merged"])
        # 农技站同日同树同类型重复上报：不同幂等键也应合并
        cross = report_incident(
            store, a["ag"], tree["tree_id"], INC_INSPECTION, "2026-03-08",
            dedup_key="agri-station-9", season="2026", dept="镇农技站",
        )
        self.assertTrue(cross["merged"])
        # 同日但不同类型：独立事件
        other = report_incident(
            store, a["guard"], tree["tree_id"], INC_CARE, "2026-03-08",
            dedup_key="client-uuid-2", season="2026",
        )
        self.assertFalse(other["merged"])

        st = state(store)
        merged = st.incidents[first["incident_id"]]
        self.assertEqual(len(merged["sources"]), 3)
        self.assertEqual({s["dept"] for s in merged["sources"]}, {"护树队", "镇农技站"})
        self.assertTrue(any(s["channel"] == "OFFLINE" for s in merged["sources"]))
        self.assertEqual(
            sum(1 for i in st.incidents.values() if i["tree_id"] == tree["tree_id"]), 2
        )

    # -- 7. 拨付资格：违约拦截、重复拦截、合同版本 --------------------------

    def test_breach_season_is_not_disbursable(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-300")
        contract(store, a, "2026", [tree["tree_id"]])
        sub, _ = approve_stage(store, a, tree["tree_id"], STAGE_PRUNE)

        breach_inc = report_incident(
            store, a["guard"], tree["tree_id"], INC_BREACH, "2026-04-01",
            dedup_key="hx300-breach", season="2026", note="发现违规采穗",
        )
        confirm_breach(
            store, a["ag"], tree["tree_id"], "2026", "NO_SCION", breach_inc["incident_id"]
        )
        # 自动汇总时被排除
        with self.assertRaises(DomainError) as cm:
            create_disbursement(store, a["ent"], "2026")
        self.assertEqual(cm.exception.code, "empty_disbursement")
        # 显式指定时给出拒绝原因
        with self.assertRaises(DomainError) as cm:
            create_disbursement(store, a["ent"], "2026", [sub["submission_id"]])
        self.assertEqual(cm.exception.code, "ineligible_milestone")

    def test_disbursement_uses_active_contract_version_and_blocks_duplicates(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-301")
        publish_contract(store, a["ent"], "2026", {STAGE_PRUNE: 100}, [tree["tree_id"]])
        sub, _ = approve_stage(store, a, tree["tree_id"], STAGE_PRUNE)

        # 企业出账前发布合同 v2，提高修剪附加金
        publish_contract(store, a["ent"], "2026", {STAGE_PRUNE: 150}, [tree["tree_id"]])
        st = state(store)
        self.assertEqual(active_contract(st, "2026")["version"], 2)

        batch = create_disbursement(store, a["ent"], "2026")
        self.assertEqual(batch["total"], 150)
        self.assertEqual(batch["lines"][0]["contract_version"], 2)

        # 同一里程碑不能二次拨付
        with self.assertRaises(DomainError) as cm:
            create_disbursement(store, a["ent"], "2026", [sub["submission_id"]])
        self.assertEqual(cm.exception.code, "ineligible_milestone")
        with self.assertRaises(DomainError):
            create_disbursement(store, a["ent"], "2026")

    def test_extreme_damage_recovery_plan_can_adjust_future(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-302")
        approve_stage(store, a, tree["tree_id"], STAGE_PRUNE, when="2026-03-05")
        damage = report_incident(
            store, a["guard"], tree["tree_id"], INC_EXTREME_DAMAGE, "2026-04-10",
            dedup_key="hx302-damage", season="2026", note="大风折枝",
        )
        add_evidence(store, a["guard"], damage["incident_id"], EV_PHOTO, "sha-dmg", "2026-04-10")
        # 极端损伤后，有权限农技员把尚未到期的防病/复壮里程碑后移
        adjust_future_milestones(
            store, a["ag"], tree["tree_id"], "2026",
            [
                {"stage_code": STAGE_DISEASE, "new_scheduled_at": "2026-06-10"},
                {"stage_code": STAGE_REJUVENATE, "new_scheduled_at": "2026-10-20"},
            ],
            reason="极端风损，恢复期顺延",
        )
        st = state(store)
        plan = st.plans[(tree["tree_id"], "2026")]
        self.assertEqual(len(plan["adjustments"][0]["changes"]), 2)

    # -- 8. 异议不改变事实 --------------------------------------------------

    def test_dispute_does_not_rewrite_verdict_or_payment(self):
        store = Store()
        a = actors()
        tree, farmer = new_tree(store, a, "HX-400")
        contract(store, a, "2026", [tree["tree_id"]])
        sub, _ = approve_stage(store, a, tree["tree_id"], STAGE_PRUNE)
        batch = create_disbursement(store, a["ent"], "2026")
        confirm_disbursement(store, a["ent"], batch["batch_id"])

        dispute = open_dispute(
            store, a["coop"],
            {"type": "BATCH", "id": batch["batch_id"]},
            note="农户认为分配金额有误",
        )
        resolve_dispute(store, a["town"], dispute["dispute_id"], "复核合同v1，金额无误，维持原分配")

        st = state(store)
        self.assertEqual(st.submissions[sub["submission_id"]]["verdict"], SUBMIT_APPROVED)
        self.assertEqual(st.batches[batch["batch_id"]]["total"], 100)
        self.assertEqual(st.disputes[dispute["dispute_id"]]["status"], "RESOLVED")

    # -- 9. 年度四分类 ------------------------------------------------------

    def test_annual_report_classifies_four_outcomes(self):
        store = Store()
        a = actors()

        # 真实存活：方案不含复壮，约定阶段全通过
        t_alive, _ = new_tree(
            store, a, "HX-500",
            stage_list=[
                {"code": STAGE_PRUNE, "scheduled_at": "2026-03-10"},
                {"code": STAGE_DISEASE, "scheduled_at": "2026-05-10"},
            ],
        )
        approve_stage(store, a, t_alive["tree_id"], STAGE_PRUNE, when="2026-03-05")
        approve_stage(store, a, t_alive["tree_id"], STAGE_DISEASE, when="2026-05-05", diagnosis=True)

        # 复壮：复壮里程碑通过
        t_rej, _ = new_tree(store, a, "HX-501")
        for stage, when in (
            (STAGE_PRUNE, "2026-03-05"),
            (STAGE_DISEASE, "2026-05-05"),
            (STAGE_REJUVENATE, "2026-09-05"),
        ):
            approve_stage(store, a, t_rej["tree_id"], stage, when=when)

        # 合理退出：当年及时上报死亡
        t_exit, _ = new_tree(store, a, "HX-502")
        approve_stage(store, a, t_exit["tree_id"], STAGE_PRUNE, when="2026-03-05")
        death = report_incident(
            store, a["guard"], t_exit["tree_id"], INC_TREE_DEATH, "2026-07-01",
            dedup_key="hx502-death", season="2026",
        )
        decide_protection_status(
            store, a["ag"], t_exit["tree_id"], STATUS_DEAD, "2026", death["incident_id"]
        )

        # 漏管：有方案但阶段未完成
        t_neg, _ = new_tree(store, a, "HX-503")

        # 漏管：根本没编年度方案
        t_noplan, _ = new_tree(store, a, "HX-504", with_plan=False)

        contract(
            store, a, "2026",
            [t_alive["tree_id"], t_rej["tree_id"], t_exit["tree_id"],
             t_neg["tree_id"], t_noplan["tree_id"]],
        )

        st = state(store)
        report = annual_report(st, "2026")
        counts = report["summary"]
        self.assertEqual(counts[YEAR_REAL_ALIVE], 1)
        self.assertEqual(counts[YEAR_REJUVENATED], 1)
        self.assertEqual(counts[YEAR_REASONABLE_EXIT], 1)
        self.assertEqual(counts[YEAR_NEGLECTED], 2)

    # -- 10. 公众隐私 -------------------------------------------------------

    def test_public_report_hides_farmer_address_and_exact_location(self):
        store = Store()
        a = actors()
        tree, farmer = new_tree(store, a, "HX-600")
        approve_stage(store, a, tree["tree_id"], STAGE_PRUNE, when="2026-03-05")

        report = public_report(state(store), "2026")
        row = next(t for t in report["trees"] if t["public_code"] == "HX-600")
        serialized = str(row)
        self.assertNotIn(farmer["farmer_id"], serialized)
        self.assertNotIn("果农甲", serialized)
        self.assertNotIn("lat", serialized)
        self.assertNotIn("红光村", serialized)
        self.assertIn("plot_hint", row)
        self.assertEqual(row["passed_stage_count"], 1)

    # -- 11. 角色边界 -------------------------------------------------------

    def test_role_boundaries(self):
        store = Store()
        a = actors()
        tree, _ = new_tree(store, a, "HX-700")
        # 护树队不能发合同
        with self.assertRaises(DomainError):
            publish_contract(store, a["guard"], "2026", AMOUNTS, [tree["tree_id"]])
        # 企业不能编方案
        with self.assertRaises(DomainError):
            publish_plan(store, a["ent"], tree["tree_id"], "2026", stages())
        # 企业不能替自己拨付没有合同的年度
        with self.assertRaises(DomainError):
            create_disbursement(store, a["ent"], "2030")
        # 待复核申报不能被重复复核为别的结论之外：先制造待复核
        inc = report_incident(
            store, a["guard"], tree["tree_id"], INC_CARE, "2026-03-05",
            dedup_key="hx700-p", season="2026",
        )
        photo = add_evidence(store, a["guard"], inc["incident_id"], EV_PHOTO, "sha", "2026-03-05")
        sub = submit_milestone(
            store, a["guard"], tree["tree_id"], "2026", STAGE_PRUNE,
            inc["incident_id"], [photo["evidence_id"]],
        )
        self.assertEqual(state(store).submissions[sub["submission_id"]]["verdict"], SUBMIT_PENDING)


def AuthzOrInput(exc):
    """别名仅为可读性：证据角色越界同样是 DomainError。"""
    return exc


if __name__ == "__main__":
    unittest.main()
