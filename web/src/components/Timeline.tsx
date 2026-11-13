import { useEffect, useMemo, useRef, useState } from "react";
import type { IncidentEvent, LiveState } from "../api";
import { actorLabel, clock, dateTime, STATE_LABEL } from "../format";
import { Icon } from "./Icons";
import { EmptyState, LiveStatus } from "./ui";

type Filter = "key" | "state" | "agent" | "tool" | "report";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "key", label: "关键事件" },
  { key: "state", label: "状态" },
  { key: "agent", label: "Agent" },
  { key: "tool", label: "工具" },
  { key: "report", label: "报告" },
];

const STATE_KINDS = new Set([
  "created", "transition", "signal", "approval_requested", "approval_decided",
  "cancel_requested", "guidance", "finalized", "repro_confirmed", "precedents",
]);
const AGENT_KINDS = new Set([
  "run_started", "run_finished", "attempt_started", "workspace_ready", "twin_ready",
  "twin_destroyed", "twin_failed", "step_failed",
]);
const TOOL_KINDS = new Set(["tool_call"]);
const REPORT_KINDS = new Set(["report"]);

const KIND_LABEL: Record<string, string> = {
  created: "创建", transition: "状态", tool_call: "工具", run_started: "开始", run_finished: "结束",
  signal: "信号", approval_requested: "审批", approval_decided: "决定", report: "报告", attempt_started: "尝试",
  twin_ready: "孪生", twin_destroyed: "回收", twin_failed: "孪生失败", workspace_ready: "工作区", step_failed: "失败",
  cancel_requested: "取消", guidance: "指导", finalized: "完成", repro_confirmed: "确认", precedents: "判例",
};

function matches(filter: Filter, e: IncidentEvent): boolean {
  switch (filter) {
    case "key": return e.kind !== "tool_call";
    case "state": return STATE_KINDS.has(e.kind);
    case "agent": return AGENT_KINDS.has(e.kind);
    case "tool": return TOOL_KINDS.has(e.kind);
    case "report": return REPORT_KINDS.has(e.kind);
  }
}

function isBad(e: IncidentEvent): boolean {
  const status = (e.payload as Record<string, unknown>).status;
  return e.kind === "step_failed" || e.kind === "twin_failed" || status === "denied" || status === "error" || status === "timeout";
}

export function Timeline({ events, live }: { events: IncidentEvent[]; live: LiveState }) {
  const [filter, setFilter] = useState<Filter>("key");
  const list = useRef<HTMLOListElement>(null);
  const stick = useRef(true);
  const lastLen = useRef(0);
  const [unread, setUnread] = useState(0);

  const shown = useMemo(() => events.filter((e) => matches(filter, e)), [events, filter]);

  useEffect(() => {
    const el = list.current;
    lastLen.current = shown.length;
    setUnread(0);
    stick.current = true;
    if (el) el.scrollTop = el.scrollHeight;
  }, [filter]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = list.current;
    const added = shown.length - lastLen.current;
    lastLen.current = shown.length;
    if (added > 0) {
      if (stick.current && el) el.scrollTop = el.scrollHeight;
      else setUnread((n) => n + added);
    }
  }, [shown.length]);

  function onScroll() {
    const el = list.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 28;
    stick.current = atBottom;
    if (atBottom) setUnread(0);
  }

  function jumpToBottom() {
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
    stick.current = true;
    setUnread(0);
  }

  return (
    <section className="card timeline-card">
      <header className="timeline-head">
        <div className="timeline-title-row">
          <div className="card-title"><Icon name="activity" size={15} className="card-title-icon" />实时事件</div>
          <div className="timeline-live"><LiveStatus state={live} /></div>
        </div>
        <div className="timeline-filters" role="tablist" aria-label="事件筛选">
          {FILTERS.map((f) => (
            <button key={f.key} type="button" role="tab" aria-selected={filter === f.key} onClick={() => setFilter(f.key)}>
              {f.label}
            </button>
          ))}
          <span className="timeline-count">{events.length}</span>
        </div>
      </header>
      <div className="timeline-scroll-wrap">
        {shown.length === 0 ? (
          <EmptyState
            icon="radio"
            title={events.length === 0 ? "还没有事件" : "此分类下没有事件"}
            description={events.length === 0 ? "Agent 尚未开始处理。" : "切换分类查看。"}
          />
        ) : (
          <ol className="timeline" ref={list} onScroll={onScroll} aria-label="事故事件时间线">
            {shown.map((e) => {
              const bad = isBad(e);
              return (
                <li key={e.id} className={`timeline-item ${bad ? "bad" : ""} ev-${e.kind}`}>
                  <span className="timeline-dot" aria-hidden />
                  <div className="timeline-meta">
                    <time className="mono" dateTime={e.created_at} title={dateTime(e.created_at)}>{clock(e.created_at)}</time>
                    <span className={`event-kind kind-${e.kind}`}>{KIND_LABEL[e.kind] ?? e.kind}</span>
                  </div>
                  <div className="timeline-body">{describe(e)}</div>
                  <div className="timeline-actor">{actorLabel(e.actor)}</div>
                </li>
              );
            })}
          </ol>
        )}
        {unread > 0 && (
          <button type="button" className="timeline-new" onClick={jumpToBottom}>
            <Icon name="arrow-down" size={13} />{unread} 条新事件
          </button>
        )}
      </div>
    </section>
  );
}

