import { describe, expect, test } from 'vitest';

import { createExportJob, isAbortError } from '../exportJob';

describe('createExportJob', () => {
  test('creates distinct jobs that start alive', () => {
    const firstJob = createExportJob();
    const secondJob = createExportJob();

    expect(firstJob.id).not.toBe(secondJob.id);
    expect(firstJob.signal.aborted).toBe(false);
    expect(secondJob.signal.aborted).toBe(false);
    expect(firstJob.isCancelled()).toBe(false);
  });

  test('cancels the job and aborts its signal once', () => {
    const job = createExportJob();

    job.cancel();

    expect(job.isCancelled()).toBe(true);
    expect(job.signal.aborted).toBe(true);

    // Cancelling twice is a no-op.
    job.cancel();
    expect(job.signal.aborted).toBe(true);
  });
});

describe('isAbortError', () => {
  test('detects abort errors by name', () => {
    const abortError = new Error('Aborted');
    abortError.name = 'AbortError';

    expect(isAbortError(abortError)).toBe(true);
  });

  test('ignores other errors and non-errors', () => {
    expect(isAbortError(new Error('Boom'))).toBe(false);
    expect(isAbortError('AbortError')).toBe(false);
    expect(isAbortError(undefined)).toBe(false);
    expect(isAbortError(null)).toBe(false);
  });
});
