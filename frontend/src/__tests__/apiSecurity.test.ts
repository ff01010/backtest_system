import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';


const USER = {
  id: 'user-1',
  username: 'researcher',
  displayName: '研究员',
  role: 'user',
} as const;


describe('API security context', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('stores the login CSRF token in memory and adds it to every write request', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        user: USER,
        csrfToken: 'csrf-from-login',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        authorizationCode: 'code',
        expiresIn: 60,
        callbackUrl: 'http://local/callback',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    const { createResearchAuthorizationCode, login } = await import('../api/client');

    await login('researcher', 'password');
    await createResearchAuthorizationCode('state');

    const loginHeaders = new Headers(fetchMock.mock.calls[0][1]?.headers);
    const writeHeaders = new Headers(fetchMock.mock.calls[1][1]?.headers);
    expect(loginHeaders.has('X-CSRF-Token')).toBe(false);
    expect(writeHeaders.get('X-CSRF-Token')).toBe('csrf-from-login');
  });

  it('uses the shared CSRF transport for existing write workflows', async () => {
    const responseBody = {
      script: {},
      runId: 'run-1',
      status: 'queued',
      publication: {},
      created: true,
      export: {},
      ok: true,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        user: USER,
        csrfToken: 'shared-csrf-token',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockImplementation(async () => new Response(JSON.stringify(responseBody), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }));
    vi.stubGlobal('fetch', fetchMock);
    const {
      cancelBacktestRun,
      createCompleteStrategy,
      createSignalDetailExport,
      createStrategyScript,
      login,
      logout,
      publishBacktestRun,
      runBacktest,
      updateCompleteStrategy,
    } = await import('../api/client');

    await login('researcher', 'password');
    await createStrategyScript({} as never);
    await createCompleteStrategy({} as never, {} as never);
    await updateCompleteStrategy('config-1', {} as never, {} as never, 4);
    await runBacktest({} as never);
    await cancelBacktestRun('run-1');
    await publishBacktestRun('run-1', { title: '共享', description: '', tags: [] });
    await createSignalDetailExport({} as never, 'export-idempotency-key');
    await logout();

    const writeCalls = fetchMock.mock.calls.slice(1);
    expect(writeCalls.map(([input]) => String(input))).toEqual([
      '/api/strategy-scripts',
      '/api/complete-strategies',
      '/api/complete-strategies/config-1',
      '/api/backtest/run',
      '/api/backtest/runs/run-1/cancel',
      '/api/backtest/runs/run-1/publication',
      '/api/backtest/signal-detail-exports',
      '/api/auth/logout',
    ]);
    for (const [, init] of writeCalls) {
      expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('shared-csrf-token');
    }
    expect(writeCalls[1][1]?.method).toBe('POST');
    expect(writeCalls[2][1]?.method).toBe('PUT');
    expect(JSON.parse(String(writeCalls[2][1]?.body))).toMatchObject({
      expectedRevision: 4,
    });
  });

  it('initializes an old session once during concurrent page refreshes', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/me')) {
        return new Response(JSON.stringify({ user: USER }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/auth/csrf')) {
        return new Response(JSON.stringify({ csrfToken: 'csrf-from-refresh' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const { fetchCurrentUser, logout } = await import('../api/client');

    await Promise.all([fetchCurrentUser(), fetchCurrentUser()]);
    await logout();

    const csrfCalls = fetchMock.mock.calls.filter(([input]) => String(input).endsWith('/auth/csrf'));
    const logoutCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith('/auth/logout'));
    expect(csrfCalls).toHaveLength(1);
    expect(new Headers(logoutCall?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-from-refresh');
  });

  it('propagates the server requestId through structured API errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: {
        code: 'request.failed',
        message: '请求失败',
        retryable: false,
        requestId: 'req_body',
      },
    }), {
      status: 409,
      headers: {
        'Content-Type': 'application/json',
        'X-Request-Id': 'req_header',
      },
    })));
    const { ApiResponseError, login } = await import('../api/client');

    await expect(login('researcher', 'password')).rejects.toMatchObject({
      name: 'ApiResponseError',
      code: 'request.failed',
      message: '请求失败',
      requestId: 'req_body',
    } satisfies Partial<InstanceType<typeof ApiResponseError>>);
  });

  it('notifies the application once when an authenticated API request returns 401', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => new Response(JSON.stringify({
      detail: '登录已失效，请重新登录',
    }), { status: 401, headers: { 'Content-Type': 'application/json' } })));
    const {
      AUTH_SESSION_EXPIRED_EVENT,
      fetchBacktestResourceStatus,
    } = await import('../api/client');
    const listener = vi.fn();
    window.addEventListener(AUTH_SESSION_EXPIRED_EVENT, listener);

    await expect(fetchBacktestResourceStatus()).rejects.toMatchObject({ status: 401 });
    await expect(fetchBacktestResourceStatus()).rejects.toMatchObject({ status: 401 });

    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener(AUTH_SESSION_EXPIRED_EVENT, listener);
  });

  it('does not report a failed login as an expired existing session', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      detail: '用户名或密码错误',
    }), { status: 401, headers: { 'Content-Type': 'application/json' } })));
    const { AUTH_SESSION_EXPIRED_EVENT, login } = await import('../api/client');
    const listener = vi.fn();
    window.addEventListener(AUTH_SESSION_EXPIRED_EVENT, listener);

    await expect(login('researcher', 'wrong-password')).rejects.toMatchObject({ status: 401 });

    expect(listener).not.toHaveBeenCalled();
    window.removeEventListener(AUTH_SESSION_EXPIRED_EVENT, listener);
  });
});
