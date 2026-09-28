/**
 * Lifecycle of a single document export, shared by every supported format
 * (PDF, DOCX, ODT, Markdown and HTML).
 *
 * An export job:
 * - is created once, when the export starts, so the document content and
 *   title can be captured for the whole lifetime of the job;
 * - exposes an `AbortSignal` used to terminate in-flight media and style
 *   requests when the user cancels;
 * - stays marked as cancelled across the stages that cannot be interrupted
 *   (PDF/DOCX/ODT conversion and ZIP compression), so a result that
 *   completes afterwards is discarded instead of being downloaded or
 *   surfaced with a success/failure notification.
 */
export interface ExportJob {
  /** Monotonic identifier, used to tell a stale job from the active one. */
  readonly id: number;
  /** Aborted as soon as the export is cancelled. */
  readonly signal: AbortSignal;
  /** Terminates pending media/style requests and marks the job as cancelled. */
  cancel: () => void;
  /** Whether the job has been cancelled. */
  isCancelled: () => boolean;
}

let nextExportJobId = 1;

export const createExportJob = (): ExportJob => {
  const controller = new AbortController();

  return {
    id: nextExportJobId++,
    signal: controller.signal,
    cancel: () => controller.abort(),
    isCancelled: () => controller.signal.aborted,
  };
};

/** Checks whether an error results from an `AbortController` cancellation. */
export const isAbortError = (error: unknown): boolean =>
  typeof error === 'object' &&
  error !== null &&
  'name' in error &&
  error.name === 'AbortError';
