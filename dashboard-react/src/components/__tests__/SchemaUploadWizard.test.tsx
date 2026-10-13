// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { SchemaUploadWizard } from '../SchemaUploadWizard';

// jsdom's File doesn't implement .text() — polyfill it for tests
beforeAll(() => {
  if (!File.prototype.text) {
    File.prototype.text = function () {
      return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.readAsText(this);
      });
    };
  }
});

// Helper: create a File object with given content
function createJsonFile(content: string, name = 'schema.json'): File {
  return new File([content], name, { type: 'application/json' });
}

function createCsvFile(content: string, name = 'data.csv'): File {
  return new File([content], name, { type: 'text/csv' });
}

// Simulate file selection via the native input inside Cloudscape FileUpload
async function uploadFile(file: File) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    fireEvent.change(input);
  });
}

describe('SchemaUploadWizard', () => {
  it('renders file upload area', () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);
    expect(screen.getByTestId('schema-file-upload')).toBeInTheDocument();
  });

  it('parses valid JSON Schema and shows preview table', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    const jsonSchema = JSON.stringify({
      properties: {
        id: { type: 'integer', description: 'Primary key' },
        name: { type: 'string', description: 'User name' },
      },
      required: ['id'],
    });

    await uploadFile(createJsonFile(jsonSchema));

    await waitFor(() => {
      expect(screen.getByTestId('schema-preview-table')).toBeInTheDocument();
    });

    expect(screen.getByText('id')).toBeInTheDocument();
    expect(screen.getByText('name')).toBeInTheDocument();
    expect(screen.getByTestId('use-schema-button')).toBeInTheDocument();
  });

  it('shows error for invalid JSON file', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    await uploadFile(createJsonFile('not valid json {{{'));

    await waitFor(() => {
      expect(screen.getByTestId('schema-parse-error')).toBeInTheDocument();
    });
    expect(screen.getByText(/Invalid JSON/)).toBeInTheDocument();
  });

  it('shows error for JSON without properties or array structure', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    await uploadFile(createJsonFile(JSON.stringify({ foo: 'bar' })));

    await waitFor(() => {
      expect(screen.getByTestId('schema-parse-error')).toBeInTheDocument();
    });
    expect(screen.getByText(/properties/)).toBeInTheDocument();
  });

  it('allows editing field type in preview table', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    const jsonSchema = JSON.stringify({
      properties: {
        amount: { type: 'string', description: '' },
      },
    });

    await uploadFile(createJsonFile(jsonSchema));

    await waitFor(() => {
      expect(screen.getByTestId('field-type-0')).toBeInTheDocument();
    });

    const typeInput = screen.getByTestId('field-type-0').querySelector('input') as HTMLInputElement;
    expect(typeInput.value).toBe('string');

    fireEvent.change(typeInput, { target: { value: 'number' } });

    fireEvent.click(screen.getByTestId('use-schema-button'));

    expect(onSchemaReady).toHaveBeenCalledWith(
      expect.arrayContaining([
        expect.objectContaining({ name: 'amount', type: 'number' }),
      ]),
    );
  });

  it('allows editing field description in preview table', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    const jsonSchema = JSON.stringify({
      properties: {
        email: { type: 'string' },
      },
    });

    await uploadFile(createJsonFile(jsonSchema));

    await waitFor(() => {
      expect(screen.getByTestId('field-desc-0')).toBeInTheDocument();
    });

    const descInput = screen.getByTestId('field-desc-0').querySelector('input') as HTMLInputElement;
    fireEvent.change(descInput, { target: { value: 'User email address' } });

    fireEvent.click(screen.getByTestId('use-schema-button'));

    expect(onSchemaReady).toHaveBeenCalledWith(
      expect.arrayContaining([
        expect.objectContaining({ name: 'email', description: 'User email address' }),
      ]),
    );
  });

  it('calls onSchemaReady with parsed fields when "Use This Schema" is clicked', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    const jsonSchema = JSON.stringify({
      properties: {
        col_a: { type: 'integer', description: 'Column A' },
      },
      required: ['col_a'],
    });

    await uploadFile(createJsonFile(jsonSchema));

    await waitFor(() => {
      expect(screen.getByTestId('use-schema-button')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('use-schema-button'));

    expect(onSchemaReady).toHaveBeenCalledWith([
      { name: 'col_a', type: 'integer', nullable: false, description: 'Column A' },
    ]);
  });

  it('parses CSV file and shows preview', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    await uploadFile(createCsvFile('id,name,score\n1,Alice,95.5\n2,Bob,87.0'));

    await waitFor(() => {
      expect(screen.getByTestId('schema-preview-table')).toBeInTheDocument();
    });

    expect(screen.getByText('id')).toBeInTheDocument();
    expect(screen.getByText('name')).toBeInTheDocument();
    expect(screen.getByText('score')).toBeInTheDocument();
  });

  it('shows error for unsupported file type', async () => {
    const onSchemaReady = vi.fn();
    render(<SchemaUploadWizard onSchemaReady={onSchemaReady} />);

    const file = new File(['data'], 'image.png', { type: 'image/png' });
    await uploadFile(file);

    await waitFor(() => {
      expect(screen.getByTestId('schema-parse-error')).toBeInTheDocument();
    });
    expect(screen.getByText(/Unsupported file type/)).toBeInTheDocument();
  });
});
