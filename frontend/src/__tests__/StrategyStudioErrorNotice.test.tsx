import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StrategyStudioErrorNotice } from '../components/BacktestPage';

describe('StrategyStudioErrorNotice', () => {
  it('在代码区上方提供可换行的错误详情', () => {
    render(
      <StrategyStudioErrorNotice
        message="当前运行环境未提供模块 package_that_does_not_exist_123"
      />
    );

    expect(screen.getByRole('alert', { name: '策略操作错误' }).textContent).toContain(
      '当前运行环境未提供模块 package_that_does_not_exist_123'
    );
  });

  it('没有错误时不占用布局空间', () => {
    const { container } = render(<StrategyStudioErrorNotice message="" />);

    expect(container.innerHTML).toBe('');
  });
});
