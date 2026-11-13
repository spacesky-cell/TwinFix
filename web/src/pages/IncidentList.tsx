import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, NON_TERMINAL_STATES, TERMINAL_STATES, subscribe,
  type AuthUser, type IncidentState, type IncidentStats, type IncidentSummary, type LiveState, type Project,
} from "../api";
import { Icon } from "../components/Icons";
import { SeverityBadge, StateBadge } from "../components/StateBadge";
import { EmptyState, ErrorState, KpiCard, LiveStatus, Modal, SkeletonRows, Spinner } from "../components/ui";
import { Ring, StackedBar } from "../components/viz";
import { compactNumber, dateTime, relTime, SCENARIOS, WAITING_STATES } from "../format";
import { navigate } from "../router";

type StatusFilter = "active" | "waiting" | "ended" | "all";
const PAGE_SIZE = 20;

const STATUS_TABS: { value: StatusFilter; label: string }[] = [
  { value: "active", label: "进行中" },
  { value: "waiting", label: "待人工" },
  { value: "ended", label: "已结束" },
  { value: "all", label: "全部" },
];

export function IncidentList({ user }: { user: AuthUser }) {
  const [items, setItems] = useState<IncidentSummary[] | null>(null);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<IncidentStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState<LiveState>("connecting");
  const [status, setStatus] = useState<StatusFilter>("active");
  const [severity, setSeverity] = useState("all");
  const [service, setService] = useState("all");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [mine, setMine] = useState(false);
  const [page, setPage] = useState(1);
  const [refreshing, setRefreshing] = useState(false);
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const [now, setNow] = useState(Date.now());
  const [createOpen, setCreateOpen] = useState(false);
  const canWrite = user.role !== "viewer";

  // 搜索输入防抖后再请求：筛选与分页都由服务端完成，控制台只取当前页
  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(query.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [query]);

  const states = useMemo<IncidentState[] | undefined>(() => {
    if (status === "active") return NON_TERMINAL_STATES;
    if (status === "waiting") return [...WAITING_STATES];
    if (status === "ended") return TERMINAL_STATES;
    return undefined;
  }, [status]);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const data = await api.incidentsPage({
        states,
        severity: severity === "all" ? undefined : [severity],
        services: service === "all" ? undefined : [service],
        q: search || undefined,
        assigneeId: mine ? user.id : undefined,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      });
      setItems(data.items);
      setTotal(data.total);
      setError(null);
      setLoadedAt(new Date());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRefreshing(false);
    }
  }, [states, severity, service, search, mine, user.id, page]);

  /** 概览统计来自独立接口：列表筛选时统计口径不变。 */
  const loadStats = useCallback(async () => {
    try {
      setStats(await api.incidentStats());
    } catch {
      /* 概览失败不阻塞列表；统计条显示占位 */
    }
  }, []);

  useEffect(() => { void load(); void loadStats(); }, [load, loadStats]);

  const refreshRef = useRef<() => void>(() => {});
  refreshRef.current = () => { void load(true); void loadStats(); };

  useEffect(() => {
    let timer: number | undefined;
    const off = subscribe("/stream", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => refreshRef.current(), 400);
    }, setLive);
    return () => { off(); window.clearTimeout(timer); };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const services = stats?.services ?? [];
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const safePage = Math.min(page, pages);
  const shown = items ?? [];
  const filtersActive = status !== "active" || severity !== "all" || service !== "all" || query.trim() !== "" || mine;

  useEffect(() => { if (page > pages) setPage(pages); }, [page, pages]);

  function clearFilters() {
    setStatus("active");
    setSeverity("all");
    setService("all");
    setQuery("");
    setSearch("");
    setMine(false);
    setPage(1);
  }

  function changeStatus(next: StatusFilter) {
    setStatus(next);
    setPage(1);
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">事故</h1>
        </div>
        <div className="page-header-side">
          <LiveStatus state={live} />
          <button type="button" className="btn btn-sm" onClick={() => { void load(); void loadStats(); }} disabled={refreshing}>
            {refreshing ? <Spinner size={13} /> : <Icon name="refresh" size={13} />}
            刷新
          </button>
          <span className="updated-at" title={loadedAt ? loadedAt.toLocaleString("zh-CN") : undefined}>
            {loadedAt ? `更新于 ${loadedAt.toLocaleTimeString("zh-CN", { hour12: false })}` : "尚未加载"}
          </span>
        </div>
      </header>

      <div className="kpi-strip" aria-label="事故概览">
        <KpiCard
          icon="activity" label="进行中" tone="active" value={stats ? stats.ongoing : "—"}
          foot={stats && stats.total > 0 ? `占全部 ${Math.round((stats.ongoing / stats.total) * 100)}%` : "未结束的事故"}
          visual={<Ring value={stats && stats.total > 0 ? stats.ongoing / stats.total : 0} tone="active" label="进行中占比" />}
        />
        <KpiCard
          icon="user-round" label="等待人工" tone="attention" value={stats ? stats.waiting : "—"}
          foot={stats && stats.waiting > 0 ? "需要你处理" : "暂无待办"}
          visual={<Ring value={stats && stats.ongoing > 0 ? stats.waiting / stats.ongoing : 0} tone="attention" label="等待人工占进行中" />}
        />
        <KpiCard
          icon="alert-triangle" label="高优先级" tone="danger" value={stats ? stats.high : "—"}
          foot="进行中的 S1 / S2"
          visual={<Ring value={stats && stats.ongoing > 0 ? stats.high / stats.ongoing : 0} tone="danger" label="高优先级占进行中" />}
        />
        <KpiCard
          icon="layers" label="累计事故" value={stats ? stats.total : "—"}
          foot={stats ? `已结束 ${stats.ended}` : "全部记录"}
          visual={stats && stats.total > 0 ? (
            <StackedBar
              label="事故构成" segments={[
                { value: Math.max(0, stats.waiting), tone: "attention", label: "等待人工" },
                { value: Math.max(0, stats.ongoing - stats.waiting), tone: "active", label: "处理中" },
                { value: Math.max(0, stats.ended), tone: "neutral", label: "已结束" },
              ]}
            />
          ) : undefined}
        />
      </div>

      <div className="list-toolbar">
        <div className="filter-tabs" role="tablist" aria-label="按处理状态筛选">
          {STATUS_TABS.map((t) => (
            <button key={t.value} type="button" role="tab" aria-selected={status === t.value} onClick={() => changeStatus(t.value)}>
              {t.label}
              <span className="tab-count">
                {!stats ? "—" : t.value === "active" ? stats.ongoing : t.value === "waiting" ? stats.waiting : t.value === "ended" ? stats.ended : stats.total}
              </span>
            </button>
          ))}
        </div>
        <div className="toolbar-divider hide-md" />
        <label className="select-control">
          <span className="sr-only">严重级别</span>
          <select
            id="filter-severity" name="severity" value={severity}
            onChange={(e) => { setSeverity(e.target.value); setPage(1); }}
          >
            <option value="all">全部级别</option>
            <option value="S1">S1 · 最高</option>
            <option value="S2">S2 · 高</option>
            <option value="S3">S3 · 中</option>
            <option value="S4">S4 · 低</option>
          </select>
        </label>
        <label className="select-control hide-sm">
          <span className="sr-only">服务</span>
          <select
            id="filter-service" name="service" value={service}
            onChange={(e) => { setService(e.target.value); setPage(1); }}
          >
            <option value="all">全部服务</option>
            {services.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <div className="search-box">
          <Icon name="search" size={14} />
          <input
            id="incident-search" name="q"
            value={query} onChange={(e) => { setQuery(e.target.value); setPage(1); }}
            placeholder="搜索标题、ID、服务或分支" aria-label="搜索事故" type="search"
          />
          {query && (
            <button
              type="button" className="search-clear"
              onClick={() => { setQuery(""); setSearch(""); setPage(1); }} aria-label="清空搜索"
            >
              <Icon name="x" size={12} />
            </button>
          )}
        </div>
        <div className="toolbar-spacer" />
        <button
          type="button" className={`btn btn-sm ${mine ? "btn-primary" : "btn-ghost"}`}
          aria-pressed={mine} onClick={() => { setMine((v) => !v); setPage(1); }}
        >
          <Icon name="user-round" size={13} /> 只看我的
        </button>
        {filtersActive && (
          <button type="button" className="btn btn-sm btn-ghost" onClick={clearFilters}>
            <Icon name="x" size={13} /> 清除筛选
          </button>
        )}
        {canWrite && (
          <button type="button" className="btn btn-primary" onClick={() => setCreateOpen(true)}>
            <Icon name="plus" size={14} /> 新建事故
          </button>
        )}
      </div>

      {error && items !== null && (
        <div className="banner banner-danger" role="alert">
          <Icon name="alert-circle" size={15} /><span>刷新失败：{error}</span>
          <button type="button" className="btn btn-sm" onClick={() => void load()}>重试</button>
        </div>
      )}

      <div className="table-shell">
        <div className="table-shell-head">
          <div>
            <strong>{STATUS_TABS.find((t) => t.value === status)?.label}</strong>
            <span className="muted"> · {total} 条{search ? `匹配“${search}”` : ""}</span>
          </div>
          <span className="muted small">按最近更新排序</span>
        </div>
        <div className="table-scroll">
          <table className="data-table incident-table">
            <thead>
              <tr>
                <th className="col-incident">事故</th>
                <th className="col-state">状态</th>
                <th className="col-service hide-md">服务</th>
                <th className="col-branch hide-lg">修复分支</th>
                <th className="col-num hide-lg">Token</th>
                <th className="col-time hide-xs">更新</th>
                <th className="col-chevron" aria-label="打开" />
              </tr>
            </thead>
            <tbody>
              {items === null && !error && <SkeletonRows cols={7} rows={6} />}
              {items === null && error && (
                <tr><td colSpan={7}><ErrorState message={error} onRetry={() => { void load(); void loadStats(); }} /></td></tr>
              )}
              {items !== null && shown.map((i) => (
                <IncidentRow key={i.id} incident={i} now={now} />
              ))}
            </tbody>
          </table>
        </div>
        {items !== null && items.length === 0 && (
          <EmptyState
            icon={stats && stats.total > 0 ? "search" : "inbox"}
            title={stats && stats.total > 0 ? "没有匹配的事故" : "还没有事故"}
            description={stats && stats.total > 0
              ? "调整筛选条件或清空搜索。"
              : "手动新建，或接入告警与日志扫描。"}
            action={stats && stats.total > 0
              ? <button type="button" className="btn" onClick={clearFilters}>清除筛选</button>
              : canWrite
                ? <button type="button" className="btn btn-primary" onClick={() => setCreateOpen(true)}><Icon name="plus" size={14} /> 新建事故</button>
                : undefined}
          />
        )}
        {items !== null && items.length > 0 && (
          <div className="pagination">
            <span>共 {total} 条 · 第 {safePage} / {pages} 页</span>
            <div className="pagination-controls">
              <button type="button" className="icon-btn" disabled={safePage <= 1} onClick={() => setPage(safePage - 1)} aria-label="上一页">
                <Icon name="chevron-left" size={15} />
              </button>
              {pageWindow(safePage, pages).map((p, idx) => p === "…" ? (
                <span key={`gap-${idx}`} className="page-gap">…</span>
              ) : (
                <button
                  key={p} type="button" className={`page-btn ${p === safePage ? "active" : ""}`}
                  aria-current={p === safePage ? "page" : undefined} onClick={() => setPage(p)}
                >{p}</button>
              ))}
              <button type="button" className="icon-btn" disabled={safePage >= pages} onClick={() => setPage(safePage + 1)} aria-label="下一页">
                <Icon name="chevron-right" size={15} />
              </button>
            </div>
          </div>
        )}
      </div>

      <CreateIncidentModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={(id) => navigate(`/incidents/${id}`)} />
    </div>
  );
}

