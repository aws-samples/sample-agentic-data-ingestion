# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the Teardown_Tool's pure helpers (p55 task 2.2, W1).

The four functions live in `scripts/destroy_lib.sh` and are exercised as the
real shell functions through the `tests/unit/conftest.py` harness — the same
shape `LGR-b5b76ed-1` (template / ratified) established for
`scripts/deploy_prereq_lib.sh`, and whose `scope` already names
`tests/unit/test_deploy_*.py` and `tests/unit/conftest.py`. See
`context/shell-test-approach.md` for the full argument.

**No structural invariant here is asserted through `destroy.sh` stdout.**
`LGR-b46a5bb-4` and `LGR-b5b76ed-1` both reject substring matching over script
output for the same reason: a `destroy.sh` that merely *mentions* a stack name
in a comment or a banner would pass. Every library assertion below is on a
function's return code and its own resolved value.

p64 task 1b.3 added two groups that reach past the library, and the distinction
that licenses them is worth keeping straight, because the rejected pattern and
these are easy to confuse:

* **`--help`** — its output is not a proxy for anything. It IS the artifact, so
  reading it is not substring-matching an invariant, and it was previously
  asserted **nowhere**. `--help` printed a hardcoded `sed -n '2,29p'` range over
  the script's own header, so documenting a new flag silently truncated the
  usage text or spilled `set -euo pipefail` into it as documentation —
  `LGR-4ea43f3-5`, the same defect and the same awk remedy, one script over.
  The strongest guard here is structural rather than textual: every flag the
  argument parser accepts must appear in the header block.
* **The stack-deletion loop under `--continue-past-failed-stacks`** — the loop
  is not a function, so it is *lifted verbatim* and run against the real library
  with `aws` shadowed by an argv-logging stub, the shape `LGR-4ea43f3-6`
  established. What happened is asserted by **parsing the stub's log**, never by
  transcribing stdout. The one place output is read is R2.2's own requirement
  that skipped stacks be *named on exit*, and there the naming is the deliverable.

Two conventions the library's contract forces on these tests:

* **Return codes, never output emptiness.** Every function writes only its own
  value to stdout and every failure is a non-zero return with empty stdout, so
  `result.returncode` is the signal and an empty `stdout` is a corroboration —
  never the assertion.
* **Absolute shebangs in the `aws` stub.** With a PATH containing only the stub
  directory, `#!/usr/bin/env bash` cannot find `bash`, the stub fails to exec,
  and `resolve_account` returns 1 — indistinguishable from the R1.2 failure arm
  under test. `#!/bin/sh` is used for that reason.

p64 task 1b.5 closed what 1b.3 flagged it had left: the three pure functions
1b.1 and 1b.2 added — `failed_resource_ids`, `compose_failure_reason` and
`compose_retain_retry_outcome` — had no tests at all, so R2.1's retry, R2.3's
per-resource reasons and R2.4's no-truncation posture rested on reading them.
Those groups also carry this file's property tests, because two of those
requirements are only meaningful as invariants over arbitrary input: "nothing is
dropped" (R2.4) is asserted as an exact line count, and "every id is a usable
`--retain-resources` argument" (R2.1) as a grammar over whatever reaches it. A
fixture can only show that nothing was dropped from that fixture.

