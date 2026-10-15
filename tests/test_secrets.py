#!/usr/bin/env python3
"""
Unit tests for `secrets_envs` entries with `env:` - a whole secret as one
environment variable - in generate_task_def.py, and the same entry through the
Batch generator.

boto3 is stubbed via sys.modules for the `name` lookup (DescribeSecret), so no
AWS credentials are needed; botocore is real, for honest ClientError shapes.
"""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

from botocore.exceptions import ClientError

ROOT = Path(__file__).parent.parent
SM = "arn:aws:secretsmanager:us-east-1:123456789012:secret"
WHOLE_ARN = f"{SM}:stripe-api-key-AbCdEf"
JSON_ARN = f"{SM}:app-secrets-abc123"


def load(name):
    script = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gtd = load("generate_task_def")
gjd = load("generate_job_def")


class FakeSecretsManager:
    def __init__(self, arns=None, error=None):
        self.arns = arns or {}
        self.error = error
        self.calls = []

    def describe_secret(self, SecretId):
        self.calls.append(SecretId)
        if self.error:
            raise ClientError({'Error': {'Code': self.error, 'Message': 'x'}}, 'DescribeSecret')
        if SecretId not in self.arns:
            raise ClientError({'Error': {'Code': 'ResourceNotFoundException', 'Message': 'x'}},
                              'DescribeSecret')
        return {'ARN': self.arns[SecretId]}

    def get_secret_value(self, SecretId):
        raise AssertionError("an env: entry must never read the secret value")


class FakeBoto3:
    def __init__(self, client):
        self._client = client

    def Session(self):
        return self

    def client(self, name, region_name=None):
        assert name == 'secretsmanager', f"unexpected client requested: {name}"
        return self._client


@contextlib.contextmanager
def patched_boto3(client):
    saved = sys.modules.get('boto3')
    sys.modules['boto3'] = FakeBoto3(client)
    try:
        yield client
    finally:
        if saved is None:
            sys.modules.pop('boto3', None)
        else:
            sys.modules['boto3'] = saved


def build(config):
    return gtd.SecretManager.build_secrets_from_config(config)


def expect_error(fn, error_cls, *fragments):
    try:
        fn()
    except error_cls as e:
        missing = [f for f in fragments if f not in str(e)]
        if missing:
            return False, f"message missing {missing}; got: {e}"
        return True, ""
    except Exception as e:  # noqa: BLE001 - reporting an unexpected type is the point
        return False, f"expected {error_cls.__name__}, got {type(e).__name__}: {e}"
    return False, f"expected {error_cls.__name__}, but nothing was raised"


# --------------------------------------------------------------------------

def test_id_whole_secret_no_suffix():
    with patched_boto3(FakeSecretsManager()) as sm:
        result = build({'secrets_envs': [{'id': WHOLE_ARN, 'env': 'STRIPE_API_KEY'}]})
    ok = result == [{"name": "STRIPE_API_KEY", "valueFrom": WHOLE_ARN}] and not sm.calls
    return ok, f"{result} calls={sm.calls}"


def test_name_resolved_with_describe_only():
    with patched_boto3(FakeSecretsManager({'license-key': f"{SM}:license-key-XyZ123"})) as sm:
        result = build({'secrets_envs': [{'name': 'license-key', 'env': 'LICENSE_KEY'}]})
    ok = (result == [{"name": "LICENSE_KEY", "valueFrom": f"{SM}:license-key-XyZ123"}]
          and sm.calls == ['license-key'])
    return ok, f"{result} calls={sm.calls}"


def test_mixed_with_json_entry_keeps_order():
    with patched_boto3(FakeSecretsManager()):
        result = build({'secrets_envs': [
            {'id': JSON_ARN, 'values': ['DATABASE_URL']},
            {'id': WHOLE_ARN, 'env': 'STRIPE_API_KEY'},
        ]})
    ok = result == [{"name": "DATABASE_URL", "valueFrom": f"{JSON_ARN}:DATABASE_URL::"},
                    {"name": "STRIPE_API_KEY", "valueFrom": WHOLE_ARN}]
    return ok, json.dumps(result)


