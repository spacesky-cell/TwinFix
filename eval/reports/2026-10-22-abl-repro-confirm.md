# TwinFix 靶场评测 2026-10-22

- 运行: abl-repro-confirm
- 日期: 2026-10-22
- Agent: MiniMax-M3.1-Flash-Preview(low) abl-repro-confirm
- 用例: C001, C002, C003, C004, C005, C006, C007, C008, C009, C010, C011, C012, C013, C014, C015, C016, C017, C018, C019, C020, C021, C022, C023, C024

## 汇总

| 指标 | 值 |
|---|---|
| 用例数 | 24 |
| 结论正确率 | 88% |
| 根因定位 Top-1 / Top-3 | 90% / 100% |
| 复现成功率 | 90% |
| 修复成功率（隐藏测试 + 回归） | 90% |
| 误修率 | 0% |
| 人工介入率 | 4% |
| 平均端到端耗时 | 523s |
| 平均 token / 成本 | 490355 / 0.0000 |

## 明细

| 用例 | 类别 | 期望 | 实际 | 结论 | 根因 | 复现 | 隐藏测试 | 误修 | 耗时 | token | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| C001 | 空指针 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 343s | 236499 |  |
| C002 | 边界 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 415s | 263972 |  |
| C003 | 精度 | fixed | REPRODUCING | ❌ | Top-1 | ❌ | - |  | - | 477886 | timeout in REPRODUCING |
| C004 | 并发 | fixed | REPRODUCING | ❌ | Top-1 | ❌ | - |  | - | 76099 | timeout in REPRODUCING |
| C005 | 事务 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 435s | 315595 |  |
| C006 | 时间 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 281s | 201539 |  |
| C007 | 事务 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 1706s | 1456664 |  |
| C008 | N+1 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 677s | 688912 |  |
| C009 | 已修复 | ALREADY_FIXED | ALREADY_FIXED | ✅ | Top-1 | ✅ | - |  | 1719s | 1240694 |  |
| C010 | 业务异常 | NOT_BUG | AWAITING_REVIEW | ❌ | - | ✅ | - |  | 1363s | 2000788 |  |
| C011 | 配置 | NEEDS_HUMAN | NEEDS_HUMAN | ✅ | Top-1 | ❌ | - |  | 502s | 159556 |  |
| C012 | 缓存 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 284s | 260131 |  |
| C013 | 缓存 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 312s | 359726 |  |
| C014 | MQ | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 232s | 262034 |  |
| C015 | MQ | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 345s | 528764 |  |
| C016 | 死锁 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 520s | 489349 |  |
| C017 | 线程安全 | fixed | AWAITING_REVIEW | ✅ | Top-3 | ✅ | ✅ |  | 264s | 331563 |  |
| C018 | 装箱比较 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 304s | 272170 |  |
| C019 | 内存 | fixed | AWAITING_REVIEW | ✅ | Top-3 | ✅ | ✅ |  | 377s | 368172 |  |
| C020 | 连接池 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 277s | 342889 |  |
| C021 | 超时 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 316s | 233045 |  |
| C022 | 序列化 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 198s | 156467 |  |
| C023 | 线程池 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 356s | 370571 |  |
| C024 | 依赖 | NOT_BUG | NOT_BUG | ✅ | - | ❌ | - |  | 287s | 675435 |  |