**Validates: Requirements 1.2, 1.6, 1.7, 1.10, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1**
"""

import re
import shlex
import shutil

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.conftest import (
    AGENTCORE_CLEANUP_SH,
    DESTROY_LIB,
    DESTROY_SH,
    _extract_bash_function,
    _run_bash,
    call_destroy_lib,
)

# The two variants of the Project_Prefix. `destroy.sh` applies both; the library
# takes the prefix as an argument precisely so the variants stay in the caller.
UNDERSCORE_PREFIX = "agentic_data_ingestion"
HYPHEN_PREFIX = "agentic-data-ingestion"

# The nine logical names, newest-dependent first. Reverse of a topological
# creation order over the `addDependency` edges in
# `deploy-cdk/bin/agentic_data_ingestion.ts`. Restated here as the expectation
# rather than read from the library, so a reordering of the library is a test
# failure rather than a tautology. Task 2.3's separate file is what keeps the
# *set* honest against the CDK entrypoint.
EXPECTED_ORDER = [
    "TestFixtures",
    "Hosting",
    "Orchestration",
    "EdgeWaf",
    "Compute",
    "AgentCore",
    "Auth",
    "Data",
    "Foundation",
]


@pytest.fixture
def hermetic_aws_path(tmp_path):
    """Build a PATH whose only executable is a stub `aws`, and log its calls.

    Returns a callable ``(stdout=None, returncode=0) -> path string``. The stub
    stands in for the *external CLI*, not for the code under test:
    `resolve_account` gates on the CLI's exit status and then validates the text
    it printed, so a stub only has to reproduce those two things.

    A hermetic PATH is required because a developer machine has a real `aws` on
    it, usually with live credentials — a test that reached it would either
    resolve a real account id or fail for a reason that has nothing to do with
    the arm under test.

    The returned callable carries `.invocations`, a `pathlib.Path` the stub
    appends one line to per call. Its **absence** is how the "aws was never
    invoked" assertion is made, which is otherwise unobservable from outside.
    """
    bin_dir = tmp_path / "hermetic_bin"
    bin_dir.mkdir()
    invocations = tmp_path / "aws_invocations"

    def build(stdout: str | None = None, returncode: int = 0) -> str:
        lines = [
            # Absolute, not `#!/usr/bin/env sh` — see the module docstring.
            "#!/bin/sh",
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(invocations))}",
        ]
        if stdout is not None:
            # printf is a shell builtin, so this needs nothing else on PATH.
            lines.append(f"printf '%s\\n' {shlex.quote(stdout)}")
        lines.append(f"exit {returncode}")
        stub = bin_dir / "aws"
        stub.write_text("\n".join(lines) + "\n")
        stub.chmod(0o755)
        return str(bin_dir)

    build.invocations = invocations
    return build


def _selected(prefix: str, names: str) -> list[str]:
    """Run `select_prefixed` over `names` and return the selected lines."""
    result = call_destroy_lib("select_prefixed", prefix, stdin=names)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


# ── stack_deletion_order (R1.6) ───────────────────────────────────────
def test_stack_deletion_order_emits_the_nine_logical_names_newest_dependent_first(call_destroy_fn):
    """**Validates: Requirements 1.6**

    Nine names, not eight: the eight always-deployed stacks plus the
    context-gated TestFixtures. A gated stack is either deployed or it is not,
    and this function cannot know which — the context flag was supplied to a
    past `cdk deploy`, not to this teardown — so omitting it would make an
    opt-in stack undeletable by the only tool built to delete it.
    """
    result = call_destroy_fn("stack_deletion_order")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == EXPECTED_ORDER


def test_hosting_is_deleted_before_edgewaf_the_stack_it_imports_from(call_destroy_fn):
    """**Validates: Requirements 1.6**

    Asserted as its own case because it is the one adjacency an intuitive
    reading gets backwards. Hosting consumes EdgeWaf's WAF ARN through
    `crossRegionReferences: true`, so deleting the *producer* of a live export
    fails on the export — the consumer goes first.
    """
    order = call_destroy_fn("stack_deletion_order").stdout.splitlines()

    assert order.index("Hosting") < order.index("EdgeWaf")


# ── stack_region (R1.7) ───────────────────────────────────────────────
def test_edgewaf_is_addressed_in_us_east_1(call_destroy_fn):
    """**Validates: Requirements 1.7**

    A CLOUDFRONT-scope WebACL can only exist in us-east-1, so that is where the
    stack was created and the only region a delete request for it can be
    addressed to. Addressing it in the operator's region returns a "stack does
    not exist" that reads as "already gone" — the failure this case prevents.
    """
    result = call_destroy_fn("stack_region", "EdgeWaf", "us-west-2")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "us-east-1"


def test_edgewaf_is_pinned_even_when_the_operator_region_is_not_us_west_2(call_destroy_fn):
    """**Validates: Requirements 1.7**

    us-west-2 is this system's primary region, so a second operator region shows
    the pin is unconditional rather than a us-west-2 coincidence.
    """
    result = call_destroy_fn("stack_region", "EdgeWaf", "eu-west-1")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "us-east-1"


def test_every_other_stack_is_addressed_in_the_operator_region(call_destroy_fn):
    """**Validates: Requirements 1.7**

    EdgeWaf is the only entry this function moves, so the remaining eight are
    asserted together — a second pinned stack would be a silent mis-target.
    """
    for name in EXPECTED_ORDER:
        if name == "EdgeWaf":
            continue
        result = call_destroy_fn("stack_region", name, "eu-west-1")
        assert result.returncode == 0, f"{name}: {result.stderr}"
        assert result.stdout.strip() == "eu-west-1", name


def test_an_empty_stack_name_is_a_failure_not_a_default(call_destroy_fn):
    """**Validates: Requirements 1.7**

    Return code 2 with no stdout. `${1:-}` rather than `$1` is what makes this a
    return code instead of an abort — the harness sources the library under
    `set -uo pipefail`, and a testable failure arm has to be a code.
    """
    result = call_destroy_fn("stack_region", "", "us-west-2")

    assert result.returncode == 2
    assert result.stdout == ""


def test_an_empty_operator_region_is_a_failure_not_a_fallback(call_destroy_fn):
    """**Validates: Requirements 1.7**

    There is no sane default. Falling back to the caller's ambient AWS_REGION
    would send delete requests at whatever region the shell happens to be
    configured for — the same class of mistake as a hardcoded account.
    """
    result = call_destroy_fn("stack_region", "Data", "")

    assert result.returncode == 2
    assert result.stdout == ""


# ── select_prefixed: the safety property is the exclusions ─────────────
def test_a_table_without_the_project_prefix_is_not_selected():
    """**Validates: Requirements 2.4**

    Its own named case, not a line inside another, because this is the one place
    the Teardown_Tool can destroy something it does not own. Everything
    `select_prefixed` emits is a deletion candidate and there is no undo, so a
    name this tool does not recognise must not appear in the output.
    """
    selected = _selected(
        UNDERSCORE_PREFIX,
        "agentic_data_ingestion_state\nsomeone_elses_table\nprod-payments\n",
    )

    assert selected == ["agentic_data_ingestion_state"]
    assert "someone_elses_table" not in selected
    assert "prod-payments" not in selected


def test_the_hyphen_variant_is_not_selected_by_the_underscore_prefix():
    """**Validates: Requirements 2.4**

    The two Project_Prefix variants are separate passes by design — the prefix
    is an argument so the variants stay in `destroy.sh`. This pins that one pass
    does not quietly cover the other, which is what would let a caller that ran
    only one pass believe it had enumerated everything.
    """
    names = "agentic_data_ingestion_state\nagentic-data-ingestion-hosting\n"

    assert _selected(UNDERSCORE_PREFIX, names) == ["agentic_data_ingestion_state"]
    assert _selected(HYPHEN_PREFIX, names) == ["agentic-data-ingestion-hosting"]


def test_the_prefix_is_matched_literally_and_not_as_a_pattern():
    """**Validates: Requirements 2.4**

    `grep "^$prefix"` reads the prefix as a regex and `case "$line" in "$prefix"*)`
    glob-expands it, so under either a prefix containing `.`, `*`, `?` or `[`
    selects names it should not — an over-selection, and over-selection here is
    a deletion. The `${line#"$prefix"}` form suppresses pattern interpretation,
    so `a.b` matches `a.b_table` and not `axb_table`.
    """
    assert _selected("a.b", "a.b_table\naxb_table\n") == ["a.b_table"]
    assert _selected("a*b", "a*b_table\naxxb_table\nab_table\n") == ["a*b_table"]


def test_selection_preserves_input_order_and_skips_blank_lines():
    """**Validates: Requirements 2.1**

    `aws --output text` emits an empty line for an empty result set, and an
    empty name selected as a deletion candidate is a malformed API call at best.
    Input order is preserved so the caller's enumeration reads in the order the
    operator will see it deleted.
    """
    selected = _selected(
        UNDERSCORE_PREFIX,
        "agentic_data_ingestion_pipelines\n\nagentic_data_ingestion_approvals\n\n",
    )

    assert selected == [
        "agentic_data_ingestion_pipelines",
        "agentic_data_ingestion_approvals",
    ]


def test_a_final_line_with_no_trailing_newline_is_still_selected():
    """**Validates: Requirements 2.1**

    `read` reports failure on a final unterminated line *after* having populated
    the variable, so without the `|| [ -n "$line" ]` guard the last table in a
    list would be silently skipped — and a table skipped here is a table that
    collides on the next deploy.
    """
    selected = _selected(UNDERSCORE_PREFIX, "agentic_data_ingestion_state")

    assert selected == ["agentic_data_ingestion_state"]


def test_an_empty_name_list_succeeds_and_selects_nothing():
    """**Validates: Requirements 1.10, 2.1**

    R1.10's empty account. An empty result is a normal, successful outcome and
    never an error — asserted as return code 0, with the empty stdout as
    corroboration rather than as the signal.
    """
    result = call_destroy_lib("select_prefixed", UNDERSCORE_PREFIX, stdin="")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_an_empty_prefix_is_a_failure_rather_than_an_empty_selection():
    """**Validates: Requirements 2.4**

    Both readings are defensible in isolation and only one is safe to get
    wrong. A caller whose prefix variable was accidentally empty gets a loud
    failure instead of a silently empty enumeration that reads as "nothing to
    delete" — and the complementary hazard, an empty prefix matching
    *everything*, is foreclosed by the same return.
    """
    result = call_destroy_lib("select_prefixed", "", stdin="agentic_data_ingestion_state\n")

    assert result.returncode == 2
    assert result.stdout == ""


def test_iam_names_are_selected_by_the_same_prefix_filter():
    """**Validates: Requirements 3.1**

    R3.1 selects IAM roles and customer-managed policies by the same rule as
    R2.1 selects tables, so this is the same function over a different name
    population rather than a second mechanism. The exclusion holds identically:
    IAM is an account-global namespace, so an unrecognised role in a shared
    account is exactly the resource this tool must not touch.
    """
    selected = _selected(
        UNDERSCORE_PREFIX,
        "agentic_data_ingestion_lambda_role\nOrganizationAccountAccessRole\nagentic_data_ingestion_glue_policy\n",
    )

    assert selected == [
        "agentic_data_ingestion_lambda_role",
        "agentic_data_ingestion_glue_policy",
    ]


# ── resolve_account (R1.1, R1.2) ──────────────────────────────────────
def test_a_well_formed_account_id_is_resolved_and_echoed(call_destroy_fn, hermetic_aws_path):
    """**Validates: Requirements 1.2**

    The success arm, present so the failure arms below are known to be failures
    of validation rather than of the harness. The value is a test fixture, not a
    real account: no account-shaped literal for either of this system's two
    accounts appears in the library or in `destroy.sh`, because a copyable
    literal is the entire mechanism of the incident `LGR-1166391-2` records.
    """
    path = hermetic_aws_path(stdout="123456789012")

    result = call_destroy_fn("resolve_account", "some-profile", path=path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "123456789012"


def test_a_failing_cli_call_terminates_resolution_with_no_account(call_destroy_fn, hermetic_aws_path):
    """**Validates: Requirements 1.2**

    Return code 1 and nothing on stdout. R1.2 requires the run to terminate
    with no resource deleted, and `resolve_account` runs before anything is
    reported and before anything is touched — which is what makes that
    enforceable rather than aspirational.
    """
    path = hermetic_aws_path(returncode=255)

    result = call_destroy_fn("resolve_account", "some-profile", path=path)

    assert result.returncode == 1
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("cli_stdout", "why"),
    [
        ("None", "the CLI prints None for a query that resolved to nothing"),
        ("12345", "too short to be an account id"),
        ("1234567890123", "thirteen digits is not an account id"),
        ("abcdefghijkl", "twelve characters, but not twelve digits"),
        ("warning: credentials expire soon\n123456789012", "a warning wrapped onto stdout ahead of the id"),
    ],
)
def test_a_cli_success_that_is_not_an_account_id_is_rejected(call_destroy_fn, hermetic_aws_path, cli_stdout, why):
    """**Validates: Requirements 1.2**

    The CLI can exit 0 while printing something that is not an account id, and
    this value is about to be reported to the operator as *the account being
    destroyed*. A value that looks like an account but is not would make that
    report a lie at the one moment the operator is relying on it, so exactly
    twelve digits is the test — not merely non-empty.
    `deploy-cdk/lib/resolve-env.ts` rejects non-12-digit values for the same
    reason on the deploy side.
    """
    path = hermetic_aws_path(stdout=cli_stdout)

    result = call_destroy_fn("resolve_account", "some-profile", path=path)

    assert result.returncode == 1, why
    assert result.stdout == ""


def test_an_empty_profile_fails_without_invoking_aws(call_destroy_fn, hermetic_aws_path):
    """**Validates: Requirements 1.2**

    Asserted as *no invocation*, not just as a non-zero return. An empty profile
    argument reaching `aws sts get-caller-identity --profile ""` resolves
    whatever ambient credentials the shell carries, which is how a teardown
    aimed at nothing in particular acquires a real account id — the failure
    `LGR-1166391-2` describes, arrived at from the other direction.
    """
    path = hermetic_aws_path(stdout="123456789012")

    result = call_destroy_fn("resolve_account", "", path=path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert not hermetic_aws_path.invocations.exists(), f"aws was invoked: {hermetic_aws_path.invocations.read_text()}"


def test_a_missing_profile_argument_fails_rather_than_aborting_the_shell(call_destroy_fn, hermetic_aws_path):
    """**Validates: Requirements 1.2**

    The harness sources the library under `set -uo pipefail`, so an unguarded
    `$1` would abort the caller's shell instead of returning. `${1:-}` is what
    makes the missing-argument arm observable as a return code — and a teardown
    tool whose failure mode is "the shell died somewhere" cannot report R1.2.
    """
    path = hermetic_aws_path(stdout="123456789012")

    result = call_destroy_fn("resolve_account", path=path)

    assert result.returncode == 1
    assert result.stdout == ""


# ── compose_skipped_stack_notice (p64 R2.2) ───────────────────────────
#
# The pure half of the opt-in continue-past-failure flag. It is in the library
# rather than the driver per `LGR-b5b76ed-1`, so it is callable directly instead
# of only through a teardown that deletes real stacks — which is the whole reason
# the notice's most important sentence (the dependency cascade warning) can be
# asserted at all.
def test_the_skip_notice_names_the_stack_and_the_region(call_destroy_fn):
    """**Validates: Requirements 2.2**

    Both, not just the stack. `EdgeWaf` is addressed in us-east-1 while every
    other stack is addressed in the operator's region (`stack_region`), so
    "Stack-X was skipped" leaves an operator unable to tell which request failed.
    """
    result = call_destroy_fn("compose_skipped_stack_notice", "AgenticDataIngestion-EdgeWaf-dev", "us-east-1", "")

    assert result.returncode == 0, result.stderr
    first_line = result.stdout.splitlines()[0]
    assert "AgenticDataIngestion-EdgeWaf-dev" in first_line
    assert "us-east-1" in first_line
    assert "SKIPPED" in first_line


def test_the_skip_notice_warns_that_later_failures_may_be_consequences_of_this_one(call_destroy_fn):
    """**Validates: Requirements 2.2**

    The cost of this flag is not the skip, it is the misreading. Continuing past
    a stack that is still standing means the stacks after it in
    `ADI_STACK_DELETION_ORDER` may fail *because* it still holds a dependency, so
    an operator who reads the cascade as several independent problems will chase
    the wrong ones. R2.2 requires the mode to "announce what it is skipping past";
    announcing the skip without announcing the consequence would satisfy the
    letter and lose the point.

    Asserted on the notice's *claim* — that a later failure may be caused by this
    one — rather than on an exact sentence, so rewording the warning is allowed
    and removing it is not.
    """
    result = call_destroy_fn("compose_skipped_stack_notice", "Stack-X", "us-west-2", "")

    assert result.returncode == 0, result.stderr
    body = result.stdout.lower()
    assert "still standing" in body, "the notice must say the stack is still there"
    assert "dependency" in body, "the notice must say why later deletions may fail"
    assert "one cause" in body, "the notice must say a run of later failures is one cause, not many"


def test_the_skip_notice_prints_a_multi_line_reason_whole(call_destroy_fn):
    """**Validates: Requirements 2.2, 2.4**

    The detail this receives from the waiter arm is the composed per-resource
    reason block (R2.3), which is multi-line by construction — one line per
    failed resource. R2.4's posture applies unchanged: nothing on this path may
    drop text, because the informative part of a CloudFormation reason is
    regularly past the first line. `${detail%%$'\\n'*}` here would discard every
    resource but the first, which is the defect p64 exists to remove.
    """
    detail = (
        "The following resource(s) failed to delete: [RoleA, BucketB].\n  RoleA: still attached\n  BucketB: not empty"
    )

    result = call_destroy_fn("compose_skipped_stack_notice", "Stack-X", "us-west-2", detail)

    assert result.returncode == 0, result.stderr
    for fragment in ("RoleA: still attached", "BucketB: not empty"):
        assert fragment in result.stdout, f"{fragment!r} was dropped from the notice"


def test_an_absent_reason_is_stated_rather_than_left_blank(call_destroy_fn):
    """**Validates: Requirements 2.2**

    Both reason lookups in `destroy.sh` are best-effort (`2>/dev/null || true`),
    so an empty detail is reachable — and it is *not* a caller error the way
    `compose_retain_retry_outcome`'s empty id list is. The skip happened whether
    or not a reason was obtainable, so this renders and says the reason is
    missing. `compose_failure_reason` takes the same posture for the same reason:
    a blank line where a reason should be reads as a tool that did not look.
    """
    result = call_destroy_fn("compose_skipped_stack_notice", "Stack-X", "us-west-2", "")

    assert result.returncode == 0, result.stderr
    assert "no reason reported" in result.stdout
    assert not any(line.strip() == "" for line in result.stdout.splitlines()), (
        "an absence must not render as a blank line"
    )


@pytest.mark.parametrize(
    ("args", "why"),
    [
        (("", "us-west-2"), "a notice about no particular stack names nothing"),
        (("Stack-X", ""), "a notice about no particular region is ambiguous between two regions"),
    ],
)
def test_a_notice_missing_its_stack_or_its_region_is_refused(call_destroy_fn, args, why):
    """**Validates: Requirements 2.2**

    The same posture `compose_retain_retry_outcome` takes on an unrecognised
    outcome: refuse loudly rather than render a report that says nothing about
    which of the nine stacks is still standing. A caller reads a rendered notice
    as if it were true.
    """
    result = call_destroy_fn("compose_skipped_stack_notice", *args)

    assert result.returncode == 2, why
    assert result.stdout == ""


# ── destroy.sh --help is self-delimiting (p64 task 1b.3's tripwire) ───
#
# `--help` printed a hardcoded `sed -n '2,29p'` range over the script's own
# header comment. Correct at the moment it was written, and wrong the moment a
# header line moves — which documenting a new flag does. `LGR-4ea43f3-5` records
# the identical defect and the identical awk remedy in `deploy.sh`, and names why
# it was silent: `--help` is asserted nowhere. These three tests are that
# assertion, and the third is the one that would have caught the original.
def _destroy_help() -> str:
    """Run `destroy.sh --help` and return its stdout, asserting a clean exit."""
    result = _run_bash('exec "$1" --help', args=[str(DESTROY_SH)])
    assert result.returncode == 0, f"--help exited {result.returncode}: {result.stderr}"
    return result.stdout


def test_help_stops_at_the_end_of_the_header_and_prints_no_shell_code():
    """**Validates: Requirements 2.2**

    The failure mode of an end line one past the block. `set -euo pipefail` is
    the first line after `destroy.sh`'s header, and the comment-stripping step
    passes a non-comment line through untouched, so an off-by-one prints a line
    of shell as if it were documentation.
    """
    help_text = _destroy_help()

    assert "set -euo pipefail" not in help_text
    assert "#!/usr/bin/env bash" not in help_text
    assert "SCRIPT_DIR=" not in help_text


def test_help_reaches_the_last_line_of_the_header_block():
    """**Validates: Requirements 2.2**

    The other failure mode: an end line short of the block truncates the usage
    text, and the operator silently loses whatever was documented last. Anchored
    on the header's real final line, read out of the file rather than transcribed,
    so this cannot drift into asserting a fixed line number by another name.
    """
    source_lines = DESTROY_SH.read_text().splitlines()
    header = [line for line in source_lines[1:]]
    # The header is the contiguous run of comment lines after the shebang; the
    # first line that is not one ends it. That is the same boundary --help's awk
    # expression derives, computed here independently.
    boundary = next(n for n, line in enumerate(header) if not line.startswith("#"))
    last_header_line = re.sub(r"^# ?", "", header[boundary - 1])

    assert last_header_line.strip(), "the header's last line is blank — this test is anchored on nothing"
    assert last_header_line in _destroy_help()


def test_every_flag_the_parser_accepts_is_documented_in_the_help_block():
    """**Validates: Requirements 2.2**

    The structural guard, and the reason this file now reads `destroy.sh` at all.
    A flag added to the `case` without a header entry is undiscoverable, and a
    header entry added without fixing the range is what broke `deploy.sh`. Both
    are one assertion: the two lists must agree.

    Read out of the argument parser rather than listed here, so adding a flag
    cannot leave this test asserting yesterday's set — the drift
    `LGR-28b4d75-5` names.
    """
    parser = DESTROY_SH.read_text().split("while [ $# -gt 0 ]; do", 1)[1].split("\ndone\n", 1)[0]
    # A `case` arm opens with its pattern and a `)`. Both shapes are in use — the
    # value-taking flags open a multi-line arm (`--profile)`) and the boolean ones
    # are one-liners (`--force) FORCE=1 ;;`), so the pattern is read off the front
    # of the line rather than the end of it. An earlier revision of this test
    # matched a trailing `)` and silently saw four of the six flags, which is the
    # same class of miss it exists to catch. The leading `-` requirement is what
    # excludes the `*)` catch-all.
    flags = {
        flag
        for line in parser.splitlines()
        if (arm := re.match(r"(-[\w-]+(?:\|-[\w-]+)*)\)", line.strip()))
        for flag in arm.group(1).split("|")
    }
    assert len(flags) >= 5, f"the parser reader found only {sorted(flags)} — it has stopped reading the arms"

    assert "--continue-past-failed-stacks" in flags, "the parser lost the flag this task added"
    help_text = _destroy_help()
    undocumented = sorted(flag for flag in flags if flag not in help_text)

    assert not undocumented, f"accepted by the parser but absent from --help: {undocumented}"


# ── The flag's two states, in the real stack-deletion loop (R2.2) ──────
#
# The loop is not a function, so it cannot be sourced. It is lifted verbatim
# between two anchor lines and run against the REAL library with `aws` shadowed
# by an argv-logging stub — the shape `LGR-4ea43f3-6` established, where the
# assertion parses the log rather than transcribing stdout. Lifting rather than
# restating is what keeps this a test of `destroy.sh` and not of a copy of it.
#
# Nothing here runs `destroy.sh` itself and no AWS call leaves the machine: the
# PATH the lifted block sees contains exactly one executable, the stub.
_ARGS_ANCHORS = ('PROFILE=""', "done")
_LOOP_ANCHORS = ("i=0", "done")
_SUMMARY_ANCHORS = ('if [ "${#ABSENT[@]}" -gt 0 ]; then', "exit 0")


def _lift_block(source: str, start: str, end: str) -> str:
    """Return `source` from the line equal to `start` through the next `end`.

    Exact whole-line matching, and `start` must be unique. Both are assertions
    rather than best-effort matches: a lift that silently found the wrong region
    would test something other than the code that ships.
    """
    lines = source.splitlines()
    starts = [n for n, line in enumerate(lines) if line == start]
    assert len(starts) == 1, f"expected exactly one {start!r} line, found {len(starts)}"
    ends = [n for n, line in enumerate(lines) if line == end and n > starts[0]]
    assert ends, f"no {end!r} line after {start!r}"
    return "\n".join(lines[starts[0] : ends[0] + 1])


@pytest.fixture
def run_stack_deletion(tmp_path):
    """Run `destroy.sh`'s real stack-deletion loop and summary against a stub CLI.

    Returns a callable
    ``(stacks, failing, continue_past=False, events_row=…) -> (result, calls)``
    where `calls` is the parsed argv log: one list of arguments per `aws`
    invocation, in order.

    The stub stands in for the *external CLI*, not for the code under test. The
    loop gates on three things only — the exit status of a delete request, the
    exit status of the waiter, and the text of two read-only reason queries — so a
    stub that reproduces those reproduces every branch.

    `#!/bin/sh` and not `#!/usr/bin/env bash`: the PATH the lifted block sees
    holds only the stub directory, so an `env`-resolved interpreter cannot be
    found and the stub would fail to exec — indistinguishable from the CLI
    failures under test. The same trap the module docstring records for the
    `resolve_account` stub.
    """
    bin_dir = tmp_path / "hermetic_bin"
    bin_dir.mkdir()
    log = tmp_path / "aws_argv"

    def run(stacks, failing, continue_past=False, events_row="RoleA\tCannot delete entity: still attached"):
        # `--output text` renders one `LogicalResourceId<TAB>Reason` row per
        # DELETE_FAILED event, and the tab is what `failed_resource_ids` splits
        # on — so the stub emits a real tab rather than a rendering of one.
        # `events_row=""` is the stack that failed for a non-resource reason:
        # no rows, hence no retainable id and no retry.
        if events_row:
            logical, _, reason = events_row.partition("\t")
            events = f"printf '%s\\t%s\\n' {shlex.quote(logical)} {shlex.quote(reason)}"
        else:
            events = ":"
        stub = bin_dir / "aws"
        stub.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}",
                    'case "$*" in',
                    f'  *"wait stack-delete-complete --stack-name {failing} "*) exit 255 ;;',
                    f'  *"describe-stacks --stack-name {failing} "*)',
                    "      printf 'The following resource(s) failed to delete: [RoleA].\\n' ;;",
                    f'  *"describe-stack-events --stack-name {failing} "*)',
                    f"      {events} ;;",
                    "esac",
                    "exit 0",
                ]
            )
            + "\n"
        )
        stub.chmod(0o755)

        source = DESTROY_SH.read_text()
        program = "\n".join(
            [
                "set -euo pipefail",
                '. "$1"',
                # The operator inputs the loop reads. ACCOUNT is deliberately not
                # account-shaped: `destroy_lib.sh` writes no account-shaped
                # literal even in a comment, because such a literal is copyable
                # and being copyable is the whole mechanism of the incident
                # `LGR-1166391-2` records. The summary only echoes it.
                'PROFILE="stub-profile"',
                'ACCOUNT="stub-account"',
                'REGION="us-west-2"',
                'ENV_NAME="dev"',
                f"CONTINUE_PAST_FAILED_STACKS={1 if continue_past else 0}",
                "STACK_NAME=({})".format(" ".join(shlex.quote(s) for s in stacks)),
                "STACK_REGION=({})".format(" ".join(["us-west-2"] * len(stacks))),
                "STACK_PROTECTED=({})".format(" ".join(["False"] * len(stacks))),
                "ABSENT=()",
                "RETAINED=()",
                "SKIPPED_STACKS=()",
                # Reporting plumbing, restated rather than lifted: these three are
                # one-liners in destroy.sh and are not the code under test. Same
                # role the `deploy_sh_failures` fixture's stub `fail` plays.
                "say()  { printf '%s\\n' \"$*\"; }",
                "item() { printf '    %s\\n' \"$*\"; }",
                "die()  { printf 'error: %s\\n' \"$*\" >&2; exit 1; }",
                _extract_bash_function(source, "skip_failed_stack"),
                _lift_block(source, *_LOOP_ANCHORS),
                _lift_block(source, *_SUMMARY_ANCHORS),
            ]
        )
        result = _run_bash(program, args=[str(DESTROY_LIB)], path=str(bin_dir))
        calls = [line.split() for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return run


def _deleted_stacks(calls):
    """The stacks a plain (non-retaining) `delete-stack` was issued for, in order."""
    return [
        call[call.index("--stack-name") + 1]
        for call in calls
        if call[:2] == ["cloudformation", "delete-stack"] and "--retain-resources" not in call
    ]


THREE_STACKS = ["AgenticDataIngestion-Hosting-dev", "AgenticDataIngestion-Compute-dev", "AgenticDataIngestion-Data-dev"]


def test_without_the_flag_a_failed_stack_stops_the_run(run_stack_deletion):
    """**Validates: Requirements 2.2**

    The default, and the half of R2.2 that is a safety property rather than a
    feature: stop-on-first-failure is a ratified posture with a stated reason
    (`destroy.sh`: "after a failed deletion the next request either fails on a
    live dependency or removes something a still-standing stack needs"), and the
    flag is opt-in *because* that reason is still true. Getting this backwards
    turns the posture into a footgun on a tool with no undo, so it is asserted
    from the argv log: the two stacks after the failure were never asked to
    delete.
    """
    result, calls = run_stack_deletion(THREE_STACKS, failing=THREE_STACKS[0])

    assert result.returncode == 1, result.stdout
    assert _deleted_stacks(calls) == [THREE_STACKS[0]], "a stack after the failure was asked to delete"
    assert "Stopped" in result.stderr
    assert "Teardown complete" not in result.stdout


def test_with_the_flag_the_remaining_stacks_are_still_deleted(run_stack_deletion):
    """**Validates: Requirements 2.2**

    The opt-in half. Asserted on the log rather than on the transcript, because
    "it said it was continuing" and "it continued" are different claims and only
    one of them is the requirement.
    """
    _, calls = run_stack_deletion(THREE_STACKS, failing=THREE_STACKS[0], continue_past=True)

    assert _deleted_stacks(calls) == THREE_STACKS, "the run did not continue past the failed stack"


def test_a_run_that_skipped_a_stack_names_it_and_exits_non_zero(run_stack_deletion):
    """**Validates: Requirements 2.2**

    Two facts, and they are one requirement. A teardown that skipped stacks did
    not succeed, so a zero exit would let CI read it as a clean sweep — and the
    operator needs to know *which* stacks are still standing, since those are the
    fixed physical names the next deploy collides with.

    This is the one assertion in this file that reads script output, because
    naming the skipped stacks on exit is itself the requirement. It parses:
    the name is looked for in the summary section *after* the SKIPPED header, so
    a stack merely mentioned in a banner earlier in the transcript does not
    satisfy it.
    """
    result, _ = run_stack_deletion(THREE_STACKS, failing=THREE_STACKS[0], continue_past=True)

    assert result.returncode != 0, "a run that skipped a stack must not exit 0"
    assert result.returncode == 1

    head, _, summary = result.stdout.partition("SKIPPED 1 stack(s) that did NOT delete")
    assert summary, f"no skipped-stack summary section was printed:\n{result.stdout}"
    named = [line.strip() for line in summary.splitlines() if line.startswith("    ")]
    assert f"{THREE_STACKS[0]} (us-west-2)" in named, f"the skipped stack was not named on exit: {named}"
    assert "Teardown INCOMPLETE" in summary
    assert "Teardown complete" not in head


def test_the_flag_is_reached_after_the_retain_resources_retry_not_instead_of_it(run_stack_deletion):
    """**Validates: Requirements 2.1, 2.2**

    The two continues in this loop are different mechanisms and this pins the
    order between them. Task 1b.2's retry still runs first, and its own
    `continue` fires only when the retry left the stack *deleted*; the skip is
    what happens when the stack is **still standing** afterwards. A flag that
    short-circuited the retry would quietly withdraw R2.1.
    """
    result, calls = run_stack_deletion(THREE_STACKS, failing=THREE_STACKS[0], continue_past=True)

    retries = [call for call in calls if "--retain-resources" in call]
    assert len(retries) == 1, f"expected exactly one retain-resources retry, got {len(retries)}"
    assert "RoleA" in retries[0], "the retry did not carry the failed logical id"
    assert _deleted_stacks(calls) == THREE_STACKS
    assert result.returncode == 1


def test_a_stack_that_fails_with_no_retainable_id_is_still_skipped(run_stack_deletion):
    """**Validates: Requirements 2.2**

    The arm where 1b.2's retry does not apply at all: a stack that failed for a
    non-resource reason has no `DELETE_FAILED` resource event, so there is
    nothing to retain and no retry to issue. The skip must not depend on the
    retry having run.
    """
    result, calls = run_stack_deletion(THREE_STACKS, failing=THREE_STACKS[0], continue_past=True, events_row="")

    assert not [call for call in calls if "--retain-resources" in call], "a retry was issued with no ids to retain"
    assert _deleted_stacks(calls) == THREE_STACKS
    assert result.returncode == 1


def test_a_run_with_no_failure_still_exits_zero_and_reports_completion(run_stack_deletion):
    """**Validates: Requirements 2.2**

    The regression guard on the exit-code change. Adding a non-zero exit for
    skipped stacks must not make the ordinary successful teardown non-zero — the
    summary's new arm is reached only when `SKIPPED_STACKS` is non-empty.
    """
    result, calls = run_stack_deletion(THREE_STACKS, failing="AgenticDataIngestion-Nothing-dev", continue_past=True)

    assert result.returncode == 0, result.stderr
    assert _deleted_stacks(calls) == THREE_STACKS
    assert "Teardown complete" in result.stdout
    assert "SKIPPED" not in result.stdout


# ── The default is OFF, asserted where it is decided (R2.2) ───────────
#
# The loop tests above SET `CONTINUE_PAST_FAILED_STACKS` in their preamble, so
# they exercise both postures and assert neither is the default — flipping the
# initialiser to 1 passes every one of them. R2.2's "MUST NOT make that mode the
# default" therefore needs its own assertion, and it belongs on the argument
# parser, which is where the default is actually decided.
#
# Behaviour, not a source grep: the initialisers and the parser are lifted
# verbatim and run with real argv, so this fails if the initialiser flips, if the
# arm is rewritten to default-on, or if the flag stops being recognised at all.
def _parse_args(*argv):
    """Run `destroy.sh`'s real initialisers and argument parser over `argv`.

    Returns the resulting `CONTINUE_PAST_FAILED_STACKS` and `FORCE` values. Both
    are read out of the shell after the parse rather than inferred, and no AWS
    call is reachable — the lifted region ends at the parser's `done`, well above
    the first `aws` invocation.
    """
    program = "\n".join(
        [
            "set -euo pipefail",
            _lift_block(DESTROY_SH.read_text(), *_ARGS_ANCHORS),
            'printf \'continue=%s force=%s\\n\' "$CONTINUE_PAST_FAILED_STACKS" "$FORCE"',
        ]
    )
    # `args` land as $1.. so the lifted `while [ $# -gt 0 ]` parses them directly.
    result = _run_bash(program, args=list(argv))
    assert result.returncode == 0, f"parse of {argv} exited {result.returncode}: {result.stderr}"
    match = re.search(r"continue=(\d+) force=(\d+)", result.stdout)
    assert match, f"could not read the parsed flags from {result.stdout!r}"
    return int(match.group(1)), int(match.group(2))


def test_continue_past_failed_stacks_is_off_unless_it_is_asked_for():
    """**Validates: Requirements 2.2**

    The half of R2.2 that is a requirement rather than a feature. Stop-on-first
    failure is ratified with a stated reason and the flag exists *because* that
    reason is still true, so an operator who did not ask for the continue mode
    must not get it. On a tool whose every action is irreversible, a default that
    walks past a standing stack is a footgun, not a convenience.
    """
    continue_past, _ = _parse_args("--profile", "stub-profile", "--region", "us-west-2")

    assert continue_past == 0


def test_the_flag_turns_the_mode_on_and_nothing_else_does():
    """**Validates: Requirements 2.2**

    The converse, plus the independence: `--force` is the other consequential
    flag on this script and it must not imply the continue mode. An operator
    passing `--force` is saying "delete these", not "delete these and walk past
    whatever fails".
    """
    assert _parse_args("--profile", "p", "--region", "r", "--continue-past-failed-stacks") == (1, 0)
    assert _parse_args("--profile", "p", "--region", "r", "--force") == (0, 1)
    assert _parse_args("--profile", "p", "--region", "r", "--force", "--continue-past-failed-stacks") == (1, 1)


# ══ AgentCore runtime sweep (p64 1b.4 / R2.5, R2.6) ═══════════════════
#
# Two layers, and the split is `LGR-b5b76ed-1`'s: the selection and the refusal
# notice are pure library functions called directly, and the driver is run whole
# with `aws` shadowed by an argv-logging stub, asserted by PARSING that log
# (`LGR-4ea43f3-6`).
#
# **NO TEST HERE CAN DELETE A RUNTIME, and that is a property of the harness
# rather than of the assertions.** The PATH the driver sees holds exactly three
# executables: the `aws` stub, and symlinks to `dirname` and `awk`, which the
# script needs to find itself and to print its own header. There is no real `aws`
# on it, so a bug in the code under test cannot reach the AgentCore control
# plane. That matters more here than for the stack tests: the fresh-account holds
# fourteen live `READY` runtimes and they are shared state.
#
# The headline assertion is the negative one — a dry run's argv log contains ZERO
# `delete-agent-runtime` calls. Dry-run-by-default is R2.6's requirement and the
# property that makes a tool that deletes AI agent runtimes safe to land, so it
# is asserted from the log rather than from the transcript: "it said it was a dry
# run" and "it issued no delete" are different claims and only one of them is the
# requirement.

# The two prefixes, and the difference between them is five live runtimes.
# `docs/ARCHITECTURE.md` § System Inventory is the authority for the whole set:
# `AgentRuntimeName: entries | 14 = 5 + 9`. No count is asserted here — the
# fixture below is a representative sample, not an inventory, and duplicating the
# 14 into a test would be a second literal for the inventory's derivation to
# drift against.
ALL_RUNTIMES_PREFIX = "agentic_data_ingestion_"
AGENT_RUNTIMES_PREFIX = "agentic_data_ingestion_agent_"

# `(name, id)` pairs shaped exactly as `list-agent-runtimes` returns them,
# confirmed against live state in task 0.3: the id is the name plus `-` plus a
# ten-character suffix. Two agent runtimes and one MCP server runtime, which is
# the smallest sample that can tell the two prefixes apart.
AGENT_RUNTIME = ("agentic_data_ingestion_agent_create_code", "agentic_data_ingestion_agent_create_code-1cxjt261tA")
OTHER_AGENT_RUNTIME = ("agentic_data_ingestion_agent_test_code", "agentic_data_ingestion_agent_test_code-9bZq04Kmr1")
MCP_RUNTIME = ("agentic_data_ingestion_s3tables_mcp", "agentic_data_ingestion_s3tables_mcp-krSrSREcYI")
FOREIGN_RUNTIME = ("someone_elses_runtime", "someone_elses_runtime-AAAAAAAAAA")

SAMPLE_RUNTIMES = [AGENT_RUNTIME, OTHER_AGENT_RUNTIME, MCP_RUNTIME, FOREIGN_RUNTIME]


def _rows(pairs) -> str:
    """Render `(name, id)` pairs as `aws --output text` renders a 2-field query."""
    return "".join(f"{name}\t{runtime_id}\n" for name, runtime_id in pairs)


def _select_agentcore(prefix: str, pairs_or_text) -> list[tuple[str, str]]:
    """Run `select_agentcore_runtimes` and return the selected `(name, id)` pairs."""
    stdin = pairs_or_text if isinstance(pairs_or_text, str) else _rows(pairs_or_text)
    result = call_destroy_lib("select_agentcore_runtimes", prefix, stdin=stdin)
    assert result.returncode == 0, result.stderr
    return [tuple(line.split("\t")) for line in result.stdout.splitlines()]


# ── select_agentcore_runtimes (R2.5) ──────────────────────────────────
def test_the_wide_prefix_selects_every_runtime_this_system_declares(call_destroy_fn):
    """**Validates: Requirements 2.5**

    The default set, and the reason it is the default: all of this system's
    runtime names are hardcoded without the `${environment}` suffix, so all of
    them collide on the next deploy. A sweep that covered only the agent runtimes
    would leave the MCP ones occupying their names — the tool failing its own
    purpose, quietly, for part of its targets.
    """
    assert _select_agentcore(ALL_RUNTIMES_PREFIX, SAMPLE_RUNTIMES) == [
        AGENT_RUNTIME,
        OTHER_AGENT_RUNTIME,
        MCP_RUNTIME,
    ]


def test_the_narrow_prefix_excludes_the_mcp_runtimes(call_destroy_fn):
    """**Validates: Requirements 2.5**

    The other half of the choice `LGR-9932c9b-1` forces this tool to make. That
    record rejected a prefix match where a second naming scheme shared the prefix,
    because "where the read-only exclusion would merely hide a real target, a
    sweep would DELETE one". Here the two candidate prefixes select genuinely
    different sets, so the tool has to name which it means — and the narrow one
    has to actually be narrower, or the argument is decorative.
    """
    assert _select_agentcore(AGENT_RUNTIMES_PREFIX, SAMPLE_RUNTIMES) == [AGENT_RUNTIME, OTHER_AGENT_RUNTIME]


def test_a_runtime_outside_the_prefix_is_never_selected(call_destroy_fn):
    """**Validates: Requirements 2.5**

    The exclusion is the safety property: everything selected is a deletion
    candidate for an AI agent runtime and there is no undo.
    """
    assert _select_agentcore(ALL_RUNTIMES_PREFIX, [FOREIGN_RUNTIME]) == []


def test_the_prefix_is_matched_against_the_name_and_not_the_id(call_destroy_fn):
    """**Validates: Requirements 2.5**

    The id is the name plus a suffix, so a match against either field looks the
    same on every well-formed row and the two only diverge on a malformed one.
    A row whose id carries the prefix while its name does not is precisely that
    divergence, and it must not be selected: the name is what identifies the
    resource as this system's.
    """
    assert _select_agentcore(ALL_RUNTIMES_PREFIX, [("someone_elses_runtime", f"{ALL_RUNTIMES_PREFIX}spoofed")]) == []


def test_the_prefix_is_matched_literally_rather_than_as_a_pattern(call_destroy_fn):
    """**Validates: Requirements 2.5**

    `grep "^$prefix"` reads the prefix as a regex and `case "$name" in "$prefix"*)`
    glob-expands it, so under either a prefix containing `.` matches a name with
    any character in that position. The `${name#"$prefix"}` form suppresses
    pattern interpretation. Same technique `select_prefixed` documents, asserted
    the same way — over-selection here is a delete.
    """
    assert _select_agentcore("agentic.data.ingestion_", SAMPLE_RUNTIMES) == []


def test_a_line_with_no_tab_is_not_selected(call_destroy_fn):
    """**Validates: Requirements 2.5**

    A one-field line is a wrapped fragment or a stray banner, not a runtime row,
    and it carries no id at all. The posture is `failed_resource_ids`' rather than
    `compose_failure_reason`': under-select when in doubt, because what is at the
    end of this path is an API argument and not a human.
    """
    assert _select_agentcore(ALL_RUNTIMES_PREFIX, f"{AGENT_RUNTIME[0]}\n") == []


@pytest.mark.parametrize(
    "bad_id,why",
    [
        ("None", "`aws --output text` prints None for a null field"),
        ("", "an empty id is a malformed delete request"),
        ("has a space", "a space means the field was not a single id"),
        ("id\textra", "a third field means this row is not the two-field shape the query pins"),
    ],
)
def test_a_row_whose_id_is_not_usable_is_not_selected(call_destroy_fn, bad_id, why):
    """**Validates: Requirements 2.5**

    The id goes straight into `delete-agent-runtime --agent-runtime-id`, so a
    field this library cannot vouch for is dropped rather than passed on. Dropping
    it means an orphan survives, which is recoverable; passing it means a delete
    request built from something that is not an id.
    """
    assert _select_agentcore(ALL_RUNTIMES_PREFIX, f"{AGENT_RUNTIME[0]}\t{bad_id}\n") == [], why


def test_an_empty_prefix_is_refused_rather_than_matching_everything(call_destroy_fn):
    """**Validates: Requirements 2.5, 2.6**

    Both readings of an empty prefix are defensible in isolation and only one is
    safe to get wrong. Selecting nothing would read as "no orphan to sweep" to a
    caller whose prefix variable came out empty; matching everything would select
    every runtime in the account, including another system's. A non-zero return
    forecloses both.
    """
    result = call_destroy_fn("select_agentcore_runtimes", "", stdin=_rows(SAMPLE_RUNTIMES))

    assert result.returncode == 2
    assert result.stdout == ""


def test_selecting_nothing_is_a_success(call_destroy_fn):
    """**Validates: Requirements 2.5**

    An account holding no orphaned runtime is the state this tool wants to find,
    not an error. Distinguished from the empty-prefix case above by the return
    code, never by the emptiness of stdout — the library's contract makes the
    return code the signal.
    """
    result = call_destroy_fn("select_agentcore_runtimes", ALL_RUNTIMES_PREFIX, stdin="")

    assert result.returncode == 0
    assert result.stdout == ""


# ── compose_agentcore_sweep_refusal (R2.5) ────────────────────────────
def test_the_refusal_names_the_stack_the_region_and_the_reported_status(call_destroy_fn):
    """**Validates: Requirements 2.5**

    The refusal is the tool's only statement about why a delete the operator
    asked for did not happen, so all three facts have to be in it. The status is
    what tells the operator whether they are looking at a healthy stack they
    should not be sweeping at all or a `DELETE_FAILED` one they need to finish
    tearing down.
    """
    result = call_destroy_fn(
        "compose_agentcore_sweep_refusal", "AgenticDataIngestion-AgentCore-dev", "us-west-2", "DELETE_FAILED"
    )

    assert result.returncode == 0, result.stderr
    assert "AgenticDataIngestion-AgentCore-dev" in result.stdout
    assert "us-west-2" in result.stdout
    assert "DELETE_FAILED" in result.stdout


def test_an_absent_status_is_stated_rather_than_left_blank(call_destroy_fn):
    """**Validates: Requirements 2.5**

    `compose_failure_reason`'s posture, applied here: a blank line where a status
    should be reads as a tool that did not look.
    """
    for status in ("", "None"):
        result = call_destroy_fn(
            "compose_agentcore_sweep_refusal", "AgenticDataIngestion-AgentCore-dev", "us-west-2", status
        )

        assert result.returncode == 0, result.stderr
        assert "no status" in result.stdout


@pytest.mark.parametrize("args", [("", "us-west-2"), ("AgenticDataIngestion-AgentCore-dev", "")])
def test_a_refusal_about_no_particular_stack_or_region_is_refused(call_destroy_fn, args):
    """**Validates: Requirements 2.5**

    `compose_skipped_stack_notice`'s posture. The region is load-bearing in a
    message about a stack, because a name without one does not say which request
    was made.
    """
    result = call_destroy_fn("compose_agentcore_sweep_refusal", *args)

    assert result.returncode == 2
    assert result.stdout == ""


# ── The driver, against an argv-logging stub (R2.6) ───────────────────
@pytest.fixture
def run_agentcore_cleanup(tmp_path):
    """Run the real `cleanup_agentcore_runtimes.sh` against a stub CLI.

    Returns a callable ``(*argv, runtimes=…, stack_status=…) -> (result, calls)``
    where `calls` is the parsed argv log: one list of arguments per `aws`
    invocation, in order. `stack_status=None` is the absent stack — the state the
    tool exists for.

    The PATH holds only the stub `aws` plus symlinks to `dirname` and `awk`, so
    no real AWS call is reachable from any test in this group. `dirname` is
    needed because the script resolves its own directory to find
    `destroy_lib.sh`; `awk` because `--help` prints the script's own header
    through it.

    The script is invoked through `"$BASH"` rather than executed directly.
    Executing it would run its `#!/usr/bin/env bash` shebang, and `env` resolves
    `bash` against PATH — which this PATH does not carry. The same trap the module
    docstring records for the `resolve_account` stub, arrived at from the shebang
    side.
    """
    bin_dir = tmp_path / "hermetic_bin"
    bin_dir.mkdir()
    for tool in ("dirname", "awk"):
        real = shutil.which(tool)
        assert real, f"{tool} is not on the ambient PATH, so the hermetic PATH cannot carry it"
        (bin_dir / tool).symlink_to(real)
    log = tmp_path / "aws_argv"

    def run(*argv, runtimes=SAMPLE_RUNTIMES, stack_status=None, delete_fails=False):
        listing = "\n".join(
            f"      printf '%s\\t%s\\n' {shlex.quote(name)} {shlex.quote(runtime_id)}" for name, runtime_id in runtimes
        )
        # A non-zero return from describe-stacks is how the CLI reports a stack
        # that is not there, and that absence is the precondition for a delete.
        describe = "      exit 254" if stack_status is None else f"      printf '%s\\n' {shlex.quote(stack_status)}"
        stub = bin_dir / "aws"
        stub.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}",
                    'case "$*" in',
                    "  *'sts get-caller-identity'*)",
                    # Not a real account. `destroy_lib.sh` writes no
                    # account-shaped literal even in a comment because such a
                    # literal is copyable, and being copyable is the whole
                    # mechanism of the incident `LGR-1166391-2` records. This one
                    # exists only because `resolve_account` validates 12 digits.
                    "      printf '123456789012\\n' ;;",
                    "  *'cloudformation describe-stacks'*)",
                    f"{describe} ;;",
                    "  *'list-agent-runtimes'*)",
                    f"{listing if listing else '      :'} ;;",
                    "  *'delete-agent-runtime'*)",
                    # Two lines, so the "printed whole" posture is observable: a
                    # `${err%%$'\n'*}` would drop the second and with it the
                    # informative half of a real CloudFormation-style message.
                    (
                        "      printf 'AccessDeniedException: not authorized\\nrole lacks "
                        "bedrock-agentcore:DeleteAgentRuntime\\n' >&2; exit 254 ;;"
                        if delete_fails
                        else "      : ;;"
                    ),
                    "esac",
                    "exit 0",
                ]
            )
            + "\n"
        )
        stub.chmod(0o755)

        result = _run_bash('"$BASH" "$@"', args=[str(AGENTCORE_CLEANUP_SH), *argv], path=str(bin_dir))
        calls = [line.split() for line in log.read_text().splitlines()] if log.exists() else []
        log.unlink(missing_ok=True)
        return result, calls

    return run


def _delete_calls(calls):
    """Every `delete-agent-runtime` invocation in the log, as argv lists."""
    return [call for call in calls if "delete-agent-runtime" in call]


def _deleted_ids(calls):
    """The `--agent-runtime-id` values a delete was issued for, in order."""
    return [call[call.index("--agent-runtime-id") + 1] for call in _delete_calls(calls)]


BASE_ARGS = ("--profile", "stub-profile", "--region", "us-west-2")


def test_a_dry_run_issues_no_delete_at_all(run_agentcore_cleanup):
    """**Validates: Requirements 2.6**

    The headline. R2.6 requires dry-run to be the default and an explicit flag to
    delete, and the only way that requirement is worth anything is if the default
    run is genuinely INERT — not merely quiet about what it did.

    Asserted from the argv log rather than from the transcript, and stated as a
    count of zero rather than as "no delete of the runtimes we expected": a delete
    of something unexpected is the failure this most needs to catch.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS)

    assert result.returncode == 0, result.stderr
    assert _delete_calls(calls) == [], f"a dry run issued a delete: {_delete_calls(calls)}"
    # Corroboration, not the assertion: every call the run did make was read-only.
    mutating = [
        call
        for call in calls
        if not any(verb in call for verb in ("get-caller-identity", "describe-stacks", "list-agent-runtimes"))
    ]
    assert mutating == [], f"a dry run made a call that is not read-only: {mutating}"


