# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Quality_Verdict mode — what the generated Glue job actually does about DQ (p30 R7.2).

This module answers one question about a run: **did anything gate the write on
data quality, and if so, what did it withhold?** It is pure — no AWS clients,
no I/O — so the answer is a function of the two facts the code-generation step
already recorded.

Why this exists at all
----------------------
By default, four of the five (source_type, target_type) combinations this system
generates code for run **no** data-quality evaluation inside the Glue job, and
the fifth runs one that removes failing rows without ever failing the job.
Nothing outside the job enforces anything either: ``job_completion`` closes a
run on Glue state alone and contains no reference to quality. So an empty Data
Quality panel in the dashboard can mean three materially different things —
never configured, configured but degraded to an empty ruleset, or evaluated and
silently skipped — and none of them leaves a state row behind to say which.
This record is the artifact that exists when no evaluation happened.

p64 R6.6 / O4: the template name is no longer sufficient on its own
-------------------------------------------------------------------
p64 R6.1/R6.2 moved the ``EvaluateDataQuality`` block into
``_partials/_data_quality.py.j2`` and included it from **all five** templates,
which fired ``LGR-eee8ac8-6``'s ``revisit_when`` verbatim ("a second template
gains an ``EvaluateDataQuality`` block"). This is that revisit.

Two facts changed and they pull in opposite directions. The block now exists in
every template — so "the other four ignore ``dqdl_rules``" stopped being true —
but the block is now **gated** on a per-pipeline opt-in (R6.6), resolved once by
``code_assembly.resolve_data_quality_enabled`` and defaulting to the posture each
template had *before* p64: on for ``glue_job_s3_tables.py.j2``, off for the other
four. So for any pipeline that has not opted in, the template name still answers
the question correctly, and every verdict this module produced before p64 is
unchanged.

What the rename records is that :data:`DQ_DEFAULT_ON_TEMPLATES` is now a
statement about the **default**, not about which files contain the block. The
authoritative answer is ``data_quality_enabled``, which ``create_code`` resolves
at render time and records on its own state row; this module takes it as an
argument. It is NOT re-derived here — that would duplicate both
``TEMPLATE_RULES`` and the opt-in resolution in a module free to drift, which is
the alternative ``LGR-eee8ac8-6`` already rejected. ``None`` means the caller
could not establish it (a state row written before p64), and falls back to the
default-posture set, which is exactly right for such a row: it predates the
opt-in, so it ran the default.

What it is NOT for
------------------
It is **not** the source of the dashboard's per-run quality label. That label is
derived, more strongly, from the DQ state row's own producer stamp
(``source: "glue_inline_dq"`` vs ``agent_type: "data_quality"``), because an
on-demand agent check ``put_item``s over the same key an inline evaluation
writes — so a codegen-time mode can be stale for the row a reader is looking at.
This record describes **the generated code**, not the run.

The key is the (source_type, target_type) pair, resolved via the template
------------------------------------------------------------------------
Verdict mode is NOT a function of target type alone. ``TEMPLATE_RULES``'
first entry is ``("JDBC", "*", "glue_job_jdbc_source.py.j2")`` and it precedes
every ``S3_UPLOAD`` entry, so a JDBC source routes to the JDBC template for
*every* target — and that template runs no DQ by default. The same target type
is therefore DQ-bearing under one source and not under the other.

Rather than re-deriving the pair here (which would duplicate ``TEMPLATE_RULES``
in a second module, free to drift), this module keys on the **template that was
actually rendered**, which is that ruleset's own resolution of the pair and is
recorded by ``create_code`` on its state row. Membership in
:data:`DQ_DEFAULT_ON_TEMPLATES` is thus the pair test, applied to the pair's
resolved value. It also covers the full-AI fallback path
(``template_used == "fallback_full_ai_v1"``), which renders no template and
therefore no DQ — a case an implementation keyed on (source, target) alone
would answer wrongly, and one that no opt-in can change, because there is no
template to gate.

