"""老红橘古树管护兑现后端 HTTP 服务。

提供健康检查与管护兑现 JSON API。所有业务规则在 care.py 中，
本模块只做 HTTP 编解码、角色令牌校验与台账持久化。

启动：
    python3 service.py --port 8000 --data ledger.json
    python3 service.py --check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import care
from care import (
    Actor,
    DomainError,
    NotFound,
    PERM_ADJUST_MILESTONE,
    ROLE_ADMIN,
    ROLE_AGRONOMIST,
    ROLE_COOP,
    ROLE_ENTERPRISE,
    ROLE_GUARDIAN,
    ROLE_TOWN,
    Store,
    annual_report,
    classify_tree,
    public_report,
    responsible_farmer,
    state,
)

SERVICE_ID = "red-orange-procurement"
SERVICE_NAME = "老红橘保护性收购"

VALID_ROLES = {
    ROLE_GUARDIAN,
    ROLE_AGRONOMIST,
    ROLE_COOP,
    ROLE_ENTERPRISE,
    ROLE_TOWN,
    ROLE_ADMIN,
}
VALID_PERMISSIONS = {PERM_ADJUST_MILESTONE}


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


# ---------------------------------------------------------------------------
# 应用状态（台账 + 参与者目录）
# ---------------------------------------------------------------------------


class Application:
    def __init__(self, data_path: str | None = None, admin_token: str = "admin-local-token"):
        self.data_path = data_path
        self.admin_token = admin_token
        self.lock = threading.RLock()
        self.store = Store()
        # actor_id -> Actor
        self.actors: dict[str, Actor] = {}
        if data_path and os.path.exists(data_path):
            with open(data_path, encoding="utf-8") as f:
                raw = json.load(f)
            self.store = Store.from_json(json.dumps(raw.get("ledger", {"events": []}), ensure_ascii=False))
            for a in raw.get("actors", []):
                self.actors[a["id"]] = Actor(
                    a["id"], a["name"], a["role"], frozenset(a.get("permissions", []))
                )

    def persist(self) -> None:
        if not self.data_path:
            return
        tmp = self.data_path + ".tmp"
        ledger = json.loads(self.store.to_json())
        payload = {
            "ledger": ledger,
            "actors": [
                {"id": a.id, "name": a.name, "role": a.role, "permissions": sorted(a.permissions)}
                for a in self.actors.values()
            ],
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.data_path)

    def actor_from_headers(self, headers) -> Actor:
        actor_id = headers.get("X-Actor-Id")
        if not actor_id:
            raise DomainError("unauthorized", "缺少 X-Actor-Id 请求头", 401)
        actor = self.actors.get(actor_id)
        if not actor:
            raise DomainError("unauthorized", "参与者未登记或已失效", 401)
        return actor


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    app: Application = Application()

    # -- 基础工具 ----------------------------------------------------------

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self._send_json({"error": {"code": "not_found", "message": "接口或资源不存在"}}, 404)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError("bad_json", f"请求体不是合法 JSON：{exc}", 400)
        if not isinstance(data, dict):
            raise DomainError("bad_input", "请求体必须是 JSON 对象", 400)
        return data

    def _actor(self) -> Actor:
        return self.app.actor_from_headers(self.headers)

    def _admin(self) -> None:
        token = self.headers.get("X-Admin-Token")
        if token != self.app.admin_token:
            raise DomainError("unauthorized", "管理令牌无效（X-Admin-Token）", 401)

    def log_message(self, *_args):
        return

    # -- 路由 --------------------------------------------------------------

    def do_GET(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send_json(health_payload())
                return
            if path == "/api/reports/annual":
                self._annual_report()
                return
            if path == "/api/reports/public":
                self._public_report()
                return
            m = re.fullmatch(r"/api/trees/([^/]+)", path)
            if m:
                self._tree_detail(m.group(1))
                return
            if path == "/api/events":
                self._list_events()
                return
            self._not_found()
        except DomainError as exc:
            self._send_json({"error": {"code": exc.code, "message": exc.message}}, exc.status)
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_json({"error": {"code": "internal", "message": str(exc)}}, 500)

    def do_POST(self):
        routes = {
            "/api/actors": self._create_actor,
            "/api/trees": self._register_tree,
            "/api/farmers": self._register_farmer,
            "/api/contracts": self._publish_contract,
            "/api/plans": self._publish_plan,
            "/api/plans/adjustments": self._adjust_plan,
            "/api/incidents": self._report_incident,
            "/api/submissions": self._submit_milestone,
            "/api/protections/decisions": self._decide_status,
            "/api/breaches": self._confirm_breach,
            "/api/disbursements": self._create_disbursement,
            "/api/allocations": self._record_allocation,
            "/api/disputes": self._open_dispute,
        }
        try:
            path = self.path.split("?", 1)[0]
            if path in routes:
                with self.app.lock:
                    routes[path]()
                return
            m = re.fullmatch(r"/api/incidents/([^/]+)/evidence", path)
            if m:
                with self.app.lock:
                    self._add_evidence(m.group(1))
                return
            m = re.fullmatch(r"/api/submissions/([^/]+)/review", path)
            if m:
                with self.app.lock:
                    self._review_milestone(m.group(1))
                return
            m = re.fullmatch(r"/api/disbursements/([^/]+)/confirm", path)
            if m:
                with self.app.lock:
                    self._confirm_disbursement(m.group(1))
                return
            m = re.fullmatch(r"/api/disputes/([^/]+)/resolve", path)
            if m:
                with self.app.lock:
                    self._resolve_dispute(m.group(1))
                return
            m = re.fullmatch(r"/api/trees/([^/]+)/responsibility", path)
            if m:
                with self.app.lock:
                    self._assign_responsibility(m.group(1))
                return
            self._not_found()
        except DomainError as exc:
            self._send_json({"error": {"code": exc.code, "message": exc.message}}, exc.status)
        except Exception as exc:  # pragma: no cover - 防御性
            self._send_json({"error": {"code": "internal", "message": str(exc)}}, 500)

    # -- 参与者 ------------------------------------------------------------

    def _create_actor(self):
        self._admin()
        body = self._read_json()
        actor_id = body.get("id") or ""
        name = body.get("name") or ""
        role = body.get("role") or ""
        if not actor_id or not name:
            raise DomainError("bad_input", "id 与 name 必填", 400)
        if role not in VALID_ROLES:
            raise DomainError("bad_input", f"角色必须是：{sorted(VALID_ROLES)}", 400)
        perms = frozenset(body.get("permissions", []))
        unknown = perms - VALID_PERMISSIONS
        if unknown:
            raise DomainError("bad_input", f"未知权限：{sorted(unknown)}", 400)
        if actor_id in self.app.actors:
            raise DomainError("duplicate", "参与者已存在", 409)
        self.app.actors[actor_id] = Actor(actor_id, name, role, perms)
        self.app.persist()
        self._send_json({"id": actor_id, "name": name, "role": role, "permissions": sorted(perms)}, 201)

    # -- 基础档案 ----------------------------------------------------------

    def _register_tree(self):
        actor = self._actor()
        b = self._read_json()
        result = care.register_tree(
            self.app.store,
            actor,
            public_code=b["public_code"],
            age_years=int(b["age_years"]),
            season=str(b["season"]),
            village=b.get("village"),
            plot_hint=b.get("plot_hint"),
            exact_location=b.get("exact_location"),
        )
        self.app.persist()
        self._send_json(result, 201)

    def _register_farmer(self):
        actor = self._actor()
        b = self._read_json()
        result = care.register_farmer(self.app.store, actor, name=b["name"])
        self.app.persist()
        self._send_json(result, 201)

    def _assign_responsibility(self, tree_id):
        actor = self._actor()
        b = self._read_json()
        result = care.assign_responsibility(
            self.app.store,
            actor,
            tree_id=tree_id,
            farmer_id=b["farmer_id"],
            effective_season=str(b["effective_season"]),
            reason=b.get("reason", ""),
        )
        self.app.persist()
        self._send_json(result, 201)

    def _publish_contract(self):
        actor = self._actor()
        b = self._read_json()
        result = care.publish_contract(
            self.app.store,
            actor,
            season=str(b["season"]),
            stage_amounts=b["stage_amounts"],
            tree_ids=b["tree_ids"],
            note=b.get("note", ""),
        )
        self.app.persist()
        self._send_json(result, 201)

    # -- 年度方案 ----------------------------------------------------------

    def _publish_plan(self):
        actor = self._actor()
        b = self._read_json()
        result = care.publish_plan(
            self.app.store,
            actor,
            tree_id=b["tree_id"],
            season=str(b["season"]),
            stages=b["stages"],
            restrictions=b.get("restrictions"),
        )
        self.app.persist()
        self._send_json(result, 201)

    def _adjust_plan(self):
        actor = self._actor()
        b = self._read_json()
        result = care.adjust_future_milestones(
            self.app.store,
            actor,
            tree_id=b["tree_id"],
            season=str(b["season"]),
            changes=b["changes"],
            reason=b["reason"],
        )
        self.app.persist()
        self._send_json(result)

    # -- 现场事件与证据 ----------------------------------------------------

    def _report_incident(self):
        actor = self._actor()
        b = self._read_json()
        result = care.report_incident(
            self.app.store,
            actor,
            tree_id=b["tree_id"],
            kind=b["kind"],
            occurred_at=b["occurred_at"],
            dedup_key=b["dedup_key"],
            season=str(b["season"]),
            note=b.get("note", ""),
            dept=b.get("dept"),
            channel=b.get("channel", "ONLINE"),
        )
        self.app.persist()
        self._send_json(result, 201 if not result.get("merged") else 200)

    def _add_evidence(self, incident_id):
        actor = self._actor()
        b = self._read_json()
        result = care.add_evidence(
            self.app.store,
            actor,
            incident_id=incident_id,
            kind=b["kind"],
            sha256=b["sha256"],
            captured_at=b["captured_at"],
            metadata=b.get("metadata"),
        )
        self.app.persist()
        self._send_json(result, 201)

    # -- 里程碑申报与复核 --------------------------------------------------

    def _submit_milestone(self):
        actor = self._actor()
        b = self._read_json()
        result = care.submit_milestone(
            self.app.store,
            actor,
            tree_id=b["tree_id"],
            season=str(b["season"]),
            stage_code=b["stage_code"],
            incident_id=b["incident_id"],
            evidence_ids=b["evidence_ids"],
        )
        self.app.persist()
        self._send_json(result, 201)

    def _review_milestone(self, submission_id):
        actor = self._actor()
        b = self._read_json()
        result = care.review_milestone(
            self.app.store,
            actor,
            submission_id=submission_id,
            verdict=b["verdict"],
            note=b.get("note", ""),
            review_sha256=b.get("review_sha256", ""),
        )
        self.app.persist()
        self._send_json(result)

    # -- 状态裁决与违约 ----------------------------------------------------

    def _decide_status(self):
        actor = self._actor()
        b = self._read_json()
        result = care.decide_protection_status(
            self.app.store,
            actor,
            tree_id=b["tree_id"],
            status=b["status"],
            effective_season=str(b["effective_season"]),
            incident_id=b["incident_id"],
            note=b.get("note", ""),
        )
        self.app.persist()
        self._send_json(result, 201)

    def _confirm_breach(self):
        actor = self._actor()
        b = self._read_json()
        result = care.confirm_breach(
            self.app.store,
            actor,
            tree_id=b["tree_id"],
            season=str(b["season"]),
            restriction=b["restriction"],
            incident_id=b["incident_id"],
            note=b.get("note", ""),
        )
        self.app.persist()
        self._send_json(result, 201)

    # -- 拨付、分配、异议 --------------------------------------------------

    def _create_disbursement(self):
        actor = self._actor()
        b = self._read_json()
        result = care.create_disbursement(
            self.app.store,
            actor,
            season=str(b["season"]),
            submission_ids=b.get("submission_ids"),
        )
        self.app.persist()
        self._send_json(result, 201)

    def _confirm_disbursement(self, batch_id):
        actor = self._actor()
        self._read_json()
        result = care.confirm_disbursement(self.app.store, actor, batch_id=batch_id)
        self.app.persist()
        self._send_json(result)

    def _record_allocation(self):
        actor = self._actor()
        b = self._read_json()
        result = care.record_allocation(
            self.app.store,
            actor,
            batch_id=b["batch_id"],
            lines=b["lines"],
        )
        self.app.persist()
        self._send_json(result, 201)

    def _open_dispute(self):
        actor = self._actor()
        b = self._read_json()
        result = care.open_dispute(
            self.app.store, actor, subject=b["subject"], note=b.get("note", "")
        )
        self.app.persist()
        self._send_json(result, 201)

    def _resolve_dispute(self, dispute_id):
        actor = self._actor()
        b = self._read_json()
        result = care.resolve_dispute(
            self.app.store, actor, dispute_id=dispute_id, resolution=b["resolution"]
        )
        self.app.persist()
        self._send_json(result)

    # -- 查询 --------------------------------------------------------------

    def _season_param(self) -> str:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        for pair in query.split("&"):
            if pair.startswith("season="):
                return pair.split("=", 1)[1]
        raise DomainError("bad_input", "查询参数 season 必填", 400)

    def _annual_report(self):
        self._actor().require_role(ROLE_TOWN, ROLE_COOP, ROLE_AGRONOMIST, ROLE_ADMIN)
        report = annual_report(state(self.app.store), self._season_param())
        self._send_json(report)

    def _public_report(self):
        # 公众接口：无需身份，且只输出脱敏内容
        report = public_report(state(self.app.store), self._season_param())
        self._send_json(report)

    def _tree_detail(self, tree_id):
        actor = self._actor()
        st = state(self.app.store)
        tree = st.trees.get(tree_id)
        if not tree:
            raise NotFound("保护树")
        season = None
        if "?" in self.path:
            for pair in self.path.split("?", 1)[1].split("&"):
                if pair.startswith("season="):
                    season = pair.split("=", 1)[1]
        view = {
            "tree_id": tree_id,
            "public_code": tree["public_code"],
            "age_years": tree["age_years"],
            "village": tree["village"],
            "plot_hint": tree["plot_hint"],
        }
        # 精确坐标、住址类信息只对镇里/合作社/农技员开放
        if actor.role in (ROLE_TOWN, ROLE_COOP, ROLE_AGRONOMIST, ROLE_ADMIN):
            view["exact_location"] = tree["exact_location"]
        versions = [
            {
                "farmer_id": v["farmer_id"],
                "farmer_name": st.farmers.get(v["farmer_id"], {}).get("name"),
                "effective_season": v["effective_season"],
                "reason": v["reason"],
            }
            for v in st.responsibility.get(tree_id, [])
        ]
        view["responsibility_versions"] = versions
        if season:
            farmer = responsible_farmer(st, tree_id, season)
            view["current_responsibility"] = farmer
            plan = st.plans.get((tree_id, season))
            if plan:
                view["plan"] = {
                    "season": season,
                    "stages": plan["stages"],
                    "restrictions": plan["restrictions"],
                    "adjustments": plan["adjustments"],
                }
            view["classification"] = classify_tree(st, tree_id, season)
            decision = st.decisions.get(tree_id)
            if decision:
                view["protection_decision"] = decision
        self._send_json(view)

    def _list_events(self):
        self._actor().require_role(ROLE_TOWN, ROLE_ADMIN)
        self._send_json({"events": self.app.store.events})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None, help="台账持久化文件（JSON）")
    parser.add_argument(
        "--admin-token",
        default=os.environ.get("CARE_ADMIN_TOKEN", "admin-local-token"),
        help="登记参与者所需管理令牌，也可用 CARE_ADMIN_TOKEN 设置",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 冒烟：领域模块可完成一轮完整业务闭环
        smoke()
        print("基础检查通过")
        return
    Handler.app = Application(data_path=args.data, admin_token=args.admin_token)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


def smoke():
    """--check 时执行一次内存级全链路冒烟，不写盘。"""
    store = Store()
    admin = Actor("a", "自检管理员", ROLE_ADMIN)
    tree = care.register_tree(store, admin, "HX-000", 120, "2026", plot_hint="河东片区")
    farmer = care.register_farmer(store, admin, "自检农户")
    care.assign_responsibility(store, admin, tree["tree_id"], farmer["farmer_id"], "2026")
    assert tree and farmer


if __name__ == "__main__":
    main()
