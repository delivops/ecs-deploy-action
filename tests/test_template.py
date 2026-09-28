#!/usr/bin/env python3
"""
Unit tests for template mode (scripts/template_task_def.py): the task definition is copied from the service's
Terraform-managed template family instead of generated from YAML.

No AWS: the SSM and ECS clients are fakes that raise the real botocore ClientError, as in test_roles.py. The
fixture is a synthetic DescribeTaskDefinition response.
"""

import copy
import datetime
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def load_module():
    script = ROOT / "scripts" / "template_task_def.py"
    spec = importlib.util.spec_from_file_location("template_task_def", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ttd = load_module()

CLUSTER = "my-cluster"
SERVICE = "my-service"
TEMPLATE_FAMILY = "my-cluster_my-service-template"
IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:v2"
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-app-AbCdEf"

TEMPLATE = {
    "taskDefinitionArn": f"arn:aws:ecs:us-east-1:123456789012:task-definition/{TEMPLATE_FAMILY}:3",
    "family": TEMPLATE_FAMILY,
    "revision": 3,
    "status": "ACTIVE",
    "taskRoleArn": "arn:aws:iam::123456789012:role/my-app-task",
    "executionRoleArn": "arn:aws:iam::123456789012:role/my-app-execution",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "512",
    "memory": "1024",
    "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
    "containerDefinitions": [
        {
            "name": "app",
            "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/my-app:template",
            "essential": True,
            "portMappings": [{"containerPort": 8080, "protocol": "tcp", "name": "default"}],
            "environment": [{"name": "LOG_LEVEL", "value": "info"}, {"name": "EMPTY", "value": ""}],
            "secrets": [{"name": "DB_PASSWORD", "valueFrom": f"{SECRET}:DB_PASSWORD::"}],
            "mountPoints": [{"sourceVolume": "data", "containerPath": "/data", "readOnly": False}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {"awslogs-group": "/ecs/my-cluster/my-service", "awslogs-region": "us-east-1",
                            "awslogs-stream-prefix": "app"},
            },
        },
        {
            "name": "otel-collector",
            "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/otel-collector:0.1",
            "essential": False,
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {"awslogs-group": "/ecs/my-cluster/my-service", "awslogs-region": "us-east-1",
                            "awslogs-stream-prefix": "otel"},
            },
        },
    ],
    "volumes": [{"name": "data", "efsVolumeConfiguration": {"fileSystemId": "fs-0123456789abcdef0",
                                                              "transitEncryption": "ENABLED"}}],
    "placementConstraints": [],
    "requiresAttributes": [{"name": "com.amazonaws.ecs.capability.logging-driver.awslogs"}],
    "compatibilities": ["EC2", "FARGATE"],
    "registeredAt": datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc),
    "registeredBy": "arn:aws:sts::123456789012:assumed-role/terraform/session",
}


