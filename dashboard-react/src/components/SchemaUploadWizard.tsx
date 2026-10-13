// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useCallback } from 'react';
import Table from '@cloudscape-design/components/table';
import FileUpload from '@cloudscape-design/components/file-upload';
import Alert from '@cloudscape-design/components/alert';
import Input from '@cloudscape-design/components/input';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { parseSchemaFile } from '../utils/schema-parser';
import type { SchemaField, SchemaParseResult } from '../utils/schema-parser';

interface Props {
  onSchemaReady: (fields: SchemaField[]) => void;
  /**
   * p35 R4.3 — the name of the file the user actually chose.
   *
   * `onSchemaReady` carries the parsed *fields*, and a consumer that uploads
   * them has to name the upload something. `SchemaManagementPage` had nothing
   * to name it with, so it rebuilt the schema as `new File([...],
   * 'schema.json')` and every "Upload with preview" landed as
   * `shared/schema_<timestamp>.json` — distinct schemas collapsed into one
   * indistinguishable family.
   *
   * Additive and optional **on purpose**: widening `onSchemaReady` to a second
   * positional argument would move every existing
   * `toHaveBeenCalledWith(fields)` assertion in this component's tests, whose
   * subject is the commit rather than the filename. A consumer that does not
   * care (`WizardStepSchema`, which commits into wizard state and never
   * uploads) simply omits it.
   */
  onFileChosen?: (fileName: string) => void;
}

export function SchemaUploadWizard({ onSchemaReady, onFileChosen }: Props) {
  const [files, setFiles] = useState<File[]>([]);
  const [parseResult, setParseResult] = useState<SchemaParseResult | null>(null);
  const [fields, setFields] = useState<SchemaField[]>([]);

  const handleFileChange = useCallback(
    async (selectedFiles: File[]) => {
      setFiles(selectedFiles);
      if (selectedFiles.length === 0) {
        // File cleared. Local preview state is reset, but the parent is NOT
        // notified: `onSchemaReady` is a commit, and clearing the picker is not
        // one. The empty case is caught by the Step 4 navigation guard in
        // CreatePipelinePage instead.
        setParseResult(null);
        setFields([]);
        return;
      }
      // R4.3 — before the parse and before any commit, so a consumer that
      // uploads on `onSchemaReady` already knows what the user called the file.
      // Reported even when the parse then fails: the name is a fact about the
      // selection, not about the parse.
      onFileChosen?.(selectedFiles[0].name);
      const result = await parseSchemaFile(selectedFiles[0]);
      setParseResult(result);
      if (result.success) {
        setFields(result.fields);
        // Auto-commit the parsed fields to the parent. Before this, the ONLY
        // path that reached `onSchemaReady` was the "Use This Schema" button,
        // so a user who uploaded a file and clicked straight through to Review
        // left `schemaFields` empty while `schemaSource` read 'upload' — the
        // review step then showed "Uploaded / 0 fields" and submit failed with
        // a 400. The button below is still meaningful: the inline type and
        // description edits (`handleFieldEdit`) mutate local state only, and
        // since auto-commit fires once per successful parse, a later button
        // click always overwrites it with the edited fields.
        onSchemaReady(result.fields);
      } else {
        // Failed parse commits nothing — the parent keeps whatever it had and
        // the navigation guard still blocks an empty advance.
        setFields([]);
      }
    },
    [onSchemaReady, onFileChosen],
  );

  const handleFieldEdit = (index: number, key: keyof SchemaField, value: string | boolean) => {
    setFields((prev) =>
      prev.map((f, i) => (i === index ? { ...f, [key]: value } : f)),
    );
  };

  return (
    <SpaceBetween size="l">
      <FileUpload
        onChange={({ detail }) => handleFileChange(detail.value)}
        value={files}
        accept=".json,.csv"
        constraintText="Accepted formats: JSON Schema, JSON array, CSV"
        i18nStrings={{
          uploadButtonText: (multiple) => (multiple ? 'Choose files' : 'Choose file'),
          dropzoneText: (multiple) =>
            multiple ? 'Drop files to upload' : 'Drop file to upload',
          removeFileAriaLabel: (fileIndex) => `Remove file ${fileIndex + 1}`,
          limitShowFewer: 'Show fewer files',
          limitShowMore: 'Show more files',
          errorIconAriaLabel: 'Error',
        }}
        data-testid="schema-file-upload"
      />

      {parseResult?.error && (
        <Alert type="error" data-testid="schema-parse-error">
          {parseResult.error}
        </Alert>
      )}

      {fields.length > 0 && (
        <>
          <Table
            data-testid="schema-preview-table"
            columnDefinitions={[
              { id: 'name', header: 'Field Name', cell: (f) => f.name },
              {
                id: 'type',
                header: 'Type',
                cell: (f) => {
                  const idx = fields.indexOf(f);
                  return (
                    <Input
                      value={f.type}
                      onChange={({ detail }) => handleFieldEdit(idx, 'type', detail.value)}
                      data-testid={`field-type-${idx}`}
                    />
                  );
                },
              },
              {
                id: 'nullable',
                header: 'Nullable',
                cell: (f) => (f.nullable ? 'Yes' : 'No'),
              },
              {
                id: 'description',
                header: 'Description',
                cell: (f) => {
                  const idx = fields.indexOf(f);
                  return (
                    <Input
                      value={f.description}
                      onChange={({ detail }) => handleFieldEdit(idx, 'description', detail.value)}
                      placeholder="Add description"
                      data-testid={`field-desc-${idx}`}
                    />
                  );
                },
              },
            ]}
            items={fields}
            variant="embedded"
          />
          <Button
            variant="primary"
            onClick={() => onSchemaReady(fields)}
            data-testid="use-schema-button"
          >
            Use This Schema
          </Button>
        </>
      )}
    </SpaceBetween>
  );
}