function IncidentRow({ incident: i, now }: { incident: IncidentSummary; now: number }) {
  const waiting = WAITING_STATES.has(i.state);
  return (
    // 等待人工的状态只由「状态」列表达，行本身用左侧色条强调，避免同一件事出现两个说法
    <tr className={`clickable ${waiting ? "row-waiting" : ""}`} onClick={() => navigate(`/incidents/${i.id}`)}>
      <td className="cell-incident">
        <div className="incident-line">
          <SeverityBadge severity={i.severity} />
          <a
            href={`/incidents/${i.id}`} className="incident-title" title={i.title}
            onClick={(e) => { e.preventDefault(); navigate(`/incidents/${i.id}`); }}
          >
            {i.title}
          </a>
          {i.signal_count > 1 && (
            <span className="signal-count" title={`同指纹信号累计 ${i.signal_count} 次`}>×{i.signal_count}</span>
          )}
        </div>
        <div className="incident-sub">
          <span className="mono">{i.id}</span>
          <span className="dot-sep">·</span>
          <span>{i.env}</span>
          {i.service && <><span className="dot-sep">·</span><span>{i.service}</span></>}
          {i.assignee_id && (
            <><span className="dot-sep">·</span><span className="assigned-tag" title="已指派负责人"><Icon name="user-round" size={11} /> 已指派</span></>
          )}
        </div>
      </td>
      <td><StateBadge state={i.state} size="sm" /></td>
      <td className="hide-md"><span className="service-chip">{i.service ?? "—"}</span></td>
      <td className="hide-lg">
        {i.fix_branch ? (
          <span className="branch-cell">
            <Icon name="git-branch" size={12} />
            <span className="mono" title={i.fix_branch}>{i.fix_branch}</span>
            {i.target_branch && <span className="branch-target">→ {i.target_branch}</span>}
          </span>
        ) : <span className="muted">—</span>}
      </td>
      <td className="num hide-lg muted">{i.cost_tokens ? compactNumber(i.cost_tokens) : "—"}</td>
      <td className="cell-time hide-xs" title={dateTime(i.updated_at)}>
        <span>{relTime(i.updated_at, now)}</span>
        {waiting && <span className="time-wait" aria-hidden />}
      </td>
      <td className="col-chevron"><Icon name="chevron-right" size={14} /></td>
    </tr>
  );
}

function CreateIncidentModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    api.projects().then(setProjects).catch(() => setProjects([]));
  }, [open]);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      let project = String(f.get("project") || "");
      if (!project) project = (await api.createProject("demo-shop", "acme/demo-shop")).name;
      const res = await api.createIncident({
        project,
        title: String(f.get("title")).trim(),
        service: String(f.get("service") || "").trim() || undefined,
        stacktrace: String(f.get("stacktrace") || "").trim() || undefined,
        scenario: String(f.get("scenario")),
      });
      onClose();
      onCreated(res.incident_id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open} onClose={onClose} size="lg"
      title="新建事故"
      description="相同指纹的信号会合并到已有事故。"
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
        <button type="submit" form="create-incident-form" className="btn btn-primary" disabled={busy}>
          {busy ? <Spinner /> : <Icon name="plus" size={14} />}{busy ? "创建中…" : "创建事故"}
        </button>
      </>}
    >
      <form id="create-incident-form" className="form-stack" onSubmit={(e) => void submit(e)}>
        <div className="form-grid">
          <label className="field">
            <span>项目</span>
            <select name="project" defaultValue={projects?.[0]?.name ?? ""} disabled={projects === null}>
              {projects === null && <option value="">加载项目中…</option>}
              {projects?.length ? projects.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)
                : <option value="">demo-shop（提交时自动创建）</option>}
            </select>
            <small>没有项目时自动创建 demo-shop。</small>
          </label>
          <label className="field">
            <span>模拟场景 <em className="field-tag">仅模拟模式</em></span>
            <select name="scenario" defaultValue="happy">
              {SCENARIOS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </select>
            <small>LLM 模式下不生效。</small>
          </label>
          <label className="field field-wide">
            <span>标题</span>
            <input name="title" required maxLength={500} defaultValue="下单并发时库存出现负数" placeholder="一句话描述现象" />
          </label>
          <label className="field">
            <span>服务</span>
            <input name="service" defaultValue="order" placeholder="例如 order" />
          </label>
          <label className="field field-wide">
            <span>异常栈 <em className="field-tag">可选</em></span>
            <textarea
              name="stacktrace" rows={5} className="mono"
              defaultValue={`java.lang.IllegalStateException: stock < 0\n    at com.acme.inventory.StockService.deduct(StockService.java:42)\n    at com.acme.order.OrderService.create(OrderService.java:88)`}
            />
          </label>
        </div>
        {error && <div className="banner banner-danger" role="alert"><Icon name="alert-circle" size={15} /><span>{error}</span></div>}
      </form>
    </Modal>
  );
}

function pageWindow(current: number, total: number): (number | "…")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const out: (number | "…")[] = [1];
  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  if (start > 2) out.push("…");
  for (let p = start; p <= end; p++) out.push(p);
  if (end < total - 1) out.push("…");
  out.push(total);
  return out;
}