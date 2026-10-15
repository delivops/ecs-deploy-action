#!/usr/bin/env python3
"""
Unit tests for Batch template mode (scripts/template_job_def.py): the job definition is copied from a
Terraform-managed template job definition instead of generated from YAML.

No AWS: the SSM and Batch clients are fakes that raise the real botocore ClientError, as in test_template.py.
The fixture is a synthetic DescribeJobDefinitions response.
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from botocore.exceptions import ClientError

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def load_module():
    script = ROOT / "scripts" / "template_job_def.py"
    spec = importlib.util.spec_from_file_location("template_job_def", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tjd = load_module()

JOB = "nightly-report"
TEMPLATE_NAME = "nightly-report-template"
IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/nightly-report:v2"
SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:nightly-report-AbCdEf"


def template(revision=4, **overrides):
    """A Fargate container job definition as DescribeJobDefinitions returns it."""
    tpl = {
        "jobDefinitionName": TEMPLATE_NAME,
        "jobDefinitionArn": f"arn:aws:batch:us-east-1:123456789012:job-definition/{TEMPLATE_NAME}:{revision}",
        "revision": revision,
        "status": "ACTIVE",
        "type": "container",
        "parameters": {"date": "today"},
        "retryStrategy": {"attempts": 3, "evaluateOnExit": [{"onExitCode": "137", "action": "RETRY"}]},
        "timeout": {"attemptDurationSeconds": 3600},
        "containerProperties": {
            "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/nightly-report:template",
            "command": ["python", "-m", "worker", "--date", "Ref::date"],
            "jobRoleArn": "arn:aws:iam::123456789012:role/nightly-report-job",
            "executionRoleArn": "arn:aws:iam::123456789012:role/nightly-report-execution",
            "volumes": [],
            "environment": [{"name": "LOG_LEVEL", "value": "info"}],
            "mountPoints": [],
            "ulimits": [],
            "resourceRequirements": [{"value": "1", "type": "VCPU"}, {"value": "2048", "type": "MEMORY"}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {"awslogs-group": "/batch/nightly-report", "awslogs-region": "us-east-1",
                            "awslogs-stream-prefix": "batch"},
                "secretOptions": [],
            },
            "secrets": [{"name": "DB_PASSWORD", "valueFrom": f"{SECRET}:DB_PASSWORD::"}],
            "networkConfiguration": {"assignPublicIp": "DISABLED"},
            "fargatePlatformConfiguration": {"platformVersion": "LATEST"},
            "ephemeralStorage": {"sizeInGiB": 30},
            "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"},
        },
        "tags": {"team": "data"},
        "propagateTags": True,
        "platformCapabilities": ["FARGATE"],
        "containerOrchestrationType": "ECS",
    }
    tpl.update(overrides)
    return tpl


TEMPLATE = template()


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


class FakeBatch:
    """`pages` is a list of jobDefinitions lists, one per page."""

    def __init__(self, pages=None, error=None):
        self.pages = pages if pages is not None else [[TEMPLATE]]
        self.error = error
        self.calls = []

    def get_paginator(self, operation):
        fake = self

        class Paginator:
            def paginate(self, **kwargs):
                fake.calls.append((operation, kwargs))
                if fake.error:
                    raise fake.error
                return [{"jobDefinitions": copy.deepcopy(page)} for page in fake.pages]

        return Paginator()


TEMPLATE_PARAM = f"/batch/{JOB}/job-definition-template"


def render(tpl=TEMPLATE):
    return tjd.render_from_template(tpl, job_name=JOB, image=IMAGE)


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

def test_name_set():
    out = render()
    return out["jobDefinitionName"] == JOB, out["jobDefinitionName"]


def test_image_replaced():
    out = render()
    return out["containerProperties"]["image"] == IMAGE, out["containerProperties"]["image"]


def test_read_only_fields_and_tags_removed():
    out = render()
    left = [f for f in ("jobDefinitionArn", "revision", "status", "containerOrchestrationType", "tags") if f in out]
    return not left, f"left: {left}"


def test_everything_else_identical():
    out = render()
    expected = copy.deepcopy(TEMPLATE)
    for f in tjd.READ_ONLY_FIELDS:
        expected.pop(f, None)
    expected["jobDefinitionName"] = JOB
    expected["containerProperties"]["image"] = IMAGE
    a, b = json.dumps(out, sort_keys=True), json.dumps(expected, sort_keys=True)
    return a == b, "" if a == b else f"diff:\n{a}\n{b}"


def test_input_not_mutated():
    before = json.dumps(TEMPLATE, sort_keys=True)
    render()
    return json.dumps(TEMPLATE, sort_keys=True) == before, ""


def test_unsupported_properties_fail():
    for prop in ("ecsProperties", "eksProperties", "nodeProperties"):
        tpl = template(**{prop: {"x": 1}})
        ok, detail = expect_error(lambda: render(tpl), tjd.TemplateError, prop, "containerProperties")
        if not ok:
            return False, f"{prop}: {detail}"
    return True, ""


def test_ecs_properties_without_container_fails():
    tpl = template(ecsProperties={"taskProperties": []})
    tpl.pop("containerProperties")
    return expect_error(lambda: render(tpl), tjd.TemplateError, "ecsProperties")


def test_multinode_fails():
    tpl = template(type="multinode")
    return expect_error(lambda: render(tpl), tjd.TemplateError, "'multinode'", "containerProperties")


def test_matches_batch_api_model():
    # Every field left after the transform must be one RegisterJobDefinition takes, with the right shape.
    import botocore.session
    from botocore.validate import validate_parameters
    shape = (botocore.session.get_session().get_service_model("batch")
             .operation_model("RegisterJobDefinition").input_shape)
    validate_parameters(render(), shape)
    ec2 = template(platformCapabilities=["EC2"])
    for key in ("networkConfiguration", "fargatePlatformConfiguration", "ephemeralStorage", "runtimePlatform"):
        ec2["containerProperties"].pop(key)
    validate_parameters(render(ec2), shape)
    return True, ""


def test_describe_only_fields_cover_the_model():
    # READ_ONLY_FIELDS minus tags must be exactly what DescribeJobDefinitions returns and Register doesn't take,
    # so a field AWS adds later shows up here instead of failing a deploy.
    import botocore.session
    model = botocore.session.get_session().get_service_model("batch")
    out = set(model.shape_for("JobDefinition").members)
    inp = set(model.operation_model("RegisterJobDefinition").input_shape.members)
    expected = set(tjd.READ_ONLY_FIELDS) - {"tags"}
    return out - inp == expected, f"describe-only: {sorted(out - inp)}"


# --------------------------------------------------------------------------
# AWS layer
# --------------------------------------------------------------------------

def test_template_param_missing():
    return expect_error(lambda: tjd.read_template_name(FakeSSM(), JOB), tjd.TemplateError,
                        TEMPLATE_PARAM, "not found")


def test_template_param_empty():
    return expect_error(lambda: tjd.read_template_name(FakeSSM({TEMPLATE_PARAM: " "}), JOB), tjd.TemplateError,
                        "is empty")


def test_template_param_is_job_itself():
    return expect_error(lambda: tjd.read_template_name(FakeSSM({TEMPLATE_PARAM: JOB}), JOB), tjd.TemplateError,
                        "the job definition itself", "into itself")


def test_template_param_arn_rejected():
    arn = f"arn:aws:batch:us-east-1:123456789012:job-definition/{TEMPLATE_NAME}:4"
    for value in (arn, f"{TEMPLATE_NAME}:4"):
        ok, detail = expect_error(lambda: tjd.read_template_name(FakeSSM({TEMPLATE_PARAM: value}), JOB),
                                  tjd.TemplateError, "not an ARN")
        if not ok:
            return False, detail
    return True, ""


def test_template_param_access_denied():
    ssm = FakeSSM(errors={TEMPLATE_PARAM: client_error("AccessDeniedException")})
    return expect_error(lambda: tjd.read_template_name(ssm, JOB), tjd.TemplateError,
                        "ssm:GetParameter", TEMPLATE_PARAM)


def test_describe_picks_highest_revision_across_pages():
    batch = FakeBatch([[template(3), template(7)], [template(12), template(5)]])
    tpl = tjd.describe_template(batch, TEMPLATE_NAME)
    ok = (tpl["revision"] == 12 and batch.calls == [
        ("describe_job_definitions", {"jobDefinitionName": TEMPLATE_NAME, "status": "ACTIVE"})])
    return ok, f"revision={tpl['revision']} calls={batch.calls}"


def test_describe_skips_inactive():
    batch = FakeBatch([[template(9, status="INACTIVE"), template(2)]])
    return tjd.describe_template(batch, TEMPLATE_NAME)["revision"] == 2, ""


def test_describe_no_active_revision():
    return expect_error(lambda: tjd.describe_template(FakeBatch([[]]), TEMPLATE_NAME), tjd.TemplateError,
                        TEMPLATE_NAME, "no ACTIVE revision")


def test_describe_access_denied():
    batch = FakeBatch(error=client_error("AccessDeniedException", "DescribeJobDefinitions"))
    return expect_error(lambda: tjd.describe_template(batch, TEMPLATE_NAME), tjd.TemplateError,
                        "batch:DescribeJobDefinitions")


def test_build_end_to_end():
    ssm = FakeSSM({TEMPLATE_PARAM: TEMPLATE_NAME})
    job_def = tjd.build(ssm, FakeBatch(), job_name=JOB, image=IMAGE)
    ok = (job_def["jobDefinitionName"] == JOB and job_def["containerProperties"]["image"] == IMAGE
          and ssm.calls == [TEMPLATE_PARAM])
    return ok, f"calls={ssm.calls}"


def run_main(args, ssm, batch):
    """main() through fake boto3; returns (rc, written job definition or None, GITHUB_OUTPUT text)."""

    class FakeSession:
        def __init__(self, region_name=None):
            pass

        def client(self, name):
            return {"ssm": ssm, "batch": batch}[name]

    class FakeBoto3:
        Session = FakeSession

    saved = sys.modules.get("boto3")
    sys.modules["boto3"] = FakeBoto3
    with tempfile.TemporaryDirectory() as d:
        out, gh = Path(d) / "jd.json", Path(d) / "gh_output"
        gh.write_text("")
        os.environ["GITHUB_OUTPUT"] = str(gh)
        try:
            rc = tjd.main(["us-east-1", "123456789012.dkr.ecr.us-east-1.amazonaws.com", "nightly-report", "v2",
                           *args, "--output", str(out), "--log-level", "ERROR"])
        finally:
            os.environ.pop("GITHUB_OUTPUT", None)
            if saved is None:
                sys.modules.pop("boto3", None)
            else:
                sys.modules["boto3"] = saved
        written = json.loads(out.read_text()) if out.exists() else None
        return rc, written, gh.read_text()


def test_main_writes_output_and_deregister_previous():
    rc, written, gh = run_main([JOB, "--deregister-previous", "true"], FakeSSM({TEMPLATE_PARAM: TEMPLATE_NAME}),
                               FakeBatch())
    ok = (rc == 0 and written["containerProperties"]["image"] == IMAGE and written["jobDefinitionName"] == JOB
          and gh == "deregister_previous=true\n")
    return ok, f"rc={rc} gh={gh!r}"


def test_main_default_deregister_previous_false():
    rc, _, gh = run_main([JOB], FakeSSM({TEMPLATE_PARAM: TEMPLATE_NAME}), FakeBatch())
    return rc == 0 and gh == "deregister_previous=false\n", f"rc={rc} gh={gh!r}"


def test_main_fails_without_output():
    rc, written, gh = run_main([JOB], FakeSSM(), FakeBatch())
    return rc == 1 and written is None and gh == "", f"rc={rc}"


def test_main_invalid_job_name():
    ssm = FakeSSM({TEMPLATE_PARAM: TEMPLATE_NAME})
    rc, written, _ = run_main(["bad name"], ssm, FakeBatch())
    return rc == 1 and written is None and ssm.calls == [], f"rc={rc} calls={ssm.calls}"


# --------------------------------------------------------------------------
# action.yml
# --------------------------------------------------------------------------

ACTION = yaml.safe_load((ROOT / "action.yml").read_text())
STEPS = {s.get("id"): s for s in ACTION["runs"]["steps"] if s.get("id")}


def run_step(step_id, **env):
    """Run a validation step's bash with the given env; (exit code, stdout)."""
    full = {**os.environ, "SOURCE": "yaml", "TASK_CONFIG_YAML": "", "DEPLOYMENT_TYPE": "service", "TASK_NAME": "",
            "DEREGISTER_PREVIOUS": "false", **env}
    proc = subprocess.run(["bash", "-c", STEPS[step_id]["run"]], env=full, capture_output=True, text=True)
    return proc.returncode, proc.stdout


