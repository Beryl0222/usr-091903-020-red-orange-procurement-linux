"use strict";

const { spawnSync } = require("node:child_process");

// 管护兑现后端的三组契约：领域规则、HTTP 接口、基础服务身份
const result = spawnSync(
  "python3",
  ["-m", "unittest", "-v", "care_contract", "service_api_contract", "service_contract"],
  { stdio: "inherit" }
);
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
