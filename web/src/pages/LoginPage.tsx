import { useState } from "react";
import { api, ApiError, setToken, type AuthUser } from "../api";
import { BrandMark, Icon } from "../components/Icons";
import { Spinner } from "../components/ui";

export function LoginPage({ onLogin }: { onLogin: (user: AuthUser) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(String(data.get("name") ?? ""), String(data.get("password") ?? ""));
      setToken(res.token);
      onLogin(res.user);
    } catch (err) {
      setError(loginError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <aside className="login-aside">
        <div className="login-brand">
          <BrandMark size={30} />
          <span>TwinFix</span>
        </div>
        <div className="login-pitch">
          <span className="login-eyebrow">AI INCIDENT RESPONSE CONSOLE</span>
          <h1>让每一次故障修复<br />都有证据、有边界。</h1>
          <p>从告警到诊断、复现、修复与验证，TwinFix 在隔离的孪生环境里完成整条闭环；生产环境始终由人来守住最后一道关。</p>
          <ul className="login-features">
            <li><Icon name="layers" size={16} /><span><strong>隔离孪生环境</strong>复现与验证不触碰生产服务</span></li>
            <li><Icon name="shield-check" size={16} /><span><strong>独立验证</strong>修复产物经过构建、复现与回归检查</span></li>
            <li><Icon name="clipboard-check" size={16} /><span><strong>审批与审计</strong>合并与转人工都有明确的人工决策记录</span></li>
          </ul>
        </div>
        <p className="login-foot">TwinFix · 故障诊断与修复控制台</p>
      </aside>

      <main className="login-main">
        <form className="login-card" onSubmit={submit}>
          <div className="login-mobile-brand"><BrandMark size={26} /><span>TwinFix</span></div>
          <div className="login-heading">
            <h2>登录控制台</h2>
            <p>使用管理员分配的账号继续。</p>
          </div>
          <label className="field">
            <span>用户名或邮箱</span>
            <input
              name="name" autoComplete="username" required autoFocus placeholder="例如 alice"
              aria-invalid={Boolean(error)} onChange={() => error && setError(null)}
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              name="password" type="password" autoComplete="current-password" required placeholder="输入密码"
              aria-invalid={Boolean(error)} onChange={() => error && setError(null)}
            />
          </label>
          {error && (
            <div className="banner banner-danger" role="alert">
              <Icon name="alert-circle" size={15} /><span>{error}</span>
            </div>
          )}
          <button className="btn btn-primary login-submit" disabled={busy}>
            {busy ? <Spinner /> : null}{busy ? "登录中…" : "登录"}
          </button>
          <div className="login-note">
            <Icon name="lock" size={13} />
            <span>账号由管理员通过 <code>twinfix-user create</code> 创建；本地开发环境可关闭鉴权。</span>
          </div>
        </form>
      </main>
    </div>
  );
}

/** 把后端英文细节换成可执行的提示；4xx 的校验信息仍原样展示。 */
function loginError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "用户名或密码不正确。";
    if (err.status === 429) return "尝试次数过多，请稍后再试。";
    if (err.status >= 500) return "服务暂时不可用，请稍后重试。";
    return err.message || "登录失败。";
  }
  return "无法连接服务，请检查网络后重试。";
}