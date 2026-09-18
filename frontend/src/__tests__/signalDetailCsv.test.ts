// @ts-expect-error Node 类型未作为前端构建依赖安装；该 API 仅在 Vitest 中读取共享样例。
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { BacktestTrade } from '../types';
import { signalDetailCsvText } from '../utils/signalDetailCsv';


interface CsvCase {
  name: string;
  requestedParams: { strategyCategory: string };
  result: {
    strategyCategory: string;
    trades: BacktestTrade[];
    skippedSignals: BacktestTrade[];
  };
  expectedBase64: string;
}

const cases = JSON.parse(readFileSync(
  new URL(
    ['..', '..', '..', 'shared', 'fixtures', 'signal_detail_csv_cases.json'].join('/'),
    import.meta.url,
  ),
  'utf-8',
)) as CsvCase[];

describe('signal detail CSV contract', () => {
  for (const item of cases) {
    it(`matches the shared golden bytes for ${item.name}`, () => {
      const trades = [...item.result.trades, ...item.result.skippedSignals]
        .sort((left, right) => String(left.signalTime || left.time)
          .localeCompare(String(right.signalTime || right.time)));
      const csv = signalDetailCsvText(
        trades,
        item.requestedParams.strategyCategory === 'core_position_overlay',
      );
      const actual = Array.from(new TextEncoder().encode(csv));
      const expected = Array.from(
        atob(item.expectedBase64),
        (character) => character.charCodeAt(0),
      );

      expect(actual).toEqual(expected);
    });
  }
});
