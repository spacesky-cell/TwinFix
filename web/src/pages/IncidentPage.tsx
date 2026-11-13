import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, downloadReport, subscribe, TERMINAL, type Attempt, type Check, type EvidenceItem, type IncidentDetail,
  type IncidentEvent, type LiveState, type ProjectDetail, type RunDetail, type SimilarIncident,
} from "../api";
import { Icon } from "../components/Icons";
import { SeverityBadge, StateBadge } from "../components/StateBadge";
import { Timeline } from "../components/Timeline";
import {
  Badge, Card, ConfirmDialog, CopyButton, EmptyState, ErrorState, KpiCard, LiveStatus, LoadingState,
  Modal, ReasonDialog, Spinner,
} from "../components/ui";
import { StackedBar, type VizTone } from "../components/viz";
import {
  AGENT_LABEL, CHECK_LABEL, clock, compactNumber, dateTime, duration,
  isTerminalState, PIPELINE_STEPS, relTime, STATE_LABEL,
} from "../format";
import { navigate } from "../router";

export function IncidentPage({ id, role }: { id: string; role: "admin" | "maintainer" | "viewer" }) {
  const [inc, setInc] = useState<IncidentDetail | null>(null);
  const [events, setEvents] = useState<IncidentEvent[]>([]);
  const [live, setLive] = useState<LiveState>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const refresh = useRef<number | undefined>(undefined);
  const loadEvents = useRef(false);

  const load = useCallback(async () => {
    try {
      const d = await api.incident(id);
      setInc(d);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    setInc(null);
    setEvents([]);
    setError(null);
    loadEvents.current = false;
    void load();
    // 先补历史，再由 SSE 追加实时事件；两份数据按 id 合并，断线时也不会丢时间线
    api.events(id).then((list) => {
      const visible = list.filter((e) => e.kind !== "usage");
      if (loadEvents.current) setEvents((prev) => mergeEvents(prev, visible));
      else setEvents(visible.sort((a, b) => a.id - b.id));
      loadEvents.current = true;
    }).catch(() => undefined);
    const off = subscribe(`/incidents/${id}/stream`, (ev) => {
      // usage 事件只用来触发详情刷新，不写进时间线
      if (ev.kind !== "usage") setEvents((prev) => mergeEvents(prev, [ev]));
      if (ev.kind !== "tool_call") {
        window.clearTimeout(refresh.current);
        refresh.current = window.setTimeout(() => void load(), 250);
      }
    }, setLive);
    return () => { off(); window.clearTimeout(refresh.current); };
  }, [id, load]);

  // 项目配置决定预算上限与所需审批人数；拿不到时按默认（无预算、1 人）展示
  useEffect(() => {
    if (!inc) return;
    if (project && project.id === inc.project_id) return;
    api.projects().then((list) => {
      setProject(list.find((p) => p.id === inc.project_id) ?? null);
    }).catch(() => setProject(null));
  }, [inc, project]);

  if (error && !inc) {
    return (
      <div className="page">
        <a className="back-link" href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }}>
          <Icon name="arrow-left" size={14} /> 返回事故列表
        </a>
        <ErrorState title="无法加载事故" message={error} onRetry={() => void load()} />
      </div>
    );
  }
  if (!inc) {
    return (
      <div className="page">
        <div className="detail-loading">
          <LoadingState label="正在加载事故详情…" />
        </div>
      </div>
    );
  }

  const terminal = isTerminalState(inc.state);
  const runs = inc.runs.filter((r) => r.agent !== "orchestrator");
  const scenario = typeof inc.meta?.scenario === "string" ? inc.meta.scenario : null;
  const budget = Number(project?.config?.budget_per_incident_tokens ?? 0) || 0;
  const requiredApprovals = Math.max(1, Number(project?.config?.required_merge_approvals ?? 1) || 1);

  return (
    <div className="page detail-page">
      <div className="detail-topline">
        <a className="back-link" href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }}>
          <Icon name="arrow-left" size={14} /> 事故列表
        </a>
        <div className="detail-topline-actions">
          <LiveStatus state={live} />
          <CopyButton text={window.location.href} label="复制链接" />
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => void load()}>
            <Icon name="refresh" size={13} /> 刷新
          </button>
        </div>
      </div>

      <header className="incident-header">
        <div className="incident-kicker">
          <SeverityBadge severity={inc.severity} />
          <span className="incident-id mono" title={inc.id}>{inc.id}</span>
          <CopyButton text={inc.id} />
          <StateBadge state={inc.state} />
        </div>
        <h1 className="incident-title-main">{inc.title}</h1>
        <div className="incident-meta">
          <span className="meta-item"><Icon name="server" size={13} />{inc.env}</span>
          <span className="meta-item"><Icon name="box" size={13} />{inc.service ?? "未标注服务"}</span>
          <span className="meta-item"><Icon name="git-branch" size={13} />
            <span className="mono">{inc.target_branch ?? "—"}</span>
            {inc.fix_branch && <><span className="meta-arrow">←</span><span className="mono">{inc.fix_branch}</span></>}
          </span>
          {inc.pr_number != null && <span className="meta-item"><Icon name="git-merge" size={13} />PR #{inc.pr_number}</span>}
          {scenario && <span className="meta-item" title="创建事故时选择的模拟场景"><Icon name="flask" size={13} />{scenario}</span>}
          <span className="meta-item"><Icon name="clock" size={13} />创建于 {dateTime(inc.created_at)}</span>
          <span className="meta-item"><AssigneePicker inc={inc} canWrite={role !== "viewer"} onChanged={() => void load()} /></span>
        </div>
        {error && <div className="banner banner-danger inline-banner"><Icon name="alert-circle" size={15} /><span>刷新失败：{error}</span></div>}
      </header>

      <div className="kpi-strip detail-stats">
        <KpiCard
          icon="activity" label="当前阶段" tone="active" value={STATE_LABEL[inc.state]}
          foot={phaseFoot(inc.state)}
        />
        <KpiCard
          icon="timer" label={terminal ? "总耗时" : "已运行"} value={<Elapsed since={inc.created_at} until={terminal ? inc.updated_at : null} />}
          foot={terminal ? "创建至结束" : "创建至今"}
        />
        <KpiCard
          icon="gauge" label="Token 消耗" value={compactNumber(inc.cost_tokens)}
          foot={budgetFoot(inc.cost_tokens, budget)}
          visual={<TokenBar runs={runs} />}
        />
        <KpiCard
          icon="rotate-ccw" label="修复尝试" tone={inc.attempts.length > 1 ? "attention" : "neutral"}
          value={String(inc.attempts.length)} foot={inc.attempts.length > 1 ? "经过多轮返工" : "单轮修复"}
        />
      </div>

      <Pipeline inc={inc} events={events} />

      <ActionPanel inc={inc} events={events} required={requiredApprovals} onChanged={() => void load()} />

      <div className="detail-grid">
        <div className="detail-main">
          <SimilarCard incidentId={inc.id} />
          {inc.diagnosis ? <DiagnosisCard diagnosis={inc.diagnosis} /> : (
            <Card title="诊断结论" icon="target">
              <EmptyState icon="target" title="诊断尚未产出" description="Agent 尚未完成诊断。" />
            </Card>
          )}
          {inc.attempts.length > 0 ? <AttemptsCard attempts={inc.attempts} /> : (
            <Card title="修复与验证" icon="file-code">
              <EmptyState icon="file-code" title="还没有修复尝试" description="Agent 还未产出补丁。" />
            </Card>
          )}
          {runs.length > 0 ? <RunsCard inc={inc} /> : (
            <Card title="Agent 运行" icon="bot">
              <EmptyState icon="bot" title="还没有 Agent 运行记录" description="调度器尚未执行。" />
            </Card>
          )}
          <EvidenceCard incidentId={inc.id} version={events.length} />
          {inc.reports.length > 0 && <ReportsCard inc={inc} />}
        </div>
        <aside className="detail-side">
          <Timeline events={events} live={live} />
        </aside>
      </div>
    </div>
  );
}

