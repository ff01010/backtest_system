import { useCallback, useEffect, useRef, useState } from 'react';
import {
  cancelSignalDetailExport,
  createSignalDetailExport,
  fetchSignalDetailExports,
  retrySignalDetailExport,
} from '../api/client';
import type { BacktestRunSelection, SignalDetailExportTask } from '../types';
import { createBrowserUuid } from '../utils/browserUuid';


async function retryOnceOnNetworkError<T>(request: () => Promise<T>): Promise<T> {
  try {
    return await request();
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    return request();
  }
}

export function useSignalDetailExports(active: boolean) {
  const [tasks, setTasks] = useState<SignalDetailExportTask[]>([]);
  const [activeCount, setActiveCount] = useState(0);
  const [undownloadedCount, setUndownloadedCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState('');
  const knownStatuses = useRef(new Map<string, string>());

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const page = await fetchSignalDetailExports();
      for (const task of page.items) {
        const previous = knownStatuses.current.get(task.id);
        if (previous && previous !== 'succeeded' && task.status === 'succeeded') {
          setNotice(
            `${task.totalRunCount ?? task.selectedRunCount} 条回测的信号明细已生成，`
            + '可在“信号明细导出记录”中下载。',
          );
        }
        knownStatuses.current.set(task.id, task.status);
      }
      setTasks(page.items);
      setActiveCount(page.activeCount);
      setUndownloadedCount(page.undownloadedSucceededCount);
    } catch {
      // 导出记录暂不可用时不影响历史回测主列表。
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!active) return undefined;
    void refresh();
    const poll = () => {
      if (document.visibilityState === 'visible' && activeCount > 0) void refresh();
    };
    const timer = window.setInterval(poll, 3000);
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [active, activeCount, refresh]);

  const create = useCallback(async (selection: BacktestRunSelection) => {
    const key = createBrowserUuid();
    const task = await retryOnceOnNetworkError(
      () => createSignalDetailExport(selection, key),
    );
    knownStatuses.current.set(task.id, task.status);
    setTasks((current) => [task, ...current.filter((item) => item.id !== task.id)]);
    setActiveCount((current) => current + 1);
    return task;
  }, []);

  const cancel = useCallback(async (exportId: string) => {
    const task = await cancelSignalDetailExport(exportId);
    await refresh();
    return task;
  }, [refresh]);

  const retry = useCallback(async (exportId: string) => {
    const key = createBrowserUuid();
    const task = await retryOnceOnNetworkError(
      () => retrySignalDetailExport(exportId, key),
    );
    await refresh();
    return task;
  }, [refresh]);

  return {
    tasks,
    activeCount,
    undownloadedCount,
    loading,
    notice,
    clearNotice: () => setNotice(''),
    refresh,
    create,
    cancel,
    retry,
  };
}
