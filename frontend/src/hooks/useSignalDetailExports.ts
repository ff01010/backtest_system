import { useCallback, useEffect, useRef, useState } from 'react';
import {
  cancelSignalDetailExport,
  createSignalDetailExport,
  fetchSignalDetailExports,
  retrySignalDetailExport,
} from '../api/client';
import type { BacktestRunSelection, SignalDetailExportTask } from '../types';


function idempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();

  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0'));
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10, 16).join(''),
  ].join('-');
}

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
    const key = idempotencyKey();
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
    const key = idempotencyKey();
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
