"""HTTP 契约测试：鉴权、错误码映射、持久化与端到端业务流。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from service import Application, Handler
from care import (
    PERM_ADJUST_MILESTONE,
    ROLE_AGRONOMIST,
    ROLE_COOP,
    ROLE_ENTERPRISE,
    ROLE_GUARDIAN,
    ROLE_TOWN,
    STAGE_DISEASE,
    STAGE_PRUNE,
    STATUS_DEAD,
    SUBMIT_APPROVED,
    SUBMIT_REJECTED,
)

ADMIN_TOKEN = "test-admin-token"


class ApiClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def request(self, method, path, body=None, actor=None, admin=False):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if actor:
            headers["X-Actor-Id"] = actor
        if admin:
            headers["X-Admin-Token"] = ADMIN_TOKEN
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            return exc.code, json.loads(raw) if raw else {}

    def get(self, path, actor=None):
        return self.request("GET", path, actor=actor)

    def post(self, path, body=None, actor=None, admin=False):
        return self.request("POST", path, body=body or {}, actor=actor, admin=admin)


class HttpContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Application(data_path=None, admin_token=ADMIN_TOKEN)
        Handler.app = cls.app
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.server.server_port}")
        cls._register_actors()

    @classmethod
    def _register_actors(cls):
        specs = [
            ("ag", "李农技", ROLE_AGRONOMIST, [PERM_ADJUST_MILESTONE]),
            ("guard", "张护树", ROLE_GUARDIAN, []),
            ("coop", "合作社小陈", ROLE_COOP, []),
            ("ent", "企业代表", ROLE_ENTERPRISE, []),
            ("town", "镇干部", ROLE_TOWN, []),
        ]
        for aid, name, role, perms in specs:
            status, _ = cls.api.post(
                "/api/actors",
                {"id": aid, "name": name, "role": role, "permissions": perms},
                admin=True,
            )
            assert status == 201, status

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_unchanged(self):
        status, body = self.api.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "red-orange-procurement")

    def test_unknown_route_404_and_auth_required(self):
        status, body = self.api.get("/nope")
        self.assertEqual(status, 404)
        status, body = self.api.post("/api/trees", {"public_code": "X", "age_years": 100, "season": "2026"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        # 管理接口需要管理令牌
        status, body = self.api.post("/api/actors", {"id": "x", "name": "x", "role": ROLE_TOWN})
        self.assertEqual(status, 401)

    def test_end_to_end_care_to_payment_and_public_privacy(self):
        # 镇里登记古树（含精确坐标），合作社登记农户并定责
        status, tree = self.api.post(
            "/api/trees",
            {
                "public_code": "HX-HTTP-1",
                "age_years": 130,
                "season": "2026",
                "village": "广兴镇红光村",
                "plot_hint": "河东老院子片区",
                "exact_location": {"lat": 30.12, "lng": 106.34},
            },
            actor="town",
        )
        self.assertEqual(status, 201)
        tree_id = tree["tree_id"]

        status, farmer = self.api.post("/api/farmers", {"name": "果农甲"}, actor="coop")
        self.assertEqual(status, 201)
        status, _ = self.api.post(
            f"/api/trees/{tree_id}/responsibility",
            {"farmer_id": farmer["farmer_id"], "effective_season": "2026", "reason": "承包"},
            actor="coop",
        )
        self.assertEqual(status, 201)

        # 农技员编年度方案（含禁止采摘、接穗限制）
        status, _ = self.api.post(
            "/api/plans",
            {
                "tree_id": tree_id,
                "season": "2026",
                "stages": [
                    {"code": STAGE_PRUNE, "name": "春季修剪", "scheduled_at": "2026-03-10"},
                    {"code": STAGE_DISEASE, "name": "防病", "scheduled_at": "2026-05-10"},
                ],
                "restrictions": [{"type": "NO_HARVEST"}, {"type": "NO_SCION"}],
            },
            actor="ag",
        )
        self.assertEqual(status, 201)

        # 企业发布合同
        status, contract = self.api.post(
            "/api/contracts",
            {"season": "2026", "stage_amounts": {"PRUNE": 100, "DISEASE": 120}, "tree_ids": [tree_id]},
            actor="ent",
        )
        self.assertEqual(status, 201)
        self.assertEqual(contract["version"], 1)

        # 护树队上报现场事件并附照片、定位
        status, inc = self.api.post(
            "/api/incidents",
            {
                "tree_id": tree_id,
                "kind": "CARE",
                "occurred_at": "2026-03-05",
                "dedup_key": "http-dedup-1",
                "season": "2026",
                "dept": "护树队",
            },
            actor="guard",
        )
        self.assertEqual(status, 201)
        inc_id = inc["incident_id"]
        evidence_ids = []
        for kind, sha in (("PHOTO", "sha-photo-1"), ("GPS", "sha-gps-1")):
            status, ev = self.api.post(
                f"/api/incidents/{inc_id}/evidence",
                {"kind": kind, "sha256": sha, "captured_at": "2026-03-05"},
                actor="guard",
            )
            self.assertEqual(status, 201)
            evidence_ids.append(ev["evidence_id"])

        # 断网重试：同幂等键合并
        status, retry = self.api.post(
            "/api/incidents",
            {
                "tree_id": tree_id,
                "kind": "CARE",
                "occurred_at": "2026-03-05",
                "dedup_key": "http-dedup-1",
                "season": "2026",
                "channel": "OFFLINE",
            },
            actor="guard",
        )
        self.assertEqual(status, 200)
        self.assertTrue(retry["merged"])
        self.assertEqual(retry["incident_id"], inc_id)

        # 申报并验收
        status, sub = self.api.post(
            "/api/submissions",
            {
                "tree_id": tree_id,
                "season": "2026",
                "stage_code": STAGE_PRUNE,
                "incident_id": inc_id,
                "evidence_ids": evidence_ids,
            },
            actor="guard",
        )
        self.assertEqual(status, 201)
        status, review = self.api.post(
            f"/api/submissions/{sub['submission_id']}/review",
            {"verdict": SUBMIT_APPROVED, "note": "达标", "review_sha256": "sha-review-1"},
            actor="ag",
        )
        self.assertEqual(status, 200)

        # 企业生成并确认拨付单
        status, batch = self.api.post("/api/disbursements", {"season": "2026"}, actor="ent")
        self.assertEqual(status, 201)
        self.assertEqual(batch["total"], 100)
        self.assertEqual(batch["lines"][0]["farmer_snapshot"]["farmer_id"], farmer["farmer_id"])
        status, _ = self.api.post(
            f"/api/disbursements/{batch['batch_id']}/confirm", {}, actor="ent"
        )
        self.assertEqual(status, 200)

        # 合作社按合同版本分配
        status, _ = self.api.post(
            "/api/allocations",
            {"batch_id": batch["batch_id"],
             "lines": [{"farmer_id": farmer["farmer_id"], "amount": 100}]},
            actor="coop",
        )
        self.assertEqual(status, 201)

        # 镇里年度报表
        qs = urlencode({"season": "2026"})
        status, annual = self.api.get(f"/api/reports/annual?{qs}", actor="town")
        self.assertEqual(status, 200)
        # 本树方案含两阶段，仅修剪通过：归类为漏管（防病未完成）
        row = next(t for t in annual["trees"] if t["public_code"] == "HX-HTTP-1")
        self.assertEqual(row["category"], "NEGLECTED")
        self.assertEqual(row["passed_stages"], [STAGE_PRUNE])
        self.assertEqual(row["missing_stages"], [STAGE_DISEASE])

        # 公众报表无身份也可看，且不含住址/坐标/农户信息
        status, pub = self.api.get(f"/api/reports/public?{qs}")
        self.assertEqual(status, 200)
        text = json.dumps(pub, ensure_ascii=False)
        self.assertNotIn("果农甲", text)
        self.assertNotIn("30.12", text)
        self.assertNotIn("红光村", text)
        self.assertIn("河东老院子片区", text)

        # 护树队无权看年度管理报表，也看不到精确坐标
        status, denied = self.api.get(f"/api/reports/annual?{qs}", actor="guard")
        self.assertEqual(status, 403)
        status, detail = self.api.get(f"/api/trees/{tree_id}?season=2026", actor="guard")
        self.assertEqual(status, 200)
        self.assertNotIn("exact_location", detail)
        status, detail_town = self.api.get(f"/api/trees/{tree_id}?season=2026", actor="town")
        self.assertIn("exact_location", detail_town)

    def test_dead_tree_next_season_claim_rejected_over_http(self):
        # 第二棵树：死亡后次季申报必须被拒
        status, tree = self.api.post(
            "/api/trees",
            {"public_code": "HX-HTTP-DEAD", "age_years": 110, "season": "2026",
             "plot_hint": "西山梁片区"},
            actor="town",
        )
        tree_id = tree["tree_id"]
        status, farmer = self.api.post("/api/farmers", {"name": "果农乙"}, actor="coop")
        self.api.post(
            f"/api/trees/{tree_id}/responsibility",
            {"farmer_id": farmer["farmer_id"], "effective_season": "2026"},
            actor="coop",
        )
        self.api.post(
            "/api/plans",
            {"tree_id": tree_id, "season": "2026",
             "stages": [{"code": STAGE_PRUNE, "scheduled_at": "2026-03-10"}]},
            actor="ag",
        )
        status, death = self.api.post(
            "/api/incidents",
            {"tree_id": tree_id, "kind": "TREE_DEATH", "occurred_at": "2026-12-01",
             "dedup_key": "http-dead-1", "season": "2026"},
            actor="guard",
        )
        status, _ = self.api.post(
            "/api/protections/decisions",
            {"tree_id": tree_id, "status": STATUS_DEAD, "effective_season": "2027",
             "incident_id": death["incident_id"], "note": "枯死"},
            actor="ag",
        )
        self.assertEqual(status, 201)
        status, plan2027 = self.api.post(
            "/api/plans",
            {"tree_id": tree_id, "season": "2027",
             "stages": [{"code": STAGE_PRUNE, "scheduled_at": "2027-03-10"}]},
            actor="ag",
        )
        self.assertEqual(status, 422)
        self.assertEqual(plan2027["error"]["code"], "tree_not_protected")

    def test_reject_requires_reason_and_validation_errors(self):
        status, tree = self.api.post(
            "/api/trees",
            {"public_code": "HX-HTTP-3", "age_years": 90, "season": "2026",
             "plot_hint": "北坡片区"},
            actor="town",
        )
        tree_id = tree["tree_id"]
        status, farmer = self.api.post("/api/farmers", {"name": "果农丙"}, actor="coop")
        self.api.post(
            f"/api/trees/{tree_id}/responsibility",
            {"farmer_id": farmer["farmer_id"], "effective_season": "2026"},
            actor="coop",
        )
        self.api.post(
            "/api/plans",
            {"tree_id": tree_id, "season": "2026",
             "stages": [{"code": STAGE_PRUNE, "scheduled_at": "2026-03-10"}]},
            actor="ag",
        )
        status, inc = self.api.post(
            "/api/incidents",
            {"tree_id": tree_id, "kind": "CARE", "occurred_at": "2026-03-05",
             "dedup_key": "http-d3", "season": "2026"},
            actor="guard",
        )
        status, ev = self.api.post(
            f"/api/incidents/{inc['incident_id']}/evidence",
            {"kind": "PHOTO", "sha256": "sha3", "captured_at": "2026-03-05"},
            actor="guard",
        )
        status, sub = self.api.post(
            "/api/submissions",
            {"tree_id": tree_id, "season": "2026", "stage_code": STAGE_PRUNE,
             "incident_id": inc["incident_id"], "evidence_ids": [ev["evidence_id"]]},
            actor="guard",
        )
        # 驳回不填原因 -> 422
        status, body = self.api.post(
            f"/api/submissions/{sub['submission_id']}/review",
            {"verdict": SUBMIT_REJECTED, "note": ""},
            actor="ag",
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "bad_input")


if __name__ == "__main__":
    unittest.main()
