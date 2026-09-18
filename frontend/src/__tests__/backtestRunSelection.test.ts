import { describe, expect, it } from 'vitest';
import {
  areAllPageRunsSelected,
  emptyBacktestRunSelection,
  isBacktestRunSelected,
  selectAllFilteredBacktestRuns,
  selectedBacktestRunCount,
  toBacktestRunSelection,
  toggleBacktestRun,
  toggleBacktestRunPage,
} from '../utils/backtestRunSelection';


describe('backtest run selection', () => {
  it('preserves explicit selections from other pages when toggling a page', () => {
    let selection = toggleBacktestRun(emptyBacktestRunSelection(), 'run-page-1', true);
    selection = toggleBacktestRunPage(selection, ['run-page-2', 'run-page-3'], true);

    expect(selectedBacktestRunCount(selection)).toBe(3);
    expect(areAllPageRunsSelected(selection, ['run-page-2', 'run-page-3'])).toBe(true);

    selection = toggleBacktestRunPage(selection, ['run-page-2', 'run-page-3'], false);
    expect(toBacktestRunSelection(selection)).toEqual({
      mode: 'ids',
      runIds: ['run-page-1'],
    });
  });

  it('represents all filtered records with per-row exclusions', () => {
    let selection = selectAllFilteredBacktestRuns({
      query: ' 候选 ',
      symbol: '688981.SH',
      configId: 'config-1',
      favoritesOnly: true,
    }, 126);
    selection = toggleBacktestRun(selection, 'run-2', false);

    expect(selectedBacktestRunCount(selection)).toBe(125);
    expect(isBacktestRunSelected(selection, 'run-1')).toBe(true);
    expect(isBacktestRunSelected(selection, 'run-2')).toBe(false);
    expect(toBacktestRunSelection(selection)).toEqual({
      mode: 'filter',
      query: '候选',
      symbol: '688981.SH',
      configId: 'config-1',
      favoritesOnly: true,
      excludedRunIds: ['run-2'],
    });

    selection = toggleBacktestRun(selection, 'run-2', true);
    expect(selectedBacktestRunCount(selection)).toBe(126);
  });
});
