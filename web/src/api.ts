// 后端 API 的类型与调用。类型与 server/twinfix/api/schemas.py 对应。

export type IncidentState =
  | "NEW" | "TRIAGED" | "DIAGNOSING" | "REPRODUCING" | "FIXING" | "VERIFYING"
  | "AWAITING_REVIEW" | "MERGING" | "MERGED" | "OBSERVING" | "NEEDS_HUMAN"
  | "NOT_BUG" | "CANNOT_REPRODUCE" | "CANNOT_FIX" | "ALREADY_FIXED" | "CANCELLED" | "CLOSED";

export const TERMINAL: ReadonlySet<IncidentState> = new Set([
  "NOT_BUG", "CANNOT_REPRODUCE", "CANNOT_FIX", "ALREADY_FIXED", "CANCELLED", "CLOSED",
]);

/** 与后端 TERMINAL_STATES 一致；服务端筛选“进行中”时传这组状态。 */
export const TERMINAL_STATES: IncidentState[] = [
  "NOT_BUG", "CANNOT_REPRODUCE", "CANNOT_FIX", "ALREADY_FIXED", "CANCELLED", "CLOSED",
];

export const NON_TERMINAL_STATES: IncidentState[] = (
  [
    "NEW", "TRIAGED", "DIAGNOSING", "REPRODUCING", "FIXING", "VERIFYING",
    "AWAITING_REVIEW", "MERGING", "MERGED", "OBSERVING", "NEEDS_HUMAN",
  ] as IncidentState[]
);

/** GET /incidents/stats：整个工作区的计数，与列表筛选无关。 */
export interface IncidentStats {
  total: number;
  ongoing: number;
  waiting: number;
  high: number;
  ended: number;
  services: string[];
}

export interface IncidentSummary {
  id: string;
  title: string;
  state: IncidentState;
  severity: "S1" | "S2" | "S3" | "S4";
  env: string;
  service: string | null;
  signal_count: number;
  target_branch: string | null;
  fix_branch: string | null;
  pr_number: number | null;
  assignee_id: string | null;
  cost_tokens: number;
  created_at: string;
  updated_at: string;
  state_entered_at: string;
}

export interface Check { name: string; status: "passed" | "failed" | "skipped"; detail: string }

export interface Attempt {
  id: string;
  seq: number;
  status: string;
  trigger: string;
  feedback: string | null;
  created_at: string;
  patch: {
    diff: string;
    files_changed: string[];
    repos?: Record<string, RepoPatch>;
    touches_tests: boolean;
    explanation: string;
    commit_sha: string | null;
  } | null;
  verifications: { checks: Check[]; passed: boolean; commit_sha: string | null; created_at: string }[];
}

export interface RepoPatch {
  diff: string;
  files_changed: string[];
  branch?: string;
  commit_sha?: string;
  pr_number?: number;
  pr_url?: string;
}

