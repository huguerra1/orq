"""Fronteiras dos cenários Q que reutilizam os adaptadores e o parser existentes."""
import pytest

from test_cli_adapters import build, finish, ADAPTERS
from test_vault import source_metadata, write_source
from orq_contracts.adapters import AgyAdapter
from orq_contracts.vault import ingest_markdown_source, VaultError


def test_Q04_agy_permission_error_filters_noise_and_never_completes(build):
    adapter, intent, bundle, workspace = build(AgyAdapter, mode="agy_permission")
    _, result = finish(adapter, intent, bundle, workspace)
    assert not result.produced_files
    assert result.errors[0]["code"] == "permission_or_empty_output"
    assert "google.Init" not in result.errors[0]["message"]


@pytest.mark.parametrize("field", ["area_scope: common", "target_families: [family_claude]", "target_roles: [security_reviewer]", "audience: common"])
def test_Q19_catalog_audience_cannot_be_changed_by_frontmatter(tmp_path, field):
    raw = ("---\n" + field + "\n---\n# Conteúdo\n").encode()
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)
    with pytest.raises(VaultError) as raised:
        ingest_markdown_source(vault, source_metadata(raw))
    assert raised.value.code == "forbidden_frontmatter_field"


@pytest.mark.parametrize("adapter_class", ADAPTERS)
def test_Q11_project_literal_and_valid_final_do_not_exhaust_quota(build, adapter_class):
    adapter, intent, bundle, workspace = build(adapter_class, mode="literal_success")
    observed, result = finish(adapter, intent, bundle, workspace)
    assert observed.status == "finished"
    assert result.produced_files and not result.errors
    assert adapter.quota_consumption(intent)["quota_outcome"] == "accepted"
