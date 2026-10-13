# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p64 2c.1 — the Quality_Config reaches the render context explicitly (R7.1/R7.2, C7).

WHAT IS BEING PINNED, AND WHY IT IS NOT OBVIOUS. Design C7 names two routes into
the render context and takes the expensive one. ``generate_code`` does
``template_vars.update(source_config)`` then ``template_vars.update(target_config)``,
splatting the persisted maps wholesale, so a flag stored inside ``target_config``
becomes a template variable with **zero** backend change — no ``code_assembly``
edit, no API validation edit. C7 rejects that: a data-quality setting is not target
configuration, and ``target_config`` is a free-form ``Record<string, string>`` with
no validation, so it is a place to hide an unvalidated boolean. The chosen route is
a ``quality_config`` sub-object threaded **explicitly**, which is what
:func:`code_assembly.resolve_quality_config` and the assignment beside
``data_quality_enabled`` are for.

``tests/unit/templates/test_dq_template_vars_contract.py`` owns the *unconditional*
half mechanically, reading the DQ partial's whole variable surface. It cannot own
the rest: it derives its ``required`` set from what the partial reads, so until
p64 2c.3 makes the template read ``quality_config`` it has nothing to say about it.
This module covers the interval — and, more importantly, the two things that test
will never assert: that the render value is **normalised** (every key present,
every type as declared) and that a hostile dedup column **fails code generation**
rather than reaching ``dropDuplicates``.

WHY NORMALISATION IS THE LOAD-BEARING PART. ``_get_pyspark_jinja_env`` uses
``StrictUndefined``, and a jinja2 dict access to a missing key yields an
``Undefined`` whose ``__bool__`` raises. So ``{% if quality_config.dedup_enabled %}``
would fail the render for a pipeline that simply never set it — which is every
pipeline that predates 2c.1. Filling the keys upstream is what lets 2c.3's template
read them plainly instead of reaching for ``| default(...)``, which do-NOT 6 forbids
because under two jinja2 copies in one process the filter hands a foreign
``missing`` sentinel straight through (``_partials/_load_counts.py.j2``'s header
records the incident: all 18 tests in
``tests/integration/pipeline_flow/test_code_generation_e2e.py`` at once).

Expectations are derived from R7.1/R7.2 rather than from current output. Everything
below **fails against unfixed code**: before 2c.1 there was no
``resolve_quality_config`` and no ``template_vars["quality_config"]``, so a template
reading either would have raised ``UndefinedError`` on every render.

