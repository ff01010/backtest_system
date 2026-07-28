import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { DisplayModule, DisplaySide } from '../types';
import type { ViewerPrefs } from '../hooks/useUrlState';
import { typography } from '../styles/typography';

interface Props {
  modules: DisplayModule[];
  sides: DisplaySide[];
  prefs: ViewerPrefs;
  onModulesChange: (modules: DisplayModule[]) => void;
  onSidesChange: (sides: DisplaySide[]) => void;
  onPrefsChange: (prefs: ViewerPrefs) => void;
}

export default function Sidebar({
  modules, sides, prefs, onModulesChange, onSidesChange, onPrefsChange,
}: Props) {
  const open = prefs.sidebarOpen;

  const toggleSidebar = () => {
    onPrefsChange({ ...prefs, sidebarOpen: !open });
  };

  const toggleModule = (m: DisplayModule) => {
    if (modules.includes(m)) {
      if (modules.length > 1) onModulesChange(modules.filter((x) => x !== m));
    } else {
      onModulesChange([...modules, m]);
    }
  };

  const toggleSide = (s: DisplaySide) => {
    if (sides.includes(s)) {
      if (sides.length > 1) onSidesChange(sides.filter((x) => x !== s));
    } else {
      onSidesChange([...sides, s]);
    }
  };

  const style = {
    container: {
      background: '#16213e', borderRight: '1px solid #2a2a4a',
      padding: open ? '12px' : '12px 6px',
      width: open ? '200px' : '44px',
      transition: 'width 0.2s',
      overflow: 'hidden' as const,
      flexShrink: 0,
      display: 'flex', flexDirection: 'column' as const, gap: '16px',
    },
    section: { display: 'flex', flexDirection: 'column' as const, gap: '6px' },
    label: { color: '#667', fontSize: typography.sm, textTransform: 'uppercase' as const, letterSpacing: '1px', marginBottom: '2px' },
    checkboxRow: {
      display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer',
      color: '#8892b0', fontSize: typography.base, opacity: open ? 1 : 0,
      transition: 'opacity 0.15s', whiteSpace: 'nowrap' as const,
    },
    toggleBtn: {
      background: 'none', border: 'none', color: '#667', cursor: 'pointer',
      padding: '2px', alignSelf: 'flex-end' as const,
    },
  };

  const A_COLOR = '#FF6B6B';
  const B_COLOR = '#00D2D3';

  return (
    <div style={style.container}>
      <button onClick={toggleSidebar} style={style.toggleBtn} title={open ? '折叠' : '展开'}>
        {open ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
      </button>

      {open && (
        <>
          {/* Module toggle */}
          <div style={style.section}>
            <div style={style.label}>模块</div>
            <label style={style.checkboxRow} onClick={() => toggleModule('A')}>
              <input type="checkbox" checked={modules.includes('A')} onChange={() => {}} />
              <span style={{ color: A_COLOR, fontWeight: 'bold' }}>模块 A</span>
            </label>
            <label style={style.checkboxRow} onClick={() => toggleModule('B')}>
              <input type="checkbox" checked={modules.includes('B')} onChange={() => {}} />
              <span style={{ color: B_COLOR, fontWeight: 'bold' }}>模块 B</span>
            </label>
          </div>

          {/* Side toggle */}
          <div style={style.section}>
            <div style={style.label}>方向</div>
            <label style={style.checkboxRow} onClick={() => toggleSide('BUY')}>
              <input type="checkbox" checked={sides.includes('BUY')} onChange={() => {}} />
              <span>▲ 买入</span>
            </label>
            <label style={style.checkboxRow} onClick={() => toggleSide('SELL')}>
              <input type="checkbox" checked={sides.includes('SELL')} onChange={() => {}} />
              <span>▼ 卖出</span>
            </label>
          </div>

          {/* Display options */}
          <div style={style.section}>
            <div style={style.label}>显示</div>
            <label style={style.checkboxRow}>
              <input
                type="checkbox" checked={prefs.showPrices}
                onChange={() => onPrefsChange({ ...prefs, showPrices: !prefs.showPrices })}
              />
              <span>价格标签</span>
            </label>
          </div>

          {/* Marker size */}
          <div style={style.section}>
            <div style={style.label}>Marker 大小</div>
            <select
              value={prefs.markerSize}
              onChange={(e) => onPrefsChange({ ...prefs, markerSize: e.target.value as ViewerPrefs['markerSize'] })}
              style={{
                background: '#1a1a2e', color: '#e0e0e0', border: '1px solid #3a3a5a',
                borderRadius: '4px', padding: '4px 8px', fontSize: '13px', width: '100%',
              }}
            >
              <option value="small">小</option>
              <option value="normal">中</option>
              <option value="large">大</option>
            </select>
          </div>

          {/* Auto refresh */}
          <div style={style.section}>
            <div style={style.label}>自动刷新</div>
            <select
              value={prefs.autoRefresh}
              onChange={(e) => onPrefsChange({ ...prefs, autoRefresh: e.target.value as ViewerPrefs['autoRefresh'] })}
              style={{
                background: '#1a1a2e', color: '#e0e0e0', border: '1px solid #3a3a5a',
                borderRadius: '4px', padding: '4px 8px', fontSize: '13px', width: '100%',
              }}
            >
              <option value="off">关闭</option>
              <option value="30s">30 秒</option>
              <option value="60s">60 秒</option>
            </select>
          </div>

          {/* Comparison mode */}
          <div style={style.section}>
            <div style={style.label}>视图</div>
            <label style={style.checkboxRow}>
              <input
                type="checkbox" checked={prefs.comparisonMode}
                onChange={() => onPrefsChange({ ...prefs, comparisonMode: !prefs.comparisonMode })}
              />
              <span>对比模式 A|B</span>
            </label>
          </div>
        </>
      )}
    </div>
  );
}