export interface Approval {
  id: string;
  action: "merge" | "human_takeover" | "tool";
  status: "pending" | "approved" | "rejected" | "expired";
  comment: string | null;
  payload: Record<string, unknown> | null;
  decided_by: string | null;
  decided_at: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface IncidentDetail extends IncidentSummary {
  project_id: string;
  fingerprint: string;
  base_commit: string | null;
  github_issue: number | null;
  recurrence_of: string | null;
  closed_reason: string | null;
  cancel_requested: boolean;
  meta: Record<string, unknown>;
  diagnosis: {
    summary: string;
    root_cause: string;
    confidence: number;
    hypotheses: { id: string; statement: string; confidence: number; status: string }[];
    suspect_locations: { file: string; line: number | null }[];
    repro: { test_selector: string; command: string; failure_output: string } | null;
  } | null;
  attempts: Attempt[];
  twin: { compose_project: string; status: string; urls: Record<string, string>; expires_at: string | null } | null;
  assignee: { id: string; name: string } | null;
  approvals: Approval[];
  reports: { id: string; type: string; title: string; content: string; status: string; created_at: string }[];
  runs: {
    id: string; agent: string; status: string; outcome: string | null; latency_ms: number | null; attempt_seq: number | null;
    input_tokens: number; output_tokens: number; error: string | null;
  }[];
}

export interface EvidenceItem { id: string; kind: string; summary: string; tokens: number; agent: string | null; created_at: string }

export interface IncidentEvent {
  id: number;
  incident_id: string;
  kind: string;
  from_state: IncidentState | null;
  to_state: IncidentState | null;
  reason: string | null;
  actor: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Project { id: string; name: string; repo: string }

/** GET /projects：含仓库映射与项目级配置（孪生模板、通知路由、审批人数等）。 */
export interface ProjectDetail extends Project {
  language: string;
  branch_map: Record<string, string>;
  config: Record<string, unknown>;
  created_at: string;
}

/** 项目接入自检的单项结果（POST /projects/{name}/check）。 */
export interface ProjectCheckItem { name: string; ok: boolean; detail: string }
export interface ProjectCheck { project: string; ok: boolean; checks: ProjectCheckItem[] }

/** GET /users/assignable：可指派账号（不含邮箱）。 */
export interface AssignableUser { id: string; name: string; role: AuthUser["role"] }

/** GET /incidents/{id}/similar：历史判例，用于评审与人工处理时参考。 */
export interface SimilarIncident {
  incident_id: string;
  similarity: number;
  title: string;
  verdict: string;
  closed_reason: string;
  root_cause: string;
  suspect_locations: { file: string; line?: number | null }[];
  fix: { files_changed: string[]; explanation: string } | null;
}

/** GET /runs/{id}：一次 Agent 运行的工具调用轨迹。 */
export interface ToolCallRow {
  id: string;
  tool: string;
  level: string;
  args: Record<string, unknown>;
  status: string;
  result_summary: string | null;
  result_ref: string | null;
  error: string | null;
  latency_ms: number | null;
  created_at: string;
}
export interface RunDetail {
  id: string;
  incident_id: string;
  agent: string;
  status: string;
  outcome: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cost: string;
  latency_ms: number | null;
  error: string | null;
  created_at: string;
  tool_calls: ToolCallRow[];
}

/** GET /stats/runtime：队列与容量。 */
export interface RuntimeStats {
  now: string;
  incidents: Partial<Record<IncidentState, number>>;
  active_runs: number;
  oldest_active_run_at: string | null;
  pending_approvals: number;
  twins: Record<string, number>;
  twin_capacity: number;
  twin_in_use: number;
  stuck_incidents: number;
  limits: { state_timeout_s: number; review_timeout_s: number; step_max_retries: number; twin_max_concurrent: number };
}

/** GET /stats/cost：成本看板。 */
export interface CostStats {
  days: number;
  since: string;
  totals: { runs: number; input_tokens: number; output_tokens: number; tokens: number; cost: number; avg_latency_ms: number };
  by_agent: { agent: string; runs: number; input_tokens: number; output_tokens: number; tokens: number; cost: number }[];
  daily: { date: string; runs: number; tokens: number; cost: number }[];
  top_incidents: { id: string; title: string; state: IncidentState; cost_tokens: number; cost_amount: number }[];
}

export interface AgentOverrides {
  model?: string;
  reasoning_effort?: string;
  max_tokens?: number;
  max_tool_calls?: number;
  max_seconds?: number;
  max_turns?: number;
}

export interface AgentConfigView {
  overrides: AgentOverrides & { per_agent?: Record<string, AgentOverrides> };
  defaults: Required<AgentOverrides>;
  effective: Record<string, Required<AgentOverrides>>;
}

/** GET /settings/overview：服务端全局配置（只读，来自环境变量）。 */
export interface SettingsOverview {
  agent_defaults: Required<AgentOverrides>;
  limits: {
    diagnosis_confidence_threshold: number;
    default_max_fix_attempts: number;
    review_timeout_s: number;
    state_timeout_s: number;
    step_max_retries: number;
    step_retry_delay_s: number;
    lock_ttl_s: number;
  };
  guards: {
    repro_required: boolean;
    repro_confirm: boolean;
    integrity_check: boolean;
    precedents_enabled: boolean;
    log_cluster: boolean;
  };
  runtime: {
    agent_mode: string;
    codehost: string;
    sandbox: string;
    twin_provider: string;
    ci: string;
    notifier: string;
    observability: string;
    llm_base_url: string;
  };
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const TOKEN_KEY = "twinfix.token";

export interface AuthUser {
  id: string;
  name: string;
  role: "admin" | "maintainer" | "viewer";
}

function token(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(value: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, value);
  } catch {
    /* 隐私模式下 localStorage 不可用：本次会话仍可继续，刷新后需重新登录 */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/** 基准测试评测（twinfix-arena eval 输出），对应 server/twinfix/arena/grading.py report_json。 */
export interface EvalSummary {
  total: number;
  outcome_accuracy: number;
  top1: number;
  top3: number;
  repro_rate: number;
  fix_rate: number;
  false_fix_rate: number;
  needs_human_rate: number;
  avg_duration_s: number | null;
  avg_tokens: number;
  avg_cost: number;
}

export interface EvalCase {
  case_id: string;
  category: string;
  expected: string;
  final_state: string;
  outcome_correct: boolean;
  root_cause_top1: boolean;
  root_cause_top3: boolean;
  root_cause_graded?: boolean; // 旧报告没有该字段，视为已判
  reproduced: boolean;
  verified?: boolean;
  hidden_passed: boolean | null;
  false_fix: boolean;
  needs_human?: boolean;
  fix_success: boolean;
  duration_s: number | null;
  tokens: number;
  cost?: number;
  incident_id: string;
  error: string | null;
  notes: string[];
}

export interface EvalReportMeta { name: string; title: string; meta: Record<string, string>; summary: EvalSummary }
export interface EvalReport { title: string; meta: Record<string, string>; summary: EvalSummary; cases: EvalCase[] }

/** 控制台发起的评测任务（POST /eval/runs）。 */
export interface EvalRun {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "interrupted";
  label: string;
  model: string;
  effort: string;
  cases: string[];
  timeout_s: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  returncode: number | null;
  error: string | null;
  report: string | null;
  log_file: string;
}

export interface EvalRunDetail { run: EvalRun; report: EvalReport | null }

/** 管理员账号列表行（GET /users）。 */
export interface UserRow extends AuthUser { email: string | null; created_at: string | null }

/** 全局审批列表行（GET /approvals），附事故上下文。 */
export interface ApprovalRow {
  id: string;
  action: "merge" | "human_takeover" | "tool";
  status: "pending" | "approved" | "rejected" | "expired";
  payload: Record<string, unknown>;
  comment: string | null;
  decided_by: string | null;
  decided_at: string | null;
  expires_at: string | null;
  created_at: string;
  incident: {
    id: string; title: string; state: IncidentState;
    severity: "S1" | "S2" | "S3" | "S4"; service: string | null; target_branch: string | null;
  };
}

/** Arena 用例元数据（GET /arena/cases，只读 case.yaml）。 */
export interface ArenaCase {
  id: string;
  title: string;
  category: string;
  description: string;
  inject: { type: "patch" | "none"; file: string; branch: string; services: string[] };
  expect: { outcome: string; root_cause_locations: string[]; root_cause_keywords: string[] };
  severity: string | null;
  hidden_tests: number;
  on_review: boolean;
  error?: string; // case.yaml 写坏时只在该行报错，不影响整页
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`/api${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* 非 JSON 错误体 */
    }
    // token 过期/被撤销：清掉本地 token，App 监听事件切回登录页（登录接口自己的 401 不算）
    if (res.status === 401 && path !== "/auth/login") {
      clearToken();
      window.dispatchEvent(new Event("twinfix:unauthorized"));
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T; // DELETE 等无响应体
  return (await res.json()) as T;
}

export const api = {
  login: (name: string, password: string) =>
    request<{ token: string; user: AuthUser }>("POST", "/auth/login", { name, password }),
  me: () => request<AuthUser>("GET", "/auth/me"),
  health: () => request<{ status: string; halted: boolean; agent_mode: string }>("GET", "/health"),
  setHalt: (halted: boolean) => request("POST", "/system/halt", { halted }),
  projects: () => request<ProjectDetail[]>("GET", "/projects"),
  createProject: (name: string, repo: string) => request<Project>("POST", "/projects", { name, repo }),
  patchProject: (name: string, body: {
    repo?: string; language?: string; branch_map?: Record<string, string>; config?: Record<string, unknown>;
  }) => request<ProjectDetail>("PATCH", `/projects/${encodeURIComponent(name)}`, body),
  checkProject: (name: string) => request<ProjectCheck>("POST", `/projects/${encodeURIComponent(name)}/check`),
  assignableUsers: () => request<AssignableUser[]>("GET", "/users/assignable"),
  setAssignee: (incidentId: string, userId: string | null) =>
    request<{ assignee_id: string | null; assignee: { id: string; name: string } | null }>(
      "POST", `/incidents/${incidentId}/assignee`, { user_id: userId },
    ),
  similar: (incidentId: string, limit = 3) =>
    request<{ items: SimilarIncident[] }>("GET", `/incidents/${incidentId}/similar?limit=${limit}`),
  run: (runId: string) => request<RunDetail>("GET", `/runs/${encodeURIComponent(runId)}`),
  runtimeStats: () => request<RuntimeStats>("GET", "/stats/runtime"),
  costStats: (days = 7) => request<CostStats>("GET", `/stats/cost?days=${days}`),
  incidents: (states?: IncidentState[]) => {
    const q = new URLSearchParams({ limit: "100" });
    states?.forEach((s) => q.append("state", s));
    return request<{ items: IncidentSummary[]; total: number }>("GET", `/incidents?${q}`);
  },
  /** 服务端筛选 + 分页：只取当前页，统计值另用 incidentStats()。 */
  incidentsPage: (query: {
    states?: IncidentState[];
    severity?: string[];
    services?: string[];
    q?: string;
    assigneeId?: string;
    unassigned?: boolean;
    limit?: number;
    offset?: number;
  } = {}) => {
    const p = new URLSearchParams({
      limit: String(query.limit ?? 20),
      offset: String(query.offset ?? 0),
    });
    query.states?.forEach((s) => p.append("state", s));
    query.severity?.forEach((s) => p.append("severity", s));
    query.services?.forEach((s) => p.append("service", s));
    if (query.q?.trim()) p.set("q", query.q.trim());
    if (query.assigneeId) p.set("assignee_id", query.assigneeId);
    if (query.unassigned) p.set("unassigned", "true");
    return request<{ items: IncidentSummary[]; total: number }>("GET", `/incidents?${p}`);
  },
  incidentStats: () => request<IncidentStats>("GET", "/incidents/stats"),
  incident: (id: string) => request<IncidentDetail>("GET", `/incidents/${id}`),
  events: (id: string) => request<IncidentEvent[]>("GET", `/incidents/${id}/events?limit=2000`),
  createIncident: (body: {
    project: string; title: string; service?: string; env?: string; stacktrace?: string; scenario?: string;
  }) => request<{ incident_id: string; created: boolean }>("POST", "/incidents", { source: "manual", ...body }),
  cancel: (id: string, reason?: string) => request("POST", `/incidents/${id}/cancel`, { reason }),
  guide: (id: string, guidance: string, restart_from?: string) =>
    request("POST", `/incidents/${id}/guide`, { guidance, restart_from }),
  resolve: (id: string, verdict: string, reason?: string) => request("POST", `/incidents/${id}/resolve`, { verdict, reason }),
  decide: (approvalId: string, approve: boolean, comment?: string, reasonKind?: "root_cause" | "patch" | "assertion") =>
    request("POST", `/approvals/${approvalId}/decide`, { approve, comment, reason_kind: reasonKind }),
  approvals: (status?: string[], action?: string) => {
    const q = new URLSearchParams({ limit: "100" });
    status?.forEach((s) => q.append("status", s));
    if (action) q.set("action", action);
    return request<{ items: ApprovalRow[]; total: number }>("GET", `/approvals?${q}`);
  },
  allApprovals: async (): Promise<{ items: ApprovalRow[]; total: number }> => {
    const first = await request<{ items: ApprovalRow[]; total: number }>("GET", "/approvals?limit=200&offset=0");
    const items = [...first.items];
    for (let offset = 200; offset < first.total; offset += 200) {
      const page = await request<{ items: ApprovalRow[]; total: number }>("GET", `/approvals?limit=200&offset=${offset}`);
      items.push(...page.items);
    }
    return { items, total: first.total };
  },
  arenaCases: () => request<ArenaCase[]>("GET", "/arena/cases"),
  arenaInject: (caseId: string, trigger = true) =>
    request<{
      case_id: string; project: string; repo: string; branch: string; sha: string;
      incident_id: string | null; created: boolean | null;
    }>("POST", "/arena/inject", { case_id: caseId, trigger }),
  settingsOverview: () => request<SettingsOverview>("GET", "/settings/overview"),
  agentConfig: (project: string) => request<AgentConfigView>("GET", `/projects/${encodeURIComponent(project)}/agent-config`),
  putAgentConfig: (project: string, body: AgentConfigView["overrides"]) =>
    request<AgentConfigView>("PUT", `/projects/${encodeURIComponent(project)}/agent-config`, body),
  evidence: (incidentId: string) => request<EvidenceItem[]>("GET", `/incidents/${incidentId}/evidence`),
  evidenceContent: (id: string) => request<{ id: string; kind: string; summary: string; content: string }>("GET", `/evidence/${id}`),
  evalReports: () => request<EvalReportMeta[]>("GET", "/eval/reports"),
  evalReport: (name: string) => request<EvalReport>("GET", `/eval/reports/${encodeURIComponent(name)}`),
  evalRuns: () => request<EvalRun[]>("GET", "/eval/runs"),
  evalRun: (id: string) => request<EvalRunDetail>("GET", `/eval/runs/${encodeURIComponent(id)}`),
  startEvalRun: (body: { cases?: string[]; label?: string; model?: string; effort?: string; timeout_s?: number }) =>
    request<EvalRun>("POST", "/eval/runs", body),
  users: () => request<UserRow[]>("GET", "/users"),
  createUser: (body: { name: string; email?: string | null; password: string; role: string }) =>
    request<UserRow>("POST", "/users", body),
  patchUser: (id: string, body: { email?: string; password?: string; role?: string }) =>
    request<UserRow>("PATCH", `/users/${encodeURIComponent(id)}`, body),
  deleteUser: (id: string) => request<void>("DELETE", `/users/${encodeURIComponent(id)}`),
};

/** 报告下载：先带 JWT 取回内容，再交给浏览器保存（直接打开链接无法携带 Authorization）。 */
export async function downloadReport(reportId: string, filename: string): Promise<void> {
  const headers: Record<string, string> = {};
  const t = token();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`/api/reports/${encodeURIComponent(reportId)}/download`, { headers });
  if (!res.ok) {
    if (res.status === 401) {
      clearToken();
      window.dispatchEvent(new Event("twinfix:unauthorized"));
    }
    throw new ApiError(res.status, res.statusText || "下载失败");
  }
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

/** SSE 连接状态。EventSource 断开后会自动重连，页面如实展示这三个阶段。 */
export type LiveState = "connecting" | "live" | "offline";

export function subscribe(
  path: string,
  onEvent: (ev: IncidentEvent) => void,
  onStatus?: (state: LiveState) => void,
): () => void {
  const t = token();
  const url = `/api${path}${t ? `?token=${encodeURIComponent(t)}` : ""}`;
  const es = new EventSource(url);
  onStatus?.("connecting");
  es.addEventListener("incident", (e) => onEvent(JSON.parse((e as MessageEvent<string>).data) as IncidentEvent));
  es.onopen = () => onStatus?.("live");
  es.onerror = () => onStatus?.("offline");
  return () => es.close();
}
