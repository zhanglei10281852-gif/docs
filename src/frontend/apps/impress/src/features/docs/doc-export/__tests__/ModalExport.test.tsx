import { VariantType } from '@gouvfr-lasuite/ui-components';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, test, vi } from 'vitest';

import { type Doc } from '@/docs/doc-management/types';
import { AppWrapper } from '@/tests/utils';

import { ModalExport } from '../components/ModalExport';

const mocks = vi.hoisted(() => ({
  addMediaFilesToMarkdownZip: vi.fn(),
  blocksToMarkdownLossy: vi.fn(),
  downloadFile: vi.fn(),
  docToBlob: vi.fn(),
  toast: vi.fn(),
}));

vi.mock('@gouvfr-lasuite/ui-components', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@gouvfr-lasuite/ui-components')>();

  return {
    ...actual,
    useToastProvider: () => ({ toast: mocks.toast }),
  };
});

vi.mock('@/core', () => ({
  useMediaUrl: () => 'https://media.test',
}));

vi.mock('@/docs/doc-editor/stores/useEditorStore', () => ({
  useEditorStore: () => ({
    editor: {
      document: [],
      blocksToMarkdownLossy: mocks.blocksToMarkdownLossy,
    },
  }),
}));

vi.mock('@/docs/doc-management', () => ({
  useTrans: () => ({ untitledDocument: 'Untitled document' }),
}));

vi.mock('../hooks/', () => ({
  default: {
    useExportAGPL: () => ({
      formats: [{ label: 'PDF', value: 'pdf', labelDescription: '.pdf' }],
      docToBlob: mocks.docToBlob,
    }),
  },
}));

vi.mock('../utils', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils')>();

  return {
    ...actual,
    downloadFile: mocks.downloadFile,
  };
});

vi.mock('../utils_markdown', () => ({
  addMediaFilesToMarkdownZip: mocks.addMediaFilesToMarkdownZip,
}));

