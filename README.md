# 老红橘保护性收购

服务用于核对老红橘分级收购、农户结算与古树管护，使产业增收和种质保护分别可查。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可确认服务身份。运行 `npm test`（或 `python3 -m unittest service_contract test_guardian test_api`）执行全部测试。

## 管护兑现后端

广兴镇把老红橘保护价合同与古树管护承诺挂钩：果农按期完成修剪、防病和低产树复壮才能获得保护附加金。后端围绕以下规则实现（`domain.py` 模型、`guardian.py` 规则引擎、`api.py` HTTP 接口）：

- **一树一档**：每棵保护树登记责任农户、年度方案、禁止采摘/接穗限制与分阶段里程碑；责任变更（换地、责任人变更）只追加版本，历史归属不改写。
- **证据留痕**：照片、定位、病害诊断、复核意见均保留原始版本，更正只追加新版本；验收通过前必须齐集必需证据。
- **调整受控**：方案延期、极端损伤等只能由获授权（`plan_adjust`）的农技人员调整未来里程碑；已通过或已驳回的事实一律冻结。
- **现场合并**：护树队离线巡查按离线键幂等去重，同树 48 小时、150 米内的多部门上报合并为一次现场事件。
- **资金门槛**：企业只对"已通过验收且树仍符合保护条件"的里程碑按合同版本注入附加金；树死亡或退出后未验收里程碑取消、后续申领被阻断。
- **分配与异议**：合作社按合同版本分配，归属验收时刻的责任农户；异议更正只追加新分配线，原线保留。
- **年度结果**：镇里可按树区分真实存活、复壮、合理退出和漏管。
- **公众视图**：`/api/public/trees` 只公开古树保护进展，不暴露农户姓名、住址与精确坐标。

### 接口概览

身份通过请求头传递：`X-Actor-Id`、`X-Actor-Role`（farmer/patrol/agronomist/enterprise/coop/town）、`X-Actor-Permissions`（如 `plan_adjust`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/farmers` | 登记农户（合作社） |
| POST | `/api/trees` | 登记保护树及限制 |
| POST | `/api/trees/{id}/assignments` | 指定/变更责任农户 |
| GET | `/api/trees/{id}/history` | 责任沿革 |
| POST | `/api/trees/{id}/death` `/api/trees/{id}/exit` | 死亡登记 / 合理退出 |
| POST | `/api/contracts` | 合同版本（各类里程碑附加金） |
| POST | `/api/trees/{id}/plans` | 年度方案与里程碑 |
| POST | `/api/plans/{id}/activate` `/adjust` | 方案生效 / 受权调整 |
| POST | `/api/milestones/{id}/evidence` `/review` | 提交证据 / 验收 |
| GET | `/api/milestones/{id}` | 里程碑状态、证据版本链、验收记录 |
| POST | `/api/reports` | 巡查/部门上报（自动合并现场事件） |
| GET | `/api/events/{id}` | 现场事件及来源上报 |
| POST | `/api/milestones/{id}/injections` | 企业注入附加金 |
| POST | `/api/distributions` | 合作社按合同版本分配 |
| POST | `/api/lines/{id}/disputes`、`/api/disputes/{id}/resolve` | 异议与处理 |
| GET | `/api/outcomes/{year}` | 镇里年度结果分类 |
| GET | `/api/public/trees` | 公众保护进展（脱敏） |