def test_the_delete_flag_deletes_by_id_and_not_by_name(run_agentcore_cleanup):
    """**Validates: Requirements 2.5, 2.6**

    Three identifier spaces are in play — the runtime name, the runtime id, and
    the CloudFormation logical id — and `delete-agent-runtime` takes the second.
    The id looks derivable from the name, which is exactly why this asserts the
    value that reached the CLI: it must be the id the listing returned, character
    for character, and never a name or something composed from one.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS, "--delete")

    assert result.returncode == 0, result.stderr
    assert _deleted_ids(calls) == [AGENT_RUNTIME[1], OTHER_AGENT_RUNTIME[1], MCP_RUNTIME[1]]
    names = {AGENT_RUNTIME[0], OTHER_AGENT_RUNTIME[0], MCP_RUNTIME[0], FOREIGN_RUNTIME[0], FOREIGN_RUNTIME[1]}
    for call in _delete_calls(calls):
        assert not names.intersection(call), f"a delete carried a name rather than an id: {call}"


def test_the_prefix_argument_reaches_the_deletes(run_agentcore_cleanup):
    """**Validates: Requirements 2.5**

    The narrow prefix is only a real choice if it changes what gets deleted, so
    this asserts it end-to-end rather than at the library boundary: the MCP
    runtime's id must not appear in the log at all.
    """
    _, calls = run_agentcore_cleanup(*BASE_ARGS, "--prefix", AGENT_RUNTIMES_PREFIX, "--delete")

    assert _deleted_ids(calls) == [AGENT_RUNTIME[1], OTHER_AGENT_RUNTIME[1]]


def test_a_standing_agentcore_stack_refuses_the_delete(run_agentcore_cleanup):
    """**Validates: Requirements 2.5, 2.6**

    The guard that does not rest on names. While the AgentCore stack exists, every
    runtime this tool can see is a LIVE, stack-managed resource rather than an
    orphan, and deleting one leaves the template claiming a resource that is gone.

    Non-zero exit, because the operator asked for a delete and did not get one —
    a zero exit here would let a script read the refusal as a completed sweep.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS, "--delete", stack_status="UPDATE_COMPLETE")

    assert result.returncode == 1, result.stdout
    assert _delete_calls(calls) == [], "a delete was issued while the AgentCore stack was still standing"
    assert "REFUSING" in result.stdout
    assert "UPDATE_COMPLETE" in result.stdout


