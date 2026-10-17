import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { searchMarketInstruments } from '../api/client';
import InstrumentSearchField from '../components/InstrumentSearchField';
import type { MarketInstrument } from '../types';

vi.mock('../api/client', () => ({
  searchMarketInstruments: vi.fn(),
}));

const instruments: MarketInstrument[] = [
  { symbol: '600519.SH', name: '贵州茅台', market: 'SH', instrumentType: 'stock', board: 'main' },
  { symbol: '920161.BJ', name: '龙辰科技', market: 'BJ', instrumentType: 'stock', board: 'beijing' },
];

const searchMock = vi.mocked(searchMarketInstruments);

function matches(query: string): MarketInstrument[] {
  const keyword = query.trim().toUpperCase();
  return instruments.filter((item) => (
    item.symbol.includes(keyword) || item.name.includes(query.trim())
  ));
}

function PreflightHarness({ onPreflight }: { onPreflight: (symbol: string) => void }) {
  const [symbol, setSymbol] = useState('600519.SH');

  useEffect(() => {
    onPreflight(symbol);
  }, [onPreflight, symbol]);

  return <InstrumentSearchField label="运行标的" value={symbol} onChange={setSymbol} />;
}

function inputValue(): string {
  return (screen.getByRole('combobox') as HTMLInputElement).value;
}

describe('InstrumentSearchField', () => {
  beforeEach(() => {
    searchMock.mockReset();
    searchMock.mockImplementation(async (query) => matches(query));
  });

  it('keeps the restored history symbol and display name consistent', async () => {
    const onChange = vi.fn();
    const view = render(<InstrumentSearchField label="运行标的" value="600519.SH" onChange={onChange} />);
    await waitFor(() => expect(inputValue()).toBe('600519.SH · 贵州茅台'));

    view.rerender(<InstrumentSearchField label="运行标的" value="920161.BJ" onChange={onChange} />);

    await waitFor(() => expect(inputValue()).toBe('920161.BJ · 龙辰科技'));
    expect(onChange).not.toHaveBeenCalled();
  });

  it('requests preflight only after the user confirms an instrument', async () => {
    const onPreflight = vi.fn();
    render(<PreflightHarness onPreflight={onPreflight} />);
    await waitFor(() => expect(inputValue()).toBe('600519.SH · 贵州茅台'));
    onPreflight.mockClear();

    const input = screen.getByRole('combobox');
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '920161' } });
    const option = await screen.findByRole('option', { name: /920161\.BJ.*龙辰科技/ });

    expect(onPreflight).not.toHaveBeenCalled();
    fireEvent.mouseDown(option);

    await waitFor(() => expect(onPreflight).toHaveBeenCalledTimes(1));
    expect(onPreflight).toHaveBeenCalledWith('920161.BJ');
  });

  it('aborts the previous lookup when the value changes or the component unmounts', async () => {
    const signals: AbortSignal[] = [];
    searchMock.mockImplementation(async (query, options) => {
      const signal = options?.signal;
      if (signal) signals.push(signal);
      return matches(query);
    });
    const view = render(
      <InstrumentSearchField label="运行标的" value="600519.SH" onChange={vi.fn()} />,
    );
    await waitFor(() => expect(signals).toHaveLength(1));

    view.rerender(
      <InstrumentSearchField label="运行标的" value="920161.BJ" onChange={vi.fn()} />,
    );
    await waitFor(() => expect(signals).toHaveLength(2));
    expect(signals[0].aborted).toBe(true);

    view.unmount();
    expect(signals[1].aborted).toBe(true);
  });
});
