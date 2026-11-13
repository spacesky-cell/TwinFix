# TwinFix 靶场评测 2026-10-23

- 运行: sample-post-c010-hi
- 日期: 2026-10-23
- Agent: MiniMax-M3.1-Flash(high) sample-post-c010-hi
- 用例: C001, C004, C009, C010, C011, C014

## 汇总

| 指标 | 值 |
|---|---|
| 用例数 | 6 |
| 结论正确率 | 67% |
| 根因定位 Top-1 / Top-3 | 100% / 100% |
| 复现成功率 | 67% |
| 修复成功率（隐藏测试 + 回归） | 67% |
| 误修率 | 0% |
| 人工介入率 | 17% |
| 平均端到端耗时 | 837s |
| 平均 token / 成本 | 721842 / 0.0000 |

## 明细

| 用例 | 类别 | 期望 | 实际 | 结论 | 根因 | 复现 | 隐藏测试 | 误修 | 耗时 | token | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| C001 | 空指针 | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 859s | 895676 |  |
| C004 | 并发 | fixed | REPRODUCING | ❌ | Top-1 | ❌ | - |  | - | 395252 | timeout in REPRODUCING |
| C009 | 已修复 | ALREADY_FIXED | DIAGNOSING | ❌ | ❌ | ❌ | - |  | - | 0 | timeout in DIAGNOSING |
| C010 | 业务异常 | NOT_BUG | NOT_BUG | ✅ | - | ❌ | - |  | 611s | 876506 |  |
| C011 | 配置 | NEEDS_HUMAN | NEEDS_HUMAN | ✅ | Top-3 | ❌ | - |  | 818s | 627000 |  |
| C014 | MQ | fixed | AWAITING_REVIEW | ✅ | Top-1 | ✅ | ✅ |  | 1062s | 1536616 |  |
