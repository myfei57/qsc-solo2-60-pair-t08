# 药剂批次库存与效期管理（`waterplant.batches`）

在原有“按总量”的库存台账之上，新增一套带**批次 + 有效期**的库存与单据系统。旧的
`/inventory*` 接口保持不变，新系统挂在 `/batches*`。

## 一、定死的仓库规则

| 主题 | 规则（代码强制，非配置项） |
|---|---|
| 消耗顺序 | **临期优先 FEFO**：先按 `expiry_date` 升序；同日到期或都无有效期时按入库先后（FIFO，用单调 `seq`），再按批次号兜底。排序键全局唯一、结果确定可复现（`models.fefo_sort_key`） |
| 过期药剂 | 正常消耗**绝不扣过期批**；可用量与“含过期总量”分别展示；过期库存只能走 `/batches/scrap` 报废并留痕 |
| 库存不足 | 消耗**全有或全无**：可用量不足整单拒绝，不产生任何批次扣减、台账流水、投加记录或审计 |
| 入库单重发（同 `request_id` + 同内容） | **幂等重放**：直接返回首次结果（含原分配），不二次入库、不二次扣减 |
| 入库单重发（同 `request_id` + 不同内容） | **不丢弃、不入账**，进入隔离区 `/batches/quarantine`，等待人工 `approve`（按新单入库）或 `reject`（丢弃但留审计） |
| 部分失败 / 崩溃恢复 | 入库先落 `received`（同时占用幂等键），再入账为 `applied`；崩溃残留 `received` 由重发自动续作，或用 `/batches/inbound/recover` 批量恢复，二者均幂等 |
| 一致性 | 批次、台账流水、投加/消耗单、幂等表、审计在**同一个存储事务**内一次原子落盘（见下“事务”） |
| 防重复扣减 | 批次 ID 由单据确定性派生（`<order_id>-L<n>`），续作时已入账的批次自动跳过；消耗以 `request_id` 去重 |
| 审计 | append-only 的批次审计（接收/消耗/重放/隔离/裁决/报废/过期翻转/恢复/失败），`/batches/audit` 供控制台展示 |
| 预警 | 临期（默认 30 天，可配）、已过期、低库存（按药剂独立配置，缺省回落到全局 25.0） |

## 二、存储事务

`Store.transaction()` 给底层 JSON 文档加了多键原子提交：事务期间写操作进入暂存区，
提交时合并后只做一次临时文件 + `os.replace`，因此：

- 多份台账要么一起落盘，要么都不落盘（不会出现“批次扣了但投加记录没写”）；
- 事务期间持锁串行化，并发请求互不覆盖，已用 8 并发入库、10 并发消耗验证不会超卖/重入；
- 事务内抛异常则丢弃暂存写入（嵌套事务共用一个外层事务）。

## 三、HTTP 接口（均为 JSON）

单据类接口必须带 `request_id`（客户端幂等键，建议用上游单号/UUID）。

| 方法 & 路径 | 作用 |
|---|---|
| `GET  /batches` | 总览：当天日期、策略、各药剂可用/含过期余量、批次视图、全部预警、隔离与待恢复计数 |
| `GET  /batches/list?chemical=` | 批次明细（含 `status`、`remaining`、`days_to_expiry`），按 FEFO 顺序 |
| `POST /batches/inbound` | 入库单：`{request_id, lines:[{chemical, amount, expiry_date?, supplier_ref?}], order_id?}`；也支持单行简写 `{request_id, chemical, amount, expiry_date}` |
| `GET  /batches/inbound` | 入库单及状态（received/applied/failed/quarantined） |
| `POST /batches/inbound/recover` | 恢复所有中断在 `received` 阶段的入库单 |
| `POST /batches/consume` | 投加/领用：`{request_id, chemical, amount, purpose?, order_id?}`，返回逐批 `allocations` |
| `GET  /batches/consume?chemical=` | 投加/消耗单及逐批分配（批次账与投加记录一一对应） |
| `POST /batches/scrap` | 报废：`{batch_id, amount?(缺省全部), reason?, request_id?}` |
| `POST /batches/expiry/refresh` | 定时任务调用：把跨过有效期的批次翻为 `expired`（幂等，只审计新翻转） |
| `GET  /batches/alerts` | 临期 / 过期 / 低库存预警 |
| `GET  /batches/quarantine?status=` | 隔离区（冲突重发） |
| `POST /batches/quarantine/resolve` | 人工裁决：`{quarantine_id, decision: approve|reject, note?}` |
| `GET  /batches/ledger?chemical=&batch_id=` | append-only 批次台账流水（in/out/scrap） |
| `GET  /batches/audit?kind=&chemical=&batch_id=` | 批次生命周期审计 |
| `GET|POST /batches/near-expiry` | 读取/设置临期提前天数 `{days}` |
| `GET|POST /batches/reorder-level` | 读取/设置某药剂补货线 `{chemical, level?}` |

冲突重发返回 `409`（内容体 `error` 中带 `quarantine_id`）；业务校验/库存不足返回 `400`。
预警同时汇总进 `GET /health/checks`（`batches-expired/near-expiry/low-stock/quarantine/inbound`）、
`GET /snapshot` 的 `batches` 字段与 `GET /report`。

## 四、典型时序

入库（正常）：
1. 服务端校验全部行（药剂名、正数、日期格式）。
2. 事务 A：写入 `received` 入库单 + 占用幂等键（状态 received）。
3. 事务 B：生成批次（确定性批次号、单调 seq）、写 in 流水、单据置 `applied`、幂等键置 applied、写接收审计。

入库重发：
- 内容一致 → 若单还在 `received` 就续作入账；否则原样返回首次结果。
- 内容不一致 → 进隔离区（同样的冲突重发只产生一条隔离记录），等待人工裁决。

投加：
1. 校验正数与 `request_id`。
2. 事务内按 FEFO 选出**未过期且有余量**的批次并试分配；不够直接抛错回滚。
3. 足够则：扣减批次（扣完置 exhausted）、写 out 流水、追加消耗单（逐批分配）、
   写幂等结果、写消耗审计 —— 同一事务一次提交。

## 五、测试

`tests/test_batches.py` 覆盖：FEFO 与 FIFO 兜底、过期不扣、全有或全无与回滚、
入库/消耗幂等、跨操作 request_id 冲突、隔离区裁决、崩溃续作、过期翻转与低库存预警、
台账流水重算余额一致、并发不超卖、HTTP 全流程。运行：

```
python3 -m unittest discover -s tests
```
