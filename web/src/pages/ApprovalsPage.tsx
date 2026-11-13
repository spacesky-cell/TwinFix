import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, subscribe, type ApprovalRow, type LiveState } from "../api";
import { Icon } from "../components/Icons";
import { SeverityBadge, StateBadge } from "../components/StateBadge";
import {
  Badge, ConfirmDialog, EmptyState, ErrorState, LiveStatus, ReasonDialog, Spinner,
} from "../components/ui";
import { actorLabel, dateTime, relTime, STATE_LABEL } from "../format";
import { navigate } from "../router";

type Filter = "pending" | "approved" | "rejected" | "all";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "pending", label: "待处理" },
  { value: "approved", label: "已批准" },
  { value: "rejected", label: "未通过" },
  { value: "all", label: "全部" },
];

const ACTION_LABEL: Record<ApprovalRow["action"], string> = {
  merge: "合并审批",
  human_takeover: "人工接手",
  tool: "工具调用",
};

const STATUS_LABEL: Record<ApprovalRow["status"], string> = {
  pending: "待处理",
  approved: "已批准",
  rejected: "已驳回",
  expired: "已过期",
};

const STATUS_TONE: Record<ApprovalRow["status"], "attention" | "success" | "danger" | "neutral"> = {
  pending: "attention",
  approved: "success",
  rejected: "danger",
  expired: "neutral",
};

export function ApprovalsPage() {
  const [items, setItems] = useState<ApprovalRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState<LiveState>("connecting");
  const [filter, setFilter] = useState<Filter>("pending");
  const [query, setQuery] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const reloadTimer = useRef<number | undefined>(undefined);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const page = await api.allApprovals();
      // 工具级审批在控制台没有决策入口，只留在审计记录里，不进入待办列表
      setItems(page.items.filter((a) => a.action !== "tool"));
      setError(null);
      setLoadedAt(new Date());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const off = subscribe("/stream", () => {
      window.clearTimeout(reloadTimer.current);
      reloadTimer.current = window.setTimeout(() => void load(true), 400);
    }, setLive);
    return () => { off(); window.clearTimeout(reloadTimer.current); };
  }, [load]);

  const counts = useMemo(() => {
    const all = items ?? [];
    const pending = all.filter((a) => a.status === "pending");
    return {
      pending: pending.length,
      approved: all.filter((a) => a.status === "approved").length,
      rejected: all.filter((a) => a.status === "rejected" || a.status === "expired").length,
      all: all.length,
    };
  }, [items]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (items ?? [])
      .filter((a) => {
        if (filter === "pending" && a.status !== "pending") return false;
        if (filter === "approved" && a.status !== "approved") return false;
        if (filter === "rejected" && a.status !== "rejected" && a.status !== "expired") return false;
        if (!q) return true;
        return [a.incident.title, a.incident.id, a.incident.service, ACTION_LABEL[a.action], a.comment]
          .some((v) => v?.toLowerCase().includes(q));
      })
      .sort((a, b) => {
        if (a.status === "pending" && b.status !== "pending") return -1;
        if (b.status === "pending" && a.status !== "pending") return 1;
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      });
  }, [items, filter, query]);

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">审批</h1>
        </div>
        <div className="page-header-side">
          <LiveStatus state={live} />
          <button type="button" className="btn btn-sm" onClick={() => void load()} disabled={refreshing}>
            {refreshing ? <Spinner size={13} /> : <Icon name="refresh" size={13} />} 刷新
          </button>
          <span className="updated-at" title={loadedAt ? loadedAt.toLocaleString("zh-CN") : undefined}>
            {loadedAt ? `更新于 ${loadedAt.toLocaleTimeString("zh-CN", { hour12: false })}` : "尚未加载"}
          </span>
        </div>
      </header>

      <div className="list-toolbar">
        <div className="filter-tabs" role="tablist" aria-label="按审批状态筛选">
          {FILTERS.map((f) => (
            <button key={f.value} type="button" role="tab" aria-selected={filter === f.value} onClick={() => setFilter(f.value)}>
              {f.label}
              <span className="tab-count">
                {f.value === "pending" ? counts.pending : f.value === "approved" ? counts.approved : f.value === "rejected" ? counts.rejected : counts.all}
              </span>
            </button>
          ))}
        </div>
        <div className="toolbar-spacer" />
        <div className="search-box">
          <Icon name="search" size={14} />
          <input
            id="approval-search" name="q"
            type="search" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索事故、服务或审批意见" aria-label="搜索审批"
          />
          {query && <button type="button" className="search-clear" onClick={() => setQuery("")} aria-label="清空搜索"><Icon name="x" size={12} /></button>}
        </div>
      </div>

      {error && items !== null && (
        <div className="banner banner-danger" role="alert">
          <Icon name="alert-circle" size={15} /><span>刷新失败：{error}</span>
          <button type="button" className="btn btn-sm" onClick={() => void load()}>重试</button>
        </div>
      )}

      {items === null && error ? (
        <ErrorState title="无法加载审批" message={error} onRetry={() => void load()} />
      ) : items === null ? (
        <div className="approval-list" aria-busy="true" aria-label="正在加载审批">
          <ApprovalSkeleton />
          <ApprovalSkeleton />
          <ApprovalSkeleton />
        </div>
      ) : filtered.length === 0 ? (
        <div className="card">
          <EmptyState
            icon={filter === "pending" ? "check-circle" : "inbox"}
            title={
              filter === "pending" ? (query ? "没有匹配的待办" : "没有待处理的审批")
                : items.length === 0 ? "还没有审批记录" : "此分类下没有记录"
            }
            description={filter === "pending" ? "Agent 请求合并或需要人工接手时会出现在这里。" : "切换到其他分类查看。"}
            action={filter !== "pending" ? <button type="button" className="btn" onClick={() => setFilter("pending")}>查看待处理</button> : undefined}
          />
        </div>
      ) : (
        <div className="approval-list">
          {filtered.map((a) => (
            <ApprovalCard key={a.id} approval={a} onChanged={() => void load(true)} />
          ))}
        </div>
      )}
    </div>
  );
}

