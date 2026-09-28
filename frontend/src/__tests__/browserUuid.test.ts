import { afterEach, describe, expect, it, vi } from 'vitest';

import { createBrowserUuid } from '../utils/browserUuid';

describe('createBrowserUuid', () => {
  const originalCrypto = globalThis.crypto;

  afterEach(() => {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: originalCrypto,
    });
  });

  it('uses the native randomUUID implementation when available', () => {
    const randomUUID = vi.fn(() => '11111111-1111-4111-8111-111111111111');
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: { randomUUID, getRandomValues: vi.fn() },
    });

    expect(createBrowserUuid()).toBe('11111111-1111-4111-8111-111111111111');
    expect(randomUUID).toHaveBeenCalledTimes(1);
  });

  it('creates a valid UUID v4 when randomUUID is unavailable', () => {
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: {
        getRandomValues: (target: Uint8Array) => {
          target.fill(0xab);
          return target;
        },
      },
    });

    expect(createBrowserUuid()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
});
