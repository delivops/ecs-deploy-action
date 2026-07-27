#!/usr/bin/env python3
"""
Unit tests for role ARN resolution (RoleResolver) in generate_task_def.py.

These cover what the golden-file runner in test.py cannot: the SSM Parameter
Store fallback and its error taxonomy. boto3 is stubbed via sys.modules so the
suite needs no AWS credentials, which is also how it runs in CI.

botocore is deliberately NOT stubbed - it ships with boto3, so the fake client
can raise the real exception classes and the taxonomy assertions stay honest.
"""

import contextlib
import importlib.util
import sys
from pathlib import Path

from botocore.exceptions import (
    ClientError, NoCredentialsError, TokenRetrievalError, NoRegionError,
    EndpointConnectionError,
)

# Whether the real boto3 was already loaded before any test ran, so the
# "never touched boto3" assertion stays valid however this file is invoked.
BOTO3_PRELOADED = 'boto3' in sys.modules

CLUSTER = "test-cluster"
SERVICE = "test-service"
REGION = "us-east-1"

TASK_PARAM = f"/ecs/{CLUSTER}/{SERVICE}/task-role"
EXEC_PARAM = f"/ecs/{CLUSTER}/{SERVICE}/execution-role"

TASK_ARN = "arn:aws:iam::123456789012:role/test-cluster_test-service"
EXEC_ARN = "arn:aws:iam::123456789012:role/test-cluster_test-service_execution"
SHARED_ARN = "arn:aws:iam::123456789012:role/ecsTaskExecutionRole"


def load_module():
    """Import generate_task_def.py by path.

    Safe to import: module-level code only calls setup_logging(), and main() is
    guarded by __name__.
    """
    script = Path(__file__).parent.parent / "scripts" / "generate_task_def.py"
    spec = importlib.util.spec_from_file_location("generate_task_def", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gtd = load_module()


class FakeSSM:
    """Minimal stand-in for an SSM client, recording every call."""

    def __init__(self, parameters=None, invalid=None, raises=None):
        self.parameters = parameters or {}
        self.invalid = invalid or []
        self.raises = raises
        self.calls = []

    def get_parameters(self, Names, WithDecryption=False):
        self.calls.append(list(Names))
        if self.raises:
            raise self.raises
        returned = [{'Name': n, 'Value': v} for n, v in self.parameters.items() if n in Names]
        missing = [n for n in Names if n not in self.parameters]
        return {'Parameters': returned, 'InvalidParameters': self.invalid + missing}


class FakeBoto3:
    """Stands in for the boto3 module: boto3.Session().client('ssm', ...)."""

    def __init__(self, ssm):
        self._ssm = ssm

    def Session(self):
        return self

    def client(self, name, region_name=None):
        assert name == 'ssm', f"unexpected client requested: {name}"
        return self._ssm


@contextlib.contextmanager
def patched_boto3(ssm):
    """Inject the stub. RoleResolver imports boto3 lazily, so this suffices."""
    saved = sys.modules.get('boto3')
    sys.modules['boto3'] = FakeBoto3(ssm)
    try:
        yield ssm
    finally:
        if saved is None:
            sys.modules.pop('boto3', None)
        else:
            sys.modules['boto3'] = saved


def resolver():
    return gtd.RoleResolver(CLUSTER, SERVICE, REGION)


def expect_error(fn, error_cls, *fragments):
    """Assert fn() raises error_cls and the message mentions every fragment."""
    try:
        fn()
    except error_cls as e:
        message = str(e)
        missing = [f for f in fragments if f not in message]
        if missing:
            return False, f"message missing {missing}; got: {message}"
        return True, ""
    except Exception as e:  # noqa: BLE001 - reporting an unexpected type is the point
        return False, f"expected {error_cls.__name__}, got {type(e).__name__}: {e}"
    return False, f"expected {error_cls.__name__}, but no exception was raised"


# --------------------------------------------------------------------------
# Test cases. Each returns (passed, detail).
# --------------------------------------------------------------------------

def test_both_from_ssm():
    ssm = FakeSSM({TASK_PARAM: TASK_ARN, EXEC_PARAM: EXEC_ARN})
    with patched_boto3(ssm):
        result = resolver().resolve({})
    if result != {'taskRoleArn': TASK_ARN, 'executionRoleArn': EXEC_ARN}:
        return False, f"unexpected result: {result}"
    if len(ssm.calls) != 1:
        return False, f"expected exactly 1 SSM call, got {len(ssm.calls)}: {ssm.calls}"
    if sorted(ssm.calls[0]) != sorted([TASK_PARAM, EXEC_PARAM]):
        return False, f"unexpected parameter names: {ssm.calls[0]}"
    return True, "both slots resolved in a single batched call"


def test_role_arn_skips_ssm():
    ssm = FakeSSM()
    with patched_boto3(ssm):
        result = resolver().resolve({'role_arn': SHARED_ARN})
    if result != {'taskRoleArn': SHARED_ARN, 'executionRoleArn': SHARED_ARN}:
        return False, f"unexpected result: {result}"
    if ssm.calls:
        return False, f"SSM must not be called when role_arn is set, got {ssm.calls}"
    return True, "role_arn short-circuits SSM entirely"


def test_per_slot_beats_shared():
    ssm = FakeSSM()
    with patched_boto3(ssm):
        result = resolver().resolve({'role_arn': SHARED_ARN, 'task_role_arn': TASK_ARN})
    if result != {'taskRoleArn': TASK_ARN, 'executionRoleArn': SHARED_ARN}:
        return False, f"unexpected result: {result}"
    if ssm.calls:
        return False, f"SSM must not be called, got {ssm.calls}"
    return True, "per-slot key wins, shared key fills the other slot"


def test_only_pending_slot_requested():
    ssm = FakeSSM({TASK_PARAM: TASK_ARN})
    with patched_boto3(ssm):
        result = resolver().resolve({'execution_role_arn': EXEC_ARN})
    if result != {'taskRoleArn': TASK_ARN, 'executionRoleArn': EXEC_ARN}:
        return False, f"unexpected result: {result}"
    if ssm.calls != [[TASK_PARAM]]:
        return False, f"expected only the task-role parameter, got {ssm.calls}"
    return True, "only the unresolved slot is fetched"


def test_missing_parameter_fails():
    ssm = FakeSSM({EXEC_PARAM: EXEC_ARN})
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            TASK_PARAM, 'task_role_arn', 'ssm:GetParameters', 'none',
        )


