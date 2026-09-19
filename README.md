# 老红橘保护性收购 · 古树管护兑现后端

把老红橘保护价合同与古树管护挂钩的兑现服务：为每棵保护树确定责任农户、
年度方案（修剪 / 防病 / 低产树复壮）、禁止采摘与接穗限制，按分阶段证据
兑付保护附加金。服务用于核对老红橘分级收购、农户结算与古树管护，使产业
增收和种质保护分别可查。

## 设计要点

- **仅追加事件台账**：所有业务动作只追加事件，查询由事件重放得到。已验收、
  已驳回、死亡裁决、原始证据等事实不会因名单变更或责任人变更而被改写。
- **原始证据只增不改**：照片、定位、病害诊断（农技员原始意见）、复核意见
  均以内容哈希（`sha256`）登记，每次补传都是新版本，无修改/删除接口。
- **责任版本不溯及既往**：果农换地、责任人变更生成新生效年度的责任版本；
  拨付单按验收事实形成时的责任农户快照生成，换责任人不改旧账。
- **未来里程碑受控调整**：仅持 `milestone.adjust` 权限的农技人员可改期/改要求，
  且只允许调整尚未裁决的未来里程碑（换地衔接、方案延期、极端损伤恢复期）。
- **现场事件合并**：护树队离线巡查（`channel=OFFLINE`）断网重试靠幂等键
  `dedup_key` 合并；多部门对同树、同日、同类型的重复上报自动合并为一次事件，
  多个上报来源都保留在 `sources` 中。
- **拨付资格闸门**：企业只能为“已复核通过 + 当季仍受保护 + 无已确认违约 +
  在合同名单内 + 未拨付过”的里程碑出账；金额按出账时有效合同版本。死树
  次季的方案编制、里程碑申报与拨付一律被拒。
- **年度四分类**：镇里年度报表逐树区分真实存活（REAL_ALIVE）、复壮
  （REJUVENATED）、合理退出（REASONABLE_EXIT，死亡/极端损伤被及时上报）、
  漏管（NEGLECTED，阶段未完成或死亡被长期隐瞒）。
- **公众视图脱敏**：公开接口只给古树编号、树龄、模糊片区与进展分类，
  不含农户姓名、住址、精确坐标；精确坐标仅授权角色可见。

## 角色

| 角色 | 主要权限 |
| --- | --- |
| guardian 护树队 | 现场事件上报、照片/定位证据、里程碑申报 |
| agronomist 农技员 | 年度方案、病害诊断、复核、违约确认、保护状态裁决、（授权后）未来里程碑调整 |
| coop 合作社 | 农户登记与定责、附加金分配、异议发起/处理 |
| enterprise 企业 | 发布保护价合同（版本化）、生成与确认拨付单 |
| town 镇里 | 古树登记、年度管理报表、事件审计、异议裁定 |

## 运行

```bash
python3 service.py --check            # 配置与领域冒烟检查
python3 service.py --port 8000 --data ledger.json
curl http://localhost:8000/health
```

可选环境变量 `CARE_ADMIN_TOKEN` 设置参与者登记令牌（默认 `admin-local-token`，
仅限本地联调）。

## API 概览

除 `/health` 与 `/api/reports/public` 外，请求需带 `X-Actor-Id` 头；
参与者由管理员用 `X-Admin-Token` 经 `POST /api/actors` 登记。

- `POST /api/trees`、`POST /api/farmers`、`POST /api/trees/{id}/responsibility`
- `POST /api/contracts`（同年度重复发布自动递增版本）
- `POST /api/plans`、`POST /api/plans/adjustments`
- `POST /api/incidents`（离线/重复自动合并）、`POST /api/incidents/{id}/evidence`
- `POST /api/submissions`、`POST /api/submissions/{id}/review`
- `POST /api/protections/decisions`（死亡/极端损伤退出/留养观察）
- `POST /api/breaches`（违反禁止采摘/接穗限制）
- `POST /api/disbursements`、`POST /api/disbursements/{id}/confirm`
- `POST /api/allocations`、`POST /api/disputes`、`POST /api/disputes/{id}/resolve`
- `GET /api/reports/annual?season=`（镇里/合作社/农技员）
- `GET /api/reports/public?season=`（公众，脱敏）
- `GET /api/trees/{id}?season=`、`GET /api/events`（审计）

业务错误统一返回 `{"error": {"code", "message"}}`，常用码：
`unauthorized(401)`、`forbidden(403)`、`not_found(404)`、`duplicate(409)`、
`bad_input(422)`、`fact_locked(422)`（试图改写已裁决事实）、
`tree_not_protected(422)`、`ineligible_milestone(422)`。

## 测试

```bash
npm test
# 等价于 python3 -m unittest -v care_contract service_api_contract service_contract
```

测试覆盖：证据原始版本追加、责任人换户不改旧拨付、里程碑事实锁与调整权限、
死树当季保留/次季拦截、离线重试与跨部门上报合并、违约与重复拨付拦截、
合同版本出账、异议不回写、年度四分类与公众隐私脱敏。
