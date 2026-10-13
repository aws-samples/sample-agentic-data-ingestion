# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which ``write_mode`` values each target type actually implements.

ONE policy, read by the three places a write mode can enter the system:
``create_pipeline`` and ``edit_pipeline`` in the dashboard API refuse an
unsupported pair with a 400 before any write, and ``code_assembly.generate_code``
refuses one before rendering, so a record stored before these checks existed
fails its run with a reason instead of running the wrong write.

Why it exists (2026-10-10). Only the S3 Tables writer implements ``merge``
(``MERGE INTO`` on ``merge_key``). The other writers branch on one value and
send everything else down their ``else`` arm, so a stored ``merge`` ran as:

* JDBC: ``mode="append"`` (``_partials/_write_jdbc.py.j2``), duplicating rows;
* S3 Parquet: ``mode("overwrite")`` (``_partials/_write_s3_parquet.py.j2``),
  replacing all existing data on every run;
* Glue Catalog: the plain catalog write (``_partials/_write_glue_catalog.py.j2``).

Each run reported success. Create accepted ``merge`` for every target, and the
Edit Pipeline dialog offered it for every target while edit validated nothing.

Pure: no AWS client and no import beyond the standard library, so
``code_assembly`` can import it without owning a client (``LGR-1e135d2-2``).

Values are compared verbatim, never case-folded, because the templates test them
with exact Jinja2 equality: ``"Overwrite"`` renders the ``else`` arm, so treating
it as ``overwrite`` here would approve a write that never happens.
"""

from __future__ import annotations

#: Every write mode any target implements, in the order messages list them.
WRITE_MODES: tuple[str, ...] = ("overwrite", "append", "merge")

#: The persisted ``target_config["type"]`` spellings of an S3 Tables target,
#: lower-cased. Production stores the wizard's upper-case spellings
#: (``NEW_S3_TABLE_BUCKET``, ``EXISTING_S3_TABLE_BUCKET``); ``s3_tables`` is
#: the normalised form and ``s3_table_bucket`` the one ``job_builders`` also
#: accepts.
S3_TABLES_TARGET_TYPES: frozenset[str] = frozenset(
    {"s3_tables", "new_s3_table_bucket", "existing_s3_table_bucket", "s3_table_bucket"}
)

_WITHOUT_MERGE: tuple[str, ...] = ("overwrite", "append")


def supported_write_modes(target_type: object) -> tuple[str, ...]:
    """The write modes the writer for ``target_type`` implements.

    ``merge`` only for S3 Tables; ``overwrite`` and ``append`` for every other
    target, including an absent or unrecognised type, which gets no template at
    all and so can implement nothing beyond the two plain writes.
    """
    normalised = target_type.strip().lower() if isinstance(target_type, str) else ""
    return WRITE_MODES if normalised in S3_TABLES_TARGET_TYPES else _WITHOUT_MERGE


def write_mode_refusal(target_type: object, write_mode: object) -> str | None:
    """Why ``write_mode`` cannot be used for ``target_type``, or ``None`` if it can.

    The message names the value as given and the modes that would work, so the
    caller can fix the request without reading code.
    """
    supported = supported_write_modes(target_type)
    if isinstance(write_mode, str) and write_mode in supported:
        return None
    allowed = ", ".join(supported)
    if write_mode == "merge":
        type_label = target_type if isinstance(target_type, str) and target_type else "this"
        return (
            f"write_mode 'merge' is not supported for a {type_label} target: only S3 Tables "
            f"targets implement merge. Must be one of: {allowed}"
        )
    return f"Invalid write_mode: {write_mode!r}. Must be one of: {allowed}"
