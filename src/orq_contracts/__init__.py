"""Contratos executáveis do ORQ."""

from .canonical import canonical_digest, canonicalize
from .context import ContextError, MaterializedContext, materialize_knowledge_context
from .jev import JevDecisionEngine
from .planner import FixedTemplatePlanner, Planner, PlanningError, PlanningResult, plan_and_validate
from .router import (
    DecisionEngine,
    DecisionEngineResponseError,
    DecisionEngineResult,
    RoutingError,
    SimulatedDecisionEngine,
    route_and_finalize,
)
from .selection import materialize_unit, select_knowledge
from .validator import (
    ValidationIssue,
    validate_instance,
    validate_context_manifest,
    validate_knowledge_source,
    validate_planning_record,
    validate_routing_decision,
    validate_routing_policy,
    validate_routing_target_catalog,
    validate_task,
    validate_workflow,
)
from .vault import IngestedSource, VaultError, build_catalog_snapshot, ingest_markdown_source

__all__ = [
    "ValidationIssue",
    "ContextError",
    "DecisionEngine",
    "DecisionEngineResponseError",
    "DecisionEngineResult",
    "FixedTemplatePlanner",
    "IngestedSource",
    "JevDecisionEngine",
    "MaterializedContext",
    "PlanningError",
    "PlanningResult",
    "Planner",
    "RoutingError",
    "SimulatedDecisionEngine",
    "VaultError",
    "build_catalog_snapshot",
    "canonical_digest",
    "canonicalize",
    "ingest_markdown_source",
    "materialize_unit",
    "materialize_knowledge_context",
    "plan_and_validate",
    "route_and_finalize",
    "select_knowledge",
    "validate_instance",
    "validate_context_manifest",
    "validate_knowledge_source",
    "validate_planning_record",
    "validate_routing_decision",
    "validate_routing_policy",
    "validate_routing_target_catalog",
    "validate_task",
    "validate_workflow",
]