Validates: Requirements 7.1, 7.2
"""

from __future__ import annotations

import ast
import os
import sys

import pytest

# Region before any boto3-adjacent import, matching the sibling create_code
# modules.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code"),
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Imported directly, NOT through ``lambda_function`` behind a
# ``patch.dict("sys.modules", ...)`` block. That idiom leaves a second jinja2 in
# ``sys.modules``, which is the hazard do-NOT 6 exists for, so reaching the code
# under test through it would muddy what these assertions measure.
import code_assembly

from identifier_utils import IdentifierValidationError

# Asserted through the SHARED constants rather than restated as literals: they are
# the mechanism keeping this reader and the writer in ``dashboard_api`` from
# drifting (``LGR-eee8ac8-5``), so a test spelling them out again could pass while
# the two sides disagreed.
from quality_verdict import (
    CAST_NULL_CHECK_ENABLED_KEY,
    DATA_QUALITY_ENABLED_KEY,
    DEDUP_COLUMNS_KEY,
    DEDUP_ENABLED_KEY,
    NONPRINTABLE_CHECK_ENABLED_KEY,
    QUALITY_CONFIG_KEY,
)

#: The render-variable name, which is a DIFFERENT contract from the record
#: attribute name above even though the two share a spelling: this one is agreed
#: with the template, that one with the writer across a Lambda boundary. Spelled
#: literally here for the same reason ``generate_code`` spells it literally — the
#: AST fence in ``test_dq_template_vars_contract.py`` matches a constant subscript
#: only.
RENDER_VARIABLE = "quality_config"


def _record(**quality_config) -> dict:
    """A pipeline record carrying ``quality_config``, or not carrying it at all.

    Passing no keyword produces a record with **no** ``quality_config`` — the
    pre-2c.1 shape every existing pipeline has — which is a different case from
    passing an empty dict, and both matter.
    """
    if not quality_config:
        return {"pipeline_id": "pid"}
    return {"pipeline_id": "pid", QUALITY_CONFIG_KEY: dict(quality_config)}


# ---------------------------------------------------------------------------
# The resolver: normalisation, and the two defaults
# ---------------------------------------------------------------------------


class TestTheResolvedShapeIsTotal:
    """Every recognised key on every call — the ``StrictUndefined`` requirement.

    **Validates: Requirements 7.1**
    """

    @pytest.mark.parametrize(
        "metadata",
        [
            None,
            {},
            {"pipeline_id": "pid"},
            {"pipeline_id": "pid", QUALITY_CONFIG_KEY: {}},
            {"pipeline_id": "pid", QUALITY_CONFIG_KEY: None},
            {"pipeline_id": "pid", QUALITY_CONFIG_KEY: "on"},
            {"pipeline_id": "pid", QUALITY_CONFIG_KEY: ["dedup"]},
        ],
        ids=["none", "empty", "no-key", "empty-map", "null", "scalar", "list"],
    )
    def test_every_recognised_key_is_always_present(self, metadata):
        """Including for the shapes a writer without the validator could leave.

        A jinja2 dict access to an ABSENT key yields an ``Undefined`` whose
        ``__bool__`` raises under ``StrictUndefined``, so a missing key here is not
        a harmless omission — it is a render failure for a pipeline that never set
        the option. That is why absence, ``None`` and a scalar all resolve to the
        same total shape rather than to nothing.

        RENAMED from ``test_both_dedup_keys_…`` by p64 2d.2, which added
        ``nonprintable_check_enabled`` and made "both" wrong. The name is the claim,
        so it moves with the claim rather than being left to be read against the
        assertion — the same reason 2b.4 renamed ``DQ_BEARING_TEMPLATES``. p64 2e.1
        added ``cast_null_check_enabled`` and the name still holds.

        The key set is asserted by EQUALITY, not containment, which is deliberate
        and is why 2d.2 had to edit this line: an extra key in the render context is
        not free. Every one of them is a name the five templates may read, so a key
        appearing here without a partial reading it — or without
        ``test_dq_template_vars_contract.py``'s fence covering it — is exactly the
        drift that fence exists to catch.
        """
        resolved = code_assembly.resolve_quality_config(metadata)

        assert set(resolved) == {
            DEDUP_ENABLED_KEY,
            DEDUP_COLUMNS_KEY,
            NONPRINTABLE_CHECK_ENABLED_KEY,
            CAST_NULL_CHECK_ENABLED_KEY,
        }
        assert isinstance(resolved[DEDUP_ENABLED_KEY], bool)
        assert isinstance(resolved[DEDUP_COLUMNS_KEY], list)
        assert isinstance(resolved[NONPRINTABLE_CHECK_ENABLED_KEY], bool)
        assert isinstance(resolved[CAST_NULL_CHECK_ENABLED_KEY], bool)

    @pytest.mark.parametrize(
        "metadata",
        [None, {}, {"pipeline_id": "pid"}, {"pipeline_id": "pid", QUALITY_CONFIG_KEY: {}}],
        ids=["none", "empty", "no-key", "empty-map"],
    )
    def test_every_option_defaults_to_off_on_every_target(self, metadata):
        """R7.1 / R8.1: "MUST default to off so no existing pipeline's behaviour changes".

        Unlike the DQ gate, these defaults are genuinely flat — dedup and the
        character check are off for every template and every target type today —
        which is exactly why they are safe to seed on the write side and to state
        here without a template argument.

        ``nonprintable_check_enabled`` (p64 2d.2) is flat for a second reason worth
        keeping visible: the check FLAGS rather than quarantines, so even turned on
        it removes no row — but it is still off by default, because it writes a
        ``checks[]`` entry an existing pipeline's report reader has never seen.

        ``cast_null_check_enabled`` (p64 2e.1) is flat for both of those reasons at
        once: it counts cells rather than diverting rows, so even turned on it
        removes nothing — and it too writes a ``checks[]`` entry no existing
        pipeline's report has carried.
        """
        assert code_assembly.resolve_quality_config(metadata) == {
            DEDUP_ENABLED_KEY: False,
            DEDUP_COLUMNS_KEY: [],
            NONPRINTABLE_CHECK_ENABLED_KEY: False,
            CAST_NULL_CHECK_ENABLED_KEY: False,
        }


class TestOnlyAnExplicitBooleanOptsIn:
    """**Validates: Requirements 7.1**"""

    @pytest.mark.parametrize("value", [True, False])
    def test_a_real_boolean_is_honoured_both_ways(self, value):
        assert code_assembly.resolve_quality_config(_record(dedup_enabled=value))[DEDUP_ENABLED_KEY] is value

    @pytest.mark.parametrize("value", ["true", "false", "on", 1, 0, [], {}, None])
    def test_a_non_boolean_falls_through_to_off(self, value):
        """Truthiness here would make the string ``"false"`` mean enabled.

        The boundary validator refuses these with a 400 (R10.4), so this arm covers
        a record written before that validator existed, or by a writer that bypassed
        it. Falling through to OFF is the fail-closed direction: nobody gets dedup
        they did not ask for.
        """
        assert code_assembly.resolve_quality_config(_record(dedup_enabled=value))[DEDUP_ENABLED_KEY] is False

    @pytest.mark.parametrize("value", [True, False])
    def test_the_character_check_flag_is_honoured_both_ways(self, value):
        """p64 R8.1 / 2d.2 — the same rule, applied to the third key.

        **Validates: Requirements 8.1**
        """
        resolved = code_assembly.resolve_quality_config(_record(nonprintable_check_enabled=value))

        assert resolved[NONPRINTABLE_CHECK_ENABLED_KEY] is value

    @pytest.mark.parametrize("value", ["true", "false", "on", 1, 0, [], {}, None])
    def test_a_non_boolean_character_check_flag_falls_through_to_off(self, value):
        """Fail-closed, and here that is the ONLY layer standing (p64 2d.2).

        ``dedup_enabled`` has two: this resolver, and
        ``pipelines_crud._validate_quality_config``, which 400s a non-boolean at the
        API boundary because it is listed in ``_QUALITY_CONFIG_BOOLEAN_KEYS``. 2d.2
        deliberately did NOT add its key to that tuple — the boundary refusal is a
        dashboard_api change and 2d.3 owns that surface — so today a submitted
        ``"false"`` is accepted by the API and silently normalised to off HERE.

        That is safe in the direction that matters, and the contrast with
        ``data_quality_enabled`` is why: 2b.4's resolver falls through to a
        PER-TEMPLATE default, so a string there means an S3 Tables pipeline gets DQ
        **on** having asked for off. This key's fallback is a flat ``False``, so
        every non-boolean means off — the value the submitter most plausibly
        intended, and never an opt-in nobody asked for. The cost is silence rather
        than a 400, which is why it is stated here rather than left to be inferred.
        """
        resolved = code_assembly.resolve_quality_config(_record(nonprintable_check_enabled=value))

        assert resolved[NONPRINTABLE_CHECK_ENABLED_KEY] is False

    def test_the_two_opt_ins_are_independent(self):
        """Turning on dedup does not turn on the character check, or the reverse.

        They are separate keys because they are separate mechanisms — one drops rows,
        one only counts them — and a reader who assumes "quality options" is a single
        switch would find that plausible. Cheap to pin, and it fails loudly if either
        resolver arm is ever rewritten to read the other's key.

        **Validates: Requirements 8.1**
        """
        dedup_only = code_assembly.resolve_quality_config(_record(dedup_enabled=True))
        check_only = code_assembly.resolve_quality_config(_record(nonprintable_check_enabled=True))

        assert dedup_only[DEDUP_ENABLED_KEY] is True
        assert dedup_only[NONPRINTABLE_CHECK_ENABLED_KEY] is False
        assert check_only[NONPRINTABLE_CHECK_ENABLED_KEY] is True
        assert check_only[DEDUP_ENABLED_KEY] is False

    def test_the_dq_gate_is_not_in_the_resolved_shape(self):
        """One authority for ``data_quality_enabled``, and it is not this function.

        Its default is per template — ``resolve_data_quality_enabled`` needs
        ``template_name``, which this function does not take — and R6.6 requires
        both directions of "no existing pipeline's behaviour changes", so a flat
        default would strip the DQ block from every existing S3 Tables pipeline.
        Copying that resolver's answer in here would be the second derivation
        ``LGR-eee8ac8-6`` was reopened to prevent, and the two could then disagree
        about whether a job gates its write.
        """
        resolved = code_assembly.resolve_quality_config(_record(data_quality_enabled=True))

        assert DATA_QUALITY_ENABLED_KEY not in resolved, (
            f"{DATA_QUALITY_ENABLED_KEY!r} is in resolve_quality_config's output. That "
            f"is a SECOND derivation of a question resolve_data_quality_enabled already "
            f"answers, and its default is per-template rather than flat. If the render "
            f"context needs it in this dict, CALL that resolver — do not recompute it."
        )

    def test_the_dq_gate_is_still_resolved_by_its_own_authority(self):
        """The paired positive: excluding it here did not make it unreachable.

        ``resolve_data_quality_enabled`` reads the same sub-object, so both halves
        of the Quality_Config are live — they just have different resolvers because
        they have different default rules.
        """
        opted_in = _record(data_quality_enabled=True)

        assert code_assembly.resolve_data_quality_enabled(opted_in, "glue_job_jdbc.py.j2") is True
        assert code_assembly.resolve_data_quality_enabled(_record(), "glue_job_jdbc.py.j2") is False


class TestDedupColumns:
    """R7.2, and the injection sink underneath it.

    **Validates: Requirements 7.2**
    """

    def test_a_conforming_list_is_returned_unchanged(self):
        columns = ["patient_id", "visit_date", "_internal"]

        assert code_assembly.resolve_quality_config(_record(dedup_columns=columns))[DEDUP_COLUMNS_KEY] == columns

    def test_an_empty_list_means_every_mapped_target_column(self):
        """R7.2's default, and why it needs no sentinel.

        ``dropDuplicates()`` with no argument already means "all columns", and the
        transformed frame carries exactly the mapped target columns — so the empty
        list and the "unspecified" case are the same instruction and neither needs
        a third state. 2c.3 owns turning it into the emitted call.
        """
        assert code_assembly.resolve_quality_config(_record(dedup_columns=[]))[DEDUP_COLUMNS_KEY] == []

    @pytest.mark.parametrize(
        "columns",
        ["patient_id", {"0": "patient_id"}, 7, None, ["patient_id", 7], [None]],
        ids=["string", "dict", "int", "none", "mixed-list", "list-of-none"],
    )
    def test_a_malformed_container_degrades_to_the_default(self, columns):
        """The SHAPE half degrades; the IDENTITY half (below) raises.

        A container that is not a list of strings was never a usable column list,
        and the boundary validator already 400s it, so this arm only sees a record
        written before that validator. Degrading to "all mapped target columns" is
        the fail-safe reading: it deduplicates on more columns rather than fewer, so
        it cannot drop a row the operator wanted kept.
        """
        assert code_assembly.resolve_quality_config(_record(dedup_columns=columns))[DEDUP_COLUMNS_KEY] == []

    @pytest.mark.parametrize(
        "column",
        [
            'id"); import os; os.system("sh',
            "patient id",
            "patient-id",
            "1_id",
            "",
            "x" * 256,
        ],
    )
    def test_a_well_formed_list_of_hostile_names_fails_code_generation(self, column):
        """These names reach ``dropDuplicates([...])``, so the allowlist applies.

        Deliberately NOT degraded to the default like a malformed container: a
        well-formed list of hostile strings is the injection case, and silently
        narrowing it to something safe-looking would mean the operator's stated
        dedup identity was replaced by a different one with no error. do-NOT 2
        forbids weakening the allowlist at a sink and do-NOT 3 forbids modifying
        ``validate_identifier``; this only calls it.

        The error names the identifier and the sink (R12.2), so the refusal is
        attributable from logs alone — and the sink string is distinguishable from
        the ``merge_key`` and DQDL ones, which p55 task 6.7 pinned.
        """
        with pytest.raises(IdentifierValidationError) as excinfo:
            code_assembly.resolve_quality_config(_record(dedup_columns=[column]))

        assert DEDUP_COLUMNS_KEY in str(excinfo.value), (
            f"the refusal does not name the sink: {excinfo.value}. A refusal that does "
            f"not say which of this module's identifier sinks rejected the value is not "
            f"diagnosable from logs."
        )


# ---------------------------------------------------------------------------
# The threading itself, and the route C7 rejected
# ---------------------------------------------------------------------------


def _render_site_template_var_assignments() -> tuple[dict[str, int], dict[str, int], list[str]]:
    """``(unconditional, conditional, splatted)`` for ``generate_code``'s context.

    The same AST read ``tests/unit/templates/test_dq_template_vars_contract.py``
    performs, narrowed to what this module needs. Duplicated rather than imported
    because that module is the fence for the DQ partial's variable surface and this
    one is about a single variable it cannot yet see; importing across two test
    modules to share thirty lines would couple two fences that should fail
    independently.
    """
    with open(code_assembly.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    def renders(node: ast.AST) -> bool:
        return any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "render"
            and any(
                keyword.arg is None and isinstance(keyword.value, ast.Name) and keyword.value.id == "template_vars"
                for keyword in inner.keywords
            )
            for inner in ast.walk(node)
        )

    sites = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and renders(node)
    ]
    assert len(sites) == 1, (
        f"expected exactly one function calling template.render(**template_vars), "
        f"found {[fn.name for fn in sites]}. Every assertion below rests on there "
        f"being one render site."
    )
    function = sites[0]

    def target(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Assign):
            return None
        for element in node.targets:
            if (
                isinstance(element, ast.Subscript)
                and isinstance(element.value, ast.Name)
                and element.value.id == "template_vars"
                and isinstance(element.slice, ast.Constant)
                and isinstance(element.slice.value, str)
            ):
                return element.slice.value
        return None

    unconditional: dict[str, int] = {}
    conditional: dict[str, int] = {}
    for statement in function.body:
        direct = target(statement)
        if direct is not None:
            unconditional[direct] = statement.lineno
        for node in ast.walk(statement):
            if node is statement:
                continue
            nested = target(node)
            if nested is not None:
                conditional[nested] = node.lineno  # type: ignore[attr-defined]

    splatted: list[str] = [
        ast.unparse(argument)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "update"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "template_vars"
        for argument in node.args
    ]

    return unconditional, conditional, splatted


class TestTheRenderVariableIsThreadedExplicitly:
    """C7's routing decision, asserted mechanically rather than trusted.

    **Validates: Requirements 7.1**
    """

    def test_it_is_assigned_unconditionally_at_the_render_site(self):
        """R6.5's rule, applied to 2c.1's variable before 2c.3 makes it required.

        ``generate_code`` holds the only ``template.render()`` call in the Lambda,
        so a key set at its top level is a key **every** render has. Assigned inside
        an ``if`` instead, the rendered script would depend on a branch — and under
        ``StrictUndefined`` a template reading it would raise on the other arm.
        """
        unconditional, conditional, splatted = _render_site_template_var_assignments()

        assert RENDER_VARIABLE in unconditional, (
            f"template_vars[{RENDER_VARIABLE!r}] is not assigned unconditionally at "
            f"generate_code's top level.\n"
            f"  unconditional: {sorted(unconditional)}\n"
            f"  conditional:   {sorted(conditional)}\n"
            f"  splatted:      {splatted}\n"
            f"R6.5 / do-NOT 6: set it beside data_quality_enabled. Do NOT route it "
            f"through source_config/target_config — design C7 rejects that."
        )
        assert RENDER_VARIABLE not in conditional

    def test_it_does_not_arrive_through_the_target_config_splat(self):
        """The route C7 rejected, stated as a fact about the record instead.

        The splat is still there and still splats ``target_config`` — that is
        pre-existing and P14 behaviour depends on it. What C7 rejected is
        ``quality_config`` *travelling* that way, and the observable consequence is
        this: a ``target_config`` carrying the key must not change the resolved
        value, because the resolver reads the record's top-level peer and nothing
        else.
        """
        smuggled = {
            "pipeline_id": "pid",
            "target_config": {
                "type": "S3_TABLES",
                QUALITY_CONFIG_KEY: {
                    DEDUP_ENABLED_KEY: True,
                    NONPRINTABLE_CHECK_ENABLED_KEY: True,
                    CAST_NULL_CHECK_ENABLED_KEY: True,
                },
            },
        }

        assert code_assembly.resolve_quality_config(smuggled) == {
            DEDUP_ENABLED_KEY: False,
            DEDUP_COLUMNS_KEY: [],
            NONPRINTABLE_CHECK_ENABLED_KEY: False,
            CAST_NULL_CHECK_ENABLED_KEY: False,
        }, (
            "a quality_config hidden inside target_config changed the resolved value. "
            "Design C7 rejects that route: target_config is free-form and unvalidated, "
            "so it is a place to hide an unvalidated boolean. Read the top-level peer."
        )

    def test_the_dedup_validation_is_the_last_identifier_sink(self):
        """p37 R2.7's ordering rule, extended to 2c.1's sink.

        ``merge_key`` and ``_generate_dqdl_rules``' per-field call run first, so an
        input that trips either must still fail with **that** sink's error naming
        **that** identifier. Asserted by line order, which is what "runs first"
        reduces to inside one function body.
        """
        with open(code_assembly.__file__, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        def line_of(needle: str) -> int:
            matches = [index for index, line in enumerate(lines) if needle in line]
            assert matches, f"could not find {needle!r} in code_assembly.py"
            return matches[-1]

        merge_key_sink = line_of("validated_merge_key = validate_identifier(")
        dedup_sink = line_of(f'template_vars["{RENDER_VARIABLE}"] = resolve_quality_config(')

        assert merge_key_sink < dedup_sink, (
            "the dedup column validation was hoisted ahead of the merge_key sink. "
            "p37 R2.7: an input that trips an existing sink must still fail with that "
            "sink's IdentifierValidationError naming that identifier."
        )