def validate(**env):
    """Both validation steps in order, each only when action.yml would run it."""
    if env.get("SOURCE", "yaml") != "yaml":
        code, out = run_step("validate-inputs", **env)
        if code:
            return code, out
    if env.get("DEPLOYMENT_TYPE") == "batch_job_definition":
        return run_step("validate-batch-inputs", **env)
    return 0, ""


def test_action_template_batch_accepted():
    code, out = validate(SOURCE="template", DEPLOYMENT_TYPE="batch_job_definition", TASK_NAME=JOB,
                         DEREGISTER_PREVIOUS="true")
    return code == 0, out


def test_action_template_batch_with_yaml_fails():
    code, out = validate(SOURCE="template", DEPLOYMENT_TYPE="batch_job_definition", TASK_NAME=JOB,
                         TASK_CONFIG_YAML="batch.yaml")
    return code == 1 and "task_config_yaml is not used" in out, out


def test_action_template_batch_needs_task_name():
    code, out = validate(SOURCE="template", DEPLOYMENT_TYPE="batch_job_definition")
    return code == 1 and "task_name" in out, out


def test_action_template_other_types_still_rejected():
    code, out = validate(SOURCE="template", DEPLOYMENT_TYPE="scheduled_task", TASK_NAME=JOB)
    return code == 1 and "service or batch_job_definition only" in out, out


