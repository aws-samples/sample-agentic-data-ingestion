// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R8.4 — the Selected_Worksheet's wire key, its "is one set" gate, and the
 * only validation of a worksheet name that is sound with no workbook in hand.
 *
 * ## Why this is a module and not three expressions in the wizard
 *
 * Same argument as `utils/schema-field-count.ts` (`LGR-4854b3d-1`, **template /
 * ratified**): the value has a producer (the Source step's input), a gate (the
 * Step 2 navigation guard), a display (the Review step) and a wire key
 * (`handleSubmit`), and every one of those written by hand is another chance to
 * spell the rule differently. `DISPLAY_LABEL_REQUEST_KEY` in
 * `utils/schema-display.ts` is the in-tree precedent for the wire key
 * specifically (p36 R2.8): two string literals that happen to match satisfy
 * "the same key" only today.
 *
 * ## There is no worksheet LIST to offer, and that is structural
 *
 * R8.4 says the choice is made "among the Worksheet names present in that
 * Excel_Workbook". At Create-Pipeline-Wizard time **no such workbook exists**,
 * for three independent reasons:
 *
 * 1. An Excel_Workbook can only reach a pipeline through `S3_UPLOAD`, and an
 *    `S3_UPLOAD` pipeline's upload prefix is `s3://{source_bucket}/{pipeline_id}/`
 *    — created by `_provision_pipeline_resources` **after** the pipeline record
 *    is written (`handlers/pipelines_crud.py`). The destination for the workbook
 *    does not exist until the pipeline does.
 * 2. This wizard is create-only. `CreatePipelinePage` takes no route parameter
 *    and has no edit mode; its one write is a single `POST /pipelines/provision`.
 * 3. The only file the wizard uploads is a **schema** (`POST /schemas/upload`,
 *    a JSON schema blob built from `state.schemaFields`), never a data file.
 *
 * So `JdbcSourceDetectionPanel`'s shape — probe the live source, show the user
 * what was found — is unavailable here, and the asymmetry is not incidental: a
 * JDBC source is an external system addressable *before* the pipeline exists,
 * while an S3-upload workbook is an object at a prefix pipeline creation itself
 * makes. (`openpyxl` being on the shared layer, and so importable by
 * `dashboard_api`, does not help: there is no object to open.)
 *
 * This is exactly the residual design §4.4 (decision W3-D5, arm (a)) ratified
 * and closes without a Step Functions gate: the name is typed here, and a run
 * that finds it absent or wrong **refuses and names every Worksheet present**
 * (`schema_extract._read_excel_table`, R8.4 residual and R8.5). The wizard is
 * therefore the place the choice is *recorded*; the run is the place the choice
 * is *checked*, because the run is the first moment a sheet list exists.
 */

/**
 * The JSON body key `POST /pipelines/provision` carries the selection under.
 *
 * **Must equal `SELECTED_WORKSHEET_ATTRIBUTE` in
 * `shared/pipeline_record_read.py`**, which is the one definition on the Python
 * side and is used by `create_pipeline` for both the request read and the record
 * write — so the wire key and the pipelines-table attribute cannot drift from
 * each other. This constant is the TypeScript end of that one hop across
 * languages; `DISPLAY_LABEL_REQUEST_KEY` carries the same residual for the same
 * reason.
 *
 * The stakes, from `LGR-eee8ac8-5`: a second spelling here would not fail. It
 * would make the selection silently never match, so a configured pipeline would
 * keep refusing multi-sheet workbooks as if nothing had been chosen.
 */
export const SELECTED_WORKSHEET_REQUEST_KEY = 'selected_worksheet';

/** Excel's own cap on a Worksheet name's length. */
export const MAX_WORKSHEET_NAME_LENGTH = 31;

/**
 * Characters Excel forbids in a Worksheet name. A name containing one of these
 * cannot name a sheet in *any* workbook, which is what makes refusing it here
 * sound without a workbook to compare against.
 */
const FORBIDDEN_WORKSHEET_CHARS = ['\\', '/', '?', '*', '[', ']', ':'] as const;

/**
 * Whether a selection has been made at all.
 *
 * **The gate trims and the forwarded value does not** — two different rules,
 * both the producer's, and the same split `handleSubmit` already draws for the
 * Display_Label. A Worksheet name may legitimately carry padding, so trimming
 * the value would make a sheet named `" Data "` permanently unselectable; but a
 * whitespace-only *input* is a user who typed nothing, and persisting it would
 * record an empty selection.
 *
 * An empty recorded selection is the one shape that must never reach the record:
 * `schema_extract._configured_worksheet` reads the attribute with a **membership
 * test**, so `""` is a *recorded* selection naming no sheet and refuses under
 * R8.5 rather than falling back to the workbook's active sheet. Absent means
 * "no human has chosen" and lets a single-sheet workbook proceed (R8.1). Storing
 * `""` for "the user chose nothing" would therefore break every single-sheet
 * Excel pipeline created through this wizard.
 *
 * `LGR-44d601e-1` (**template / ratified**) settled the same question for the
 * Display_Label in the same direction — clearing a value REMOVES the key rather
 * than storing `""`, because "a stored `\"\"` would have to be distinguished
 * from absence at every reader".
 */
export function worksheetSelectionIsSet(raw: string | undefined | null): boolean {
  return (raw ?? '').trim().length > 0;
}

/**
 * The reason a worksheet name is impossible, or `undefined` when it is not.
 *
 * Deliberately narrow. This cannot check that the sheet **exists** — no workbook
 * is in hand — so it checks only what is true of every workbook: Excel's length
 * cap and its forbidden characters. That is not a substitute for R8.5, it is a
 * complement to it: R8.5 catches a name that is *possible but absent*, and this
 * catches a name that could never match anything, which would otherwise be
 * persisted as a key guaranteed to refuse every run of the pipeline
 * (`LGR-eee8ac8-6` — do not persist a key nothing honours).
 *
 * Returns `undefined` for an empty input: no selection is valid (R8.1), so
 * emptiness is not an error. Use `worksheetSelectionIsSet` for that question.
 */
export function worksheetSelectionError(raw: string | undefined | null): string | undefined {
  const value = raw ?? '';
  if (!worksheetSelectionIsSet(value)) return undefined;
  if (value.length > MAX_WORKSHEET_NAME_LENGTH) {
    return `A worksheet name cannot be longer than ${MAX_WORKSHEET_NAME_LENGTH} characters.`;
  }
  const offending = FORBIDDEN_WORKSHEET_CHARS.filter((c) => value.includes(c));
  if (offending.length > 0) {
    return `A worksheet name cannot contain ${offending.join(' ')} — Excel does not allow it.`;
  }
  return undefined;
}
