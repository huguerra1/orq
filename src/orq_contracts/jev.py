"""Adaptador opcional TypeSafe/Jev para a fronteira DecisionEngine."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from .router import DecisionEngineResponseError, DecisionEngineResult, RoutingError


def _probability(value: Any, path: str) -> str:
    try:
        decimal = Decimal(str(value))
    except Exception as error:
        raise DecisionEngineResponseError("invalid_jev_probability", path, "probabilidade não decimal") from error
    if not decimal.is_finite() or decimal < 0 or decimal > 1:
        raise DecisionEngineResponseError("invalid_jev_probability", path, "probabilidade fora de 0..1")
    return format(decimal, "f")


class JevDecisionEngine:
    """Converte um Choice do SDK oficial em recomendação neutra do ORQ.

    `client_factory` e `choice_factory` permitem testes sem rede. Quando omitidos,
    o SDK opcional é importado apenas na primeira chamada.
    """

    def __init__(
        self,
        *,
        model_id: str,
        client_factory: Callable[[], Any] | None = None,
        choice_factory: Callable[..., Any] | None = None,
        sdk_version: str = "0.7.2",
    ):
        self.model_id = model_id
        self._client_factory = client_factory
        self._choice_factory = choice_factory
        self.sdk_version = sdk_version

    def _factories(self) -> tuple[Callable[[], Any], Callable[..., Any]]:
        if self._client_factory is not None and self._choice_factory is not None:
            return self._client_factory, self._choice_factory
        try:
            from typesafe_sdk import Choice, TypeSafeClient
        except ImportError as error:
            raise RoutingError(
                "typesafe_sdk_unavailable",
                "/decision_engine",
                "instale o extra opcional 'jev' para usar TypeSafe/Jev",
            ) from error
        return self._client_factory or TypeSafeClient, self._choice_factory or Choice

    def decide(self, routing_input: dict[str, Any]) -> DecisionEngineResult:
        options = routing_input.get("options", [])
        if not options:
            raise RoutingError("no_jev_options", "/options", "nenhum destino elegível")
        client_factory, choice_factory = self._factories()
        criteria = {
            option["target_id"]: {
                "model": option["model_ref"]["id"],
                "executor": option["executor_ref"]["id"],
                "rule_priority": option["priority"],
                "description": option["routing_description"],
                "specializations": option["specialization_tags"],
            }
            for option in options
        }
        question = choice_factory(
            instructions=(
                "Escolha exatamente um target_id elegível para executar a tarefa. "
                "Considere adequação sem reinterpretar política, permissões ou elegibilidade."
            ),
            criteria=criteria,
        )
        with client_factory() as client:
            response = client.system_one(
                state={"routing": routing_input},
                questions={"selected_target": question},
                model=self.model_id,
            )
        answer = response.choices["selected_target"]
        scores = {
            str(target_id): _probability(value, f"/probabilities/{target_id}")
            for target_id, value in answer.probabilities.items()
        }
        usage = getattr(response, "usage", None)
        provider_extension: dict[str, Any] = {
            "typesafe": {
                "sdk_version": self.sdk_version,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            }
        }
        return DecisionEngineResult(
            target_id=str(answer.choice),
            confidence=_probability(answer.confidence, "/confidence"),
            scores=scores,
            engine_call_id=getattr(response, "request_id", None),
            requested_model_id=self.model_id,
            resolved_model_id=getattr(response, "model", None),
            provider_extension=provider_extension,
        )
