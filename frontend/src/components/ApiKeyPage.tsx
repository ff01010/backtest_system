import { useCallback, useEffect, useState } from 'react';
import {
  createApiKey,
  fetchApiKeys,
  revokeApiKey,
  type ApiAccessKey,
} from '../api/client';

const ALL_SCOPES: ApiAccessKey['scopes'] = [
  'backtest:submit',
  'backtest:read',
  'backtest:cancel',
];

const SCOPE_LABELS: Record<ApiAccessKey['scopes'][number], string> = {
  'backtest:submit': '提交回测',
  'backtest:read': '查询结果',
  'backtest:cancel': '取消任务',
};

function formatTime(value: string | null): string {
  return value ? value.replace('T', ' ').slice(0, 16) : '—';
}

export default function ApiKeyPage() {
  const [keys, setKeys] = useState<ApiAccessKey[]>([]);
  const [name, setName] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [scopes, setScopes] = useState<ApiAccessKey['scopes']>(ALL_SCOPES);
  const [plainTextKey, setPlainTextKey] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setKeys(await fetchApiKeys());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '读取 API Key 失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggleScope = (scope: ApiAccessKey['scopes'][number]) => {
    setScopes((current) => current.includes(scope)
      ? current.filter((item) => item !== scope)
      : [...current, scope]);
  };

  const handleCreate = async () => {
    if (!name.trim() || scopes.length === 0) {
      setError('请填写名称并至少选择一项权限');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const created = await createApiKey({
        name: name.trim(),
        scopes,
        expiresAt: expiresAt ? `${expiresAt}T23:59:59+08:00` : null,
      });
      setPlainTextKey(created.plainTextKey);
      setName('');
      setExpiresAt('');
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建 API Key 失败');
    } finally {
      setLoading(false);
    }
  };

  const handleRevoke = async (keyId: string) => {
    setLoading(true);
    setError('');
    try {
      await revokeApiKey(keyId);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '撤销 API Key 失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <main style={{ flex: 1, overflow: 'auto', padding: '28px clamp(20px, 4vw, 56px)', background: '#f4f7fa' }}>
      <section style={{ maxWidth: 1080, margin: '0 auto' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 20, alignItems: 'end', marginBottom: 22 }}>
          <div>
            <div style={{ color: '#0f172a', fontSize: 24, fontWeight: 760, letterSpacing: '-0.02em' }}>个人 API Key</div>
            <p style={{ color: '#64748b', margin: '7px 0 0', fontSize: 13 }}>
              仅用于调用已保存并已发布的策略方案；密钥只在创建时显示一次。
            </p>
          </div>
          <span style={{ color: '#64748b', fontSize: 12, fontVariantNumeric: 'tabular-nums' }}>{keys.length} 个密钥</span>
        </div>

        {plainTextKey && (
          <section style={{ marginBottom: 18, border: '1px solid #f59e0b', borderLeft: '4px solid #d97706', background: '#fffbeb', padding: 16 }}>
            <div style={{ color: '#92400e', fontWeight: 750, fontSize: 14 }}>请立即复制并安全保存此 API Key</div>
            <code style={{ display: 'block', marginTop: 10, padding: 11, overflowWrap: 'anywhere', background: '#fff', border: '1px solid #fde68a', color: '#1f2937', fontSize: 12 }}>{plainTextKey}</code>
            <button type="button" onClick={() => setPlainTextKey(null)} style={{ marginTop: 10, border: 0, background: 'transparent', color: '#92400e', cursor: 'pointer', padding: 0, fontSize: 12 }}>我已保存，关闭提示</button>
          </section>
        )}

        {error && <div style={{ marginBottom: 16, padding: '10px 12px', background: '#fef2f2', border: '1px solid #fecaca', color: '#b91c1c', fontSize: 13 }}>{error}</div>}

        <section style={{ background: '#fff', border: '1px solid #dbe3eb', boxShadow: '0 1px 2px rgba(15, 23, 42, 0.04)', padding: 18, marginBottom: 18 }}>
          <div style={{ color: '#334155', fontSize: 13, fontWeight: 760, marginBottom: 14 }}>创建访问密钥</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(220px, 1.3fr) minmax(170px, 0.7fr) auto', gap: 12, alignItems: 'end' }}>
            <label style={{ display: 'grid', gap: 6, color: '#64748b', fontSize: 12 }}>
              名称
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} placeholder="例如：研究脚本" style={inputStyle} />
            </label>
            <label style={{ display: 'grid', gap: 6, color: '#64748b', fontSize: 12 }}>
              到期日期（可选）
              <input type="date" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} style={inputStyle} />
            </label>
            <button type="button" onClick={() => { void handleCreate(); }} disabled={loading} style={primaryButtonStyle}>{loading ? '处理中…' : '创建 Key'}</button>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 9, marginTop: 15 }}>
            {ALL_SCOPES.map((scope) => (
              <label key={scope} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 9px', border: '1px solid #dbe3eb', background: scopes.includes(scope) ? '#eff6ff' : '#fff', color: '#334155', fontSize: 12, cursor: 'pointer' }}>
                <input type="checkbox" checked={scopes.includes(scope)} onChange={() => toggleScope(scope)} />
                {SCOPE_LABELS[scope]}
              </label>
            ))}
          </div>
        </section>

        <section style={{ background: '#fff', border: '1px solid #dbe3eb', overflow: 'hidden' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 1.15fr 0.9fr 0.9fr 90px', gap: 14, padding: '11px 16px', background: '#f8fafc', borderBottom: '1px solid #dbe3eb', color: '#64748b', fontSize: 11, fontWeight: 720, letterSpacing: '.04em' }}>
            <span>名称 / 前缀</span><span>权限</span><span>最后使用</span><span>到期时间</span><span>操作</span>
          </div>
          {keys.length === 0 && !loading ? (
            <div style={{ padding: '40px 16px', textAlign: 'center', color: '#94a3b8', fontSize: 13 }}>尚未创建 API Key</div>
          ) : keys.map((key) => (
            <div key={key.id} style={{ display: 'grid', gridTemplateColumns: '1.1fr 1.15fr 0.9fr 0.9fr 90px', gap: 14, alignItems: 'center', padding: '13px 16px', borderBottom: '1px solid #edf2f7', color: '#334155', fontSize: 12 }}>
              <div><div style={{ color: '#0f172a', fontWeight: 700 }}>{key.name}</div><code style={{ color: '#64748b', fontSize: 11 }}>{key.prefix}…</code></div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>{key.scopes.map((scope) => <span key={scope} style={scopeTagStyle}>{SCOPE_LABELS[scope]}</span>)}</div>
              <span>{formatTime(key.lastUsedAt)}</span>
              <span style={{ color: key.revokedAt ? '#94a3b8' : '#334155' }}>{key.revokedAt ? '已撤销' : formatTime(key.expiresAt)}</span>
              <button type="button" disabled={loading || Boolean(key.revokedAt)} onClick={() => { void handleRevoke(key.id); }} style={dangerButtonStyle}>撤销</button>
            </div>
          ))}
        </section>
      </section>
    </main>
  );
}

const inputStyle = {
  height: 34,
  border: '1px solid #cbd5e1',
  borderRadius: 3,
  padding: '0 9px',
  color: '#0f172a',
  background: '#fff',
  outline: 'none',
};

const primaryButtonStyle = {
  height: 34,
  border: '1px solid #075985',
  borderRadius: 3,
  padding: '0 14px',
  background: '#0f766e',
  color: '#fff',
  fontSize: 12,
  fontWeight: 700,
  cursor: 'pointer',
};

const dangerButtonStyle = {
  border: '1px solid #fecaca',
  borderRadius: 3,
  padding: '5px 9px',
  background: '#fff',
  color: '#b91c1c',
  fontSize: 12,
  cursor: 'pointer',
};

const scopeTagStyle = {
  padding: '2px 5px',
  borderRadius: 2,
  background: '#f1f5f9',
  color: '#475569',
  fontSize: 11,
};
