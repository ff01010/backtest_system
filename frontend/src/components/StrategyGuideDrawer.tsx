import { Check, ChevronRight, Clipboard, FileText, Link2, LoaderCircle, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { fetchStrategyGuide } from '../api/client';

interface Props {
  open: boolean;
  onClose: () => void;
  onUseExample: (code: string) => void;
}

type GuideBlock =
  | { type: 'heading'; level: number; text: string; id: string }
  | { type: 'paragraph'; text: string }
  | { type: 'quote'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'code'; code: string }
  | { type: 'table'; rows: string[][] };

function guideId(index: number): string {
  return `strategy-guide-section-${index}`;
}

function splitTableRow(line: string): string[] {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
}

function isTableDivider(line: string): boolean {
  return /^\|?\s*:?-{3,}/.test(line.trim());
}

function parseGuide(content: string): GuideBlock[] {
  const lines = content.replace(/\r/g, '').split('\n');
  const blocks: GuideBlock[] = [];
  let index = 0;
  let headingIndex = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    if (line.startsWith('```')) {
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith('```')) code.push(lines[index++]);
      blocks.push({ type: 'code', code: code.join('\n') });
      index += 1;
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, text: heading[2], id: guideId(headingIndex++) });
      index += 1;
      continue;
    }
    if (line.trim().startsWith('|')) {
      const rows: string[][] = [];
      while (index < lines.length && lines[index].trim().startsWith('|')) {
        if (!isTableDivider(lines[index])) rows.push(splitTableRow(lines[index]));
        index += 1;
      }
      blocks.push({ type: 'table', rows });
      continue;
    }
    const list = line.match(/^(\d+\.|[-*])\s+(.+)$/);
    if (list) {
      const ordered = list[1].endsWith('.');
      const items: string[] = [];
      while (index < lines.length) {
        const item = lines[index].match(/^(\d+\.|[-*])\s+(.+)$/);
        if (!item || item[1].endsWith('.') !== ordered) break;
        items.push(item[2]);
        index += 1;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }
    if (line.startsWith('> ')) {
      blocks.push({ type: 'quote', text: line.slice(2) });
      index += 1;
      continue;
    }
    const paragraph: string[] = [];
    while (index < lines.length && lines[index].trim() && !/^(#{1,3})\s|^```|^\||^(\d+\.|[-*])\s+|^> /.test(lines[index])) {
      paragraph.push(lines[index++]);
    }
    blocks.push({ type: 'paragraph', text: paragraph.join(' ') });
  }
  return blocks;
}

function GuideBlocks({ blocks, onUseExample }: { blocks: GuideBlock[]; onUseExample: (code: string) => void }) {
  const [copiedCode, setCopiedCode] = useState<string | null>(null);
  const [copyError, setCopyError] = useState('');

  const copyCode = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopyError('');
      setCopiedCode(code);
      window.setTimeout(() => setCopiedCode(null), 1600);
    } catch {
      setCopyError('复制失败，请手动复制代码。');
    }
  };

  return <div className="strategy-guide-content">
    {blocks.map((block, index) => {
      if (block.type === 'heading') {
        const Tag = `h${block.level}` as 'h1' | 'h2' | 'h3';
        return <Tag className={`strategy-guide-heading level-${block.level}`} id={block.id} key={`${block.id}-${block.text}`}>{block.text}</Tag>;
      }
      if (block.type === 'quote') return <blockquote className="strategy-guide-quote" key={`quote-${index}`}>{block.text}</blockquote>;
      if (block.type === 'list') {
        const Tag = block.ordered ? 'ol' : 'ul';
        return <Tag className="strategy-guide-list" key={`list-${index}`}>{block.items.map((item) => <li key={item}>{item}</li>)}</Tag>;
      }
      if (block.type === 'code') {
        const copied = copiedCode === block.code;
        return <div className="strategy-guide-code" key={`code-${index}`}>
          <div className="strategy-guide-code-actions">
            <span>Python 示例</span>
            <div>
              <button type="button" onClick={() => void copyCode(block.code)}>{copied ? <Check size={13} /> : <Clipboard size={13} />}{copied ? '已复制' : '复制代码'}</button>
              <button className="is-primary" type="button" onClick={() => onUseExample(block.code)}>写入编辑器</button>
            </div>
          </div>
          {copyError ? <p className="strategy-guide-copy-error">{copyError}</p> : null}
          <pre><code>{block.code}</code></pre>
        </div>;
      }
      if (block.type === 'table') {
        return <div className="strategy-guide-table-wrap" key={`table-${index}`}><table className="strategy-guide-table"><tbody>{block.rows.map((row, rowIndex) => <tr key={`${rowIndex}-${row.join()}`}>{row.map((cell, cellIndex) => rowIndex === 0 ? <th key={`${cellIndex}-${cell}`}>{cell}</th> : <td key={`${cellIndex}-${cell}`}>{cell}</td>)}</tr>)}</tbody></table></div>;
      }
      return <p className="strategy-guide-paragraph" key={`paragraph-${index}`}>{block.text}</p>;
    })}
  </div>;
}

