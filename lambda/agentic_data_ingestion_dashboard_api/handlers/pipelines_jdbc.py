#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""JDBC/RDS source+target validation and Glue-connection provisioning.

Extracted verbatim from ``handlers/pipelines.py`` (P21 Task 7.2 — module
decomposition). Behaviour is unchanged; ``handlers/pipelines.py`` re-exports
the public names for router/test compatibility.
"""

import json
import re
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from jdbc_helpers import (
    SUPPORTED_ENGINES,
    create_glue_jdbc_connection,
    initial_watermark_entry,
    is_managed_glue_connection,
    validate_identifier,
    validate_watermark_value,
)

from .common import logger

# `_get_glue_connection_tags_safe` used to live here and has been removed. Glue
# Connections are not a taggable resource type — `GetTags` on a
# `…:connection/<name>` ARN raises `InvalidInputException` — so it caught that
# exception and returned `{}` on every call. Its caller now reads `SECRET_ID` from
# `ConnectionProperties`, which is what the `or` branch already resolved to. The
# full reasoning, including how this hid the defect, is at that call site.


# Required keys on every JDBC source_config payload, regardless of read mode.
_JDBC_SOURCE_REQUIRED_FIELDS: tuple[str, ...] = (
    "jdbc_url",
    "jdbc_driver",
    "jdbc_schema",
    "jdbc_table",
    "secret_arn",
    "engine",
    "read_mode",
)

# Additional required keys when read_mode == "incremental".
_JDBC_SOURCE_INCREMENTAL_REQUIRED_FIELDS: tuple[str, ...] = (
    "watermark_column",
    "watermark_type",
)

_JDBC_READ_MODES: frozenset[str] = frozenset({"full_table", "incremental"})

_JDBC_WATERMARK_TYPES: frozenset[str] = frozenset({"numeric", "timestamp"})

# Required keys on every manual-mode source_config / target_config payload.
# `availability_zone` is intentionally absent — Glue derives it from the
# subnet when omitted (Req 6.3, design Component 9).
_JDBC_MANUAL_REQUIRED_FIELDS: tuple[str, ...] = (
    "engine",
    "jdbc_url",
    "jdbc_driver",
    "secret_arn",
    "subnet_id",
    "security_group_ids",
)

# Validation regexes for the manual-mode networking + secret fields.
# These are the same regexes the wizard enforces client-side; validating
# server-side as defence-in-depth surfaces a clean HTTP 400 instead of
# letting Glue fail at runtime with an opaque error.
SECRET_ARN_RE = re.compile(r"^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[\w/+=.@-]+$")

SUBNET_ID_RE = re.compile(r"^subnet-[a-f0-9]{8,17}$")

SG_ID_RE = re.compile(r"^sg-[a-f0-9]{8,17}$")


def _normalise_sg_list(raw: Any, field_name: str) -> list[str]:
    """Coerce ``raw`` into a ``list[str]`` of security-group IDs.

    The wizard JSON-encodes the list at submission time (Req 9.3 — the
    `WizardState.sourceConfig` shape is `Record<string, string>`). The
    backend accepts both shapes:

    * a real ``list[str]`` (e.g. from `select_rds` mode where the wizard
      sends a real array).
    * a JSON-encoded string `'["sg-...", "sg-..."]'` from manual mode.

    Returns the normalised list. Raises :class:`ValueError` with
    ``field_name`` in the message on parse failure or when the result is
    not a non-empty list of strings — callers translate to HTTP 400.

    Validates: P20 Req 9.3, Property 7 (JSON round-trip).
    """
    if isinstance(raw, list):
        if not all(isinstance(item, str) for item in raw):
            raise ValueError(f"{field_name} must be a list of strings; got mixed types")
        return list(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be a JSON-encoded list of strings: {exc}") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{field_name} JSON value must be a list of strings")
        return parsed
    raise ValueError(f"{field_name} must be a list of strings or JSON-encoded list; got {type(raw).__name__}")


def _validate_jdbc_source_config(source_config: dict[str, Any]) -> None:
    """Validate ``source_config`` for ``source_type == "JDBC"`` payloads.

    Mirrors the per-target validation pattern already used for
    ``target_config`` in :func:`create_pipeline`. Raises :class:`ValueError`
    with a descriptive message on any failure; callers translate that to
    HTTP 400.

    P20 — dispatch on ``_connection_mode``: ``select_rds``,
    ``glue_connection``, or ``manual``. Pre-feature payloads without
    the field default to ``select_rds`` for backward compatibility
    (Req 12.1). The ``select_rds`` and ``glue_connection`` validation
    rules are unchanged from P17 (Req 6.6, Req 12.5).

    Args:
        source_config: The raw ``source_config`` map from the request body.

    Raises:
        ValueError: If ``source_config`` is not a dict or fails any of
            the per-mode validation rules.
    """
    if not isinstance(source_config, dict):
        raise ValueError("source_config must be an object when source_type is JDBC")

    mode = source_config.get("_connection_mode", "select_rds")
    if mode == "manual":
        _validate_jdbc_source_config_manual(source_config)
    elif mode == "glue_connection":
        _validate_jdbc_source_config_glue_connection(source_config)
    else:
        # 'select_rds' or legacy payloads with no _connection_mode field
        _validate_jdbc_source_config_select_rds(source_config)


def _validate_jdbc_source_required_common(source_config: dict[str, Any]) -> None:
    """Validate the required-fields common to ALL JDBC source modes.

    The same field set is required regardless of connection mode — what
    differs across modes is *where* the values come from (RDS-derived
    vs. user-entered vs. Glue-Connection-derived). Centralising the
    common check keeps the per-mode validators thin.
    """
    # Always-required fields
    for field in _JDBC_SOURCE_REQUIRED_FIELDS:
        if not source_config.get(field):
            raise ValueError(f"source_config.{field} is required when source_type is JDBC")

    engine = source_config["engine"]
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"source_config.engine must be one of {sorted(SUPPORTED_ENGINES)}; got {engine!r}")

    read_mode = source_config["read_mode"]
    if read_mode not in _JDBC_READ_MODES:
        raise ValueError(f"source_config.read_mode must be one of {sorted(_JDBC_READ_MODES)}; got {read_mode!r}")

    # Identifier validation — defence-in-depth re-check at the API boundary
    # so the wizard's regex check is mirrored server-side and propagates a
    # clean HTTP 400 instead of failing later in Glue.
    for ident_field in ("jdbc_schema", "jdbc_table"):
        try:
            validate_identifier(source_config[ident_field])
        except ValueError as exc:
            raise ValueError(f"source_config.{ident_field} is not a valid SQL identifier: {exc}") from exc

    # Incremental-only fields
    if read_mode == "incremental":
        for field in _JDBC_SOURCE_INCREMENTAL_REQUIRED_FIELDS:
            if not source_config.get(field):
                raise ValueError(f"source_config.{field} is required when read_mode is 'incremental'")

        watermark_type = source_config["watermark_type"]
        if watermark_type not in _JDBC_WATERMARK_TYPES:
            raise ValueError(
                f"source_config.watermark_type must be one of {sorted(_JDBC_WATERMARK_TYPES)}; got {watermark_type!r}"
            )

        try:
            validate_identifier(source_config["watermark_column"])
        except ValueError as exc:
            raise ValueError(f"source_config.watermark_column is not a valid SQL identifier: {exc}") from exc

        # Optional initial seed value — validated only when present and
        # non-empty so empty strings (the wizard's default for "no seed")
        # don't trip the regex check. Resolved through the same
        # `initial_watermark_entry` code generation uses, so what is
        # validated here is exactly what the job will read. This used to read
        # `initial_watermark_value`, a key the wizard never sends, so the
        # wizard's seed was never validated (and never used either).
        initial = initial_watermark_entry(source_config)
        if initial is not None:
            initial_key, initial_value = initial
            try:
                validate_watermark_value(initial_value, watermark_type)
            except ValueError as exc:
                raise ValueError(f"source_config.{initial_key} is not a valid {watermark_type} value: {exc}") from exc


def _derive_engine_from_jdbc_url(jdbc_url: str) -> str | None:
    """Return the canonical engine key implied by a JDBC URL prefix.

    AgenticDataIngestion's wizard never asks the user to pick an engine in
    ``glue_connection`` mode — the user-selected Glue Connection already
    carries the JDBC URL, and the engine is implied by the URL's
    protocol prefix. This helper recovers a canonical
    :data:`SUPPORTED_ENGINES` value from the prefix so server-side
    validation succeeds and downstream codegen has a concrete engine
    string to work with.

    Returns ``None`` for unrecognised prefixes; callers translate that
    into an HTTP 400 because we genuinely cannot infer the engine for
    a connection whose URL starts with something we don't recognise.

    Note: for engines that have an Aurora variant (``mysql`` /
    ``aurora-mysql``, ``postgres`` / ``aurora-postgresql``) we return
    the non-Aurora value because the JDBC URL prefix is identical and
    the runtime template behaviour is the same.
    """
    if not isinstance(jdbc_url, str):
        return None
    if jdbc_url.startswith("jdbc:postgresql:"):
        return "postgres"
    if jdbc_url.startswith("jdbc:mysql:"):
        return "mysql"
    if jdbc_url.startswith("jdbc:mariadb:"):
        return "mariadb"
    if jdbc_url.startswith("jdbc:oracle:"):
        return "oracle-se2"
    if jdbc_url.startswith("jdbc:sqlserver:"):
        return "sqlserver-se"
    return None


def _augment_glue_connection_config(config: dict[str, Any], field_prefix: str) -> None:
    """Fill in ``secret_arn`` / ``engine`` from a Glue Connection's metadata.

    In ``glue_connection`` mode the wizard only forwards the Glue
    Connection name plus the connection's JDBC URL/driver — it does
    NOT forward ``secret_arn`` or ``engine`` because the user never
    types those values. Both are still required by the canonical
    pipeline_record shape (P17 Req 6.3) so we resolve them server-side
    by reading the named Glue Connection's properties:

      * ``secret_arn``: read from ``ConnectionProperties.SECRET_ID`` (the
        ``adi:secret_arn`` tag is not readable — see the comment at the read).
        A connection with no SECRET_ID is rejected on BOTH sides, with the
        same remediation: neither the JDBC source job nor the JDBC target
        write can use a connection's inline ``USERNAME`` / ``PASSWORD``
        without passing them as plaintext Glue job arguments, which standing
        invariant ``LGR-819980f-1`` forbids. The target side used to tolerate
        it; see the ``if not secret_id:`` branch below for why it no longer
        does, and for the part of ``LGR-c246ab8-1`` that this supersedes.

      * ``engine``: derived from the connection's
        ``JDBC_CONNECTION_URL`` prefix via
        :func:`_derive_engine_from_jdbc_url`.

    Mutates ``config`` in place. Idempotent — when both fields are
    already populated this is a no-op so wizard sessions that DO
    surface the values continue to work.

    Args:
        config: The raw ``source_config`` or ``target_config`` map.
        field_prefix: ``"source_config"`` or ``"target_config"``, used
            to produce error messages with the right scope name.

    Raises:
        ValueError: When the connection cannot be fetched, when the
            connection lacks a ``SECRET_ID`` property (either side), or when
            the JDBC URL prefix is unrecognised so the engine cannot be
            derived.
    """
    if not isinstance(config, dict):
        # Let the validator surface the clean "must be an object" error
        # instead of crashing with AttributeError on .get().
        return

    if config.get("_connection_mode") != "glue_connection":
        return

    connection_name = config.get("glue_connection_name")
    if not connection_name:
        # The validator will surface a clean error for this; no point
        # re-raising here.
        return

    # Fast path — both already populated, no need to call Glue.
    if config.get("secret_arn") and config.get("engine"):
        return

    glue_client = boto3.client("glue")
    try:
        response = glue_client.get_connection(Name=connection_name)
    except glue_client.exceptions.EntityNotFoundException as exc:
        raise ValueError(
            f"{field_prefix}.glue_connection_name {connection_name!r} does not "
            "exist; pick an existing Glue Connection or switch to a different "
            "connection mode"
        ) from exc
    except (ClientError, BotoCoreError) as exc:
        raise ValueError(f"Failed to fetch Glue Connection {connection_name!r}: {exc}") from exc

    connection = response.get("Connection", {}) or {}
    props = connection.get("ConnectionProperties", {}) or {}

    if not config.get("secret_arn"):
        # Resolve the Secrets Manager ARN that backs this connection.
        # AgenticDataIngestion-provisioned connections store it BOTH in the
        # ``adi:secret_arn`` tag and the ``SECRET_ID`` connection
        # property (Glue requires SECRET_ID for credentials). User-managed
        # connections may carry only one. Check the tag first (cheap, set
        # by our provisioner), then fall back to the SECRET_ID property.
        # `SECRET_ID` only. The `adi:secret_arn` tag used to be consulted first
        # here; it is INERT and always was, so this is behaviour-preserving.
        #
        # Glue Connections are not a taggable resource type: `GetTags` and
        # `TagResource` both reject a `…:connection/<name>` ARN with
        # `InvalidInputException`. Verified against the live API, including on
        # connections our own provisioner created while passing
        # `Tags={"adi:secret_arn": …}` to `CreateConnection` — they read back
        # `SECRET_ID` correctly and still fail `GetTags`. Tagging a Glue
        # *database* succeeds, which rules out a credentials or ARN-format
        # problem.
        #
        # `_get_glue_connection_tags_safe` caught that exception and returned
        # `{}`, so `tags.get("adi:secret_arn")` evaluated to None on every call
        # and the expression already reduced to what is written here. Dropping it
        # also removes an STS `GetCallerIdentity` and a doomed Glue call from
        # every JDBC validation.
        #
        # This does NOT touch the other half of `LGR-819980f-2`: `SECRET_ID` in
        # `ConnectionProperties` is load-bearing, is what Glue consults at job
        # runtime, and is still required. That record's claim that the tag is a
        # working "read-path mirror" is the part that does not hold.
        secret_id = props.get("SECRET_ID")
        if not secret_id:
            # Glue Connections can carry credentials two ways: a Secrets
            # Manager reference (``SECRET_ID``) OR inline ``USERNAME`` /
            # ``PASSWORD`` properties. Only the first is usable by this system,
            # on EITHER side, and both sides are now refused the same way:
            #
            #   * SOURCE (``source_config``): the JDBC source PySpark template
            #     (glue_job_jdbc_source.py.j2) requires ``secret_arn`` and
            #     calls get_secret_value() at runtime. Unchanged.
            #
            #   * TARGET (``target_config``): CHANGED. This branch used to log
            #     and proceed, because job_arguments.py then copied the
            #     connection's inline USERNAME/PASSWORD — or the plaintext
            #     behind its SECRET_ID — into ``--{prefix}jdbc_user`` /
            #     ``--{prefix}jdbc_password`` job arguments. That fallback IS
            #     the violation of standing invariant ``LGR-819980f-1`` (JDBC
            #     credentials only ever by Secrets Manager ARN; plaintext never
            #     in Glue job arguments), and it is what leaked a target
            #     credential. The fallback is gone: the generated JDBC write
            #     always fetches by ``secret_arn`` at run time, and
            #     ``build_jdbc_arguments`` raises when no reference resolves. A
            #     target connection without SECRET_ID therefore cannot run, and
            #     refusing it here turns a failure at Glue-job creation, three
            #     steps into the first run, into a 400 at the wizard.
            #
            # SUPERSEDES PART OF ``LGR-c246ab8-1`` (template / ratified). That
            # record kept ``secret_arn`` out of the mode-independent JDBC target
            # checks because "the user's existing connection may carry inline
            # credentials that ``_augment_glue_connection_config`` deliberately
            # leaves unresolved on the target side". That tolerance is withdrawn
            # in favour of the invariant. The rest of the record stands: the
            # ``select_rds`` check is still keyed on ``glue_connection_name``,
            # and ``glue_connection`` mode still needs no ``secret_arn`` in the
            # REQUEST — this function supplies it from SECRET_ID or refuses.
            #
            # The remediation names SECRET_ID, not the tag. An earlier wording
            # told the operator to "add the adi:secret_arn tag", which cannot be
            # done — Glue rejects TagResource on a connection ARN. It also
            # states the mutual exclusivity, because that is the trap: Glue
            # refuses SECRET_ID *alongside* USERNAME/PASSWORD with "Validation
            # for connection properties failed", so adding it is not enough —
            # the inline pair has to go in the same call.
            side = "source" if field_prefix == "source_config" else "target"
            raise ValueError(
                f"{field_prefix}.secret_arn is required, but the Glue "
                f"Connection {connection_name!r} carries no SECRET_ID "
                f"property; the JDBC {side} job reads credentials from "
                "Secrets Manager at runtime, and plaintext credentials are "
                "never passed to the Glue job. Fix the connection by setting "
                "SECRET_ID to the secret's ARN in its ConnectionProperties "
                "— note Glue rejects SECRET_ID together with inline "
                "USERNAME/PASSWORD, so remove those in the same "
                "UpdateConnection call — or switch to select_rds / manual "
                "connection mode."
            )
        config["secret_arn"] = secret_id

    if not config.get("engine"):
        jdbc_url = props.get("JDBC_CONNECTION_URL") or config.get("jdbc_url", "")
        derived = _derive_engine_from_jdbc_url(jdbc_url)
        if not derived:
            raise ValueError(
                f"{field_prefix}.engine could not be derived from the Glue "
                f"Connection {connection_name!r}'s JDBC URL "
                f"({jdbc_url!r}); the URL must start with one of "
                "jdbc:postgresql:, jdbc:mysql:, jdbc:mariadb:, jdbc:oracle:, "
                "jdbc:sqlserver:"
            )
        config["engine"] = derived


def _validate_jdbc_source_config_select_rds(source_config: dict[str, Any]) -> None:
    """Validate a ``select_rds`` source_config (P17 behaviour, unchanged).

    Validates: P17 Req 6.3, P20 Req 6.6, Req 12.1, Req 12.5.
    """
    _validate_jdbc_source_required_common(source_config)


def _validate_jdbc_source_config_glue_connection(
    source_config: dict[str, Any],
) -> None:
    """Validate a ``glue_connection`` source_config (P17 behaviour, unchanged).

    The wizard populates ``jdbc_url`` / ``jdbc_driver`` / ``secret_arn``
    from the user-selected Glue Connection's metadata; the validation
    is identical to ``select_rds`` because the resulting `source_config`
    shape is identical.

    Validates: P17 Req 6.3, P20 Req 6.6, Req 12.1, Req 12.5.
    """
    _validate_jdbc_source_required_common(source_config)


def _validate_jdbc_source_config_manual(source_config: dict[str, Any]) -> None:
    """Validate a ``manual`` source_config (P20 — new).

    Manual mode requires the same JDBC-identity fields as the other two
    modes plus the user-supplied networking fields (``subnet_id``,
    ``security_group_ids``). ``availability_zone`` is optional — Glue
    derives it from the subnet when omitted (Req 6.3).

    On success, this function MUTATES ``source_config["security_group_ids"]``
    by replacing the JSON-encoded string (or the original list) with the
    parsed ``list[str]`` so downstream readers (the physical-requirements
    helper) see a uniform shape.

    Raises:
        ValueError: With ``source_config.<field>`` named in the message
            for any required field missing or any regex mismatch.

    Validates: P20 Req 6.1, 6.2, 6.4.
    """
    # Same JDBC-identity + read-mode validation as the other two modes.
    _validate_jdbc_source_required_common(source_config)

    # Manual-mode-specific networking fields.
    for field in _JDBC_MANUAL_REQUIRED_FIELDS:
        # `engine` / `jdbc_url` / `jdbc_driver` / `secret_arn` are checked
        # by the common validator above; only the new networking fields
        # need re-checking here for presence.
        if field in ("engine", "jdbc_url", "jdbc_driver", "secret_arn"):
            continue
        if not source_config.get(field):
            raise ValueError(
                f"source_config.{field} is required when source_type is JDBC and connection mode is manual"
            )

    if not source_config["jdbc_url"].startswith("jdbc:"):
        raise ValueError("source_config.jdbc_url must start with 'jdbc:' when connection mode is manual")

    if not SECRET_ARN_RE.fullmatch(source_config["secret_arn"]):
        raise ValueError("source_config.secret_arn is not a valid Secrets Manager ARN")

    if not SUBNET_ID_RE.fullmatch(source_config["subnet_id"]):
        raise ValueError("source_config.subnet_id is not a valid subnet ID")

    sg_list = _normalise_sg_list(source_config["security_group_ids"], "source_config.security_group_ids")
    if not sg_list:
        raise ValueError("source_config.security_group_ids must be a non-empty list when connection mode is manual")
    for sg in sg_list:
        if not SG_ID_RE.fullmatch(sg):
            raise ValueError(f"source_config.security_group_ids contains invalid security group ID: {sg!r}")

    # Mutate the config so the physical-requirements helper sees a
    # uniform `list[str]` regardless of how the wizard encoded it.
    source_config["security_group_ids"] = sg_list


# The plaintext-credential keys a `target_config` may not carry. Spelled once here
# and read by both `create_pipeline` and `edit_pipeline` through
# `_reject_plaintext_target_credentials`, so the two endpoints cannot drift into
# two policies for one wire shape.
_PLAINTEXT_TARGET_CREDENTIAL_KEYS: tuple[str, ...] = ("jdbc_user", "jdbc_password")


def _reject_plaintext_target_credentials(target_config: Any) -> None:
    """Refuse a ``target_config`` that carries a plaintext JDBC credential.

    Standing invariant ``LGR-819980f-1``: JDBC credentials are only ever
    referenced by Secrets Manager ARN; plaintext user/password never appear in
    Glue job arguments or ConnectionProperties. Before the JDBC-target credential
    fix, ``target_config.jdbc_user`` / ``jdbc_password`` "always won" in
    ``create_glue_pipeline/job_arguments.py`` and went straight into
    ``--{prefix}jdbc_user`` / ``--{prefix}jdbc_password`` — visible in the Glue
    console, CloudTrail and job-run history, and logged at INFO. That builder now
    ignores both keys, so accepting them here would store a credential in the
    pipelines table (and return it from ``get_pipeline_details``) for no effect
    at all. Refusing at the boundary is the honest answer.

    Refused on PRESENCE, whatever the value, and for any target type. The
    wizard never sends either key, so nothing legitimate is turned away; and on
    the edit path the stored ``type`` is not in the request, so a type-keyed
    check would be bypassable by omitting ``type`` from a per-key merge. The
    message names the KEYS only, never a value — it is returned to the client
    and logged.

    Raises:
        ValueError: When either key is present. The handler turns it into a 400.
    """
    if not isinstance(target_config, dict):
        return
    present = [key for key in _PLAINTEXT_TARGET_CREDENTIAL_KEYS if key in target_config]
    if present:
        named = ", ".join(f"target_config.{key}" for key in present)
        raise ValueError(
            f"Plaintext credentials are not accepted ({named}). Reference a Secrets "
            "Manager secret instead: set target_config.secret_arn to the ARN of a secret "
            "holding {username, password}, or use a Glue connection whose "
            "ConnectionProperties carry SECRET_ID."
        )


def _reject_managed_glue_connection(config: Any, field_prefix: str) -> None:
    """Refuse a ``glue_connection_name`` that names a connection this system owns.

    "Use Glue Connection" mode means the pipeline references a connection the
    USER owns, and every lifecycle path treats it that way: the pipeline never
    creates, updates or deletes it (``_provision_jdbc_source_connection``,
    ``_provision_jdbc_target_connection``, ``delete_pipeline``). The
    connections this system creates for itself break that assumption, because
    each one is tied to its owner: another pipeline's ``-source`` connection is
    deleted with that pipeline, and a Detect Schema ``discover-`` connection is
    deleted by the discovery janitor after seven days. A pipeline that
    referenced one would fail when its owner went, with nothing to warn it. The
    wizard no longer lists them (``rds_resources.list_glue_jdbc_connections``);
    this refuses the same names from any caller.

    Keyed on the presence of ``glue_connection_name``, not on
    ``_connection_mode``: the ownership checks above key on presence too, so a
    request that omitted the mode would otherwise get the same hazard past this
    guard. Name only, no Glue call. Every connection the provisioner creates is
    named by a pattern ``is_managed_glue_connection`` matches; its Description
    arm exists for the listing, which already holds the Description.

    Raises:
        ValueError: When the name is one this system mints. The handler turns
            it into a 400.
    """
    if not isinstance(config, dict):
        return
    name = config.get("glue_connection_name")
    if isinstance(name, str) and name and is_managed_glue_connection(name):
        raise ValueError(
            f"{field_prefix}.glue_connection_name {name!r} is a connection this app "
            "created for another pipeline or for Detect Schema, and it is deleted "
            "with its owner. Pick a Glue connection you own, or use the select_rds "
            "or manual connection mode so this pipeline gets a connection of its own."
        )


def _validate_jdbc_target_config(target_config: dict[str, Any]) -> None:
    """Validate ``target_config`` for JDBC / RDS_DATABASE target payloads.

    P20 — dispatch on ``_connection_mode``: ``select_rds``,
    ``glue_connection``, or ``manual``. Pre-feature payloads without
    the field default to ``select_rds`` for backward compatibility
    (Req 12.1). Manual mode requires the user-supplied networking
    fields (``subnet_id``, ``security_group_ids``); ``select_rds``
    requires only the credential reference; ``glue_connection`` needs
    neither.

    Note: this validator runs ALONGSIDE the inline target-type field
    checks in :func:`create_pipeline` — the ``JDBC`` branch's ``jdbc_url``
    / ``jdbc_driver`` / ``jdbc_table`` presence checks, and the identical
    checks in the legacy ``RDS_DATABASE`` alias branch beside it. Those
    run for ALL THREE connection modes; this function adds the per-mode
    rules on top of them.

    Args:
        target_config: The persisted ``target_config`` map.

    Raises:
        ValueError: With ``target_config.<field>`` named in the message
            for any required field missing or any regex mismatch.
    """
    if not isinstance(target_config, dict):
        raise ValueError("target_config must be an object when target_type is JDBC")

    mode = target_config.get("_connection_mode", "select_rds")
    if mode == "manual":
        _validate_jdbc_target_config_manual(target_config)
    elif mode != "glue_connection":
        # `select_rds`, and legacy payloads carrying no `_connection_mode`.
        _validate_jdbc_target_config_select_rds(target_config)
    # `glue_connection` deliberately has no rules of its own: it bypasses
    # project-managed connection creation altogether, so the user's existing
    # connection supplies both the networking and the credentials (P20 Req
    # 7.6). Its FIELD validation is not skipped — the `JDBC` /
    # `RDS_DATABASE` branches in `create_pipeline` enforce `jdbc_url` /
    # `jdbc_driver` / `jdbc_table` for every mode, this one included
    # (P31 R9.2).


def _validate_jdbc_target_config_select_rds(target_config: dict[str, Any]) -> None:
    """Validate a ``select_rds`` target_config — the credential reference.

    ``select_rds`` needs no NETWORKING checks (subnet and security groups
    are derived from the described RDS instance rather than typed by the
    user, which is why P31 R9.3 recorded the absence as deliberate). It
    does need a credential reference, and that check was missing.

    Why this is required here and NOT in ``create_pipeline``'s
    mode-independent ``JDBC`` branch: this mode CREATES the Glue
    Connection. ``_provision_jdbc_target_connection`` calls
    :func:`jdbc_helpers.create_glue_jdbc_connection`, which sends
    ``SECRET_ID`` only when ``secret_arn`` is set — otherwise it sends
    ``JDBC_CONNECTION_URL`` + ``JDBC_ENFORCE_SSL`` alone, and Glue
    rejects a credential-less JDBC connection with the generic
    ``InvalidInputException: Validation for connection properties
    failed`` (``LGR-819980f-2``, verified against the live API). So a
    ``select_rds`` target without ``secret_arn`` is not merely
    under-specified, it is unprovisionable.

    ``glue_connection`` mode is exempt because it never reaches that
    call, and because ``_augment_glue_connection_config`` has already
    supplied ``secret_arn`` from the existing connection's ``SECRET_ID`` —
    or refused the request with a 400 when it has none. That refusal is
    new: the target side used to tolerate an inline-credential connection
    and leave ``secret_arn`` unset, which is the tolerance ``LGR-c246ab8-1``
    gave as the reason for this exemption. The tolerance is withdrawn in
    favour of standing invariant ``LGR-819980f-1``; the exemption survives
    on the first reason alone. It is keyed on ``glue_connection_name``
    rather than on the mode string because that field is what
    :func:`_provision_jdbc_target_connection` early-returns on, in ANY
    mode — so this check fires exactly when a connection will actually be
    created.
    """
    if target_config.get("glue_connection_name"):
        return

    secret_arn = target_config.get("secret_arn")
    if not secret_arn:
        raise ValueError(
            "target_config.secret_arn is required when target_type is JDBC and "
            "connection mode is select_rds: AgenticDataIngestion creates the Glue "
            "Connection for this target, and Glue rejects a JDBC connection that "
            "carries no credentials. Supply the Secrets Manager ARN holding the "
            "target database's {username, password}, or switch to glue_connection "
            "mode to reuse an existing connection."
        )

    if not SECRET_ARN_RE.fullmatch(secret_arn):
        raise ValueError("target_config.secret_arn is not a valid Secrets Manager ARN")


def _validate_jdbc_target_config_manual(target_config: dict[str, Any]) -> None:
    """Validate a ``manual`` target_config (P20 — new).

    Requires the same networking fields as the source-side manual-mode
    validator. ``availability_zone`` is optional — Glue derives it from
    the subnet when omitted (Req 6.3).

    On success, this function MUTATES ``target_config["security_group_ids"]``
    by replacing the JSON-encoded string (or the original list) with the
    parsed ``list[str]`` so downstream readers see a uniform shape.

    Validates: P20 Req 6.5, 7.5.
    """
    for field in _JDBC_MANUAL_REQUIRED_FIELDS:
        if not target_config.get(field):
            raise ValueError(
                f"target_config.{field} is required when target_type is JDBC and connection mode is manual"
            )

    if not target_config["jdbc_url"].startswith("jdbc:"):
        raise ValueError("target_config.jdbc_url must start with 'jdbc:' when connection mode is manual")

    if not SECRET_ARN_RE.fullmatch(target_config["secret_arn"]):
        raise ValueError("target_config.secret_arn is not a valid Secrets Manager ARN")

    if not SUBNET_ID_RE.fullmatch(target_config["subnet_id"]):
        raise ValueError("target_config.subnet_id is not a valid subnet ID")

    sg_list = _normalise_sg_list(target_config["security_group_ids"], "target_config.security_group_ids")
    if not sg_list:
        raise ValueError("target_config.security_group_ids must be a non-empty list when connection mode is manual")
    for sg in sg_list:
        if not SG_ID_RE.fullmatch(sg):
            raise ValueError(f"target_config.security_group_ids contains invalid security group ID: {sg!r}")

    target_config["security_group_ids"] = sg_list

    engine = target_config["engine"]
    if engine not in SUPPORTED_ENGINES:
        raise ValueError(f"target_config.engine must be one of {sorted(SUPPORTED_ENGINES)}; got {engine!r}")


def _build_jdbc_source_physical_requirements(
    source_config: dict[str, Any],
) -> dict[str, Any]:
    """Return the ``PhysicalConnectionRequirements`` dict for the source.

    Mirrors the canonical shape consumed by
    :func:`jdbc_helpers.create_glue_jdbc_connection`. The networking
    fields (``subnet_id``, ``security_group_ids``, optional
    ``availability_zone``) come from different sources depending on the
    connection mode:

    * ``select_rds`` — the wizard populates them from the chosen RDS
      instance's ``DBSubnetGroup`` / ``VpcSecurityGroups``.
    * ``manual`` — the wizard collects them from the user via the
      `<ManualConnectionForm>` VPC / Subnet / Security Group dropdowns.
    * ``glue_connection`` — this helper is NOT called; the user-supplied
      Glue Connection's `PhysicalConnectionRequirements` is reused as-is.

    P20 — dispatch on ``_connection_mode``. ``select_rds`` and ``manual``
    share the same shape (the validator has normalised
    `security_group_ids` to a list in both cases); the only observable
    difference is the error-message phrasing per mode (Req 7.1-7.4).

    Args:
        source_config: The persisted ``source_config`` map from the request.

    Returns:
        Dict suitable for the ``physical_requirements`` argument of
        :func:`jdbc_helpers.create_glue_jdbc_connection`.

    Raises:
        ValueError: If the helper is invoked for ``glue_connection`` mode
            (caller bug — that mode reuses the user's connection without
            building physical requirements), or if a required networking
            field is missing.
    """
    mode = source_config.get("_connection_mode", "select_rds")

    if mode == "glue_connection":
        # Defensive raise — the caller path
        # (`_provision_jdbc_source_connection`) skips this helper for
        # `glue_connection` mode today; if it ever calls us we want a
        # clear failure rather than building a bogus
        # PhysicalConnectionRequirements that overrides the user's
        # connection.
        raise ValueError("_build_jdbc_source_physical_requirements should not be called for glue_connection mode")

    subnet_id = source_config.get("subnet_id")
    security_group_ids_raw = source_config.get("security_group_ids")

    if not subnet_id:
        raise ValueError(f"source_config.subnet_id is required when source_type is JDBC and connection mode is {mode}")
    if not security_group_ids_raw:
        raise ValueError(
            f"source_config.security_group_ids must be a non-empty list when "
            f"source_type is JDBC and connection mode is {mode}"
        )
    # Accept either a real list (P17 select_rds backend-direct path) or
    # a JSON-encoded list (the wizard's `Record<string, string>` config
    # shape; both `select_rds` and `manual` modes now send this).
    security_group_ids = _normalise_sg_list(security_group_ids_raw, "source_config.security_group_ids")
    if not security_group_ids:
        raise ValueError(
            f"source_config.security_group_ids must be a non-empty list when "
            f"source_type is JDBC and connection mode is {mode}"
        )

    physical: dict[str, Any] = {
        "SubnetId": subnet_id,
        "SecurityGroupIdList": security_group_ids,
    }
    az = source_config.get("availability_zone")
    if az:
        physical["AvailabilityZone"] = az
    return physical


def _provision_jdbc_source_connection(pipeline_id: str, source_config: dict[str, Any]) -> str:
    """Provision the source-side Glue JDBC Connection for a JDBC pipeline.

    Implements P17 Requirement 11.1 / 11.2: when the wizard's connection
    mode is ``select_rds`` (signalled by the absence of
    ``source_config.glue_connection_name``), idempotently create a Glue
    Connection named ``agentic-data-ingestion-{pipeline_id}-source`` carrying the
    JDBC URL, driver, secret, and ``PhysicalConnectionRequirements``
    (subnet + security groups + AZ) derived from the selected RDS
    instance. When the mode is ``glue_connection``, skip creation
    entirely and reference the user's existing connection by name —
    AgenticDataIngestion does not own the lifecycle of user-supplied connections.

    P20 ``manual`` mode takes the SAME create-or-update branch as
    ``select_rds``: the branch below keys off the presence of
    ``glue_connection_name``, which manual mode never sets (the wizard's
    `handleModeChange` deletes it on every mode transition). The only
    difference is the provenance of the networking fields — user-entered
    rather than RDS-derived — which
    :func:`_build_jdbc_source_physical_requirements` already handles per
    mode. Three modes in, two provisioning paths.

    The connection name is returned in all modes so callers can persist
    a single ``source_connection_name`` value on the pipeline DDB record
    regardless of which mode was used.

    Args:
        pipeline_id: Pipeline identifier used to derive the per-pipeline
            connection name in ``select_rds`` and ``manual`` modes.
        source_config: The validated JDBC ``source_config`` payload.

    Returns:
        The resolved connection name — ``agentic-data-ingestion-{pipeline_id}-source``
        in ``select_rds`` / ``manual`` mode, or
        ``source_config["glue_connection_name"]`` in ``glue_connection`` mode.

    Raises:
        ValueError: If ``select_rds`` or ``manual`` mode is missing
            required networking fields. Caller surfaces this as HTTP 400.
        botocore.exceptions.ClientError: If Glue rejects the create /
            update call (e.g. malformed subnet, IAM denial). Caller is
            responsible for rolling back the partially-persisted pipeline
            record (Task 5.3 wires this up explicitly).
    """
    # glue_connection mode — reuse the user's existing connection as-is.
    # We do NOT call create_glue_jdbc_connection because the connection
    # was created by the customer outside AgenticDataIngestion's lifecycle and any
    # update would risk overwriting their PhysicalConnectionRequirements
    # or properties.
    existing_connection = source_config.get("glue_connection_name")
    if existing_connection:
        logger.info(
            "JDBC source uses existing Glue connection (no creation): %s",
            existing_connection,
        )
        return existing_connection

    # select_rds AND manual modes — create-or-update the per-pipeline
    # source connection. Re-validate networking fields here as defence-in-depth
    # so a malformed source_config produces a clean ValueError that the
    # caller can map to HTTP 400 alongside the existing JDBC validation
    # errors. The wizard normally enforces these, but the API surface
    # also accepts external clients.
    physical_requirements = _build_jdbc_source_physical_requirements(source_config)

    connection_name = f"agentic-data-ingestion-{pipeline_id}-source"

    create_glue_jdbc_connection(
        connection_name=connection_name,
        jdbc_url=source_config["jdbc_url"],
        jdbc_driver=source_config["jdbc_driver"],
        secret_arn=source_config.get("secret_arn"),
        physical_requirements=physical_requirements,
        tags={
            "Type": "agentic-data-ingestion-source",
            "PipelineId": pipeline_id,
        },
    )
    logger.info(
        "Provisioned source Glue JDBC connection %s for pipeline %s",
        connection_name,
        pipeline_id,
    )
    return connection_name


def _provision_jdbc_runtime_crawler(
    pipeline_id: str,
    source_config: dict[str, Any],
    source_connection_name: str,
) -> str:
    """Provision the per-pipeline runtime Glue Crawler for a JDBC pipeline.

    Implements P17 Requirement 11.8: every JDBC pipeline owns a dedicated
    runtime crawler ``agentic-data-ingestion-{pipeline_id}-source-discover`` that the
    ``schema_extract`` Lambda triggers each run to refresh the source
    schema in the shared discovery database. The crawler is created at
    pipeline-creation time so the customer's connection / network setup
    is validated up-front, not on the first execution.

    Idempotent: ``CreateCrawler`` raises ``AlreadyExistsException`` when
    the crawler already exists; we fall through to ``UpdateCrawler`` so
    re-runs of ``create_pipeline`` (or future re-provisioning flows)
    converge on the same configuration. Tags are accepted only on
    ``CreateCrawler`` per the boto3 contract — on the update path we
    silently skip them; the daily janitor (Task 4.3) only filters by
    ``Type=agentic-data-ingestion-discovery`` and explicitly skips ``agentic-data-ingestion-runtime``
    crawlers, so missing tags on an existing runtime crawler do NOT cause
    accidental cleanup.

    The crawler is tagged with ``Type=agentic-data-ingestion-runtime`` (deliberately
    different from the wizard-time discovery tag ``agentic-data-ingestion-discovery``)
    so the daily TTL janitor in ``handlers.jdbc_resources`` does NOT
    touch per-pipeline crawlers — their lifecycle is bound to the
    pipeline and managed by ``delete_pipeline`` (Task 5.4).

    Args:
        pipeline_id: Pipeline identifier — used both in the crawler name
            and the ``TablePrefix`` so the schema_extract Lambda can find
            the crawler's output table via
            ``glue:GetTables(Expression="agentic_data_ingestion_{pipeline_id}_*")``.
        source_config: The validated JDBC ``source_config`` payload —
            ``jdbc_schema`` / ``jdbc_table`` are read here.
        source_connection_name: Name of the Glue JDBC Connection the
            crawler should use. This is the value pre-computed onto
            ``pipeline_item["source_connection_name"]`` by Task 5.1 —
            ``agentic-data-ingestion-{pipeline_id}-source`` in ``select_rds`` mode or
            ``source_config.glue_connection_name`` in ``glue_connection``
            mode.

    Returns:
        The crawler name, ``agentic-data-ingestion-{pipeline_id}-source-discover``.

    Raises:
        RuntimeError: If ``GLUE_PIPELINE_ROLE_ARN`` is not configured.
            Surfaced as a clean configuration error rather than a
            confusing Glue ``InvalidInputException("Role cannot be empty")``.
        botocore.exceptions.ClientError: If Glue rejects the create /
            update call (e.g. malformed connection name, IAM denial).
            Caller is responsible for rollback semantics — Task 5.3
            wires that up explicitly.
    """
    # Re-use the same configuration knobs the wizard-time discovery flow
    # reads (``handlers.jdbc_resources``) so there is a single source of
    # truth for the discovery database name and the Glue role ARN.
    # Importing inside the function keeps the module-load cost off the
    # cold-start path for the (much more common) non-JDBC requests.
    from .jdbc_resources import DISCOVERY_DB, GLUE_PIPELINE_ROLE_ARN

    if not GLUE_PIPELINE_ROLE_ARN:
        raise RuntimeError(
            "GLUE_PIPELINE_ROLE_ARN environment variable is not set; "
            "the dashboard API Lambda must be deployed with this env var "
            "(see Task 14.2 / compute-stack.ts)."
        )

    crawler_name = f"agentic-data-ingestion-{pipeline_id}-source-discover"
    table_prefix = f"agentic_data_ingestion_{pipeline_id}_"
    schema = source_config["jdbc_schema"]
    table = source_config["jdbc_table"]

    crawler_input: dict[str, Any] = {
        "Name": crawler_name,
        "Role": GLUE_PIPELINE_ROLE_ARN,
        "DatabaseName": DISCOVERY_DB,
        "Targets": {
            "JdbcTargets": [
                {
                    "ConnectionName": source_connection_name,
                    "Path": f"{schema}/{table}",
                }
            ]
        },
        "TablePrefix": table_prefix,
    }

    # Tag value MUST be ``agentic-data-ingestion-runtime`` (NOT ``agentic-data-ingestion-discovery``)
    # so the daily janitor in handlers.jdbc_resources skips this crawler
    # — runtime crawlers live for the pipeline's lifetime, not 7 days.
    tags = {
        "Type": "agentic-data-ingestion-runtime",
        "PipelineId": pipeline_id,
    }

    glue_client = boto3.client("glue")
    try:
        glue_client.create_crawler(**crawler_input, Tags=tags)
        logger.info(
            "Created Glue runtime crawler %s for pipeline %s",
            crawler_name,
            pipeline_id,
        )
    except glue_client.exceptions.AlreadyExistsException:
        # Tags is intentionally NOT passed on update — Glue rejects it
        # on UpdateCrawler. Existing tags are preserved by Glue.
        glue_client.update_crawler(**crawler_input)
        logger.info(
            "Updated existing Glue runtime crawler %s for pipeline %s",
            crawler_name,
            pipeline_id,
        )

    return crawler_name


def _build_jdbc_target_physical_requirements(
    target_config: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the ``PhysicalConnectionRequirements`` dict for a JDBC target.

    Mirrors :func:`_build_jdbc_source_physical_requirements`. The wizard
    populates ``target_config`` with ``subnet_id`` and
    ``security_group_ids`` from one of three sources, depending on
    connection mode:

    * ``select_rds`` — derived from the chosen RDS instance's
      ``DBSubnetGroup`` / ``VpcSecurityGroups`` (closes the P14 gap;
      Requirement 11.4 / 14.5 / 15.3).
    * ``manual`` — entered directly by the user via the
      `<ManualConnectionForm>` VPC / Subnet / Security Group dropdowns
      (P20).
    * ``glue_connection`` — this helper is NOT called; the user-
      supplied Glue Connection's `PhysicalConnectionRequirements` is
      reused as-is.

    Backward-compat: older API clients that predate Task 5.5 may not
    send ``subnet_id`` / ``security_group_ids``. In that case we return
    ``None`` and let :func:`create_glue_jdbc_connection` create the
    connection without a VPC attachment — this keeps the Lambda
    accepting historic P14 payloads while still letting newer wizard
    payloads exercise the full VPC routing path.

    Args:
        target_config: The persisted ``target_config`` map for an
            ``RDS_DATABASE`` / ``JDBC`` target.

    Returns:
        Dict with ``SubnetId``, ``SecurityGroupIdList`` and optional
        ``AvailabilityZone`` when both required fields are present;
        ``None`` otherwise.
    """
    mode = target_config.get("_connection_mode", "select_rds")

    if mode == "glue_connection":
        # Defensive: the caller path skips this helper for
        # `glue_connection` mode. If it ever calls us, return None so
        # `create_glue_jdbc_connection` doesn't override the user's
        # connection's networking metadata.
        return None

    subnet_id = target_config.get("subnet_id")
    security_group_ids_raw = target_config.get("security_group_ids")

    # Backward-compat: legacy P14 payloads may omit subnet_id / sg list.
    # Return None so the connection is created without VPC attachment.
    if not subnet_id or not security_group_ids_raw:
        return None

    # Accept either a real list (legacy direct-API path) or a JSON-
    # encoded list (the wizard's `Record<string, string>` config
    # shape). Manual mode (P20) also routes through here.
    try:
        security_group_ids = _normalise_sg_list(security_group_ids_raw, "target_config.security_group_ids")
    except ValueError:
        # Backward-compat: a malformed legacy payload returns None
        # rather than 500 — the connection is created without VPC
        # attachment and the runtime job surfaces a clearer error.
        return None
    if not security_group_ids:
        return None

    physical: dict[str, Any] = {
        "SubnetId": subnet_id,
        "SecurityGroupIdList": security_group_ids,
    }
    az = target_config.get("availability_zone")
    if az:
        physical["AvailabilityZone"] = az
    return physical


