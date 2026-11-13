import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, type EvalCase, type EvalReport, type EvalReportMeta, type EvalRun, type EvalSummary,
} from "../api";
import { Icon } from "../components/Icons";
import {
  Badge, CopyButton, EmptyState, ErrorState, KpiCard, LoadingState, Spinner,
} from "../components/ui";
import { Ring, Sparkline } from "../components/viz";
import { compactNumber, dateTime, duration, pct } from "../format";
import { navigate } from "../router";

const METRICS: { key: keyof EvalSummary; label: string; lowerIsBetter?: boolean }[] = [
  { key: "outcome_accuracy", label: "结论正确率" },
  { key: "top1", label: "根因 Top-1" },
  { key: "top3", label: "根因 Top-3" },
  { key: "repro_rate", label: "复现成功率" },
  { key: "fix_rate", label: "修复成功率" },
  { key: "false_fix_rate", label: "误修率", lowerIsBetter: true },
  { key: "needs_human_rate", label: "人工介入率", lowerIsBetter: true },
];

// 对比表只放关键指标，避免把七列压到无法阅读
const COMPARE_METRICS = METRICS.filter((m) =>
  ["outcome_accuracy", "fix_rate", "false_fix_rate", "needs_human_rate"].includes(m.key));

const RUN_LABEL: Record<EvalRun["status"], string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "完成",
  failed: "失败",
  interrupted: "已中断",
};

const RUN_TONE: Record<EvalRun["status"], "neutral" | "attention" | "success" | "danger"> = {
  queued: "neutral",
  running: "attention",
  succeeded: "success",
  failed: "danger",
  interrupted: "danger",
};

const ACTIVE = new Set<EvalRun["status"]>(["queued", "running"]);

