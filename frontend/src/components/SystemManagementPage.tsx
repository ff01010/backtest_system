import { useState } from 'react';
import { Activity, Users } from 'lucide-react';
import SystemErrorLogPage from './SystemErrorLogPage';
import UserManagementPage from './UserManagementPage';

type SystemSection = 'monitoring' | 'users';

export default function SystemManagementPage() {
  const [section, setSection] = useState<SystemSection>('monitoring');
  const navigation = (
    <nav className="system-management-switcher" aria-label="系统管理页面">
      <button
        type="button"
        className={section === 'monitoring' ? 'is-active' : ''}
        aria-current={section === 'monitoring' ? 'page' : undefined}
        onClick={() => setSection('monitoring')}
      >
        <Activity size={15} aria-hidden="true" />
        回测监控
      </button>
      <button
        type="button"
        className={section === 'users' ? 'is-active' : ''}
        aria-current={section === 'users' ? 'page' : undefined}
        onClick={() => setSection('users')}
      >
        <Users size={15} aria-hidden="true" />
        人员与权限
      </button>
    </nav>
  );
  return (
    <section className="system-management-shell">
      <div className="system-management-content">
        {section === 'monitoring'
          ? <SystemErrorLogPage headerNavigation={navigation} />
          : <UserManagementPage headerNavigation={navigation} />}
      </div>
    </section>
  );
}
