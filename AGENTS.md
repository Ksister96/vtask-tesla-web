# AGENTS.md — 本仓库协作与生产红线

面向 Cursor / 自动化代理与人工协作者。改代码可以大胆，**动生产必须守规矩**。

## 生产部署红线（必读）

1. **禁止**用 GitHub 或本地文件对生产做**整文件覆盖**（尤其 `car-web-multi.py`）。生产常有未入库补丁；整文件覆盖会静默抹掉。
2. **只部署卡妹儿审过的 PR**。未审合入 / 未审通过的分支不得上生产。
3. **部署前必须先备份**，再把**现网文件**与**待部署版本**做 diff。若发现「现网有、仓库没有」的代码，**先停下上报**，不得强推覆盖。
4. **删数据 / 解绑 / 清理类操作**，必须产品负责人（周哥）本人点头。
5. **部署后自测**核心接口（登录、车控、行程列表 `/api/trip_list`）通过后再报完成。

## 事故备忘（2026-11-03）

2026-11-03：将 PR#2（monitor `user_ops`）相关变更以 GitHub main 的 `car-web-multi.py` **整文件覆盖**上生产，导致生产独有、尚未入库的 `/api/trip_list`（小程序行程列表）被抹掉；现网已从备份 `car-web-multi.py.bak.20261103014912` 外科手术式恢复。本仓库须把 `trip_list` 等现网能力对齐进 GitHub，且以后禁止再整文件覆盖。

同日另起事故：批量解绑约 32 个「黑珍珠」账号，以及 `/api/monitor/cleanup` 清理废账号时，误清了主人现网绑定。主人身份：`z13162002608@hotmail.com` / uid `E2299e37a4acc93ea44ca671e`。代码层已加硬保护（见下节）。

## 解绑 / 清理硬保护（代码门禁）

`car-web-multi.py` 中 `PROTECTED_UNBIND_UIDS`（可扩展）：

- `E2299e37a4acc93ea44ca671e`（主人 hotmail）
- `demo01`（演示账号，必须保留）

规则：

1. **二次确认**：`POST /api/unbind`、`POST /api/monitor/cleanup`、`POST /api/monitor/unbind` 必须带 `confirm=true` / `confirm=UNBIND`（cleanup 亦接受 `CLEANUP`）或键入目标 uid；否则 **4xx**，不改动任何文件。
2. **白名单硬拒绝**：命中 `PROTECTED_UNBIND_UIDS` 时即使带 confirm 也 **403**（批量解绑则该 uid 条目 `ok: false`）。**无超级旁路**；要从名单拿掉须改代码 + 周哥点头。
3. 保护范围：禁止删其 `tokens.json` / `account.json` / `schedules.json` / `notifications.json`、用户目录（cleanup `rmtree`）、以及运维解绑时的 `pair_state` 条目。

## 改代码约定（摘要）

- 保持变更窄、可审：不要顺手大重构。
- 触及行程 / 小程序契约时，对齐现网与 `vtask-miniapp` 的调用形状，勿另起一套 API。
- 监控大屏 `user_ops` 等已合入能力保留，恢复行程接口时不要回滚无关功能。

## 与 Codex 协作（周哥 2026-11-10）

Codex 是主线施工队，卡妹儿团队是专线队。专线永远叠在最新主线上，不另开一套。出处：https://github.com/Ksister96/vtask-tesla-web/issues/7

1. **Codex = 主线施工队**：开 PR、合进 `main`。主线以已经合进 `main` 的为准。
2. **卡妹儿团队 = 专线队**：开工前先对齐最新 `main`，看清 Codex 已经合进去的 PR。不拿旧 tip 硬推。
3. **改动叠在最新 main**：卡妹儿侧改动都叠在最新主线上。冲突时卡妹儿侧 rebase 到 Codex 后面，不抢同一批提交。
4. **部署 / 版本排在 Codex 这批之后**：体验版、部署版本号都排在这批后面。不合不部署。
5. **生产红线照旧，一条不松**（详见上文「生产部署红线」）：
   - **禁止整文件覆盖**生产（尤其 `car-web-multi.py`）。
   - **只部署卡妹儿审过的 PR**。未审合入、未审通过的分支不得上生产。
   - **部署前必须先备份**，再把现网文件和待部署版本做 diff。现网有、仓库没有的，先停下上报，不得强推覆盖。