export function EvalPage() {
  const [reports, setReports] = useState<EvalReportMeta[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<EvalReport | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportNonce, setReportNonce] = useState(0);

  const reload = useCallback(async (select?: string) => {
    try {
      const list = await api.evalReports();
      setReports(list);
      setLoadError(null);
      setSelected((s) => select ?? s ?? list[0]?.name ?? null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  useEffect(() => {
    if (!selected) { setReport(null); return; }
    let cancelled = false;
    setReportLoading(true);
    setReportError(null);
    setReport(null);
    api.evalReport(selected)
      .then((r) => { if (!cancelled) setReport(r); })
      .catch((e: Error) => { if (!cancelled) setReportError(e.message); })
      .finally(() => { if (!cancelled) setReportLoading(false); });
    return () => { cancelled = true; };
  }, [selected, reportNonce]);

  const latest = reports?.[0] ?? null;
  const selectedMeta = reports?.find((r) => r.name === selected) ?? null;

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">评测</h1>
        </div>
        <div className="page-header-side">
          <a className="btn btn-sm" href="/arena" onClick={(e) => { e.preventDefault(); navigate("/arena"); }}>
            <Icon name="flask" size={13} /> 浏览 Arena
          </a>
          <button type="button" className="btn btn-sm" onClick={() => void reload()} disabled={reports === null}>
            <Icon name="refresh" size={13} /> 刷新报告
          </button>
        </div>
      </header>

      <div className="kpi-strip" aria-label="评测概览">
        <KpiCard
          icon="layers" label="评测报告" value={reports ? reports.length : "—"}
          foot={latest ? `最新 ${latest.name}` : "尚无报告"}
        />
        <KpiCard
          icon="inbox" label="最新样本量" value={latest ? latest.summary?.total ?? "—" : "—"} foot="纳入汇总的用例"
        />
        <KpiCard
          icon="target" label="结论正确率" tone="success" value={latest ? pct(latest.summary?.outcome_accuracy) : "—"}
          foot="最新报告汇总值"
          visual={<Ring value={Number(latest?.summary?.outcome_accuracy ?? 0)} tone="success" label="最新结论正确率" />}
        />
        <KpiCard
          icon="shield-alert" label="误修率" tone="danger" value={latest ? pct(latest.summary?.false_fix_rate) : "—"}
          foot="自身验证通过但隐藏测试失败"
          visual={<Ring value={Number(latest?.summary?.false_fix_rate ?? 0)} tone="danger" label="最新误修率" />}
        />
      </div>

      <RunPanel onFinished={(name) => { void reload(name); }} />

      <section className="eval-reports">
        {loadError && reports === null && <ErrorState title="无法加载评测报告" message={loadError} onRetry={() => void reload()} />}
        {loadError && reports !== null && (
          <div className="banner banner-danger" role="alert"><Icon name="alert-circle" size={15} /><span>{loadError}</span></div>
        )}
        {reports === null && !loadError && <LoadingState label="正在读取评测报告…" />}
        {reports?.length === 0 && (
          <div className="card">
            <EmptyState
              icon="chart" title="还没有评测报告"
              description="在上方发起一次评测。"
            />
          </div>
        )}

        {reports && reports.length > 1 && (
          <CompareTable reports={reports} selected={selected} selectedMeta={selectedMeta} onSelect={setSelected} />
        )}

        {reportLoading && <LoadingState label="正在读取报告明细…" />}
        {reportError && <ErrorState title="无法加载报告" message={reportError} onRetry={() => setReportNonce((n) => n + 1)} />}
        {report && <ReportView report={report} />}
      </section>
    </div>
  );
}

/** 逐次评测的指标走势：只画报告 summary 里的原始值，不做平滑或显著性判断。 */
function TrendBlock({ reports }: { reports: EvalReportMeta[] }) {
  const asc = [...reports].reverse();
  const series = [
    { key: "outcome_accuracy", label: "结论正确率", tone: "success" as const },
    { key: "false_fix_rate", label: "误修率", tone: "danger" as const },
    { key: "needs_human_rate", label: "人工介入率", tone: "attention" as const },
  ];
  return (
    <div className="trend-grid">
      {series.map((s) => {
        const values = asc.map((r) => {
          const v = r.summary?.[s.key as keyof EvalSummary];
          return typeof v === "number" ? v : null;
        });
        const last = [...values].reverse().find((v) => v != null) ?? null;
        return (
          <div className="trend-item" key={s.key}>
            <div className="trend-head">
              <span className="trend-label"><span className={`trend-swatch tone-${s.tone}`} />{s.label}</span>
              <strong className="trend-value">{pct(last)}</strong>
            </div>
            <Sparkline values={values} tone={s.tone} label={`${s.label}趋势`} />
            <span className="trend-axis"><span>最早</span><span>最新</span></span>
          </div>
        );
      })}
    </div>
  );
}

function RunPanel({ onFinished }: { onFinished: (report: string) => void }) {
  const [runs, setRuns] = useState<EvalRun[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const [form, setForm] = useState({ cases: "", label: "", model: "", effort: "", timeout_s: "3600" });
  const lastStatus = useRef<Map<string, EvalRun["status"]>>(new Map());
  const primed = useRef(false);
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const list = await api.evalRuns();
        if (stopped) return;
        setRuns(list);
        setLoadError(null);
        setNow(Date.now());
        if (primed.current) {
          for (const r of list) {
            const prev = lastStatus.current.get(r.id);
            if (r.status === "succeeded" && r.report && prev !== undefined && prev !== "succeeded") {
              onFinishedRef.current(r.report);
            }
          }
        }
        primed.current = true;
        lastStatus.current = new Map(list.map((r) => [r.id, r.status]));
        timer = window.setTimeout(() => void tick(), list.some((r) => ACTIVE.has(r.status)) ? 3000 : 20000);
      } catch (e) {
        if (stopped) return;
        setLoadError((e as Error).message);
        timer = window.setTimeout(() => void tick(), 20000);
      }
    };
    void tick();
    return () => { stopped = true; if (timer !== undefined) window.clearTimeout(timer); };
  }, []);

  const activeRun = runs?.find((r) => ACTIVE.has(r.status)) ?? null;

  async function start(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setNotice(null);
    const cases = form.cases.split(/[\s,，]+/).map((s) => s.trim()).filter(Boolean);
    const timeout = Number(form.timeout_s);
    if (cases.length > 50) { setFormError("一次最多提交 50 个用例。"); return; }
    const bad = cases.find((c) => !/^[A-Za-z0-9_-]{1,20}$/.test(c));
    if (bad) { setFormError(`用例 ID 格式不合法：${bad}（只允许字母、数字、_ 和 -）。`); return; }
    if (!Number.isFinite(timeout) || timeout < 60 || timeout > 86400) {
      setFormError("超时时间需在 60 – 86400 秒之间。");
      return;
    }
    setBusy(true);
    try {
      const run = await api.startEvalRun({
        cases,
        label: form.label.trim(),
        model: form.model.trim(),
        effort: form.effort,
        timeout_s: timeout,
      });
      setRuns((prev) => [run, ...(prev ?? [])]);
      lastStatus.current.set(run.id, run.status);
      setNotice("评测任务已提交。");
      setForm((f) => ({ ...f, cases: "", label: "" }));
    } catch (e) {
      setFormError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card eval-run-card">
      <header className="card-head">
        <div className="card-heading">
          <div className="card-title"><Icon name="zap" size={15} className="card-title-icon" />发起评测<span className="card-count">单任务串行</span></div>
        </div>
        {activeRun && (
          <div className="active-run-chip">
            <span className="live-dot" aria-hidden />
            <span>{RUN_LABEL[activeRun.status]}</span>
            <span className="mono">{activeElapsed(activeRun, now)}</span>
          </div>
        )}
      </header>

      <div className="card-body">
        <form className="eval-form" onSubmit={(e) => void start(e)}>
          <div className="eval-cases">
            <label className="field">
              <span>用例 <em className="field-tag">留空 = 全部</em></span>
              <input id="eval-cases" name="cases" value={form.cases} maxLength={1000} placeholder="C001 C002，或逗号分隔" onChange={(e) => setForm({ ...form, cases: e.target.value })} />
              <small>最多 50 个。</small>
            </label>
            <div className="eval-presets">
              <button type="button" className="btn btn-sm" onClick={() => setForm({ ...form, cases: "C001 C002 C011 C014 C018 C024" })}>
                快速回归 6 例
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setForm({ ...form, cases: "C001 C002 C009 C011 C012 C014 C018 C024" })}>
                核心 8 例
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setForm({ ...form, cases: "P001 P002 P003 P004" })}>
                Python 4 例
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setForm({ ...form, cases: "G001 G002 G003 G004" })}>
                Go 4 例
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setForm({ ...form, cases: "T001 T002 T003 T004" })}>
                TypeScript 4 例
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setForm({ ...form, cases: "S001 S002 S003 S004" })}>
                C# 4 例
              </button>
            </div>
          </div>
          <label className="field">
            <span>标签</span>
            <input id="eval-label" name="label" value={form.label} maxLength={200} placeholder="如 baseline / 提示词 v2" onChange={(e) => setForm({ ...form, label: e.target.value })} />
          </label>
          <details className="disclosure eval-advanced">
            <summary>
              <Icon name="sliders" size={13} />
              <span>模型与超时</span>
              <span className="disclosure-meta">留空使用项目默认</span>
              <Icon name="chevron-down" size={13} className="disclosure-chevron" />
            </summary>
            <div className="disclosure-body">
              <div className="eval-advanced-grid">
                <label className="field">
                  <span>模型</span>
                  <input id="eval-model" name="model" value={form.model} maxLength={200} placeholder="留空 = 项目默认" onChange={(e) => setForm({ ...form, model: e.target.value })} />
                </label>
                <label className="field">
                  <span>推理强度</span>
                  <select id="eval-effort" name="effort" value={form.effort} onChange={(e) => setForm({ ...form, effort: e.target.value })}>
                    <option value="">默认</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                  </select>
                </label>
                <label className="field">
                  <span>超时（秒）</span>
                  <input id="eval-timeout" name="timeout_s" type="number" min={60} max={86400} step={1} value={form.timeout_s} onChange={(e) => setForm({ ...form, timeout_s: e.target.value })} />
                  <small>60 – 86400 秒</small>
                </label>
              </div>
            </div>
          </details>
          <div className="eval-form-submit">
            <button type="submit" className="btn btn-primary" disabled={busy || activeRun !== null}>
              {busy ? <Spinner size={13} /> : <Icon name="play" size={13} />}
              {busy ? "提交中…" : activeRun ? "有任务正在运行" : "开始评测"}
            </button>
          </div>
        </form>

        {formError && <div className="banner banner-danger" role="alert"><Icon name="alert-circle" size={15} /><span>{formError}</span></div>}
        {notice && <div className="banner banner-success" role="status"><Icon name="check-circle" size={15} /><span>{notice}</span></div>}
        {loadError && (
          <div className="banner banner-danger" role="alert">
            <Icon name="alert-circle" size={15} /><span>任务列表刷新失败：{loadError}。将在后台自动重试。</span>
          </div>
        )}

        <div className="run-list-head">
          <h3 className="subhead">任务记录</h3>
        </div>

        {runs === null && !loadError ? <LoadingState label="正在加载评测任务…" /> : runs && runs.length === 0 ? (
          <EmptyState icon="play" title="还没有评测任务" description="填写用例后发起评测。" />
        ) : runs && (
          <div className="runs-table-wrap">
            <table className="runs-table eval-runs-table">
              <thead>
                <tr>
                  <th>任务</th><th>状态</th><th className="hide-md">标签</th><th className="hide-lg">模型 / 强度</th>
                  <th className="num">用例</th><th className="hide-md">发起</th><th className="num">耗时</th><th>报告 / 日志</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const elapsed = activeElapsed(r, now);
                  return (
                    <tr key={r.id} className={ACTIVE.has(r.status) ? "run-active" : undefined}>
                      <td><span className="mono small">{r.id}</span></td>
                      <td>
                        <Badge tone={RUN_TONE[r.status]} dot>{RUN_LABEL[r.status]}</Badge>
                        {r.error && <span className="run-error" title={r.error}>{r.error}</span>}
                      </td>
                      <td className="hide-md small">{r.label || "—"}</td>
                      <td className="hide-lg"><span className="mono small">{[r.model, r.effort].filter(Boolean).join(" / ") || "默认"}</span></td>
                      <td className="num small">{r.cases.length || "全部"}</td>
                      <td className="hide-md small nowrap">{dateTime(r.created_at)}</td>
                      <td className="num small mono">{elapsed ?? "—"}</td>
                      <td>
                        {r.report ? (
                          <button type="button" className="linklike" onClick={() => onFinished(r.report!)}>
                            <Icon name="file-text" size={12} /> 查看报告
                          </button>
                        ) : r.status === "failed" || r.status === "interrupted" ? (
                          <span className="log-path"><CopyButton text={r.log_file} label="复制日志路径" /></span>
                        ) : r.status === "succeeded" ? (
                          <span className="muted small">报告缺失</span>
                        ) : (
                          <span className="muted small">运行中…</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

function CompareTable({ reports, selected, selectedMeta, onSelect }: {
  reports: EvalReportMeta[]; selected: string | null; selectedMeta: EvalReportMeta | null; onSelect: (n: string) => void;
}) {
  return (
    <section className="card compare-card">
      <header className="card-head">
        <div className="card-heading">
          <div className="card-title"><Icon name="layers" size={15} className="card-title-icon" />报告对比<span className="card-count">{reports.length} 份</span></div>
        </div>
        <label className="select-control compact">
          <span className="sr-only">选择评测报告</span>
          <select id="eval-report-select" name="report" value={selected ?? ""} onChange={(e) => onSelect(e.target.value)}>
            {reports.map((r) => <option key={r.name} value={r.name}>{r.name} · n={r.summary?.total ?? "?"}</option>)}
          </select>
        </label>
      </header>
      <div className="card-body">
        <TrendBlock reports={reports} />
      </div>
      <div className="card-body flush">
        <div className="compare-cards">
          {reports.map((r) => {
            const compat = comparability(selectedMeta?.meta, r.meta);
            const current = r.name === selected;
            return (
              <button
                key={r.name} type="button" aria-pressed={current}
                className={`compare-card-item ${current ? "selected" : ""}`}
                onClick={() => onSelect(r.name)}
              >
                <span className="compare-card-head">
                  <span className="compare-card-name">{r.name}</span>
                  <CompareStatus current={current} compat={compat} />
                </span>
                <span className="compare-card-meta">
                  {r.meta?.Agent ?? "—"} · n = {r.summary?.total ?? "—"}
                </span>
                <span className="compare-card-metrics">
                  {COMPARE_METRICS.map((m) => {
                    const v = Number(r.summary?.[m.key] ?? 0);
                    return (
                      <span className="compare-mini" key={m.key}>
                        <span className="compare-mini-label">{m.label}</span>
                        <span className="compare-mini-value">{pct(v)}</span>
                        <span className="compare-bar" aria-hidden><span style={{ width: `${clampPct(v)}%` }} /></span>
                      </span>
                    );
                  })}
                </span>
              </button>
            );
          })}
        </div>
        <div className="compare-scroll hide-xs">
          <table className="data-table compare-table">
            <thead>
              <tr>
                <th>运行</th>
                <th className="hide-md">Agent</th>
                <th className="num">n</th>
                {COMPARE_METRICS.map((m) => <th key={m.key} className="num">{m.label}</th>)}
                <th>可比性</th>
              </tr>
            </thead>
            <tbody>
              {reports.map((r) => {
                const compat = comparability(selectedMeta?.meta, r.meta);
                return (
                  <tr
                    key={r.name} className={`clickable ${r.name === selected ? "selected" : ""}`}
                    onClick={() => onSelect(r.name)}
                  >
                    <td>
                      <button type="button" className="linklike mono small" onClick={(e) => { e.stopPropagation(); onSelect(r.name); }}>
                        {r.name}
                      </button>
                    </td>
                    <td className="hide-md small">{r.meta?.Agent ?? "—"}</td>
                    <td className="num small">{r.summary?.total ?? "—"}</td>
                    {COMPARE_METRICS.map((m) => {
                      const v = Number(r.summary?.[m.key] ?? 0);
                      return (
                        <td key={m.key} className="num compare-value">
                          <span>{pct(v)}</span>
                          <span className="compare-bar" aria-hidden><span style={{ width: `${clampPct(v)}%` }} /></span>
                        </td>
                      );
                    })}
                    <td>
                      <CompareStatus current={r.name === selected} compat={compat} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function CompareStatus({ current, compat }: { current: boolean; compat: boolean | null }) {
  if (current) return <Badge tone="brand">当前</Badge>;
  if (compat === true) return <Badge tone="success" dot>同用例集</Badge>;
  if (compat === false) return <Badge tone="attention" dot title="与当前报告的用例集合不同">用例集不同</Badge>;
  return <Badge tone="neutral" title="报告 meta 没有标注用例列表，无法确认可比性">未标注</Badge>;
}

function ReportView({ report }: { report: EvalReport }) {
  const s = report.summary;
  const [filter, setFilter] = useState<CaseFilter>("all");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const counts = useMemo(() => ({
    all: report.cases.length,
    wrong: report.cases.filter((c) => !c.outcome_correct).length,
    falseFix: report.cases.filter((c) => c.false_fix).length,
    human: report.cases.filter((c) => c.needs_human).length,
    error: report.cases.filter((c) => c.error).length,
  }), [report.cases]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return report.cases.filter((c) => {
      if (filter === "wrong" && c.outcome_correct) return false;
      if (filter === "false_fix" && !c.false_fix) return false;
      if (filter === "human" && !c.needs_human) return false;
      if (filter === "error" && !c.error) return false;
      if (!q) return true;
      return [c.case_id, c.category, c.expected, c.final_state, ...c.notes, c.error ?? ""]
        .some((v) => v?.toLowerCase().includes(q));
    });
  }, [report.cases, filter, query]);

  return (
    <div className="eval-report">
      <div className="report-header">
        <div>
          <h3 className="report-title">{report.title}</h3>
          <div className="report-meta">
            {Object.entries(report.meta ?? {}).map(([k, v]) => (
              <span className="meta-chip" key={k} title={v}><span>{k}</span><strong>{v}</strong></span>
            ))}
          </div>
        </div>
        <div className="report-count"><strong>{s.total}</strong><span>用例纳入汇总</span></div>
      </div>

      <div className="metric-grid">
        {METRICS.map((m) => {
          const v = Number(s[m.key] ?? 0);
          return (
            <div className="metric-card" key={m.key}>
              <div className="metric-top">
                <span className="metric-label">{m.label}</span>
                <strong className="metric-value">{pct(v)}</strong>
              </div>
              <div className="metric-track" role="img" aria-label={`${m.label} ${pct(v)}`}>
                <span style={{ width: `${clampPct(v)}%` }} />
              </div>
              <span className="metric-note">
                {m.lowerIsBetter ? "越低越好" : "占比"}
              </span>
            </div>
          );
        })}
      </div>

      <div className="eval-secondary-stats">
        <div><span>平均端到端耗时</span><strong>{s.avg_duration_s != null ? duration(s.avg_duration_s) : "—"}</strong></div>
        <div><span>平均 token</span><strong>{compactNumber(s.avg_tokens)}</strong></div>
        {s.avg_cost > 0 && <div><span>平均成本</span><strong>{s.avg_cost.toFixed(4)}</strong></div>}
        <div><span>纳入明细</span><strong>{report.cases.length} 条</strong></div>
      </div>

      {report.cases.length !== s.total && (
        <p className="metric-caveat">
          <Icon name="alert-circle" size={13} />
          汇总 n = {s.total}，明细 {report.cases.length} 条，两者不一致。
        </p>
      )}

      <div className="case-section">
        <div className="case-section-head">
          <div>
            <h3 className="subhead">用例明细</h3>
          </div>
          <div className="case-filters" role="tablist" aria-label="按用例结果筛选">
            <CaseFilterButton value="all" count={counts.all} current={filter} onSelect={setFilter} label="全部" />
            <CaseFilterButton value="wrong" count={counts.wrong} current={filter} onSelect={setFilter} label="结论错误" />
            <CaseFilterButton value="false_fix" count={counts.falseFix} current={filter} onSelect={setFilter} label="误修" />
            <CaseFilterButton value="human" count={counts.human} current={filter} onSelect={setFilter} label="需人工" />
            <CaseFilterButton value="error" count={counts.error} current={filter} onSelect={setFilter} label="有错误" />
          </div>
          <div className="search-box compact">
            <Icon name="search" size={13} />
            <input id="eval-case-search" name="case-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索用例或类别" aria-label="搜索评测用例" />
            {query && <button type="button" className="search-clear" onClick={() => setQuery("")} aria-label="清空"><Icon name="x" size={11} /></button>}
          </div>
        </div>

        {shown.length === 0 ? (
          <EmptyState
            icon={report.cases.length === 0 ? "inbox" : "search"}
            title={report.cases.length === 0 ? "报告没有用例明细" : "没有匹配的用例"}
            description={report.cases.length === 0 ? "这份报告只包含汇总数据。" : "切换筛选或清空搜索后再看。"}
            action={report.cases.length > 0 ? <button type="button" className="btn" onClick={() => { setFilter("all"); setQuery(""); }}>清除筛选</button> : undefined}
          />
        ) : (
          <div className="table-shell">
            <div className="table-scroll">
              <table className="data-table eval-case-table">
                <thead>
                  <tr>
                    <th className="col-incident">用例</th>
                    <th className="hide-md">类别</th>
                    <th className="hide-xs">期望 → 实际</th>
                    <th className="num col-mark">结论</th>
                    <th className="col-root">根因</th>
                    <th className="num hide-lg">复现</th>
                    <th className="num col-mark">隐藏测试</th>
                    <th className="num hide-lg">耗时</th>
                    <th className="num hide-lg">Token</th>
                    <th className="col-chevron" aria-label="展开" />
                  </tr>
                </thead>
                <tbody>
                  {shown.map((c) => (
                    <Fragment key={c.case_id}>
                      <tr className={`clickable ${expanded === c.case_id ? "row-open" : ""} ${!c.outcome_correct ? "row-fail" : ""}`}
                        onClick={() => setExpanded(expanded === c.case_id ? null : c.case_id)}>
                        <td>
                          <div className="incident-line">
                            <span className="mono case-id">{c.case_id}</span>
                            {c.false_fix && <Badge tone="danger" dot>误修</Badge>}
                            {c.needs_human && <Badge tone="attention">需人工</Badge>}
                          </div>
                        </td>
                        <td className="hide-md small">{c.category}</td>
                        <td className="hide-xs"><span className="mono small">{c.expected}</span><span className="arrow-sep">→</span><span className="mono small">{c.final_state}</span></td>
                        <td className="num col-mark"><Mark ok={c.outcome_correct} label="结论正确" /></td>
                        <td className="col-root"><RootCause c={c} /></td>
                        <td className="num hide-lg"><Mark ok={c.reproduced} label="复现" /></td>
                        <td className="num col-mark"><Mark ok={c.hidden_passed} label="隐藏测试" /></td>
                        <td className="num hide-lg small mono">{c.duration_s != null ? duration(c.duration_s) : "—"}</td>
                        <td className="num hide-lg small mono">{c.tokens ? compactNumber(c.tokens) : "—"}</td>
                        <td className="col-chevron"><Icon name={expanded === c.case_id ? "chevron-down" : "chevron-right"} size={14} /></td>
                      </tr>
                      {expanded === c.case_id && (
                        <tr className="case-detail-row">
                          <td colSpan={10}>
                            <div className="eval-case-detail">
                              <div className="eval-case-detail-grid">
                                <div><span>判定</span><strong>{c.outcome_correct ? "结论正确" : "结论错误"}</strong></div>
                                <div><span>期望 → 实际</span><strong className="mono">{c.expected} → {c.final_state}</strong></div>
                                <div><span>根因判定</span><strong>{c.root_cause_graded === false ? "报告未判定" : c.root_cause_top1 ? "Top-1 命中" : c.root_cause_top3 ? "Top-3 命中" : "未命中"}</strong></div>
                                <div><span>修复验证</span><strong>{c.verified == null ? "报告未提供" : c.verified ? "已验证" : "未验证"}</strong></div>
                                <div><span>隐藏测试</span><strong>{c.hidden_passed == null ? "不适用" : c.hidden_passed ? "通过" : "未通过"}</strong></div>
                                <div><span>误修</span><strong>{c.false_fix ? "是：自身验证通过但隐藏测试失败" : "否"}</strong></div>
                                <div><span>人工介入</span><strong>{c.needs_human == null ? "报告未提供" : c.needs_human ? "是" : "否"}</strong></div>
                                <div><span>耗时</span><strong>{c.duration_s != null ? duration(c.duration_s) : "—"}</strong></div>
                                <div><span>Token</span><strong>{compactNumber(c.tokens)}</strong></div>
                              </div>
                              {c.notes.length > 0 && (
                                <div className="case-notes">
                                  <span>备注</span>
                                  <ul>{c.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
                                </div>
                              )}
                              {c.error && (
                                <div className="case-error">
                                  <span>执行错误</span>
                                  <pre className="code-block">{c.error}</pre>
                                </div>
                              )}
                              {c.incident_id && (
                                <a
                                  href={`/incidents/${c.incident_id}`} className="case-incident-link"
                                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); navigate(`/incidents/${c.incident_id}`); }}
                                >
                                  <Icon name="external-link" size={13} /> 打开对应事故 {c.incident_id}
                                </a>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

type CaseFilter = "all" | "wrong" | "false_fix" | "human" | "error";

function CaseFilterButton({ value, count, current, onSelect, label }: {
  value: CaseFilter; count: number; current: CaseFilter; onSelect: (v: CaseFilter) => void; label: string;
}) {
  return (
    <button type="button" role="tab" aria-selected={current === value} onClick={() => onSelect(value)}>
      {label}<span className="tab-count">{count}</span>
    </button>
  );
}

function Mark({ ok, label }: { ok: boolean | null; label: string }) {
  if (ok === null) {
    return <span className="eval-mark na" title={`${label}：不适用`} aria-label={`${label}：不适用`}>–</span>;
  }
  return (
    <span className={`eval-mark ${ok ? "pass" : "fail"}`} title={`${label}：${ok ? "通过" : "未通过"}`} aria-label={`${label}：${ok ? "通过" : "未通过"}`}>
      <Icon name={ok ? "check" : "x"} size={12} />
    </span>
  );
}

function RootCause({ c }: { c: EvalCase }) {
  if (c.root_cause_graded === false) return <span className="muted small" title="旧报告未记录根因判定">未判定</span>;
  if (c.root_cause_top1) return <Badge tone="success">Top-1</Badge>;
  if (c.root_cause_top3) return <Badge tone="attention">Top-3</Badge>;
  return <Badge tone="danger">未命中</Badge>;
}

function activeElapsed(r: EvalRun, now: number): string | null {
  if (!r.started_at) return null;
  const start = new Date(r.started_at).getTime();
  const end = r.finished_at ? new Date(r.finished_at).getTime() : now;
  return duration(Math.max(0, (end - start) / 1000));
}

function clampPct(v: number): number {
  if (!Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(100, v * 100));
}

function caseSet(meta: Record<string, string> | undefined): Set<string> | null {
  if (!meta) return null;
  const raw = meta["用例"] ?? meta["cases"];
  if (!raw || typeof raw !== "string") return null;
  const ids = raw.split(/[\s,，]+/).map((s) => s.trim()).filter(Boolean);
  return ids.length ? new Set(ids) : null;
}

function comparability(base: Record<string, string> | undefined, other: Record<string, string> | undefined): boolean | null {
  const a = caseSet(base);
  const b = caseSet(other);
  if (!a || !b) return null;
  if (a.size !== b.size) return false;
  for (const id of a) if (!b.has(id)) return false;
  return true;
}