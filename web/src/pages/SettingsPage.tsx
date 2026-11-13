import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, type AgentConfigView, type AgentOverrides, type AuthUser, type Project, type ProjectCheck,
  type ProjectDetail, type SettingsOverview, type UserRow,
} from "../api";
import { Icon } from "../components/Icons";
import {
  Badge, ConfirmDialog, EmptyState, ErrorState, LoadingState, Modal, Spinner,
} from "../components/ui";
import { compactNumber, dateTime, duration, pct } from "../format";

const AGENTS = [
  ["diagnoser", "诊断"],
  ["reproducer", "复现"],
  ["fixer", "修复"],
  ["verifier", "验证"],
] as const;

type Field = {
  key: keyof AgentOverrides;
  label: string;
  hint: string;
  kind: "int" | "text" | "effort";
  min?: number;
  max?: number;
  unit?: string;
};

const FIELDS: Field[] = [
  { key: "model", label: "模型", hint: "OpenAI 兼容网关上的模型名；留空沿用默认", kind: "text" },
  { key: "reasoning_effort", label: "推理强度", hint: "推理模型的 reasoning_effort；低 = 更快、更少超时", kind: "effort" },
  { key: "max_tokens", label: "Token 上限", hint: "单个 Agent 一次运行累计输入 + 输出", kind: "int", min: 10_000, max: 20_000_000, unit: "tokens" },
  { key: "max_tool_calls", label: "工具调用上限", hint: "读文件、写测试、跑测试等每次算一次", kind: "int", min: 5, max: 1_000, unit: "次" },
  { key: "max_seconds", label: "时间上限", hint: "单次运行的墙钟时间", kind: "int", min: 60, max: 14_400, unit: "秒" },
  { key: "max_turns", label: "最大轮数", hint: "一次 LLM 调用为一轮", kind: "int", min: 5, max: 500, unit: "轮" },
];

const EFFORTS = [
  ["", "继承 / 默认"], ["low", "low"], ["medium", "medium"], ["high", "high"],
] as const;

interface Draft {
  top: Record<string, string>;
  per: Record<string, Record<string, string>>;
}

type Body = AgentOverrides & { per_agent?: Record<string, AgentOverrides> };

export function SettingsPage({ me }: { me: AuthUser }) {
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">设置</h1>
        </div>
      </header>
      <GlobalDefaults />
      <AgentSettings />
      <ProjectsCard me={me} />
      {me.role === "admin" ? <UsersCard me={me} /> : (
        <div className="card">
          <EmptyState icon="lock" title="账号管理仅管理员可用" description="当前角色不能查看或修改控制台账号。" />
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ 全局默认（只读） */

function GlobalDefaults() {
  const [view, setView] = useState<SettingsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setError(null);
    api.settingsOverview().then(setView).catch((e: Error) => setError(e.message));
  }, [nonce]);

  return (
    <section className="settings-block">
      <div className="section-head">
        <div>
          <h2 className="section-title">全局默认</h2>
        </div>
        <Badge tone="neutral">只读</Badge>
      </div>
      {error ? (
        <ErrorState title="无法读取全局配置" message={error} onRetry={() => setNonce((n) => n + 1)} />
      ) : !view ? (
        <div className="card"><LoadingState label="正在读取全局配置…" /></div>
      ) : (
        <div className="card settings-global">
          <details className="disclosure global-disclosure">
            <summary>
              <Icon name="sliders" size={13} />
              <span>Agent 默认值、运行边界与质量守卫</span>
              <span className="disclosure-meta">来自服务端环境变量</span>
              <Icon name="chevron-down" size={13} className="disclosure-chevron" />
            </summary>
            <div className="disclosure-body">
              <div className="global-grid">
              <GlobalGroup title="Agent 默认值">
                <GlobalRow label="模型" value={<span className="mono">{view.agent_defaults.model}</span>} />
                <GlobalRow label="推理强度" value={view.agent_defaults.reasoning_effort || "模型默认"} />
                <GlobalRow label="Token 上限" value={`${compactNumber(view.agent_defaults.max_tokens)} tokens`} />
                <GlobalRow label="工具调用上限" value={`${view.agent_defaults.max_tool_calls} 次`} />
                <GlobalRow label="时间上限" value={duration(view.agent_defaults.max_seconds)} />
                <GlobalRow label="最大轮数" value={`${view.agent_defaults.max_turns} 轮`} />
              </GlobalGroup>

              <GlobalGroup title="运行边界">
                <GlobalRow label="诊断置信度阈值" value={pct(view.limits.diagnosis_confidence_threshold)} />
                <GlobalRow label="最大修复轮数" value={`${view.limits.default_max_fix_attempts} 轮`} />
                <GlobalRow label="评审有效期" value={duration(view.limits.review_timeout_s)} />
                <GlobalRow label="无进展转人工" value={duration(view.limits.state_timeout_s)} />
                <GlobalRow label="步骤重试" value={`${view.limits.step_max_retries} 次 · 间隔 ${view.limits.step_retry_delay_s}s`} />
                <GlobalRow label="任务锁 TTL" value={`${view.limits.lock_ttl_s}s`} />
              </GlobalGroup>

              <GlobalGroup title="质量守卫">
                <GlobalRow label="先复现后修复" value={<GuardBadge on={view.guards.repro_required} />} />
                <GlobalRow label="系统独立复核复现" value={<GuardBadge on={view.guards.repro_confirm} />} />
                <GlobalRow label="测试完整性扫描" value={<GuardBadge on={view.guards.integrity_check} />} />
                <GlobalRow label="历史判例注入" value={<GuardBadge on={view.guards.precedents_enabled} />} />
                <GlobalRow label="日志聚类摘要" value={<GuardBadge on={view.guards.log_cluster} />} />
              </GlobalGroup>

              <GlobalGroup title="运行环境">
                <GlobalRow label="Agent 模式" value={<span className="mono">{view.runtime.agent_mode}</span>} />
                <GlobalRow label="代码托管" value={<span className="mono">{view.runtime.codehost}</span>} />
                <GlobalRow label="沙箱" value={<span className="mono">{view.runtime.sandbox}</span>} />
                <GlobalRow label="孪生环境" value={<span className="mono">{view.runtime.twin_provider}</span>} />
                <GlobalRow label="CI" value={<span className="mono">{view.runtime.ci}</span>} />
                <GlobalRow label="通知渠道" value={<span className="mono">{view.runtime.notifier}</span>} />
                <GlobalRow label="可观测" value={<span className="mono">{view.runtime.observability}</span>} />
                <GlobalRow label="LLM 网关" value={<span className="mono global-url" title={view.runtime.llm_base_url}>{view.runtime.llm_base_url}</span>} />
              </GlobalGroup>
            </div>
            </div>
          </details>
        </div>
      )}
    </section>
  );
}

function GlobalGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="global-group">
      <h3 className="global-group-title">{title}</h3>
      <dl className="global-rows">{children}</dl>
    </div>
  );
}

function GlobalRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="global-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function GuardBadge({ on }: { on: boolean }) {
  return <Badge tone={on ? "success" : "neutral"} dot>{on ? "开启" : "关闭"}</Badge>;
}

/* ------------------------------------------------------------------ Agent 参数 */

function AgentSettings() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [pendingProject, setPendingProject] = useState<string | null>(null);
  const [view, setView] = useState<AgentConfigView | null>(null);
  const [viewError, setViewError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>({ top: {}, per: {} });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [savedAt, setSavedAt] = useState<Date | null>(null);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);

  const loadProjects = useCallback(() => {
    setProjects(null);
    setProjectsError(null);
    api.projects().then((ps) => {
      setProjects(ps);
      setName((n) => n || ps[0]?.name || "");
    }).catch((e: Error) => setProjectsError(e.message));
  }, []);

  useEffect(() => { loadProjects(); }, [loadProjects]);

  useEffect(() => {
    if (!name) return;
    setView(null);
    setViewError(null);
    setMsg(null);
    setSavedAt(null);
    api.agentConfig(name).then((v) => {
      setView(v);
      setDraft(toDraft(v));
    }).catch((e: Error) => setViewError(e.message));
  }, [name, reloadNonce]);

  const built = useMemo(() => buildOverrides(draft), [draft]);
  const baseline = useMemo(() => (view ? normalizeBody(view.overrides) : null), [view]);
  const dirty = view != null && JSON.stringify(built.body) !== JSON.stringify(baseline);
  const errorCount = Object.keys(built.errors).length;
  const hasOverrides = view != null && JSON.stringify(normalizeBody(view.overrides)) !== "{}";
  const savedPerAgents = view ? Object.keys(view.overrides.per_agent ?? {}).length : 0;

  function setTop(key: string, value: string) {
    setDraft((d) => ({ ...d, top: { ...d.top, [key]: value } }));
    setMsg(null);
  }

  function setPer(agent: string, key: string, value: string) {
    setDraft((d) => ({
      ...d,
      per: { ...d.per, [agent]: { ...(d.per[agent] ?? {}), [key]: value } },
    }));
    setMsg(null);
  }

  function clearAgent(agent: string) {
    setDraft((d) => ({
      ...d,
      per: { ...d.per, [agent]: Object.fromEntries(FIELDS.map((f) => [f.key, ""])) },
    }));
    setMsg(null);
  }

  function requestProject(next: string) {
    if (next === name) return;
    if (dirty) setPendingProject(next);
    else setName(next);
  }

  async function save() {
    if (!view || errorCount > 0) return;
    setBusy(true);
    setMsg(null);
    try {
      const v = await api.putAgentConfig(name, built.body);
      setView(v);
      setDraft(toDraft(v));
      setSavedAt(new Date());
      setMsg({ ok: true, text: "已保存。" });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  if (projects === null) {
    return projectsError
      ? <ErrorState title="无法加载项目" message={projectsError} onRetry={loadProjects} />
      : <div className="card"><LoadingState label="正在加载项目…" /></div>;
  }
  if (projects.length === 0) {
    return (
      <div className="card">
        <EmptyState icon="box" title="还没有项目" description="先创建项目或注入一个基准用例，项目级 Agent 配置才会出现。" />
      </div>
    );
  }

  return (
    <section className="settings-block">
      <div className="section-head">
        <div>
          <h2 className="section-title">Agent 运行参数</h2>
        </div>
        <div className="row">
          <label className="select-control">
            <span className="sr-only">选择项目</span>
            <select id="settings-project" name="project" value={name} onChange={(e) => requestProject(e.target.value)} disabled={busy}>
              {projects.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}
            </select>
          </label>
          <button type="button" className="btn btn-sm" onClick={() => setReloadNonce((n) => n + 1)} disabled={busy}>
            <Icon name="refresh" size={13} /> 重新读取
          </button>
        </div>
      </div>

      {viewError ? (
        <ErrorState title="无法读取项目配置" message={viewError} onRetry={() => setReloadNonce((n) => n + 1)} />
      ) : !view ? (
        <div className="card"><LoadingState label="正在读取 Agent 配置…" /></div>
      ) : (
        <>
          <div className="inheritance">
            <div className="inheritance-step"><span className="inherit-kicker">全局默认</span><strong>服务端环境配置</strong></div>
            <Icon name="chevron-right" size={14} className="inherit-arrow" />
            <div className="inheritance-step"><span className="inherit-kicker">项目覆盖</span><strong className="mono">{name}</strong></div>
            <Icon name="chevron-right" size={14} className="inherit-arrow" />
            <div className="inheritance-step"><span className="inherit-kicker">单 Agent 覆盖</span><strong>仅设置的字段</strong></div>
            <Icon name="chevron-right" size={14} className="inherit-arrow" />
            <div className="inheritance-step final"><span className="inherit-kicker">最终生效</span><strong>下一次运行</strong></div>
          </div>

          <div className="effective-strip">
            {AGENTS.map(([agent, label]) => {
              const e = view.effective[agent];
              if (!e) return null;
              return (
                <div className="effective-agent" key={agent}>
                  <span className="effective-name">{label}</span>
                  <strong className="effective-model" title={e.model}>{e.model}</strong>
                  <span className="effective-meta">{compactNumber(e.max_tokens)} tok · {duration(e.max_seconds)}</span>
                  <span className="effective-meta">{e.max_tool_calls} 次工具 · {e.max_turns} 轮 · {e.reasoning_effort || "默认强度"}</span>
                </div>
              );
            })}
          </div>

          <div className="card settings-card">
            <header className="card-head">
              <div className="card-heading">
                <div className="card-title"><Icon name="sliders" size={15} className="card-title-icon" />项目覆盖</div>
              </div>
            </header>
            <div className="card-body">
              <div className="settings-grid">
                {FIELDS.map((f) => (
                  <FieldControl
                    key={f.key} field={f} id={`top-${f.key}`}
                    value={draft.top[f.key] ?? ""}
                    placeholder={String(view.defaults[f.key] ?? "")}
                    inherited={`全局默认：${displayValue(f, view.defaults[f.key])}`}
                    error={built.errors[`top.${f.key}`]}
                    onChange={(v) => setTop(f.key, v)}
                  />
                ))}
              </div>
            </div>
          </div>

          <div className="card settings-card settings-per-agent">
            <details className="disclosure panel-disclosure">
              <summary>
                <Icon name="users" size={14} />
                <span>按 Agent 覆盖</span>
                <span className="disclosure-meta">
                  {savedPerAgents > 0 ? `已设置 ${savedPerAgents} 个 Agent` : "通常不需要"}
                </span>
                <Icon name="chevron-down" size={13} className="disclosure-chevron" />
              </summary>
              <div className="disclosure-body flush">
                <div className="settings-table-wrap">
                <table className="settings-table">
                  <thead>
                    <tr>
                      <th scope="col">参数</th>
                      {AGENTS.map(([agent, label]) => (
                        <th scope="col" key={agent}>
                          <div className="agent-col-head">
                            <span>{label}</span>
                            {Object.values(draft.per[agent] ?? {}).some(Boolean) && (
                              <button type="button" className="linklike small" onClick={() => clearAgent(agent)}>清除</button>
                            )}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {FIELDS.map((f) => (
                      <tr key={f.key}>
                        <th scope="row">
                          <span className="param-name">{f.label}</span>
                          <span className="param-hint">{f.hint}</span>
                        </th>
                        {AGENTS.map(([agent]) => {
                          const e = view.effective[agent];
                          return (
                            <td key={agent}>
                              {f.kind === "effort" ? (
                                <select
                                  aria-label={`${agent} ${f.label}`}
                                  name={`${agent}.${f.key}`}
                                  value={draft.per[agent]?.[f.key] ?? ""}
                                  onChange={(ev) => setPer(agent, f.key, ev.target.value)}
                                  className={built.errors[`${agent}.${f.key}`] ? "input-invalid" : undefined}
                                >
                                  {EFFORTS.map(([v, label]) => <option key={v || "inherit"} value={v}>{label}</option>)}
                                </select>
                              ) : (
                                <input
                                  aria-label={`${agent} ${f.label}`}
                                  name={`${agent}.${f.key}`}
                                  type={f.kind === "int" ? "number" : "text"}
                                  min={f.min} max={f.max} step={f.kind === "int" ? 1 : undefined}
                                  maxLength={f.kind === "text" ? 200 : undefined}
                                  value={draft.per[agent]?.[f.key] ?? ""}
                                  placeholder={e ? displayValue(f, e[f.key]) : ""}
                                  onChange={(ev) => setPer(agent, f.key, ev.target.value)}
                                  className={built.errors[`${agent}.${f.key}`] ? "input-invalid" : undefined}
                                />
                              )}
                              <span className="cell-foot">
                                {built.errors[`${agent}.${f.key}`]
                                  ? <span className="field-error">{built.errors[`${agent}.${f.key}`]}</span>
                                  : <span>生效：{e ? displayValue(f, e[f.key]) : "—"}</span>}
                              </span>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              </div>
            </details>
          </div>

          {msg && (
            <div className={`banner ${msg.ok ? "banner-success" : "banner-danger"}`} role={msg.ok ? "status" : "alert"}>
              <Icon name={msg.ok ? "check-circle" : "alert-circle"} size={15} /><span>{msg.text}</span>
            </div>
          )}

          <div className={`save-bar ${dirty ? "is-dirty" : ""}`}>
            <div className="save-state">
              {errorCount > 0 ? (
                <><span className="save-dot error" />{errorCount} 处输入不合法，无法保存</>
              ) : dirty ? (
                <><span className="save-dot" />有未保存的修改</>
              ) : savedAt ? (
                <><Icon name="check-circle" size={13} />已保存于 {savedAt.toLocaleTimeString("zh-CN", { hour12: false })}</>
              ) : (
                <><Icon name="check-circle" size={13} />配置已加载，没有未保存修改</>
              )}
            </div>
            <div className="save-actions">
              <button type="button" className="btn btn-sm btn-ghost" disabled={!dirty || busy} onClick={() => { setDraft(toDraft(view)); setMsg(null); }}>
                撤销修改
              </button>
              <button
                type="button" className="btn btn-sm btn-ghost text-danger"
                disabled={!hasOverrides || busy} onClick={() => setRestoreOpen(true)}
              >
                全部恢复默认
              </button>
              <button type="button" className="btn btn-sm btn-primary" disabled={!dirty || busy || errorCount > 0} onClick={() => void save()}>
                {busy ? <Spinner size={13} /> : <Icon name="check" size={13} />}
                {busy ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </>
      )}

      <ConfirmDialog
        open={pendingProject !== null}
        onClose={() => setPendingProject(null)}
        onConfirm={() => { if (pendingProject) { setName(pendingProject); setPendingProject(null); } }}
        title="放弃未保存的修改？" confirmLabel="放弃并切换" tone="danger"
        description="切换项目会重新读取配置，当前草稿中的修改不会保留。"
      />

      <ConfirmDialog
        open={restoreOpen}
        onClose={() => setRestoreOpen(false)}
        onConfirm={() => {
          setDraft({ top: Object.fromEntries(FIELDS.map((f) => [f.key, ""])), per: Object.fromEntries(AGENTS.map(([a]) => [a, Object.fromEntries(FIELDS.map((f) => [f.key, ""]))])) });
          setRestoreOpen(false);
          setMsg(null);
        }}
        title="清空所有项目覆盖？" confirmLabel="清空草稿"
        description="这会清空项目与单 Agent 的所有覆盖值，使配置全部继承全局默认。"
      >
        <div className="confirm-impact">
          <p className="confirm-impact-title"><Icon name="alert-triangle" size={15} /> 影响范围</p>
          <ul>
            <li>清空的是当前编辑草稿，点击“保存”后才会写入项目配置。</li>
            <li>保存后，下一次 Agent 运行将完全使用服务端全局默认值。</li>
            <li>进行中的 Agent 运行不受影响。</li>
          </ul>
        </div>
      </ConfirmDialog>
    </section>
  );
}

function FieldControl({ field, id, value, placeholder, inherited, error, onChange }: {
  field: Field; id: string; value: string; placeholder: string; inherited: string; error?: string; onChange: (v: string) => void;
}) {
  return (
    <label className="field" htmlFor={id}>
      <span>{field.label}{field.unit && <em className="field-tag">{field.unit}</em>}</span>
      {field.kind === "effort" ? (
        <select id={id} value={value} onChange={(e) => onChange(e.target.value)} className={error ? "input-invalid" : undefined}>
          {EFFORTS.map(([v, label]) => <option key={v || "inherit"} value={v}>{label}</option>)}
        </select>
      ) : (
        <input
          id={id} type={field.kind === "int" ? "number" : "text"}
          min={field.min} max={field.max} step={field.kind === "int" ? 1 : undefined}
          maxLength={field.kind === "text" ? 200 : undefined}
          value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)}
          className={error ? "input-invalid" : undefined}
          aria-invalid={Boolean(error)}
        />
      )}
      {error ? <span className="field-error">{error}</span> : <small title={field.hint}>{inherited}</small>}
    </label>
  );
}

/* ------------------------------------------------------------------ 账号 */

const ROLES = [
  ["admin", "管理员", "账号、审批与全部操作"],
  ["maintainer", "维护者", "发起事故、审批、注入基准用例"],
  ["viewer", "只读", "查看事故与报告"],
] as const;

function UsersCard({ me }: { me: AuthUser }) {
  const [rows, setRows] = useState<UserRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [resetTarget, setResetTarget] = useState<UserRow | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<UserRow | null>(null);
  const createForm = useRef<HTMLFormElement>(null);

  const reload = useCallback(() => {
    api.users().then(setRows).catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => { reload(); }, [reload]);

  useEffect(() => {
    if (createOpen) {
      createForm.current?.reset();
      setCreateError(null);
    }
  }, [createOpen]);

  async function act(fn: () => Promise<unknown>, ok: string): Promise<boolean> {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
      reload();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (creating) return;
    const f = new FormData(e.currentTarget);
    const password = String(f.get("password") || "");
    if (password.length < 8) { setCreateError("密码至少 8 位。"); return; }
    if (password.length > 1000) { setCreateError("密码最多 1000 位。"); return; }
    setCreating(true);
    setCreateError(null);
    try {
      await api.createUser({
        name: String(f.get("name") || "").trim(),
        email: String(f.get("email") || "").trim() || null,
        password,
        role: String(f.get("role") || "viewer"),
      });
      setCreateOpen(false);
      setMsg("账号已创建。");
      reload();
    } catch (err) {
      setCreateError((err as Error).message);
    } finally {
      setCreating(false);
    }
  }

  return (
    <section className="settings-block">
      <div className="section-head">
        <div>
          <h2 className="section-title">控制台账号</h2>
        </div>
        <button type="button" className="btn btn-primary btn-sm" onClick={() => setCreateOpen(true)}>
          <Icon name="plus" size={13} /> 新建账号
        </button>
      </div>

      {error && <div className="banner banner-danger" role="alert"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
      {msg && <div className="banner banner-success" role="status"><Icon name="check-circle" size={15} /><span>{msg}</span></div>}

      {rows === null && !error ? <div className="card"><LoadingState label="正在加载账号…" /></div> : (
        <div className="card settings-users">
          <div className="card-body flush">
            <div className="table-scroll">
            <table className="data-table users-table">
              <thead>
                <tr>
                  <th>账号</th><th>角色</th><th className="hide-md">邮箱</th><th className="hide-md">创建时间</th><th className="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                {rows?.map((u) => (
                  <tr key={u.id}>
                    <td>
                      <div className="user-cell">
                        <span className="avatar" aria-hidden>{u.name.slice(0, 1).toUpperCase()}</span>
                        <div>
                          <strong>{u.name}{u.id === me.id && <span className="self-tag">我</span>}</strong>
                          <span className="mono small muted">{u.id}</span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <select
                        aria-label={`${u.name} 的角色`} name={`role-${u.id}`} value={u.role} disabled={busy}
                        onChange={(e) => void act(() => api.patchUser(u.id, { role: e.target.value }), `已将 ${u.name} 的角色改为 ${e.target.value}。`)}
                      >
                        {ROLES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
                      </select>
                    </td>
                    <td className="hide-md small">{u.email || "—"}</td>
                    <td className="hide-md small nowrap">{u.created_at ? dateTime(u.created_at) : "—"}</td>
                    <td className="col-actions">
                      <div className="row">
                        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setResetTarget(u)}>改密码</button>
                        <button
                          type="button" className="btn btn-sm btn-ghost text-danger"
                          disabled={busy || u.id === me.id}
                          title={u.id === me.id ? "不能删除自己的账号" : "删除账号"}
                          onClick={() => setDeleteTarget(u)}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            {rows?.length === 0 && <EmptyState icon="users" title="还没有其他账号" description="新建一个账号，或使用 CLI 创建管理员。" />}
          </div>
        </div>
      )}

      <Modal
        open={createOpen} onClose={() => setCreateOpen(false)} size="md"
        title="新建控制台账号"
        description="账号创建后立即可以登录；密码至少 8 位。"
        footer={<>
          <button type="button" className="btn" onClick={() => setCreateOpen(false)} disabled={creating}>取消</button>
          <button type="submit" form="create-user-form" className="btn btn-primary" disabled={creating}>
            {creating ? <Spinner size={13} /> : <Icon name="plus" size={13} />}{creating ? "创建中…" : "创建账号"}
          </button>
        </>}
      >
        <form id="create-user-form" ref={createForm} className="form-grid" onSubmit={(e) => void create(e)}>
          <label className="field">
            <span>用户名</span>
            <input name="name" required pattern="[A-Za-z0-9._-]{2,64}" placeholder="alice" autoComplete="off" />
            <small>2 – 64 位，只允许字母、数字、点、下划线与连字符。</small>
          </label>
          <label className="field">
            <span>邮箱 <em className="field-tag">可选</em></span>
            <input name="email" type="email" placeholder="alice@example.com" autoComplete="off" />
          </label>
          <label className="field">
            <span>密码</span>
            <input name="password" type="password" required minLength={8} maxLength={1000} autoComplete="new-password" />
            <small>至少 8 位。</small>
          </label>
          <label className="field">
            <span>角色</span>
            <select name="role" defaultValue="viewer">
              {ROLES.map(([v, label, desc]) => <option key={v} value={v} title={desc}>{label} · {desc}</option>)}
            </select>
          </label>
          {createError && <div className="banner banner-danger field-wide" role="alert"><Icon name="alert-circle" size={15} /><span>{createError}</span></div>}
        </form>
      </Modal>

      <ResetPasswordDialog
        target={resetTarget}
        onClose={() => setResetTarget(null)}
        onDone={(name) => {
          setMsg(`已更新 ${name} 的密码。`);
          setResetTarget(null);
        }}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        busy={busy}
        onConfirm={() => {
          if (!deleteTarget) return;
          void act(() => api.deleteUser(deleteTarget.id), `账号 ${deleteTarget.name} 已删除。`).then((ok) => { if (ok) setDeleteTarget(null); });
        }}
        title="删除控制台账号？" confirmLabel="删除账号" tone="danger"
        description="删除后该账号无法登录，且此操作不可撤销。"
      >
        {deleteTarget && (
          <div className="confirm-impact">
            <p className="confirm-impact-title"><Icon name="alert-triangle" size={15} /> 删除 {deleteTarget.name}</p>
            <ul>
              <li>账号与登录能力会被移除，历史审批记录会保留。</li>
              <li>如果该账号出现在审批记录中，后端会拒绝删除，需要改为降级角色。</li>
              <li>不能删除当前登录的自己。</li>
            </ul>
          </div>
        )}
      </ConfirmDialog>
    </section>
  );
}

function ResetPasswordDialog({ target, onClose, onDone }: {
  target: UserRow | null; onClose: () => void; onDone: (name: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (target) {
      setPassword("");
      setTouched(false);
      setError(null);
    }
  }, [target]);

  const invalid = password.length < 8 || password.length > 1000;

  async function submit() {
    if (busy) return;
    if (!target || invalid) { setTouched(true); return; }
    setBusy(true);
    setError(null);
    try {
      await api.patchUser(target.id, { password });
      onDone(target.name);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={target !== null} onClose={onClose} size="sm"
      title={target ? `重置 ${target.name} 的密码` : "重置密码"}
      description="新密码至少 8 位。已签发的令牌仍有效，直到自然过期。"
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
        <button type="button" className="btn btn-primary" disabled={busy || (touched && invalid)} onClick={() => void submit()}>
          {busy ? <Spinner size={13} /> : <Icon name="key" size={13} />}{busy ? "更新中…" : "更新密码"}
        </button>
      </>}
    >
      <label className="field">
        <span>新密码</span>
        <input
          data-autofocus name="new-password" type="password" value={password} maxLength={1000} autoComplete="new-password"
          aria-invalid={touched && invalid}
          onChange={(e) => { setPassword(e.target.value); setTouched(true); }}
          onKeyDown={(e) => { if (e.key === "Enter") void submit(); }}
        />
        {touched && invalid && <span className="field-error">密码需为 8 – 1000 位。</span>}
      </label>
      {error && <div className="banner banner-danger" role="alert"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
    </Modal>
  );
}

/* ------------------------------------------------------------------ 项目接入 */

const CHECK_NAME: Record<string, string> = {
  repo_config: "仓库与分支映射",
  codehost: "代码托管",
  extra_repos: "附加仓库",
  workspace_source: "工作区来源",
  twin_provider: "孪生提供方",
  twin_template: "孪生模板",
  notifiers: "通知渠道",
};

type CheckState = ProjectCheck | "busy" | { error: string };

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? { ...(v as Record<string, unknown>) } : {};
}

function asList(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => String(x)).filter(Boolean) : [];
}

function numOf(cfg: Record<string, unknown>, key: string): number | null {
  const n = Number(cfg[key]);
  return Number.isFinite(n) ? n : null;
}

/** 项目接入：仓库映射、孪生模板、预算与通知路由，配只读自检。 */
function ProjectsCard({ me }: { me: AuthUser }) {
  const [projects, setProjects] = useState<ProjectDetail[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checks, setChecks] = useState<Record<string, CheckState>>({});
  const [editing, setEditing] = useState<ProjectDetail | null>(null);
  const canEdit = me.role === "admin";

  const load = useCallback(() => {
    setError(null);
    api.projects().then(setProjects).catch((e: Error) => setError(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);

  async function runCheck(name: string) {
    setChecks((c) => ({ ...c, [name]: "busy" }));
    try {
      const res = await api.checkProject(name);
      setChecks((c) => ({ ...c, [name]: res }));
    } catch (e) {
      setChecks((c) => ({ ...c, [name]: { error: (e as Error).message } }));
    }
  }

  return (
    <section className="settings-block">
      <div className="section-head">
        <div>
          <h2 className="section-title">项目接入</h2>
          <p className="section-desc">仓库与分支映射、孪生模板、单人预算、审批人数与通知路由。自检只做只读探测，不会建分支或容器。</p>
        </div>
        <button type="button" className="btn btn-sm btn-ghost" onClick={load}>
          <Icon name="refresh" size={13} /> 刷新
        </button>
      </div>

      {error && <div className="banner banner-danger" role="alert"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
      {projects === null && !error && <div className="card"><LoadingState label="正在加载项目…" /></div>}
      {projects?.length === 0 && (
        <div className="card"><EmptyState icon="box" title="还没有项目" description="通过接口或基准用例注入创建第一个项目后，这里可以配置接入参数。" /></div>
      )}

      {projects && projects.length > 0 && (
        <div className="card">
          <div className="card-body project-list">
            {projects.map((p) => {
              const cfg = asRecord(p.config);
              const twin = asRecord(cfg.twin);
              const notify = asRecord(cfg.notify);
              const budget = numOf(cfg, "budget_per_incident_tokens");
              const approvals = numOf(cfg, "required_merge_approvals");
              const recipients = asList(cfg.report_recipients);
              const quiet = asRecord(notify.quiet_hours);
              const check = checks[p.name];
              return (
                <div className="project-row" key={p.id}>
                  <div className="project-row-main">
                    <div className="project-ident">
                      <strong>{p.name}</strong>
                      <span className="mono small muted">{p.repo}</span>
                    </div>
                    <div className="project-tags">
                      {Object.entries(p.branch_map).map(([env, branch]) => (
                        <span className="tag" key={env}>{env} → <span className="mono">{branch}</span></span>
                      ))}
                      {Array.isArray(cfg.repos) && cfg.repos.length > 0 && (
                        <span className="tag">附加仓库 {cfg.repos.length}</span>
                      )}
                      <span className="tag">{budget ? `${compactNumber(budget)} tok/事故` : "预算不限"}</span>
                      <span className="tag">审批 {approvals && approvals > 1 ? `${approvals} 人` : "1 人"}</span>
                      {recipients.length > 0 && <span className="tag">{recipients.length} 个收件人</span>}
                      {typeof quiet.start === "string" && typeof quiet.end === "string" && (
                        <span className="tag">静默 {quiet.start}–{quiet.end}</span>
                      )}
                      {asList(twin.services).length > 0 && (
                        <span className="tag">孪生服务 {asList(twin.services).length}</span>
                      )}
                    </div>
                  </div>
                  <div className="project-row-actions">
                    <button
                      type="button" className="btn btn-sm" disabled={check === "busy"}
                      onClick={() => void runCheck(p.name)}
                    >
                      {check === "busy" ? <Spinner size={13} /> : <Icon name="shield-check" size={13} />}
                      {check === "busy" ? "检测中…" : "自检"}
                    </button>
                    <button
                      type="button" className="btn btn-sm btn-ghost" disabled={!canEdit}
                      title={canEdit ? "编辑接入配置" : "只有管理员可以修改项目接入配置"}
                      onClick={() => setEditing(p)}
                    >
                      <Icon name="sliders" size={13} /> 调整
                    </button>
                  </div>

                  {check && check !== "busy" && (
                    "error" in check ? (
                      <div className="banner banner-danger project-check" role="alert">
                        <Icon name="alert-circle" size={14} />
                        <span>自检失败：{check.error}</span>
                      </div>
                    ) : (
                      <ul className="check-list project-check">
                        {check.checks.map((c) => (
                          <li key={c.name} className={c.ok ? "ok" : "bad"}>
                            <Icon name={c.ok ? "check-circle" : "alert-circle"} size={13} />
                            <span className="check-name">{CHECK_NAME[c.name] ?? c.name}</span>
                            <span className="check-detail mono" title={c.detail}>{c.detail}</span>
                          </li>
                        ))}
                      </ul>
                    )
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <ProjectEditDialog
        project={editing}
        onClose={() => setEditing(null)}
        onSaved={(updated) => {
          setProjects((ps) => ps?.map((p) => (p.name === updated.name ? updated : p)) ?? [updated]);
          setEditing(null);
        }}
      />
    </section>
  );
}

interface ProjectForm {
  repo: string;
  language: string;
  dev: string;
  test: string;
  extraRepos: string;
  serviceRepos: string;
  directory: string;
  composeFile: string;
  services: string;
  commands: string;
  budget: string;
  approvals: string;
  recipients: string;
  quietStart: string;
  quietEnd: string;
}

function toProjectForm(p: ProjectDetail): ProjectForm {
  const cfg = asRecord(p.config);
  const twin = asRecord(cfg.twin);
  const notify = asRecord(cfg.notify);
  const quiet = asRecord(notify.quiet_hours);
  const approvals = numOf(cfg, "required_merge_approvals");
  const extraRepos = Array.isArray(cfg.repos) ? cfg.repos : [];
  const serviceRepos = asRecord(cfg.service_repos);
  return {
    repo: p.repo,
    language: p.language || "java",
    dev: p.branch_map.dev ?? "",
    test: p.branch_map.test ?? "",
    extraRepos: extraRepos.length ? JSON.stringify(extraRepos, null, 2) : "",
    serviceRepos: Object.keys(serviceRepos).length ? JSON.stringify(serviceRepos, null, 2) : "",
    directory: String(twin.directory ?? ""),
    composeFile: String(twin.compose_file ?? ""),
    services: asList(twin.services).join(", "),
    commands: Object.keys(asRecord(cfg.commands)).length ? JSON.stringify(cfg.commands, null, 2) : "",
    budget: numOf(cfg, "budget_per_incident_tokens")?.toString() ?? "",
    approvals: approvals && approvals > 1 ? String(approvals) : "",
    recipients: asList(cfg.report_recipients).join(", "),
    quietStart: String(quiet.start ?? ""),
    quietEnd: String(quiet.end ?? ""),
  };
}

function splitList(raw: string): string[] {
  return raw.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
}

/** 把表单合并回项目配置：只动这里管得到的键，其余（severity_recipients、data_snapshot 等）原样保留。 */
function buildProjectPatch(p: ProjectDetail, form: ProjectForm) {
  const cfg = { ...p.config };

  const budget = form.budget.trim() === "" ? null : Number(form.budget);
  if (budget && budget > 0) cfg.budget_per_incident_tokens = Math.trunc(budget);
  else delete cfg.budget_per_incident_tokens;

  const approvals = form.approvals.trim() === "" ? 1 : Math.trunc(Number(form.approvals));
  if (approvals > 1) cfg.required_merge_approvals = approvals;
  else delete cfg.required_merge_approvals;

  const recipients = splitList(form.recipients);
  if (recipients.length) cfg.report_recipients = recipients;
  else delete cfg.report_recipients;

  if (form.extraRepos.trim()) cfg.repos = JSON.parse(form.extraRepos);
  else delete cfg.repos;
  if (form.serviceRepos.trim()) cfg.service_repos = JSON.parse(form.serviceRepos);
  else delete cfg.service_repos;

  const notify = asRecord(cfg.notify);
  if (form.quietStart && form.quietEnd) notify.quiet_hours = { ...asRecord(notify.quiet_hours), start: form.quietStart, end: form.quietEnd };
  else delete notify.quiet_hours;
  if (Object.keys(notify).length) cfg.notify = notify;
  else delete cfg.notify;

  const twin = asRecord(cfg.twin);
  if (form.directory.trim()) twin.directory = form.directory.trim();
  else delete twin.directory;
  if (form.composeFile.trim()) twin.compose_file = form.composeFile.trim();
  else delete twin.compose_file;
  const services = splitList(form.services);
  if (services.length) twin.services = services;
  else delete twin.services;
  if (Object.keys(twin).length) cfg.twin = twin;
  else delete cfg.twin;

  if (form.commands.trim()) cfg.commands = JSON.parse(form.commands);
  else delete cfg.commands;

  return {
    repo: form.repo.trim(),
    language: form.language,
    branch_map: { ...p.branch_map, dev: form.dev.trim(), test: form.test.trim() },
    config: cfg,
  };
}

const CLOCK_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const REPO_RE = /^[\w.-]+\/[\w.-]+$/;

function parseJson(raw: string): { ok: true; value: unknown } | { ok: false } {
  try {
    return { ok: true, value: JSON.parse(raw) };
  } catch {
    return { ok: false };
  }
}

function validateProject(form: ProjectForm): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!REPO_RE.test(form.repo.trim())) errors.repo = "形如 owner/name。";
  if (!form.dev.trim()) errors.dev = "不能为空。";
  if (!form.test.trim()) errors.test = "不能为空。";
  if (form.extraRepos.trim()) {
    const parsed = parseJson(form.extraRepos);
    if (!parsed.ok) errors.extraRepos = "JSON 格式错误。";
    else if (!Array.isArray(parsed.value)) errors.extraRepos = "需为 JSON 数组。";
    else {
      const items = parsed.value as Array<Record<string, unknown>>;
      if (items.some((item) => !item || typeof item !== "object" || !REPO_RE.test(String(item.repo ?? "")))) {
        errors.extraRepos = "每项至少要有 owner/name 形式的 repo。";
      } else {
        const names = items.map((item) => String(item.name ?? "")).filter(Boolean);
        if (new Set(names).size !== names.length) errors.extraRepos = "仓库名不能重复。";
        if (items.some((item) => item.repo === form.repo.trim())) errors.extraRepos = "附加仓库不能与主仓库相同。";
      }
    }
  }
  if (form.serviceRepos.trim()) {
    const parsed = parseJson(form.serviceRepos);
    if (!parsed.ok) errors.serviceRepos = "JSON 格式错误。";
    else if (typeof parsed.value !== "object" || parsed.value === null || Array.isArray(parsed.value)) {
      errors.serviceRepos = "需为 JSON 对象，如 {\"inventory\":\"inventory\"}。";
    } else if (Object.values(parsed.value as Record<string, unknown>).some((v) => typeof v !== "string")) {
      errors.serviceRepos = "映射值需为仓库名字符串。";
    }
  }
  if (form.commands.trim()) {
    const parsed = parseJson(form.commands);
    if (!parsed.ok) errors.commands = "JSON 格式错误。";
    else if (typeof parsed.value !== "object" || parsed.value === null || Array.isArray(parsed.value)) {
      errors.commands = "需为 JSON 对象，如 {\"test\":\"npx vitest run\"}。";
    }
  }
  if (form.budget.trim() !== "" && (!Number.isFinite(Number(form.budget)) || Number(form.budget) < 0)) {
    errors.budget = "需为不小于 0 的整数，留空表示不限。";
  }
  if (form.approvals.trim() !== "" && (!/^\d+$/.test(form.approvals.trim()) || Number(form.approvals) < 2 || Number(form.approvals) > 5)) {
    errors.approvals = "留空表示 1 人；多人审批填 2 – 5。";
  }
  if (Boolean(form.quietStart) !== Boolean(form.quietEnd)) errors.quiet = "开始与结束需同时填写，或同时留空。";
  else if (form.quietStart && (!CLOCK_RE.test(form.quietStart) || !CLOCK_RE.test(form.quietEnd))) {
    errors.quiet = "时间格式为 HH:MM。";
  }
  if (splitList(form.recipients).some((r) => !r.includes(":"))) errors.recipients = "收件人需为 channel:address 形式。";
  return errors;
}

function ProjectEditDialog({ project, onClose, onSaved }: {
  project: ProjectDetail | null;
  onClose: () => void;
  onSaved: (p: ProjectDetail) => void;
}) {
  const [form, setForm] = useState<ProjectForm | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (project) {
      setForm(toProjectForm(project));
      setError(null);
    }
  }, [project]);

  const errors = form ? validateProject(form) : {};
  const invalid = Object.keys(errors).length > 0;

  function set<K extends keyof ProjectForm>(key: K, value: string) {
    setForm((f) => (f ? { ...f, [key]: value } : f));
  }

  async function save() {
    if (!project || !form || invalid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.patchProject(project.name, buildProjectPatch(project, form));
      onSaved(updated);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={project !== null} onClose={onClose} size="lg"
      title={project ? `调整 ${project.name} 的接入配置` : "调整接入配置"}
      description="只修改这里列出的键，其余项目配置（按级别收件人、快照等）保持原样。"
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
        <button type="button" className="btn btn-primary" disabled={busy || invalid} onClick={() => void save()}>
          {busy ? <Spinner size={13} /> : <Icon name="check" size={13} />}{busy ? "保存中…" : "保存配置"}
        </button>
      </>}
    >
      {form && (
        <div className="form-grid project-form">
          <label className="field field-wide">
            <span>仓库</span>
            <input
              data-autofocus value={form.repo} onChange={(e) => set("repo", e.target.value)}
              className={errors.repo ? "input-invalid" : undefined} placeholder="owner/name" autoComplete="off"
            />
            {errors.repo ? <span className="field-error">{errors.repo}</span> : <small>GitHub 或本地仓库的 owner/name。</small>}
          </label>

          <label className="field">
            <span>项目语言</span>
            <select value={form.language} onChange={(e) => set("language", e.target.value)}>
              <option value="java">Java（Maven / Gradle）</option>
              <option value="python">Python（pytest）</option>
              <option value="go">Go（go build / go test）</option>
              <option value="typescript">TypeScript（node --test）</option>
              <option value="csharp">C# / .NET（dotnet run 测试工程）</option>
            </select>
            <small>决定构建、测试命令与代码索引；改动对之后的事故生效。</small>
          </label>

          <label className="field field-wide">
            <span>自定义构建 / 测试命令 <em className="field-tag">JSON 对象，可选</em></span>
            <textarea
              rows={5}
              value={form.commands}
              onChange={(e) => set("commands", e.target.value)}
              className={errors.commands ? "input-invalid" : undefined}
              placeholder='{"build":"npx tsc --noEmit","test":"npx vitest run --reporter=junit --outputFile=.twinfix/junit.xml","report":".twinfix/junit.xml","report_format":"junit"}'
            />
            {errors.commands ? <span className="field-error">{errors.commands}</span> : (
              <small>覆盖该语言默认命令；支持 test_selector / report_format（junit / trx / jest-json）与超时。镜像需自带工具链。</small>
            )}
          </label>

          <label className="field">
            <span>开发分支</span>
            <input value={form.dev} onChange={(e) => set("dev", e.target.value)} className={errors.dev ? "input-invalid" : undefined} />
            {errors.dev ? <span className="field-error">{errors.dev}</span> : <small>修复分支从这里拉出。</small>}
          </label>
          <label className="field">
            <span>测试分支</span>
            <input value={form.test} onChange={(e) => set("test", e.target.value)} className={errors.test ? "input-invalid" : undefined} />
            {errors.test ? <span className="field-error">{errors.test}</span> : <small>自检与孪生默认读取这条分支。</small>}
          </label>

          <label className="field field-wide">
            <span>附加仓库 <em className="field-tag">JSON 数组，可选</em></span>
            <textarea
              rows={4}
              value={form.extraRepos}
              onChange={(e) => set("extraRepos", e.target.value)}
              className={errors.extraRepos ? "input-invalid" : undefined}
              placeholder='[{"name":"inventory","repo":"acme/inventory","branch_map":{"test":"dev"},"fixable":true}]'
            />
            {errors.extraRepos ? <span className="field-error">{errors.extraRepos}</span> : (
              <small>检出到工作区 _repos/&lt;name&gt;；fixable=true 的仓库允许 Fixer 修改并单独开 PR，默认只读。</small>
            )}
          </label>
          <label className="field field-wide">
            <span>服务 → 仓库 <em className="field-tag">JSON 对象，可选</em></span>
            <textarea
              rows={3}
              value={form.serviceRepos}
              onChange={(e) => set("serviceRepos", e.target.value)}
              className={errors.serviceRepos ? "input-invalid" : undefined}
              placeholder='{"inventory":"inventory"}'
            />
            {errors.serviceRepos ? <span className="field-error">{errors.serviceRepos}</span> : (
              <small>事故按 service 自动加载对应仓库；留空只加载主仓库。</small>
            )}
          </label>

          <label className="field">
            <span>孪生目录 <em className="field-tag">可选</em></span>
            <input value={form.directory} onChange={(e) => set("directory", e.target.value)} placeholder="沿用全局模板" />
          </label>
          <label className="field">
            <span>Compose 文件 <em className="field-tag">可选</em></span>
            <input value={form.composeFile} onChange={(e) => set("composeFile", e.target.value)} placeholder="docker-compose.yml" />
          </label>
          <label className="field field-wide">
            <span>孪生服务 <em className="field-tag">逗号分隔</em></span>
            <input value={form.services} onChange={(e) => set("services", e.target.value)} placeholder="mysql, redis, app" />
            <small>留空使用全局模板的服务集合。</small>
          </label>

          <label className="field">
            <span>单人预算 <em className="field-tag">tokens</em></span>
            <input
              type="number" min={0} value={form.budget} onChange={(e) => set("budget", e.target.value)}
              className={errors.budget ? "input-invalid" : undefined} placeholder="留空 = 不限"
            />
            {errors.budget ? <span className="field-error">{errors.budget}</span> : <small>同一事故累计消耗超过后转人工。</small>}
          </label>
          <label className="field">
            <span>审批人数</span>
            <input
              type="number" min={2} max={5} value={form.approvals} onChange={(e) => set("approvals", e.target.value)}
              className={errors.approvals ? "input-invalid" : undefined} placeholder="留空 = 1 人"
            />
            {errors.approvals ? <span className="field-error">{errors.approvals}</span> : <small>2 人及以上需要不同账号分别投票。</small>}
          </label>

          <label className="field field-wide">
            <span>通知收件人 <em className="field-tag">channel:address</em></span>
            <input
              value={form.recipients} onChange={(e) => set("recipients", e.target.value)}
              className={errors.recipients ? "input-invalid" : undefined}
              placeholder="feishu:https://..., email:oncall@example.com"
            />
            {errors.recipients ? <span className="field-error">{errors.recipients}</span> : <small>按级别配置的收件人不受影响，这里是不分级别的兜底。</small>}
          </label>

          <label className="field">
            <span>静默开始 <em className="field-tag">HH:MM</em></span>
            <input value={form.quietStart} onChange={(e) => set("quietStart", e.target.value)} placeholder="22:00" />
          </label>
          <label className="field">
            <span>静默结束 <em className="field-tag">HH:MM</em></span>
            <input value={form.quietEnd} onChange={(e) => set("quietEnd", e.target.value)} placeholder="08:00" />
            {errors.quiet ? <span className="field-error">{errors.quiet}</span> : <small>静默期间仅 S1 即时发送，其余报告标记为暂缓。</small>}
          </label>

          {error && <div className="banner banner-danger field-wide" role="alert"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
        </div>
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------ 数据转换 */

function toDraft(v: AgentConfigView): Draft {
  const over = v.overrides;
  const top = Object.fromEntries(FIELDS.map((f) => {
    const raw = over[f.key];
    return [f.key, raw == null ? "" : String(raw)];
  }));
  const per = Object.fromEntries(AGENTS.map(([agent]) => {
    const p = over.per_agent?.[agent];
    return [agent, Object.fromEntries(FIELDS.map((f) => {
      const raw = p?.[f.key];
      return [f.key, raw == null ? "" : String(raw)];
    }))];
  }));
  return { top, per };
}

function buildOverrides(draft: Draft): { body: Body; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  const body: Body = {};
  for (const f of FIELDS) {
    const raw = draft.top[f.key] ?? "";
    const parsed = parseField(f, raw);
    if (parsed.error) errors[`top.${f.key}`] = parsed.error;
    else if (parsed.value !== undefined) (body as Record<string, unknown>)[f.key] = parsed.value;
  }
  const per: Record<string, AgentOverrides> = {};
  for (const [agent] of AGENTS) {
    const values: AgentOverrides = {};
    for (const f of FIELDS) {
      const raw = draft.per[agent]?.[f.key] ?? "";
      const parsed = parseField(f, raw);
      if (parsed.error) errors[`${agent}.${f.key}`] = parsed.error;
      else if (parsed.value !== undefined) (values as Record<string, unknown>)[f.key] = parsed.value;
    }
    if (Object.keys(values).length > 0) per[agent] = values;
  }
  if (Object.keys(per).length > 0) body.per_agent = per;
  return { body, errors };
}

function parseField(f: Field, raw: string): { value?: string | number; error?: string } {
  const s = raw.trim();
  if (!s) return {};
  if (f.kind === "int") {
    const n = Number(s);
    if (!Number.isFinite(n) || !Number.isInteger(n)) return { error: "请输入整数。" };
    if (f.min != null && n < f.min) return { error: `不能小于 ${f.min}。` };
    if (f.max != null && n > f.max) return { error: `不能大于 ${f.max}。` };
    return { value: n };
  }
  if (f.kind === "text") {
    if (s.length > 200) return { error: "最多 200 个字符。" };
    return { value: s };
  }
  if (!["low", "medium", "high"].includes(s)) return { error: "只能选 low / medium / high。" };
  return { value: s };
}

function normalizeBody(overrides: AgentConfigView["overrides"]): Body {
  const body: Body = {};
  for (const f of FIELDS) {
    const raw = overrides[f.key];
    if (raw !== undefined && raw !== null && raw !== "") (body as Record<string, unknown>)[f.key] = raw;
  }
  const per: Record<string, AgentOverrides> = {};
  for (const [agent] of AGENTS) {
    const p = overrides.per_agent?.[agent];
    if (!p) continue;
    const values: AgentOverrides = {};
    for (const f of FIELDS) {
      const raw = p[f.key];
      if (raw !== undefined && raw !== null && raw !== "") (values as Record<string, unknown>)[f.key] = raw;
    }
    if (Object.keys(values).length > 0) per[agent] = values;
  }
  if (Object.keys(per).length > 0) body.per_agent = per;
  return body;
}

function displayValue(f: Field, v: unknown): string {
  if (v === undefined || v === null || v === "") return "—";
  if (f.kind === "effort") return String(v) || "默认";
  if (f.kind === "int") {
    if (f.key === "max_seconds") return `${v} 秒（${duration(Number(v))}）`;
    if (f.key === "max_tokens") return `${compactNumber(Number(v))} tokens`;
    return `${String(v)}${f.unit ? ` ${f.unit}` : ""}`;
  }
  return String(v);
}