``enforced`` requires three conditions, not one
-----------------------------------------------
1. **the DQ block was enabled for this pipeline.** By default that means the
   source is ``S3_UPLOAD`` and the target normalises to ``s3_tables`` — together
   exactly "the rendered template is ``glue_job_s3_tables.py.j2``" — because that
   is the one template whose DQ block is on without an opt-in. A pipeline that
   opts in (p64 R6.6) satisfies this condition on any of the five templates, and
   ``data_quality_enabled`` is how the caller says so, **and**
2. the rendered template is one that carries the block at all — every template
   does since p64 R6.1, but the full-AI fallback path renders none, **and**
3. the **generated DQDL ruleset** is non-empty. ``_generate_dqdl_rules``
   returns ``""`` when there are no field mappings, and the DQ partial's body is
   gated on ``{% if data_quality_enabled and dqdl_rules … %}`` — so a
   mapping-less pipeline renders no DQ at all, on any target type, opted in or
   not.

Condition 3 is **not** derivable from ``create_quality_checks``' own
``quality_rules`` / ``rule_summary.total_rules``: those are a different rule
vocabulary, built by a different function from the target schema, and are
routinely non-empty while the rendered DQDL is empty. The count this module
reports is named ``dqdl_rule_count`` for that reason — it counts rules in the
DQDL string the job actually carries, and must not be conflated with the
``rule_count`` of R7.4's zero-rule degradation.

What ``enforced`` does and does not claim
-----------------------------------------
``enforced`` means **failing rows were withheld from the write** — the DQ block
splits the frame and the job writes only the passing rows, quarantining the
rest. It does **not** mean a quality failure stops the run: the evaluation is
wrapped in ``except Exception`` and the job still reports success. Hence
``blocks_run`` is always ``False``; it is recorded rather than omitted because
"enforced" invites exactly the opposite reading. Two further limits:

* **Dataset-level rules are advisory even here.** A rule that fails without
  producing failing rows (e.g. ``RowCount > 0``) withholds nothing, which is
  why ``enforcement_scope`` says ``row_level`` rather than implying a whole-run
  gate.
