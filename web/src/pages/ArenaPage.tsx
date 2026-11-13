import { useEffect, useMemo, useState } from "react";
import { api, type ArenaCase } from "../api";
import { Icon } from "../components/Icons";
import { SeverityBadge } from "../components/StateBadge";
import {
  Badge, ConfirmDialog, EmptyState, LoadingState, Spinner,
} from "../components/ui";
import { dateTime } from "../format";
import { navigate } from "../router";

const OUTCOME_LABEL: Record<string, string> = {
  fixed: "修复",
  NEEDS_HUMAN: "转人工",
  NOT_BUG: "非缺陷",
  ALREADY_FIXED: "已被修复",
  CANNOT_REPRODUCE: "无法复现",
  CANNOT_FIX: "无法修复",
};

const OUTCOME_TONE: Record<string, "success" | "attention" | "neutral" | "danger"> = {
  fixed: "success",
  NEEDS_HUMAN: "attention",
  NOT_BUG: "neutral",
  ALREADY_FIXED: "neutral",
  CANNOT_REPRODUCE: "danger",
  CANNOT_FIX: "danger",
};

interface InjectResult {
  project: string;
  repo: string;
  branch: string;
  sha: string;
  incident_id: string | null;
  created: boolean | null;
  at: string;
}

export function ArenaPage() {
  const [cases, setCases] = useState<ArenaCase[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [category, setCategory] = useState("all");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, InjectResult>>({});
  const [notice, setNotice] = useState<{ case: ArenaCase; result: InjectResult } | null>(null);
  const [pending, setPending] = useState<ArenaCase | null>(null);
  const [trigger, setTrigger] = useState(true);
  const [loadError, setLoadError] = useState(false);

  function load() {
    setCases(null);
    setError(null);
    setLoadError(false);
    api.arenaCases().then(setCases).catch((e: Error) => { setError(e.message); setLoadError(true); });
  }

  useEffect(() => { load(); }, []);

  const usable = useMemo(() => (cases ?? []).filter((c) => !c.error), [cases]);
  const categories = useMemo(() => {
    const map = new Map<string, number>();
    for (const c of usable) map.set(c.category, (map.get(c.category) ?? 0) + 1);
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0], "zh-CN"));
  }, [usable]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (cases ?? []).filter((c) => {
      if (category !== "all" && !c.error && c.category !== category) return false;
      if (!q) return true;
      return [c.id, c.title, c.category, c.description, ...c.inject.services, c.inject.file, ...c.expect.root_cause_keywords]
        .some((v) => v?.toLowerCase().includes(q));
    });
  }, [cases, category, query]);

  function openInject(c: ArenaCase) {
    setPending(c);
    setTrigger(true);
  }

  async function inject(c: ArenaCase, withTrigger: boolean) {
    setBusy(c.id);
    setError(null);
    setNotice(null);
    try {
      const r = await api.arenaInject(c.id, withTrigger);
      const result: InjectResult = {
        project: r.project, repo: r.repo, branch: r.branch, sha: r.sha,
        incident_id: r.incident_id, created: r.created, at: new Date().toISOString(),
      };
      setResults((prev) => ({ ...prev, [c.id]: result }));
      setNotice({ case: c, result });
      setPending(null);
      setExpanded(c.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const injectedCount = Object.keys(results).length;

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">Arena</h1>
        </div>
        <div className="page-header-side">
          <a className="btn btn-sm" href="/eval" onClick={(e) => { e.preventDefault(); navigate("/eval"); }}>
            <Icon name="chart" size={13} /> 查看评测
          </a>
          <button type="button" className="btn btn-sm" onClick={load} disabled={cases === null}>
            <Icon name="refresh" size={13} /> 刷新
          </button>
        </div>
      </header>

      <div className="list-toolbar arena-toolbar">
        <div className="filter-tabs scroll-x" role="tablist" aria-label="按故障类别筛选">
          <button type="button" role="tab" aria-selected={category === "all"} onClick={() => setCategory("all")}>
            全部<span className="tab-count">{usable.length}</span>
          </button>
          {categories.map(([c, n]) => (
            <button key={c} type="button" role="tab" aria-selected={category === c} onClick={() => setCategory(c)}>
              {c}<span className="tab-count">{n}</span>
            </button>
          ))}
        </div>
        <div className="toolbar-spacer" />
        {injectedCount > 0 && (
          <span className="session-injected" title="仅统计当前页面操作">
            <Icon name="check-circle" size={12} /> 本次已注入 {injectedCount}
          </span>
        )}
        <div className="search-box">
          <Icon name="search" size={14} />
          <input
            id="arena-search" name="q"
            type="search" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索用例、类别、服务或注入文件" aria-label="搜索用例"
          />
          {query && <button type="button" className="search-clear" onClick={() => setQuery("")} aria-label="清空搜索"><Icon name="x" size={12} /></button>}
        </div>
      </div>

      {error && (
        <div className="banner banner-danger" role="alert">
          <Icon name="alert-circle" size={15} /><span>{error}</span>
          {loadError && <button type="button" className="btn btn-sm" onClick={load}>重试</button>}
        </div>
      )}
      {notice && <InjectNotice notice={notice} onDismiss={() => setNotice(null)} />}

      {cases === null && !error && <LoadingState label="正在读取用例…" />}
      {cases !== null && shown.length === 0 && (
        <div className="card">
          <EmptyState
            icon={usable.length === 0 ? "flask" : "search"}
            title={usable.length === 0 ? "还没有用例" : "没有匹配的用例"}
            description={usable.length === 0
              ? "把 case.yaml 放进 arena/cases/<id>/ 后刷新。"
              : "调整类别或清空搜索。"}
            action={usable.length > 0 ? (
              <button type="button" className="btn" onClick={() => { setQuery(""); setCategory("all"); }}>清除筛选</button>
            ) : undefined}
          />
        </div>
      )}
      {cases !== null && shown.length > 0 && (
        <div className="table-shell">
          <div className="table-shell-head">
            <div><strong>{category === "all" ? "全部用例" : category}</strong><span className="muted"> · {shown.length} 条</span></div>
          </div>
          <div className="table-scroll">
            <table className="data-table arena-table">
              <thead>
                <tr>
                  <th className="col-incident">用例</th>
                  <th className="col-state">期望结论</th>
                  <th className="col-chevron" aria-label="展开" />
                </tr>
              </thead>
              <tbody>
                {shown.map((c) => c.error ? (
                  <CaseErrorRow key={c.id} c={c} />
                ) : (
                  <CaseRows
                    key={c.id} c={c}
                    open={expanded === c.id}
                    result={results[c.id]}
                    busy={busy === c.id}
                    anyBusy={busy !== null}
                    onToggle={() => setExpanded(expanded === c.id ? null : c.id)}
                    onInject={() => openInject(c)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={pending !== null}
        onClose={() => setPending(null)}
        busy={pending !== null && busy === pending.id}
        onConfirm={() => pending && void inject(pending, trigger)}
        title="注入故障并创建事故？"
        confirmLabel={trigger ? "注入并上报" : "仅注入"}
        description="这会创建真实的本地仓库与分支，不能从控制台自动撤销。"
      >
        {pending && (
          <div className="confirm-impact">
            <p className="confirm-impact-title"><Icon name="flask" size={15} /> 将执行的操作</p>
            <ul>
              <li>为用例 <code>{pending.id}</code> 创建项目仓库，并注入 <code>{pending.inject.file}</code>@{pending.inject.branch} 的缺陷补丁。</li>
              <li>涉及服务：<code>{pending.inject.services.join("、") || "运行时"}</code>。</li>
              <li>资源由后端按项目隔离；同一用例再次注入会创建新的项目与事故。</li>
            </ul>
            <label className="check-field">
              <input type="checkbox" name="trigger" checked={trigger} onChange={(e) => setTrigger(e.target.checked)} />
              <span>注入后立即上报信号，自动创建事故并交给 Agent 处理</span>
            </label>
            <p className="hint">不勾选时只准备仓库与缺陷，不创建事故。</p>
          </div>
        )}
      </ConfirmDialog>
    </div>
  );
}


function CaseRows({ c, open, result, busy, anyBusy, onToggle, onInject }: {
  c: ArenaCase; open: boolean; result?: InjectResult; busy: boolean; anyBusy: boolean;
  onToggle: () => void; onInject: () => void;
}) {
  const sev = c.severity === "S1" || c.severity === "S2" || c.severity === "S3" || c.severity === "S4" ? c.severity : null;
  return (
    <>
      <tr className={`clickable ${open ? "row-open" : ""}`} onClick={onToggle} aria-expanded={open}>
        <td className="cell-incident">
          <div className="incident-line">
            <span className="mono case-id">{c.id}</span>
            <span className="incident-title" title={c.description || c.title}>{c.title}</span>
            {result && <span className="injected-mark" title={`本次已于 ${dateTime(result.at)} 注入`}><Icon name="check-circle" size={13} /></span>}
            {sev && <SeverityBadge severity={sev} />}
          </div>
          <div className="incident-sub">
            <span className="service-chip">{c.category}</span>
            {c.on_review && <><span className="dot-sep">·</span><span>评审剧本</span></>}
            {c.description && <><span className="dot-sep">·</span><span className="case-desc-inline">{c.description}</span></>}
          </div>
        </td>
        <td><Badge tone={OUTCOME_TONE[c.expect.outcome] ?? "neutral"} dot>{OUTCOME_LABEL[c.expect.outcome] ?? c.expect.outcome}</Badge></td>
        <td className="col-chevron"><Icon name={open ? "chevron-down" : "chevron-right"} size={14} /></td>
      </tr>
      {open && (
        <tr className="case-detail-row">
          <td colSpan={3}>
            <div className="case-detail">
              <div className="case-detail-main">
                {c.description && <p className="case-detail-desc">{c.description}</p>}
                <div className="case-detail-grid">
                  <div><span>类别</span><strong>{c.category}</strong></div>
                  <div><span>期望结论</span><strong>{OUTCOME_LABEL[c.expect.outcome] ?? c.expect.outcome}</strong></div>
                  <div><span>注入类型</span><strong>{c.inject.type === "patch" ? "代码补丁" : "运行时"}</strong></div>
                  <div><span>补丁位置</span><strong className="mono">{c.inject.type === "patch" ? `${c.inject.file}@${c.inject.branch}` : "—"}</strong></div>
                  <div><span>涉及服务</span><strong>{c.inject.services.join("、") || "—"}</strong></div>
                  <div><span>隐藏测试</span><strong>{c.hidden_tests ? `${c.hidden_tests} 项` : "无"}</strong></div>
                </div>
                {c.expect.root_cause_keywords.length > 0 && (
                  <div className="keyword-row">
                    <span>根因关键词</span>
                    {c.expect.root_cause_keywords.map((k) => <span className="keyword" key={k}>{k}</span>)}
                  </div>
                )}
                {c.expect.root_cause_locations.length > 0 && (
                  <div className="location-list compact">
                    {c.expect.root_cause_locations.map((l) => (
                      <div className="location-row" key={l}><Icon name="file-code" size={13} /><span className="mono location-path">{l}</span></div>
                    ))}
                  </div>
                )}
                {result && (
                  <div className="inject-result">
                    <p className="inject-result-title"><Icon name="check-circle" size={14} /> 已在本次会话注入</p>
                    <div className="case-detail-grid">
                      <div><span>项目</span><strong className="mono">{result.project}</strong></div>
                      <div><span>仓库</span><strong className="mono">{result.repo}</strong></div>
                      <div><span>分支</span><strong className="mono">{result.branch}</strong></div>
                      <div><span>提交</span><strong className="mono">{result.sha.slice(0, 10)}</strong></div>
                      <div><span>注入时间</span><strong>{dateTime(result.at)}</strong></div>
                    </div>
                    <p className="small muted">
                      {result.incident_id
                        ? result.created === false
                          ? "信号已合并到现有事故："
                          : "已创建事故："
                        : "本次未上报信号，因此没有创建事故。"}
                      {result.incident_id && (
                        <a href={`/incidents/${result.incident_id}`} onClick={(e) => { e.preventDefault(); navigate(`/incidents/${result.incident_id}`); }}>
                          {result.incident_id}
                        </a>
                      )}
                    </p>
                  </div>
                )}
              </div>
              <div className="case-detail-side">
                <button type="button" className="btn btn-primary" disabled={anyBusy} onClick={(e) => { e.stopPropagation(); onInject(); }}>
                  {busy ? <Spinner size={13} /> : <Icon name="plus" size={13} />}
                  {busy ? "注入中…" : "注入并创建事故"}
                </button>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function CaseErrorRow({ c }: { c: ArenaCase }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className="clickable row-error" onClick={() => setOpen((v) => !v)}>
        <td><span className="mono case-id">{c.id}</span></td>
        <td><span className="text-danger small">case.yaml 无法解析</span></td>
        <td className="col-chevron"><Icon name={open ? "chevron-down" : "chevron-right"} size={14} /></td>
      </tr>
      {open && (
        <tr className="case-detail-row">
          <td colSpan={3}><pre className="code-block">{c.error}</pre></td>
        </tr>
      )}
    </>
  );
}

function InjectNotice({ notice, onDismiss }: { notice: { case: ArenaCase; result: InjectResult }; onDismiss: () => void }) {
  const { case: c, result: r } = notice;
  return (
    <div className="banner banner-success inject-notice" role="status">
      <Icon name="check-circle" size={16} />
      <div>
        <strong>已注入用例 {c.id}。</strong>
        <span>项目 <code>{r.project}</code> · 分支 <code>{r.branch}</code> · 提交 <code>{r.sha.slice(0, 10)}</code>。</span>
        {r.incident_id ? (
          <span>
            {r.created === false ? " 信号已合并到现有事故 " : " 已创建事故 "}
            <a href={`/incidents/${r.incident_id}`} onClick={(e) => { e.preventDefault(); navigate(`/incidents/${r.incident_id}`); }}>
              {r.incident_id} <Icon name="arrow-up-right" size={11} />
            </a>
          </span>
        ) : <span> 本次未上报信号。</span>}
      </div>
      <button type="button" className="icon-btn" onClick={onDismiss} aria-label="关闭"><Icon name="x" size={13} /></button>
    </div>
  );
}