def test_action_yaml_batch_rejects_deregister_input():
    code, out = validate(DEPLOYMENT_TYPE="batch_job_definition", TASK_NAME=JOB, TASK_CONFIG_YAML="batch.yaml",
                         DEREGISTER_PREVIOUS="true")
    return code == 1 and "set deregister_previous in the YAML" in out, out


def test_action_yaml_batch_still_accepted():
    code, out = validate(DEPLOYMENT_TYPE="batch_job_definition", TASK_NAME=JOB, TASK_CONFIG_YAML="batch.yaml")
    return code == 0, out


def test_action_bad_deregister_value():
    code, out = validate(SOURCE="template", DEPLOYMENT_TYPE="batch_job_definition", TASK_NAME=JOB,
                         DEREGISTER_PREVIOUS="yes")
    return code == 1 and "'true' or 'false'" in out, out


def test_action_steps_gated():
    ok = (STEPS["template-job-def"]["if"] == ("${{ inputs.task_definition_source == 'template' && "
                                              "inputs.deployment_type == 'batch_job_definition' }}")
          and STEPS["template-task-def"]["if"] == ("${{ inputs.task_definition_source == 'template' && "
                                                   "inputs.deployment_type == 'service' }}")
          and "yaml" in STEPS["generate-job-def"]["if"]
          and "${{ inputs" not in STEPS["template-job-def"]["run"]
          and ACTION["inputs"]["deregister_previous"]["default"] == "false")
    return ok, ""