function ApprovalSkeleton() {
  return (
    <div className="approval-skeleton" aria-hidden="true">
      <div className="row">
        <span className="skeleton" style={{ width: 26, height: 18 }} />
        <span className="skeleton" style={{ width: "42%", height: 13 }} />
        <span className="skeleton" style={{ width: 64, height: 18 }} />
      </div>
      <span className="skeleton" style={{ width: "64%" }} />
      <span className="skeleton" style={{ width: "86%" }} />
      <span className="skeleton" style={{ width: "36%" }} />
    </div>
  );
}


function ApprovalCard({ approval: a, onChanged }: { approval: ApprovalRow; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [approveOpen, setApproveOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const inc = a.incident;
  const pending = a.status === "pending";
  const expired = pending && a.expires_at != null && new Date(a.expires_at).getTime() <= Date.now();
  const expiringSoon = pending && !expired && a.expires_at != null && new Date(a.expires_at).getTime() - Date.now() < 86400_000;
  const canDecide = pending && !expired && a.action === "merge" && inc.state === "AWAITING_REVIEW";
  const canHandle = pending && !expired && a.action === "human_takeover" && inc.state === "NEEDS_HUMAN";
  const stale = pending && ((a.action === "merge" && inc.state !== "AWAITING_REVIEW") || (a.action === "human_takeover" && inc.state !== "NEEDS_HUMAN"));

  async function decide(approve: boolean, comment?: string) {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      await api.decide(a.id, approve, comment);
      setApproveOpen(false);
      setRejectOpen(false);
      setDone(approve ? "已批准合并。" : "已驳回，意见已交回修复。");
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className={`approval-card ${pending ? "is-pending" : "is-decided"} ${a.action === "merge" && pending ? "is-merge" : ""}`}>
      <div className="approval-main">
        <div className="approval-top">
          <SeverityBadge severity={inc.severity} />
          <a
            className="approval-title" href={`/incidents/${inc.id}`}
            onClick={(e) => { e.preventDefault(); navigate(`/incidents/${inc.id}`); }}
          >
            {inc.title}
          </a>
          {a.action !== "human_takeover" && (
            <Badge tone={a.action === "merge" ? "attention" : "neutral"} dot>{ACTION_LABEL[a.action]}</Badge>
          )}
          <StateBadge state={inc.state} />
        </div>
        <div className="approval-meta">
          <span className="mono">{inc.id}</span>
          {inc.service && <><span className="dot-sep">·</span><span>{inc.service}</span></>}
          {inc.target_branch && <><span className="dot-sep">·</span><span className="mono">目标 {inc.target_branch}</span></>}
          <span className="dot-sep">·</span>
          <span>请求于 {relTime(a.created_at)}</span>
        </div>
        <div className="approval-facts">
          {typeof a.payload?.pr_number === "number" && (
            <span className="fact-chip"><Icon name="git-merge" size={12} />PR #{String(a.payload.pr_number)}</span>
          )}
          {a.action === "tool" && typeof a.payload?.tool === "string" && (
            <span className="fact-chip"><Icon name="terminal" size={12} /><span className="mono">{String(a.payload.tool)}</span></span>
          )}
          {a.comment && (
            <span className="fact-chip fact-comment" title={a.comment}><Icon name="info" size={12} />{a.comment}</span>
          )}
        </div>

        {(error || done) && (
          <div className={`banner ${error ? "banner-danger" : "banner-success"} approval-banner`} role={error ? "alert" : "status"}>
            <Icon name={error ? "alert-circle" : "check-circle"} size={14} />
            <span>{error ?? done}</span>
          </div>
        )}
      </div>

      <div className="approval-side">
        <div className="approval-side-top">
          <Badge tone={expired ? "neutral" : STATUS_TONE[a.status]} dot>{expired ? "已过期" : STATUS_LABEL[a.status]}</Badge>
          {expiringSoon && <Badge tone="danger">即将到期</Badge>}
        </div>
        {a.expires_at && pending && (
          <span className={`approval-expiry ${expiringSoon || expired ? "urgent" : ""}`}>
            <Icon name="clock" size={12} />
            {expired ? "已于 " : "有效期至 "}
            {new Date(a.expires_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}
          </span>
        )}
        {!pending && (
          <div className="decision-record">
            {a.decided_by && <span><Icon name="user" size={12} />{actorLabel(`user:${a.decided_by}`)}</span>}
            {a.decided_at && <span>{dateTime(a.decided_at)}</span>}
          </div>
        )}

        <div className="approval-actions">
          {canDecide && (
            <>
              <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={() => setApproveOpen(true)}>
                <Icon name="check" size={13} /> 批准合并
              </button>
              <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setRejectOpen(true)}>
                <Icon name="rotate-ccw" size={13} /> 驳回
              </button>
            </>
          )}
          {canHandle && (
            <button type="button" className="btn btn-sm btn-primary" onClick={() => navigate(`/incidents/${inc.id}`)}>
              <Icon name="arrow-up-right" size={13} /> 前往处理
            </button>
          )}
          {!canDecide && !canHandle && (
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => navigate(`/incidents/${inc.id}`)}>查看事故</button>
          )}
          {stale && (
            <span className="muted small">事故已转为 {STATE_LABEL[inc.state]}，此待办已失效</span>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={approveOpen} onClose={() => setApproveOpen(false)} busy={busy}
        onConfirm={() => void decide(true)}
        title="确认批准并合并？" confirmLabel="批准合并"
        description="会改写目标分支；审批人与时间写入审计记录。"
      >
        <div className="confirm-impact">
          <p className="confirm-impact-title"><Icon name="git-merge" size={15} /> 合并信息</p>
          <ul>
            <li>事故：{inc.title}</li>
            <li>目标分支：<code>{inc.target_branch ?? "项目默认分支"}</code></li>
            {typeof a.payload?.pr_number === "number" && <li>合并请求：PR #{String(a.payload.pr_number)}</li>}
          </ul>
          <p className="hint">批准前请核对验证结果与代码差异。</p>
        </div>
      </ConfirmDialog>

      <ReasonDialog
        open={rejectOpen} onClose={() => setRejectOpen(false)} busy={busy}
        onConfirm={(reason) => void decide(false, reason)}
        title="驳回合并审批" confirmLabel="确认驳回" tone="danger"
        description="驳回意见会作为下一轮修复的输入。"
        label="驳回原因" placeholder="例如：回归测试没有覆盖取消订单的场景，请补充后再提审。"
      />
    </article>
  );
}

