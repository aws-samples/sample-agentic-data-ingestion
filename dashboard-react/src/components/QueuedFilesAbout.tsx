// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import DetailPopover from './common/DetailPopover';

/**
 * The Queued Files header's question-mark button, shared by the Executions tab and
 * the Overview page so the explanation is spelled once.
 *
 * The sentence is p54 R9.2's: it states what is known, that a run is holding the
 * pipeline, and makes no claim about when the files will be processed. It replaced
 * "will process after current execution completes", a promise that was false
 * whenever a run sat at an approval gate past the file's redelivery budget. Do not
 * reword it back into a promise.
 *
 * Only the explanation lives here. The Failed (DLQ) sentence stays on the page,
 * because it reports that a file was lost (R9.4), and that has to be seen without
 * being looked for.
 */
export default function QueuedFilesAbout() {
  return (
    <DetailPopover header="Why files are queued" ariaLabel="About queued files" testId="queued-files-about">
      A run is currently holding this pipeline. These files are waiting for it to
      release the pipeline; how long that takes depends on that run.
    </DetailPopover>
  );
}
