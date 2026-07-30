import { useState } from 'react';
import { login } from '../api/client';
import type { SessionUser } from '../api/client';

interface Props {
  onLogin: (user: SessionUser) => void;
}

export default function LoginPage({ onLogin }: Props) {
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
    <main style={{
      minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 24,
      background: 'radial-gradient(circle at 12% 4%, #d5efe9 0, transparent 30%), radial-gradient(circle at 92% 100%, #e3edf7 0, transparent 34%), var(--canvas)',
    }}>
      <section style={{ width: 'min(440px, 100%)', padding: '42px 38px', borderRadius: 18,
        border: '1px solid var(--border)', background: 'rgba(255, 255, 255, .96)', boxShadow: '0 24px 65px rgba(31, 57, 83, .12)' }}>
        <div style={{ color: 'var(--accent)', fontSize: 12, letterSpacing: 2, fontWeight: 750 }}>QUANT WORKSPACE</div>
        <h1 style={{ margin: '10px 0 8px', fontSize: 28, color: 'var(--text)', fontFamily: 'ui-serif, "Songti SC", "Noto Serif SC", serif' }}>玄龄量化回测系统</h1>
        <p style={{ color: 'var(--text-muted)', marginBottom: 28 }}>请使用管理员创建的账户登录。</p>
        <form onSubmit={submit} style={{ display: 'grid', gap: 16 }}>
          <label style={{ color: 'var(--text-muted)', display: 'grid', gap: 7 }}>
            用户名
            <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required
              style={inputStyle} />
          </label>
          <label style={{ color: 'var(--text-muted)', display: 'grid', gap: 7 }}>
            密码
            <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" required
              style={inputStyle} />
          </label>
          {error && <div style={{ color: 'var(--danger)', background: 'var(--danger-soft)', padding: '9px 11px', borderRadius: 7 }}>{error}</div>}
          <button disabled={submitting} type="submit" style={{ height: 42, marginTop: 5, border: 0, borderRadius: 8,
            color: '#ffffff', background: submitting ? '#7caea7' : 'var(--accent)', fontWeight: 800, cursor: submitting ? 'wait' : 'pointer' }}>
            {submitting ? '正在验证…' : '登录系统'}
          </button>
        </form>
      </section>
    </main>
  );
}

const inputStyle = {
  height: 40, borderRadius: 7, border: '1px solid var(--border-strong)', padding: '0 11px',
  color: 'var(--text)', background: 'var(--surface-muted)', fontSize: 14,
};
