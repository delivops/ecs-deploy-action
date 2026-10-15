#!/usr/bin/env python3
"""
Tests for deployment_type=batch_job_definition (scripts/generate_job_def.py).

Two parts:
  * goldens: every examples/batch/*.yaml is rendered and compared with
    tests/expected_outputs/batch/<name>.json. As in test.py, a missing golden is
    a failure; run locally with UPDATE_EXPECTED=1 to create one, then review it.
  * unit tests for the YAML -> job definition mapping and its validation.

boto3 is stubbed via sys.modules where SSM is involved, so no AWS credentials
are needed (botocore ships with boto3 and is used for real).
"""

import contextlib
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
EXAMPLES = ROOT / "examples" / "batch"
EXPECTED = Path(__file__).parent / "expected_outputs" / "batch"

CLUSTER = "test-cluster"
REGION = "us-east-1"
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
IMAGE = "test-app"
TAG = "latest"

JOB_ARN = "arn:aws:iam::123456789012:role/job"
EXEC_ARN = "arn:aws:iam::123456789012:role/execution"
SSM_SECRET = "arn:aws:ssm:us-east-1:123456789012:parameter/app/token"
SM_SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:app"


def load_module():
    script = ROOT / "scripts" / "generate_job_def.py"
    spec = importlib.util.spec_from_file_location("generate_job_def", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gjd = load_module()
gtd = gjd.gtd


class FakeSSM:
    def __init__(self, parameters):
        self.parameters = parameters
        self.calls = []

    def get_parameters(self, Names, WithDecryption=False):
        self.calls.append(list(Names))
        return {
            'Parameters': [{'Name': n, 'Value': v} for n, v in self.parameters.items() if n in Names],
            'InvalidParameters': [n for n in Names if n not in self.parameters],
        }


class FakeBoto3:
    def __init__(self, ssm):
        self._ssm = ssm

    def Session(self):
        return self

    def client(self, name, region_name=None):
        assert name == 'ssm', f"unexpected client requested: {name}"
        return self._ssm


@contextlib.contextmanager
def patched_boto3(ssm):
    saved = sys.modules.get('boto3')
    sys.modules['boto3'] = FakeBoto3(ssm)
    try:
        yield ssm
    finally:
        if saved is None:
            sys.modules.pop('boto3', None)
        else:
            sys.modules['boto3'] = saved


@contextlib.contextmanager
def captured_warnings():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)


def render(config, name="job", cluster=None, registry=REGISTRY):
    return gjd.generate_job_definition(config, name, cluster, REGION, registry, IMAGE, TAG)


def base(**extra):
    """A minimal valid config with both roles in YAML, so nothing hits SSM."""
    config = {'task_role_arn': JOB_ARN, 'execution_role_arn': EXEC_ARN}
    config.update(extra)
    return config


