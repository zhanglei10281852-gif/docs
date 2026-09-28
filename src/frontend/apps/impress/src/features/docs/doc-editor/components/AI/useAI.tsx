import { AIExtension } from '@blocknote/xl-ai';
import { DefaultChatTransport } from 'ai';
import { useEffect, useMemo, useRef } from 'react';

import { fetchAPI } from '@/api';
import { useConfig } from '@/core';
import { Doc } from '@/docs/doc-management';

export const useAI = (docId: Doc['id'], aiAllowed: boolean) => {
  const conf = useConfig().data;

  // Tracks the AI streams in flight for this editor. Switching document
  // (the editor unmounts while the new collaboration provider loads) or
  // leaving the page aborts the very same HTTP requests, so the backend
  // stops pulling the upstream model stream instead of keeping the
  // generation running in the background with the UI hidden.
  const activeStreamsRef = useRef(new Set<AbortController>());

  useEffect(() => {
    const activeStreams = activeStreamsRef.current;

    return () => {
      activeStreams.forEach((controller) => {
        controller.abort(
          new DOMException('AI generation stopped', 'AbortError'),
        );
      });
      activeStreams.clear();
    };
  }, [docId]);

  return useMemo(() => {
    if (!aiAllowed) {
      return null;
    }

    const extension = AIExtension({
      transport: new DefaultChatTransport({
        fetch: (input, init) => {
          // Create a new headers object without the Authorization header
          const headers = new Headers(init?.headers);
          headers.delete('Authorization');

          // The AI SDK aborts its own `init.signal` when chat.stop() is
          // called (the "Stop" button / Escape). Chain it with a controller
          // owned by this hook: either signal aborts the single fetch below,
          // and editor teardown aborts it too even if the extension itself
          // never invokes abort().
          const controller = new AbortController();
          const untrack = () => {
            activeStreamsRef.current.delete(controller);
          };
          activeStreamsRef.current.add(controller);
          controller.signal.addEventListener('abort', untrack, { once: true });

          const upstreamSignal = init?.signal;
          if (upstreamSignal) {
            if (upstreamSignal.aborted) {
              controller.abort(upstreamSignal.reason);
            } else {
              upstreamSignal.addEventListener(
                'abort',
                () => controller.abort(upstreamSignal.reason),
                { once: true },
              );
            }
          }

          return fetchAPI(`documents/${docId}/ai-proxy/`, {
            ...init,
            headers,
            signal: controller.signal,
          }).finally(untrack);
        },
      }),
      agentCursor: conf?.AI_BOT,
    });

    return extension;
  }, [conf?.AI_BOT, docId, aiAllowed]);
};
