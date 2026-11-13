import type { IncidentState } from "../api";
import { STATE_LABEL, stateTone } from "../format";
import { Badge } from "./ui";

export function StateBadge({ state, size }: { state: IncidentState; size?: "sm" | "md" }) {
  const tone = stateTone(state);
  return (
    <Badge tone={tone} dot className={`state-badge ${size === "sm" ? "badge-sm" : ""}`} title={`状态：${STATE_LABEL[state]}（${state}）`}>
      {STATE_LABEL[state]}
    </Badge>
  );
}

export function SeverityBadge({ severity, size }: { severity: "S1" | "S2" | "S3" | "S4"; size?: "sm" | "md" }) {
  return (
    <span
      className={`sev-badge sev-${severity} ${size === "sm" ? "sev-sm" : ""}`}
      title={`严重级别 ${severity}${severity === "S1" ? "（最高）" : severity === "S4" ? "（最低）" : ""}`}
    >
      {severity}
    </span>
  );
}