def test_existing_shapes_unchanged():
    # The two formats that existed before must render exactly as on main.
    legacy = build({'secrets': [{'DB': JSON_ARN}]})
    grouped = build({'secrets_envs': [{'id': JSON_ARN, 'values': ['A', 'B']}]})
    ok = (legacy == [{"name": "DB", "valueFrom": f"{JSON_ARN}:DB::"}]
          and grouped == [{"name": "A", "valueFrom": f"{JSON_ARN}:A::"},
                          {"name": "B", "valueFrom": f"{JSON_ARN}:B::"}])
    return ok, ""


def test_name_not_found_fails():
    with patched_boto3(FakeSecretsManager()):
        return expect_error(
            lambda: build({'secrets_envs': [{'name': 'missing', 'env': 'X'}]}),
            gtd.SecretResolutionError, "'missing'", "does not exist")


def test_name_access_denied_fails():
    with patched_boto3(FakeSecretsManager(error='AccessDeniedException')):
        return expect_error(
            lambda: build({'secrets_envs': [{'name': 'x', 'env': 'X'}]}),
            gtd.SecretResolutionError, "secretsmanager:DescribeSecret")


def test_resolution_error_is_validation_error():
    return issubclass(gtd.SecretResolutionError, gtd.ValidationError), ""


def test_env_with_values_rejected():
    return expect_error(
        lambda: build({'secrets_envs': [{'id': WHOLE_ARN, 'env': 'X', 'values': ['A']}]}),
        gtd.ValidationError, "not both")


def test_env_needs_exactly_one_source():
    both = expect_error(
        lambda: build({'secrets_envs': [{'id': WHOLE_ARN, 'name': 'x', 'env': 'X'}]}),
        gtd.ValidationError, "exactly one")
    neither = expect_error(
        lambda: build({'secrets_envs': [{'env': 'X'}]}), gtd.ValidationError, "exactly one")
    return both[0] and neither[0], both[1] or neither[1]


def test_bad_env_name_rejected():
    return expect_error(
        lambda: build({'secrets_envs': [{'id': WHOLE_ARN, 'env': '1BAD-NAME'}]}),
        gtd.ValidationError, "environment variable name")


def test_duplicate_with_envs_rejected():
    return expect_error(
        lambda: build({'envs': [{'STRIPE_API_KEY': 'x'}],
                       'secrets_envs': [{'id': WHOLE_ARN, 'env': 'STRIPE_API_KEY'}]}),
        gtd.ValidationError, "already set by envs")


def test_duplicate_with_json_key_after_rejected():
    return expect_error(
        lambda: build({'secrets_envs': [{'id': WHOLE_ARN, 'env': 'A'},
                                        {'id': JSON_ARN, 'values': ['A']}]}),
        gtd.ValidationError, "also set by another")


def test_duplicate_env_entries_rejected():
    return expect_error(
        lambda: build({'secrets_envs': [{'id': WHOLE_ARN, 'env': 'A'},
                                        {'id': JSON_ARN, 'env': 'A'}]}),
        gtd.ValidationError, "already set by another")


def test_ignored_with_legacy_secrets_fails():
    return expect_error(
        lambda: build({'secrets': [{'DB': JSON_ARN}],
                       'secrets_envs': [{'id': WHOLE_ARN, 'env': 'X'}]}),
        gtd.ValidationError, "ignored when 'secrets'")


def test_legacy_secrets_with_old_secrets_envs_still_ignored():
    # Long-standing behaviour kept as is: no `env` entry, no new error.
    result = build({'secrets': [{'DB': JSON_ARN}],
                    'secrets_envs': [{'id': JSON_ARN, 'values': ['A']}]})
    return result == [{"name": "DB", "valueFrom": f"{JSON_ARN}:DB::"}], json.dumps(result)


