# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one definition of what may go inside a Glue ``UpdateTable`` ``TableInput``.

Glue ``UpdateTable`` **replaces** the stored table with the ``TableInput``
given — it does not merge — so any correct writer has to be a
read-modify-write: ``glue:GetTable``, then a ``TableInput`` built from what was
read. That read returns fields ``UpdateTable`` rejects inside a ``TableInput``
(``DatabaseName``, ``CreateTime``, ``UpdateTime``, ``CreatedBy``, ``VersionId``,
``CatalogId``, ``IsRegisteredWithLakeFormation``), so handing the read
``Table`` straight back fails live. This module strips them, once.

**Why one definition and not a copy in each Lambda.** Two writers now perform
that read-modify-write against the **same** Glue mirror table:

1. ``create_infrastructure``'s registration refresh (p35 R7.6/R7.7), which
   writes ``Columns`` and preserves a populated ``metadata_location``;
2. ``job_completion``'s ``_populate_mirror_metadata_location`` (p35 R7.3),
   which writes ``metadata_location`` and preserves the ``Columns``.

The allowlist decides which of the stored table's keys survive each write. Two
lists that disagree produce two different tables from one read, and the
disagreement is **silent** — Glue accepts the narrower write and simply drops
whatever the narrower writer did not name. That is the same class of failure
``LGR-eee8ac8-5`` (**template / ratified**) put the processed-file fingerprint
in ``shared/`` to prevent: "a second definition in either Lambda would disable
the check silently by never matching, so the fingerprint and row key live in
one shared module imported by both."

``LGR-cad8032-1``'s one-definition principle is about **addresses**, and this
is not an address — so that record does not compel this module. The reason
above is independent of it: it is about two writers producing one object.

**An allowlist, not a denylist of the read-only fields.** A denylist breaks
silently the day Glue adds an eighth read-only field: the writer would pass it
through and the call would start failing for a reason no test names. The
allowlist is the API's own ``TableInput`` member list, so a new *writable*
field is the thing that needs adding here — visible as a field that stops being
carried forward, not as a rejected write.

**This module carries the key list and the projection, and nothing else.** No
defaults, no ``Name`` fixing, no ``Parameters`` policy: what a given writer
wants to *change* is that writer's own business, and putting a default here
would make one writer's policy silently apply to the other.
"""

from collections.abc import Mapping
from typing import Any, Final

#: The keys ``glue:UpdateTable`` accepts inside ``TableInput``, in the API's own
#: order. Deliberately excludes every field a ``glue:GetTable`` response adds
#: around them — see the module docstring for why this is an allowlist.
GLUE_TABLE_INPUT_KEYS: Final[tuple[str, ...]] = (
    "Name",
    "Description",
    "Owner",
    "LastAccessTime",
    "LastAnalyzedTime",
    "Retention",
    "StorageDescriptor",
    "PartitionKeys",
    "ViewOriginalText",
    "ViewExpandedText",
    "TableType",
    "Parameters",
    "TargetTable",
)


def glue_table_input_from_stored(stored_table: Mapping[str, Any]) -> dict[str, Any]:
    """Project a ``glue:GetTable`` ``Table`` onto a legal ``TableInput``.

    Args:
        stored_table: The ``Table`` member of a ``glue:GetTable`` response, or
            ``{}`` when nothing was read.

    Returns:
        A new ``dict`` carrying only the keys in :data:`GLUE_TABLE_INPUT_KEYS`
        that the stored table actually has. Absent keys stay absent rather than
        becoming ``None``, because ``UpdateTable`` treats a present key as a
        value to store — a ``None`` ``StorageDescriptor`` is a request to
        erase it, which is exactly the clobber the read exists to prevent.

        The values are the read's own objects, not copies. A caller that
        intends to modify one (``Parameters``, ``StorageDescriptor``) should
        copy it first, so the projection stays a description of what was read.
    """
    return {key: stored_table[key] for key in GLUE_TABLE_INPUT_KEYS if key in stored_table}
