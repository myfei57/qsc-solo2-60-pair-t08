# 药剂批次库存规则

## 固定业务规则

- 消耗策略固定为 **FEFO（最早有效期优先）**；同一有效期按入库先后 **FIFO** 打破并列。
- 已过期批次不参与投加；即使总库存足够，可用库存不足也会整笔拒绝，不做部分扣减。
- 入库必须提供批次号和有效期；旧的单批次接口仍兼容无有效期数据，但控制台会产生“缺少有效期”预警。
- 入库单号重发：
  - 单号与报文完全一致：幂等返回原入库结果，不重复入账。
  - 单号相同但明细不一致：不自动覆盖、不丢弃，单据置为 `conflict` 并保留给仓库人工确认。
  - 人工确认重复后调用丢弃接口，单据置为 `discarded`；再次使用同一单号会返回冲突，要求换新单号。
- 入库单先持久化 `pending` 标记，再写入批次。若在批次写入过程中失败，可调用恢复接口补齐缺失批次；已写入批次不会重复入账。
- 消耗必须携带 `request_id`；同号重放返回原批次分配结果。同号不同药剂/数量返回 409。
- 批次扣减、消耗流水和投加审计在同一个存储事务内提交；异常时三者一起回滚。

## 控制台接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/inventory` | 总量、可用量、批次、临期/过期/低库存预警、单据和流水 |
| POST | `/inventory/receipts` | 多批次入库，body 含 `document_id` 和 `items` |
| GET | `/inventory/receipts` | 查询全部入库单 |
| GET | `/inventory/receipts?document_id=...` | 查询单个入库单 |
| POST | `/inventory/receipts/recover` | 恢复所有 `pending` 部分失败入库单 |
| POST | `/inventory/receipts/discard` | 人工确认后丢弃冲突/重复单 |
| POST | `/inventory/consume` | 兼容接口；带 `request_id` 时幂等 |
| POST | `/inventory/dispense` | 正式投加接口；FEFO 消耗且必须传 `request_id` |
| POST | `/inventory/expired/sweep` | 将当前已过期批次标记为 blocked/expired |
| POST | `/inventory/dispose` | 人工报废批次并保留流水 |
| GET | `/inventory/movements` | JSON 批次变动流水 |
| GET | `/export/inventory` | CSV 批次变动流水 |
| POST | `/inventory/reorder-level` | 设置低库存阈值 |
| POST | `/inventory/expiry-warning` | 设置临期预警天数，默认 30 天 |

### 多批次入库示例

```json
{
  "document_id": "GRN-20261005-01",
  "items": [
    {
      "chemical": "chlorine",
      "lot_id": "CL-2610-A",
      "amount": 100.0,
      "received_on": "2026-10-05",
      "expires_on": "2027-04-05"
    }
  ]
}
```

### 投加消耗示例

```json
{
  "chemical": "chlorine",
  "amount": 12.5,
  "request_id": "dose-20261005-0001"
}
```

## 审计

`inventory:movements` 记录 `receive`、`consume`、`expire`、`dispose`、`receipt_conflict`、`receipt_discarded`。
消耗流水带 `request_id` 和 `audit_id`，可与 `audit:entries` 中的投加记录互相追踪。
