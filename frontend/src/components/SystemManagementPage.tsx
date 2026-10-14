import { useState } from 'react';
import SystemErrorLogPage from './SystemErrorLogPage';
import UserManagementPage from './UserManagementPage';

type SystemSection = 'monitoring' | 'users';

export default function SystemManagementPage() {
  const [section, setSection] = useState<SystemSection>('monitoring');
  return (
    <section className="system-management-shell">
      <nav className="system-management-nav" aria-label="系统管理">
        <div>
          <span>系统管理</span>
          <strong>运行观测与访问控制</strong>
        </div>
        <button
          type="button"
          className={section === 'monitoring' ? 'is-active' : ''}
          aria-current={section === 'monitoring' ? 'page' : undefined}
          onClick={() => setSection('monitoring')}
        >
          回测监控
        </button>
        <button
          type="button"
          className={section === 'users' ? 'is-active' : ''}
          aria-current={section === 'users' ? 'page' : undefined}
          onClick={() => setSection('users')}
        >
          人员与权限
        </button>
      </nav>
      <div className="system-management-content">
        {section === 'monitoring' ? <SystemErrorLogPage /> : <UserManagementPage />}
      </div>
    </section>
  );
}
