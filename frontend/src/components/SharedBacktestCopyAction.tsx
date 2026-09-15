import { Copy } from 'lucide-react';
import { useState } from 'react';
import { copySharedBacktest } from '../api/client';


interface Props {
  publicationId: string;
  hasCopied?: boolean;
  onOpenStrategy: (configId: string) => void;
}


export default function SharedBacktestCopyAction({
  publicationId,
  hasCopied = false,
  onOpenStrategy,
}: Props) {
  const [copying, setCopying] = useState(false);
  const [error, setError] = useState('');

  const copyStrategy = async () => {
    setCopying(true);
    setError('');
    try {
      const copied = await copySharedBacktest(publicationId);
      onOpenStrategy(copied.configId);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '复制共享回测失败');
    } finally {
      setCopying(false);
    }
  };

  return (
    <section style={{ border: '1px solid #bfdbfe', borderRadius: 8, background: '#eff6ff', padding: 18 }}>
      <h2 style={{ marginTop: 0, fontSize: 18 }}>复制为我的策略</h2>
      <p style={{ color: '#475569', lineHeight: 1.7 }}>
        将创建独立策略、脚本和首个不可变版本，不会自动运行。原作者的回测账户不会复制，进入编辑器后请检查代码并选择自己的账户。
      </p>
      <button type="button" onClick={() => void copyStrategy()} disabled={copying}>
        <Copy size={15} /> {copying ? '正在打开…' : hasCopied ? '打开我的副本' : '复制到我的策略'}
      </button>
      {error && <div role="alert" style={{ color: '#b91c1c', marginTop: 10 }}>{error}</div>}
    </section>
  );
}