def test_both_missing_reported_together():
    ssm = FakeSSM()
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            TASK_PARAM, EXEC_PARAM,
        )


def test_no_credentials_fails_without_mock():
    ssm = FakeSSM(raises=NoCredentialsError())
    with patched_boto3(ssm):
        try:
            result = resolver().resolve({})
        except gtd.RoleResolutionError as e:
            message = str(e)
            if 'credentials' not in message.lower():
                return False, f"message should mention credentials: {message}"
            if 'arn:aws:iam::123456789012' in message:
                return False, "message leaked a mock ARN"
            return True, "hard failure, no mock ARN substituted"
        return False, f"expected RoleResolutionError, got a result: {result}"


def test_access_denied():
    error = ClientError(
        {'Error': {'Code': 'AccessDeniedException', 'Message': 'denied'}}, 'GetParameters')
    ssm = FakeSSM(raises=error)
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            'ssm:GetParameters', f'parameter/ecs/{CLUSTER}/{SERVICE}/*',
        )


def test_throttling():
    error = ClientError(
        {'Error': {'Code': 'ThrottlingException', 'Message': 'slow down'}}, 'GetParameters')
    ssm = FakeSSM(raises=error)
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            'ThrottlingException', 'Retry',
        )


def test_expired_token():
    ssm = FakeSSM(raises=TokenRetrievalError(provider='sts', error_msg='expired'))
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            'credentials',
        )


def test_no_region():
    ssm = FakeSSM(raises=NoRegionError())
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            'region',
        )


def test_endpoint_unreachable():
    ssm = FakeSSM(raises=EndpointConnectionError(endpoint_url='https://ssm.example'))
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            'SSM endpoint',
        )


def test_opt_out_omits_key():
    ssm = FakeSSM({EXEC_PARAM: EXEC_ARN})
    with patched_boto3(ssm):
        result = resolver().resolve({'task_role_arn': 'none'})
    if 'taskRoleArn' in result:
        return False, f"taskRoleArn should be absent, got {result}"
    if result != {'executionRoleArn': EXEC_ARN}:
        return False, f"unexpected result: {result}"
    if ssm.calls != [[EXEC_PARAM]]:
        return False, f"opted-out slot must not be fetched, got {ssm.calls}"
    return True, "'none' omits the key and skips its SSM lookup"


def test_opt_out_is_case_insensitive():
    ssm = FakeSSM()
    with patched_boto3(ssm):
        result = resolver().resolve({'role_arn': 'None'})
    if result != {}:
        return False, f"expected no role keys, got {result}"
    return True, "'None' is accepted as the sentinel"


