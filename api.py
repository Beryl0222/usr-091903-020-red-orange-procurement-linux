"""管护兑现后端的 HTTP 接口层：路由、身份解析与 JSON 序列化。"""

from __future__ import annotations

import dataclasses
import enum
import json
import re
from datetime import date, datetime

from domain import Actor, DomainError, NotFound, Role
from guardian import GuardianEngine


def dump(obj):
    """把领域对象递归转成 JSON 可序列化结构。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): dump(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [dump(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(dump(v) for v in obj)
    return obj


def _parse_dt(value, field):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError(f"字段 {field} 需要 ISO 时间格式") from None


class Api:
    """把 HTTP 请求映射到引擎方法；actor 来自请求头。"""

    def __init__(self, engine=None):
        self.engine = engine or GuardianEngine()
        self._routes = []
        self._register_routes()

    def _route(self, method, pattern):
        regex = re.compile(f"^{pattern}$")

        def decorator(func):
            self._routes.append((method, regex, func))
            return func

        return decorator

    @staticmethod
    def _actor(headers):
        role = headers.get("x-actor-role")
        if not role:
            return Actor("anonymous", Role.PUBLIC)
        try:
            role = Role(role)
        except ValueError:
            raise DomainError(f"未知角色: {role}") from None
        permissions = frozenset(
            p for p in headers.get("x-actor-permissions", "").split(",") if p
        )
        return Actor(headers.get("x-actor-id", "unknown"), role, permissions)

    def handle(self, method, path, headers, raw_body):
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("请求体不是合法 JSON") from None
        if not isinstance(body, dict):
            raise DomainError("请求体必须是 JSON 对象")
        actor = self._actor(headers)
        for route_method, regex, func in self._routes:
            if route_method != method:
                continue
            match = regex.match(path)
            if match:
                try:
                    result = func(actor, body, **match.groupdict())
                except KeyError as error:
                    raise DomainError(f"缺少字段: {error.args[0]}") from None
                return 200, dump(result)
        raise NotFound(f"接口不存在: {method} {path}")

    def _register_routes(self):
        engine = self.engine
        route = self._route

        @route("POST", r"/api/farmers")
        def create_farmer(actor, body):
            return engine.register_farmer(
                actor, body["name"], body["address"], body["village"]
            )

        @route("POST", r"/api/trees")
        def create_tree(actor, body):
            return engine.register_tree(
                actor,
                body["code"],
                body["age_years"],
                body["lat"],
                body["lng"],
                body["village"],
                body.get("restrictions", ()),
            )

        @route("GET", r"/api/trees/(?P<tree_id>[^/]+)")
        def get_tree(actor, body, tree_id):
            return engine.get_tree(tree_id)

        @route("GET", r"/api/trees/(?P<tree_id>[^/]+)/history")
        def tree_history(actor, body, tree_id):
            return engine.tree_history(actor, tree_id)

        @route("POST", r"/api/trees/(?P<tree_id>[^/]+)/assignments")
        def assign(actor, body, tree_id):
            return engine.assign(
                actor,
                tree_id,
                body["farmer_id"],
                _parse_dt(body["effective_from"], "effective_from"),
                body["reason"],
            )

        @route("POST", r"/api/trees/(?P<tree_id>[^/]+)/death")
        def record_death(actor, body, tree_id):
            return engine.record_death(
                actor,
                tree_id,
                _parse_dt(body["observed_at"], "observed_at"),
                body["diagnosis"],
                body.get("note", ""),
            )

        @route("POST", r"/api/trees/(?P<tree_id>[^/]+)/exit")
        def approve_exit(actor, body, tree_id):
            observed = body.get("observed_at")
            return engine.approve_exit(
                actor,
                tree_id,
                body["reason"],
                _parse_dt(observed, "observed_at") if observed else None,
            )

        @route("POST", r"/api/contracts")
        def create_contract(actor, body):
            return engine.create_contract(
                actor,
                body["year"],
                body["version"],
                body["milestone_amounts"],
                body.get("terms", ""),
            )

        @route("POST", r"/api/trees/(?P<tree_id>[^/]+)/plans")
        def create_plan(actor, body, tree_id):
            return engine.create_plan(
                actor, tree_id, body["year"], body["contract_id"], body["milestones"]
            )

        @route("GET", r"/api/plans/(?P<plan_id>[^/]+)")
        def get_plan(actor, body, plan_id):
            plan = engine.get_plan(plan_id)
            return {
                "plan": plan,
                "milestones": [engine.milestones[mid] for mid in plan.milestone_ids],
                "adjustments": [
                    engine.adjustments[aid] for aid in plan.adjustment_ids
                ],
            }

        @route("POST", r"/api/plans/(?P<plan_id>[^/]+)/activate")
        def activate_plan(actor, body, plan_id):
            return engine.activate_plan(actor, plan_id)

        @route("POST", r"/api/plans/(?P<plan_id>[^/]+)/adjust")
        def adjust_plan(actor, body, plan_id):
            return engine.adjust_plan(
                actor,
                plan_id,
                body["reason"],
                body["changes"],
                body.get("note", ""),
            )

        @route("GET", r"/api/milestones/(?P<milestone_id>[^/]+)")
        def milestone_detail(actor, body, milestone_id):
            return engine.milestone_detail(milestone_id)

        @route("POST", r"/api/milestones/(?P<milestone_id>[^/]+)/evidence")
        def submit_evidence(actor, body, milestone_id):
            return engine.submit_evidence(
                actor,
                milestone_id,
                body["kind"],
                body["label"],
                body.get("payload", {}),
            )

        @route("POST", r"/api/milestones/(?P<milestone_id>[^/]+)/review")
        def review(actor, body, milestone_id):
            return engine.review_milestone(
                actor, milestone_id, body["decision"], body.get("opinion", "")
            )

        @route("POST", r"/api/milestones/(?P<milestone_id>[^/]+)/injections")
        def inject(actor, body, milestone_id):
            return engine.inject_funding(actor, milestone_id)

        @route("POST", r"/api/reports")
        def submit_report(actor, body):
            return engine.submit_report(
                actor,
                body["source"],
                body["tree_id"],
                _parse_dt(body["observed_at"], "observed_at"),
                body["lat"],
                body["lng"],
                body.get("content", ""),
                body.get("offline_id"),
            )

        @route("GET", r"/api/events/(?P<event_id>[^/]+)")
        def get_event(actor, body, event_id):
            event = engine.get_event(event_id)
            return {
                "event": event,
                "reports": [engine.reports[rid] for rid in event.report_ids],
            }

        @route("POST", r"/api/distributions")
        def run_distribution(actor, body):
            return engine.run_distribution(actor, body["year"], body["contract_id"])

        @route("GET", r"/api/distributions/(?P<distribution_id>[^/]+)")
        def get_distribution(actor, body, distribution_id):
            distribution = engine._get(
                engine.distributions, distribution_id, "分配批次"
            )
            return {
                "distribution": distribution,
                "lines": [engine.lines[lid] for lid in distribution.line_ids],
            }

        @route("POST", r"/api/lines/(?P<line_id>[^/]+)/disputes")
        def file_dispute(actor, body, line_id):
            return engine.file_dispute(actor, line_id, body["reason"])

        @route("POST", r"/api/disputes/(?P<dispute_id>[^/]+)/resolve")
        def resolve_dispute(actor, body, dispute_id):
            return engine.resolve_dispute(
                actor,
                dispute_id,
                bool(body["uphold"]),
                body.get("note", ""),
                body.get("adjust_to_farmer_id"),
            )

        @route("GET", r"/api/outcomes/(?P<year>\d{4})")
        def outcomes(actor, body, year):
            return engine.annual_outcomes(actor, int(year))

        @route("GET", r"/api/public/trees")
        def public_trees(actor, body):
            return engine.public_progress()
