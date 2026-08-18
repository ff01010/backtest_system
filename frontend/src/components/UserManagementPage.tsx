import { useEffect, useState } from 'react';
import { createUser, deleteUser, fetchUserPage, resetUserPassword, updateUser } from '../api/client';
import type { ManagedUser } from '../api/client';

const PAGE_SIZE = 20;

export default function UserManagementPage() {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState({ username: '', displayName: '', password: '', role: 'user' });
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  const refresh = async (requestedPage = page) => {
    try {
      setLoading(true);
      const result = await fetchUserPage(requestedPage, PAGE_SIZE);
      setUsers(result.users);
      setPage(result.page);
      setTotal(result.total);
      setError('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取人员列表');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  const addUser = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      await createUser(draft);
      setDraft({ username: '', displayName: '', password: '', role: 'user' });
      await refresh(1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '创建失败');
    }
  };

  const saveUser = async (user: ManagedUser, status: string, role: string) => {
    try {
      await updateUser(user.id, { displayName: user.displayName, role, status });
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败');
    }
  };

  const resetPassword = async (user: ManagedUser) => {
    const password = window.prompt(`为 ${user.username} 设置新密码（至少 6 位）`);
    if (!password) return;
    try {
      await resetUserPassword(user.id, password);
      window.alert('密码已重置，该用户的现有会话已失效。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '重置失败');
    }
  };

  const removeUser = async (user: ManagedUser) => {
    if (!window.confirm(`确认删除用户“${user.username}”吗？\n存在历史回测记录时将改为禁用，以保留历史归属。`)) return;
    try {
      const result = await deleteUser(user.id);
      await refresh();
      window.alert(result.message);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败');
    }
  };

  return (
    <section style={{ padding: 28, overflow: 'auto', height: '100%', background: 'var(--canvas)' }}>
      <div style={{ maxWidth: 1100, margin: '0 auto' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'end', marginBottom: 20 }}>
          <div><div style={{ color: 'var(--accent)', fontSize: 12, letterSpacing: 1.5, fontWeight: 750 }}>ACCESS CONTROL</div><h2 style={{ marginTop: 5, color: 'var(--text)' }}>人员管理</h2></div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span style={{ color: 'var(--text-muted)' }}>仅管理员可创建、禁用、重置及删除账号</span>          </div>
        </div>
        <form onSubmit={addUser} style={panelStyle}>
          <strong style={{ color: 'var(--text)' }}>创建人员</strong>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 120px auto', gap: 10, marginTop: 12 }}>
            <input placeholder="用户名" value={draft.username} required style={inputStyle} onChange={(e) => setDraft({ ...draft, username: e.target.value })} />
            <input placeholder="姓名" value={draft.displayName} required style={inputStyle} onChange={(e) => setDraft({ ...draft, displayName: e.target.value })} />
            <input placeholder="初始密码（≥6位）" type="password" value={draft.password} required minLength={6} style={inputStyle} onChange={(e) => setDraft({ ...draft, password: e.target.value })} />
            <select value={draft.role} style={inputStyle} onChange={(e) => setDraft({ ...draft, role: e.target.value })}><option value="user">普通用户</option><option value="admin">管理员</option></select>
            <button type="submit" style={buttonStyle}>创建</button>
          </div>
        </form>
        {error && <div style={{ margin: '14px 0', color: 'var(--danger)' }}>{error}</div>}
        <div style={{ ...panelStyle, padding: 0, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', color: 'var(--text)' }}><thead><tr style={{ color: 'var(--text-muted)', background: 'var(--surface-muted)' }}>
            <th style={cellStyle}>用户名</th><th style={cellStyle}>姓名</th><th style={cellStyle}>角色</th><th style={cellStyle}>状态</th><th style={cellStyle}>最近登录</th><th style={cellStyle}>操作</th>
          </tr></thead><tbody>{users.map((user) => <tr key={user.id} style={{ borderTop: '1px solid var(--border)' }}>
            <td style={cellStyle}>{user.username}</td><td style={cellStyle}>{user.displayName}</td>
            <td style={cellStyle}><select defaultValue={user.role} style={compactInput} onChange={(e) => void saveUser(user, user.status, e.target.value)}><option value="user">普通用户</option><option value="admin">管理员</option></select></td>
            <td style={cellStyle}><button onClick={() => void saveUser(user, user.status === 'active' ? 'disabled' : 'active', user.role)} style={{ ...statusButton, color: user.status === 'active' ? 'var(--success)' : 'var(--warning)' }}>{user.status === 'active' ? '启用中' : '已禁用'}</button></td>
            <td style={cellStyle}>{user.lastLoginAt || '从未登录'}</td><td style={cellStyle}>
              <button onClick={() => void resetPassword(user)} style={linkButton}>重置密码</button>
              <button onClick={() => void removeUser(user)} style={{ ...linkButton, marginLeft: 10, color: 'var(--danger)' }}>删除</button>
            </td>
          </tr>)}</tbody></table>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 14, color: 'var(--text-muted)', fontSize: 13 }}>
          <span>共 {total} 名人员 · 第 {page} / {Math.max(1, Math.ceil(total / PAGE_SIZE))} 页</span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="button" disabled={loading || page <= 1} onClick={() => void refresh(page - 1)} style={pageButtonStyle}>上一页</button>
            <button type="button" disabled={loading || page * PAGE_SIZE >= total} onClick={() => void refresh(page + 1)} style={pageButtonStyle}>下一页</button>
          </div>
        </div>
      </div>
    </section>
  );
}

const panelStyle = { background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 10, padding: 16, boxShadow: '0 4px 18px rgba(26, 53, 77, .04)' };
const inputStyle = { height: 36, borderRadius: 6, border: '1px solid var(--border-strong)', background: 'var(--surface-muted)', color: 'var(--text)', padding: '0 9px' };
const compactInput = { ...inputStyle, height: 31 };
const buttonStyle = { border: 0, borderRadius: 6, padding: '0 16px', background: 'var(--accent)', color: '#ffffff', fontWeight: 700, cursor: 'pointer' };
const statusButton = { background: 'transparent', border: '1px solid var(--border-strong)', borderRadius: 5, padding: '5px 9px', cursor: 'pointer' };
const linkButton = { background: 'transparent', border: 0, color: 'var(--accent)', cursor: 'pointer' };
const pageButtonStyle = { border: '1px solid var(--border-strong)', borderRadius: 5, padding: '5px 10px', background: 'var(--surface)', color: 'var(--text)', cursor: 'pointer' };
const cellStyle = { padding: '12px 14px', textAlign: 'left' as const, fontWeight: 'normal' };
