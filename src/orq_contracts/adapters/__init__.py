"""Adaptadores de assinaturas oficiais; nenhuma integração de banco ou memória."""

from .cli import AgyAdapter, ClaudeCodeAdapter, CodexAdapter, KiroAdapter, CLIPermissions
from .quota import QuotaLimitDetector, QuotaDetection
from .verify import V01_V12_IDS, probe_cli_versions

__all__ = ["AgyAdapter", "ClaudeCodeAdapter", "CodexAdapter", "KiroAdapter", "CLIPermissions",
           "QuotaLimitDetector", "QuotaDetection", "V01_V12_IDS", "probe_cli_versions"]
