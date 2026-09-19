"""管护兑现 HTTP 接口的端到端冒烟测试。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from api import Api
from guardian import GuardianEngine

COOP = {"X-Actor-Id": "coop-1", "X-Actor-Role": "coop"}
AGRO = {"X-Actor-Id": "agro-1", "X-Actor-Role": "agronomist",
        "X-Actor-Permissions": "plan_adjust"}
ENTERPRISE = {"X-Actor-Id": "ent-1", "X-Actor-Role": "enterprise"}


class ApiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.API = Api(GuardianEngine())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None, headers=None, expect=200):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(f"{self.base}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, expect)
                return json.load(response)
        except HTTPError as error:
            payload = error.read().decode("utf-8")
            self.assertEqual(error.code, expect, payload)
            return json.loads(payload)

    def test_full_fulfillment_flow(self):
        farmer = self.call("POST", "/api/farmers", {
            "name": "张三", "address": "广兴镇幸福村3组12号", "village": "幸福村",
        }, COOP)
        tree = self.call("POST", "/api/trees", {
            "code": "GX-201", "age_years": 150, "lat": 30.123456, "lng": 120.654321,
            "village": "幸福村", "restrictions": ["no_picking"],
        }, COOP)
        self.call("POST", f"/api/trees/{tree['tree_id']}/assignments", {
            "farmer_id": farmer["farmer_id"],
            "effective_from": "2026-01-01T00:00:00",
            "reason": "responsibility_change",
        }, COOP)
        contract = self.call("POST", "/api/contracts", {
            "year": 2026, "version": "2026-v1",
            "milestone_amounts": {"pruning": 30000}, "terms": "保护价附加金",
        }, COOP)
        plan = self.call("POST", f"/api/trees/{tree['tree_id']}/plans", {
            "year": 2026, "contract_id": contract["contract_id"],
            "milestones": [{"type": "pruning", "due_date": "2026-04-01",
                            "required_evidence": ["photo"]}],
        }, AGRO)
        self.call("POST", f"/api/plans/{plan['plan_id']}/activate", {}, COOP)
        milestone_id = plan["milestone_ids"][0]

        # 未授权角色不能调整方案
        denied = self.call("POST", f"/api/plans/{plan['plan_id']}/adjust", {
            "reason": "postponement",
            "changes": [{"milestone_id": milestone_id, "due_date": "2026-05-01"}],
        }, COOP, expect=403)
        self.assertIn("error", denied)

        self.call("POST", f"/api/milestones/{milestone_id}/evidence", {
            "kind": "photo", "label": "修剪照", "payload": {"url": "a.jpg"},
        }, AGRO)
        self.call("POST", f"/api/milestones/{milestone_id}/review", {
            "decision": "accept", "opinion": "符合要求",
        }, AGRO)
        injection = self.call(
            "POST", f"/api/milestones/{milestone_id}/injections", {}, ENTERPRISE
        )
        self.assertEqual(injection["amount_cents"], 30000)

        distribution = self.call("POST", "/api/distributions", {
            "year": 2026, "contract_id": contract["contract_id"],
        }, COOP)
        detail = self.call("GET", f"/api/distributions/{distribution['distribution_id']}")
        self.assertEqual(detail["lines"][0]["farmer_id"], farmer["farmer_id"])

        outcomes = self.call("GET", "/api/outcomes/2026", headers={
            "X-Actor-Id": "town-1", "X-Actor-Role": "town",
        })
        self.assertEqual(outcomes[0]["outcome"], "survived")

    def test_public_view_and_error_mapping(self):
        public = self.call("GET", "/api/public/trees")
        self.assertIsInstance(public, list)
        if public:
            self.assertNotIn("address", public[0])
        self.call("GET", "/api/unknown", expect=404)
        self.call("POST", "/api/farmers", {"name": "x"}, COOP, expect=400)
        bad_role = self.call("GET", "/api/trees/tree-1",
                             headers={"X-Actor-Role": "nobody"}, expect=400)
        self.assertIn("未知角色", bad_role["error"])

    def test_health_still_works(self):
        with urlopen(f"{self.base}/health", timeout=2) as response:
            self.assertEqual(json.load(response)["service"], "red-orange-procurement")


if __name__ == "__main__":
    unittest.main()
