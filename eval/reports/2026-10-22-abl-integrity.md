# TwinFix 靶场评测 2026-10-22

- 运行: abl-integrity
- 日期: 2026-10-22
- Agent: MiniMax-M3.1-Flash-Preview(low) abl-integrity
- 用例: C001, C002, C003, C004, C005, C006, C007, C008, C009, C010, C011, C012, C013, C014, C015, C016, C017, C018, C019, C020, C021, C022, C023, C024

## 汇总

| 指标 | 值 |
|---|---|
| 用例数 | 24 |
| 结论正确率 | 79% |
| 根因定位 Top-1 / Top-3 | 75% / 85% |
| 复现成功率 | 90% |
| 修复成功率（隐藏测试 + 回归） | 85% |
| 误修率 | 0% |
| 人工介入率 | 8% |
| 平均端到端耗时 | 475s |
| 平均 token / 成本 | 520432 / 0.0000 |

## 明细

| 用例 | 类别 | 期望 | 实际 | 结论 | 根因 | 复现 | 隐藏测试 | 误修 | 耗时 | token | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| C001 | 空指针 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 779s | 667065 |  |
| C002 | 边界 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 215s | 185021 |  |
| C003 | 精度 | fixed | DIAGNOSING | ❌ | ❌ | ❌ | - |  | - | 0 | timeout in DIAGNOSING |
| C004 | 并发 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 700s | 707033 |  |
| C005 | 事务 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 477s | 634754 |  |
| C006 | 时间 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 192s | 186060 |  |
| C007 | 事务 | fixed | NEEDS_HUMAN | ❌ | ❌ | ❌ | - |  | 828s | 869115 |  |
| C008 | N+1 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 282s | 316876 |  |
| C009 | 已修复 | ALREADY_FIXED | ALREADY_FIXED | ✅ | Top-1 | ✅ | - |  | 951s | 1181222 |  |
| C010 | 业务异常 | NOT_BUG | AWAITING_REVIEW | ❌ | - | ✅ | - |  | 818s | 2026026 |  |
| C011 | 配置 | NEEDS_HUMAN | NEEDS_HUMAN | ✅ | Top-1 | ❌ | - |  | 517s | 275730 |  |
| C012 | 缓存 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 461s | 759028 |  |
| C013 | 缓存 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 280s | 378142 |  |
| C014 | MQ | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 253s | 276991 |  |
| C015 | MQ | fixed | AWAITING_REVIEW | ✅ | ❌ | ✅ | ✅ |  | 264s | 344254 |  |
| C016 | 死锁 | fixed | REPRODUCING | ❌ | Top-1 | ✅ | - |  | - | 236821 | timeout in REPRODUCING |
| C017 | 线程安全 | fixed | AWAITING_REVIEW | ✅ | Top-3 | ✅ | ✅ |  | 484s | 276399 |  |
| C018 | 装箱比较 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 316s | 359144 |  |
| C019 | 内存 | fixed | AWAITING_REVIEW | ✅ | Top-3 | ✅ | ✅ |  | 283s | 374077 |  |
| C020 | 连接池 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 348s | 480663 |  |
| C021 | 超时 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 679s | 238473 |  |
| C022 | 序列化 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 190s | 148837 |  |
| C023 | 线程池 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 343s | 402279 |  |
| C024 | 依赖 | NOT_BUG | AWAITING_REVIEW | ❌ | - | ✅ | - |  | 794s | 1166354 |  |
