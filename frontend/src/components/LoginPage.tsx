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
      background: 'radial-gradient(circle at 18% 10%, #173c55 0, transparent 34%), #090f1d',
    }}>
      <section style={{ width: 'min(440px, 100%)', padding: '42px 38px', borderRadius: 18,
        border: '1px solid #29445b', background: 'rgba(13, 24, 40, .94)', boxShadow: '0 28px 80px rgba(0,0,0,.42)' }}>
        <div style={{ color: '#63e3d5', fontSize: 12, letterSpacing: 2, fontWeight: 700 }}>QUANT WORKSPACE</div>
        <h1 style={{ margin: '10px 0 8px', fontSize: 28, color: '#effaf8' }}>玄龄量化回测系统</h1>
        <p style={{ color: '#8da3b8', marginBottom: 28 }}>请使用管理员创建的账户登录。</p>
        <form onSubmit={submit} style={{ display: 'grid', gap: 16 }}>
          <label style={{ color: '#b9c7d5', display: 'grid', gap: 7 }}>
            用户名
            <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required
              style={inputStyle} />
          </label>
          <label style={{ color: '#b9c7d5', display: 'grid', gap: 7 }}>
            密码
            <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" required
              style={inputStyle} />
          </label>
          {error && <div style={{ color: '#ffaaa5', background: '#441f2a', padding: '9px 11px', borderRadius: 7 }}>{error}</div>}
          <button disabled={submitting} type="submit" style={{ height: 42, marginTop: 5, border: 0, borderRadius: 8,
            color: '#06231f', background: submitting ? '#5d968e' : '#5ee6d4', fontWeight: 800, cursor: submitting ? 'wait' : 'pointer' }}>
            {submitting ? '正在验证…' : '登录系统'}
          </button>
        </form>
      </section>
    </main>
  );
}

const inputStyle = {
  height: 40, borderRadius: 7, border: '1px solid #38516a', padding: '0 11px',
  color: '#eef7f7', background: '#0a1424', fontSize: 14,
};