def test_a_dry_run_against_a_standing_stack_reports_and_exits_zero(run_agentcore_cleanup):
    """**Validates: Requirements 2.6**

    The complement. A dry run that was never going to delete anything reported the
    state correctly and the operator asked for nothing, so it succeeds — but it
    says the delete would have been refused, which is the fact an operator about
    to add `--delete` needs.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS, stack_status="DELETE_FAILED")

    assert result.returncode == 0, result.stderr
    assert _delete_calls(calls) == []
    assert "would have been refused" in result.stdout


def test_an_account_that_holds_no_matching_runtime_succeeds_without_deleting(run_agentcore_cleanup):
    """**Validates: Requirements 2.5**

    A clean account is the outcome this tool wants to find. It is reported as
    such and exits 0, and — the part worth asserting — it reaches no delete even
    under `--delete`.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS, "--delete", runtimes=[FOREIGN_RUNTIME])

    assert result.returncode == 0, result.stderr
    assert _delete_calls(calls) == []
    assert "No AgentCore runtime" in result.stdout


def test_the_target_account_is_resolved_from_the_profile_before_anything_is_read(run_agentcore_cleanup):
    """**Validates: Requirements 2.5**

    `LGR-1166391-2`: no account literal exists in the script, the account is
    derived from the operator's profile, and it is echoed before anything else
    happens. Asserted on ordering because that is what makes it useful — an
    account echoed after the deletes is a receipt, not a check.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS, "--delete")

    assert calls[0][:2] == ["sts", "get-caller-identity"], f"the first call was {calls[0]}"
    assert "123456789012" in result.stdout


def test_an_empty_prefix_is_refused_by_the_driver_rather_than_defaulted(run_agentcore_cleanup):
    """**Validates: Requirements 2.5, 2.6**

    An absent `--prefix` means "use the documented default"; an explicit empty one
    means a caller whose variable came out empty. Silently substituting the
    default for the second would select every runtime this system declares on
    behalf of a caller who asked for nothing, so it exits before any AWS call.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS, "--prefix", "", "--delete")

    assert result.returncode == 2
    assert calls == [], f"aws was invoked despite the empty prefix: {calls}"


