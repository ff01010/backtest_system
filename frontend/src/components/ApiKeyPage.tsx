import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createApiKey,
  fetchApiKeys,
  revokeApiKey,
  type ApiAccessKey,
} from '../api/client';

const DEFAULT_SCOPES: ApiAccessKey['scopes'] = [
  'backtest:submit',
  'backtest:read',
  'backtest:cancel',
  'strategy:read',
];

const SCOPE_LABELS: Record<ApiAccessKey['scopes'][number], string> = {
  'backtest:submit': '提交回测',
  'backtest:read': '查询结果',
  'backtest:cancel': '取消任务',
  'experiment:submit': '创建、重试与恢复实验',
  'experiment:read': '读取实验与 Trial',
  'experiment:cancel': '取消实验',
  'strategy:read': '读取策略与版本',
  'strategy:write': '创建、修改与恢复策略',
  'strategy:publish': '发布策略版本',
  'strategy:delete': '归档策略',
};

const SCOPE_GROUPS: Array<{ title: string; scopes: ApiAccessKey['scopes'] }> = [
  {
    title: '回测任务',
    scopes: ['backtest:submit', 'backtest:read', 'backtest:cancel'],
  },
  {
    title: '自动化实验',
    scopes: ['experiment:submit', 'experiment:read', 'experiment:cancel'],
  },
  {
    title: '策略管理',
    scopes: ['strategy:read', 'strategy:write', 'strategy:publish', 'strategy:delete'],
  },
];

function formatTime(value: string | null): string {
  return value ? value.replace('T', ' ').slice(0, 16) : '—';
}

export default function ApiKeyPage() {
  const [keys, setKeys] = useState<ApiAccessKey[]>([]);
  const [name, setName] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [scopes, setScopes] = useState<ApiAccessKey['scopes']>(DEFAULT_SCOPES);
  const [plainTextKey, setPlainTextKey] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const creatingRef = useRef(false);

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
    if (creatingRef.current) return;
    if (!name.trim() || scopes.length === 0) {
      setError('请填写名称并至少选择一项权限');
      return;
    }
    creatingRef.current = true;
    setCreating(true);
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
      creatingRef.current = false;
      setCreating(false);
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
    <main className="api-page">
      <section className="api-shell">
        <header className="api-header">
          <div>
            <div className="api-eyebrow">Access / Keys</div>
            <h1 className="api-title">个人 API Key</h1>
            <p className="api-subtitle">仅用于调用已保存并已发布的策略方案；密钥只在创建时显示一次。</p>
          </div>
          <span className="api-count">{keys.length} 个密钥</span>
        </header>

        {plainTextKey && (
          <section className="api-reveal" role="status">
            <div className="api-reveal-title">请立即复制并安全保存此 API Key</div>
            <code className="api-reveal-key">{plainTextKey}</code>
            <button type="button" className="api-reveal-dismiss" onClick={() => setPlainTextKey(null)}>
              我已保存，关闭提示
            </button>
          </section>
        )}

        {error && <div className="api-alert" role="alert">{error}</div>}

        <section className="api-card">
          <h2 className="api-card-title">创建访问密钥</h2>
          <div className="api-form-grid">
            <label className="api-field">
              名称
              <input
                className="api-input"
                value={name}
                onChange={(event) => setName(event.target.value)}
                maxLength={80}
                placeholder="例如：研究脚本"
              />
            </label>
            <label className="api-field">
              到期日期（可选）
              <input
                className="api-input"
                type="date"
                value={expiresAt}
                onChange={(event) => setExpiresAt(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="app-button is-primary"
              onClick={() => { void handleCreate(); }}
              disabled={creating}
            >
              {creating ? '处理中…' : '创建 Key'}
            </button>
          </div>
          <div className="api-scope-groups">
            {SCOPE_GROUPS.map((group) => (
              <div key={group.title} className="api-scope-group">
                <div className="api-scope-group-title">{group.title}</div>
                <div className="api-scope-list">
                  {group.scopes.map((scope) => (
                    <label
                      key={scope}
                      className={`api-scope${scopes.includes(scope) ? ' is-checked' : ''}`}
                    >
                      <input
                        type="checkbox"
                        checked={scopes.includes(scope)}
                        onChange={() => toggleScope(scope)}
                      />
                      {SCOPE_LABELS[scope]}
                    </label>
                  ))}
                </div>
              </div>
            ))}
            <p className="api-warning">
              实验创建、恢复、重试、取消以及策略写入会改变资源；这些高权限默认不勾选，请按调用方最小需要授权。
            </p>
          </div>
        </section>

        <section className="api-table">
          <div className="api-table-head">
            <span>名称 / 前缀</span><span>权限</span><span>最后使用</span><span>到期时间</span><span>操作</span>
          </div>
          {keys.length === 0 && !loading ? (
            <div className="api-empty">尚未创建 API Key</div>
          ) : keys.map((key) => (
            <div key={key.id} className="api-table-row">
              <div>
                <div className="api-key-name">{key.name}</div>
                <code className="api-key-prefix">{key.prefix}…</code>
              </div>
              <div className="api-key-scopes">
                {key.scopes.map((scope) => (
                  <span key={scope} className="api-scope-chip">{SCOPE_LABELS[scope]}</span>
                ))}
              </div>
              <span>{formatTime(key.lastUsedAt)}</span>
              <span className={key.revokedAt ? 'api-muted' : undefined}>
                {key.revokedAt ? '已撤销' : formatTime(key.expiresAt)}
              </span>
              <button
                type="button"
                className="app-button is-danger is-compact"
                disabled={loading || Boolean(key.revokedAt)}
                onClick={() => { void handleRevoke(key.id); }}
              >
                撤销
              </button>
            </div>
          ))}
        </section>
      </section>
    </main>
  );
}
