﻿import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn((_url: string, _init?: RequestInit): Promise<Response> =>
    Promise.resolve(new Response()),
  ),
  aiExtension: vi.fn((options: unknown) => options),
  chatTransport: vi.fn((options: unknown) => options),
}));

vi.mock('@/api', () => ({
  fetchAPI: mocks.fetchAPI,
}));

vi.mock('@/core', () => ({
  useConfig: () => ({ data: { AI_BOT: 'test-bot' } }),
}));

vi.mock('@/docs/doc-management', () => ({}));

vi.mock('@blocknote/xl-ai', () => ({
  AIExtension: mocks.aiExtension,
}));

vi.mock('ai', () => ({
  DefaultChatTransport: class {
    public constructor(options: unknown) {
      mocks.chatTransport(options);
      Object.assign(this, options);
    }
  },
}));

import { useAI } from '../useAI';

const signalOfCall = (index: number): AbortSignal => {
  const init = mocks.fetchAPI.mock.calls[index]?.[1];
  if (!init?.signal) {
    throw new Error(`expected an abort signal for fetchAPI call ${index}`);
  }
  return init.signal;
};

describe('useAI', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.fetchAPI.mockResolvedValue(new Response());
  });

  it('returns null when AI is not allowed', () => {
    const { result } = renderHook(() => useAI('doc-1', false));
    expect(result.current).toBeNull();
  });

  it('configures a transport whose custom fetch calls the AI proxy', async () => {
    const { result } = renderHook(() => useAI('doc-1', true));

    const extension = result.current as unknown as {
      transport: {
        fetch: (input: string, init?: RequestInit) => Promise<Response>;
      };
    };
    const upstream = new AbortController();

    await extension.transport.fetch('http://upstream', {
      signal: upstream.signal,
    });

    expect(mocks.fetchAPI).toHaveBeenCalledWith(
      'documents/doc-1/ai-proxy/',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('aborts the request when the AI SDK signal aborts (Stop button)', async () => {
    const { result } = renderHook(() => useAI('doc-1', true));
    const extension = result.current as unknown as {
      transport: {
        fetch: (input: string, init?: RequestInit) => Promise<Response>;
      };
    };

    const upstream = new AbortController();
    const promise = extension.transport.fetch('http://upstream', {
      signal: upstream.signal,
    });

    const chainedSignal = signalOfCall(0);
    expect(chainedSignal.aborted).toBe(false);

    upstream.abort('stopped by user');
    expect(chainedSignal.aborted).toBe(true);
    expect(chainedSignal.reason).toBe('stopped by user');

    await promise;
  });

  it('aborts in-flight requests on unmount (document switch / navigation)', () => {
    const { result, unmount } = renderHook(() => useAI('doc-1', true));
    const extension = result.current as unknown as {
      transport: {
        fetch: (input: string, init?: RequestInit) => Promise<Response>;
      };
    };

    const first = new AbortController();
    const second = new AbortController();
    void extension.transport.fetch('http://upstream', { signal: first.signal });
    void extension.transport.fetch('http://upstream', {
      signal: second.signal,
    });

    const firstSignal = signalOfCall(0);
    const secondSignal = signalOfCall(1);

    unmount();

    expect(firstSignal.aborted).toBe(true);
    expect(secondSignal.aborted).toBe(true);
  });

  it('does not fail when unmounting after requests completed', async () => {
    const { result, unmount } = renderHook(() => useAI('doc-1', true));
    const extension = result.current as unknown as {
      transport: {
        fetch: (input: string, init?: RequestInit) => Promise<Response>;
      };
    };

    await extension.transport.fetch('http://upstream');

    await waitFor(() => {
      const signal = signalOfCall(0);
      expect(signal.aborted).toBe(false);
    });

    expect(() => unmount()).not.toThrow();
  });

  it('starts already aborted when the upstream signal is aborted beforehand', () => {
    const { result } = renderHook(() => useAI('doc-1', true));
    const extension = result.current as unknown as {
      transport: {
        fetch: (input: string, init?: RequestInit) => Promise<Response>;
      };
    };

    const upstream = new AbortController();
    upstream.abort('already stopped');
    void extension.transport.fetch('http://upstream', {
      signal: upstream.signal,
    });

    const chainedSignal = signalOfCall(0);
    expect(chainedSignal.aborted).toBe(true);
    expect(chainedSignal.reason).toBe('already stopped');
  });

  it('does not share cancellation state across document instances', () => {
    const { result: first, unmount: unmountFirst } = renderHook(() =>
      useAI('doc-1', true),
    );
    const firstExtension = first.current as unknown as {
      transport: {
        fetch: (input: string, init?: RequestInit) => Promise<Response>;
      };
    };
    void firstExtension.transport.fetch('http://upstream');
    const firstSignal = signalOfCall(0);

    const { result: second } = renderHook(() => useAI('doc-2', true));
    const secondExtension = second.current as unknown as {
      transport: {
        fetch: (input: string, init?: RequestInit) => Promise<Response>;
      };
    };
    void secondExtension.transport.fetch('http://upstream');
    const secondSignal = signalOfCall(1);

    unmountFirst();

    expect(firstSignal.aborted).toBe(true);
    expect(secondSignal.aborted).toBe(false);
  });
});
