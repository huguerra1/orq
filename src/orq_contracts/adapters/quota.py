"""Gramáticas de cota: somente diagnósticos da CLI, nunca texto de ferramentas."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..executor import Record, seal_record, stable_id
from ..simulated_executor import format_utc, parse_utc


VERSIONS = {cli: f"{cli}-quota-v1" for cli in ("claude", "codex", "agy", "kiro-cli")}
_PATTERNS = {
    "codex": r"(?:you['’]ve hit your usage limit|usage limit(?: reached| exceeded)?)(?:[ .:].*)?",
    "claude": r"(?:you(?:['’]ve)? (?:have )?hit your (?:usage|session) limit|rate limit(?: reached| exceeded)?|usage limit(?: reached| exceeded)?|limit reached)(?:[ .:].*)?",
    "agy": r"(?:RESOURCE_EXHAUSTED|quota exceeded|usage limit(?: reached| exceeded)?)(?:[ .:].*)?",
    "kiro-cli": r"(?:usage limit|credit limit|monthly limit)(?: reached| exceeded)?(?:[ .:].*)?",
}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class QuotaDetection:
    reason: str
    detector_version: str
    excerpt: str | None = None
    reset_at: str | None = None
    blocked_until: str | None = None
    reset_source: str | None = None

    @property
    def requires_probe(self) -> bool:
        return self.reset_source == "cooldown_policy"


class QuotaLimitDetector:
    def __init__(self, cli: str, *, local_timezone: str = "UTC", default_cooldown_ms: int = 3600000):
        self.version = VERSIONS[cli]
        self.pattern = re.compile(_PATTERNS[cli], re.IGNORECASE)
        self.zone = ZoneInfo(local_timezone)
        if type(default_cooldown_ms) is not int or default_cooldown_ms <= 0:
            raise ValueError("cooldown deve ser positivo")
        self.cooldown_ms = default_cooldown_ms

    def detect(self, *, returncode: int, final_response: bool, diagnostics: tuple[str, ...],
               observed_at: str) -> QuotaDetection:
        """diagnostics já deve excluir result/assistant/tool/file dos envelopes JSON.

        Texto livre só é aceito em linhas diagnósticas ancoradas pelo parser da CLI.
        Não há parâmetro de workspace nem leitura de arquivos nesta fronteira.
        """
        if returncode == 0 and final_response:
            return QuotaDetection("none", self.version)
        fallback = None
        for raw in (line for diagnostic in diagnostics for line in diagnostic.splitlines()):
            line = _ANSI.sub("", raw).strip()
            line = re.sub(r"^(?:ERROR|Error|error|status):\s*", "", line)
            # Login/DNS/rede não bloqueiam o pool de assinatura.
            if re.search(r"\b(?:Could not load your login|Not logged in|login expired|authentication failed|"
                         r"session expired|unauthorized|please log in)\b", line, re.I):
                fallback = fallback or QuotaDetection("authentication_unavailable", self.version, line[:512])
            if re.search(r"\b(?:network error|DNS|ENOTFOUND|EAI_AGAIN|connection (?:failed|refused|timed out))\b", line, re.I):
                fallback = fallback or QuotaDetection("network_unavailable", self.version, line[:512])
            if not self.pattern.fullmatch(line):
                continue
            now = parse_utc(observed_at)
            reset = self._reset(line, now)
            return QuotaDetection(
                "quota_exhausted", self.version, line[:512], format_utc(reset) if reset else None,
                format_utc(reset or now + timedelta(milliseconds=self.cooldown_ms)),
                "cli_reported" if reset else "cooldown_policy",
            )
        return fallback or QuotaDetection("none", self.version)

    def _reset(self, line: str, now: datetime) -> datetime | None:
        match = re.search(r"(?:try again at|resets? at)\s+(\d{4}-\d\d-\d\d[T ][0-9:+.Z-]+)", line, re.I)
        if match:
            try:
                value = datetime.fromisoformat(match[1].rstrip(".").replace("Z", "+00:00"))
                value = value.replace(tzinfo=self.zone) if value.tzinfo is None else value
                return value.astimezone(timezone.utc) if value > now else None
            except ValueError:
                return None
        match = re.search(r"(?:try again at|resets? at)\s+(\d{1,2}):(\d{2})\s*(AM|PM)?\b", line, re.I)
        if not match:
            return None
        hour, minute = int(match[1]), int(match[2])
        meridiem = match[3]
        if minute > 59 or (meridiem and not 1 <= hour <= 12) or (not meridiem and hour > 23):
            return None
        if meridiem:
            hour = hour % 12 + (12 if meridiem.upper() == "PM" else 0)
        local = now.astimezone(self.zone)
        reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= local:
            reset += timedelta(days=1)
        # Hora inexistente na mudança de DST não deve virar um horário inventado.
        utc = reset.astimezone(timezone.utc)
        if utc.astimezone(self.zone).replace(tzinfo=None) != reset.replace(tzinfo=None):
            return None
        return utc

    def state_record(self, detection: QuotaDetection, *, profile: Record, intent: Record,
                     observed_at: str) -> Record:
        if detection.reason != "quota_exhausted":
            raise ValueError("observação não comprova cota")
        record = {
            "schema_version": "0.1", "quota_event_id": stable_id("quota", intent["dispatch_idempotency_key"]),
            "run_id": intent["run_id"], "source_attempt_id": intent["attempt_id"],
            **{key: profile[key] for key in ("subscription_id", "family_id", "quota_pool_id")},
            "status": "cooldown" if detection.requires_probe else "quota_exhausted",
            "observed_at": observed_at, "reset_at": detection.reset_at,
            "blocked_until": detection.blocked_until, "reset_source": detection.reset_source,
            "raw_message_excerpt": detection.excerpt,
        }
        if detection.requires_probe:
            record["default_cooldown_ms"] = self.cooldown_ms
        return seal_record(record, "quota_state_record")