def test_every_flag_the_parser_accepts_is_documented_in_the_header(run_agentcore_cleanup):
    """**Validates: Requirements 2.6**

    `--help` prints the script's own header, so a flag the parser takes but the
    header omits is undiscoverable — the defect `LGR-4ea43f3-5` records for
    `deploy.sh` and that `destroy.sh` now guards the same way. Structural rather
    than textual: the parser's own arms are the expectation.
    """
    source = AGENTCORE_CLEANUP_SH.read_text()
    parser = _lift_block(source, "while [ $# -gt 0 ]; do", "done")
    flags = {
        flag
        for line in parser.splitlines()
        if (arm := re.match(r"(-[\w-]+(?:\|-[\w-]+)*)\)", line.strip()))
        for flag in arm.group(1).split("|")
    }
    assert len(flags) >= 6, f"the parser reader found only {sorted(flags)} — it has stopped reading the arms"
    assert "--delete" in flags, "the parser lost the flag R2.6 requires"

    result, _ = run_agentcore_cleanup("--help")
    assert result.returncode == 0, result.stderr
    undocumented = sorted(flag for flag in flags if flag not in result.stdout)

    assert not undocumented, f"accepted by the parser but absent from --help: {undocumented}"


def test_the_help_text_states_the_memory_resource_is_not_covered(run_agentcore_cleanup):
    """**Validates: Requirements 2.5**

    `list-agent-runtimes` cannot see `AWS::BedrockAgentCore::Memory` — a different
    resource type on a different API — so this system's one Memory resource is a
    blind spot by construction. The tool's name is scoped to runtimes for that
    reason, and the header says so, because a teardown tool that silently covers
    less than its category implies leaves an operator believing a resource was
    handled.
    """
    result, _ = run_agentcore_cleanup("--help")

    assert "Memory" in result.stdout
    assert "list-memories" in result.stdout