def test_both_opted_out_stays_offline():
    # Deliberately unpatched: if the resolver reached for SSM here it would
    # import the real boto3, which this assertion would catch.
    result = resolver().resolve({'role_arn': 'none'})
    if result != {}:
        return False, f"expected no role keys, got {result}"
    if not BOTO3_PRELOADED and 'boto3' in sys.modules:
        return False, "boto3 was imported despite both slots being opted out"
    return True, "fully offline when both slots opt out"


def test_key_order_is_stable():
    ssm = FakeSSM({TASK_PARAM: TASK_ARN})
    with patched_boto3(ssm):
        result = resolver().resolve({'execution_role_arn': EXEC_ARN})
    if list(result) != ['taskRoleArn', 'executionRoleArn']:
        return False, f"unexpected key order: {list(result)}"
    return True, "key order is stable regardless of which source won"


def test_bare_role_name_from_ssm_rejected():
    ssm = FakeSSM({TASK_PARAM: 'test-cluster_test-service', EXEC_PARAM: EXEC_ARN})
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            f'SSM parameter {TASK_PARAM}', 'not an IAM role ARN',
        )


def test_empty_ssm_value_treated_as_missing():
    ssm = FakeSSM({TASK_PARAM: '   ', EXEC_PARAM: EXEC_ARN})
    with patched_boto3(ssm):
        return expect_error(
            lambda: resolver().resolve({}),
            gtd.RoleResolutionError,
            'Could not determine taskRoleArn',
        )


def test_bare_role_name_in_yaml_rejected():
    return expect_error(
        lambda: gtd.validate_config({'task_role_arn': 'my-role'}),
        gtd.ValidationError,
        'not an IAM role ARN',
    )


def test_yaml_boolean_trap():
    # `task_role_arn: no` parses as the boolean False under YAML 1.1.
    return expect_error(
        lambda: gtd.validate_config({'task_role_arn': False}),
        gtd.ValidationError,
        'boolean', 'none',
    )


def test_empty_string_falls_through():
    ssm = FakeSSM({TASK_PARAM: TASK_ARN, EXEC_PARAM: EXEC_ARN})
    with patched_boto3(ssm):
        result = resolver().resolve({'role_arn': '  '})
    if result != {'taskRoleArn': TASK_ARN, 'executionRoleArn': EXEC_ARN}:
        return False, f"unexpected result: {result}"
    return True, "an empty role_arn falls through to SSM instead of emitting ''"


def test_validate_config_accepts_opt_out():
    try:
        gtd.validate_config({'task_role_arn': 'none', 'execution_role_arn': SHARED_ARN})
    except Exception as e:  # noqa: BLE001
        return False, f"unexpected error: {type(e).__name__}: {e}"
    return True, "'none' passes offline validation"


TESTS = [
    ("both slots from SSM in one call", test_both_from_ssm),
    ("role_arn skips SSM", test_role_arn_skips_ssm),
    ("per-slot key beats shared role_arn", test_per_slot_beats_shared),
    ("only the unresolved slot is fetched", test_only_pending_slot_requested),
    ("missing SSM parameter fails hard", test_missing_parameter_fails),
    ("both missing slots reported together", test_both_missing_reported_together),
    ("no credentials fails without mock fallback", test_no_credentials_fails_without_mock),
    ("access denied names ssm:GetParameters", test_access_denied),
    ("throttling suggests a retry", test_throttling),
    ("expired token reported as credentials", test_expired_token),
    ("missing region reported", test_no_region),
    ("unreachable endpoint reported", test_endpoint_unreachable),
    ("'none' omits the key", test_opt_out_omits_key),
    ("'none' is case-insensitive", test_opt_out_is_case_insensitive),
    ("both opted out stays offline", test_both_opted_out_stays_offline),
    ("key order is stable", test_key_order_is_stable),
    ("bare role name from SSM rejected", test_bare_role_name_from_ssm_rejected),
    ("empty SSM value treated as missing", test_empty_ssm_value_treated_as_missing),
    ("bare role name in YAML rejected", test_bare_role_name_in_yaml_rejected),
    ("YAML boolean trap explained", test_yaml_boolean_trap),
    ("empty role_arn falls through to SSM", test_empty_string_falls_through),
    ("validate_config accepts 'none'", test_validate_config_accepts_opt_out),
]


def main():
    print("=" * 60)
    print("ROLE RESOLUTION TESTS")
    print("=" * 60)

    failed = []
    for name, test in TESTS:
        try:
            passed, detail = test()
        except Exception as e:  # noqa: BLE001 - a crashing test is a failing test
            passed, detail = False, f"{type(e).__name__}: {e}"

        if passed:
            print(f"✅ PASSED: {name}" + (f" - {detail}" if detail else ""))
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
    print("\n🎉 All role resolution tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
