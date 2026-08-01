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
      aria-label="回测工作台导航"
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'stretch',
        gap: 3,
        padding: 8,
        background: '#f8fafc',
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
            aria-label={tab.label}
            title={tab.label}
            style={{
              display: 'flex',
              width: '100%',
              justifyContent: 'center',
              alignItems: 'center',
              minHeight: 36,
              padding: 0,
              border: 'none',
              borderLeft: `3px solid ${active ? 'var(--accent)' : 'transparent'}`,
              borderRadius: 4,
              background: active ? 'var(--surface)' : 'transparent',
              color: active ? 'var(--text)' : 'var(--text-muted)',
              cursor: 'pointer',
              fontSize: 13,
              fontWeight: active ? 700 : 500,
              whiteSpace: 'nowrap',
            }}
          >
            <Icon size={15} />
          </button>
        );
      })}
    </nav>
  );
}
