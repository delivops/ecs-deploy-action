#!/usr/bin/env python3
"""Unit tests for the pure logic in scripts/generate_task_def.py.

These complement the YAML->JSON snapshot tests in test.py by asserting on the
individual building blocks (validation, config merge, dotenv parsing, image
parsing, secret building) including their failure paths.

Run with: pytest tests/test_unit.py
"""
import sys
from pathlib import Path

import pytest

# Make the generator importable regardless of the working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import generate_task_def as g  # noqa: E402


# --------------------------------------------------------------------------- #
# validate_config
# --------------------------------------------------------------------------- #
def test_validate_config_valid_fargate():
    g.validate_config({"launch_type": "FARGATE", "cpu": 256, "memory": 512})


def test_validate_config_invalid_launch_type():
    with pytest.raises(g.ValidationError):
        g.validate_config({"launch_type": "SERVERLESS"})


def test_validate_config_fargate_requires_awsvpc():
    with pytest.raises(g.ValidationError):
        g.validate_config({"launch_type": "FARGATE", "network_mode": "bridge"})


def test_validate_config_invalid_cpu_memory_combo():
    with pytest.raises(g.ValidationError):
        g.validate_config({"launch_type": "FARGATE", "cpu": 256, "memory": 4096})


def test_validate_config_ec2_flexible_but_positive():
    g.validate_config({"launch_type": "EC2", "network_mode": "bridge", "cpu": 100, "memory": 100})
    with pytest.raises(g.ValidationError):
        g.validate_config({"launch_type": "EC2", "cpu": -1})


# --------------------------------------------------------------------------- #
# merge_configs — arrays extend, scalars/objects replace, null removes
# --------------------------------------------------------------------------- #
def test_merge_arrays_extend():
    merged = g.merge_configs({"envs": [{"A": "1"}]}, {"envs": [{"B": "2"}]})
    assert merged["envs"] == [{"A": "1"}, {"B": "2"}]


def test_merge_scalar_replaces():
    merged = g.merge_configs({"cpu": 256}, {"cpu": 512})
    assert merged["cpu"] == 512


def test_merge_object_replaces():
    merged = g.merge_configs(
        {"health_check": {"command": "old", "interval": 30}},
        {"health_check": {"command": "new"}},
    )
    assert merged["health_check"] == {"command": "new"}


def test_merge_null_removes_field():
    merged = g.merge_configs({"port": 8080}, {"port": None})
    assert "port" not in merged


def test_merge_strips_services_overrides():
    merged = g.merge_configs({"cpu": 256, "services_overrides": {"x": {}}}, {})
    assert "services_overrides" not in merged


# --------------------------------------------------------------------------- #
# parse_dotenv_file
# --------------------------------------------------------------------------- #
def test_parse_dotenv_basic(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nFOO=bar\n\nBAZ='qux'\nQUOTED=\"v a l\"\n")
    assert g.parse_dotenv_file(p) == {"FOO": "bar", "BAZ": "qux", "QUOTED": "v a l"}


def test_parse_dotenv_last_key_wins(tmp_path):
    p = tmp_path / ".env"
    p.write_text("K=1\nK=2\n")
    assert g.parse_dotenv_file(p) == {"K": "2"}


def test_parse_dotenv_missing_equals_raises(tmp_path):
    p = tmp_path / ".env"
    p.write_text("NOT_VALID\n")
    with pytest.raises(g.ValidationError):
        g.parse_dotenv_file(p)


def test_parse_dotenv_invalid_key_raises(tmp_path):
    p = tmp_path / ".env"
    p.write_text("1BAD=x\n")
    with pytest.raises(g.ValidationError):
        g.parse_dotenv_file(p)


def test_parse_dotenv_missing_file_raises(tmp_path):
    with pytest.raises(g.ValidationError):
        g.parse_dotenv_file(tmp_path / "nope.env")


# --------------------------------------------------------------------------- #
# parse_image_parts
# --------------------------------------------------------------------------- #
def test_parse_image_strips_registry():
    name, tag = g.parse_image_parts("123.dkr.ecr.us-east-1.amazonaws.com/my-app", "latest")
    assert name == "my-app"
    assert tag == "latest"


def test_parse_image_extracts_embedded_tag():
    name, tag = g.parse_image_parts("my-app:v2", "")
    assert name == "my-app"
    assert tag == "v2"


def test_parse_image_explicit_tag_wins():
    name, tag = g.parse_image_parts("my-app:v2", "v3")
    assert name == "my-app"
    assert tag == "v3"


# --------------------------------------------------------------------------- #
# build_secrets_from_config
# --------------------------------------------------------------------------- #
def test_build_secrets_legacy_format():
    secrets = g.SecretManager.build_secrets_from_config(
        {"secrets": [{"DB_PASS": "arn:aws:secretsmanager:...:secret:db"}]}
    )
    assert secrets == [{"name": "DB_PASS", "valueFrom": "arn:aws:secretsmanager:...:secret:db:DB_PASS::"}]


def test_build_secrets_new_id_values_format():
    secrets = g.SecretManager.build_secrets_from_config(
        {"secrets_envs": [{"id": "arn:secret:app", "values": ["A", "B"]}]}
    )
    assert secrets == [
        {"name": "A", "valueFrom": "arn:secret:app:A::"},
        {"name": "B", "valueFrom": "arn:secret:app:B::"},
    ]


def test_name_only_secret_fails_loudly_without_mock(monkeypatch):
    # No opt-in flag: a name-only lookup that can't reach AWS must raise, not
    # silently emit fabricated ARNs.
    monkeypatch.delenv("ECS_DEPLOY_ALLOW_MOCK_SECRETS", raising=False)

    def boom(secret_name):
        raise RuntimeError("no network")

    monkeypatch.setattr(
        g.SecretManager, "discover_secret_keys", staticmethod(boom)
    )
    with pytest.raises((g.ValidationError, RuntimeError)):
        g.SecretManager.build_secrets_from_config(
            {"secrets_envs": [{"name": "database-credentials"}]}
        )


def test_mock_allowed_toggle(monkeypatch):
    monkeypatch.setenv("ECS_DEPLOY_ALLOW_MOCK_SECRETS", "1")
    assert g.SecretManager._mock_allowed() is True
    monkeypatch.setenv("ECS_DEPLOY_ALLOW_MOCK_SECRETS", "false")
    assert g.SecretManager._mock_allowed() is False
    monkeypatch.delenv("ECS_DEPLOY_ALLOW_MOCK_SECRETS", raising=False)
    assert g.SecretManager._mock_allowed() is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
