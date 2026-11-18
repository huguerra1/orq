from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orq_contracts.adapters.quota import QuotaLimitDetector, VERSIONS


FIXTURES = Path(__file__).parents[1] / "fixtures/v0.1/cli-output"


def local(hour, minute):
    return datetime(2026, 11, 14, hour, minute, tzinfo=ZoneInfo("America/Sao_Paulo")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def detect(text, *, observed_at=None, cli="codex", returncode=1, final_response=False, cooldown=3600000):
    return QuotaLimitDetector(cli, local_timezone="America/Sao_Paulo", default_cooldown_ms=cooldown).detect(
        returncode=returncode, final_response=final_response, diagnostics=tuple(text.splitlines()), observed_at=observed_at or local(21, 30))


def test_real_codex_curly_apostrophe_and_local_reset():
    result = detect((FIXTURES / "codex/usage-limit-2026-11-10.txt").read_text(encoding="utf-8"))
    assert result.reason == "quota_exhausted"
    assert result.reset_at == "2026-11-15T01:05:00.000000Z"
    assert result.blocked_until == result.reset_at
    assert result.reset_source == "cli_reported"
    assert result.detector_version == VERSIONS["codex"]
    assert not result.requires_probe


def test_real_codex_absolute_date_2026_11_14_uses_local_timezone():
    # Saída real de 14/11/2026 às 23:48 locais: data absoluta e apóstrofo tipográfico.
    text = (FIXTURES / "codex/usage-limit-2026-11-14.txt").read_text(encoding="utf-8")
    assert "’" in text and "try again at Nov 15th, 2026 2:00 AM." in text
    result = detect(text, observed_at=local(23, 48))
    assert result.reason == "quota_exhausted"
    assert result.detector_version == VERSIONS["codex"] == "codex-quota-v2"
    expected = datetime(2026, 11, 15, 2, 0, tzinfo=ZoneInfo("America/Sao_Paulo")).astimezone(timezone.utc)
    assert expected.isoformat() == "2026-11-15T05:00:00+00:00"
    assert result.reset_at == result.blocked_until == "2026-11-15T05:00:00.000000Z"
    assert result.reset_source == "cli_reported" and not result.requires_probe
    # Outro fuso configurado muda o UTC: a data impressa é local, não UTC. Às
    # 23:48 de São Paulo (02:48Z), 02:00 em UTC já passou e vira cooldown.
    utc = QuotaLimitDetector("codex", local_timezone="UTC")
    late = utc.detect(returncode=1, final_response=False, diagnostics=tuple(text.splitlines()), observed_at=local(23, 48))
    assert late.reset_at is None and late.reset_source == "cooldown_policy"
    early = utc.detect(returncode=1, final_response=False, diagnostics=tuple(text.splitlines()), observed_at=local(21, 30))
    assert early.reset_at == "2026-11-15T02:00:00.000000Z"


@pytest.mark.parametrize("phrase,expected", [
    ("Nov 15th, 2026 2:00 AM", "2026-11-15T05:00:00.000000Z"),
    ("Nov 15, 2026 2:00 AM", "2026-11-15T05:00:00.000000Z"),
    ("November 16th, 2026 14:30", "2026-11-16T17:30:00.000000Z"),
    ("Dec 1st, 2026 12:05 AM", "2026-12-01T03:05:00.000000Z"),
    ("Nov 15th, 2026 at 1:00 PM", "2026-11-15T16:00:00.000000Z"),
])
def test_codex_absolute_date_variants(phrase, expected):
    assert detect("You’ve hit your usage limit. ... or try again at " + phrase + ".", observed_at=local(23, 48)).reset_at == expected


@pytest.mark.parametrize("phrase", ["Nov 14th, 2026 2:00 AM", "Foo 15th, 2026 2:00 AM", "Feb 30th, 2027 2:00 AM",
                                    "Nov 15th, 2026 13:00 PM", "Nov 15th, 2026 2:75 AM"])
def test_codex_absolute_date_past_or_invalid_uses_cooldown(phrase):
    result = detect("You’ve hit your usage limit. Try again at " + phrase, observed_at=local(23, 48))
    assert result.reason == "quota_exhausted" and result.reset_at is None
    assert result.reset_source == "cooldown_policy"


def test_absolute_date_is_a_codex_v2_grammar_only():
    # As gramáticas v1 das outras CLIs não mudaram: data absoluta cai no cooldown.
    result = detect("Usage limit reached. Resets at Nov 15th, 2026 2:00 AM", cli="claude", observed_at=local(23, 48))
    assert result.detector_version == "claude-quota-v1" and result.reset_at is None


@pytest.mark.parametrize("message,now,expected", [
    ("You've hit your usage limit. Try again at 01:05 AM", local(23, 10), "2026-11-15T04:05:00.000000Z"),
    ("You've hit your usage limit. Try again at 22:05", local(21, 30), "2026-11-15T01:05:00.000000Z"),
    ("You've hit your usage limit. Try again at 12:05 AM", local(23, 10), "2026-11-15T03:05:00.000000Z"),
    ("You've hit your usage limit. Try again at 12:05 PM", local(10, 0), "2026-11-14T15:05:00.000000Z"),
    ("You've hit your usage limit. Try again at 2026-11-16T10:05:00-03:00", local(21, 30), "2026-11-16T13:05:00.000000Z"),
])
def test_reset(message, now, expected):
    assert detect(message, observed_at=now).reset_at == expected


@pytest.mark.parametrize("cli,filename", [("claude", "claude/quota-synthetic.txt"), ("agy", "agy/quota-synthetic.txt"), ("kiro-cli", "kiro/quota-synthetic.txt")])
def test_versioned_synthetic_grammars(cli, filename):
    result = detect((FIXTURES / filename).read_text(encoding="utf-8"), cli=cli)
    assert result.reason == "quota_exhausted"
    assert result.reset_at is None
    assert result.reset_source == "cooldown_policy"
    assert result.blocked_until == "2026-11-15T01:30:00.000000Z"
    assert result.requires_probe


@pytest.mark.parametrize("time", ["25:99", "0:10 PM", "13:00 AM", "2026-11-10T00:00:00Z"])
def test_bad_or_past_explicit_time_uses_cooldown(time):
    result = detect("You've hit your usage limit. Try again at " + time, cooldown=1000)
    assert result.reset_at is None
    assert result.blocked_until == "2026-11-15T00:30:01.000000Z"


@pytest.mark.parametrize("file", ["login-io-error-2026-11-14.txt", "whoami-not-logged-in-2026-11-14.txt"])
def test_real_kiro_login_is_not_quota(file):
    result = detect((FIXTURES / "kiro" / file).read_text(encoding="utf-8"), cli="kiro-cli")
    assert result.reason == "authentication_unavailable"
    assert result.reset_at is result.blocked_until is result.reset_source is None


@pytest.mark.parametrize("message", ["DNS lookup failed", "ENOTFOUND host", "network error: unavailable", "connection refused",
                                    "Error: getaddrinfo ENOTFOUND api.openai.com", "dispatch failure: connection refused"])
def test_network_is_not_quota(message):
    result = detect(message)
    assert result.reason == "network_unavailable"
    assert result.reset_at is None


def test_success_and_unanchored_project_quotes_do_not_trigger():
    assert detect("You've hit your usage limit", returncode=0, final_response=True).reason == "none"
    assert detect('README says: "usage limit"').reason == "none"
    assert detect('[tool] usage limit').reason == "none"
    assert detect("You've hit your usage limit", returncode=0, final_response=False).requires_probe


def test_grammars_are_cli_specific():
    assert detect("RESOURCE_EXHAUSTED", cli="codex").reason == "none"
    assert detect("monthly limit exceeded", cli="claude").reason == "none"


def test_prefixed_login_is_not_quota():
    assert detect("kiro-cli: Not logged in", cli="kiro-cli").reason == "authentication_unavailable"
    assert detect("authentication failedness").reason == "none"
    assert detect("noENOTFOUNDhere").reason == "none"


@pytest.mark.parametrize("first", ["connection failed, retrying...", "kiro-cli: Not logged in"])
@pytest.mark.parametrize("multiline", [False, True])
def test_quota_has_precedence_over_transient_diagnostics(first, multiline):
    lines = [first, "You've hit your usage limit. Try again at 22:05"]
    diagnostics = ("\n".join(lines),) if multiline else tuple(lines)
    result = QuotaLimitDetector("codex", local_timezone="America/Sao_Paulo").detect(
        returncode=1, final_response=False, diagnostics=diagnostics, observed_at=local(21, 30))
    assert result.reason == "quota_exhausted"
    assert result.reset_at == "2026-11-15T01:05:00.000000Z"