def test_a_delete_that_fails_is_reported_whole_and_the_sweep_continues(run_agentcore_cleanup):
    """**Validates: Requirements 2.4, 2.6**

    Three facts about the failure arm, and they are one behaviour.

    It continues: these resources are already unmanaged, so stopping at the first
    stubborn one would leave the rest of the collision in place for no gain —
    unlike `destroy.sh`'s stack loop, where a standing stack really does block
    what comes after it.

    It exits non-zero, because a runtime that would not delete is still live and
    still occupying its name, and a zero exit would let a script read that as a
    clean sweep.

    And it prints the CLI's reason whole. R2.4's posture, one tool over: an
    unparsed line is a formatting problem, a dropped line is a lost cause — and
    the second line here is the one that says *why* the delete was refused.
    """
    result, calls = run_agentcore_cleanup(*BASE_ARGS, "--delete", delete_fails=True)

    assert result.returncode == 1, result.stdout
    assert len(_delete_calls(calls)) == 3, "the sweep stopped at the first failure"
    assert "role lacks bedrock-agentcore:DeleteAgentRuntime" in result.stdout, "the reason was truncated"
    # Every failed runtime is named on exit, for the reason destroy.sh names its
    # skipped stacks: these are the fixed physical names the next deploy collides
    # with, so "3 failed" without the names is not actionable.
    for name, _ in (AGENT_RUNTIME, OTHER_AGENT_RUNTIME, MCP_RUNTIME):
        assert name in result.stdout, f"a runtime that failed to delete was not named on exit: {name}"


# ══ The reason-reporting pair (p64 1b.1 / R2.1, R2.3, R2.4) ═══════════
#
# `failed_resource_ids` and `compose_failure_reason` read the SAME event rows and
# hold OPPOSITE postures, which is why they are two functions rather than one
# returning both. The ids feed an API argument (`delete-stack
# --retain-resources`), so extraction must UNDER-select when in doubt; the
# reasons feed a human, so composition must NEVER drop input. Neither had a test
# before this task — task 1b.3's harness reached the driver loop around them, not
# these.
#
# `describe-stack-events --query "…[?ResourceStatus=='DELETE_FAILED']
# .[LogicalResourceId,ResourceStatusReason]" --output text` renders one
# `LogicalResourceId<TAB>Reason` row per event, so a real tab is what these
# helpers build rows from — never a rendering of one.
TAB = "\t"


def _event_rows(*pairs: tuple[str, str]) -> str:
    """Render `(logical_id, reason)` pairs as `--output text` renders them."""
    return "".join(f"{logical}{TAB}{reason}\n" for logical, reason in pairs)


def _retain_ids(stdin: str) -> list[str]:
    """Run `failed_resource_ids` over `stdin` and return the selected ids."""
    result = call_destroy_lib("failed_resource_ids", stdin=stdin)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


# The reported teardown: eleven resources failed in one stack and the operator
# was shown one reason. Built rather than transcribed, so the count is the
# generator's and cannot drift out of step with the assertions below.
ELEVEN_FAILED = tuple((f"Resource{n:02d}", f"reason number {n}") for n in range(1, 12))


# ── failed_resource_ids: de-duplication (R2.1) ────────────────────────
def test_a_resource_that_failed_on_several_attempts_is_retained_once():
    """**Validates: Requirements 2.1**

    `describe-stack-events` returns the stack's whole event history, so a stack
    whose deletion was attempted twice carries a DELETE_FAILED event per resource
    per attempt. `--retain-resources` rejects a repeated id, so passing the raw
    list turns a recoverable failure into an invalid request — the retry R2.1
    requires would fail on its own argument list.
    """
    ids = _retain_ids(
        _event_rows(
            ("RoleA", "still attached"),
            ("BucketB", "not empty"),
            ("RoleA", "still attached"),
            ("RoleA", "still attached"),
        )
    )

    assert ids == ["RoleA", "BucketB"]
    assert len(ids) == len(set(ids)), f"a repeated id reached the retain list: {ids}"


def test_the_ids_are_emitted_in_first_seen_order():
    """**Validates: Requirements 2.1**

    The events arrive newest-first, and first-seen order is what makes the retain
    list read in the order CloudFormation last reported the failures. Asserted as
    an ordering rather than as a set, because the operator reads this list beside
    the hand-recovery command `destroy.sh` prints from the same array.
    """
    assert _retain_ids(_event_rows(("Zeta", "r"), ("Alpha", "r"), ("Mid", "r"), ("Alpha", "r"))) == [
        "Zeta",
        "Alpha",
        "Mid",
    ]


# ── failed_resource_ids: the two filters are independent (R2.1) ───────
def test_a_row_with_no_tab_is_not_an_event_row():
    """**Validates: Requirements 2.1, 2.4**

    The first of the two filters, isolated. A `ResourceStatusReason` wraps onto
    further lines, and a continuation line has no tab — so it has no id field at
    all, and treating its leading word as one would put a fragment of English
    into `--retain-resources`. Note the complementary posture next door:
    `compose_failure_reason` KEEPS this same line, because dropping it would lose
    a reason.
    """
    assert _retain_ids("RoleA\n") == []
    assert _retain_ids("  because the role is still attached to a policy\n") == []


def test_a_row_that_has_a_tab_but_no_well_formed_id_is_not_selected():
    """**Validates: Requirements 2.1**

    The second filter, isolated — a row that passes the tab test and fails the
    grammar test. Either filter alone is weaker than the pair, and this is the
    case that shows why the tab test is not sufficient on its own: a wrapped
    reason line that happens to contain a tab still has no id in its first field.
    """
    assert _retain_ids(f"Cannot delete entity{TAB}must detach the policy first\n") == []


