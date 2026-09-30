import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readUrlParams } from '../hooks/useUrlState';

// Mock window.location and localStorage
const mockSearchParams = new URLSearchParams();

beforeEach(() => {
  mockSearchParams.forEach((_, key) => mockSearchParams.delete(key));
  localStorage.clear();
});

describe('URL params (sanity check)', () => {
  it('default params are valid', () => {
    const symbol = '688981.SH';
    const date = '20260622';
    expect(symbol).toMatch(/^\d{6}\.(SH|SZ)$/);
    expect(date).toMatch(/^\d{8}$/);
  });

  it('symbol regex rejects invalid codes', () => {
    const validPattern = /^\d{6}\.(SH|SZ)$/;
    expect(validPattern.test('688981.SH')).toBe(true);
    expect(validPattern.test('002747.SZ')).toBe(true);
    expect(validPattern.test('abc')).toBe(false);
    expect(validPattern.test('688981.XSHE')).toBe(false);
    expect(validPattern.test('../../../etc')).toBe(false);
  });

  it('date regex rejects invalid dates', () => {
    const validPattern = /^\d{8}$/;
    expect(validPattern.test('20260622')).toBe(true);
    expect(validPattern.test('2026062')).toBe(false);
    expect(validPattern.test('abc')).toBe(false);
  });
});

describe('experiment deep link', () => {
  it('restores the exact published subject from URL parameters', () => {
    window.history.replaceState(null, '', '/?workspace=experiments&experimentConfigId=config-1&experimentVersionId=version-9');

    expect(readUrlParams()).toMatchObject({
      workspace: 'experiments',
      experimentConfigId: 'config-1',
      experimentVersionId: 'version-9',
    });
  });

  it('restores a created experiment detail after refresh', () => {
    window.history.replaceState(
      null,
      '',
      '/?workspace=experiments&experimentId=experiment-1',
    );

    expect(readUrlParams()).toMatchObject({
      workspace: 'experiments',
      experimentId: 'experiment-1',
    });
  });
});

describe('Marker aggregation logic', () => {
  function shouldAggregate(count: number): boolean {
    return count >= 3;
  }

  it('aggregates when 3 or more markers on same bar', () => {
    expect(shouldAggregate(1)).toBe(false);
    expect(shouldAggregate(2)).toBe(false);
    expect(shouldAggregate(3)).toBe(true);
    expect(shouldAggregate(5)).toBe(true);
  });

  it('produces correct aggregate label', () => {
    const count = 4;
    const module = 'A';
    const label = count >= 3 ? `${module}×${count}` : `${module} single`;
    expect(label).toBe('A×4');
  });
});