describe('ModalExport', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.addMediaFilesToMarkdownZip.mockResolvedValue(0);
    mocks.blocksToMarkdownLossy.mockResolvedValue('# Roadmap');
    mocks.docToBlob.mockResolvedValue(undefined);
  });

  test('downloads Markdown directly when no media is archived', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(
      <ModalExport doc={{ title: 'Roadmap' } as Doc} onClose={onClose} />,
      { wrapper: AppWrapper },
    );

    await user.click(screen.getByRole('combobox', { name: 'Format' }));
    await user.click(screen.getByRole('option', { name: /Markdown/ }));
    await user.click(screen.getByTestId('doc-export-download-button'));

    await waitFor(() =>
      expect(mocks.downloadFile).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'text/markdown;charset=utf-8' }),
        'roadmap.md',
      ),
    );
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('downloads a ZIP when Markdown contains archived media', async () => {
    mocks.addMediaFilesToMarkdownZip.mockResolvedValue(1);
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(
      <ModalExport doc={{ title: 'Roadmap' } as Doc} onClose={onClose} />,
      { wrapper: AppWrapper },
    );

    await user.click(screen.getByRole('combobox', { name: 'Format' }));
    await user.click(screen.getByRole('option', { name: /Markdown/ }));
    await user.click(screen.getByTestId('doc-export-download-button'));

    await waitFor(() =>
      expect(mocks.downloadFile).toHaveBeenCalledWith(
        expect.any(Blob),
        'roadmap.zip',
      ),
    );
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('clears the loading state when Markdown media export fails', async () => {
    let rejectMediaExport: (reason: Error) => void = () => undefined;
    mocks.addMediaFilesToMarkdownZip.mockImplementation(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectMediaExport = reject;
        }),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(
      <ModalExport doc={{ title: 'Roadmap' } as Doc} onClose={onClose} />,
      { wrapper: AppWrapper },
    );

    await user.click(screen.getByRole('combobox', { name: 'Format' }));
    await user.click(screen.getByRole('option', { name: /Markdown/ }));

    const downloadButton = screen.getByTestId('doc-export-download-button');
    await user.click(downloadButton);
    await waitFor(() => expect(downloadButton).toBeDisabled());

    await act(async () => {
      rejectMediaExport(new Error('Media export failed'));
    });

    await waitFor(() => expect(downloadButton).toBeEnabled());
    expect(mocks.toast).toHaveBeenCalledWith(
      'The export failed',
      VariantType.ERROR,
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  test('captures the document title and content once, at job start', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(
      <ModalExport doc={{ title: 'Roadmap' } as Doc} onClose={onClose} />,
      { wrapper: AppWrapper },
    );

    await user.click(screen.getByTestId('doc-export-download-button'));

    await waitFor(() =>
      expect(mocks.docToBlob).toHaveBeenCalledWith(
        'pdf',
        'Roadmap',
        [],
        expect.any(AbortSignal),
      ),
    );
  });

  test('cancelling during media packaging aborts the job without download or toast', async () => {
    let rejectMediaExport: (reason: unknown) => void = () => undefined;
    let capturedSignal: AbortSignal | undefined;
    mocks.addMediaFilesToMarkdownZip.mockImplementation(
      (_blocks, _zip, _mediaUrl, _resolveMedia, signal) =>
        new Promise<number>((_resolve, reject) => {
          capturedSignal = signal as AbortSignal;
          rejectMediaExport = reject;
        }),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(
      <ModalExport doc={{ title: 'Roadmap' } as Doc} onClose={onClose} />,
      { wrapper: AppWrapper },
    );

    await user.click(screen.getByRole('combobox', { name: 'Format' }));
    await user.click(screen.getByRole('option', { name: /Markdown/ }));
    await user.click(screen.getByTestId('doc-export-download-button'));

    await waitFor(() => expect(capturedSignal).toBeInstanceOf(AbortSignal));

    await user.click(
      screen.getByRole('button', { name: 'Cancel the download' }),
    );

    expect(capturedSignal?.aborted).toBe(true);
    expect(onClose).toHaveBeenCalledOnce();

    const abortError = new Error('Aborted');
    abortError.name = 'AbortError';
    await act(async () => {
      rejectMediaExport(abortError);
    });

    expect(mocks.downloadFile).not.toHaveBeenCalled();
    expect(mocks.toast).not.toHaveBeenCalled();
    expect(mocks.blocksToMarkdownLossy).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('discards a PDF conversion result that completes after cancellation', async () => {
    let resolveDocToBlob: (value: Blob | undefined) => void = () => undefined;
    let capturedSignal: AbortSignal | undefined;
    mocks.docToBlob.mockImplementation(
      (_format, _title, _blocks, signal) =>
        new Promise<Blob | undefined>((resolve) => {
          capturedSignal = signal as AbortSignal;
          resolveDocToBlob = resolve;
        }),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();

    render(
      <ModalExport doc={{ title: 'Roadmap' } as Doc} onClose={onClose} />,
      { wrapper: AppWrapper },
    );

    // PDF is the default format when the AGPL exporters are available.
    await user.click(screen.getByTestId('doc-export-download-button'));

    await waitFor(() => expect(capturedSignal).toBeInstanceOf(AbortSignal));

    await user.click(
      screen.getByRole('button', { name: 'Cancel the download' }),
    );

    expect(capturedSignal?.aborted).toBe(true);

    await act(async () => {
      resolveDocToBlob(new Blob(['pdf'], { type: 'application/pdf' }));
    });

    expect(mocks.downloadFile).not.toHaveBeenCalled();
    expect(mocks.toast).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledOnce();
  });

  test('unmounting the modal aborts the export and discards a late result', async () => {
    let resolveMediaExport: (value: number) => void = () => undefined;
    let capturedSignal: AbortSignal | undefined;
    mocks.addMediaFilesToMarkdownZip.mockImplementation(
      (_blocks, _zip, _mediaUrl, _resolveMedia, signal) =>
        new Promise<number>((resolve) => {
          capturedSignal = signal as AbortSignal;
          resolveMediaExport = resolve;
        }),
    );
    const onClose = vi.fn();
    const user = userEvent.setup();

    const { unmount } = render(
      <ModalExport doc={{ title: 'Roadmap' } as Doc} onClose={onClose} />,
      { wrapper: AppWrapper },
    );

    await user.click(screen.getByRole('combobox', { name: 'Format' }));
    await user.click(screen.getByRole('option', { name: /Markdown/ }));
    await user.click(screen.getByTestId('doc-export-download-button'));

    await waitFor(() => expect(capturedSignal).toBeInstanceOf(AbortSignal));

    unmount();

    expect(capturedSignal?.aborted).toBe(true);

    await act(async () => {
      resolveMediaExport(0);
    });

    expect(mocks.downloadFile).not.toHaveBeenCalled();
    expect(mocks.toast).not.toHaveBeenCalled();
    expect(mocks.blocksToMarkdownLossy).not.toHaveBeenCalled();
  });
});
