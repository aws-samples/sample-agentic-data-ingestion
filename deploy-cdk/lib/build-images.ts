// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Container images CDK runs at synth time to bundle assets, pinned by digest
 * (threat model T-17, R-13).
 *
 * A tag is a moving pointer: two synths of the same commit could bundle Lambda
 * layers or the dashboard with different toolchains, and a compromised tag
 * would run in the build. The tag stays in each reference for readability; the
 * digest is what Docker pulls.
 *
 * To bump an image, read the new digest and check its platforms:
 *
 *   docker buildx imagetools inspect public.ecr.aws/docker/library/node:20
 *
 * Use the index (manifest list) digest where the tag has one, so Docker still
 * selects the host platform. `latest-arm64` is a single-platform tag with no
 * index, so its digest is the linux/arm64 manifest itself; it equals the
 * linux/arm64 entry of the `:latest` index.
 *
 * Pinned by test/build-images-pinned.test.ts, which also fails if any
 * `DockerImage.fromRegistry(` in lib/ bypasses this module.
 */
import * as cdk from 'aws-cdk-lib';

/** SAM Python 3.12 build image, linux/arm64 only. Bundles the arm64 Lambda layers. */
export const SAM_PYTHON312_ARM64_BUILD_IMAGE =
  'public.ecr.aws/sam/build-python3.12:latest-arm64@sha256:d2f3901efc382ce2af90264df0d29e9243c7bf9d604038a5e0d9ed070fb19d59';

/** SAM Python 3.12 build image, multi-arch index (linux/amd64, linux/arm64). */
export const SAM_PYTHON312_BUILD_IMAGE =
  'public.ecr.aws/sam/build-python3.12:latest@sha256:de65ccf40ee2f292287dfe72a38fb7d603489bf66bae4d5e1c51ea49733d555f';

/** Node 20, multi-arch index. Builds the dashboard when host Node is unavailable. */
export const NODE20_BUILD_IMAGE =
  'public.ecr.aws/docker/library/node:20@sha256:8f693eaa7e0a8e71560c9a82b55fd54c2ae920a2ba5d2cde28bac7d1c01c9ba5';

/** A pinned build image as a CDK DockerImage. */
export function pinnedBuildImage(reference: string): cdk.DockerImage {
  if (!/@sha256:[0-9a-f]{64}$/.test(reference)) {
    throw new Error(`Build image '${reference}' is not pinned by digest (R-13)`);
  }
  return cdk.DockerImage.fromRegistry(reference);
}
