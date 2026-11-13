import type { IncidentState } from "./api";

export const STATE_LABEL: Record<IncidentState, string> = {
  NEW: "新建",
  TRIAGED: "已分诊",
  DIAGNOSING: "诊断中",
  REPRODUCING: "复现中",
  FIXING: "修复中",
  VERIFYING: "验证中",
  AWAITING_REVIEW: "待评审",
  MERGING: "合并中",
  MERGED: "已合并",
  OBSERVING: "观察期",
  NEEDS_HUMAN: "需人工",
  NOT_BUG: "非缺陷",
  CANNOT_REPRODUCE: "无法复现",
  CANNOT_FIX: "无法修复",
  ALREADY_FIXED: "已被修复",
  CANCELLED: "已取消",
  CLOSED: "已关闭",
};

export type Tone = "neutral" | "active" | "attention" | "success" | "danger";

export function stateTone(s: IncidentState): Tone {
  switch (s) {
    case "AWAITING_REVIEW":
    case "NEEDS_HUMAN":
      return "attention";
    case "CLOSED":
    case "MERGED":
      return "success";
    case "OBSERVING":
      return "active";
    case "CANNOT_FIX":
    case "CANNOT_REPRODUCE":
      return "danger";
    case "NOT_BUG":
    case "ALREADY_FIXED":
    case "CANCELLED":
      return "neutral";
    default:
      return "active";
  }
}

/** 等待人工处理的状态：事故列表、审批中心都按这个口径统计。 */
export const WAITING_STATES: ReadonlySet<IncidentState> = new Set(["AWAITING_REVIEW", "NEEDS_HUMAN"]);

// 主干流程，用于详情页的进度条
export const PIPELINE: IncidentState[] = [
  "NEW", "TRIAGED", "DIAGNOSING", "REPRODUCING", "FIXING", "VERIFYING", "AWAITING_REVIEW", "MERGING", "MERGED", "OBSERVING", "CLOSED",
];

/** 进度条上真正需要用户感知的 8 个阶段（新建与分诊合并为“接入”，观察期并入合并之后）。 */
export const PIPELINE_STEPS: { key: string; label: string; states: IncidentState[] }[] = [
  { key: "intake", label: "接入分诊", states: ["NEW", "TRIAGED"] },
  { key: "diagnose", label: "诊断", states: ["DIAGNOSING"] },
  { key: "reproduce", label: "复现", states: ["REPRODUCING"] },
  { key: "fix", label: "修复", states: ["FIXING"] },
  { key: "verify", label: "验证", states: ["VERIFYING"] },
  { key: "review", label: "人工评审", states: ["AWAITING_REVIEW"] },
  { key: "merge", label: "合并", states: ["MERGING", "MERGED"] },
  { key: "observe", label: "观察期", states: ["OBSERVING"] },
  { key: "done", label: "完成", states: ["CLOSED"] },
];

export const SCENARIOS: { value: string; label: string }[] = [
  { value: "happy", label: "全流程成功" },
  { value: "verify_fail_once", label: "首次验证失败后通过" },
  { value: "verify_fail_always", label: "验证始终失败 → 转人工" },
  { value: "not_bug", label: "诊断为非缺陷" },
  { value: "low_confidence", label: "诊断置信度不足 → 转人工" },
  { value: "cannot_reproduce", label: "无法复现" },
  { value: "cannot_fix", label: "无法修复" },
  { value: "fixer_needs_human", label: "修复需人工决策" },
  { value: "already_fixed", label: "合并前发现已修复" },
  { value: "merge_conflict", label: "合并前重验失败" },
];

export function relTime(iso: string, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 45) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("zh-CN", { hour12: false });
}

export function dateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function actorLabel(actor: string): string {
  if (actor.startsWith("agent:")) return actor.slice(6);
  if (actor.startsWith("user:")) return actor.slice(5);
  if (actor.startsWith("source:")) return actor.slice(7);
  if (actor.startsWith("github:")) return `${actor.slice(7)}（GitHub）`;
  return actor;
}

export function isTerminalState(s: IncidentState): boolean {
  return ["NOT_BUG", "CANNOT_REPRODUCE", "CANNOT_FIX", "ALREADY_FIXED", "CANCELLED", "CLOSED"].includes(s);
}

/** 1000 -> 1.0k，1_234_567 -> 1.23M；用于 token 等大数字。 */
export function compactNumber(n: number): string {
  if (!Number.isFinite(n)) return "-";
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 2).replace(/\.?0+$/, "")}M`;
  if (Math.abs(n) >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1).replace(/\.0$/, "")}k`;
  return String(n);
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "-";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  // 整分/整时不带零尾："1h" 而不是 "1h 0m"
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`;
  return m % 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${Math.floor(m / 60)}h`;
}

export function elapsedSince(iso: string, now = Date.now()): string {
  return duration((now - new Date(iso).getTime()) / 1000);
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return "-";
  return `${(v * 100).toFixed(digits)}%`;
}

export const CHECK_LABEL: Record<string, string> = {
  build: "构建编译",
  repro: "复现用例",
  regression: "回归测试",
  twin_errors: "孪生环境错误检查",
  test_integrity: "测试完整性",
};

export const AGENT_LABEL: Record<string, string> = {
  diagnoser: "诊断",
  reproducer: "复现",
  fixer: "修复",
  verifier: "验证",
  reporter: "报告",
  orchestrator: "编排",
};