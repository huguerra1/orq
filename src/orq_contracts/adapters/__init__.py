"""Adaptadores de assinaturas oficiais; nenhuma integração de banco ou memória."""

from .cli import AgyAdapter, ClaudeCodeAdapter, CodexAdapter, KiroAdapter, CLIPermissions
from .quota import QuotaLimitDetector, QuotaDetection

__all__ = ["AgyAdapter", "ClaudeCodeAdapter", "CodexAdapter", "KiroAdapter", "CLIPermissions",
           "QuotaLimitDetector", "QuotaDetection"]
