// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import {
  NODE20_BUILD_IMAGE,
  SAM_PYTHON312_ARM64_BUILD_IMAGE,
  SAM_PYTHON312_BUILD_IMAGE,
  pinnedBuildImage,
} from '../lib/build-images';

/**
 * Threat model T-17, R-13: every container image CDK runs to bundle an asset
 * is pinned by digest. The Dockerfiles under agentcore/ and mcp/ are pinned by
 * tests/unit/agentcore/test_container_dependencies_pinned.py.
 *
 * Static checks only. Whether a digest exists and lists the expected platforms
 * is a registry fact, checked when bumping (see lib/build-images.ts).
 */
const LIB_DIR = path.join(__dirname, '..', 'lib');
const DIGEST = /@sha256:[0-9a-f]{64}$/;

const libSources = () =>
  fs.readdirSync(LIB_DIR).filter((f) => f.endsWith('.ts')).map((f) => ({ file: f, text: fs.readFileSync(path.join(LIB_DIR, f), 'utf-8') }));

describe('R-13 — bundling images are pinned by digest', () => {
  it.each([
    ['SAM_PYTHON312_ARM64_BUILD_IMAGE', SAM_PYTHON312_ARM64_BUILD_IMAGE],
    ['SAM_PYTHON312_BUILD_IMAGE', SAM_PYTHON312_BUILD_IMAGE],
    ['NODE20_BUILD_IMAGE', NODE20_BUILD_IMAGE],
  ])('%s carries a sha256 digest and keeps its tag', (_name, reference) => {
    expect(reference).toMatch(DIGEST);
    expect(reference.split('@')[0]).toMatch(/:[A-Za-z0-9._-]+$/);
  });

  it('pinnedBuildImage refuses a reference without a digest', () => {
    expect(() => pinnedBuildImage('public.ecr.aws/docker/library/node:20')).toThrow(/not pinned by digest/);
  });

  it('no stack calls DockerImage.fromRegistry except through build-images.ts', () => {
    const offenders = libSources()
      .filter(({ file }) => file !== 'build-images.ts')
      .flatMap(({ file, text }) =>
        text.split('\n').flatMap((line, i) =>
          !/^\s*\/\//.test(line) && /DockerImage\.fromRegistry\(/.test(line) ? [`${file}:${i + 1}: ${line.trim()}`] : [],
        ),
      );
    expect(offenders).toEqual([]);
  });

  it('no stack bundles with a Runtime\'s default (unpinned) bundling image', () => {
    const offenders = libSources().flatMap(({ file, text }) =>
      text.split('\n').flatMap((line, i) =>
        // Comments may name it (test-fixtures-stack.ts explains what it resolved to).
        !/^\s*\/\//.test(line) && /Runtime\.\w+\.bundlingImage/.test(line) ? [`${file}:${i + 1}: ${line.trim()}`] : [],
      ),
    );
    expect(offenders).toEqual([]);
  });
});
