import { afterEach, describe, expect, it, vi } from 'vitest';
import { acknowledgeRelease, fetchReleaseStatus } from '../api/client';

describe('release notification api', () => {
  afterEach(() => vi.restoreAllMocks());

  it('保留 304 对应的 ETag 且不解析空响应体', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, {
      status: 304,
      headers: { ETag: '"cached"' },
    })));

    const result = await fetchReleaseStatus('a'.repeat(40), 1, '"cached"');

    expect(result).toEqual({ status: null, etag: '"cached"' });
  });

  it('确认公告不发送用户 ID', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"acknowledged":true}', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);

    await acknowledgeRelease('release-1', 'b'.repeat(40));

    const [, request] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(request.body))).toEqual({ clientBuildId: 'b'.repeat(40) });
  });
});
