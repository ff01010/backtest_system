import type { LucideIcon } from 'lucide-react';

interface BacktestTab {
  id: string;
  label: string;
  icon: LucideIcon;
}

interface Props {
  activeTab: string;
  tabs: BacktestTab[];
  onChange: (tabId: string) => void;
}

export default function BacktestTabs({ activeTab, tabs, onChange }: Props) {
  return (
    <nav
      aria-label="回测结果导航"
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        padding: 4,
        border: '1px solid #d7dde8',
        borderRadius: 8,
        background: '#f8fafc',
        overflowX: 'auto',
      }}
    >
      {tabs.map((tab) => {
        const Icon = tab.icon;
        const active = tab.id === activeTab;
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            style={{
              display: 'inline-flex',
              flex: '0 0 auto',
              alignItems: 'center',
              gap: 6,
              minHeight: 32,
              padding: '0 10px',
              border: `1px solid ${active ? '#0f766e' : 'transparent'}`,
              borderRadius: 5,
              background: active ? '#ccfbf1' : 'transparent',
              color: active ? '#0f766e' : '#64748b',
              cursor: 'pointer',
              fontSize: 13,
              fontWeight: active ? 700 : 500,
              whiteSpace: 'nowrap',
            }}
          >
            <Icon size={15} />
            {tab.label}
          </button>
        );
      })}
    </nav>
  );
}
