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
      createSignalDetailExport,
      createStrategyScript,
      login,
      logout,
      publishBacktestRun,
      runBacktest,
    } = await import('../api/client');

    await login('researcher', 'password');
    await createStrategyScript({} as never);
    await runBacktest({} as never);
    await cancelBacktestRun('run-1');
    await publishBacktestRun('run-1', { title: '共享', description: '', tags: [] });
    await createSignalDetailExport({} as never, 'export-idempotency-key');
    await logout();

    const writeCalls = fetchMock.mock.calls.slice(1);
    expect(writeCalls.map(([input]) => String(input))).toEqual([
      '/api/strategy-scripts',
      '/api/backtest/run',
      '/api/backtest/runs/run-1/cancel',
      '/api/backtest/runs/run-1/publication',
      '/api/backtest/signal-detail-exports',
      '/api/auth/logout',
    ]);
    for (const [, init] of writeCalls) {
      expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('shared-csrf-token');
    }
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
});
