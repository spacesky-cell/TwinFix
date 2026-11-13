import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { LiveState } from "../api";
import { Icon, type IconName } from "./Icons";

export function Spinner({ size = 14 }: { size?: number }) {
  return <span className="spinner" style={{ width: size, height: size }} aria-hidden />;
}

export function LoadingState({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <Spinner size={16} />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({ icon = "inbox", title, description, action }: {
  icon?: IconName; title: string; description?: ReactNode; action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon"><Icon name={icon} size={20} /></span>
      <strong>{title}</strong>
      {description && <p>{description}</p>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}

export function ErrorState({ title = "加载失败", message, onRetry }: {
  title?: string; message: string; onRetry?: () => void;
}) {
  return (
    <div className="error-state" role="alert">
      <span className="empty-icon danger"><Icon name="alert-circle" size={20} /></span>
      <strong>{title}</strong>
      <p>{message}</p>
      {onRetry && <button type="button" className="btn btn-sm" onClick={onRetry}><Icon name="refresh" size={14} /> 重试</button>}
    </div>
  );
}

export function SkeletonRows({ cols = 6, rows = 6 }: { cols?: number; rows?: number }) {
  return (
    <>
      {Array.from({ length: rows }, (_, r) => (
        <tr className="skeleton-row" key={r} aria-hidden="true">
          {Array.from({ length: cols }, (_, c) => (
            <td key={c}><span className="skeleton" style={{ width: c === 0 ? "78%" : `${38 + ((r + c) % 4) * 11}%` }} /></td>
          ))}
        </tr>
      ))}
    </>
  );
}

export function Modal({ open, onClose, title, description, children, footer, size = "md" }: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  // 只依赖 open：调用方常传内联 onClose，若把它放进依赖会在每次输入时重新聚焦、反复锁定 body。
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => {
      const preferred = panel.current?.querySelector<HTMLElement>("[data-autofocus]");
      const first = panel.current?.querySelector<HTMLElement>("input, textarea, select, button");
      (preferred ?? first)?.focus();
    }, 20);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
      window.clearTimeout(focusTimer);
    };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={panel} className={`modal modal-${size}`} role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="modal-head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description && <p className="modal-desc">{description}</p>}
          </div>
          <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭">
            <Icon name="x" size={16} />
          </button>
        </div>
        {children && <div className="modal-body">{children}</div>}
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

export function ConfirmDialog({ open, onClose, onConfirm, title, description, children, confirmLabel = "确认", cancelLabel = "取消", tone = "primary", busy }: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "primary" | "danger";
  busy?: boolean;
}) {
  return (
    <Modal
      open={open} onClose={onClose} title={title} description={description} size="sm"
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>{cancelLabel}</button>
        <button type="button" className={`btn ${tone === "danger" ? "btn-danger" : "btn-primary"}`} onClick={onConfirm} disabled={busy}>
          {busy ? <Spinner /> : null}{busy ? "处理中…" : confirmLabel}
        </button>
      </>}
    >
      {children}
    </Modal>
  );
}

export function ReasonDialog({ open, onClose, onConfirm, title, description, label, placeholder, confirmLabel = "确认", tone = "primary", required = true, busy }: {
  open: boolean;
  onClose: () => void;
  onConfirm: (reason: string) => void;
  title: ReactNode;
  description?: ReactNode;
  label: string;
  placeholder?: string;
  confirmLabel?: string;
  tone?: "primary" | "danger";
  required?: boolean;
  busy?: boolean;
}) {
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (open) {
      setReason("");
      setTouched(false);
    }
  }, [open]);
  const invalid = required && !reason.trim();
  return (
    <Modal
      open={open} onClose={onClose} title={title} description={description} size="md"
      footer={<>
        <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
        <button
          type="button" className={`btn ${tone === "danger" ? "btn-danger" : "btn-primary"}`}
          disabled={busy || invalid}
          onClick={() => { setTouched(true); if (!invalid) onConfirm(reason.trim()); }}
        >
          {busy ? <Spinner /> : null}{busy ? "提交中…" : confirmLabel}
        </button>
      </>}
    >
      <label className="field">
        <span>{label}{required && <em className="required">必填</em>}</span>
        <textarea
          data-autofocus rows={4} value={reason} placeholder={placeholder}
          aria-invalid={touched && invalid}
          onChange={(e) => { setReason(e.target.value); setTouched(true); }}
        />
        {touched && invalid && <span className="field-error">请填写原因后再提交。</span>}
      </label>
    </Modal>
  );
}

export function CopyButton({ text, label, className = "" }: { text: string; label?: string; className?: string }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = window.setTimeout(() => setState("idle"), 1600);
    return () => window.clearTimeout(t);
  }, [state]);
  return (
    <button
      type="button" className={`icon-btn copy-btn ${className}`} title={state === "ok" ? "已复制" : state === "fail" ? "复制失败" : "复制"}
      onClick={(e) => {
        e.stopPropagation();
        void (navigator.clipboard?.writeText(text) ?? Promise.reject(new Error("unsupported")))
          .then(() => setState("ok")).catch(() => setState("fail"));
      }}
    >
      <Icon name={state === "ok" ? "check" : "copy"} size={13} />
      {label && <span>{state === "ok" ? "已复制" : label}</span>}
    </button>
  );
}

export function LiveStatus({ state, compact = false }: { state: LiveState; compact?: boolean }) {
  const text = state === "live" ? "实时" : state === "offline" ? "连接中断" : "连接中";
  return (
    <span className={`live-status ${state}`} title={state === "live" ? "SSE 实时连接正常" : state === "offline" ? "SSE 已断开，浏览器正在自动重连" : "正在建立 SSE 实时连接"}>
      <span className="live-dot" aria-hidden />
      {!compact && <span>{text}</span>}
    </span>
  );
}

export function Badge({ tone = "neutral", dot = false, children, className = "", title }: {
  tone?: "neutral" | "active" | "attention" | "success" | "danger" | "brand";
  dot?: boolean;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span className={`badge badge-${tone} ${className}`} title={title}>
      {dot && <span className="badge-dot" aria-hidden />}
      {children}
    </span>
  );
}

/** 概览指标卡：数字 + 一句口径说明 + 可选可视化（环形 / 构成条）。 */
export function KpiCard({ icon, label, value, foot, tone = "neutral", visual }: {
  icon: IconName;
  label: string;
  value: ReactNode;
  foot?: ReactNode;
  tone?: "neutral" | "active" | "attention" | "danger" | "success";
  visual?: ReactNode;
}) {
  return (
    <div className={`kpi-card tone-${tone}`}>
      <div className="kpi-head">
        <span className="kpi-icon"><Icon name={icon} size={13} /></span>
        <span className="kpi-label">{label}</span>
      </div>
      <div className="kpi-value-row">
        <strong className="kpi-value">{value}</strong>
        {visual && <span className="kpi-visual">{visual}</span>}
      </div>
      {foot && <span className="kpi-foot">{foot}</span>}
    </div>
  );
}

export function Card({ title, icon, subtitle, count, actions, children, className = "", bodyClassName = "card-body", flush = false }: {
  title?: ReactNode;
  icon?: IconName;
  subtitle?: ReactNode;
  count?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  flush?: boolean;
}) {
  return (
    <section className={`card ${className}`}>
      {title && (
        <header className="card-head">
          <div className="card-heading">
            <div className="card-title">
              {icon && <Icon name={icon} size={15} className="card-title-icon" />}
              {title}
              {count != null && <span className="card-count">{count}</span>}
            </div>
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className={flush ? `${bodyClassName} flush` : bodyClassName}>{children}</div>
    </section>
  );
}