def _provision_jdbc_target_connection(pipeline_id: str, target_config: dict[str, Any]) -> str | None:
    """Provision the target-side Glue JDBC Connection for an RDS_DATABASE target.

    Replaces the P14 ``_auto_create_rds_glue_connection`` helper (Task 5.5
    in the P17 plan; Requirement 11.4 / 14.5). Delegates to the
    canonical :func:`jdbc_helpers.create_glue_jdbc_connection` so the
    target side gets the same VPC routing fix that the source side
    received in Task 5.1.

    Behaviour:
        * If ``target_config["glue_connection_name"]`` is set, the user
          supplied an existing Glue Connection — return its name as-is
          and skip creation. AgenticDataIngestion does not own the lifecycle of
          user-supplied connections.
        * If ``target_config["jdbc_url"]`` is missing, log a warning and
          return ``None`` — the legacy P14 code path used this signal
          to skip creation for partial RDS configs, and we preserve that
          behaviour so older API clients don't suddenly hard-fail.
        * Otherwise, idempotently create-or-update the per-pipeline
          target connection ``agentic-data-ingestion-{pipeline_id}-rds`` carrying
          the JDBC URL, driver, secret, and (when supplied)
          ``PhysicalConnectionRequirements`` derived from the selected
          RDS instance's networking metadata.

    Args:
        pipeline_id: Pipeline identifier — used to derive the
            connection name ``agentic-data-ingestion-{pipeline_id}-rds``.
        target_config: The validated ``RDS_DATABASE`` target config.

    Returns:
        The resolved connection name, or ``None`` when ``jdbc_url`` is
        absent (matches legacy P14 semantics).

    Raises:
        botocore.exceptions.ClientError: If Glue rejects the create /
            update call (e.g. malformed subnet, IAM denial). The caller
            in ``create_pipeline`` fails the create and rolls back what
            the request made (``_undo_pipeline_create``). It used to log
            and continue, on the premise that the Glue job would surface a
            clear connection error; it does not — the first run failed with
            "A Glue connection is required for a JDBC target" after a 201.
    """
    # User-supplied existing Glue Connection — skip creation entirely.
    existing_connection = target_config.get("glue_connection_name")
    if existing_connection:
        return existing_connection

    jdbc_url = target_config.get("jdbc_url", "")
    jdbc_driver = target_config.get("jdbc_driver", "")
    secret_arn = target_config.get("secret_arn", "") or None

    if not jdbc_url:
        logger.warning(
            "Cannot auto-create Glue Connection for %s: missing jdbc_url",
            pipeline_id,
        )
        return None

    connection_name = f"agentic-data-ingestion-{pipeline_id}-rds"
    physical_requirements = _build_jdbc_target_physical_requirements(target_config)

    create_glue_jdbc_connection(
        connection_name=connection_name,
        jdbc_url=jdbc_url,
        jdbc_driver=jdbc_driver,
        secret_arn=secret_arn,
        physical_requirements=physical_requirements,
        tags={
            "Type": "agentic-data-ingestion-target",
            "PipelineId": pipeline_id,
        },
    )
    logger.info(
        "Provisioned target Glue JDBC connection %s for pipeline %s",
        connection_name,
        pipeline_id,
    )
    return connection_name
