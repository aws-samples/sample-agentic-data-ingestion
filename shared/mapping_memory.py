# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Memory client for schema mapping decisions.

Stores and retrieves historical field mapping patterns using AgentCore Memory's
MemorySessionManager for proper semantic indexing via conversation turns.

Environment variables:
    MAPPING_MEMORY_ID: AgentCore Memory resource ID
    MEMORY_ENABLED: Feature flag (default: "true"). Set to "false" to disable.
"""

import logging
import os
from datetime import UTC, datetime

import boto3

logger = logging.getLogger(__name__)


class MappingMemoryClient:
    """Client for storing and querying schema mapping decisions in AgentCore Memory.

    Uses the MemorySessionManager pattern:
    - Writes mapping decisions as conversation turns (triggers semantic extraction)
    - Searches via search_long_term_memories, falling back to the low-level
      boto3 API if the SDK session manager is unavailable
    - Lists a pipeline's entries via the low-level list_memory_records only
    """

    NAMESPACE = "schema-mappings"

    # R15.4 batching: get_similar_mappings issues ONE retrieval per mapping run.
    # The single call over-fetches (top_k sized to the field count) and results
    # are filtered client-side by field name.
    RETRIEVAL_PER_FIELD_FACTOR = 3  # candidate records requested per source field
    RETRIEVAL_TOP_K_MAX = 100  # AgentCore Memory retrieval API maximum topK

    def __init__(self, memory_id: str | None = None, region: str | None = None):
        self.memory_id = memory_id or os.environ.get("MAPPING_MEMORY_ID", "")
        self.enabled = os.environ.get("MEMORY_ENABLED", "true").lower() != "false"
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.client = None
        self._session_manager = None

        if self.enabled and self.memory_id:
            self.client = boto3.client("bedrock-agentcore", region_name=self.region)
            # Try to use the high-level SDK session manager
            try:
                from bedrock_agentcore.memory.session import MemorySessionManager

                self._session_manager = MemorySessionManager(
                    memory_id=self.memory_id,
                    region_name=self.region,
                )
                logger.info("Using MemorySessionManager for memory operations")
            except ImportError:
                logger.info("bedrock_agentcore SDK not available, using low-level boto3 API")
            except Exception as e:
                logger.warning("Failed to initialize MemorySessionManager: %s", e)
        else:
            if self.enabled and not self.memory_id:
                logger.warning("MAPPING_MEMORY_ID not set — Memory features disabled")
                self.enabled = False

    def _get_session(self, pipeline_id: str):
        """Get or create a memory session for a pipeline."""
        if not self._session_manager:
            return None
        try:
            session = self._session_manager.create_memory_session(
                actor_id=f"pipeline:{pipeline_id}" if pipeline_id else "pipeline:unknown",
                session_id=f"mappings-{pipeline_id or 'global'}",
            )
            return session
        except Exception as e:
            logger.warning("Failed to create memory session: %s", e)
            return None

    def store_mapping_decision(
        self,
        source_field: str,
        target_field: str,
        confidence: float,
        pipeline_id: str = "",
        approved: bool = False,
    ) -> bool:
        """Store a mapping decision as a conversation turn in Memory.

        Uses MemorySessionManager.add_turns() so the semantic strategy
        automatically extracts and indexes the mapping for search.

        Args:
            source_field: Source schema field name.
            target_field: Target schema field name.
            confidence: Mapping confidence score (0-100).
            pipeline_id: Pipeline that produced this mapping.
            approved: Whether the mapping has been approved by a human.

        Returns:
            True if stored successfully, False otherwise.
        """
        if not self.enabled:
            return False

        status = "approved" if approved else "pending"
        # Normalize confidence: if <= 1, treat as decimal (0.95 = 95%, 1.0 = 100%), otherwise as percentage
        conf_pct = confidence * 100 if confidence <= 1 else confidence
        semantic_text = (
            f"Field '{source_field}' maps to '{target_field}' "
            f"with {conf_pct:.0f}% confidence. "
            f"Status: {status}. Pipeline: {pipeline_id or 'unknown'}."
        )

        # Write directly to memory records store via batch_create_memory_records.
        # This is synchronous and immediately queryable via list_memory_records.
        # (The session.add_turns() path writes to the event store which requires
        # async extraction and is not reliably queryable via list_memory_records.)
        if not self.client:
            return False

        try:
            import hashlib

            timestamp = datetime.now(UTC).isoformat()

            # Use a deterministic ID based on pipeline + source + target to prevent duplicates
            dedup_key = f"{pipeline_id or 'unknown'}:{source_field}:{target_field}"
            record_id = hashlib.md5(dedup_key.encode(), usedforsecurity=False).hexdigest()

            namespace = f"{self.NAMESPACE}/{pipeline_id}" if pipeline_id else self.NAMESPACE

            # Check if this exact mapping already exists — update confidence/status if so
            try:
                existing = self.client.list_memory_records(
                    memoryId=self.memory_id,
                    namespace=namespace,
                    maxResults=100,
                )
                for rec in existing.get("memoryRecordSummaries", []):
                    existing_text = rec.get("content", {}).get("text", "")
                    if f"'{source_field}' maps to '{target_field}'" in existing_text:
                        # Already exists — update status/confidence if changed
                        import re

                        new_text = re.sub(r"Status:\s*\w+", f"Status: {status}", existing_text)
                        new_text = re.sub(r"with \d+% confidence", f"with {conf_pct:.0f}% confidence", new_text)
                        if new_text != existing_text:
                            rid = rec.get("memoryRecordId", "")
                            ts = rec.get("createdAt", "")
                            self.client.batch_update_memory_records(
                                memoryId=self.memory_id,
                                records=[
                                    {
                                        "memoryRecordId": rid,
                                        "content": {"text": new_text},
                                        "namespaces": [namespace],
                                        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                                    }
                                ],
                            )
                            logger.info("Updated existing mapping decision: %s -> %s", source_field, target_field)
                        else:
                            logger.info(
                                "Mapping decision already exists (unchanged): %s -> %s", source_field, target_field
                            )
                        return True
            except Exception as e:
                logger.debug("Could not check for existing mapping (will create new): %s", e)

            self.client.batch_create_memory_records(
                memoryId=self.memory_id,
                records=[
                    {
                        "requestIdentifier": record_id,
                        "content": {"text": semantic_text},
                        "namespaces": [namespace],
                        "timestamp": timestamp,
                    }
                ],
            )

            logger.info(
                "Stored mapping decision via batch_create API: %s -> %s (confidence=%.0f%%)",
                source_field,
                target_field,
                confidence,
            )
            return True

        except Exception as e:
            logger.warning("Failed to store mapping decision in Memory: %s", e)
            return False

    def get_similar_mappings(self, source_fields: list[str], limit: int = 10) -> list[dict]:
        """Query semantic memory for similar past mappings with ONE batched retrieval.

        Issues a single namespace-level semantic retrieval per call — O(1)
        billable calls regardless of field count (R15.4) — then filters
        results client-side by field name. Previously this looped one
        semantic query per field (O(fields) billable calls).

        The single query names every requested field. ``top_k`` is sized to
        the field count: ``max(limit, len(source_fields) * RETRIEVAL_PER_FIELD_FACTOR)``,
        capped at ``RETRIEVAL_TOP_K_MAX`` (the API maximum), so wide schemas
        still retrieve enough candidate records per field after filtering.

        Uses search_long_term_memories via session manager, with fallback
        to low-level retrieve_memory_records — each path makes exactly one
        retrieval call.

        Args:
            source_fields: List of source field names to search for.
            limit: Floor for the batched retrieval size (kept for signature
                compatibility with existing callers).

        Returns:
            Deduplicated list of ``{text, score, record_id}`` records that
            mention at least one requested field, sorted by score descending.
        """
        if not self.enabled or not source_fields:
            return []

        query = "field mapping for fields: " + ", ".join(f"'{field}'" for field in source_fields)
        top_k = min(
            max(limit, len(source_fields) * self.RETRIEVAL_PER_FIELD_FACTOR),
            self.RETRIEVAL_TOP_K_MAX,
        )

        # Try high-level SDK first — a single batched search per run
        if self._session_manager:
            try:
                session = self._get_session("")
                if session:
                    records = session.search_long_term_memories(
                        query=query,
                        namespace_prefix="/",
                        top_k=top_k,
                    )
                    results = self._collect_field_matches(records, source_fields)
                    if results:
                        return results
            except Exception as e:
                logger.warning("Batched session search failed, falling back: %s", e)

        # Fallback to low-level API — also a single batched retrieval
        if not self.client:
            return []

        try:
            response = self.client.retrieve_memory_records(
                memoryId=self.memory_id,
                namespace=self.NAMESPACE,
                searchCriteria={"searchQuery": query, "topK": top_k},
            )
            return self._collect_field_matches(response.get("memoryRecords", []), source_fields)
        except Exception as e:
            logger.warning("Batched memory retrieval failed for %d fields: %s", len(source_fields), e)
            return []

    def _collect_field_matches(self, records: list, source_fields: list[str]) -> list[dict]:
        """Filter batched retrieval results client-side by field name, dedupe, and sort.

        Part of the R15.4 batching contract: the single namespace retrieval
        over-fetches, and this narrows results to records mentioning at least
        one requested source field.
        """
        seen_texts: set[str] = set()
        results: list[dict] = []
        for record in records:
            # The SDK's search_long_term_memories returns MemoryRecord objects,
            # which are dict-like (DictWrapper) but not dict subclasses, while the
            # low-level API returns plain dicts. Test for dict-style access, not
            # for dict: str() of a MemoryRecord is the repr of the whole record,
            # which parses with 'memoryRecordId' as the source field.
            if hasattr(record, "get"):
                text = record.get("content", {}).get("text", "")
                score = record.get("score", 0.0)
                record_id = record.get("memoryRecordId", "")
            else:
                text = str(record)
                score = 0.5
                record_id = ""
            if not text or text in seen_texts:
                continue
            if not self._matches_any_field(text, source_fields):
                continue
            seen_texts.add(text)
            results.append({"text": text, "score": score, "record_id": record_id})
        results.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        return results

    @staticmethod
    def _matches_any_field(text: str, source_fields: list[str]) -> bool:
        """True if the record text mentions any requested field name.

        Stored records quote field names ("Field 'x' maps to 'y' ..."), so
        matching on the quoted form gives exact field-name matching
        (case-insensitive) without substring false positives — requested
        field 'id' does not match a record for 'cust_id'.
        """
        lowered = text.lower()
        return any(f"'{field.lower()}'" in lowered for field in source_fields)

    def get_entries(self, pipeline_id: str, limit: int = 100) -> list[dict]:
        """Retrieve all mapping memory entries for a given pipeline.

        Always reads through the low-level ``list_memory_records`` API, scoped
        to the ``schema-mappings/<pipeline_id>`` namespace that
        ``store_mapping_decision`` writes to. Deliberately has no
        session-manager path: the SDK's ``list_long_term_memory_records``
        returns ``MemoryRecord`` wrappers rather than dicts, and an unscoped
        call to it once showed every row's source field as the literal
        ``memoryRecordId`` with no entry id or timestamp.

        Args:
            pipeline_id: Pipeline to retrieve entries for.
            limit: Maximum number of entries to return.

        Returns:
            List of parsed mapping entry dicts sorted by confidence descending.
        """
        if not self.enabled or not self.client:
            return []

        entries = []

        namespace = f"{self.NAMESPACE}/{pipeline_id}" if pipeline_id else self.NAMESPACE
        try:
            response = self.client.list_memory_records(
                memoryId=self.memory_id,
                namespace=namespace,
                maxResults=limit,
            )
            # API returns memoryRecordSummaries (not memoryRecords)
            for record in response.get("memoryRecordSummaries", response.get("memoryRecords", [])):
                text = record.get("content", {}).get("text", "")
                parsed = self._parse_mapping_text(text)
                if parsed:
                    parsed["entry_id"] = record.get("memoryRecordId", "")
                    ts = record.get("createdAt", record.get("timestamp", ""))
                    parsed["timestamp"] = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
                    entries.append(parsed)

            entries.sort(key=lambda e: e.get("confidence", 0), reverse=True)
            return entries[:limit]

        except Exception as e:
            logger.warning("Failed to retrieve mapping entries for pipeline '%s': %s", pipeline_id, e)
            return []

    def delete_entry(self, entry_id: str) -> bool:
        """Delete a specific mapping memory entry by record ID.

        Args:
            entry_id: The memory record ID to delete.

        Returns:
            True if deleted successfully, False otherwise.
        """
        if not self.enabled or not self.client:
            return False

        try:
            self.client.batch_delete_memory_records(
                memoryId=self.memory_id,
                records=[{"memoryRecordId": entry_id}],
            )
            logger.info("Deleted mapping memory entry: %s", entry_id)
            return True

        except Exception as e:
            logger.warning("Failed to delete mapping memory entry '%s': %s", entry_id, e)
            return False

    def update_entries_status(self, pipeline_id: str, approved: bool) -> int:
        """Update all memory entries for a pipeline to approved/pending status.

        Reads existing entries, rewrites their text with the new status,
        and calls batch_update_memory_records to update in place.

        Returns:
            Number of entries updated.
        """
        if not self.enabled or not self.client:
            return 0

        namespace = f"{self.NAMESPACE}/{pipeline_id}" if pipeline_id else self.NAMESPACE
        status_text = "approved" if approved else "pending"

        try:
            response = self.client.list_memory_records(
                memoryId=self.memory_id,
                namespace=namespace,
                maxResults=100,
            )
            records = response.get("memoryRecordSummaries", [])
            if not records:
                return 0

            updates = []
            for record in records:
                text = record.get("content", {}).get("text", "")
                rid = record.get("memoryRecordId", "")
                if not text or not rid:
                    continue
                # Replace Status: pending/approved with the new status
                import re

                new_text = re.sub(r"Status:\s*\w+", f"Status: {status_text}", text)
                if new_text != text:
                    ts = record.get("createdAt", "")
                    updates.append(
                        {
                            "memoryRecordId": rid,
                            "content": {"text": new_text},
                            "namespaces": [namespace],
                            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                        }
                    )

            if not updates:
                return 0

            resp = self.client.batch_update_memory_records(
                memoryId=self.memory_id,
                records=updates,
            )
            succeeded = len(resp.get("successfulRecords", []))
            failed = len(resp.get("failedRecords", []))
            logger.info("Updated %d memory entries to '%s' (%d failed)", succeeded, status_text, failed)
            return succeeded

        except Exception as e:
            logger.warning("Failed to update memory entries status for '%s': %s", pipeline_id, e)
            return 0

    def store_approval_feedback(
        self,
        mapping_id: str,
        approved: bool,
        reviewer_notes: str = "",
    ) -> bool:
        """Store approval or rejection feedback as a conversation turn.

        Args:
            mapping_id: Identifier for the mapping (typically the approval_id).
            approved: True if approved, False if rejected.
            reviewer_notes: Optional reviewer comments or rejection reason.

        Returns:
            True if stored successfully, False otherwise.
        """
        if not self.enabled:
            return False

        event_type = "mapping_approved" if approved else "mapping_rejected"
        text = f"Mapping {mapping_id} was {event_type}."
        if reviewer_notes:
            text += f" Reviewer notes: {reviewer_notes}"

        # Try session-based approach
        session = self._get_session(mapping_id)
        if session:
            try:
                from bedrock_agentcore.memory.constants import ConversationalMessage, MessageRole

                session.add_turns(
                    messages=[ConversationalMessage(text, MessageRole.USER)],
                )
                logger.info("Stored approval feedback via session: mapping_id=%s", mapping_id)
                return True
            except Exception as e:
                logger.warning("Session add_turns failed for approval: %s", e)

        # Fallback to event API
        if not self.client:
            return False

        try:
            timestamp = datetime.now(UTC).isoformat()
            self.client.create_event(
                memoryId=self.memory_id,
                actorId=f"reviewer:{mapping_id}",
                eventTimestamp=timestamp,
                payload=[
                    {
                        "conversational": {
                            "content": {"text": text},
                            "role": "user",
                        }
                    }
                ],
            )
            logger.info("Stored approval feedback via event API: mapping_id=%s", mapping_id)
            return True

        except Exception as e:
            logger.warning("Failed to store approval feedback in Memory: %s", e)
            return False

    def format_historical_context(self, similar_mappings: list[dict]) -> str:
        """Format Memory results into the Historical Mapping Patterns prompt section."""
        if not similar_mappings:
            return ""

        rows = []
        for mapping in similar_mappings:
            parsed = self._parse_mapping_text(mapping.get("text", ""))
            if parsed:
                rows.append(parsed)

        if not rows:
            return ""

        lines = [
            "## Historical Mapping Patterns",
            "",
            "The following mappings have been approved in previous pipelines. Use them as strong signals:",
            "",
            "| Source Field | Target Field | Confidence | Approvals | Rejections |",
            "|-------------|-------------|-----------|-----------|------------|",
        ]

        for row in rows:
            approvals = 1 if row["approved"] else 0
            rejections = 0 if row["approved"] else 1
            lines.append(
                f"| {row['source_field']} | {row['target_field']} "
                f"| {row['confidence']:.0f}% | {approvals} | {rejections} |"
            )

        lines.extend(
            [
                "",
                "Rules:",
                "- If a source field exactly matches a historical pattern with "
                "0 rejections, use that mapping with 99% confidence.",
                "- If a source field is similar (but not exact) to a historical "
                "pattern, use it as a hint but verify with schema context.",
                "- If a historical mapping was rejected, avoid that mapping unless the target schema requires it.",
            ]
        )

        return "\n".join(lines)

    @staticmethod
    def _parse_mapping_text(text: str) -> dict | None:
        """Parse a semantic memory text record into structured fields.

        Expected format:
            "Field 'source' maps to 'target' with 95% confidence. Status: approved. Pipeline: xyz."

        Returns:
            Dict with source_field, target_field, confidence, approved — or None if unparseable.
        """
        if not text:
            return None

        try:
            source_start = text.index("'") + 1
            source_end = text.index("'", source_start)
            source_field = text[source_start:source_end]

            maps_to = "maps to '"
            target_start = text.index(maps_to) + len(maps_to)
            target_end = text.index("'", target_start)
            target_field = text[target_start:target_end]

            conf_marker = "with "
            conf_start = text.index(conf_marker, target_end) + len(conf_marker)
            conf_end = text.index("%", conf_start)
            confidence = float(text[conf_start:conf_end])

            approved = "approved" in text.lower().split("status:")[-1] if "status:" in text.lower() else False

            return {
                "source_field": source_field,
                "target_field": target_field,
                "confidence": confidence,
                "approved": approved,
            }
        except (ValueError, IndexError):
            logger.debug("Could not parse mapping text: %s", text[:100])
            return None