function mergeEvents(prev: IncidentEvent[], incoming: IncidentEvent[]): IncidentEvent[] {
  const map = new Map(prev.map((e) => [e.id, e]));
  for (const e of incoming) map.set(e.id, e);
  return [...map.values()].sort((a, b) => a.id - b.id);
}

/** 当前状态在主干流程中的位置；不在主干上（如转人工）时只说明不在流程里。 */
function phaseFoot(state: string): string {
  const idx = PIPELINE_STEPS.findIndex((s) => (s.states as string[]).includes(state));
  if (idx >= 0) return `第 ${idx + 1} / ${PIPELINE_STEPS.length} 步`;
  return state === "NEEDS_HUMAN" ? "等待人工处理" : "已离开主干流程";
}

const AGENT_TONE: Record<string, VizTone> = {
  diagnoser: "brand",
  reproducer: "active",
  fixer: "attention",
  verifier: "success",
  reporter: "neutral",
};

/** 运行中的耗时每秒自增；只重渲染这一小块，不带着整页刷新。 */
function Elapsed({ since, until }: { since: string; until: string | null }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (until) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [until]);
  const end = until ? new Date(until).getTime() : now;
  return <>{duration(Math.max(0, (end - new Date(since).getTime()) / 1000))}</>;
}

/** 观察期剩余时间倒计时；到点后由 sweeper 唤醒，页面等推送刷新即可。 */
function Countdown({ until }: { until: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const left = new Date(until).getTime() - now;
  return <>{left > 0 ? duration(left / 1000) : "即将结束"}</>;
}

/** 预算使用说明：没配预算时只说明累计用量。 */
function budgetFoot(used: number, budget: number): string {
  if (!budget) return `累计 ${compactNumber(used)} tokens`;
  const pctUsed = Math.min(100, Math.round((used / budget) * 100));
  return `预算 ${compactNumber(budget)} · 已用 ${pctUsed}%`;
}

function AssigneePicker({ inc, canWrite, onChanged }: {
  inc: IncidentDetail; canWrite: boolean; onChanged: () => void;
}) {
  const [users, setUsers] = useState<{ id: string; name: string }[] | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 详情自带负责人名字；账号列表拿到后再用列表里的名字覆盖（刚改完指派时更及时）
  const name = users?.find((u) => u.id === inc.assignee_id)?.name ?? inc.assignee?.name ?? null;

  useEffect(() => { setOpen(false); }, [inc.id]);

  async function pick(userId: string | null) {
    setBusy(true);
    setError(null);
    try {
      await api.setAssignee(inc.id, userId);
      setOpen(false);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggle() {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (users !== null) return;
    try {
      setUsers(await api.assignableUsers());
    } catch (e) {
      setError((e as Error).message);
      setUsers([]);
    }
  }

  if (!canWrite) {
    if (!inc.assignee_id) return null;
    return (
      <span className="assignee-control">
        <span className="assignee-trigger"><Icon name="user-round" size={13} /><span>{name ?? "已指派"}</span></span>
      </span>
    );
  }
  return (
    <span className="assignee-control">
      <button
        type="button" className="assignee-trigger" onClick={() => void toggle()}
        aria-haspopup="menu" aria-expanded={open}
        title={inc.assignee_id ? "点击更换负责人" : "指派负责人"}
      >
        <Icon name="user-round" size={13} />
        <span>{inc.assignee_id ? (name ?? "已指派") : "未指派"}</span>
        <Icon name="chevron-down" size={12} />
      </button>
      {open && (
        <div className="assignee-menu" role="menu">
          {users === null && <span className="assignee-hint"><Spinner size={12} /> 加载账号…</span>}
          {users?.length === 0 && <span className="assignee-hint">没有可指派账号</span>}
          {users?.map((u) => (
            <button
              key={u.id} type="button" role="menuitem" className={`assignee-item ${u.id === inc.assignee_id ? "selected" : ""}`}
              disabled={busy} onClick={() => void pick(u.id)}
            >
              <span className="assignee-avatar" aria-hidden>{u.name.slice(0, 1).toUpperCase()}</span>
              <span>{u.name}</span>
              {u.id === inc.assignee_id && <Icon name="check" size={12} />}
            </button>
          ))}
          {inc.assignee_id && (
            <button type="button" role="menuitem" className="assignee-item clear" disabled={busy} onClick={() => void pick(null)}>
              <Icon name="x" size={12} /> 取消指派
            </button>
          )}
          {error && <span className="assignee-hint danger">{error}</span>}
        </div>
      )}
    </span>
  );
}

function RejectReviewDialog({ open, onClose, onConfirm, busy }: {
  open: boolean; onClose: () => void; busy: boolean;
  onConfirm: (reason: string, kind: "root_cause" | "patch") => void;
}) {
  const [reason, setReason] = useState("");
  const [kind, setKind] = useState<"root_cause" | "patch">("patch");
  useEffect(() => { if (open) { setReason(""); setKind("patch"); } }, [open]);
  const invalid = !reason.trim();
  return (
    <Modal
      open={open} onClose={onClose} title="驳回修复并退回 Agent" size="md"
      description="驳回会结束本轮评审，并把意见作为下一轮输入。"
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
        <button type="button" className="btn btn-danger" disabled={busy || invalid} onClick={() => onConfirm(reason.trim(), kind)}>
          {busy ? <Spinner /> : null}{busy ? "提交中…" : "确认驳回"}
        </button>
      </>}
    >
      <div className="reject-kind" role="radiogroup" aria-label="退回阶段">
        <button
          type="button" role="radio" aria-checked={kind === "patch"}
          className={`reject-option ${kind === "patch" ? "selected" : ""}`} onClick={() => setKind("patch")}
        >
          <strong>补丁需要修改</strong>
          <span>根因判断是对的，从修复阶段重做（默认）</span>
        </button>
        <button
          type="button" role="radio" aria-checked={kind === "root_cause"}
          className={`reject-option ${kind === "root_cause" ? "selected" : ""}`} onClick={() => setKind("root_cause")}
        >
          <strong>根因判断有误</strong>
          <span>从诊断阶段重新分析，再做复现与修复</span>
        </button>
      </div>
      <label className="field">
        <span>驳回原因 <em className="required">必填</em></span>
        <textarea
          data-autofocus rows={4} value={reason}
          placeholder={kind === "root_cause" ? "例如：根因是缓存过期策略，不是扣减逻辑。" : "例如：没有覆盖订单已取消的情况，请补充回归测试。"}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
    </Modal>
  );
}
function TokenBar({ runs }: { runs: IncidentDetail["runs"] }) {
  const totals = new Map<string, number>();
  for (const r of runs) {
    const n = r.input_tokens + r.output_tokens;
    if (n > 0) totals.set(r.agent, (totals.get(r.agent) ?? 0) + n);
  }
  if (totals.size === 0) return null;
  return (
    <StackedBar
      label="各 Agent 的 token 占比"
      segments={[...totals.entries()].map(([agent, value]) => ({
        value,
        tone: AGENT_TONE[agent] ?? "neutral",
        label: `${AGENT_LABEL[agent] ?? agent} ${compactNumber(value)}`,
      }))}
    />
  );
}

function Pipeline({ inc, events }: { inc: IncidentDetail; events: IncidentEvent[] }) {
  const visited = useMemo(
    () => new Set(events.flatMap((e) => (e.to_state ? [e.to_state] : []))),
    [events],
  );
  const currentIndex = PIPELINE_STEPS.findIndex((s) => s.states.includes(inc.state));
  const offPath = currentIndex < 0;
  const lastVisited = Math.max(
    -1,
    ...PIPELINE_STEPS.map((s, idx) => (s.states.some((st) => visited.has(st)) ? idx : -1)),
  );
  const activeIndex = currentIndex >= 0 ? currentIndex : lastVisited;
  const successEnd = inc.state === "MERGED" || inc.state === "CLOSED";

  return (
    <section className="pipeline-card" aria-label="处理进度">
      <ol className={`pipeline ${offPath ? "pipeline-off" : ""}`}>
        {PIPELINE_STEPS.map((step, idx) => {
          const isCurrent = idx === currentIndex;
          const done = successEnd || idx < activeIndex || (idx === activeIndex && !isCurrent && offPath);
          const cls = [
            isCurrent ? (successEnd ? "current success" : "current") : "",
            done ? "done" : "",
            offPath && idx === activeIndex ? "stopped" : "",
          ].filter(Boolean).join(" ");
          return (
            <li key={step.key} className={cls} aria-current={isCurrent ? "step" : undefined}>
              <span className="step-node">
                {done && !isCurrent ? <Icon name="check" size={11} /> : <span className="step-number">{idx + 1}</span>}
              </span>
              <span className="step-name">{step.label}</span>
            </li>
          );
        })}
      </ol>
      <div className="pipeline-foot">
        <span>
          <strong>{STATE_LABEL[inc.state]}</strong>
          <span className="muted"> · 进入于 {relTime(inc.state_entered_at)}</span>
        </span>
        {offPath ? (
          <span className="pipeline-note note-off">
            <Icon name="alert-triangle" size={13} />
            {TERMINAL.has(inc.state) ? "已结束，未走完主干流程" : "已离开主干流程，等待人工处理"}
            {inc.closed_reason ? `：${inc.closed_reason}` : ""}
          </span>
        ) : null}
      </div>
    </section>
  );
}

function ActionPanel({ inc, events, required, onChanged }: {
  inc: IncidentDetail; events: IncidentEvent[]; required: number; onChanged: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [doneMsg, setDoneMsg] = useState<string | null>(null);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [resolve, setResolve] = useState<"not_bug" | "cannot_fix" | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const pendingMerge = inc.approvals.find((a) => a.action === "merge" && a.status === "pending");
  const mergeVotes = Array.isArray(pendingMerge?.payload?.approvals)
    ? (pendingMerge?.payload?.approvals as { actor?: string; comment?: string | null; at?: string }[])
    : [];
  const terminal = isTerminalState(inc.state);
  const intent = useMemo(() => new URLSearchParams(window.location.search).get("action"), []);
  const lastAttempt = inc.attempts[inc.attempts.length - 1];
  const latestVerification = lastAttempt?.verifications[lastAttempt.verifications.length - 1];
  const lastChange = [...events].reverse().find((e) => e.kind === "transition" && e.to_state === inc.state);

  useEffect(() => {
    if (!intent || !pendingMerge) return;
    panelRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    if (intent === "reject") textRef.current?.focus();
  }, [intent, pendingMerge]);

  async function run(fn: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError(null);
    setDoneMsg(null);
    try {
      await fn();
      setText("");
      setDoneMsg(message);
      setMergeOpen(false);
      setRejectOpen(false);
      setCancelOpen(false);
      setResolve(null);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (terminal) {
    const good = inc.state === "MERGED" || inc.state === "CLOSED";
    return (
      <section className={`terminal-summary ${good ? "good" : ""}`}>
        <span className="terminal-icon"><Icon name={good ? "check-circle" : "alert-circle"} size={20} /></span>
        <div className="terminal-copy">
          <div className="terminal-title"><StateBadge state={inc.state} /> <span>事故已结束</span></div>
          <p>{inc.closed_reason || lastChange?.reason || "该事故已进入终态，Agent 不会再继续执行。"}</p>
          <span className="muted small">最近更新于 {dateTime(inc.updated_at)}</span>
        </div>
        <div className="terminal-actions">
          {inc.reports.length > 0 && (
            <a className="btn btn-sm" href="#reports"><Icon name="file-text" size={13} /> 查看报告</a>
          )}
          <a className="btn btn-sm btn-ghost" href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }}>
            <Icon name="arrow-left" size={13} /> 返回列表
          </a>
        </div>
      </section>
    );
  }

  if (inc.state === "AWAITING_REVIEW") {
    return (
      <div ref={panelRef}>
        <section className="decision-card decision-review">
          <div className="decision-head">
            <span className="decision-icon"><Icon name="clipboard-check" size={19} /></span>
            <div className="decision-heading">
              <div className="decision-title-row">
                <h2>等待你的合并评审</h2>
                <Badge tone="attention" dot>需要处理</Badge>
              </div>
            </div>
          </div>

          <div className="decision-facts">
            <div><span>合并目标</span><strong className="mono">{inc.target_branch ?? "—"}</strong></div>
            <div><span>来源分支</span><strong className="mono">{inc.fix_branch ?? "—"}</strong></div>
            <div><span>合并请求</span><strong>{inc.pr_number != null ? `PR #${inc.pr_number}` : "待创建"}</strong></div>
            {Object.entries((inc.meta.repo_prs as Record<string, { pr_number?: number }> | undefined) ?? {}).length > 0 && (
              <div>
                <span>附加仓库 PR</span>
                <strong>
                  {Object.entries((inc.meta.repo_prs as Record<string, { pr_number?: number }> | undefined) ?? {})
                    .map(([name, info]) => `${name} #${info.pr_number ?? "?"}`)
                    .join("、")}
                </strong>
              </div>
            )}
            <div><span>修复尝试</span><strong>{inc.attempts.length} 轮</strong></div>
          </div>

          {latestVerification ? (
            <VerificationPills checks={latestVerification.checks} passed={latestVerification.passed} />
          ) : (
            <p className="muted small">尚未找到验证记录，请在批准前确认修复证据。</p>
          )}

          {required > 1 && (
            <div className="vote-block">
              <div className="vote-head">
                <span><Icon name="users" size={13} /> 合并审批</span>
                <strong>{mergeVotes.length} / {required}</strong>
                <span className="muted small">{mergeVotes.length >= required ? "已收齐" : `还需 ${required - mergeVotes.length} 位审批人`}</span>
              </div>
              <div className="vote-track" role="progressbar" aria-valuemin={0} aria-valuemax={required} aria-valuenow={mergeVotes.length}>
                <span style={{ width: `${Math.min(100, (mergeVotes.length / required) * 100)}%` }} />
              </div>
              {mergeVotes.length > 0 && (
                <ul className="vote-list">
                  {mergeVotes.map((v, idx) => (
                    <li key={`${v.actor ?? "vote"}-${idx}`}>
                      <Icon name="check" size={12} />
                      <span>{v.actor?.startsWith("github:") ? `${v.actor.slice(7)}（GitHub）` : v.actor?.replace(/^user:/, "") ?? "审批人"}</span>
                      {v.comment && <span className="muted small">· {v.comment}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {inc.twin && Object.keys(inc.twin.urls ?? {}).length > 0 && (
            <div className="twin-links">
              <span>孪生环境</span>
              {Object.entries(inc.twin.urls).map(([k, v]) => (
                <a key={k} href={v} target="_blank" rel="noreferrer" className="twin-link">
                  {k}<Icon name="external-link" size={12} />
                </a>
              ))}
            </div>
          )}

          <label className="field decision-comment">
            <span>评审意见 <em className="field-tag">可选</em></span>
            <textarea
              ref={textRef} rows={2} value={text} onChange={(e) => setText(e.target.value)}
              placeholder="批准时作为备注；驳回时交回 Fixer 重修。"
            />
          </label>

          {error && <div className="banner banner-danger"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
          {doneMsg && <div className="banner banner-success"><Icon name="check-circle" size={15} /><span>{doneMsg}</span></div>}

          <div className="decision-actions">
            <button type="button" className="btn btn-primary" disabled={busy || !pendingMerge} onClick={() => setMergeOpen(true)}>
              <Icon name="git-merge" size={14} /> {required > 1 ? "投票批准" : "批准并合并"}
            </button>
            <button type="button" className="btn" disabled={busy || !pendingMerge} onClick={() => setRejectOpen(true)}>
              <Icon name="rotate-ccw" size={14} /> 驳回并退回修复
            </button>
            <span className="toolbar-spacer" />
            <button type="button" className="btn btn-ghost text-danger" disabled={busy} onClick={() => setCancelOpen(true)}>取消事故</button>
          </div>
          {required > 1 && (
            <p className="hint">
              项目配置需要 {required} 位审批人通过：每人各投一票，收齐后自动合并；任何一位驳回都会退回修复。
            </p>
          )}
          {!pendingMerge && <p className="hint">未找到待处理的合并审批，请刷新页面。</p>}
        </section>

        <ConfirmDialog
          open={mergeOpen} onClose={() => setMergeOpen(false)} busy={busy}
          onConfirm={() => pendingMerge && void run(() => api.decide(pendingMerge.id, true, text || undefined), "已提交合并审批，Agent 将继续后续流程。")}
          title="确认合并修复？" confirmLabel="确认合并"
          description="合并会改变目标分支，请确认验证证据与影响范围。"
        >
          <div className="confirm-impact">
            <p className="confirm-impact-title"><Icon name="git-merge" size={15} /> 操作影响</p>
            <ul>
              <li>将 <code>{inc.fix_branch ?? "修复分支"}</code> 合并到 <code>{inc.target_branch ?? "目标分支"}</code>。</li>
              {inc.pr_number != null && <li>合并请求：PR #{inc.pr_number}。</li>}
              <li>审批人、时间与意见会写入事故审计记录。</li>
            </ul>
          </div>
        </ConfirmDialog>

        <ConfirmDialog
          open={mergeOpen} onClose={() => setMergeOpen(false)} busy={busy}
          onConfirm={() => pendingMerge && void run(() => api.decide(pendingMerge.id, true, text || undefined), "已提交合并审批，Agent 将继续后续流程。")}
          title={required > 1 ? "确认投出批准票？" : "确认合并修复？"} confirmLabel={required > 1 ? "确认批准" : "确认合并"}
          description={required > 1 ? `收齐 ${required} 位审批人后自动合并。` : "合并会改变目标分支，请确认验证证据与影响范围。"}
        >
          <div className="confirm-impact">
            <p className="confirm-impact-title"><Icon name="git-merge" size={15} /> 操作影响</p>
            <ul>
              <li>将 <code>{inc.fix_branch ?? "修复分支"}</code> 合并到 <code>{inc.target_branch ?? "目标分支"}</code>。</li>
              {inc.pr_number != null && <li>合并请求：PR #{inc.pr_number}。</li>}
              <li>审批人、时间与意见会写入事故审计记录。</li>
            </ul>
          </div>
        </ConfirmDialog>

        <RejectReviewDialog
          open={rejectOpen} onClose={() => setRejectOpen(false)} busy={busy}
          onConfirm={(reason, kind) => pendingMerge && void run(
            () => api.decide(pendingMerge.id, false, reason, kind),
            kind === "root_cause" ? "已驳回，事故回到诊断阶段。" : "已驳回，意见已交回 Fixer。",
          )}
        />

        <ConfirmDialog
          open={cancelOpen} onClose={() => setCancelOpen(false)} busy={busy}
          onConfirm={() => void run(() => api.cancel(inc.id), "已请求取消，Agent 将在下一次工具调用时停止。")}
          title="取消这个事故？" confirmLabel="取消事故" tone="danger"
          description="取消会请求 Agent 停止，并回收孪生环境。"
        >
          <div className="confirm-impact">
            <p className="confirm-impact-title"><Icon name="alert-triangle" size={15} /> 操作影响</p>
            <ul>
              <li>Agent 会在下一次工具调用时停止，当前步骤之后不再继续。</li>
              <li>孪生环境会被回收；已产生的代码与报告保留。</li>
            </ul>
          </div>
        </ConfirmDialog>
      </div>
    );
  }

  if (inc.state === "OBSERVING") {
    const observation = (inc.meta?.observation ?? {}) as { until?: string; observe_s?: number; signals_at_merge?: number };
    const until = typeof observation.until === "string" ? observation.until : null;
    return (
      <div ref={panelRef}>
        <section className="decision-card decision-observe">
          <div className="decision-head">
            <span className="decision-icon active"><Icon name="activity" size={19} /></span>
            <div className="decision-heading">
              <div className="decision-title-row">
                <h2>修复已合并，正在观察线上表现</h2>
                <Badge tone="active" dot>观察期</Badge>
              </div>
              <p className="decision-sub">观察期内出现同一指纹的新信号，会自动转人工复核；观察期满无复发才归档。</p>
            </div>
          </div>
          <div className="decision-facts">
            <div><span>观察截止</span><strong>{until ? dateTime(until) : "—"}</strong></div>
            <div><span>剩余时间</span><strong>{until ? <Countdown until={until} /> : "—"}</strong></div>
            <div><span>观察时长</span><strong>{duration(observation.observe_s ?? null)}</strong></div>
            <div><span>合并时信号数</span><strong>{observation.signals_at_merge ?? "—"}</strong></div>
          </div>
          <div className="decision-actions">
            <button type="button" className="btn btn-sm btn-ghost text-danger" disabled={busy} onClick={() => setCancelOpen(true)}>
              取消事故
            </button>
            <span className="muted small">不需要人工操作；复发时事故会自动回到「需人工」。</span>
          </div>
          {error && <div className="banner banner-danger"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
        </section>
        <ConfirmDialog
          open={cancelOpen} onClose={() => setCancelOpen(false)} busy={busy}
          onConfirm={() => void run(() => api.cancel(inc.id), "已请求取消。")}
          title="取消这个事故？" confirmLabel="取消事故" tone="danger"
          description="取消会结束观察期，事故不再继续处理。"
        />
      </div>
    );
  }

  if (inc.state === "NEEDS_HUMAN") {
    return (
      <div ref={panelRef}>
        <section className="decision-card decision-human">
          <div className="decision-head">
            <span className="decision-icon warning"><Icon name="user-round" size={19} /></span>
            <div className="decision-heading">
              <div className="decision-title-row">
                <h2>需要人工介入</h2>
                <Badge tone="attention" dot>需要处理</Badge>
              </div>
            </div>
          </div>
          {(lastChange?.reason || inc.closed_reason) && (
            <div className="decision-reason">
              <Icon name="info" size={14} />
              <span><strong>转人工原因：</strong>{lastChange?.reason || inc.closed_reason}</span>
            </div>
          )}
          <div className="decision-facts">
            <div><span>当前阶段</span><strong>{STATE_LABEL[inc.state]}</strong></div>
            <div><span>修复尝试</span><strong>{inc.attempts.length} 轮</strong></div>
            <div><span>诊断置信度</span><strong>{inc.diagnosis ? `${Math.round(inc.diagnosis.confidence * 100)}%` : "—"}</strong></div>
            <div><span>已消耗</span><strong>{compactNumber(inc.cost_tokens)} tokens</strong></div>
          </div>
          <label className="field decision-comment">
            <span>给 Agent 的指导意见</span>
            <textarea
              rows={3} value={text} onChange={(e) => setText(e.target.value)}
              placeholder="例如：根因在缓存过期策略，不在扣减逻辑；请从诊断阶段重新分析。"
            />
          </label>
          <div className="restart-actions">
            <span className="action-label">带着指导意见重新开始</span>
            <div className="row wrap">
              {(["DIAGNOSING", "REPRODUCING", "FIXING"] as const).map((s) => (
                <button
                  key={s} type="button" className="btn btn-primary btn-sm" disabled={busy || !text.trim()}
                  onClick={() => void run(() => api.guide(inc.id, text, s), `已从${STATE_LABEL[s].replace("中", "")}阶段重新开始。`)}
                >
                  <Icon name="rotate-ccw" size={13} /> 从{STATE_LABEL[s].replace("中", "")}重来
                </button>
              ))}
            </div>
            {!text.trim() && <p className="hint">重启前需要填写指导意见。</p>}
          </div>
          <div className="resolve-actions">
            <span className="action-label">或直接给出结论</span>
            <div className="row wrap">
              <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setResolve("not_bug")}>标记为非缺陷</button>
              <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setResolve("cannot_fix")}>标记为无法修复</button>
              <button type="button" className="btn btn-sm btn-ghost text-danger" disabled={busy} onClick={() => setCancelOpen(true)}>取消事故</button>
            </div>
          </div>
          {error && <div className="banner banner-danger"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
          {doneMsg && <div className="banner banner-success"><Icon name="check-circle" size={15} /><span>{doneMsg}</span></div>}
        </section>

        <ReasonDialog
          open={resolve !== null} onClose={() => setResolve(null)} busy={busy}
          onConfirm={(reason) => resolve && void run(
            () => api.resolve(inc.id, resolve, reason || undefined),
            resolve === "not_bug" ? "已将事故标记为非缺陷。" : "已将事故标记为无法修复。",
          )}
          title={resolve === "not_bug" ? "标记为非缺陷？" : "标记为无法修复？"}
          confirmLabel="确认结束事故" tone="danger" required={false}
          description="这将结束事故，Agent 不会继续处理。"
          label="说明" placeholder="可选，会写入事故记录，便于后续复盘。"
        />

        <ConfirmDialog
          open={cancelOpen} onClose={() => setCancelOpen(false)} busy={busy}
          onConfirm={() => void run(() => api.cancel(inc.id), "已请求取消，Agent 将在下一次工具调用时停止。")}
          title="取消这个事故？" confirmLabel="取消事故" tone="danger"
          description="取消会请求 Agent 停止，并回收孪生环境。"
        />
      </div>
    );
  }

  return (
    <div ref={panelRef}>
      <section className="guide-bar">
        <div className="guide-copy">
          <span className="guide-icon"><Icon name="zap" size={15} /></span>
          <div>
            <strong>Agent 正在处理</strong>
            <span>指导会注入给下一个 Agent。</span>
          </div>
        </div>
        <div className="guide-form">
          <input
            name="guide" value={text} onChange={(e) => setText(e.target.value)}
            placeholder="例如：优先检查库存扣减的并发边界"
            aria-label="中途指导"
            onKeyDown={(e) => {
              if (e.key === "Enter" && text.trim() && !busy) {
                void run(() => api.guide(inc.id, text), "指导已发送。");
              }
            }}
          />
          <button type="button" className="btn btn-sm btn-primary" disabled={busy || !text.trim()} onClick={() => void run(() => api.guide(inc.id, text), "指导已发送。")}>
            {busy ? <Spinner size={13} /> : <Icon name="arrow-up-right" size={13} />} 发送指导
          </button>
          <button type="button" className="btn btn-sm btn-ghost text-danger" disabled={busy || inc.cancel_requested} onClick={() => setCancelOpen(true)}>
            {inc.cancel_requested ? "取消中…" : "取消事故"}
          </button>
        </div>
      </section>
      {error && <div className="banner banner-danger"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
      {doneMsg && <div className="banner banner-success"><Icon name="check-circle" size={15} /><span>{doneMsg}</span></div>}
      <ConfirmDialog
        open={cancelOpen} onClose={() => setCancelOpen(false)} busy={busy}
        onConfirm={() => void run(() => api.cancel(inc.id), "已请求取消，Agent 将在下一次工具调用时停止。")}
        title="取消这个事故？" confirmLabel="取消事故" tone="danger"
        description="取消会请求 Agent 停止，并回收孪生环境。"
      >
        <div className="confirm-impact">
          <p className="confirm-impact-title"><Icon name="alert-triangle" size={15} /> 操作影响</p>
          <ul>
            <li>Agent 会在下一次工具调用时停止，当前步骤之后不再继续。</li>
            <li>孪生环境会被回收；已产生的代码与报告保留。</li>
          </ul>
        </div>
      </ConfirmDialog>
    </div>
  );
}

function SimilarCard({ incidentId }: { incidentId: string }) {
  const [items, setItems] = useState<SimilarIncident[] | null>(null);
  useEffect(() => {
    let alive = true;
    api.similar(incidentId, 3)
      .then((r) => { if (alive) setItems(r.items); })
      .catch(() => { if (alive) setItems([]); });
    return () => { alive = false; };
  }, [incidentId]);

  if (!items || items.length === 0) return null;
  return (
    <Card title="相似历史事故" icon="layers" count={items.length} subtitle="同一项目里有结论的过往事故，可作为根因判断的参考。">
      <ul className="similar-list">
        {items.map((it) => (
          <li key={it.incident_id} className="similar-item">
            <div className="similar-head">
              <a
                href={`/incidents/${it.incident_id}`} className="similar-title"
                onClick={(e) => { e.preventDefault(); navigate(`/incidents/${it.incident_id}`); }}
              >
                {it.title}
              </a>
              <Badge tone={it.verdict === "fixed" ? "success" : "neutral"} dot>
                {it.verdict === "fixed" ? "已修复" : it.verdict === "NOT_BUG" ? "非缺陷" : it.verdict === "ALREADY_FIXED" ? "已被修复" : it.verdict}
              </Badge>
              <span className="muted small">相似度 {Math.round(it.similarity * 100)}%</span>
            </div>
            {it.root_cause && <p className="similar-cause">{it.root_cause}</p>}
            {it.fix?.files_changed?.length ? (
              <div className="file-chips">
                {it.fix.files_changed.slice(0, 4).map((f) => (
                  <span className="file-chip" key={f}><Icon name="file-code" size={11} /><span className="mono">{f}</span></span>
                ))}
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function DiagnosisCard({ diagnosis }: { diagnosis: NonNullable<IncidentDetail["diagnosis"]> }) {
  const groups = [
    { key: "confirmed", label: "已确认", tone: "success" as const },
    { key: "open", label: "待验证", tone: "attention" as const },
    { key: "rejected", label: "已排除", tone: "neutral" as const },
  ].map((g) => ({ ...g, rows: diagnosis.hypotheses.filter((h) => h.status === g.key) }))
    .filter((g) => g.rows.length > 0);

  return (
    <Card title="诊断结论" icon="target" count={diagnosis.hypotheses.length ? `${diagnosis.hypotheses.length} 个假设` : undefined}>
      <div className="confidence-row">
        <span className="confidence-label">诊断置信度</span>
        <div className="confidence-track" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(diagnosis.confidence * 100)}>
          <span style={{ width: `${Math.round(diagnosis.confidence * 100)}%` }} />
        </div>
        <strong className="confidence-value">{Math.round(diagnosis.confidence * 100)}%</strong>
      </div>

      <div className="root-cause">
        <span className="root-cause-label"><Icon name="target" size={13} /> 根因结论</span>
        <p>{diagnosis.root_cause || "尚未给出根因结论"}</p>
      </div>
      {diagnosis.summary && diagnosis.summary !== diagnosis.root_cause && (
        <p className="diagnosis-summary">{diagnosis.summary}</p>
      )}

      {groups.length > 0 && (
        <div className="hypotheses-wrap">
          <h3 className="subhead">根因假设</h3>
          {groups.map((g) => (
            <div key={g.key} className={`hypothesis-group group-${g.key}`}>
              <div className="hypothesis-group-head">
                <Badge tone={g.tone} dot>{g.label}</Badge>
                <span className="muted small">{g.rows.length}</span>
              </div>
              <ul className="hypothesis-list">
                {g.rows.map((h) => (
                  <li key={h.id} className={h.status}>
                    <div className="hypothesis-line">
                      <span className="hypothesis-statement">{h.statement}</span>
                      <span className="hypothesis-confidence">{Math.round(h.confidence * 100)}%</span>
                    </div>
                    <span className="hypothesis-meter" aria-hidden><span style={{ width: `${Math.round(h.confidence * 100)}%` }} /></span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {diagnosis.suspect_locations.length > 0 && (
        <div className="locations-block">
          <h3 className="subhead">相关代码位置</h3>
          <div className="location-list">
            {diagnosis.suspect_locations.map((l, idx) => {
              const path = `${l.file}${l.line != null ? `:${l.line}` : ""}`;
              return (
                <div className="location-row" key={`${path}-${idx}`}>
                  <Icon name="file-code" size={14} />
                  <span className="mono location-path">{l.file}</span>
                  {l.line != null && <span className="location-line">:{l.line}</span>}
                  <CopyButton text={path} />
                </div>
              );
            })}
          </div>
        </div>
      )}

      {diagnosis.repro && (
        <details className="disclosure">
          <summary>
            <Icon name="terminal" size={14} />
            <span>复现用例</span>
            <span className="mono disclosure-meta">{diagnosis.repro.test_selector}</span>
            <Icon name="chevron-down" size={14} className="disclosure-chevron" />
          </summary>
          <div className="disclosure-body">
            <div className="code-label">执行命令 <CopyButton text={diagnosis.repro.command} /></div>
            <pre className="code-block">{diagnosis.repro.command}</pre>
            <div className="code-label">失败输出 <CopyButton text={diagnosis.repro.failure_output} /></div>
            <pre className="code-block">{diagnosis.repro.failure_output}</pre>
          </div>
        </details>
      )}
    </Card>
  );
}

function AttemptsCard({ attempts }: { attempts: Attempt[] }) {
  const [selected, setSelected] = useState<number | null>(null);
  const current = attempts.find((a) => a.seq === selected) ?? attempts[attempts.length - 1]!;
  const verifications = current.verifications;
  const latest = verifications[verifications.length - 1];
  const older = verifications.slice(0, -1);

  return (
    <Card title="修复与验证" icon="file-code" count={`${attempts.length} 轮`}>
      <div className="attempt-tabs" role="tablist" aria-label="选择修复尝试">
        {attempts.map((a) => (
          <button
            key={a.id} type="button" role="tab" aria-selected={a.seq === current.seq}
            className={`attempt-tab ${a.seq === current.seq ? "active" : ""}`}
            onClick={() => setSelected(a.seq)}
          >
            <span>#{a.seq}</span>
            <span className={`attempt-dot ${attemptTone(a.status)}`} aria-hidden />
            {attemptStatus(a.status)}
          </button>
        ))}
      </div>

      <div className="attempt-body">
        <div className="attempt-summary">
          <div className="attempt-summary-main">
            <Badge tone={attemptTone(current.status)} dot>{attemptStatus(current.status)}</Badge>
            <span className="muted small">触发：{triggerLabel(current.trigger)}</span>
            {current.created_at && <span className="muted small">· {dateTime(current.created_at)}</span>}
          </div>
          {current.patch?.commit_sha && (
            <span className="commit-ref">commit <CopyButton text={current.patch.commit_sha} /><span className="mono">{current.patch.commit_sha.slice(0, 10)}</span></span>
          )}
        </div>

        {current.feedback && (
          <div className="feedback-box">
            <Icon name="info" size={14} />
            <div><strong>本轮输入意见</strong><p>{current.feedback}</p></div>
          </div>
        )}

        {current.patch ? (
          <div className="patch-section">
            <div className="patch-head">
              <div>
                <strong>补丁说明</strong>
                <p>{current.patch.explanation || "未提供说明"}</p>
              </div>
              {current.patch.touches_tests && (
                <Badge tone="danger" dot>修改了测试文件</Badge>
              )}
            </div>
            <div className="file-chips">
              {current.patch.files_changed.map((f) => (
                <span className="file-chip" key={f}><Icon name="file-code" size={12} /><span className="mono">{f}</span></span>
              ))}
            </div>
            <div className="diff-head"><span>代码差异</span><CopyButton text={current.patch.diff} label="复制 diff" /></div>
            <DiffBlock diff={current.patch.diff} />
            {Object.entries(current.patch.repos ?? {}).map(([name, repo]) => (
              <div className="extra-patch" key={name}>
                <div className="diff-head">
                  <span>附加仓库 <span className="mono">{name}</span></span>
                  {repo.pr_url && <a className="linklike small" href={repo.pr_url} target="_blank" rel="noreferrer">PR #{repo.pr_number}</a>}
                </div>
                {repo.files_changed.length > 0 && (
                  <div className="file-chips">
                    {repo.files_changed.map((f) => (
                      <span className="file-chip" key={f}><Icon name="file-code" size={12} /><span className="mono">{f}</span></span>
                    ))}
                  </div>
                )}
                <DiffBlock diff={repo.diff} />
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon="file-code" title="本轮还没有补丁" description="Fixer 产出修改后会显示在这里。" />
        )}

        <div className="verification-section" id="verification">
          <div className="subhead-row">
            <h3 className="subhead">验证结果</h3>
            {latest && (
              <span className="muted small">
                {latest.checks.filter((c) => c.status === "passed").length} / {latest.checks.length} 项通过 · {dateTime(latest.created_at)}
              </span>
            )}
          </div>
          {latest ? (
            <VerificationBlock checks={latest.checks} passed={latest.passed} />
          ) : (
            <p className="muted small">本轮尚未产生验证记录。</p>
          )}
          {older.length > 0 && (
            <details className="disclosure history-disclosure">
              <summary>
                <Icon name="rotate-ccw" size={13} />
                <span>此前 {older.length} 次验证</span>
                <Icon name="chevron-down" size={13} className="disclosure-chevron" />
              </summary>
              <div className="disclosure-body">
                {older.slice().reverse().map((v, idx) => (
                  <div className="history-verification" key={`${v.created_at}-${idx}`}>
                    <div className="history-verification-head">
                      <Badge tone={v.passed ? "success" : "danger"} dot>{v.passed ? "通过" : "未通过"}</Badge>
                      <span className="muted small">{dateTime(v.created_at)}</span>
                    </div>
                    <VerificationBlock checks={v.checks} passed={v.passed} compact />
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      </div>
    </Card>
  );
}

function VerificationBlock({ checks, passed, compact = false }: { checks: Check[]; passed: boolean; compact?: boolean }) {
  return (
    <div className={`verification-block ${compact ? "compact" : ""}`}>
      {!compact && (
        <div className={`verification-verdict ${passed ? "passed" : "failed"}`}>
          <Icon name={passed ? "shield-check" : "shield-alert"} size={16} />
          <span>{passed ? "验证通过" : "验证未通过"}</span>
        </div>
      )}
      <ul className="check-list">
        {checks.map((c) => (
          <li key={c.name} className={`check-item ${c.status}`}>
            <span className="check-icon" aria-hidden>{c.status === "passed" ? <Icon name="check" size={12} /> : c.status === "failed" ? <Icon name="x" size={12} /> : "–"}</span>
            <span className="check-name">{CHECK_LABEL[c.name] ?? c.name}</span>
            <span className="check-detail mono">{c.detail || "—"}</span>
            <span className="check-state">{c.status === "passed" ? "通过" : c.status === "failed" ? "未通过" : "跳过"}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function VerificationPills({ checks, passed }: { checks: Check[]; passed: boolean }) {
  return (
    <div className="verification-pills">
      <Badge tone={passed ? "success" : "danger"} dot>{passed ? "最近验证通过" : "最近验证未通过"}</Badge>
      {checks.map((c) => (
        <span key={c.name} className={`check-pill ${c.status}`} title={c.detail || undefined}>
          <Icon name={c.status === "passed" ? "check" : c.status === "failed" ? "x" : "circle-dot"} size={11} />
          {CHECK_LABEL[c.name] ?? c.name}
        </span>
      ))}
    </div>
  );
}

function DiffBlock({ diff }: { diff: string }) {
  return (
    <pre className="diff-block" aria-label="代码差异">
      {diff.split("\n").map((line, i) => {
        const kind = line.startsWith("+++") || line.startsWith("---") ? "meta"
          : line.startsWith("+") ? "add"
            : line.startsWith("-") ? "del"
              : line.startsWith("@@") ? "hunk" : "ctx";
        return <span key={i} className={`diff-line ${kind}`}>{line || " "}</span>;
      })}
    </pre>
  );
}

function RunsCard({ inc }: { inc: IncidentDetail }) {
  const runs = inc.runs.filter((r) => r.agent !== "orchestrator");
  const maxTokens = Math.max(1, ...runs.map((r) => r.input_tokens + r.output_tokens));
  const totalTokens = runs.reduce((n, r) => n + r.input_tokens + r.output_tokens, 0);
  const [openRun, setOpenRun] = useState<string | null>(null);
  return (
    <Card title="Agent 运行" icon="bot" count={`${runs.length} 次`} subtitle="点击某一行查看这次运行的工具调用轨迹。">
      <div className="runs-table-wrap">
        <table className="runs-table">
          <thead>
            <tr><th>Agent</th><th>结论</th><th className="num">耗时</th><th>Token</th><th className="hide-sm">尝试</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const tokens = r.input_tokens + r.output_tokens;
              return (
                <tr
                  key={r.id} title={r.error ?? "查看运行轨迹"} className="run-row"
                  tabIndex={0} role="button"
                  onClick={() => setOpenRun(r.id)}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpenRun(r.id); } }}
                >
                  <td><span className="agent-name"><Icon name="bot" size={12} />{AGENT_LABEL[r.agent] ?? r.agent}</span></td>
                  <td>
                    <Badge tone={r.status === "succeeded" ? "success" : r.status === "failed" ? "danger" : "neutral"} dot>
                      {r.outcome ?? r.status}
                    </Badge>
                  </td>
                  <td className="num mono">{r.latency_ms != null ? duration(r.latency_ms / 1000) : "—"}</td>
                  <td>
                    <div className="token-cell">
                      <span className="token-bar"><span style={{ width: `${(tokens / maxTokens) * 100}%` }} /></span>
                      <span className="mono">{compactNumber(tokens)}</span>
                    </div>
                  </td>
                  <td className="hide-sm muted">{r.attempt_seq ? `#${r.attempt_seq}` : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="runs-foot"><span className="muted small">Agent 运行合计</span><span className="mono small">{compactNumber(totalTokens)} tokens</span></div>
      <RunDrawer runId={openRun} onClose={() => setOpenRun(null)} />
    </Card>
  );
}

/** 运行轨迹：一次 Agent 运行的元信息 + 按时间排序的工具调用。 */
function RunDrawer({ runId, onClose }: { runId: string | null; onClose: () => void }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) { setRun(null); setError(null); return; }
    let alive = true;
    setRun(null);
    api.run(runId).then((r) => { if (alive) setRun(r); }).catch((e: Error) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [runId]);

  return (
    <Modal
      open={runId !== null} onClose={onClose} size="lg"
      title={run ? `${AGENT_LABEL[run.agent] ?? run.agent} · 运行轨迹` : "运行轨迹"}
      description={run ? `${run.status}${run.outcome ? ` · ${run.outcome}` : ""} · ${run.tool_calls.length} 次工具调用` : undefined}
    >
      {error && <ErrorState message={error} />}
      {!run && !error && <LoadingState label="正在加载运行轨迹…" />}
      {run && (
        <div className="run-detail">
          <div className="run-facts">
            <span>模型 <strong className="mono">{run.model ?? "—"}</strong></span>
            <span>输入 <strong className="mono">{compactNumber(run.input_tokens)}</strong></span>
            <span>输出 <strong className="mono">{compactNumber(run.output_tokens)}</strong></span>
            <span>耗时 <strong className="mono">{duration(run.latency_ms != null ? run.latency_ms / 1000 : null)}</strong></span>
            <span>开始于 <strong>{dateTime(run.created_at)}</strong></span>
          </div>
          {run.error && <div className="banner banner-danger"><Icon name="alert-circle" size={14} /><span>{run.error}</span></div>}
          {run.tool_calls.length === 0 ? (
            <EmptyState icon="terminal" title="没有工具调用" description="这次运行没有执行工具，或记录尚未落库。" />
          ) : (
            <ol className="tool-timeline">
              {run.tool_calls.map((c) => (
                <li key={c.id} className={`tool-call ${c.status}`}>
                  <div className="tool-call-head">
                    <span className="tool-call-name mono">{c.tool}</span>
                    <Badge tone={c.status === "ok" || c.status === "succeeded" ? "success" : c.status === "failed" ? "danger" : "neutral"} dot>{c.status}</Badge>
                    <span className="muted small">{c.level}</span>
                    <span className="muted small">{clock(c.created_at)}</span>
                    {c.latency_ms != null && <span className="muted small mono">{duration(c.latency_ms / 1000)}</span>}
                  </div>
                  {Object.keys(c.args ?? {}).length > 0 && (
                    <pre className="tool-call-args">{JSON.stringify(c.args, null, 2)}</pre>
                  )}
                  {c.result_summary && <p className="tool-call-result">{c.result_summary}</p>}
                  {c.error && <p className="tool-call-error">{c.error}</p>}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </Modal>
  );
}

/** 证据超过这个条数时默认收起，避免详情页被长列表撑开。 */
const EVIDENCE_FOLD_THRESHOLD = 6;

function EvidenceCard({ incidentId, version }: { incidentId: string; version: number }) {
  const [items, setItems] = useState<EvidenceItem[] | null>(null);
  const [open, setOpen] = useState<Record<string, string>>({});
  const [kind, setKind] = useState("all");
  // null：跟随条数自动决定；用户手动点过之后按用户选择
  const [expanded, setExpanded] = useState<boolean | null>(null);

  useEffect(() => {
    const t = window.setTimeout(() => {
      api.evidence(incidentId).then(setItems).catch(() => setItems([]));
    }, 400);
    return () => window.clearTimeout(t);
  }, [incidentId, version]);

  if (items === null) {
    return <Card title="证据库" icon="layers"><LoadingState label="正在加载证据…" /></Card>;
  }
  if (items.length === 0) {
    return (
      <Card title="证据库" icon="layers">
        <EmptyState icon="layers" title="还没有沉淀证据" description="Agent 还没有执行工具。" />
      </Card>
    );
  }
  const kinds = Array.from(new Set(items.map((e) => e.kind))).sort();
  const shown = kind === "all" ? items : items.filter((e) => e.kind === kind);
  const collapsible = items.length > EVIDENCE_FOLD_THRESHOLD;
  const isExpanded = expanded ?? !collapsible;
  const latest = items[items.length - 1];

  async function toggle(id: string) {
    if (open[id] !== undefined) {
      setOpen((o) => Object.fromEntries(Object.entries(o).filter(([k]) => k !== id)));
      return;
    }
    setOpen((o) => ({ ...o, [id]: "正在读取全文…" }));
    try {
      const full = await api.evidenceContent(id);
      setOpen((o) => ({ ...o, [id]: full.content }));
    } catch (e) {
      setOpen((o) => ({ ...o, [id]: `加载失败：${(e as Error).message}` }));
    }
  }

  return (
    <Card
      title="证据库" icon="layers" count={items.length}
      actions={
        <div className="evidence-actions">
          {isExpanded && (
            <label className="select-control compact">
              <span className="sr-only">证据类型</span>
              <select name="evidence-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="all">全部类型</option>
                {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
          )}
          {collapsible && (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setExpanded(!isExpanded)} aria-expanded={isExpanded}>
              <Icon name={isExpanded ? "chevron-down" : "chevron-right"} size={14} />
              {isExpanded ? "收起" : "展开"}
            </button>
          )}
        </div>
      }
    >
      {isExpanded ? (
        <ul className="evidence-list">
          {shown.map((e) => (
            <li key={e.id} className="evidence-item">
              <button type="button" className="evidence-toggle" aria-expanded={open[e.id] !== undefined} onClick={() => void toggle(e.id)}>
                <span className="evidence-chevron"><Icon name={open[e.id] !== undefined ? "chevron-down" : "chevron-right"} size={13} /></span>
                <span className="kind-tag">{e.kind}</span>
                <span className="evidence-summary">{e.summary}</span>
                <span className="evidence-meta">
                  {e.agent ? `${AGENT_LABEL[e.agent] ?? e.agent} · ` : ""}{clock(e.created_at)} · {compactNumber(e.tokens)} tok
                </span>
              </button>
              {open[e.id] !== undefined && (
                <pre className="code-block evidence-content">{open[e.id]}</pre>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <button type="button" className="evidence-folded" onClick={() => setExpanded(true)}>
          <span className="evidence-folded-text">
            已收起 · 共 {items.length} 条证据{latest ? `，最近一条 ${clock(latest.created_at)}` : ""}
          </span>
          <span className="evidence-folded-hint">展开查看 <Icon name="chevron-right" size={13} /></span>
        </button>
      )}
    </Card>
  );
}

function ReportsCard({ inc }: { inc: IncidentDetail }) {
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function download(reportId: string, type: string) {
    setBusyId(reportId);
    setError(null);
    try {
      await downloadReport(reportId, `${inc.id}-${type}.md`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div id="reports">
      <Card title="处理报告" icon="file-text" count={inc.reports.length}>
        {error && <div className="banner banner-danger"><Icon name="alert-circle" size={14} /><span>{error}</span></div>}
        <div className="report-list">
          {inc.reports.map((r) => (
            <details key={r.id} className="report-item">
              <summary>
                <span className="report-type"><Icon name="file-text" size={13} />{r.title}</span>
                <Badge tone={r.status === "sent" ? "success" : r.status === "failed" ? "danger" : "neutral"} dot>{r.status}</Badge>
                <span className="muted small">{dateTime(r.created_at)}</span>
                <Icon name="chevron-down" size={14} className="disclosure-chevron" />
              </summary>
              <div className="report-body">
                <div className="report-toolbar">
                  <span>报告内容</span>
                  <div className="report-toolbar-actions">
                    <button
                      type="button" className="btn btn-sm btn-ghost" disabled={busyId === r.id}
                      onClick={(e) => { e.preventDefault(); void download(r.id, r.type); }}
                    >
                      {busyId === r.id ? <Spinner size={12} /> : <Icon name="file-text" size={13} />} 下载 Markdown
                    </button>
                    <CopyButton text={r.content} label="复制" />
                  </div>
                </div>
                <pre className="report-content">{r.content}</pre>
              </div>
            </details>
          ))}
        </div>
      </Card>
    </div>
  );
}

function attemptStatus(status: string): string {
  return ({ running: "进行中", passed: "通过", failed: "未通过", aborted: "已中止" } as Record<string, string>)[status] ?? status;
}

function attemptTone(status: string): "success" | "danger" | "active" | "neutral" {
  return status === "passed" ? "success" : status === "failed" || status === "aborted" ? "danger" : status === "running" ? "active" : "neutral";
}

function triggerLabel(t: string): string {
  return ({
    initial: "首次尝试",
    verify_failed: "验证未通过",
    review_rejected: "评审驳回",
    human_guided: "人工指导",
  } as Record<string, string>)[t] ?? t;
}