def test_action_register_reads_either_step():
    expr = STEPS["register-job-def"]["env"]["DEREGISTER_PREVIOUS"]
    ok = expr == ("${{ steps.generate-job-def.outputs.deregister_previous || "
                  "steps.template-job-def.outputs.deregister_previous }}")
    return ok, expr


TESTS = [
    ("transform: name set to task_name", test_name_set),
    ("transform: image replaced", test_image_replaced),
    ("transform: read-only fields and tags removed", test_read_only_fields_and_tags_removed),
    ("transform: everything else identical", test_everything_else_identical),
    ("transform: input not mutated", test_input_not_mutated),
    ("transform: ecs/eks/node properties fail", test_unsupported_properties_fail),
    ("transform: ecsProperties-only template fails", test_ecs_properties_without_container_fails),
    ("transform: multinode fails", test_multinode_fails),
    ("api: output matches the RegisterJobDefinition model", test_matches_batch_api_model),
    ("api: describe-only fields are the dropped ones", test_describe_only_fields_cover_the_model),
    ("ssm: template param missing", test_template_param_missing),
    ("ssm: template param empty", test_template_param_empty),
    ("ssm: template param names the job itself", test_template_param_is_job_itself),
    ("ssm: template param ARN / name:revision rejected", test_template_param_arn_rejected),
    ("ssm: template param access denied", test_template_param_access_denied),
    ("batch: highest revision across pages", test_describe_picks_highest_revision_across_pages),
    ("batch: inactive revisions skipped", test_describe_skips_inactive),
    ("batch: no ACTIVE revision", test_describe_no_active_revision),
    ("batch: access denied", test_describe_access_denied),
    ("build: end to end", test_build_end_to_end),
    ("main: output file + deregister_previous", test_main_writes_output_and_deregister_previous),
    ("main: deregister_previous defaults to false", test_main_default_deregister_previous_false),
    ("main: failure writes nothing", test_main_fails_without_output),
    ("main: invalid task_name fails before AWS", test_main_invalid_job_name),
    ("action: template + batch accepted", test_action_template_batch_accepted),
    ("action: template + batch + task_config_yaml fails", test_action_template_batch_with_yaml_fails),
    ("action: template + batch needs task_name", test_action_template_batch_needs_task_name),
    ("action: template + other types rejected", test_action_template_other_types_still_rejected),
    ("action: YAML batch rejects deregister_previous input", test_action_yaml_batch_rejects_deregister_input),
    ("action: YAML batch unchanged", test_action_yaml_batch_still_accepted),
    ("action: bad deregister_previous value", test_action_bad_deregister_value),
    ("action: steps gated", test_action_steps_gated),
    ("action: register reads either step", test_action_register_reads_either_step),
]


def main():
    print("=" * 60)
    print("BATCH TEMPLATE MODE TESTS")
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
    print("\n🎉 All batch template mode tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