def client_error(code, operation="GetParameter"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeSSM:
    def __init__(self, parameters=None, errors=None):
        self.parameters = parameters or {}
        self.errors = errors or {}
        self.calls = []

    def get_parameter(self, Name, WithDecryption=False):
        self.calls.append(Name)
        if Name in self.errors:
            raise self.errors[Name]
        if Name not in self.parameters:
            raise client_error("ParameterNotFound")
        return {"Parameter": {"Name": Name, "Value": self.parameters[Name]}}


class FakeECS:
    def __init__(self, task_definition=None, error=None):
        self.task_definition = task_definition
        self.error = error
        self.calls = []

    def describe_task_definition(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"taskDefinition": copy.deepcopy(self.task_definition), "tags": [{"key": "team", "value": "x"}]}


TEMPLATE_PARAM = f"/ecs/{CLUSTER}/{SERVICE}/task-definition-template"
REPLICA_PARAM = f"/ecs/{CLUSTER}/{SERVICE}/replica-count"


def render(template=TEMPLATE, container_name="app"):
    return ttd.render_from_template(template, family="my-cluster_my-service", container_name=container_name,
                                    image=IMAGE)


def expect_error(fn, error_cls, *fragments):
    try:
        fn()
    except error_cls as e:
        missing = [f for f in fragments if f not in str(e)]
        return (not missing), (f"message missing {missing}; got: {e}" if missing else "")
    except Exception as e:  # noqa: BLE001
        return False, f"expected {error_cls.__name__}, got {type(e).__name__}: {e}"
    return False, f"expected {error_cls.__name__}, but nothing was raised"


# --------------------------------------------------------------------------
# Transform
# --------------------------------------------------------------------------

def test_family_renamed():
    out = render()
    return out["family"] == "my-cluster_my-service", out["family"]


def test_only_named_image_replaced():
    out = render()
    images = {c["name"]: c["image"] for c in out["containerDefinitions"]}
    ok = images["app"] == IMAGE and images["otel-collector"] == TEMPLATE["containerDefinitions"][1]["image"]
    return ok, str(images)


def test_read_only_fields_removed():
    out = render()
    left = [f for f in ttd.READ_ONLY_FIELDS if f in out]
    return not left, f"left: {left}"


def test_everything_else_identical():
    out = render()
    expected = copy.deepcopy(TEMPLATE)
    for f in ttd.READ_ONLY_FIELDS:
        expected.pop(f, None)
    expected["family"] = "my-cluster_my-service"
    expected["containerDefinitions"][0]["image"] = IMAGE
    a, b = json.dumps(out, sort_keys=True), json.dumps(expected, sort_keys=True)
    return a == b, "" if a == b else f"diff:\n{a}\n{b}"


def test_input_not_mutated():
    before = json.dumps(TEMPLATE, sort_keys=True, default=str)
    render()
    return json.dumps(TEMPLATE, sort_keys=True, default=str) == before, ""


def test_missing_container_fails():
    return expect_error(lambda: render(container_name="web"), ttd.TemplateError,
                        "no container named 'web'", "'app'", "'otel-collector'")


def test_duplicate_container_fails():
    tpl = copy.deepcopy(TEMPLATE)
    tpl["containerDefinitions"][1]["name"] = "app"
    return expect_error(lambda: render(template=tpl), ttd.TemplateError, "2 containers named 'app'")


def test_custom_container_name():
    out = render(container_name="otel-collector")
    images = {c["name"]: c["image"] for c in out["containerDefinitions"]}
    ok = images["otel-collector"] == IMAGE and images["app"] == TEMPLATE["containerDefinitions"][0]["image"]
    return ok, str(images)


def test_no_tags_in_output():
    task_def, _ = ttd.build(FakeSSM({TEMPLATE_PARAM: TEMPLATE_FAMILY}), FakeECS(TEMPLATE), cluster=CLUSTER,
                            service=SERVICE, container_name="app", image=IMAGE)
    return "tags" not in task_def, ""


def test_output_is_json_serializable():
    task_def, _ = ttd.build(FakeSSM({TEMPLATE_PARAM: TEMPLATE_FAMILY}), FakeECS(TEMPLATE), cluster=CLUSTER,
                            service=SERVICE, container_name="app", image=IMAGE)
    json.dumps(task_def)  # registeredAt (a datetime) must be gone
    return True, ""


# --------------------------------------------------------------------------
# AWS layer
# --------------------------------------------------------------------------

def test_template_param_missing():
    return expect_error(lambda: ttd.read_template_family(FakeSSM(), CLUSTER, SERVICE), ttd.TemplateError,
                        TEMPLATE_PARAM, "task_definition_template", "3.2.0")


def test_template_param_empty():
    return expect_error(lambda: ttd.read_template_family(FakeSSM({TEMPLATE_PARAM: "  "}), CLUSTER, SERVICE),
                        ttd.TemplateError, "is empty")


def test_template_param_is_service_family():
    ssm = FakeSSM({TEMPLATE_PARAM: "my-cluster_my-service"})
    return expect_error(lambda: ttd.read_template_family(ssm, CLUSTER, SERVICE), ttd.TemplateError,
                        "own family", "into itself")


def test_template_param_access_denied():
    ssm = FakeSSM(errors={TEMPLATE_PARAM: client_error("AccessDeniedException")})
    return expect_error(lambda: ttd.read_template_family(ssm, CLUSTER, SERVICE), ttd.TemplateError,
                        "ssm:GetParameter", TEMPLATE_PARAM)


def test_replica_count_missing_is_none():
    return ttd.read_replica_count(FakeSSM(), CLUSTER, SERVICE) is None, ""


def test_replica_count_value():
    return ttd.read_replica_count(FakeSSM({REPLICA_PARAM: "3"}), CLUSTER, SERVICE) == "3", ""


def test_replica_count_access_denied_fails():
    ssm = FakeSSM(errors={REPLICA_PARAM: client_error("AccessDeniedException")})
    return expect_error(lambda: ttd.read_replica_count(ssm, CLUSTER, SERVICE), ttd.TemplateError,
                        "ssm:GetParameter", REPLICA_PARAM)


def test_replica_count_throttled_fails():
    ssm = FakeSSM(errors={REPLICA_PARAM: client_error("ThrottlingException")})
    return expect_error(lambda: ttd.read_replica_count(ssm, CLUSTER, SERVICE), ttd.TemplateError, "throttled")


def test_describe_without_include():
    ecs = FakeECS(TEMPLATE)
    ttd.describe_template(ecs, TEMPLATE_FAMILY)
    return ecs.calls == [{"taskDefinition": TEMPLATE_FAMILY}], str(ecs.calls)


def test_describe_missing_family():
    ecs = FakeECS(error=client_error("ClientException", "DescribeTaskDefinition"))
    return expect_error(lambda: ttd.describe_template(ecs, TEMPLATE_FAMILY), ttd.TemplateError,
                        TEMPLATE_FAMILY, "no ACTIVE revision")


def test_describe_access_denied():
    ecs = FakeECS(error=client_error("AccessDeniedException", "DescribeTaskDefinition"))
    return expect_error(lambda: ttd.describe_template(ecs, TEMPLATE_FAMILY), ttd.TemplateError,
                        "ecs:DescribeTaskDefinition")


def test_build_end_to_end():
    ssm = FakeSSM({TEMPLATE_PARAM: TEMPLATE_FAMILY, REPLICA_PARAM: "2"})
    task_def, count = ttd.build(ssm, FakeECS(TEMPLATE), cluster=CLUSTER, service=SERVICE, container_name="app",
                                image=IMAGE)
    ok = (task_def["family"] == "my-cluster_my-service" and count == "2"
          and ssm.calls == [TEMPLATE_PARAM, REPLICA_PARAM])
    return ok, f"count={count} calls={ssm.calls}"


def test_image_uri_shared_with_yaml_path():
    # Same helper as the YAML path: a registry or tag embedded in image_name is handled the same way.
    gtd = ttd.gtd
    return (gtd.build_image_uri("reg.example.com", "my-app", "v2") == "reg.example.com/my-app:v2"
            and gtd.build_image_uri("", "nginx:1.27", "") == "nginx:1.27"), ""


def test_main_writes_output_and_replica_count():
    """main() through fake boto3: task-definition.json written, replica_count emitted to GITHUB_OUTPUT."""
    ssm = FakeSSM({TEMPLATE_PARAM: TEMPLATE_FAMILY, REPLICA_PARAM: "4"})
    ecs = FakeECS(TEMPLATE)

    class FakeSession:
        def __init__(self, region_name=None):
            pass

        def client(self, name):
            return {"ssm": ssm, "ecs": ecs}[name]

    class FakeBoto3:
        Session = FakeSession

    saved = sys.modules.get("boto3")
    sys.modules["boto3"] = FakeBoto3
    with tempfile.TemporaryDirectory() as d:
        out, gh = Path(d) / "td.json", Path(d) / "gh_output"
        os.environ["GITHUB_OUTPUT"] = str(gh)
        try:
            rc = ttd.main([CLUSTER, "us-east-1", "123456789012.dkr.ecr.us-east-1.amazonaws.com", "my-app", "v2",
                           SERVICE, "--output", str(out), "--log-level", "ERROR"])
        finally:
            os.environ.pop("GITHUB_OUTPUT", None)
            if saved is None:
                sys.modules.pop("boto3", None)
            else:
                sys.modules["boto3"] = saved
        written = json.loads(out.read_text())
        ok = (rc == 0 and written["containerDefinitions"][0]["image"] == IMAGE
              and gh.read_text() == "replica_count=4\n")
        return ok, f"rc={rc} gh={gh.read_text()!r}"


# --------------------------------------------------------------------------
# action.yml: YAML callers unchanged
# --------------------------------------------------------------------------

ACTION = yaml.safe_load((ROOT / "action.yml").read_text())
STEPS = {s.get("id"): s for s in ACTION["runs"]["steps"] if s.get("id")}


def test_action_defaults():
    inputs = ACTION["inputs"]
    ok = (inputs["task_definition_source"]["default"] == "yaml" and inputs["container_name"]["default"] == "app"
          and inputs["task_config_yaml"]["required"] is False)
    return ok, ""


def test_action_generate_step_unchanged():
    expected = (
        'python3 ${{ github.action_path }}/scripts/generate_task_def.py \\\n'
        '  "${{ inputs.task_config_yaml }}" \\\n'
        '  "${{ inputs.ecs_cluster }}" \\\n'
        '  "${{ inputs.aws_region }}" \\\n'
        '  "${{ steps.define-registry.outputs.registry }}" \\\n'
        '  "${{ steps.define-registry.outputs.container_registry }}" \\\n'
        '  "${{ inputs.image_name }}" \\\n'
        '  "${{ inputs.tag }}" \\\n'
        '  "${{ steps.determine-name.outputs.name }}"\n'
    )
    step = STEPS["generate-task-def"]
    ok = step["run"] == expected and step["if"] == "${{ inputs.task_definition_source == 'yaml' }}"
    return ok, repr(step["run"])


def test_action_new_steps_gated():
    ok = (STEPS["validate-inputs"]["if"] == "${{ inputs.task_definition_source != 'yaml' }}"
          and STEPS["template-task-def"]["if"] == "${{ inputs.task_definition_source == 'template' }}"
          and "${{" not in STEPS["validate-inputs"]["run"] and "${{ inputs" not in STEPS["template-task-def"]["run"])
    return ok, "new steps gated, inputs passed through env"


def test_action_desired_count_expression():
    expr = STEPS["ecs-deploy-service"]["with"]["desired-count"]
    ok = expr == ("${{ inputs.task_definition_source == 'template' && steps.template-task-def.outputs.replica_count"
                  " || steps.generate-task-def.outputs.replica_count }}")
    return ok, expr


TESTS = [
    ("transform: family renamed", test_family_renamed),
    ("transform: only the named image replaced", test_only_named_image_replaced),
    ("transform: read-only fields removed", test_read_only_fields_removed),
    ("transform: everything else identical", test_everything_else_identical),
    ("transform: input not mutated", test_input_not_mutated),
    ("transform: missing container fails", test_missing_container_fails),
    ("transform: duplicate container fails", test_duplicate_container_fails),
    ("transform: custom container_name", test_custom_container_name),
    ("transform: no tags carried", test_no_tags_in_output),
    ("transform: output JSON-serializable", test_output_is_json_serializable),
    ("ssm: template param missing", test_template_param_missing),
    ("ssm: template param empty", test_template_param_empty),
    ("ssm: template param names the service family", test_template_param_is_service_family),
    ("ssm: template param access denied", test_template_param_access_denied),
    ("ssm: replica-count missing -> None", test_replica_count_missing_is_none),
    ("ssm: replica-count value", test_replica_count_value),
    ("ssm: replica-count access denied fails", test_replica_count_access_denied_fails),
    ("ssm: replica-count throttled fails", test_replica_count_throttled_fails),
    ("ecs: describe without include", test_describe_without_include),
    ("ecs: missing family", test_describe_missing_family),
    ("ecs: access denied", test_describe_access_denied),
    ("build: end to end", test_build_end_to_end),
    ("image uri shared with the YAML path", test_image_uri_shared_with_yaml_path),
    ("main: output file + replica_count", test_main_writes_output_and_replica_count),
    ("action: defaults", test_action_defaults),
    ("action: generate-task-def unchanged", test_action_generate_step_unchanged),
    ("action: new steps gated", test_action_new_steps_gated),
    ("action: desired-count expression", test_action_desired_count_expression),
]


def main():
    print("=" * 60)
    print("TEMPLATE MODE TESTS")
    print("=" * 60)
    failed = []
    for name, test in TESTS:
        try:
            passed, detail = test()
        except Exception as e:  # noqa: BLE001
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
    print("\n🎉 All template mode tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
