import { useState } from 'react';
import { login } from '../api/client';
import type { SessionUser } from '../api/client';
import BrandMark from './BrandMark';

interface Props {
  onLogin: (user: SessionUser) => void;
  notice?: string;
}

export default function LoginPage({ onLogin, notice = '' }: Props) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    try {
      onLogin(await login(username, password));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message.replace(/^API \d+: /, '') : '登录失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="login-shell">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <BrandMark size={34} />
          <span className="login-brand-copy">
            <span className="login-brand-eyebrow">Qingheng Quant</span>
            <span className="login-brand-name">青衡量化</span>
          </span>
        </div>
        <h1 className="login-title" id="login-title" aria-label="青衡量化回测系统">
          <span className="login-title-brand">青衡</span>
          <span className="login-title-accent">量化回测系统</span>
        </h1>
        <p className="login-subtitle">策略研究、回测与验证的统一工作台。请使用管理员创建的账户登录。</p>
        {notice && <div role="status" className="login-notice">{notice}</div>}
        <form className="login-form" onSubmit={submit}>
          <label className="login-field">
            用户名
            <input
              className="login-input"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              required
            />
          </label>
          <label className="login-field">
            密码
            <input
              className="login-input"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              type="password"
              autoComplete="current-password"
              required
            />
          </label>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button className="login-submit" disabled={submitting} type="submit">
            {submitting ? '正在验证…' : '登录系统'}
          </button>
        </form>
        <div className="login-footnote">
          <span>受控访问 · 账户由管理员统一创建</span>
          <code>QINGHENG / 01</code>
        </div>
      </section>
    </main>
  );
}
