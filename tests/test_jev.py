from __future__ import annotations

from types import SimpleNamespace

import pytest

from orq_contracts.jev import JevDecisionEngine
from orq_contracts.router import DecisionEngineResponseError


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.call = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def system_one(self, **kwargs):
        self.call = kwargs
        return self.response


def routing_input() -> dict:
    ref = {"id": "x", "version": "1", "content_digest": {"algorithm": "sha256", "value": "1" * 64}}
    return {
        "routing_request_id": "request-1",
        "task": {"task_id": "T1", "objective": "Analisar", "task_type": "analysis", "required_capabilities": []},
        "options": [{
            "target_id": "target-a",
            "model_ref": ref,
            "executor_ref": ref,
            "priority": 1,
            "routing_description": "Análise rápida",
            "specialization_tags": ["analysis"],
        }],
    }


def test_jev_adapter_maps_official_choice_response_without_dispatching() -> None:
    answer = SimpleNamespace(choice="target-a", confidence=0.91, probabilities={"target-a": 0.91})
    response = SimpleNamespace(
        choices={"selected_target": answer},
        model="jev-1.13.0",
        request_id="call-1",
        usage=SimpleNamespace(input_tokens=123, output_tokens=4),
    )
    client = FakeClient(response)
    captured = {}

    def choice_factory(**kwargs):
        captured.update(kwargs)
        return kwargs

    engine = JevDecisionEngine(
        model_id="jev-1.13.0",
        client_factory=lambda: client,
        choice_factory=choice_factory,
    )

    result = engine.decide(routing_input())

    assert set(captured["criteria"]) == {"target-a"}
    assert client.call["state"]["routing"]["options"][0]["target_id"] == "target-a"
    assert result.target_id == "target-a"
    assert result.confidence == "0.91"
    assert result.requested_model_id == result.resolved_model_id == "jev-1.13.0"
    assert result.provider_extension["typesafe"]["input_tokens"] == 123


def test_jev_adapter_rejects_invalid_probability() -> None:
    answer = SimpleNamespace(choice="target-a", confidence=2, probabilities={"target-a": 2})
    response = SimpleNamespace(choices={"selected_target": answer}, model="jev", usage=None)
    engine = JevDecisionEngine(
        model_id="jev",
        client_factory=lambda: FakeClient(response),
        choice_factory=lambda **kwargs: kwargs,
    )

    with pytest.raises(DecisionEngineResponseError, match="invalid_jev_probability"):
        engine.decide(routing_input())


def test_pinned_official_sdk_surface_is_available() -> None:
    from importlib.metadata import version

    from typesafe_sdk import Choice, TypeSafeClient

    assert version("typesafe-sdk") == "0.7.2"
    assert Choice(instructions="Escolha", criteria={"a": None, "b": None}).type == "choice"
    assert callable(TypeSafeClient.system_one)
