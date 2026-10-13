# Provenance: vendored `awslabs` AWS DataProcessing MCP Server

This directory holds the project's **single** vendored copy of the AWS Labs
DataProcessing MCP server package (P28 R19.7). All four dataprocessing-based
MCP servers (`mcp/analytics_server`, `mcp/codegen_server`, `mcp/data_processing`,
`mcp/infra_server`) import `awslabs.aws_dataprocessing_mcp_server` from this
tree — both locally (each `server.py` adds this directory to `sys.path` when no
sibling `awslabs/` copy exists) and in the container images (each server's
Dockerfile COPYies `vendor/awslabs_aws_dataprocessing_mcp_server/awslabs/` into
the image next to `server.py`).

Do not re-duplicate this tree into the per-server directories; a guard test
(`tests/unit/mcp/test_common_resource_handler_iam_gating.py::TestSingleVendoredCopy`)
fails if a second copy appears.

## Upstream

- Repository: <https://github.com/awslabs/mcp>
- Server path: `src/aws-dataprocessing-mcp-server`
- Docs: <https://awslabs.github.io/mcp/servers/aws-dataprocessing-mcp-server>
- License: Apache-2.0
- Upstream NOTICE (`src/aws-dataprocessing-mcp-server/NOTICE`), reproduced in
  this project's top-level `NOTICE`:

  ```
  awslabs.aws-dataprocessing-mcp-server
  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
  ```

## Version vendored

**Release 0.1.16** of `awslabs.aws-dataprocessing-mcp-server`, upstream commit
`57d7d67b0b51f57475321e111f7d76677740319f` (2025-11-07, release tag
`2025.11.20251107160628`).

- Vendored into this repository with the initial project import (repo commit
  `362fb51`, 2026-02-02, "adding agentglue"). That import carried no submodule or subtree metadata,
  so the base was **recovered by content comparison** on 2026-10-02: every one of
  the 60 upstream commits touching `awslabs/` was normalised with this project's
  ruff configuration and diffed against this tree. 21 of the 36 files in the
  original import are byte-identical to that commit; every other difference is
  listed below.
- **0.1.15 and 0.1.16 cannot be told apart from content.** Their `awslabs/` trees
  differ only in the `__version__` line, and the original import had overwritten
  that line. 0.1.16 is cited as the later of two code-identical releases.
- An earlier revision of this file gave the version as `0.1.0`. That string was
  never published on PyPI (the earliest release is 0.1.1) and came from the
  locally written `mcp/data_processing/pyproject.toml`, not from upstream.

## Local modifications

Every file in this tree has also been reformatted with this project's ruff
configuration (`make format` covers `mcp/`). Those are formatting-only changes
with no behavioural effect, and they are the only difference in the files not
listed here.

Files with substantive changes, each of which carries a `NOTICE: This file has
been MODIFIED` comment below its licence header:

| File (under `awslabs/aws_dataprocessing_mcp_server/`) | Change |
|---|---|
| `handlers/commons/common_resource_handler.py` | Added the `register_iam_mutation_tools: bool = True` constructor parameter (P28 R16.2, task 13.1). When `False`, the IAM-mutation tools (`add_inline_policy`, `create_data_processing_role`) are never registered; read-only IAM tools and S3 tools remain. The infra and legacy data_processing servers pass `False`. |
| `handlers/athena/athena_data_catalog_handler.py`, `athena_query_handler.py`, `athena_workgroup_handler.py` | Tool-method return annotations changed from `Union[<response models>]` to `mcp.types.CallToolResult`, with imports adjusted. |
| `handlers/emr/emr_ec2_cluster_handler.py`, `emr_ec2_instance_handler.py`, `emr_ec2_steps_handler.py` | Same annotation change. |
| `handlers/glue/crawler_handler.py`, `data_catalog_handler.py`, `glue_commons_handler.py`, `glue_etl_handler.py`, `interactive_sessions_handler.py`, `worklows_handler.py` | Same annotation change. In `data_catalog_handler.py` five `List*Response` imports that it left unused were removed. (`worklows` is upstream's spelling.) |

The annotation change covers 25 tool methods across those 12 files. It was made
locally: no upstream branch, tag or PR head checked carries those file contents.
Applying exactly that transformation to upstream 0.1.16 reproduces all 12 files
as originally imported. The likely reason is compatibility with `mcp` releases
above 1.18.0, which upstream 0.1.16 capped (`mcp[cli]>=1.11.0,<=1.18.0`); that
reason is inferred, not recorded.

Locally added (not part of upstream):

- `handlers/emr/__init__.py` — a package marker for `handlers/emr/`. Upstream
  ships no `__init__.py` there.

Restored to upstream content on 2026-10-02, so no longer modified:

- `awslabs/__init__.py` had been replaced by a one-line comment, which dropped
  upstream's licence header. It now matches upstream again.
- `awslabs/aws_dataprocessing_mcp_server/__init__.py` had been reduced to a
  package marker carrying `__version__ = "0.1.0"`. It now matches upstream again,
  with `__version__ = "0.1.16"`. `utils/aws_helper.py` reads that value into the
  boto3 user-agent suffix (`awslabs/mcp/aws-dataprocessing-mcp-server/<version>`).

## Not vendored

The tree was trimmed to the modules the four server profiles import. Upstream
files under `src/aws-dataprocessing-mcp-server/` at the commit above that are
**not** here: `awslabs/aws_dataprocessing_mcp_server/server.py`; the packaging
and build files (`pyproject.toml`, `uv.lock`, `uv-requirements.txt`,
`.python-version`, `Dockerfile`, `docker-healthcheck.sh`, `.gitignore`);
`README.md`, `CHANGELOG.md`, `NOTICE`; and `tests/`. A copy of the
AgentCore-adapted `pyproject.toml` lives in `mcp/data_processing/`.

`LICENSE` here is the complete Apache License 2.0 text as published at
apache.org. Upstream's own copy is the same licence text with the closing
"END OF TERMS AND CONDITIONS" line and the appendix omitted.

At consolidation time (P28 task 15.4) the four per-server copies were diffed
wholesale (`diff -rq`, excluding `__pycache__`) and were **byte-identical**;
no per-server reconciliation was needed.

## Related code outside this tree

Upstream's `server.py` is not vendored, but two files outside this tree are
adapted from it, and both now say so in their headers:

- `mcp/data_processing/server.py` follows upstream's shape directly: about 115
  of its 189 lines match, mostly the handler import block and the handler
  instantiation. Changes: AgentCore host and transport settings, and
  `register_iam_mutation_tools=False` on the common resource handler.
- `mcp/analytics_server/server.py` was split out of `mcp/data_processing/server.py`
  and keeps the Athena and EMR part of that structure.

Each keeps this project's MIT-0 header and adds a notice naming the upstream
file, commit `57d7d67b`, the Apache License 2.0, and that it was modified. The
top-level `NOTICE` lists them under this component.

`mcp/codegen_server/server.py`, `mcp/infra_server/server.py` and
`mcp/s3_tables/server.py` were compared against upstream and are this
project's own code: they register handlers through a different structure and
share only the import paths every consumer of the package must use.

## Updating

There is no automated sync. To pick up upstream changes: diff this tree
against the current upstream `src/aws-dataprocessing-mcp-server/awslabs/`,
re-apply the local modifications listed above, keep the per-file notices, and
update this file. Note that releases from 0.1.18 onward restructure the handlers
(roughly 5,800 changed lines against this tree), so an update is a re-vendoring,
not a patch. Upstreaming the local modifications is explicitly out of scope
(P28 R20.5).
