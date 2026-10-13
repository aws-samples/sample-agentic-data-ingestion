# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The `target_config` shapes the JDBC wizard actually emits, one per connection mode.

Shared fixture data, not a test module. Lives here rather than inline in one
test file because both the contract layer (`tests/contracts/api_responses/
test_jdbc_target_config_contract.py`, which pins these against the keys
`create_pipeline` enforces) and the unit layer can need the same payload, and a
second inlined copy is how the two drift.

Provenance — every key below is traced to the component that sets it. Line
numbers are deliberately omitted: `JdbcConnectionSelector.tsx` is under
concurrent edit for the R10 presentation work, and a fixture that cites line
numbers goes stale on a cosmetic change.

`dashboard-react/src/pages/wizard/JdbcTargetForm.tsx`
    Sets `type = 'JDBC'` unconditionally (Requirement 14.4 — the legacy
    `'RDS_DATABASE'` spelling is no longer produced), derives
    `jdbc_table` as `database_name.table_name` whenever both are present, and
    defaults `write_mode` to `'overwrite'`.

`dashboard-react/src/pages/wizard/JdbcConnectionSelector.tsx`
    `handleRdsSelect` populates the `select_rds` block from the chosen RDS
    instance / cluster: `engine`, `jdbc_url` (built from endpoint + port + db
    name), `jdbc_driver` (from `JDBC_DRIVER_MAP`), the identifier key, and the
    networking triple. `handleGlueConnectionSelect` populates the
    `glue_connection` block from the selected connection's metadata:
    `glue_connection_name`, `jdbc_url`, `jdbc_driver` — and *nothing else*.
    The `secret_arn` dropdown is `select_rds`-only.

`dashboard-react/src/pages/wizard/ManualConnectionForm.tsx`
    The `manual` block: `engine` (which auto-populates `jdbc_driver`),
    `jdbc_url`, `vpc_id`, `subnet_id` + `availability_zone` (both persisted
    from one subnet choice), `security_group_ids`, and `secret_arn`.

These shapes are not taken on trust. `test_jdbc_target_config_contract.py`
reads the three components above at test time and asserts that every key they
assign appears here, so this module cannot quietly go stale while still being
used as the "what the wizard emits" side of a contract (``LGR-28b4d75-5``: a
transcribed field list cannot detect the thing it transcribes drifting).

Two transport details that are easy to lose and are the reason these shapes are
worth pinning as data:

* `security_group_ids` is **JSON-encoded as a string**, not a list.
  `WizardState.targetConfig` is a `Record<string, string>`, so the wizard
  cannot put a real array in it. The backend's `_normalise_sg_list` accepts
  both shapes; the wizard only ever sends the string.
* The `glue_connection` shape carries **no `secret_arn` and no `engine`**. Both
  are resolved server-side by `_augment_glue_connection_config`: `secret_arn`
  from the connection's `SECRET_ID`, and a connection with none is refused on
  the target side as on the source side (T-19, `66e97e5`; a connection's
  inline credentials are never used). C8 / invariant ``LGR-819980f-1``.

Every secret-shaped value here is a realistically-formed but entirely fake
Secrets Manager ARN. No plaintext credential appears in this module, and none
may be added: credentials are referenced by ARN only (``LGR-819980f-1``).
"""

from __future__ import annotations

import json
from typing import Any

# A syntactically valid but fake Secrets Manager ARN. Matches the backend's
# `SECRET_ARN_RE`. Never a plaintext credential (`LGR-819980f-1`).
FAKE_SECRET_ARN = "arn:aws:secretsmanager:us-west-2:123456789012:secret:adi/target-db-Xk3Qz9"

# The three connection modes `JdbcConnectionSelector` offers, in the order the
# selector renders them.
JDBC_CONNECTION_MODES: tuple[str, ...] = ("select_rds", "glue_connection", "manual")

_TARGET_TABLE_IDENTITY = {
    "database_name": "analytics",
    "table_name": "users_mirror",
    # Derived by JdbcTargetForm as `database_name.table_name`.
    "jdbc_table": "analytics.users_mirror",
}


def emitted_jdbc_target_config(mode: str) -> dict[str, Any]:
    """The `target_config` the wizard emits for connection `mode`.

    Returns a fresh dict each call — `create_pipeline` mutates `target_config`
    in place (it back-fills `write_mode` and may set `secret_arn`), so callers
    must not share one instance.
    """
    if mode not in JDBC_CONNECTION_MODES:
        raise ValueError(f"unknown connection mode {mode!r}; expected one of {JDBC_CONNECTION_MODES}")

    # Set by JdbcTargetForm for every mode.
    config: dict[str, Any] = {
        "type": "JDBC",
        "write_mode": "overwrite",
        "_connection_mode": mode,
        **_TARGET_TABLE_IDENTITY,
    }

    if mode == "select_rds":
        config.update(
            {
                "_selected_rds": "target-postgres",
                "db_instance_identifier": "target-postgres",
                "engine": "postgres",
                "jdbc_url": "jdbc:postgresql://target-postgres.abc123.us-west-2.rds.amazonaws.com:5432/analytics",
                "jdbc_driver": "org.postgresql.Driver",
                "_port": "5432",
                "subnet_id": "subnet-0a1b2c3d4e5f6789a",
                # JSON-encoded, not a list — see module docstring.
                "security_group_ids": json.dumps(["sg-0a1b2c3d4e5f6789a"]),
                "availability_zone": "us-west-2a",
                # The selector's secret dropdown is select_rds-only.
                "secret_arn": FAKE_SECRET_ARN,
            }
        )
    elif mode == "glue_connection":
        # Deliberately minimal. `engine` and `secret_arn` are absent because
        # the selector never asks for them in this mode.
        config.update(
            {
                "glue_connection_name": "customer-owned-postgres",
                "jdbc_url": "jdbc:postgresql://customer-db.example.internal:5432/analytics",
                "jdbc_driver": "org.postgresql.Driver",
            }
        )
    else:  # manual
        config.update(
            {
                "engine": "postgres",
                "jdbc_url": "jdbc:postgresql://onprem-db.example.internal:5432/analytics",
                "jdbc_driver": "org.postgresql.Driver",
                # `vpc_id` is manual-only: ManualConnectionForm auto-selects a
                # VPC so the subnet dropdown has something to scope to. No
                # backend validator requires it — it is here because the wizard
                # sends it, which is what this fixture is a record of.
                "vpc_id": "vpc-0a1b2c3d4e5f6789a",
                "subnet_id": "subnet-0a1b2c3d4e5f6789a",
                "security_group_ids": json.dumps(["sg-0a1b2c3d4e5f6789a"]),
                "availability_zone": "us-west-2a",
                "secret_arn": FAKE_SECRET_ARN,
            }
        )

    return config


def create_pipeline_request_body(
    target_config: dict[str, Any],
    *,
    pipeline_name: str = "p31 jdbc target contract",
) -> str:
    """A `create_pipeline` request body carrying `target_config`.

    The source is left at the `S3_UPLOAD` default so nothing on the source side
    can be the reason for a rejection — the target is the only variable.
    """
    return json.dumps(
        {
            "pipeline_name": pipeline_name,
            "target_schema": "uploaded_test_schema",
            "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
            "target_config": target_config,
        }
    )