def test_both_filters_are_load_bearing_and_neither_subsumes_the_other():
    """**Validates: Requirements 2.1**

    Stated as one assertion because the pair is one safety property. Two rows,
    each passing exactly one filter, and one row passing both — so removing
    either filter makes this fail rather than leaving it green on the survivor.
    """
    stdin = "".join(
        [
            "RoleA\n",  # well-formed id, no tab
            f"Cannot delete entity{TAB}reason\n",  # tab, not an id
            f"BucketB{TAB}not empty\n",  # both
        ]
    )

    assert _retain_ids(stdin) == ["BucketB"]


# ── failed_resource_ids: the id grammar (R2.1) ────────────────────────
@pytest.mark.parametrize(
    ("logical", "why"),
    [
        ("None", "`aws --output text` prints None for a null field"),
        ("", "an empty first field is not an id"),
        ("Role A", "a space means the field was not a single token"),
        ("Role-A", "CloudFormation logical ids are alphanumeric; a hyphen is not"),
        ("Role_A", "an underscore is not alphanumeric either"),
        ("Role.A", "a dot would also make the prefix a regex if this were matched as one"),
        ("arn:aws:iam::123456789012:role/RoleA", "a physical id or ARN is not a logical id"),
    ],
)
def test_a_first_field_that_is_not_a_logical_id_is_dropped(logical, why):
    """**Validates: Requirements 2.1**

    Every value here is about to become a `--retain-resources` argument, and
    CloudFormation restricts a logical resource id to alphanumerics — so the
    grammar is exact rather than heuristic. Dropping a value means one resource
    stays stack-managed, which is recoverable; passing one means the retry R2.1
    requires is rejected outright and the whole retain list is lost with it.
    """
    assert _retain_ids(f"{logical}{TAB}some reason\n") == [], why


def test_the_stacks_own_failure_event_is_excluded_from_its_own_retain_list():
    """**Validates: Requirements 2.1**

    A `DELETE_FAILED` stack emits an event for itself, carrying the DEPLOYED
    stack name as its `LogicalResourceId`. That name is
    `AgenticDataIngestion-<Logical>-<env>` — hyphenated, so the grammar excludes
    it, and a stack cannot retain itself. Its own case because the exclusion is a
    consequence of the grammar rather than a check for it: this is what pins the
    consequence, so a widened grammar fails here instead of sending a stack name
    to `--retain-resources`.
    """
    stdin = _event_rows(
        ("AgenticDataIngestion-Data-dev", "The following resource(s) failed to delete: [RoleA]."),
        ("RoleA", "Cannot delete entity: still attached"),
    )

    assert _retain_ids(stdin) == ["RoleA"]


def test_no_event_row_at_all_is_a_success_with_an_empty_selection():
    """**Validates: Requirements 2.1**

    A stack that failed for a non-resource reason has no DELETE_FAILED resource
    event, so there is nothing to retain and no retry to issue. That is a normal
    outcome, not an error — asserted as return code 0, with the empty stdout as
    corroboration rather than as the signal, per this library's contract.
    `destroy.sh` reads the empty result as "the retry does not apply" and falls
    through, which the driver test
    `test_a_stack_that_fails_with_no_retainable_id_is_still_skipped` covers from
    the other side.
    """
    result = call_destroy_lib("failed_resource_ids", stdin="")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@given(
    lines=st.lists(
        st.one_of(
            st.sampled_from(
                [
                    "",
                    "None",
                    f"None{TAB}reason",
                    f"RoleA{TAB}still attached",
                    f"RoleA{TAB}still attached",
                    "  a wrapped continuation line",
                    f"AgenticDataIngestion-Data-dev{TAB}The following resource(s) failed",
                    f"{TAB}",
                    f"Role A{TAB}reason",
                ]
            ),
            # A small alphabet on purpose: `\r`, `\v`, `\f` and friends are line
            # boundaries to Python's splitlines() but not to bash's `read`, so
            # generating them would break the parse rather than the code.
            st.text(alphabet="abZ09_-.: \t[]", max_size=14),
        ),
        max_size=8,
    )
)
@settings(max_examples=10)
def test_every_id_this_function_emits_is_a_usable_retain_resources_argument(lines):
    """**Validates: Requirements 2.1**

    The safety invariant, over arbitrary input rather than over a fixture. Each
    of the cases above pins one rejection; this pins that there is no unlisted
    way through. Two structural properties, and neither reads the input: every
    emitted line is a well-formed logical id, and no id is emitted twice — which
    together are exactly what `--retain-resources` requires of its arguments.
    """
    result = call_destroy_lib("failed_resource_ids", stdin="".join(f"{line}\n" for line in lines))

    assert result.returncode == 0, result.stderr
    emitted = result.stdout.splitlines()
    for value in emitted:
        assert re.fullmatch(r"[A-Za-z0-9]+", value), f"not a logical id: {value!r}"
        assert value != "None", "the null-field spelling reached the retain list"
    assert len(emitted) == len(set(emitted)), f"a duplicate reached the retain list: {emitted}"


# ── compose_failure_reason: both inputs, unconditionally (R2.3) ───────
def _failure_report(stack_reason: str, stdin: str) -> str:
    """Run `compose_failure_reason` and return its report, asserting a clean exit."""
    result = call_destroy_lib("compose_failure_reason", stack_reason, stdin=stdin)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_report_carries_the_stack_level_reason_and_the_per_resource_reasons():
    """**Validates: Requirements 2.3**

    The defect R2.3 names, from the fixed side. `destroy.sh` used to consult the
    events only when the stack-level reason came back empty — and for a
    `DELETE_FAILED` stack it never is, so the branch reading the real cause was
    unreachable and the operator got the list of stuck resources with no cause.
    Both inputs are now read unconditionally, so the report answers both
    questions: WHICH resources failed, and WHY each one did.
    """
    report = _failure_report(
        "The following resource(s) failed to delete: [RoleA, BucketB].",
        _event_rows(("RoleA", "Cannot delete entity: still attached"), ("BucketB", "The bucket is not empty")),
    )

    assert "The following resource(s) failed to delete: [RoleA, BucketB]." in report
    assert "RoleA: Cannot delete entity: still attached" in report
    assert "BucketB: The bucket is not empty" in report


def test_eleven_failed_resources_are_all_reported_with_their_own_reasons():
    """**Validates: Requirements 2.3, 2.4**

    The reported incident, at its reported size: eleven resources failed in one
    stack and the operator was shown one reason. `${reason%%$'\\n'*}` is what did
    that, and `|[0]` on the events query would do it again by another route.

    Asserted as a count derived from the fixture rather than as a transcript, so
    a partial truncation — nine of eleven, say — fails as loudly as a total one.
    """
    report = _failure_report(
        "The following resource(s) failed to delete: [11 resources].",
        _event_rows(*ELEVEN_FAILED),
    )

    reported = re.findall(r"^ {2}(\w+): (.+)$", report, re.MULTILINE)

    assert reported == [(logical, reason) for logical, reason in ELEVEN_FAILED]
    assert len(reported) == len(ELEVEN_FAILED)


# ── compose_failure_reason: R2.4, nothing is dropped ──────────────────
def test_a_multi_line_resource_reason_keeps_every_line():
    """**Validates: Requirements 2.4**

    R2.4's own case. A `ResourceStatusReason` regularly wraps, and the
    informative clause is regularly past the first line — a permission problem
    reads identically to an incomplete retain list until you get to it. The
    continuation lines have no tab, so they are emitted at a deeper indent rather
    than skipped: unparsed is a formatting problem, dropped is a lost cause.
    """
    stdin = "".join(
        [
            f"RoleA{TAB}Cannot delete entity, must detach all policies first\n",
            "  (Service: Iam, Status Code: 409)\n",
            "  attached: AdiGluePolicy, AdiLambdaPolicy\n",
        ]
    )

    report = _failure_report("The following resource(s) failed to delete: [RoleA].", stdin)

    for fragment in ("(Service: Iam, Status Code: 409)", "attached: AdiGluePolicy, AdiLambdaPolicy"):
        assert fragment in report, f"{fragment!r} was dropped from the report"


def test_a_row_this_function_cannot_retain_is_still_a_row_it_reports():
    """**Validates: Requirements 2.3, 2.4**

    The deliberate divergence between the pair, and the one a future tidy-up
    would remove. `failed_resource_ids` excludes the stack's own DELETE_FAILED
    event because a hyphenated stack name is not a logical id; applying that same
    grammar here sent the row down the continuation arm, where it rendered as a
    wrapped fragment of the preceding resource's reason. The grammar exists to
    keep a bad argument out of `--retain-resources`, which is not this function's
    concern.

    Asserted as the disagreement itself — the same input, the two functions, and
    the row present in one output and absent from the other.
    """
    stdin = _event_rows(
        ("AgenticDataIngestion-Data-dev", "The following resource(s) failed to delete: [RoleA]."),
        ("RoleA", "still attached"),
    )

    assert _retain_ids(stdin) == ["RoleA"], "the stack's own event must not be retainable"
    report = _failure_report("The following resource(s) failed to delete: [RoleA].", stdin)
    assert re.search(r"^ {2}AgenticDataIngestion-Data-dev: ", report, re.MULTILINE), (
        "the stack's own event was not reported as a row"
    )


@given(
    lines=st.lists(
        st.one_of(
            st.sampled_from(
                [
                    "",
                    "None",
                    f"RoleA{TAB}still attached",
                    f"RoleA{TAB}None",
                    f"RoleA{TAB}",
                    f"{TAB}reason with no id",
                    "  a wrapped continuation line",
                    f"a{TAB}b{TAB}c",
                ]
            ),
            st.text(alphabet="abZ09_-.: \t[]", max_size=14),
        ),
        max_size=8,
    ),
    stack_reason=st.sampled_from(["", "None", "The following resource(s) failed to delete: [RoleA]."]),
)
@settings(max_examples=10)
def test_the_report_has_one_line_per_input_line_and_never_fewer(lines, stack_reason):
    """**Validates: Requirements 2.4**

    R2.4 as a **line-count invariant over arbitrary input**, which is the only
    shape that says "nothing is dropped" rather than "nothing was dropped from
    this fixture". Every substantive input line contributes exactly one output
    line, whichever arm it takes, plus one for the stack-level reason and one
    more when no resource row was found at all.

    Equality and not `>=`: a report that duplicated lines would pass an
    inequality while telling the operator a resource failed twice. Empty input
    lines are the one thing deliberately not carried — `--output text` emits one
    for an empty result set, and there is nothing in it to preserve.

    A whitespace-only input line, by contrast, IS carried, and the first draft of
    this test wrongly asserted otherwise. It read the library's "an absence must
    not render as a blank line" as "no output line may look blank", which are
    different claims: the first is about the two substituted-absence arms, pinned
    by the two `..._is_stated_rather_than_left_blank` cases below, and the second
    would require this function to drop input — the defect R2.4 exists to
    prevent. So the blank-line guard is asserted where it is true.
    """
    substantive = [line for line in lines if line]
    result = call_destroy_lib("compose_failure_reason", stack_reason, stdin="".join(f"{line}\n" for line in lines))

    assert result.returncode == 0, result.stderr
    produced = result.stdout.splitlines()
    resource_rows = [line for line in substantive if TAB in line]
    expected = 1 + len(substantive) + (0 if resource_rows else 1)

    assert len(produced) == expected, f"{len(substantive)} input line(s) produced {len(produced)}: {produced}"
    if all(line.strip() for line in substantive):
        assert not any(line.strip() == "" for line in produced), f"a blank line was emitted: {produced}"


