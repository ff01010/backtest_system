import { useCallback, useEffect, useRef, useState } from 'react';
import {
  acknowledgeRelease,
  fetchReleaseHistory,
  fetchReleaseStatus,
  ReleaseApiError,
} from '../api/client';
import type { ReleaseNotice, ReleaseStatus } from '../types';
import ReleaseHistoryDrawer from './ReleaseHistoryDrawer';

type Props = {
  userId: string;
  hasUnsavedStrategy: boolean;
  isStrategySavePending: boolean;
  onRequestStrategySave: () => Promise<boolean>;
  clientBuildId?: string;
  clientCompatibilityVersion?: number;
};

const CLIENT_BUILD_ID = import.meta.env.VITE_APPLICATION_BUILD_ID || 'dev';
const CLIENT_COMPATIBILITY_VERSION = Number(import.meta.env.VITE_CLIENT_COMPATIBILITY_VERSION || 1);
const secondaryButtonStyle = {
  border: '1px solid #475569', borderRadius: 4, padding: '7px 13px',
  background: '#202632', color: '#dbeafe', cursor: 'pointer', fontWeight: 650,
} as const;
const primaryButtonStyle = {
  border: '1px solid #2dd4bf', borderRadius: 4, padding: '7px 13px',
  background: '#0f766e', color: '#f0fdfa', cursor: 'pointer', fontWeight: 750,
  boxShadow: 'inset 0 1px rgba(255,255,255,.12)',
} as const;

