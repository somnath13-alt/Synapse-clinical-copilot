from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "data" / "fixtures"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
NOTICE = "SYNTHETIC — DEMO ONLY. Not for clinical or coverage decisions."
REQUIRED_SOURCE_TYPES = {
    "EHR",
    "GUIDELINE",
    "PAYER_POLICY",
    "FORMULARY",
    "SPECIALIST_NOTE",
}
REQUIRED_PROVENANCE_FIELDS = {
    "source_id",
    "source_type",
    "source_title",
    "version",
    "timestamp",
    "recorded_at",
    "effective_from",
    "effective_to",
    "section",
    "relevant_excerpt",
    "checksum_algorithm",
    "checksum",
    "synthetic",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_fixture_set() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(MANIFEST_PATH)
    fixtures = [_read_json(FIXTURE_ROOT / entry["path"]) for entry in manifest["fixture_files"]]
    return manifest, fixtures


def _versions(fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [version for fixture in fixtures for version in fixture["versions"]]


def _evidence_items(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [evidence for version in versions for evidence in version["evidence_items"]]


def _assertions(versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [assertion for version in versions for assertion in version["assertions"]]


def _by_id(items: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    return {item[field]: item for item in items}


def _parse_utc(value: str) -> datetime:
    assert value.endswith("Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset() is not None
    return parsed


def _canonical_sha256(item: dict[str, Any], fields: list[str]) -> str:
    payload = {field: item[field] for field in fields}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assert_unique(values: list[str]) -> None:
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    assert duplicates == []


def test_manifest_lists_every_fixture_and_all_fixture_ids_are_unique() -> None:
    manifest, fixtures = _load_fixture_set()
    listed_paths = {entry["path"] for entry in manifest["fixture_files"]}
    actual_paths = {
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in FIXTURE_ROOT.rglob("*.json")
        if path != MANIFEST_PATH
    }

    assert listed_paths == actual_paths
    _assert_unique([entry["fixture_id"] for entry in manifest["fixture_files"]])
    assert {fixture["fixture_id"] for fixture in fixtures} == {
        entry["fixture_id"] for entry in manifest["fixture_files"]
    }


def test_object_identities_are_distinct_and_unique_within_each_identity_type() -> None:
    manifest, fixtures = _load_fixture_set()
    versions = _versions(fixtures)
    evidence = _evidence_items(versions)
    assertions = _assertions(versions)

    _assert_unique([version["document_version_id"] for version in versions])
    _assert_unique([item["evidence_id"] for item in evidence])
    _assert_unique([assertion["assertion_id"] for assertion in assertions])
    _assert_unique([claim["claim_id"] for claim in manifest["supported_claims"]])
    _assert_unique([scenario["scenario_id"] for scenario in manifest["scenarios"]])
    _assert_unique([entity["entity_id"] for entity in manifest["fictional_entities"]])
    _assert_unique(
        [relationship["relationship_id"] for relationship in manifest["source_relationships"]]
    )

    identity_sets = {
        "source": {version["source_id"] for version in versions},
        "document": {version["document_id"] for version in versions},
        "version": {version["document_version_id"] for version in versions},
        "evidence": {item["evidence_id"] for item in evidence},
        "assertion": {assertion["assertion_id"] for assertion in assertions},
        "claim": {claim["claim_id"] for claim in manifest["supported_claims"]},
    }
    names = list(identity_sets)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            assert identity_sets[left_name].isdisjoint(identity_sets[right_name])


def test_all_evidence_references_and_expected_claims_resolve() -> None:
    manifest, fixtures = _load_fixture_set()
    versions = _versions(fixtures)
    evidence_ids = {item["evidence_id"] for item in _evidence_items(versions)}
    claim_by_id = _by_id(manifest["supported_claims"], "claim_id")

    for assertion in _assertions(versions):
        assert assertion["evidence_ids"]
        assert set(assertion["evidence_ids"]) <= evidence_ids

    for claim in manifest["supported_claims"]:
        assert claim["evidence_ids"]
        assert set(claim["evidence_ids"]) <= evidence_ids

    for scenario in manifest["scenarios"]:
        for claim_id in scenario["expected_claim_ids"]:
            assert claim_id in claim_by_id
            assert claim_by_id[claim_id]["evidence_ids"]


def test_every_version_and_evidence_item_has_complete_provenance() -> None:
    _, fixtures = _load_fixture_set()
    versions = _versions(fixtures)

    for fixture in fixtures:
        assert fixture["synthetic"] is True
        assert fixture["synthetic_data_notice"] == NOTICE

    for version in versions:
        assert REQUIRED_PROVENANCE_FIELDS <= version.keys()
        assert version["document_version_id"].startswith("DV-SYN-")
        assert version["document_id"].startswith("DOC-SYN-")
        assert version["source_id"].startswith("SRC-SYN-")
        assert version["source_title"].startswith("SYNTHETIC —")
        assert version["synthetic"] is True
        assert version["relevant_excerpt"]
        _parse_utc(version["timestamp"])
        _parse_utc(version["recorded_at"])
        _parse_utc(version["effective_from"])
        if version["effective_to"] is not None:
            assert _parse_utc(version["effective_from"]) <= _parse_utc(
                version["effective_to"]
            )

        for evidence in version["evidence_items"]:
            assert REQUIRED_PROVENANCE_FIELDS <= evidence.keys()
            assert evidence["evidence_id"].startswith("EV-SYN-")
            assert evidence["document_version_id"] == version["document_version_id"]
            assert evidence["document_id"] == version["document_id"]
            assert evidence["source_id"] == version["source_id"]
            assert evidence["source_type"] == version["source_type"]
            assert evidence["source_title"] == version["source_title"]
            assert evidence["version"] == version["version"]
            assert evidence["synthetic"] is True
            assert evidence["section"]
            assert evidence["relevant_excerpt"]
            _parse_utc(evidence["timestamp"])
            _parse_utc(evidence["recorded_at"])
            _parse_utc(evidence["effective_from"])


def test_all_stored_checksums_match_canonical_utf8_json() -> None:
    manifest, fixtures = _load_fixture_set()
    scopes = manifest["fixture_contract"]["checksum_scope"]

    for version in _versions(fixtures):
        assert version["checksum_algorithm"] == "SHA-256"
        assert version["checksum"] == _canonical_sha256(
            version, scopes["document_version"]
        )
        for evidence in version["evidence_items"]:
            assert evidence["checksum_algorithm"] == "SHA-256"
            assert evidence["checksum"] == _canonical_sha256(
                evidence, scopes["evidence_item"]
            )


def test_v1_starts_current_and_v2_is_a_noncurrent_coherent_candidate() -> None:
    _, fixtures = _load_fixture_set()
    version_by_id = _by_id(_versions(fixtures), "document_version_id")
    v1 = version_by_id["DV-SYN-POL-VEL-V1"]
    v2 = version_by_id["DV-SYN-POL-VEL-V2"]

    assert (v1["governance_state"], v1["current_at_seed"]) == ("APPLIED", True)
    assert (v2["governance_state"], v2["current_at_seed"]) == (
        "CANDIDATE_NOT_CURRENT",
        False,
    )
    assert v2["candidate_successor_of"] == v1["document_version_id"]
    assert _parse_utc(v1["effective_to"]) < _parse_utc(v2["effective_from"])
    assert _parse_utc(v2["timestamp"]) < _parse_utc(v2["recorded_at"])
    assert _parse_utc(v2["recorded_at"]) < _parse_utc(v2["effective_from"])

    manifest = _read_json(MANIFEST_PATH)
    correction = next(
        scenario
        for scenario in manifest["scenarios"]
        if scenario["scenario_id"] == "SCN-E-GOVERNED-CORRECTION"
    )
    pending, applied = correction["state_sequence"][1:3]
    assert _parse_utc(pending["at"]) < _parse_utc(v2["effective_from"])
    assert _parse_utc(v2["effective_from"]) < _parse_utc(applied["at"])
    assert pending["current_policy_version_id"] == v1["document_version_id"]
    assert applied["current_policy_version_id"] == v2["document_version_id"]


def test_v1_and_v2_encode_the_approved_prerequisite_change() -> None:
    _, fixtures = _load_fixture_set()
    version_by_id = _by_id(_versions(fixtures), "document_version_id")
    v1 = version_by_id["DV-SYN-POL-VEL-V1"]["structured_data"]
    v2 = version_by_id["DV-SYN-POL-VEL-V2"]["structured_data"]
    ehr = version_by_id["DV-SYN-EHR-CASE-001-V1"]["structured_data"]

    assert v1["prior_authorization_required"] is True
    assert v2["prior_authorization_required"] is True
    assert v1["required_preferred_therapies"] == v2["required_preferred_therapies"] == [
        "SYN-MED-NOR",
        "SYN-MED-BRV",
    ]
    assert (v1["combination_rule"], v1["minimum_distinct_failures"]) == ("ALL", 2)
    assert (v2["combination_rule"], v2["minimum_distinct_failures"]) == ("ANY", 1)
    assert v1["minimum_days_each"] == v2["minimum_days_each"] == 30
    assert ehr["prior_therapy_medication_id"] == "SYN-MED-NOR"
    assert ehr["prior_therapy_days"] == 35
    assert ehr["prior_therapy_outcome"] == "INADEQUATE_RESPONSE"
    assert ehr["bravex_trial_status"] == "NOT_DOCUMENTED"


def test_flagship_reconciliation_is_cross_dimensional_not_a_contradiction() -> None:
    manifest, fixtures = _load_fixture_set()
    assertion_by_id = _by_id(_assertions(_versions(fixtures)), "assertion_id")
    guideline = assertion_by_id["AST-SYN-GUIDE-SUPPORT"]
    payer = assertion_by_id["AST-SYN-POL-V1-PA"]
    scenario = next(
        item for item in manifest["scenarios"] if item["scenario_id"] == "SCN-B-CROSS-DIMENSION"
    )

    assert guideline["decision_dimension"] == "CLINICAL_APPROPRIATENESS"
    assert payer["decision_dimension"] == "AUTHORIZATION_REQUIREMENT"
    assert guideline["decision_dimension"] != payer["decision_dimension"]
    assert scenario["expected_conflict"] == {
        "type": "COMPATIBLE_CONSTRAINT",
        "dimensions": ["CLINICAL_APPROPRIATENESS", "AUTHORIZATION_REQUIREMENT"],
        "severity": "INFORMATIONAL",
        "resolution_state": "NOT_APPLICABLE",
    }


def test_true_conflict_is_same_scope_same_dimension_and_isolated() -> None:
    manifest, fixtures = _load_fixture_set()
    version_by_id = _by_id(_versions(fixtures), "document_version_id")
    assertion_by_id = _by_id(_assertions(_versions(fixtures)), "assertion_id")
    payer = assertion_by_id["AST-SYN-POL-V1-PA"]
    conflict = assertion_by_id["AST-SYN-FORM-CONFLICT-PA"]
    scenario = next(
        item
        for item in manifest["scenarios"]
        if item["scenario_id"] == "SCN-F-TEST-ONLY-CONFLICT"
    )
    conflict_version = version_by_id[scenario["injected_version_id"]]

    assert payer["normalized_scope"] == conflict["normalized_scope"]
    assert payer["decision_dimension"] == conflict["decision_dimension"] == (
        "AUTHORIZATION_REQUIREMENT"
    )
    assert payer["value"] is True
    assert conflict["value"] is False
    as_of = _parse_utc(scenario["as_of"])
    for version_id in ("DV-SYN-POL-VEL-V1", "DV-SYN-FORM-HLP-CONFLICT"):
        version = version_by_id[version_id]
        assert _parse_utc(version["effective_from"]) <= as_of <= _parse_utc(
            version["effective_to"]
        )

    assert scenario["test_only"] is True
    assert scenario["expected_confidence"] == "LOW"
    assert scenario["expected_escalation"] is True
    assert scenario["expected_conflict"] == {
        "type": "SAME_DIMENSION_DISAGREEMENT",
        "dimension": "AUTHORIZATION_REQUIREMENT",
        "severity": "HIGH",
        "resolution_state": "UNRESOLVED",
    }
    assert conflict_version["test_only"] is True
    assert conflict_version["baseline_included"] is False
    assert conflict_version["current_at_seed"] is False
    conflict_entry = next(
        entry
        for entry in manifest["fixture_files"]
        if entry["fixture_id"] == "FIX-SYN-FORM-CONFLICT"
    )
    assert conflict_entry["path"].startswith("test_only/")
    assert conflict_entry["baseline_included"] is False
    assert conflict_entry["test_only"] is True


def test_fixture_content_is_visibly_synthetic_and_uses_only_fictional_entities() -> None:
    manifest, fixtures = _load_fixture_set()
    entities = manifest["fictional_entities"]
    allowed_entity_ids = {entity["entity_id"] for entity in entities}
    forbidden_real_world_terms = {
        "aetna",
        "anthem",
        "blue cross",
        "cigna",
        "humira",
        "medicaid",
        "medicare",
        "optum",
        "unitedhealth",
    }

    assert manifest["synthetic"] is True
    assert manifest["synthetic_data_notice"] == NOTICE
    assert all(entity["fictional"] is True for entity in entities)
    assert all(entity["entity_id"].startswith("SYN-") for entity in entities)
    patient = next(entity for entity in entities if entity["entity_type"] == "PATIENT")
    assert patient["display_name"].startswith("Synthetic Patient")
    assert not ({"name", "birth_date", "address", "member_number"} & patient.keys())

    serialized = json.dumps(fixtures, ensure_ascii=False).lower()
    assert not any(term in serialized for term in forbidden_real_world_terms)

    for assertion in _assertions(_versions(fixtures)):
        for field in ("subject_id", "object_id"):
            value = assertion.get(field)
            if value is not None and value.startswith("SYN-"):
                assert value in allowed_entity_ids


def test_golden_scenario_metadata_is_complete_and_safety_aligned() -> None:
    manifest, _ = _load_fixture_set()
    scenarios = _by_id(manifest["scenarios"], "scenario_id")
    assert set(scenarios) == {
        "SCN-A-BASELINE-PA",
        "SCN-B-CROSS-DIMENSION",
        "SCN-C-MISSING-PAYER",
        "SCN-D-UNSUPPORTED",
        "SCN-E-GOVERNED-CORRECTION",
        "SCN-F-TEST-ONLY-CONFLICT",
    }

    common_fields = {
        "scenario_id",
        "name",
        "as_of",
        "source_mode",
        "expected_claim_ids",
        "expected_confidence",
        "expected_escalation",
        "expected_behavior",
    }
    for scenario in scenarios.values():
        assert common_fields <= scenario.keys()
        _parse_utc(scenario["as_of"])
        assert scenario["expected_behavior"]

    assert scenarios["SCN-A-BASELINE-PA"]["expected_confidence"] == "HIGH"
    assert scenarios["SCN-C-MISSING-PAYER"]["expected_confidence"] == "LOW"
    assert scenarios["SCN-C-MISSING-PAYER"]["expected_escalation"] is True
    assert scenarios["SCN-C-MISSING-PAYER"]["prohibited_conclusions"] == [
        "PA_REQUIRED",
        "PA_NOT_REQUIRED",
    ]
    assert scenarios["SCN-D-UNSUPPORTED"]["expected_confidence"] is None
    assert scenarios["SCN-D-UNSUPPORTED"]["expected_claim_ids"] == []
    assert scenarios["SCN-D-UNSUPPORTED"]["source_mode"] == "NO_RETRIEVAL"
    assert scenarios["SCN-F-TEST-ONLY-CONFLICT"]["expected_confidence"] == "LOW"
    assert scenarios["SCN-F-TEST-ONLY-CONFLICT"]["expected_escalation"] is True


def test_source_categories_and_flagship_criticality_are_complete() -> None:
    manifest, fixtures = _load_fixture_set()
    baseline_source_types = {
        version["source_type"]
        for fixture in fixtures
        for version in fixture["versions"]
        if version["baseline_included"] and not version["test_only"]
    }
    assert baseline_source_types == REQUIRED_SOURCE_TYPES
    assert manifest["source_criticality"] == {
        "intent": "PRIOR_AUTHORIZATION",
        "critical": ["PATIENT_CASE", "MEDICATION", "PAYER_PLAN", "PAYER_POLICY"],
        "important": ["GUIDELINE", "FORMULARY"],
        "optional": ["SPECIALIST_NOTE"],
    }


def test_fixture_files_have_stable_canonical_representations() -> None:
    manifest, _ = _load_fixture_set()
    first_pass: dict[str, bytes] = {}
    second_pass: dict[str, bytes] = {}
    for entry in manifest["fixture_files"]:
        path = FIXTURE_ROOT / entry["path"]
        first_pass[entry["path"]] = json.dumps(
            _read_json(path), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        second_pass[entry["path"]] = json.dumps(
            _read_json(path), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    assert first_pass == second_pass
    assert all(payload for payload in first_pass.values())