@given(
    reason_lines=st.lists(
        st.text(alphabet="abZ09_-.: []", min_size=1, max_size=14).filter(lambda s: s.strip()),
        min_size=1,
        max_size=6,
    )
)
@settings(max_examples=10)
def test_every_line_of_a_wrapped_reason_survives_verbatim(reason_lines):
    """**Validates: Requirements 2.4**

    The content half of the same requirement: a line count can be right while
    the text is mangled. Generated continuation lines carry no tab, so each takes
    the arm that must preserve it whole, and each is asserted present verbatim.
    """
    stdin = f"RoleA{TAB}first clause\n" + "".join(f"{line}\n" for line in reason_lines)

    report = _failure_report("The following resource(s) failed to delete: [RoleA].", stdin)

    for line in reason_lines:
        assert line in report, f"{line!r} was dropped from the report"


# ── compose_failure_reason: absences are stated (R2.3) ────────────────
@pytest.mark.parametrize("stack_reason", ["", "None"])
def test_an_absent_stack_level_reason_is_stated_rather_than_left_blank(stack_reason):
    """**Validates: Requirements 2.3**

    Both lookups in `destroy.sh` are best-effort (`2>/dev/null || true`) and
    `--output text` renders a null field as `None`, so both spellings of absence
    are reachable. A blank first line where the reason should be reads as a tool
    that did not look — which is indistinguishable, to the operator, from the
    defect R2.3 describes.
    """
    report = _failure_report(stack_reason, _event_rows(("RoleA", "still attached")))

    assert report.splitlines()[0] == "no stack-level reason reported by CloudFormation"
    assert "RoleA: still attached" in report


@pytest.mark.parametrize("detail", ["", "None"])
def test_an_absent_per_resource_reason_is_stated_rather_than_left_blank(detail):
    """**Validates: Requirements 2.3**

    The same posture one field over. A resource named with nothing after the
    colon looks like a rendering bug; a resource named as having no reported
    reason is a fact the operator can act on — it means the events query
    answered, and this is what it said.
    """
    report = _failure_report("The following resource(s) failed to delete: [RoleA].", f"RoleA{TAB}{detail}\n")

    assert "RoleA: no per-resource reason reported by CloudFormation" in report


def test_a_stack_that_failed_with_no_resource_event_says_so():
    """**Validates: Requirements 2.3**

    The stack-level reason alone is exactly the report R2.3 was written against,
    so when that is genuinely all CloudFormation returned, the report says which
    of the two it is missing. Silence here would be indistinguishable from the
    unreachable-branch defect.
    """
    report = _failure_report("Resource handler returned message: timed out", "")

    assert "Resource handler returned message: timed out" in report
    assert "no DELETE_FAILED resource event reported by CloudFormation" in report


# ══ compose_retain_retry_outcome (p64 1b.2 / R2.1) ════════════════════
#
# The retry's report. Task 1b.3's driver tests reached the retry only through the
# arms its own flag interacts with — that the retry is issued once, carries the
# failed id, and is reached before the skip. Which of the three outcomes it
# reported, and whether the three are distinguishable at all, was unasserted.
RETRY_OUTCOMES_IN_DRIVER = re.compile(r"compose_retain_retry_outcome\s+([a-z]+)\b")


def _retry_report(outcome: str, stack: str, *rest: str, ids: tuple[str, ...] = ("RoleA",)):
    """Run `compose_retain_retry_outcome` over `ids` on stdin."""
    return call_destroy_lib("compose_retain_retry_outcome", outcome, stack, *rest, stdin="".join(f"{i}\n" for i in ids))


def test_the_outcomes_destroy_sh_passes_are_exactly_the_outcomes_the_library_accepts():
    """**Validates: Requirements 2.1**

    Read out of both producers, transcribed from neither — `LGR-4ea43f3-6`'s
    shape, and the reason it matters here is that the driver `die`s when this
    function returns non-zero. A spelling that drifts out of step aborts the
    teardown mid-run with "internal: could not compose the retry report", at the
    one moment the operator is relying on the report.

    Both directions. A call site the library refuses is that abort; an accepted
    arm no call site reaches is dead code that will be read as live.
    """
    used = set(RETRY_OUTCOMES_IN_DRIVER.findall(DESTROY_SH.read_text()))
    assert len(used) >= 3, f"the call-site reader found only {sorted(used)} — it has stopped reading destroy.sh"

    definition = _extract_bash_function(DESTROY_LIB.read_text(), "compose_retain_retry_outcome")
    guard = re.search(r"^\s*([a-z]+(?:\|[a-z]+)+)\)\s*;;\s*$", definition, re.MULTILINE)
    assert guard, "could not find the accepted-outcome case arm in the library"
    accepted = set(guard.group(1).split("|"))

    assert used == accepted, f"destroy.sh passes {sorted(used)}, the library accepts {sorted(accepted)}"

    # And behaviourally, not only structurally: each spelling really renders.
    for outcome in sorted(used):
        result = _retry_report(outcome, "Stack-X")
        assert result.returncode == 0, f"{outcome}: {result.stderr}"
        assert "Stack-X" in result.stdout, f"{outcome} rendered without naming the stack"


def test_the_three_outcomes_are_distinguishable_from_each_other():
    """**Validates: Requirements 2.1**

    `rejected` and `failed` are different facts and the operator's next move
    differs: a rejection means CloudFormation refused the request and the
    resources are still stack-managed, while a failure means the request was
    accepted and the retain list was not enough to free the stack. Collapsing
    them into "the retry didn't work" is the same class of lie this spec exists
    to remove.

    Asserted as pairwise distinctness of the rendered reports, so a future
    rewrite cannot quietly make two arms say the same thing.
    """
    reports = {outcome: _retry_report(outcome, "Stack-X").stdout for outcome in ("retained", "rejected", "failed")}

    assert len(set(reports.values())) == 3, "two outcomes render the same report"
    assert "SUCCEEDED" in reports["retained"]
    assert "REJECTED" in reports["rejected"]
    assert "FAILED" in reports["failed"]


def test_the_success_arm_names_every_abandoned_resource_and_says_what_that_means():
    """**Validates: Requirements 2.1**

    `--retain-resources` deletes the stack and does NOT delete the retained
    resources, so the success arm describes a stack that is gone and N resources
    that are still live, still costing money, and no longer managed by any
    template. A count alone would be read as "deleted" — and these are exactly
    the fixed physical names the next deploy collides with, so the operator needs
    the names, not the number.
    """
    ids = ("RoleA", "BucketB", "LogGroupC")

    result = _retry_report("retained", "Stack-X", ids=ids)

    assert result.returncode == 0, result.stderr
    listed = [line.strip() for line in result.stdout.splitlines() if line.startswith("      ") and line.strip()]
    assert listed == list(ids), f"the abandoned resources were not all named: {listed}"
    assert "ABANDONED" in result.stdout, "the report does not say retained means abandoned"


def test_the_non_success_arms_report_the_count_without_relisting_the_ids():
    """**Validates: Requirements 2.1**

    Nothing was abandoned on either arm, and `destroy.sh` printed the same ids
    immediately above as a hand-recovery hint — so re-listing them under a
    REJECTED or FAILED heading would suggest they are now unmanaged, which is the
    opposite of what happened. The count still appears, because how many
    resources are stuck is the part that transfers.
    """
    ids = ("RoleA", "BucketB")

    for outcome in ("rejected", "failed"):
        result = _retry_report(outcome, "Stack-X", ids=ids)
        assert result.returncode == 0, result.stderr
        assert "2 resource(s)" in result.stdout, f"{outcome} did not report the count"
        listed = [line.strip() for line in result.stdout.splitlines() if line.startswith("      ") and line.strip()]
        assert listed == [], f"{outcome} re-listed ids that were not abandoned: {listed}"


@given(count=st.integers(min_value=1, max_value=6))
@settings(max_examples=10)
def test_the_reported_count_is_the_length_of_the_list_it_introduces(count):
    """**Validates: Requirements 2.1**

    stdin is consumed once, for the list AND for the count, so the two cannot
    disagree — asserted by parsing the rendered count back out and comparing it
    to the ids actually listed. A report claiming three abandoned resources above
    a list of two is a statement about live infrastructure that is wrong by one.
    """
    ids = tuple(f"Resource{n}" for n in range(count))

    result = _retry_report("retained", "Stack-X", ids=ids)

    assert result.returncode == 0, result.stderr
    reported = re.search(r"(\d+) resource\(s\) RETAINED", result.stdout)
    assert reported, f"no count in the report:\n{result.stdout}"
    listed = [line.strip() for line in result.stdout.splitlines() if line.startswith("      ") and line.strip()]

    assert int(reported.group(1)) == count == len(listed)


@pytest.mark.parametrize(
    ("outcome", "stack", "ids", "why"),
    [
        ("succeeded", "Stack-X", ("RoleA",), "a fourth spelling says nothing about which of the three happened"),
        ("", "Stack-X", ("RoleA",), "an empty outcome is not one of the three"),
        ("Retained", "Stack-X", ("RoleA",), "the vocabulary is exact, and a near-miss must not fall through"),
        ("retained", "", ("RoleA",), "a report about no particular stack names nothing"),
        ("retained", "Stack-X", (), "there is no retry to report unless something was retained"),
    ],
)
def test_a_report_that_would_say_nothing_useful_is_refused(outcome, stack, ids, why):
    """**Validates: Requirements 2.1**

    Return code 2 with no stdout, for all five. The empty-stdin case is the one
    worth separating from `select_prefixed`'s empty result: there emptiness is
    data and success is right, whereas a zero-length retain list would render
    "with 0 resource(s) RETAINED" above an empty list — a statement about a retry
    that cannot have happened, printed in the middle of a real teardown.

    `destroy.sh` only calls this inside its own non-empty guard, so the refusal is
    unreachable from the one caller today. It is asserted so that dropping that
    guard fails loudly instead of printing the lie.
    """
    result = _retry_report(outcome, stack, ids=ids)

    assert result.returncode == 2, why
    assert result.stdout == ""


def test_the_cli_detail_is_printed_whole_rather_than_to_its_first_newline():
    """**Validates: Requirements 2.1, 2.4**

    R2.4's posture on this function's third argument. A CloudFormation rejection
    routinely puts the useful clause past the first newline, and this text is the
    whole reason an operator can tell a permission problem from an incomplete
    retain list. Asserted as a line count as well as by content, so a detail that
    is present but collapsed onto one line fails too.
    """
    detail = "ValidationError: cannot retain a resource that is not in DELETE_FAILED\n  offending: BucketB\n  (Service: CloudFormation, Status Code: 400)"

    result = _retry_report("rejected", "Stack-X", detail)

    assert result.returncode == 0, result.stderr
    carried = [line.strip() for line in result.stdout.splitlines() if line.startswith("      ")]
    assert carried == [line.strip() for line in detail.splitlines()]


def test_an_absent_cli_detail_renders_nothing_rather_than_a_blank_line():
    """**Validates: Requirements 2.1**

    The `retained` arm passes no detail at all, so the absent case is the common
    one rather than an edge. `<<<"$detail"` yields one empty line for an empty
    string, and emitting it would put a stray blank into the middle of the
    teardown transcript — the report reads as truncated.
    """
    result = _retry_report("retained", "Stack-X")

    assert result.returncode == 0, result.stderr
    assert not any(line.strip() == "" for line in result.stdout.splitlines())
