import { baseApiUrl } from '@/api';
import { Doc } from '@/features/docs/doc-management';
import { isDataUrl, isLocalDevOrigin, isSameOrigin } from '@/utils/url';

import { isAbortError } from '../exportJob';

export const exportCorsResolveFileUrl = async (
  docId: Doc['id'],
  url: string,
  signal?: AbortSignal,
) => {
  let resolvedUrl = url;

  // data: urls are already embedded, there is nothing to fetch remotely.
  // Anything else that is not from the same origin (or, in local dev, the
  // same localhost host on a different port) is proxied to avoid CORS
  // issues.
  if (!isDataUrl(url) && !isSameOrigin(url) && !isLocalDevOrigin(url)) {
    resolvedUrl = `${baseApiUrl()}documents/${docId}/cors-proxy/?url=${encodeURIComponent(url)}`;
  }

  return exportResolveFileUrl(resolvedUrl, signal);
};

export const exportResolveFileUrl = async (
  url: string,
  signal?: AbortSignal,
) => {
  try {
    const response = await fetch(url, {
      credentials: 'include',
      signal,
    });

    if (!response.ok) {
      throw new Error(`Unexpected response status: ${response.status}`);
    }

    return await response.blob();
  } catch (error) {
    // A user-triggered cancellation is not a regular media read failure:
    // rethrow it so the export job stops and never ships a partial archive,
    // instead of falling back to the original URL.
    if (isAbortError(error) || signal?.aborted) {
      throw error;
    }

    console.error(`Failed to fetch image: ${url}`, error);
  }

  return url;
};
