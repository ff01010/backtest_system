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
