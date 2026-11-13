import { useEffect, useRef, useState } from "react";
import { api, clearToken, type AuthUser } from "./api";
import { BrandMark, Icon, type IconName } from "./components/Icons";
import { ConfirmDialog } from "./components/ui";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { ArenaPage } from "./pages/ArenaPage";
import { EvalPage } from "./pages/EvalPage";
import { IncidentList } from "./pages/IncidentList";
import { IncidentPage } from "./pages/IncidentPage";
import { LoginPage } from "./pages/LoginPage";
import { OpsPage } from "./pages/OpsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { navigate, usePath } from "./router";

const ROLE_LABEL: Record<AuthUser["role"], string> = {
  admin: "管理员",
  maintainer: "维护者",
  viewer: "只读",
};

const LOGIN_PATH = "/login";

/** 登录后回到原页面；只接受站内路径，避免 next 参数被用来跳转外站。 */
function nextPath(): string {
  const raw = new URLSearchParams(window.location.search).get("next");
  return raw && raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
}

export function App() {
  const path = usePath();
  const match = path.match(/^\/incidents\/([0-9A-Z]{26})$/);
  const [user, setUser] = useState<AuthUser | null | undefined>(undefined);
  const [halted, setHalted] = useState<boolean | null>(null);
  const [mode, setMode] = useState("");
  const [confirmHalt, setConfirmHalt] = useState(false);
  const [haltBusy, setHaltBusy] = useState(false);
  const [shellError, setShellError] = useState<string | null>(null);

  useEffect(() => {
    api.me().then(setUser).catch(() => setUser(null));
    const onUnauthorized = () => setUser(null);
    window.addEventListener("twinfix:unauthorized", onUnauthorized);
    return () => window.removeEventListener("twinfix:unauthorized", onUnauthorized);
  }, []);

  // 未登录时跳到 /login，并记住原地址；已登录访问 /login 则回到目标页
  useEffect(() => {
    if (user === null && path !== LOGIN_PATH) {
      navigate(`${LOGIN_PATH}?next=${encodeURIComponent(path + window.location.search)}`, { replace: true });
    }
  }, [user, path]);

  useEffect(() => {
    if (user && path === LOGIN_PATH) navigate(nextPath(), { replace: true });
  }, [user, path]);

  function handleLogin(nextUser: AuthUser) {
    setUser(nextUser);
    navigate(nextPath(), { replace: true });
  }

  useEffect(() => {
    if (!user) return;
    let stopped = false;
    const loadHealth = () => {
      api.health().then((h) => {
        if (stopped) return;
        setHalted(h.halted);
        setMode(h.agent_mode);
      }).catch(() => { if (!stopped) setHalted(null); });
    };
    loadHealth();
    const timer = window.setInterval(loadHealth, 20_000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [user]);

  async function setSystemHalt(next: boolean) {
    setHaltBusy(true);
    setShellError(null);
    try {
      await api.setHalt(next);
      setHalted(next);
      setConfirmHalt(false);
    } catch (e) {
      setShellError((e as Error).message);
    } finally {
      setHaltBusy(false);
    }
  }

  function logout() {
    clearToken();
    setUser(null);
  }

  if (user === undefined) {
    return (
      <div className="boot-screen">
        <BrandMark size={30} />
        <span>正在载入 TwinFix 控制台…</span>
      </div>
    );
  }
  if (user === null) return <LoginPage onLogin={handleLogin} />;
  if (path === LOGIN_PATH) {
    return (
      <div className="boot-screen">
        <BrandMark size={30} />
        <span>正在进入控制台…</span>
      </div>
    );
  }

  // 只读账号用不到 Arena 与评测（后端也会返回 403），导航与路由一起隐藏，避免点进去吃错误
  const canDemo = user.role !== "viewer";

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-inner">
          <a className="brand" href="/" onClick={(e) => { e.preventDefault(); navigate("/"); }}>
            <span className="brand-mark"><BrandMark size={26} /></span>
            <span className="brand-name">TwinFix</span>
          </a>
          <nav className="nav" aria-label="主导航">
            <NavLink to="/" icon="alert-circle" active={path === "/" || path.startsWith("/incidents")}>事故</NavLink>
            <NavLink to="/approvals" icon="clipboard-check" active={path === "/approvals"}>审批</NavLink>
            <NavLink to="/ops" icon="server" active={path === "/ops"}>运行</NavLink>
            {canDemo && <NavLink to="/arena" icon="flask" active={path === "/arena"}>Arena</NavLink>}
            {canDemo && <NavLink to="/eval" icon="chart" active={path === "/eval"}>评测</NavLink>}
            <NavLink to="/settings" icon="sliders" active={path === "/settings"}>设置</NavLink>
          </nav>
          <div className="topbar-actions">
            {mode && (
              <span className="mode-chip" title="Agent 实现模式，由服务端 /health 返回，不等同于运行状态">
                <Icon name="bot" size={14} />
                <span className="mode-chip-label">Agent</span>
                <span className="mode-chip-value">{mode}</span>
              </span>
            )}
            <span
              className={`sys-chip ${halted === true ? "halted" : halted === false ? "ok" : "unknown"}`}
              title={halted === true ? "所有 Agent 工具调用已暂停" : halted === false ? "调度器与 Agent 正常运行" : "尚未获取服务状态"}
            >
              <span className="sys-dot" aria-hidden />
              {halted === true ? "已急停" : halted === false ? "运行正常" : "状态未知"}
            </span>
            <span className="topbar-sep" aria-hidden />
            <UserMenu user={user} onLogout={logout} />
            <button
              type="button"
              className={`btn btn-sm halt-btn ${halted ? "btn-danger-soft" : ""}`}
              onClick={() => halted ? void setSystemHalt(false) : setConfirmHalt(true)}
              disabled={halted === null || haltBusy}
              title={halted ? "恢复 Agent 工具调用" : "暂停所有 Agent 工具调用"}
            >
              {haltBusy ? <span className="spinner" /> : <Icon name={halted ? "play" : "pause"} size={13} />}
              {halted ? "恢复运行" : "急停"}
            </button>
          </div>
        </div>
      </header>
      {halted && (
        <div className="banner banner-attention global-banner" role="alert">
          <Icon name="pause" size={15} />
          <div>
            <strong>系统已急停。</strong>Agent 停止发起新的工具调用，进行中的事故停在当前步骤。
          </div>
        </div>
      )}
      {shellError && (
        <div className="banner banner-danger global-banner" role="alert">
          <Icon name="alert-circle" size={15} /><span>{shellError}</span>
          <button type="button" className="icon-btn" onClick={() => setShellError(null)} aria-label="关闭"><Icon name="x" size={13} /></button>
        </div>
      )}
      <main className="content">
        {match ? (
          <IncidentPage id={match[1]!} role={user.role} />
        ) : path === "/approvals" ? (
          <ApprovalsPage />
        ) : path === "/ops" ? (
          <OpsPage />
        ) : path === "/arena" ? (
          canDemo ? <ArenaPage /> : <NoAccess title="Arena" />
        ) : path === "/eval" ? (
          canDemo ? <EvalPage /> : <NoAccess title="评测" />
        ) : path === "/settings" ? (
          <SettingsPage me={user} />
        ) : (
          <IncidentList user={user} />
        )}
      </main>

      <ConfirmDialog
        open={confirmHalt}
        onClose={() => setConfirmHalt(false)}
        onConfirm={() => void setSystemHalt(true)}
        title="确认急停 Agent？"
        description="这是面向全系统的运行控制，不只是当前页面。"
        confirmLabel="确认急停"
        tone="danger"
        busy={haltBusy}
      >
        <div className="confirm-impact">
          <p className="confirm-impact-title"><Icon name="alert-triangle" size={15} /> 急停的影响范围</p>
          <ul>
            <li>所有 Agent 停止发起新的工具调用，事故停留在当前步骤。</li>
            <li>不会自动回滚已经产生的代码、分支或孪生环境。</li>
            <li>事故数据与已生成的报告保留，可在准备好后恢复运行。</li>
          </ul>
        </div>
      </ConfirmDialog>
    </div>
  );
}

/** 只读角色访问管理类页面时的占位，比直接抛 403 更容易理解。 */
function NoAccess({ title }: { title: string }) {
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-title">{title}</h1>
        </div>
      </header>
      <div className="empty-state">
        <span className="empty-icon"><Icon name="lock" size={20} /></span>
        <strong>当前账号无权访问</strong>
        <p>{title}只对管理员与维护者开放。需要时请联系管理员调整角色。</p>
      </div>
    </div>
  );
}

