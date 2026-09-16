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
    <section className="shared-copy-panel">
      <div className="shared-copy-panel-icon"><Copy size={19} /></div>
      <div className="shared-copy-panel-content">
        <h2>复制为我的策略</h2>
        <p>
          将创建独立策略、脚本和首个不可变版本，不会自动运行。原作者的回测账户不会复制，进入编辑器后请检查代码并选择自己的账户。
        </p>
        {error && <div role="alert" className="shared-alert is-error">{error}</div>}
      </div>
      <button type="button" className="shared-button is-primary" onClick={() => void copyStrategy()} disabled={copying}>
        <Copy size={15} /> {copying ? '正在打开…' : hasCopied ? '打开我的副本' : '复制到我的策略'}
      </button>
    </section>
  );
}
