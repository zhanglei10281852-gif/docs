import { validate as validateUuid, version as uuidVersion } from 'uuid';
import { describe, expect, it } from 'vitest';

import { getImportDocumentId } from '../importId';

const makeFile = (name: string, content: string) =>
  new File([content], name, { type: 'text/markdown' });

describe('getImportDocumentId', () => {
  it('returns a valid v5 UUID', async () => {
    const id = await getImportDocumentId(makeFile('a.md', 'hello'));

    expect(validateUuid(id)).toBe(true);
    expect(uuidVersion(id)).toBe(5);
  });

  it('is stable for the same file and parent across retries', async () => {
    const file = makeFile('readme.md', '# Hello');

    const first = await getImportDocumentId(file);
    const retry = await getImportDocumentId(file);

    expect(first).toBe(retry);
  });

  it('produces the same identity for an identical file picked again', async () => {
    const first = await getImportDocumentId(makeFile('readme.md', '# Hello'));
    const pickedAgain = await getImportDocumentId(
      makeFile('readme.md', '# Hello'),
    );

    expect(first).toBe(pickedAgain);
  });

  it('produces a different identity for a different file', async () => {
    const first = await getImportDocumentId(makeFile('a.md', 'content a'));
    const other = await getImportDocumentId(makeFile('a.md', 'content b'));

    expect(other).not.toBe(first);
  });

  it('scopes the identity with the target parent', async () => {
    const file = makeFile('readme.md', '# Hello');

    const rootId = await getImportDocumentId(file);
    const childId = await getImportDocumentId(file, 'parent-uuid');
    const otherChildId = await getImportDocumentId(file, 'other-parent-uuid');

    expect(childId).not.toBe(rootId);
    expect(otherChildId).not.toBe(childId);

    // The same parent keeps a stable identity.
    expect(await getImportDocumentId(file, 'parent-uuid')).toBe(childId);
  });
});