def load_yaml_text(text, name="job"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "task.yaml"
        path.write_text(text)
        return gjd.load_batch_config(str(path), name)


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


def container(job):
    return job["containerProperties"]


# --------------------------------------------------------------------------
# Goldens
# --------------------------------------------------------------------------

# Job definition name per example: the base name, or the services_overrides
# entry the example exists to show.
GOLDEN_NAMES = {"batch-fargate": "nightly-report"}


def golden_tests():
    tests = []
    for yaml_file in sorted(EXAMPLES.glob("*.yaml")):
        stem = yaml_file.stem

        def run(yaml_file=yaml_file, stem=stem):
            name = GOLDEN_NAMES.get(stem, stem)
            config = gjd.load_batch_config(str(yaml_file), name)
            actual = render(config, name=name, cluster=CLUSTER)
            expected_path = EXPECTED / f"{stem}.json"
            if not expected_path.exists():
                if os.environ.get('UPDATE_EXPECTED') == '1':
                    EXPECTED.mkdir(parents=True, exist_ok=True)
                    expected_path.write_text(json.dumps(actual, indent=2) + "\n")
                    return True, f"created {expected_path.name} - review it"
                return False, f"missing {expected_path}; run with UPDATE_EXPECTED=1"
            expected = json.loads(expected_path.read_text())
            if actual != expected:
                return False, "differs:\n" + json.dumps(actual, indent=2)
            return True, ""

        tests.append((f"golden: {stem}", run))
    return tests


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------

def test_defaults():
    job = render(base())
    c = container(job)
    ok = (job["type"] == "container" and job["platformCapabilities"] == ["EC2"]
          and job["jobDefinitionName"] == "job"
          and job["retryStrategy"] == {"attempts": 1} and job["propagateTags"] is True
          and c["resourceRequirements"] == [{"type": "VCPU", "value": "1"},
                                            {"type": "MEMORY", "value": "2048"}]
          and c["image"] == f"{REGISTRY}/{IMAGE}:{TAG}"
          and "timeout" not in job and "parameters" not in job
          and "command" not in c and "environment" not in c and "secrets" not in c
          and "runtimePlatform" not in c)
    return ok, json.dumps(job)


def test_image_without_registry():
    c = container(render(base(), registry=""))
    return c["image"] == f"{IMAGE}:{TAG}", c["image"]


def test_fractional_vcpus_on_fargate():
    c = container(render(base(platform="FARGATE", cpu=0.25, memory=512)))
    ok = (c["resourceRequirements"][0] == {"type": "VCPU", "value": "0.25"}
          and c["runtimePlatform"] == {"operatingSystemFamily": "LINUX", "cpuArchitecture": "X86_64"})
    return ok, json.dumps(c)


def test_platform_lowercase_accepted():
    job = render(base(platform="fargate", cpu=1, memory=2048))
    return job["platformCapabilities"] == ["FARGATE"], ""


def test_fargate_ephemeral_storage():
    c = container(render(base(platform="FARGATE", cpu=1, memory=2048, ephemeral_storage=50)))
    return c.get("ephemeralStorage") == {"sizeInGiB": 50}, json.dumps(c)


def test_command_and_envs():
    c = container(render(base(command=["run", "--n", 3], envs=[{"A": "1"}, {"B": True}])))
    ok = (c["command"] == ["run", "--n", "3"]
          and c["environment"] == [{"name": "A", "value": "1"}, {"name": "B", "value": "True"}])
    return ok, json.dumps(c)


def test_batch_only_keys():
    job = render(base(timeout_seconds=600, retry_attempts=3, propagate_tags=False,
                      parameters={"date": "today", "dry": True, "n": 5}))
    ok = (job["timeout"] == {"attemptDurationSeconds": 600}
          and job["retryStrategy"] == {"attempts": 3}
          and job["propagateTags"] is False
          and job["parameters"] == {"date": "today", "dry": "true", "n": "5"})
    return ok, json.dumps(job)


def test_log_group_with_cluster():
    log = container(render(base(), cluster=CLUSTER))["logConfiguration"]
    ok = log == {"logDriver": "awslogs", "options": {
        "awslogs-group": f"/ecs/{CLUSTER}/job", "awslogs-region": REGION,
        "awslogs-stream-prefix": "batch"}}
    return ok, json.dumps(log)


def test_no_log_configuration_without_cluster():
    c = container(render(base()))
    return "logConfiguration" not in c, ""


def test_ignored_keys_warn():
    config = base(port=8080, health_check={"command": "true"}, replica_count=2,
                  sidecars=[{"name": "x", "image": "y"}], launch_type="FARGATE")
    with captured_warnings() as stream:
        job = render(config)
    text = stream.getvalue()
    missing = [k for k in ("port", "health_check", "replica_count", "sidecars", "launch_type")
               if f"'{k}' does not apply" not in text]
    blob = json.dumps(job)
    leaked = [k for k in ("portMappings", "healthCheck", "8080") if k in blob]
    return not missing and not leaked, f"missing warnings {missing}, leaked {leaked}"


def test_overrides_apply_by_task_name():
    config = load_yaml_text(
        "cpu: 256\nmemory: 512\nservices_overrides:\n  job:\n    cpu: 2\n    memory: 4096\n"
        f"role_arn: {JOB_ARN}\n")
    c = container(render(config))
    ok = c["resourceRequirements"] == [{"type": "VCPU", "value": "2"},
                                       {"type": "MEMORY", "value": "4096"}]
    return ok, json.dumps(c)


def test_shared_yaml_sidecars_not_validated_as_ecs():
    # On ECS this sidecar budget (128 of 1 CPU unit) would be rejected. For Batch
    # the sidecar is ignored, so the YAML must load.
    config = load_yaml_text(
        f"cpu: 1\nmemory: 2048\nrole_arn: {JOB_ARN}\n"
        "sidecars:\n  - name: helper\n    image: busybox\n    cpu: 128\n")
    return "sidecars" in config, ""


# --------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------

def test_entrypoint_rejected():
    return expect_error(lambda: render(base(entrypoint=["/bin/sh", "-c"])),
                        gtd.ValidationError, "entryPoint", "command")


def test_entrypoint_rejected_on_load():
    return expect_error(lambda: load_yaml_text('entrypoint: ["/bin/sh"]\n'),
                        gtd.ValidationError, "entryPoint")


def test_secret_files_rejected():
    return expect_error(lambda: render(base(secret_files=["cert"])),
                        gtd.ValidationError, "secret_files")


def test_ecs_cpu_units_rejected():
    return expect_error(lambda: render(base(cpu=1024)), gtd.ValidationError,
                        "vCPUs", "Did you mean 1?")


def test_fractional_on_ec2_rejected():
    return expect_error(lambda: render(base(cpu=0.5)), gtd.ValidationError, "FARGATE")


def test_bad_fargate_combo_rejected():
    return expect_error(lambda: render(base(platform="FARGATE", cpu=0.25, memory=4096)),
                        gtd.ValidationError, "Invalid memory 4096")


def test_bad_fargate_vcpu_rejected():
    return expect_error(lambda: render(base(platform="FARGATE", cpu=3, memory=8192)),
                        gtd.ValidationError, "Invalid cpu 3")


def test_bad_platform_rejected():
    return expect_error(lambda: render(base(platform="EKS")), gtd.ValidationError, "platform")


def test_timeout_minimum():
    return expect_error(lambda: render(base(timeout_seconds=30)), gtd.ValidationError, "60")


def test_retry_attempts_range():
    return expect_error(lambda: render(base(retry_attempts=11)), gtd.ValidationError, "1 to 10")


def test_bool_keys_must_be_bool():
    return expect_error(lambda: render(base(deregister_previous="yes")),
                        gtd.ValidationError, "deregister_previous")


def test_parameters_must_be_mapping():
    return expect_error(lambda: render(base(parameters=["a"])), gtd.ValidationError, "mapping")


def test_parameter_name_checked():
    return expect_error(lambda: render(base(parameters={"a b": "x"})), gtd.ValidationError,
                        "parameter name")


def test_ephemeral_storage_ec2_rejected():
    return expect_error(lambda: render(base(ephemeral_storage=50)), gtd.ValidationError, "FARGATE")


def test_bad_job_name_rejected():
    return expect_error(lambda: render(base(), name="bad name"), gtd.ValidationError, "task_name")


# --------------------------------------------------------------------------
# Secrets: all three existing formats plus SSM parameter ARNs
# --------------------------------------------------------------------------

def test_secrets_legacy_with_ssm():
    c = container(render(base(secrets=[{"DB": SM_SECRET}, {"TOKEN": SSM_SECRET}])))
    ok = c["secrets"] == [{"name": "DB", "valueFrom": f"{SM_SECRET}:DB::"},
                          {"name": "TOKEN", "valueFrom": SSM_SECRET}]
    return ok, json.dumps(c["secrets"])


def test_secrets_envs_id_values():
    c = container(render(base(secrets_envs=[{"id": SM_SECRET, "values": ["A", "B"]}])))
    ok = c["secrets"] == [{"name": "A", "valueFrom": f"{SM_SECRET}:A::"},
                          {"name": "B", "valueFrom": f"{SM_SECRET}:B::"}]
    return ok, json.dumps(c["secrets"])


def test_secrets_envs_name_only():
    saved = gtd.SecretManager.discover_secret_keys
    gtd.SecretManager.discover_secret_keys = staticmethod(lambda name: (["K1", "K2"], f"{SM_SECRET}-x1"))
    try:
        c = container(render(base(secrets_envs=[{"name": "app"}])))
    finally:
        gtd.SecretManager.discover_secret_keys = saved
    ok = c["secrets"] == [{"name": "K1", "valueFrom": f"{SM_SECRET}-x1:K1::"},
                          {"name": "K2", "valueFrom": f"{SM_SECRET}-x1:K2::"}]
    return ok, json.dumps(c["secrets"])


def test_ssm_arn_shape_is_strict():
    # Not an SSM parameter ARN (no account id): treated as a Secrets Manager reference.
    weird = "arn:aws:ssm:us-east-1::parameter/x"
    c = container(render(base(secrets=[{"X": weird}])))
    return c["secrets"] == [{"name": "X", "valueFrom": f"{weird}:X::"}], json.dumps(c["secrets"])


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------

def test_roles_from_yaml_mapped():
    c = container(render(base()))
    return c.get("jobRoleArn") == JOB_ARN and c.get("executionRoleArn") == EXEC_ARN, json.dumps(c)


def test_role_arn_shared():
    c = container(render({'role_arn': JOB_ARN}))
    return c.get("jobRoleArn") == JOB_ARN and c.get("executionRoleArn") == JOB_ARN, ""


def test_roles_from_ssm_with_cluster():
    ssm = FakeSSM({f"/ecs/{CLUSTER}/job/task-role": JOB_ARN,
                   f"/ecs/{CLUSTER}/job/execution-role": EXEC_ARN})
    with patched_boto3(ssm):
        c = container(render({}, cluster=CLUSTER))
    ok = c.get("jobRoleArn") == JOB_ARN and c.get("executionRoleArn") == EXEC_ARN and len(ssm.calls) == 1
    return ok, f"{c} {ssm.calls}"


def test_yaml_beats_ssm():
    ssm = FakeSSM({f"/ecs/{CLUSTER}/job/execution-role": EXEC_ARN})
    with patched_boto3(ssm):
        c = container(render({'task_role_arn': JOB_ARN}, cluster=CLUSTER))
    ok = (c.get("jobRoleArn") == JOB_ARN and c.get("executionRoleArn") == EXEC_ARN
          and ssm.calls == [[f"/ecs/{CLUSTER}/job/execution-role"]])
    return ok, f"{ssm.calls}"


def test_cluster_missing_ssm_fails():
    with patched_boto3(FakeSSM({})):
        return expect_error(lambda: render({}, cluster=CLUSTER), gtd.RoleResolutionError,
                            "/ecs/test-cluster/job/task-role")


def test_no_cluster_ec2_roles_optional():
    c = container(render({}))
    return "jobRoleArn" not in c and "executionRoleArn" not in c, json.dumps(c)


def test_no_cluster_fargate_needs_execution_role():
    return expect_error(lambda: render({'platform': 'FARGATE'}), gtd.RoleResolutionError,
                        "executionRoleArn", "FARGATE")


def test_no_cluster_secrets_need_execution_role():
    return expect_error(lambda: render({'secrets': [{"T": SSM_SECRET}]}), gtd.RoleResolutionError,
                        "executionRoleArn", "secrets")


def test_bad_role_arn_rejected():
    return expect_error(lambda: render({'role_arn': 'my-role'}), gtd.ValidationError, "IAM role ARN")


# --------------------------------------------------------------------------
# CLI and action.yml
# --------------------------------------------------------------------------

def test_main_writes_output():
    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "job.yaml"
        yaml_path.write_text(f"role_arn: {JOB_ARN}\n"
                             "deregister_previous: true\n")
        out = Path(tmp) / "job-definition.json"
        gh_out = Path(tmp) / "gh_output"
        saved = os.environ.get('GITHUB_OUTPUT')
        os.environ['GITHUB_OUTPUT'] = str(gh_out)
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                code = gjd.main([str(yaml_path), "job", REGION, REGISTRY, IMAGE, TAG,
                                 "--output", str(out)])
        finally:
            if saved is None:
                os.environ.pop('GITHUB_OUTPUT', None)
            else:
                os.environ['GITHUB_OUTPUT'] = saved
        ok = (code == 0 and json.loads(out.read_text())["jobDefinitionName"] == "job"
              and json.loads(stdout.getvalue())["jobDefinitionName"] == "job"
              and gh_out.read_text() == "deregister_previous=true\n")
        return ok, f"code={code} gh_output={gh_out.read_text() if gh_out.exists() else None!r}"


def test_main_fails_on_entrypoint():
    with tempfile.TemporaryDirectory() as tmp:
        yaml_path = Path(tmp) / "job.yaml"
        yaml_path.write_text('entrypoint: ["sh"]\n')
        code = gjd.main([str(yaml_path), "job", REGION, REGISTRY, IMAGE, TAG,
                         "--output", str(Path(tmp) / "out.json")])
        return code == 1 and not (Path(tmp) / "out.json").exists(), f"code={code}"


ACTION = yaml.safe_load((ROOT / "action.yml").read_text())
STEPS = {s.get("id"): s for s in ACTION["runs"]["steps"] if s.get("id")}
BATCH_IF = "${{ inputs.deployment_type == 'batch_job_definition' }}"


def test_action_batch_steps_gated():
    ok = (STEPS["validate-batch-inputs"]["if"] == BATCH_IF
          and STEPS["generate-job-def"]["if"] == BATCH_IF
          and STEPS["register-job-def"]["if"] == (
              "${{ inputs.deployment_type == 'batch_job_definition' && inputs.dry_run == 'false' }}")
          and "${{ inputs" not in STEPS["generate-job-def"]["run"]
          and "${{" not in STEPS["register-job-def"]["run"])
    return ok, ""


def test_action_ecs_steps_skip_batch():
    # None of the ECS registration/deploy steps may run for a batch job.
    for step_id in ("ecs-deploy-service", "check-service-deployment", "register-task-def",
                    "update-eventbridge-target"):
        if "batch_job_definition" in STEPS[step_id]["if"]:
            return False, step_id
    return "batch_job_definition" in STEPS["generate-task-def"]["if"], ""


def test_action_output_and_inputs():
    ok = (ACTION["outputs"]["job_definition_arn"]["value"]
          == "${{ steps.register-job-def.outputs.job_definition_arn }}"
          and ACTION["inputs"]["ecs_cluster"]["required"] is False
          and ACTION["inputs"]["deployment_type"]["default"] == "service")
    return ok, ""


def test_matches_batch_api_model():
    # Offline check against botocore's RegisterJobDefinition input shape, so a
    # misspelt or mistyped field fails here rather than at deploy time.
    import botocore.session
    from botocore.validate import validate_parameters
    shape = (botocore.session.get_session().get_service_model('batch')
             .operation_model('RegisterJobDefinition').input_shape)
    rendered = [json.loads(p.read_text()) for p in sorted(EXPECTED.glob("*.json"))]
    rendered.append(render(base(platform="FARGATE", cpu=0.25, memory=512, ephemeral_storage=30,
                                timeout_seconds=60, parameters={"a": 1},
                                secrets=[{"T": SSM_SECRET}]), cluster=CLUSTER))
    rendered.append(render({}))
    for job in rendered:
        validate_parameters(job, shape)
    return True, f"{len(rendered)} job definitions"


TESTS = golden_tests() + [
    ("api: matches the RegisterJobDefinition model", test_matches_batch_api_model),
    ("mapping: defaults", test_defaults),
    ("mapping: image without registry", test_image_without_registry),
    ("mapping: fractional vCPUs on Fargate", test_fractional_vcpus_on_fargate),
    ("mapping: platform case-insensitive", test_platform_lowercase_accepted),
    ("mapping: Fargate ephemeral storage", test_fargate_ephemeral_storage),
    ("mapping: command and envs", test_command_and_envs),
    ("mapping: Batch-only keys", test_batch_only_keys),
    ("mapping: log group with cluster", test_log_group_with_cluster),
    ("mapping: no log config without cluster", test_no_log_configuration_without_cluster),
    ("mapping: inapplicable keys warn and are dropped", test_ignored_keys_warn),
    ("mapping: services_overrides by task_name", test_overrides_apply_by_task_name),
    ("mapping: shared YAML sidecars not ECS-validated", test_shared_yaml_sidecars_not_validated_as_ecs),
    ("reject: entrypoint", test_entrypoint_rejected),
    ("reject: entrypoint at load", test_entrypoint_rejected_on_load),
    ("reject: secret_files", test_secret_files_rejected),
    ("reject: ECS CPU units", test_ecs_cpu_units_rejected),
    ("reject: fractional vCPU on EC2", test_fractional_on_ec2_rejected),
    ("reject: bad Fargate memory", test_bad_fargate_combo_rejected),
    ("reject: bad Fargate vCPU", test_bad_fargate_vcpu_rejected),
    ("reject: bad platform", test_bad_platform_rejected),
    ("reject: timeout below 60", test_timeout_minimum),
    ("reject: retry_attempts out of range", test_retry_attempts_range),
    ("reject: non-bool flag", test_bool_keys_must_be_bool),
    ("reject: parameters not a mapping", test_parameters_must_be_mapping),
    ("reject: bad parameter name", test_parameter_name_checked),
    ("reject: ephemeral_storage on EC2", test_ephemeral_storage_ec2_rejected),
    ("reject: bad job definition name", test_bad_job_name_rejected),
    ("secrets: legacy + SSM ARN", test_secrets_legacy_with_ssm),
    ("secrets: secrets_envs id/values", test_secrets_envs_id_values),
    ("secrets: secrets_envs name-only", test_secrets_envs_name_only),
    ("secrets: SSM ARN shape strict", test_ssm_arn_shape_is_strict),
    ("roles: YAML mapped to jobRoleArn", test_roles_from_yaml_mapped),
    ("roles: role_arn shared", test_role_arn_shared),
    ("roles: SSM with cluster", test_roles_from_ssm_with_cluster),
    ("roles: YAML beats SSM", test_yaml_beats_ssm),
    ("roles: missing SSM fails", test_cluster_missing_ssm_fails),
    ("roles: optional on EC2 without cluster", test_no_cluster_ec2_roles_optional),
    ("roles: Fargate needs execution role", test_no_cluster_fargate_needs_execution_role),
    ("roles: secrets need execution role", test_no_cluster_secrets_need_execution_role),
    ("roles: bad ARN rejected", test_bad_role_arn_rejected),
    ("main: output file + GITHUB_OUTPUT", test_main_writes_output),
    ("main: entrypoint exits 1", test_main_fails_on_entrypoint),
    ("action: batch steps gated", test_action_batch_steps_gated),
    ("action: ECS steps skip batch", test_action_ecs_steps_skip_batch),
    ("action: output and inputs", test_action_output_and_inputs),
]


def main():
    print("=" * 60)
    print("BATCH JOB DEFINITION TESTS")
    print("=" * 60)
    failed = []
    for name, test in TESTS:
        try:
            passed, detail = test()
        except Exception as e:  # noqa: BLE001
            passed, detail = False, f"{type(e).__name__}: {e}"
        if passed:
            print(f"✅ PASSED: {name}" + (f" - {detail}" if detail and 'created' in detail else ""))
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
    print("\n🎉 All batch job definition tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
