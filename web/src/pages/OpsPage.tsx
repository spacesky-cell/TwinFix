import { useCallback, useEffect, useState } from "react";
import { api, type CostStats, type IncidentState, type RuntimeStats } from "../api";
import { Icon } from "../components/Icons";
import { Badge, Card, EmptyState, ErrorState, KpiCard, LoadingState, Spinner } from "../components/ui";
import { Ring, Sparkline, StackedBar, type VizTone } from "../components/viz";
import { AGENT_LABEL, compactNumber, duration, elapsedSince, STATE_LABEL } from "../format";
import { navigate } from "../router";

const COST_DAYS = [7, 14, 30] as const;

const STATE_TONE: Partial<Record<IncidentState, VizTone>> = {
  AWAITING_REVIEW: "attention",
  NEEDS_HUMAN: "danger",
  OBSERVING: "brand",
  FIXING: "active",
  DIAGNOSING: "active",
  VERIFYING: "active",
  REPRODUCING: "active",
};

/** 运行态与用量看板：容量、队列积压、token 与运行次数趋势（金额以服务商控制台为准，这里不做估算）。 */
export function OpsPage() {
  const [runtime, setRuntime] = useState<RuntimeStats | null>(null);
  const [cost, setCost] = useState<CostStats | null>(null);
  const [days, setDays] = useState<number>(7);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const [r, c] = await Promise.all([api.runtimeStats(), api.costStats(days)]);
      setRuntime(r);
      setCost(c);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRefreshing(false);
    }
  }, [days]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const timer = window.setInterval(() => void load(true), 20_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const states = runtime
    ? (Object.entries(runtime.incidents) as [IncidentState, number][]).filter(([, n]) => n > 0)
      .sort((a, b) => b[1] - a[1])
    : [];
  const twinUse = runtime && runtime.twin_capacity > 0 ? runtime.twin_in_use / runtime.twin_capacity : 0;
  const daily = cost?.daily ?? [];
  const tokenMax = Math.max(1, ...daily.map((d) => d.tokens));

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">运行</h1>
        </div>
        <div className="page-header-side">
          <button type="button" className="btn btn-sm" onClick={() => void load()} disabled={refreshing}>
            {refreshing ? <Spinner size={13} /> : <Icon name="refresh" size={13} />} 刷新
          </button>
        </div>
      </header>

      {error && !runtime && <ErrorState title="无法加载运行数据" message={error} onRetry={() => void load()} />}
      {!runtime && !error && <div className="card"><LoadingState label="正在读取运行态…" /></div>}

      {runtime && (
        <>
          <div className="kpi-strip">
            <KpiCard
              icon="activity" label="进行中的运行" value={runtime.active_runs}
              foot={runtime.oldest_active_run_at ? `最长已运行 ${elapsedSince(runtime.oldest_active_run_at)}` : "调度器空闲"}
              tone={runtime.active_runs > 0 ? "active" : "neutral"}
            />
            <KpiCard
              icon="clipboard-check" label="待审批" value={runtime.pending_approvals} tone={runtime.pending_approvals > 0 ? "attention" : "neutral"}
              foot={runtime.pending_approvals > 0 ? "有人工待办" : "没有待办"}
            />
            <KpiCard
              icon="box" label="孪生占用" value={`${runtime.twin_in_use} / ${runtime.twin_capacity}`}
              tone={twinUse >= 0.9 ? "danger" : twinUse >= 0.7 ? "attention" : "neutral"}
              foot={twinUse >= 0.9 ? "容量接近打满" : "并发沙箱"}
              visual={<Ring value={twinUse} tone={twinUse >= 0.9 ? "danger" : twinUse >= 0.7 ? "attention" : "brand"} label="孪生占用率" />}
            />
            <KpiCard
              icon="alert-triangle" label="疑似卡住" value={runtime.stuck_incidents} tone={runtime.stuck_incidents > 0 ? "attention" : "neutral"}
              foot={`超过 ${duration(runtime.limits.state_timeout_s)} 未推进`}
            />
          </div>

          <div className="ops-grid">
            <Card title="事故状态分布" icon="layers" count={states.length}>
              {states.length === 0 ? (
                <EmptyState icon="inbox" title="还没有事故" description="上报信号或手动新建后，这里会显示各状态的积压情况。" />
              ) : (
                <>
                  <StackedBar
                    label="事故状态分布"
                    segments={states.map(([s, n]) => ({
                      value: n,
                      tone: STATE_TONE[s] ?? "neutral",
                      label: `${STATE_LABEL[s] ?? s} ${n}`,
                    }))}
                  />
                  <ul className="state-dist">
                    {states.map(([s, n]) => (
                      <li key={s}>
                        <span className={`state-dot tone-${STATE_TONE[s] ?? "neutral"}`} aria-hidden />
                        <span>{STATE_LABEL[s] ?? s}</span>
                        <strong className="mono">{n}</strong>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </Card>

            <Card title="队列与容量" icon="server">
              <dl className="ops-rows">
                <div><dt>状态超时</dt><dd>{duration(runtime.limits.state_timeout_s)}</dd></div>
                <div><dt>评审有效期</dt><dd>{duration(runtime.limits.review_timeout_s)}</dd></div>
                <div><dt>步骤重试</dt><dd>{runtime.limits.step_max_retries} 次</dd></div>
                <div><dt>孪生并发上限</dt><dd>{runtime.limits.twin_max_concurrent}</dd></div>
                <div>
                  <dt>孪生状态</dt>
                  <dd className="mono">
                    {Object.entries(runtime.twins).filter(([, n]) => n > 0).map(([s, n]) => `${s} ${n}`).join(" · ") || "无"}
                  </dd>
                </div>
              </dl>
              <p className="hint">卡住的事故超过状态超时后由 sweeper 兜底推进；孪生占满时新事故会排队等待端口。</p>
            </Card>
          </div>
        </>
      )}

      <section className="settings-block">
        <div className="section-head">
          <div>
            <h2 className="section-title">用量</h2>
            <p className="section-desc">近 {days} 天的 token、运行次数与耗时；金额以模型服务商控制台为准。</p>
          </div>
          <div className="filter-tabs" role="tablist" aria-label="统计周期">
            {COST_DAYS.map((d) => (
              <button
                key={d} type="button" role="tab" aria-selected={days === d}
                className={days === d ? "active" : ""} onClick={() => setDays(d)}
              >
                {d} 天
              </button>
            ))}
          </div>
        </div>

        {error && cost && (
          <div className="banner banner-danger"><Icon name="alert-circle" size={15} /><span>刷新失败：{error}</span></div>
        )}
        {!cost && !error && <div className="card"><LoadingState label="正在汇总用量…" /></div>}
        {cost && (
          <>
            <div className="kpi-strip">
              <KpiCard icon="bot" label="Agent 运行" value={cost.totals.runs} foot={`${days} 天内`} />
              <KpiCard
                icon="gauge" label="Token" value={compactNumber(cost.totals.tokens)}
                foot={`输入 ${compactNumber(cost.totals.input_tokens)} · 输出 ${compactNumber(cost.totals.output_tokens)}`}
                visual={<Sparkline values={daily.map((d) => d.tokens / tokenMax)} tone="brand" label="每日 token 趋势" />}
              />
              <KpiCard
                icon="timer" label="平均耗时" value={duration(cost.totals.avg_latency_ms / 1000)}
                foot="单次 Agent 运行"
              />
            </div>

            <div className="ops-grid">
              <Card title="按 Agent" icon="bot" count={cost.by_agent.length}>
                {cost.by_agent.length === 0 ? (
                  <EmptyState icon="bot" title="没有运行记录" description="Agent 运行后这里会按角色汇总用量。" />
                ) : (
                  <table className="runs-table">
                    <thead>
                      <tr><th>Agent</th><th className="num">运行</th><th className="num">Token</th></tr>
                    </thead>
                    <tbody>
                      {cost.by_agent.map((a) => (
                        <tr key={a.agent}>
                          <td>{AGENT_LABEL[a.agent] ?? a.agent}</td>
                          <td className="num mono">{a.runs}</td>
                          <td className="num mono">{compactNumber(a.tokens)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>

              <Card title="Token 消耗最高的事故" icon="alert-triangle" count={cost.top_incidents.length}>
                {cost.top_incidents.length === 0 ? (
                  <EmptyState icon="inbox" title="还没有消耗" description="事故产生 token 消耗后会出现在这里。" />
                ) : (
                  <ul className="cost-list">
                    {cost.top_incidents.map((it) => (
                      <li key={it.id}>
                        <a
                          href={`/incidents/${it.id}`}
                          onClick={(e) => { e.preventDefault(); navigate(`/incidents/${it.id}`); }}
                        >
                          <span className="cost-title">{it.title}</span>
                          <Badge tone="neutral">{STATE_LABEL[it.state] ?? it.state}</Badge>
                          <span className="mono small">{compactNumber(it.cost_tokens)} tok</span>
                        </a>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </div>

            <Card title="每日趋势" icon="chart" count={`${cost.daily.length} 天`}>
              {cost.daily.length === 0 ? (
                <EmptyState icon="chart" title="窗口内没有运行" description="换一个更长的统计周期试试。" />
              ) : (
                <div className="daily-bars">
                  {daily.map((d) => (
                    <div className="daily-bar" key={d.date} title={`${d.date}：${d.runs} 次运行，${compactNumber(d.tokens)} tokens`}>
                      <span className="daily-bar-fill" style={{ height: `${Math.max(4, (d.tokens / tokenMax) * 100)}%` }} />
                      <span className="daily-bar-label">{d.date.slice(5)}</span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </>
        )}
      </section>
    </div>
  );
}