def test_sidecar_gets_whole_secret():
    config = {'role_arn': 'arn:aws:iam::123456789012:role/app',
              'sidecars': [{'name': 'agent', 'image': 'busybox',
                            'secrets_envs': [{'id': WHOLE_ARN, 'env': 'AGENT_TOKEN'}]}]}
    td = gtd.generate_task_definition(config_dict=config, cluster_name='c', aws_region='us-east-1',
                                      registry='r', container_registry='r', image_name='app',
                                      tag='1', service_name='svc')
    agent = next(c for c in td['containerDefinitions'] if c['name'] == 'agent')
    return agent['secrets'] == [{"name": "AGENT_TOKEN", "valueFrom": WHOLE_ARN}], json.dumps(agent['secrets'])


def test_batch_job_gets_whole_secret():
    config = {'role_arn': 'arn:aws:iam::123456789012:role/app',
              'secrets_envs': [{'id': WHOLE_ARN, 'env': 'STRIPE_API_KEY'}]}
    job = gjd.generate_job_definition(config, 'job', 'us-east-1', '', 'img', '1')
    secrets = job['containerProperties']['secrets']
    return secrets == [{"name": "STRIPE_API_KEY", "valueFrom": WHOLE_ARN}], json.dumps(secrets)


def test_overrides_extend_secrets_envs():
    merged = gtd.merge_configs(
        {'secrets_envs': [{'id': JSON_ARN, 'values': ['A']}]},
        {'secrets_envs': [{'id': WHOLE_ARN, 'env': 'B'}]})
    return len(merged['secrets_envs']) == 2, json.dumps(merged)


TESTS = [
    ("env: id used as-is, no suffix, no AWS call", test_id_whole_secret_no_suffix),
    ("env: name resolved via DescribeSecret only", test_name_resolved_with_describe_only),
    ("env: mixed with a JSON entry, order kept", test_mixed_with_json_entry_keeps_order),
    ("existing secrets / secrets_envs shapes unchanged", test_existing_shapes_unchanged),
    ("env: name not found fails", test_name_not_found_fails),
    ("env: name access denied fails", test_name_access_denied_fails),
    ("SecretResolutionError is a ValidationError", test_resolution_error_is_validation_error),
    ("reject: env with values", test_env_with_values_rejected),
    ("reject: id and name / neither", test_env_needs_exactly_one_source),
    ("reject: bad variable name", test_bad_env_name_rejected),
    ("reject: duplicate of an envs variable", test_duplicate_with_envs_rejected),
    ("reject: duplicate of a later JSON key", test_duplicate_with_json_key_after_rejected),
    ("reject: two env entries, same name", test_duplicate_env_entries_rejected),
    ("reject: env entry next to legacy secrets", test_ignored_with_legacy_secrets_fails),
    ("legacy secrets still ignore old secrets_envs", test_legacy_secrets_with_old_secrets_envs_still_ignored),
    ("sidecar: whole secret", test_sidecar_gets_whole_secret),
    ("batch: whole secret", test_batch_job_gets_whole_secret),
    ("services_overrides extend secrets_envs", test_overrides_extend_secrets_envs),
]


def main():
    print("=" * 60)
    print("WHOLE-SECRET (secrets_envs env:) TESTS")
    print("=" * 60)
    failed = []
    for name, test in TESTS:
        try:
            passed, detail = test()
        except Exception as e:  # noqa: BLE001
            passed, detail = False, f"{type(e).__name__}: {e}"
        if passed:
            print(f"✅ PASSED: {name}")
        else:
            print(f"❌ FAILED: {name} - {detail}")
            failed.append(name)
    print("=" * 60)
    print(f"Total: {len(TESTS)}  Failed: {len(failed)}")
    if failed:
        print("\nFailed tests:")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("\n🎉 All whole-secret tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
