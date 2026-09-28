import { v5 as uuidv5 } from 'uuid';

import { Doc } from '../types';

/**
 * Fixed namespace used to derive replayable import identities. Any valid UUID
 * works here: it only scopes the generated v5 UUIDs.
 */
const IMPORT_ID_NAMESPACE = '9e6f2c11-7a31-4b6d-95f2-8d2a0c6e4b91';

// One File object can be submitted with different target parents in theory;
// the parent is part of the derived identity.
const identitiesCache = new WeakMap<File, Record<string, string>>();

const hashFile = async (file: File): Promise<string> => {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, '0'),
  ).join('');
};

/**
 * Derive the replayable identity of a file import.
 *
 * The same file imported into the same parent always yields the same UUID, so
 * retries after a network drop/timeout (or a quick double submission) are
 * recognized by the server as one import. A different file or a different
 * target parent produces another identity; reusing an identity with another
 * file or parent is rejected by the server.
 */
export const getImportDocumentId = async (
  file: File,
  parentId?: Doc['id'],
): Promise<string> => {
  const parentKey = parentId ?? '';
  const cached = identitiesCache.get(file)?.[parentKey];
  if (cached) {
    return cached;
  }

  const fileHash = await hashFile(file);
  const id = uuidv5(
    `${fileHash}:${file.name}:${parentKey}`,
    IMPORT_ID_NAMESPACE,
  );

  const identities = identitiesCache.get(file) ?? {};
  identities[parentKey] = id;
  identitiesCache.set(file, identities);

  return id;
};
