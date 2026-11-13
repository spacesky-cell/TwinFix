// 轻量 SVG 可视化。不引入图表库：图形只表达比例与趋势，颜色由 CSS token 控制。

export type VizTone = "brand" | "active" | "attention" | "danger" | "success" | "neutral";

function clamp01(v: number): number {
  if (!Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(1, v));
}

/** 环形进度：value 为 0–1 的比例。 */
export function Ring({ value, tone = "brand", size = 30, stroke = 3.5, label }: {
  value: number; tone?: VizTone; size?: number; stroke?: number; label?: string;
}) {
  const pct = clamp01(value) * 100;
  return (
    <svg
      className={`viz-ring tone-${tone}`} width={size} height={size} viewBox="0 0 36 36"
      role="img" aria-label={label ?? `占比 ${Math.round(pct)}%`}
    >
      <circle className="viz-ring-track" cx="18" cy="18" r="15.5" strokeWidth={stroke} />
      {pct > 0 && (
        <circle
          className="viz-ring-value" cx="18" cy="18" r="15.5" strokeWidth={stroke}
          pathLength={100} strokeDasharray={`${pct} 100`} transform="rotate(-90 18 18)"
        />
      )}
    </svg>
  );
}

/** 构成条：按比例分段，用于展示总量由哪几部分组成。 */
export function StackedBar({ segments, label }: {
  segments: { value: number; tone: VizTone; label: string }[]; label?: string;
}) {
  const total = segments.reduce((n, s) => n + Math.max(0, s.value), 0);
  return (
    <div
      className="viz-stack"
      role="img" aria-label={label ?? segments.map((s) => `${s.label} ${s.value}`).join("，")}
    >
      {total === 0
        ? <span className="viz-stack-seg tone-neutral viz-stack-idle" />
        : segments.map((s) => s.value > 0 && (
          <span
            key={s.label} className={`viz-stack-seg tone-${s.tone}`}
            style={{ width: `${(s.value / total) * 100}%` }} title={`${s.label} ${s.value}`}
          />
        ))}
    </div>
  );
}

/** 迷你折线：values 为 0–1 的序列，null 表示该点缺失。 */
export function Sparkline({ values, tone = "brand", label, height = 46 }: {
  values: (number | null)[]; tone?: VizTone; label?: string; height?: number;
}) {
  const points = values
    .map((v, i) => ({ i, v: v == null ? null : clamp01(v) }))
    .filter((p): p is { i: number; v: number } => p.v !== null);
  if (points.length === 0) return <div className="viz-spark-empty" style={{ height }} />;

  const w = 100;
  const h = 40;
  const pad = 3;
  const x = (i: number) => (values.length <= 1 ? w / 2 : (i / (values.length - 1)) * w);
  const y = (v: number) => h - pad - v * (h - pad * 2);
  const line = points
    .map((p, idx) => `${idx === 0 ? "M" : "L"}${x(p.i).toFixed(2)},${y(p.v).toFixed(2)}`)
    .join(" ");
  const first = points[0]!;
  const last = points[points.length - 1]!;
  const area = `${line} L${x(last.i).toFixed(2)},${h} L${x(first.i).toFixed(2)},${h} Z`;

  return (
    <svg
      className={`viz-spark tone-${tone}`} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
      style={{ height }} role="img" aria-label={label ?? "趋势"}
    >
      <path className="viz-spark-area" d={area} />
      <path className="viz-spark-line" d={line} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}