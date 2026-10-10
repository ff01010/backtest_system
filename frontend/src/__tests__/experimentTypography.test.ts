// @ts-expect-error Node 类型未作为前端构建依赖安装；该 API 仅在 Vitest 中读取样式源码。
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const globalCss = readFileSync('src/styles/global.css', 'utf8');
const comparisonCss = readFileSync('src/features/experiments/comparison.css', 'utf8');

function undersizedFontDeclarations(css: string): string[] {
  return [...css.matchAll(/font-size:\s*(\d+(?:\.\d+)?)px/g)]
    .filter((match) => Number(match[1]) < 12)
    .map((match) => match[0]);
}

describe('automation experiment typography', () => {
  const experimentCss = globalCss.slice(globalCss.indexOf('.experiment-page'));

  it('keeps functional text at twelve pixels or larger', () => {
    expect(globalCss).toMatch(/--font-sm:\s*12px/);
    expect(undersizedFontDeclarations(experimentCss)).toEqual([]);
    expect(undersizedFontDeclarations(comparisonCss)).toEqual([]);
  });

  it('does not use low-contrast or undefined text tokens in experiment styles', () => {
    expect(experimentCss).not.toContain('var(--text-subtle)');
    expect(experimentCss).not.toContain('var(--text-secondary)');
    expect(comparisonCss).not.toContain('var(--text-subtle)');
    expect(comparisonCss).not.toContain('var(--text-secondary)');
  });
});
