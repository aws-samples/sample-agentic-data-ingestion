# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One definition of the pipelines-table record read (p47 R12.4).

What is shared, and what deliberately is not
--------------------------------------------
Shared here: the *mechanism* — ``get_item`` on the pipelines table,
:func:`step_errors.raise_for_read_client_error` for the ``ClientError``
classification, the ``"Item" in response`` test, and the ``logger.error`` that
keeps a missing *table* distinguishable from a missing *record*.

NOT shared: each caller's **contract**. The not-found value is a parameter, so
``test_code`` and ``schema_mapping`` keep returning ``None`` and ``create_code``
keeps returning ``{}``. Those are three different answers to "what does absence
mean here", and p47 R12.1 / ``LGR-b2f79ef-8`` make ``test_code``'s ``None``
load-bearing: ``{}`` has no ``target_schema_s3_key`` either, so a missing
pipeline record would silently present as "no target schema" and report
``verified: False`` for a run whose real problem is a missing record. Collapsing
the sentinels here would reintroduce exactly that.

Three of the five pipeline-record reads call this. ``schema_compare`` and
``create_infrastructure`` keep their own copies knowingly — ``LGR-b2f79ef-5``'s
scope field says verbatim "explicitly NOT
``…_schema_compare/**`` or ``…_create_infrastructure/**``", and records the
exclusion as "deferred convergence, not an oversight". p47 meets neither of that
record's two release triggers, so the two copies stay. See
``decision-records/E2-shared-read-primitive.txt``.

Why a table *object* and not a table name
-----------------------------------------
The five pipeline-record reads acquire their table three different ways —
module scope (``schema_compare``, ``create_infrastructure``), in-function
(``test_code``, ``schema_mapping``) and as an injected parameter
(``create_code/mapping_resolution``). The three that adopt this module use the
last two. Taking an already-constructed resource parameterises cleanly over all
three shapes, so the two copies could join later without changing this signature,
and it leaves every caller's acquisition
timing, and therefore every existing test's patch target
(``patch.object(_mod, "dynamodb", …)``, ``patch.object(_mod, "pipelines_table", …)``),
working untouched. A primitive that took a name and built the resource itself
would move acquisition at two sites and break those seams — which is what p47
R12.8 forbids.

Why each caller keeps its own ``def``, and why table/logger/classifier are all
arguments
------------------------------------------------------------------------------
Roughly 60 test sites patch ``get_pipeline_metadata`` as a module attribute of
the Lambda's own module (``patch.object(lambda_function, …)`` /
``@patch("lambda_function.get_pipeline_metadata")``), resolved at call time. So
every caller keeps its own ``def`` and calls this from inside it; none is
replaced by a bare import alias or an import-time-bound partial, either of which
would break those patches. ``LGR-b2f79ef-10`` records the same reasoning for a
sibling shim, and p47 R12.2 for ``create_code``'s delegating wrapper.

The same reasoning is why the table, the logger **and the classifier** arrive as
arguments rather than being resolved inside this module: each is read from the
caller's globals at call time, so ``patch.object(_mod, "dynamodb", …)``,
``patch.object(_mod, "logger", …)`` and
``patch.object(_mod, "raise_for_read_client_error", …)`` all keep intercepting.
That last one is not hypothetical — ``test_code``'s
``test_an_already_classified_step_failure_is_not_reclassified_on_the_way_out``
patches the classifier on the Lambda module to prove an already-classified
failure is not re-labelled on the way out. Resolving the classifier here instead
would leave that patch inert and the test passing **vacuously**, which is the
failure mode p47's finding 20 records: a test that still passes while no longer
testing anything.

One-definition argument, and its honest limit
---------------------------------------------
Argued from ``LGR-eee8ac8-5`` ("a second definition in either Lambda would
disable the check silently by never matching, so the fingerprint and row key
live in one shared module imported by both") and explicitly **not** from
``LGR-cad8032-1`` — ``LGR-aa15a1b-1`` already drew that line: that record's
one-definition principle "is about addresses, and a key list is not an address".
A read mechanism is not an address either.

The limit, stated rather than papered over: ``LGR-eee8ac8-5``'s force comes from
two copies being *checked against each other*, where a disagreement disables a
feature silently. Pipeline-record reads are not in that relationship — no copy
validates another, and a drifted copy fails visibly in its own Lambda. So that
record supports one definition here on **maintenance** grounds (one place to fix
the classification, one place the log cannot be dropped from) and does not
supply the silent-disagreement failure mode it supplies for the fingerprint.
"""

from __future__ import annotations

from typing import Any, Final, Protocol

from botocore.exceptions import ClientError

from step_errors import raise_for_read_client_error

#: Partition-key attribute of the pipelines table.
PIPELINE_ID_ATTRIBUTE: Final = "pipeline_id"

#: Attribute holding the Selected_Worksheet for an Excel-sourced pipeline
#: (p55 R8.3, design §Data Models). One definition, here, for
#: ``LGR-eee8ac8-5``'s reason rather than for tidiness: this attribute is read
#: in one Lambda and written in another, and the two are only ever compared
#: through this string. A second spelling would not fail — it would make the
#: worksheet selection silently never match, so a configured pipeline would
#: keep refusing multi-sheet workbooks as if nothing had been chosen. That is
#: the silent-disagreement failure mode, which is exactly the case that record
#: says warrants one shared definition.
#:
#: Its ABSENCE is meaningful and must be read with a membership test against
#: the record — ``SELECTED_WORKSHEET_ATTRIBUTE in record`` — never by coercing
#: the value, because an empty string is a *recorded* selection naming no sheet
#: and must refuse (R8.5) rather than fall back to the workbook's active sheet.
#: This is the same distinction ``unmapped_fields``' ``default_value`` already
#: draws between a recorded null and an absent key.
#:
#: The writer is p55 task 5.2 and does not exist yet; the reader is
#: ``schema_extract``. Deliberately NOT a key this module reads — the primitive
#: below returns the whole record and each caller reads its own attributes.
SELECTED_WORKSHEET_ATTRIBUTE: Final = "selected_worksheet"


class ErrorLogger(Protocol):
    """The only thing this module needs from a logger.

    Structural, because the callers hold an ``aws_lambda_powertools.Logger``,
    which is not a ``logging.Logger`` subclass.
    """

    # ``**kwargs: Any`` rather than ``object``: ``aws_lambda_powertools.Logger``
    # and ``logging.Logger`` both type named keyword-only params (``exc_info``,
    # ``stack_info``, ``stacklevel``, ``extra``) more narrowly than ``object``,
    # so an ``object``-typed catch-all makes neither a structural subtype.
    def error(self, msg: object, *args: object, **kwargs: Any) -> None: ...


class ReadErrorClassifier(Protocol):
    """The shape of :func:`step_errors.raise_for_read_client_error`.

    A Protocol rather than ``Callable`` because ``operation`` is keyword-only and
    that is part of the contract the callers' tests assert against.
    """

    def __call__(self, exc: ClientError, step_name: str, *, operation: str) -> None: ...


def pipeline_record_key(pipeline_id: str) -> dict[str, str]:
    """The full DynamoDB key for one pipelines-table record.

    One definition of the key shape, so a caller cannot drift it. Pinned by
    ``test_code``'s characterization test, which asserts
    ``get_item`` was called with exactly ``Key={"pipeline_id": …}``.
    """
    return {PIPELINE_ID_ATTRIBUTE: pipeline_id}


def read_pipeline_record[NotFoundT](
    pipelines_table: Any,
    pipeline_id: str,
    *,
    step_name: str,
    operation: str,
    not_found: NotFoundT,
    logger: ErrorLogger,
    classify: ReadErrorClassifier = raise_for_read_client_error,
) -> dict[str, Any] | NotFoundT:
    """Read one pipelines-table record, or return ``not_found``.

    Three outcomes, and the split between them is the whole point (P28 R17.3):

    - the record exists → the item, returned unchanged;
    - it genuinely does not exist (no ``Item``, or a ``ClientError`` whose code
      is in :data:`step_errors.NOT_FOUND_CLIENT_ERROR_CODES`) → ``not_found``;
    - anything else → raises. Transient infra codes raise
      :class:`step_errors.TransientStepFailure` so Step Functions Retry fires;
      everything else raises :class:`step_errors.StepFailure`. An infrastructure
      error must never masquerade as a missing record.

    ``except ClientError`` is deliberately this narrow. ``LGR-b2f79ef-13`` is the
    standing example of the cost of widening it: a blanket ``except Exception``
    around a read re-labelled an already-classified ``StepFailure`` as a bare
    ``ValueError``, so the classification this function exists to make was thrown
    away one frame later. Do not widen it, and do not wrap a call to it in
    anything broader.

    Args:
        pipelines_table: An already-constructed DynamoDB ``Table`` resource. See
            the module docstring on why this is not a table name.
        pipeline_id: Partition key value.
        step_name: Canonical step name for the raised failure — the caller's own
            step, not this module's.
        operation: Read-operation name recorded in ``details["operation"]``.
        not_found: The caller's not-found value, returned on both not-found
            paths. Not defaulted: absence means different things to different
            callers and this module must not pick for them. ``NotFoundT`` binds
            to ``None`` for ``test_code`` / ``schema_mapping`` and to ``dict``
            for ``create_code``, so each caller's declared return type survives.
        logger: The **caller's** logger. Required rather than optional so no
            caller can silently drop the not-found-``ClientError`` log.
            ``LGR-b2f79ef-8`` keeps that log because
            ``ResourceNotFoundException`` is a not-found code, so a missing
            *table* and a missing *record* both return the sentinel and the log
            is the only place the two stay distinguishable.
        classify: The ``ClientError`` classifier. Defaults to
            :func:`step_errors.raise_for_read_client_error`; callers pass their
            own module-global reference so
            ``patch.object(_mod, "raise_for_read_client_error", …)`` keeps
            intercepting. See the module docstring.
    """
    try:
        response = pipelines_table.get_item(Key=pipeline_record_key(pipeline_id))
    except ClientError as e:
        classify(e, step_name, operation=operation)
        # Reached only when the code was a not-found one, i.e. the table (or
        # index) is missing. P21 Req 6.4's log, on the one path here that
        # catches an exception.
        logger.error(f"Pipelines table not found getting metadata for {pipeline_id}: {e}")
        return not_found
    if "Item" in response:
        item: dict[str, Any] = response["Item"]
        return item
    # No exception was caught on this path — `get_item` succeeded and returned
    # no `Item` — so Req 6.4's antecedent ("WHERE a handler catches an
    # exception") does not apply and no log is owed here.
    return not_found
