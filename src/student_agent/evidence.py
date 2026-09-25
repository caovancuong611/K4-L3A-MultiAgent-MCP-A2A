from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass(frozen=True)
class EvidenceRecord:
    """One MCP evidence response, kept exactly as the gateway returned it."""

    tool_name: str
    domain: str
    evidence_ref: str
    data: Any
    warnings: tuple[str, ...]


class CaseEvidenceCollector:
    """Fetches MCP evidence for a single case and keeps the competition rules
    structurally true rather than just documented:

    - every call is scoped to this collector's ``case_id``, so evidence can
      never be pulled cross-case by mistake;
    - tool names are whatever ``gateway.call`` resolves via discovery; this
      class never invents or edits an ``evidence_ref``, it only stores what
      the gateway/contract validation returned;
    - each *new* fetch emits a ``tool_result_consumed`` trace event for the
      calling actor, so evidence-to-conclusion linkage stays auditable;
    - repeat fetches with the same tool + arguments are served from cache
      instead of re-calling the (audited) MCP gateway.

    Fetching evidence is not the same as citing it: callers should still only
    place an ``evidence_ref`` into ``claim_assessments``/``evidence_refs`` in
    the final output when that specific record actually supports the claim
    being made, not simply because it was fetched during investigation.
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter, case_id: str) -> None:
        self._gateway = gateway
        self._trace = trace
        self._case_id = case_id
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...]], EvidenceRecord] = {}

    @property
    def case_id(self) -> str:
        return self._case_id

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        """Every distinct evidence record fetched so far for this case."""
        return tuple(self._cache.values())

    async def fetch(self, tool_name: str, *, actor: str, **arguments: str) -> EvidenceRecord:
        """Call an MCP tool for this case and log its consumption.

        ``actor`` is the specialist agent name recorded on the trace event
        (e.g. ``"order-agent"``); ``arguments`` are the tool's own parameters
        (never ``case_id``, which this collector always supplies itself).
        """
        key = (tool_name, tuple(sorted(arguments.items())))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        evidence = await self._gateway.call(tool_name, case_id=self._case_id, **arguments)
        record = EvidenceRecord(
            tool_name=tool_name,
            domain=evidence["domain"],
            evidence_ref=evidence["evidence_ref"],
            data=evidence["data"],
            warnings=tuple(evidence.get("warnings", ())),
        )
        self._cache[key] = record

        self._trace.emit(
            case_id=self._case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[record.evidence_ref],
        )
        return record