function describe(e: IncidentEvent): React.ReactNode {
  const p = e.payload as Record<string, unknown>;
  switch (e.kind) {
    case "transition":
      return (
        <>
          <strong>{e.from_state ? STATE_LABEL[e.from_state] : "接入"} → {e.to_state ? STATE_LABEL[e.to_state] : "—"}</strong>
          {e.reason && <span className="event-reason"> {e.reason}</span>}
        </>
      );
    case "tool_call":
      return (
        <>
          <span className="mono tool-name">{String(p.tool)}</span>
          <span className={`inline-tag ${String(p.status)}`}>{String(p.status)}</span>
          <span className="event-reason">{String(p.summary ?? "").slice(0, 160)}</span>
          {p.latency_ms != null && <span className="event-latency">{String(p.latency_ms)}ms</span>}
        </>
      );
    case "run_started":
      return <>Agent <strong>{String(p.agent)}</strong> 开始执行{p.attempt_seq ? ` · 第 ${String(p.attempt_seq)} 轮` : ""}</>;
    case "run_finished":
      return (
        <>
          Agent <strong>{String(p.agent)}</strong> {String(p.status)}
          {p.outcome ? <> → <strong>{String(p.outcome)}</strong></> : null}
          <span className="event-latency">{String(p.tool_calls ?? 0)} 次工具 · {String(p.latency_ms ?? 0)}ms</span>
          {p.error ? <span className="text-danger"> {String(p.error)}</span> : null}
        </>
      );
    case "attempt_started":
      return <>第 {String(p.seq)} 次修复（{triggerLabel(String(p.trigger))}）</>;
    case "approval_requested":
      return <>{p.action === "merge" ? "请求合并审批" : "请求人工介入"}{p.status === "approved" ? "（自动批准）" : ""}</>;
    case "approval_decided":
      return <>审批{String(p.status)}{e.reason ? `：${e.reason}` : ""}</>;
    case "report":
      return <>报告 {String(p.type)} · {String(p.status)}</>;
    case "twin_ready":
    case "twin_destroyed":
      return <span className="mono">{String(p.compose_project ?? "")}</span>;
    case "signal":
      return <>同指纹信号，累计 {String(p.count)} 次</>;
    case "repro_confirmed":
      return <>系统重跑复现用例 <span className="mono">{String(p.selector)}</span>：{String(p.failures)} 次失败</>;
    case "precedents":
      return <>参考了 {(p.ids as string[] | undefined)?.length ?? 0} 个历史判例</>;
    case "step_failed":
      return <><strong>步骤失败</strong>{p.error ? <span className="text-danger"> {String(p.error)}</span> : null}</>;
    case "twin_failed":
      return <><strong>孪生环境准备失败</strong>{p.error ? <span className="text-danger"> {String(p.error)}</span> : null}</>;
    default:
      return e.reason ?? "";
  }
}

function triggerLabel(t: string): string {
  return ({
    initial: "首次尝试",
    verify_failed: "验证未通过",
    review_rejected: "评审驳回",
    human_guided: "人工指导",
  } as Record<string, string>)[t] ?? t;
}