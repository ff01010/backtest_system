import { useEffect, useState } from 'react';
import { createUser, fetchUsers, resetUserPassword, updateUser } from '../api/client';
import type { ManagedUser } from '../api/client';

export default function UserManagementPage() {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState({ username: '', displayName: '', password: '', role: 'user' });

  const refresh = async () => {
    try {
      setUsers(await fetchUsers());
      setError('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取人员列表');
    }
  };

  useEffect(() => { void refresh(); }, []);

  const addUser = async (event: React.FormEvent) => {
    event.preventDefault();
    try {
      await createUser(draft);
      setDraft({ username: '', displayName: '', password: '', role: 'user' });
      await refresh();
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

  return (
    <section style={{ padding: 24, overflow: 'auto', height: '100%', background: '#0f1726' }}>
      <div style={{ maxWidth: 1100, margin: '0 auto' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'end', marginBottom: 20 }}>
          <div><div style={{ color: '#63e3d5', fontSize: 12, letterSpacing: 1.5 }}>ACCESS CONTROL</div><h2 style={{ marginTop: 5 }}>人员管理</h2></div>
          <div style={{ color: '#92a7bb' }}>仅管理员可创建、禁用及重置账号密码</div>
        </div>
        <form onSubmit={addUser} style={panelStyle}>
          <strong style={{ color: '#e9fbf8' }}>创建人员</strong>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 120px auto', gap: 10, marginTop: 12 }}>
            <input placeholder="用户名" value={draft.username} required style={inputStyle} onChange={(e) => setDraft({ ...draft, username: e.target.value })} />
            <input placeholder="姓名" value={draft.displayName} required style={inputStyle} onChange={(e) => setDraft({ ...draft, displayName: e.target.value })} />
            <input placeholder="初始密码（≥6位）" type="password" value={draft.password} required minLength={6} style={inputStyle} onChange={(e) => setDraft({ ...draft, password: e.target.value })} />
            <select value={draft.role} style={inputStyle} onChange={(e) => setDraft({ ...draft, role: e.target.value })}><option value="user">普通用户</option><option value="admin">管理员</option></select>
            <button type="submit" style={buttonStyle}>创建</button>
          </div>
        </form>
        {error && <div style={{ margin: '14px 0', color: '#ffb4aa' }}>{error}</div>}
        <div style={{ ...panelStyle, padding: 0, overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', color: '#d6e2e8' }}><thead><tr style={{ color: '#82a1b4', background: '#101d2e' }}>
            <th style={cellStyle}>用户名</th><th style={cellStyle}>姓名</th><th style={cellStyle}>角色</th><th style={cellStyle}>状态</th><th style={cellStyle}>最近登录</th><th style={cellStyle}>操作</th>
          </tr></thead><tbody>{users.map((user) => <tr key={user.id} style={{ borderTop: '1px solid #24374a' }}>
            <td style={cellStyle}>{user.username}</td><td style={cellStyle}>{user.displayName}</td>
            <td style={cellStyle}><select defaultValue={user.role} style={compactInput} onChange={(e) => void saveUser(user, user.status, e.target.value)}><option value="user">普通用户</option><option value="admin">管理员</option></select></td>
            <td style={cellStyle}><button onClick={() => void saveUser(user, user.status === 'active' ? 'disabled' : 'active', user.role)} style={{ ...statusButton, color: user.status === 'active' ? '#70e1cd' : '#f5b86a' }}>{user.status === 'active' ? '启用中' : '已禁用'}</button></td>
            <td style={cellStyle}>{user.lastLoginAt || '从未登录'}</td><td style={cellStyle}><button onClick={() => void resetPassword(user)} style={linkButton}>重置密码</button></td>
          </tr>)}</tbody></table>
        </div>
      </div>
    </section>
  );
}

const panelStyle = { background: '#142238', border: '1px solid #29445a', borderRadius: 10, padding: 16 };
const inputStyle = { height: 36, borderRadius: 6, border: '1px solid #38526b', background: '#0c1727', color: '#e8f5f6', padding: '0 9px' };
const compactInput = { ...inputStyle, height: 31 };
const buttonStyle = { border: 0, borderRadius: 6, padding: '0 16px', background: '#62e4d2', color: '#09211f', fontWeight: 700, cursor: 'pointer' };
const statusButton = { background: 'transparent', border: '1px solid #38526b', borderRadius: 5, padding: '5px 9px', cursor: 'pointer' };
const linkButton = { background: 'transparent', border: 0, color: '#6ee5d2', cursor: 'pointer' };
const cellStyle = { padding: '12px 14px', textAlign: 'left' as const, fontWeight: 'normal' };
