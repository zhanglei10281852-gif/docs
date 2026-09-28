import {
  Button,
  Loader,
  Modal,
  ModalSize,
  Select,
  VariantType,
} from '@gouvfr-lasuite/ui-components';
import i18next from 'i18next';
import JSZip from 'jszip';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { css } from 'styled-components';

import { Box, ButtonCloseModal, Text } from '@/components';
import { useMediaUrl } from '@/core';
import { useEditorStore } from '@/docs/doc-editor/stores/useEditorStore';
import { type Doc, useTrans } from '@/docs/doc-management';
import { useToast } from '@/hooks';
import { fallbackLng } from '@/i18n/config';

import { exportResolveFileUrl } from '../api';
import { type ExportJob, createExportJob, isAbortError } from '../exportJob';
import ModulesExport from '../hooks/';
import { downloadFile, getExportFilename } from '../utils';
import {
  addMediaFilesToZip,
  generateHtmlDocument,
  improveHtmlAccessibility,
} from '../utils_html';
import { addMediaFilesToMarkdownZip } from '../utils_markdown';

const useExportAGPL = ModulesExport?.useExportAGPL;

interface ModalExportProps {
  onClose: () => void;
  doc: Doc;
}

export const ModalExport = ({ onClose, doc }: ModalExportProps) => {
  const { t } = useTranslation();
  const { toast } = useToast();
  const { editor } = useEditorStore();
  const [isExporting, setIsExporting] = useState(false);
  const { untitledDocument } = useTrans();
  const mediaUrl = useMediaUrl();
  const selectRef = useRef<HTMLDivElement>(null);
  // The unique export job currently owned by this modal, if any.
  const activeJobRef = useRef<ExportJob | null>(null);
  const isMountedRef = useRef(true);
  const exportAGPL = useExportAGPL?.(doc, editor);
  const [format, setFormat] = useState(
    exportAGPL?.formats.find((opt) => opt.value === 'pdf')?.value || 'html',
  );

  useEffect(() => {
    const frameId = requestAnimationFrame(() => {
      const button = selectRef.current?.querySelector<HTMLButtonElement>(
        'button, [role="combobox"]',
      );
      button?.focus();
    });
    return () => cancelAnimationFrame(frameId);
  }, []);

  // Switching to another document or leaving the page unmounts the modal:
  // the job it owns is cancelled so a late conversion or compression can
  // never trigger a stale download / notification.
  useEffect(() => {
    return () => {
      isMountedRef.current = false;
      activeJobRef.current?.cancel();
    };
  }, []);

  /**
   * Closing the modal (Cancel button, close button, click outside) while an
   * export is running cancels the job: in-flight media and style requests
   * are aborted, and any result produced afterwards is discarded.
   */
  const handleClose = () => {
    activeJobRef.current?.cancel();
    onClose();
  };

  const formatSelect = useMemo(() => {
    const formatOptions = (exportAGPL?.formats || []).concat([
      {
        label: t('Markdown'),
        value: 'markdown',
        labelDescription: t('.md(zip)'),
      },
      {
        label: t('HTML'),
        value: 'html',
        labelDescription: t('.html(zip)'),
      },
    ]);

    const formatLabels = Object.fromEntries(
      formatOptions.map((opt) => [opt.value, opt.label]),
    );

    const labels = formatOptions.map((opt) => opt.labelDescription);
    const or = t('or', {
      description:
        'Word joining the last two items of the list of available export formats',
    });
    const allFormatsLabel =
      labels.length > 1
        ? `${labels.slice(0, -1).join(', ')} ${or} ${labels[labels.length - 1]}`
        : labels.join('');

    return { formatOptions, formatLabels, allFormatsLabel };
  }, [t, exportAGPL?.formats]);

  /**
   * Runs the export as a single job with an explicit lifecycle.
   *
   * The document content and title are captured once at start. Every await
   * boundary re-checks the job state: a cancellation aborts the pending
   * media/style requests, and the result of an uninterruptible stage
   * (PDF/DOCX/ODT conversion, ZIP compression) is discarded when it
   * completes after the cancellation.
   */
  async function onSubmit() {
    if (!editor) {
      toast(t('The export failed'), VariantType.ERROR);
      return;
    }

    // A new export always supersedes a previous one: a stale job can never
    // overwrite a more recent export.
    activeJobRef.current?.cancel();
    const job = createExportJob();
    activeJobRef.current = job;

    // Fix the document content and title for the whole job lifetime.
    const documentTitle = doc.title || untitledDocument;
    const filename = getExportFilename(documentTitle);
    const blocks = structuredClone(editor.document);

    setIsExporting(true);
    let shouldClose = false;

    try {
      let downloadExtension = format === 'markdown' ? 'md' : format;
      let blobExport: Blob | undefined;

      if (format === 'pdf' || format === 'docx' || format === 'odt') {
        blobExport = await exportAGPL?.docToBlob(
          format,
          documentTitle,
          blocks,
          job.signal,
        );

        // The format conversion cannot be interrupted: discard a result
        // that completes after the job was cancelled.
        if (job.isCancelled()) {
          return;
        }
      }

      if (!blobExport && format === 'markdown') {
        const zip = new JSZip();

        const mediaFileCount = await addMediaFilesToMarkdownZip(
          blocks,
          zip,
          mediaUrl,
          exportResolveFileUrl,
          job.signal,
        );

        if (job.isCancelled()) {
          return;
        }

        const markdown = await editor.blocksToMarkdownLossy(blocks);

        if (job.isCancelled()) {
          return;
        }

        if (mediaFileCount === 0) {
          blobExport = new Blob([markdown], {
            type: 'text/markdown;charset=utf-8',
          });
        } else {
          zip.file(`${filename}.md`, markdown);
          blobExport = await zip.generateAsync({ type: 'blob' });

          // ZIP compression cannot be interrupted; discard its output if
          // the job was cancelled meanwhile.
          if (job.isCancelled()) {
            return;
          }

          downloadExtension = 'zip';
        }
      }

      if (!blobExport && format === 'html') {
        // Use BlockNote "full HTML" export so that we stay closer to the editor rendering.
        const fullHtml = await editor.blocksToFullHTML(blocks);

        if (job.isCancelled()) {
          return;
        }

        // Parse HTML and fetch media so that we can package a fully offline HTML document in a ZIP.
        const domParser = new DOMParser();
        const parsedDocument = domParser.parseFromString(fullHtml, 'text/html');

        const zip = new JSZip();

        improveHtmlAccessibility(parsedDocument, documentTitle);
        await addMediaFilesToZip(
          parsedDocument,
          zip,
          mediaUrl,
          exportResolveFileUrl,
          job.signal,
        );

        if (job.isCancelled()) {
          return;
        }

        const lang = i18next.language || fallbackLng;
        const body = parsedDocument.body;
        const editorHtmlWithLocalMedia = body ? body.innerHTML : '';

        const htmlContent = generateHtmlDocument(
          documentTitle,
          editorHtmlWithLocalMedia,
          lang,
        );

        zip.file('index.html', htmlContent);

        // CSS Styles - abortable together with the media requests.
        const cssResponse = await fetch(
          new URL(
            '../assets/export-html-styles.txt',
            import.meta.url,
          ).toString(),
          { signal: job.signal },
        );
        const cssContent = await cssResponse.text();

        if (job.isCancelled()) {
          return;
        }

        zip.file('styles.css', cssContent);

        blobExport = await zip.generateAsync({ type: 'blob' });

        // ZIP compression cannot be interrupted; discard its output if
        // the job was cancelled meanwhile.
        if (job.isCancelled()) {
          return;
        }

        downloadExtension = 'zip';
      }

      if (job.isCancelled()) {
        return;
      }

      if (!blobExport) {
        toast(t('The export failed'), VariantType.ERROR);
        return;
      }

      downloadFile(blobExport, `${filename}.${downloadExtension}`);

      toast(
        t('Your {{format}} was downloaded succesfully', {
          format,
        }),
        VariantType.SUCCESS,
      );

      shouldClose = true;
    } catch (error) {
      // A genuine cancellation aborts media/style requests on purpose and
      // must stay silent: no error toast, no partial archive. Ordinary
      // media read failures are degraded inside the media helpers, so an
      // error here is a real export failure.
      if (job.isCancelled() || isAbortError(error)) {
        return;
      }

      toast(t('The export failed'), VariantType.ERROR);
    } finally {
      if (isMountedRef.current && activeJobRef.current === job) {
        activeJobRef.current = null;
        setIsExporting(false);
      }
    }

    if (shouldClose && isMountedRef.current) {
      onClose();
    }
  }

  return (
    <Modal
      data-testid="modal-export"
      isOpen
      closeOnClickOutside
      onClose={handleClose}
      hideCloseButton
      aria-labelledby="modal-export-title"
      aria-describedby="modal-export-description"
      rightActions={
        <>
          <Button
            aria-label={t('Cancel the download')}
            variant="secondary"
            fullWidth
            onClick={handleClose}
          >
            {t('Cancel')}
          </Button>
          <Button
            data-testid="doc-export-download-button"
            aria-label={t('Download {{format}}', {
              format: formatSelect.formatLabels[format],
            })}
            variant="primary"
            fullWidth
            onClick={() => void onSubmit()}
            disabled={isExporting}
          >
            {t('Download')}
          </Button>
        </>
      }
      size={ModalSize.MEDIUM}
      title={
        <>
          <Text
            as="h1"
            $margin="0"
            id="modal-export-title"
            $size="h6"
            $align="flex-start"
            data-testid="modal-export-title"
          >
            {t('Export')}
          </Text>
          <Box $position="absolute" $css="top: 8px; right: 8px;">
            <ButtonCloseModal
              aria-label={t('Close the download modal')}
              onClick={handleClose}
            />
          </Box>
        </>
      }
    >
      <Box
        $margin={{ bottom: 'xl' }}
        $gap="1rem"
        className="--docs--modal-export-content"
      >
        <Text
          $variation="secondary"
          $size="sm"
          as="p"
          id="modal-export-description"
        >
          {t('Export your document to download in {{format}} format.', {
            format: formatSelect.allFormatsLabel,
          })}
        </Text>
        <Box ref={selectRef}>
          <Select
            clearable={false}
            fullWidth
            label={t('Format')}
            options={formatSelect.formatOptions}
            value={format}
            onChange={(options) => setFormat(options.target.value as string)}
          />
        </Box>

        {isExporting && (
          <Box
            $align="center"
            $margin={{ top: 'big' }}
            $css={css`
              position: absolute;
              left: 50%;
              top: 50%;
              transform: translate(-50%, -100%);
            `}
          >
            <Loader />
          </Box>
        )}
      </Box>
    </Modal>
  );
};
