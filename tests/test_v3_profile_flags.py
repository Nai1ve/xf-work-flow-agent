"""V3 发布开关的冻结与显式启用契约。"""

from utils.profiles import ProfileConfig
from my_agent import _runtime_package_version


def test_v3_clusters_are_off_by_default(monkeypatch) -> None:
    for name in (
        "AGENT_MEETING_REFERENCE_V3",
        "AGENT_MEETING_WORKSPACE_BUILDING_PROBE_V3",
        "AGENT_SPEECH_ACT_V3",
        "AGENT_LEAVE_RANGE_V3",
        "AGENT_BUDGET_RUNTIME_V3",
        "AGENT_PROJECT_SEARCH_REFINEMENT_V3",
    ):
        monkeypatch.delenv(name, raising=False)

    config = ProfileConfig.from_env()

    assert config.meeting_reference_v3 is False
    assert config.meeting_workspace_building_probe_v3 is False
    assert config.speech_act_v3 is False
    assert config.leave_range_v3 is False
    assert config.budget_runtime_v3 is False
    assert config.project_search_refinement_v3 is False


def test_v3_clusters_can_be_enabled_independently(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MEETING_REFERENCE_V3", "1")
    monkeypatch.setenv("AGENT_MEETING_WORKSPACE_BUILDING_PROBE_V3", "yes")
    monkeypatch.setenv("AGENT_SPEECH_ACT_V3", "0")
    monkeypatch.setenv("AGENT_LEAVE_RANGE_V3", "true")
    monkeypatch.setenv("AGENT_BUDGET_RUNTIME_V3", "off")
    monkeypatch.setenv("AGENT_PROJECT_SEARCH_REFINEMENT_V3", "1")

    flags = ProfileConfig.from_env().feature_flags()

    assert flags["meeting_reference_v3"] is True
    assert flags["meeting_workspace_building_probe_v3"] is True
    assert flags["speech_act_v3"] is False
    assert flags["leave_range_v3"] is True
    assert flags["budget_runtime_v3"] is False
    assert flags["project_search_refinement_v3"] is True


def test_package_version_has_config_default_and_env_override(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_PACKAGE_VERSION", raising=False)
    assert _runtime_package_version() == "v3-clustered-dev"
    monkeypatch.setenv("AGENT_PACKAGE_VERSION", "candidate-contract-001")
    assert _runtime_package_version() == "candidate-contract-001"


def test_oa_semantic_gate_is_independent_from_contract_switch() -> None:
    config = ProfileConfig(
        contract_fixes_v2=False,
        oa_semantic_gate_v3=True,
        legacy_oa_compat=False,
    )
    assert config.allow_oa_postcheck(explicit_request=False, multi_domain=True) is False
    assert config.allow_oa_postcheck(explicit_request=True, multi_domain=True) is True


def test_oa_semantic_gate_still_allows_explicit_legacy_compat() -> None:
    config = ProfileConfig(
        contract_fixes_v2=True,
        oa_semantic_gate_v3=True,
        legacy_oa_compat=True,
    )
    assert config.allow_oa_postcheck(explicit_request=False, multi_domain=True) is True