* **The quarantine grant on the two non-S3_TABLES target types is now CLOSED.**
  p64 2b.2 found that the per-pipeline Glue execution role granted
  ``s3:PutObject`` on ``adi-generated-code-*`` only for the S3_TABLES and
  GLUE_CATALOG branches; the JDBC and S3_PARQUET branches granted ``GetObject``
  only, so an opted-in quarantine write on either would have failed with
  AccessDenied. It was unreachable while nothing could opt in. p64 2c.1 made the
  opt-in settable and therefore added the grant in the same change — a dedicated
  ``s3:PutObject``-on-``adi-generated-code-*/*`` statement in each of those two
  branches of ``create_glue_pipeline/iam_roles.py``. So ``enforced`` on those two
  target types no longer implies a possible run-time refusal at the quarantine
  write. (Read the DQ state row for what actually happened regardless — that is
  the point of the next bullet.)
* **The enforced combination can still degrade silently at runtime.** If
  ``awsgluedq`` is missing from the job classpath the block logs a warning and
  skips, writing the full frame. This is a statement about generated code, not
  a guarantee about a run — read the DQ state row for what actually happened.

Layer 0 in ``shared/__init__._LAYER_MAP`` — no shared-module dependencies.
"""

from typing import Any, Final

# The two values R7.2 allows on the state row.
MODE_ENFORCED = "enforced"
MODE_ADVISORY = "advisory"

# ---------------------------------------------------------------------------
# The Quality_Config attribute names (p64 R7 / R10.4, design C7)
# ---------------------------------------------------------------------------
#
# ONE definition of each name, here, because the writer and the readers live in
# DIFFERENT Lambdas and are only ever compared through these strings
# (``LGR-eee8ac8-5``). ``dashboard_api``'s ``create_pipeline`` writes the
# sub-object; ``create_code``'s ``code_assembly`` reads it. A second spelling on
# either side would not fail loudly — it would make every opt-in silently never
# match, so an operator's dedup or DQ toggle would appear saved and do nothing.
# That is the same defect class the Approval_Config casing gap was, one attribute
# over, and it is why these are not two literals.
#
# WHY HERE. This module already owns the DQ-posture vocabulary
# (:data:`DQ_DEFAULT_ON_TEMPLATES` carries the same "single source of this set"
# rule) and it is Layer 0 — pure, no AWS client — which is what lets
# ``code_assembly`` import it without breaking
# ``test_code_assembly_still_owns_no_aws_client`` (``LGR-1e135d2-2``).
# ``shared/pipeline_record_read.py`` is the other candidate and was rejected: it
# holds a boto3 resource at module scope, so importing it into ``code_assembly``
# would give that module an AWS client by the back door.
#
# The sub-object is a PEER of ``approval_config`` on the pipeline record and is
# deliberately NOT a key inside ``target_config`` — see design C7 and
# ``code_assembly.resolve_quality_config``.
QUALITY_CONFIG_KEY: Final[str] = "quality_config"

#: R6.6's DQ opt-in. Read by ``code_assembly.resolve_data_quality_enabled``,
#: which is THE authority on the answer — this constant is only the key.
DATA_QUALITY_ENABLED_KEY: Final[str] = "data_quality_enabled"

#: R7.1's dedup opt-in. Defaults to off, unconditionally, on every target type.
DEDUP_ENABLED_KEY: Final[str] = "dedup_enabled"

#: R7.2's duplicate-identifying columns. An empty list means "all mapped target
#: columns", which is what ``dropDuplicates()`` with no argument already does —
#: so absence and emptiness agree and neither needs a sentinel.
DEDUP_COLUMNS_KEY: Final[str] = "dedup_columns"

#: R8.1's non-printable character check. Defaults to off, unconditionally, on
#: every target type — the same flat default ``dedup_enabled`` takes, and for the
#: same reason: the check is new, so "off" is true of every existing pipeline
#: regardless of template.
#:
#: The check FLAGS; it does not quarantine (p64 task 2d.1, reasoned in full in
#: ``_partials/_data_quality.py.j2``'s header). So this key gates a Spark
#: predicate and a count, NOT a row diversion — nothing it turns on removes a row
#: from the target or adds to ``rows_quarantined``.
#:
#: It is deliberately its OWN key rather than a mode on
#: :data:`DATA_QUALITY_ENABLED_KEY`, because the check must render on all five
#: templates and that gate is off by default on four of them
#: (:data:`DQ_DEFAULT_ON_TEMPLATES`). Folding the two together would make a
#: JDBC-target pipeline's opt-in a silent no-op.
NONPRINTABLE_CHECK_ENABLED_KEY: Final[str] = "nonprintable_check_enabled"

#: R9.1/R9.3's type-cast failure count. Defaults to off, unconditionally, for the
#: same reason the two keys above do: the count is new, so "off" is true of every
#: existing pipeline regardless of template.
#:
#: WHAT IT COUNTS, AND WHY IT IS NOT THE COMPLETENESS RULE. Spark's ``cast``
#: returns NULL on failure rather than raising, so a source value of
#: ``"twenty-five"`` cast to ``int`` becomes NULL and is then indistinguishable
#: from a row whose source genuinely had no value. The DQ ``Completeness`` rule
#: counts both identically, which is how an unparseable value passes as missing
#: data — R9.1's finding. This key turns on a count of the first kind taken at the
#: moment the conversion happens, where both the source value and the target type
#: are still in hand.
#:
#: IT COUNTS CELLS, NOT ROWS, and it loses none of either. Every row continues to
#: the target with the NULL in place; what the count reports is how many
#: (row, column) values were nulled BY the conversion. So nothing this key turns on
#: adds to ``rows_quarantined`` or to any term of R7.6's row-count decomposition.
#: That is why the finding lands as a ``checks[]`` entry on the data quality report
#: rather than on the ``(run_id, "load_metrics")`` row that 2c.4's
#: ``rows_deduplicated`` went to: that row's numbers are terms in a row-count sum,
#: and a cell count is not one.
#:
#: REACH IS NARROWER THAN THE OTHER TWO KEYS, stated here rather than discovered.
#: The count is only computable where an auto-cast block exists, which is the
#: S3 Tables arms of ``glue_job_s3_tables.py.j2`` and ``glue_job_jdbc_source.py.j2``.
#: The two Glue Catalog arms cast inside a ``try`` whose target schema is read from
#: the catalog at runtime, and ``glue_job_jdbc.py.j2`` / ``glue_job_s3_parquet.py.j2``
#: cast nothing at all. Where the count is not taken the ``checks[]`` entry is
#: ABSENT rather than zero — a written zero and an absent key are different claims,
#: and "no conversion nulled a value" is not something an unmeasured run can say.
CAST_NULL_CHECK_ENABLED_KEY: Final[str] = "cast_null_check_enabled"

# The template whose DQ block is ON WITHOUT AN OPT-IN. Since p64 R6.1 all five
# templates include ``_partials/_data_quality.py.j2``, so this is no longer "the
# only file containing an EvaluateDataQuality call" — it is the DEFAULT posture,
# and it is exactly the posture that existed before p64, which is what makes
# R6.6 hold: no pipeline's verdict moves without a human opting in.
#
# Renamed from ``DQ_BEARING_TEMPLATES`` in p64 2b.4 rather than left with its old
# name and a corrected comment. The old name asserts a property of the template
# FILES that is now false for four of them, and a name that has to be read
# against its docstring to be understood is the shape this module exists to
# avoid. No alias is kept: an alias is a door (``LGR-b46a5bb-4``).
#
# The single source of this set. ``code_assembly.resolve_data_quality_enabled``
# imports it rather than restating it, so the default the renderer applies and
# the default this module falls back to for a pre-p64 state row cannot diverge.
DQ_DEFAULT_ON_TEMPLATES = frozenset({"glue_job_s3_tables.py.j2"})

# ``create_code`` writes this in place of a template name on the full-AI fallback
# path, which renders NO template and therefore no DQ block whatever the
# pipeline's opt-in says. Named here so an opt-in can never make the derivation
# claim a script that has no DQ block gates its write: there is no template to
# gate. This is the same fail-safe direction the ``determined_from`` arm takes —
# when in doubt, do not report data as gated.
FALLBACK_TEMPLATE_MARKER = "fallback_full_ai_v1"

# ``determined_from`` values — provenance of the two input facts, so a reader
# can tell "we looked and there is no DQ" from "we could not look".
FROM_CREATE_CODE_ROW = "create_code_state_row"
FROM_MISSING_CREATE_CODE_ROW = "create_code_state_row_absent"
FROM_UNREADABLE_CREATE_CODE_ROW = "create_code_state_row_unreadable"


def count_dqdl_rules(dqdl_rules: str | None) -> int:
    """Count the rules in a generated DQDL ruleset string.

    ``_generate_dqdl_rules`` emits a fixed shape::

        Rules = [
            RowCount > 0,
            Completeness "id" >= 0.95
        ]

    so the rules are the comma-separated entries between the brackets. Anything
    that is not that shape (empty string, ``None``, a bracketless fragment)
    counts as 0 rather than raising — this is a provenance annotation and must
    never be the reason a pipeline step fails.
    """
    if not dqdl_rules:
        return 0
    open_bracket = dqdl_rules.find("[")
    close_bracket = dqdl_rules.rfind("]")
    if open_bracket == -1 or close_bracket <= open_bracket:
        return 0
    body = dqdl_rules[open_bracket + 1 : close_bracket]
    return len([entry for entry in body.split(",") if entry.strip()])


def derive_quality_verdict(
    *,
    template_used: str | None,
    dqdl_rules: str | None,
    target_type: str | None = None,
    determined_from: str = FROM_CREATE_CODE_ROW,
    data_quality_enabled: bool | None = None,
) -> dict[str, Any]:
    """Return the Quality_Verdict record for a run's generated code (R7.2).

    Args:
        template_used: The PySpark template ``create_code`` actually rendered
            (``output_data.summary.template_used``), or ``"fallback_full_ai_v1"``
            for the full-AI path. Empty/``None`` means the fact was unavailable.
        dqdl_rules: The generated DQDL ruleset string baked into that code
            (``output_data.approval.dqdl_rules``). ``""`` is the meaningful
            value R7.4's degradation produces — it is not "missing".
        target_type: The normalised target type, recorded for provenance only.
            It is deliberately NOT part of the decision: the template already
            encodes the (source, target) resolution, and target type alone
            would mislabel every JDBC-source pipeline.
        determined_from: Provenance of the two facts above. When it is not
            :data:`FROM_CREATE_CODE_ROW`, the mode is reported as ``advisory``
            with ``evaluation_point: "unknown"`` — the safe direction (do not
            trust data as gated) while stating that nothing was established.
        data_quality_enabled: p64 R6.6. Whether the DQ block was actually
            enabled for the render, as resolved once by
            ``code_assembly.resolve_data_quality_enabled`` and recorded on the
            ``create_code`` state row. ``None`` means the caller could not
            establish it — a state row written before p64 — and falls back to
            :data:`DQ_DEFAULT_ON_TEMPLATES`, which is the posture such a row
            actually ran under, so every pre-p64 verdict is byte-identical. A
            ``bool`` is authoritative and supersedes the template-name default in
            both directions: ``True`` makes an opted-in non-s3_tables pipeline
            report ``enforced``, and ``False`` makes an s3_tables pipeline that
            opted OUT report ``advisory``.

    Returns:
        A flat dict safe to persist on a DynamoDB state row: no floats, no
        nested structures. See the module docstring for what each field does
        and does not claim.
    """
    template = template_used or ""
    rule_count = count_dqdl_rules(dqdl_rules)
    determined = determined_from == FROM_CREATE_CODE_ROW
    # p64 R6.6. `None` is not "off" — it is "the caller did not know", which for a
    # pre-p64 state row means the render ran the default posture. Resolving it to
    # the default here rather than at each call site keeps one answer for
    # "did this job carry a DQ block", and keeps every pre-p64 verdict unchanged.
    enabled = template in DQ_DEFAULT_ON_TEMPLATES if data_quality_enabled is None else data_quality_enabled

    if not determined or not template:
        mode = MODE_ADVISORY
        scope = "none"
        evaluation_point = "unknown"
        reason = (
            "The rendered template could not be established for this run "
            f"({determined_from}), so no inline evaluation can be claimed. "
            "Treat the write as ungated unless the run's data_quality state row says otherwise."
        )
    elif not enabled or template == FALLBACK_TEMPLATE_MARKER:
        mode = MODE_ADVISORY
        scope = "none"
        evaluation_point = "none"
        reason = (
            f"{template} renders no EvaluateDataQuality block for this pipeline, so no quality "
            "evaluation runs inside the generated job. Any quality report for this pipeline is "
            "produced after the load by the on-demand data quality agent."
        )
    elif rule_count == 0:
        mode = MODE_ADVISORY
        scope = "none"
        evaluation_point = "none"
        reason = (
            f"{template} renders its EvaluateDataQuality block only when the generated DQDL "
            "ruleset is non-empty, and this run generated none (no field mappings reached "
            "code generation), so no quality evaluation runs inside the generated job."
        )
    else:
        mode = MODE_ENFORCED
        scope = "row_level"
        evaluation_point = "before_write"
        reason = (
            f"{template} evaluates {rule_count} DQDL rule(s) before the target write and "
            "quarantines failing rows, so they never land. The run itself is not blocked: a "
            "rule failure does not fail the Glue job, and a dataset-level rule that fails "
            "without producing failing rows withholds nothing."
        )

    return {
        "mode": mode,
        "enforcement_scope": scope,
        "evaluation_point": evaluation_point,
        # Always False. No combination fails the run on a quality failure —
        # recorded explicitly because "enforced" reads as if one does.
        "blocks_run": False,
        "dqdl_rule_count": rule_count,
        "template_used": template,
        "target_type": target_type or "",
        "determined_from": determined_from,
        # p64 R6.6 — the RESOLVED gate, not the argument. Recorded so a reader can
        # tell "this target type never runs DQ" from "this pipeline opted out" and
        # from "this pipeline opted in", which the mode alone collapses into one
        # value. `None` in, default applied, bool out.
        "data_quality_enabled": enabled,
        "reason": reason,
    }