function NavLink({ to, icon, active, children }: {
  to: string; icon: IconName; active: boolean; children: React.ReactNode;
}) {
  return (
    <a
      href={to} className={active ? "nav-link active" : "nav-link"} aria-current={active ? "page" : undefined}
      onClick={(e) => { e.preventDefault(); navigate(to); }}
    >
      <Icon name={icon} size={15} />
      <span>{children}</span>
    </a>
  );
}

function UserMenu({ user, onLogout }: { user: AuthUser; onLogout: () => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const initial = user.name.slice(0, 1).toUpperCase();
  return (
    <div className="user-menu" ref={root}>
      <button type="button" className="user-trigger" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="avatar" aria-hidden>{initial}</span>
        <span className="user-copy">
          <span className="user-name">{user.name}</span>
          <span className="user-role">{ROLE_LABEL[user.role]}</span>
        </span>
        <Icon name="chevron-down" size={13} className="user-chevron" />
      </button>
      {open && (
        <div className="user-pop" role="menu">
          <div className="user-pop-head">
            <span className="avatar avatar-lg" aria-hidden>{initial}</span>
            <div>
              <strong>{user.name}</strong>
              <span>{ROLE_LABEL[user.role]} · {user.role}</span>
            </div>
          </div>
          <div className="user-pop-sep" />
          <button type="button" role="menuitem" className="user-pop-item" onClick={onLogout}>
            <Icon name="log-out" size={14} /> 退出登录
          </button>
        </div>
      )}
    </div>
  );
}