export default function StrategyGuideDrawer({ open, onClose, onUseExample }: Props) {
  const [guide, setGuide] = useState<{ title: string; content: string } | null>(null);
  const [error, setError] = useState('');
  const [hasLoaded, setHasLoaded] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [copiedPublicLink, setCopiedPublicLink] = useState(false);
  const scrollContainerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open || hasLoaded) return;
    setError('');
    let cancelled = false;
    fetchStrategyGuide().then((data) => {
      if (cancelled) return;
      setGuide(data);
      setHasLoaded(true);
    }).catch((reason: unknown) => {
      if (cancelled) return;
      setError(reason instanceof Error ? reason.message : '读取策略编写指南失败');
    });
    return () => { cancelled = true; };
  }, [hasLoaded, loadAttempt, open]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, open]);

  const blocks = useMemo(() => guide ? parseGuide(guide.content) : [], [guide]);
  const sections = useMemo(() => blocks.flatMap((block) => {
    if (block.type !== 'heading' || block.level !== 2) return [];
    return [{ id: block.id, text: block.text }];
  }), [blocks]);

  const scrollToSection = (sectionId: string) => {
    const container = scrollContainerRef.current;
    const target = document.getElementById(sectionId);
    if (!container || !target) return;
    const targetTop = target.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
    container.scrollTo({ top: Math.max(0, targetTop - 18), behavior: 'smooth' });
  };

  const copyPublicLink = async () => {
    try {
      await navigator.clipboard.writeText(`${window.location.origin}/strategy-guide.md`);
      setCopiedPublicLink(true);
      window.setTimeout(() => setCopiedPublicLink(false), 1600);
    } catch {
      setError('复制公开链接失败，请手动复制浏览器地址。');
    }
  };

  if (!open) return null;
  return <div className="strategy-guide-backdrop" role="presentation" onMouseDown={onClose}>
    <aside aria-label="策略编写指南" className="strategy-guide-drawer" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
      <header className="strategy-guide-drawer-header">
        <div><span className="strategy-guide-eyebrow"><FileText size={14} />策略代码参考</span><h2>{guide?.title || '策略编写指南'}</h2><p>读取当前系统契约；示例可复制或写入编辑器。</p></div>
        <div className="strategy-guide-header-actions">
          <a href="/strategy-guide.md" rel="noreferrer" target="_blank"><Link2 size={14} />打开 Markdown</a>
          <button type="button" onClick={() => void copyPublicLink()}>{copiedPublicLink ? <Check size={14} /> : <Clipboard size={14} />}{copiedPublicLink ? '已复制' : '复制公开链接'}</button>
          <button aria-label="关闭策略编写指南" className="strategy-guide-close" type="button" onClick={onClose}><X size={18} /></button>
        </div>
      </header>
      {guide ? <div className="strategy-guide-layout">
        <nav aria-label="指南目录" className="strategy-guide-toc">{sections.map((section) => <button type="button" onClick={() => scrollToSection(section.id)} key={section.id}><ChevronRight size={13} />{section.text}</button>)}</nav>
        <main className="strategy-guide-scroll" ref={scrollContainerRef}><GuideBlocks blocks={blocks} onUseExample={onUseExample} /></main>
      </div> : error ? <div className="strategy-guide-state is-error"><strong>指南暂不可用</strong><span>{error}</span><button type="button" onClick={() => { setError(''); setHasLoaded(false); setLoadAttempt((currentAttempt) => currentAttempt + 1); }}>重新加载</button></div> : <div className="strategy-guide-state"><LoaderCircle className="is-spinning" size={20} />正在读取策略编写指南…</div>}
    </aside>
  </div>;
}