export default function ReleaseNotificationManager({
  userId,
  hasUnsavedStrategy,
  isStrategySavePending,
  onRequestStrategySave,
  clientBuildId = CLIENT_BUILD_ID,
  clientCompatibilityVersion = CLIENT_COMPATIBILITY_VERSION,
}: Props) {
  const [status, setStatus] = useState<ReleaseStatus | null>(null);
  const [dismissedReleaseId, setDismissedReleaseId] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [history, setHistory] = useState<ReleaseNotice[]>([]);
  const [refreshWarning, setRefreshWarning] = useState('');
  const [acknowledgeError, setAcknowledgeError] = useState('');
  const [acknowledgePending, setAcknowledgePending] = useState(false);
  const etagRef = useRef<string | undefined>();
  const runningRef = useRef(false);
  const lastCheckRef = useRef(0);
  const nextCheckRef = useRef(0);
  const failureCountRef = useRef(0);
  const failureLoggedRef = useRef(false);
  const disabledRef = useRef(false);
  const releaseIdRef = useRef<string | null>(null);
  const channelRef = useRef<BroadcastChannel | null>(null);

  const checkRelease = useCallback(async () => {
    if (
      clientBuildId === 'dev'
      || disabledRef.current
      || runningRef.current
      || Date.now() < nextCheckRef.current
    ) return;
    runningRef.current = true;
    try {
      const result = await fetchReleaseStatus(
        clientBuildId, clientCompatibilityVersion, etagRef.current,
      );
      lastCheckRef.current = Date.now();
      if (result.etag) etagRef.current = result.etag;
      if (result.status) {
        const nextReleaseId = result.status.release?.id ?? null;
        if (releaseIdRef.current !== nextReleaseId) {
          setDismissedReleaseId(null);
          setAcknowledgeError('');
        }
        releaseIdRef.current = nextReleaseId;
        setStatus(result.status);
      }
      failureCountRef.current = 0;
      nextCheckRef.current = 0;
      failureLoggedRef.current = false;
    } catch (error) {
      if (error instanceof ReleaseApiError && error.status === 404) {
        disabledRef.current = true;
        return;
      }
      failureCountRef.current += 1;
      const retryDelay = Math.min(15 * 60_000, 60_000 * (2 ** (failureCountRef.current - 1)));
      nextCheckRef.current = Date.now() + retryDelay;
      if (!failureLoggedRef.current) {
        console.warn('版本通知检查失败，将自动退避重试', error);
        failureLoggedRef.current = true;
      }
    } finally {
      runningRef.current = false;
    }
  }, [clientBuildId, clientCompatibilityVersion]);

  useEffect(() => {
    etagRef.current = undefined;
    setStatus(null);
    setDismissedReleaseId(null);
    releaseIdRef.current = null;
    nextCheckRef.current = 0;
    failureCountRef.current = 0;
    failureLoggedRef.current = false;
    disabledRef.current = false;
    void checkRelease();
  }, [userId, checkRelease]);

  useEffect(() => {
    if (!('BroadcastChannel' in window)) return undefined;
    const channel = new BroadcastChannel('release-notifications');
    channelRef.current = channel;
    channel.onmessage = (event: MessageEvent<{ releaseId?: string }>) => {
      const releaseId = event.data.releaseId;
      if (!releaseId) return;
      setDismissedReleaseId(releaseId);
      setStatus((current) => current?.release?.id === releaseId
        ? { ...current, acknowledged: true, unread: false }
        : current);
    };
    return () => {
      channel.close();
      channelRef.current = null;
    };
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => { if (!document.hidden) void checkRelease(); }, 300_000);
    const onVisible = () => { if (!document.hidden) void checkRelease(); };
    const onFocus = () => {
      if (Date.now() - lastCheckRef.current >= 60_000) void checkRelease();
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onFocus);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', onFocus);
    };
  }, [checkRelease]);

  const acknowledge = async (): Promise<void> => {
    if (!status?.release) return;
    try {
      await acknowledgeRelease(status.release.id, clientBuildId);
    } catch (error) {
      if (error instanceof ReleaseApiError && error.status === 409) {
        etagRef.current = undefined;
        nextCheckRef.current = 0;
        await checkRelease();
        setAcknowledgeError('公告状态已更新，请重试。');
        return;
      }
      throw error;
    }
    etagRef.current = undefined;
    setAcknowledgeError('');
    setDismissedReleaseId(status.release.id);
    setStatus({ ...status, acknowledged: true, unread: false });
    channelRef.current?.postMessage({ releaseId: status.release.id });
  };

  const acknowledgeWithFeedback = async (): Promise<void> => {
    if (acknowledgePending) return;
    setAcknowledgePending(true);
    setAcknowledgeError('');
    try {
      await acknowledge();
    } catch (error) {
      console.warn('版本公告确认失败', error);
      setAcknowledgeError('确认失败，请重试。');
    } finally {
      setAcknowledgePending(false);
    }
  };

  const refresh = async (abandonChanges = false): Promise<void> => {
    if (hasUnsavedStrategy && !abandonChanges) {
      setRefreshWarning('策略存在未保存修改，请先保存后再刷新。');
      return;
    }
    try {
      await acknowledge();
    } catch {
      // 刷新后服务端仍会返回未读公告，不在本地跨账号补写回执。
    }
    window.location.reload();
  };

  const saveAndRefresh = async (): Promise<void> => {
    if (await onRequestStrategySave()) await refresh(true);
  };

  const openHistory = async (): Promise<void> => {
    setHistoryOpen(true);
    setHistoryLoading(true);
    try {
      setHistory(await fetchReleaseHistory());
    } catch (error) {
      console.warn('更新记录加载失败', error);
    } finally {
      setHistoryLoading(false);
    }
  };

  const mandatory = status?.upgradeRequired === true;
  const releaseOpen = Boolean(
    status?.release && status.unread && status.release.id !== dismissedReleaseId,
  );
  const mismatchLabel = status?.presentationMode === 'rollback_mismatch'
    ? '页面版本与服务器不一致'
    : '有新版本';

  return (
    <>
      <button type="button" onClick={() => { void openHistory(); }} style={{
        border: '1px solid #3a3f4b', borderRadius: 3, padding: '4px 9px',
        background: status?.clientOutdated ? '#3b2f12' : 'transparent',
        color: status?.clientOutdated ? '#facc15' : 'var(--chrome-text)', cursor: 'pointer',
      }}>
        {status?.clientOutdated ? mismatchLabel : '更新记录'}
      </button>
      {(releaseOpen || mandatory) && status && (
        <div role="dialog" aria-modal="true" aria-label="版本更新" style={{
          position: 'fixed', zIndex: 1300, inset: 0, display: 'grid', placeItems: 'center',
          background: 'rgba(0, 0, 0, .62)', padding: 20,
        }}>
          <section style={{ width: 'min(560px, 96vw)', border: '1px solid #385268', borderRadius: 8, background: 'linear-gradient(145deg, #171d27, #11151c)', color: '#f3f4f6', padding: 24, boxShadow: '0 24px 70px rgba(0,0,0,.58), inset 3px 0 #2dd4bf' }}>
            {status.release?.level === 'normal' && !mandatory && (
              <button type="button" aria-label="关闭版本通知" style={{
                ...secondaryButtonStyle, float: 'right', padding: '2px 8px', fontSize: 18,
              }} disabled={acknowledgePending} onClick={() => { void acknowledgeWithFeedback(); }}>×</button>
            )}
            {status.release && <div style={{ color: '#93c5fd', fontSize: 13 }}>{status.release.version} · {new Date(status.release.publishedAt).toLocaleString()}</div>}
            <h2 style={{ margin: '8px 0' }}>{mandatory ? '需要刷新后继续使用' : '发现新版本'}</h2>
            <h3 style={{ margin: '12px 0 6px' }}>{status.release?.title || '当前页面版本已不再兼容'}</h3>
            <p style={{ color: '#cbd5e1' }}>{status.release?.summary || '请刷新页面加载服务器要求的客户端版本。'}</p>
            {status.release && <ul>{status.release.highlights.map((item) => <li key={item}>{item}</li>)}</ul>}
            {refreshWarning && <p role="alert" style={{ color: '#fbbf24' }}>{refreshWarning}</p>}
            {acknowledgeError && <p role="alert" style={{ color: '#fca5a5' }}>{acknowledgeError}</p>}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 20, flexWrap: 'wrap' }}>
              {!mandatory && (
                <button type="button" style={secondaryButtonStyle} disabled={acknowledgePending} onClick={() => { void acknowledgeWithFeedback(); }}>
                  {acknowledgePending ? '正在确认…' : '知道了'}
                </button>
              )}
              {hasUnsavedStrategy && (
                <button type="button" style={primaryButtonStyle} disabled={isStrategySavePending} onClick={() => { void saveAndRefresh(); }}>
                  {isStrategySavePending ? '正在保存…' : '保存策略后刷新'}
                </button>
              )}
              {mandatory && hasUnsavedStrategy && (
                <button type="button" style={{ ...secondaryButtonStyle, borderColor: '#b45309', color: '#fde68a' }} disabled={isStrategySavePending} onClick={() => {
                  if (window.confirm('放弃未保存的策略修改并刷新？')) void refresh(true);
                }}>放弃修改并刷新</button>
              )}
              {(!hasUnsavedStrategy || !mandatory) && (
                <button type="button" style={primaryButtonStyle} onClick={() => { void refresh(); }}>刷新使用新版本</button>
              )}
            </div>
          </section>
        </div>
      )}
      <ReleaseHistoryDrawer open={historyOpen} loading={historyLoading} items={history} onClose={() => setHistoryOpen(false)} />
    </>
  );
}
