#!/usr/bin/env python3
"""
Unit tests for scripts/ecs_wait.py: wait_for (healthy / none / stable) on one service, and deployment_type: wait
on several.

No AWS. The ECS and ELBv2 clients are fakes that replay a list of frames, one per DescribeServices call (a poll),
each frame being what DescribeServices / ListTasks / DescribeTasks / DescribeTargetHealth answer at that moment.
The responses keep the real API's shape (the fields ecs_wait reads, and the ones around them). The clock is fake
too, so timeouts take no time. Errors are the real botocore ClientError.
"""

import contextlib
import copy
import datetime
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from botocore.exceptions import ClientError

ROOT = Path(__file__).parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location("ecs_wait", ROOT / "scripts" / "ecs_wait.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ew = load_module()

ACCOUNT = "123456789012"
REGION = "us-east-1"
CLUSTER = "my-cluster"
OLD_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/my-cluster_api:41"
NEW_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/my-cluster_api:42"
OLD_DEP = "ecs-svc/1111111111111111111"
NEW_DEP = "ecs-svc/2222222222222222222"
TG_ARN = f"arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:targetgroup/my-cluster-api/0123456789abcdef"
T0 = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------- response builders

def deployment(dep_id, arn, status, desired, running, rollout="IN_PROGRESS", failed=0, created=0, updated=0,
               reason=None):
    return {
        "id": dep_id,
        "status": status,
        "taskDefinition": arn,
        "desiredCount": desired,
        "pendingCount": max(desired - running, 0),
        "runningCount": running,
        "failedTasks": failed,
        "createdAt": T0 + datetime.timedelta(seconds=created),
        "updatedAt": T0 + datetime.timedelta(seconds=updated or created),
        "launchType": "FARGATE",
        "platformVersion": "1.4.0",
        "rolloutState": rollout,
        "rolloutStateReason": reason or (f"ECS deployment {dep_id} completed." if rollout == "COMPLETED"
                                         else f"ECS deployment {dep_id} in progress."),
    }


def service(name, deployments, desired, lbs=None, breaker=True, controller="ECS", status="ACTIVE", events=None):
    return {
        "serviceArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/{CLUSTER}/{name}",
        "serviceName": name,
        "clusterArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}",
        "loadBalancers": lbs or [],
        "status": status,
        "desiredCount": desired,
        "runningCount": sum(d["runningCount"] for d in deployments),
        "pendingCount": sum(d["pendingCount"] for d in deployments),
        "taskDefinition": next((d["taskDefinition"] for d in deployments if d["status"] == "PRIMARY"), None),
        "deploymentConfiguration": {
            "deploymentCircuitBreaker": {"enable": breaker, "rollback": breaker},
            "maximumPercent": 200,
            "minimumHealthyPercent": 100,
        },
        "deployments": deployments,
        "events": events if events is not None else [
            {"id": "e2", "createdAt": T0 + datetime.timedelta(seconds=20),
             "message": f"(service {name}) has started 1 tasks: (task aaaa)."},
            {"id": "e1", "createdAt": T0, "message": f"(service {name}) has reached a steady state."},
        ],
        "deploymentController": {"type": controller},
    }


def task(task_id, dep_id, arn, last="RUNNING", health="UNKNOWN", ip="10.0.1.10", stopped_reason=None,
         exit_code=None, started=-60):
    t = {
        "taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER}/{task_id}",
        "taskDefinitionArn": arn,
        "startedBy": dep_id,
        "lastStatus": last,
        "desiredStatus": "RUNNING" if last != "STOPPED" else "STOPPED",
        "healthStatus": health,
        "launchType": "FARGATE",
        "startedAt": T0 + datetime.timedelta(seconds=started),
        "attachments": [{
            "id": f"att-{task_id}", "type": "ElasticNetworkInterface", "status": "ATTACHED",
            "details": [
                {"name": "subnetId", "value": "subnet-0123"},
                {"name": "networkInterfaceId", "value": f"eni-{task_id}"},
                {"name": "privateIPv4Address", "value": ip},
            ],
        }] if ip else [],
        "containers": [{"name": "app", "lastStatus": last, "healthStatus": health,
                        **({"exitCode": exit_code} if exit_code is not None else {}),
                        **({"reason": "Essential container in task exited"} if exit_code else {})}],
    }
    if stopped_reason:
        t["stoppedReason"] = stopped_reason
    return t


def access_denied(operation):
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, operation)


# --------------------------------------------------------------------------- fakes

class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeAWS:
    """frames: [{"services": {name: service}, "tasks": {(dep_id, status): [task]}, "targets": {tg: {(ip, port): state}}}]

    Each DescribeServices call moves to the next frame (the last one repeats); the other calls read the current
    frame. A frame without "tasks" has, for each deployment, runningCount RUNNING tasks started a minute before T0.
    deny: names of operations that raise AccessDenied."""

    def __init__(self, frames, deny=()):
        self.frames = frames
        self.index = -1
        self.deny = set(deny)
        self.calls = []

    @property
    def frame(self):
        return self.frames[min(max(self.index, 0), len(self.frames) - 1)]

    def describe_services(self, cluster, services):
        assert cluster == CLUSTER
        assert len(services) <= 10, "DescribeServices takes at most 10 services"
        self.calls.append(("describe_services", tuple(services)))
        self.index += 1
        known = self.frame["services"]
        return {
            "services": [copy.deepcopy(known[n]) for n in services if n in known],
            "failures": [{"arn": n, "reason": "MISSING"} for n in services if n not in known],
        }

    def frame_tasks(self):
        if "tasks" in self.frame:
            return self.frame["tasks"]
        derived = {}
        for svc in self.frame["services"].values():
            for dep in svc["deployments"]:
                derived[(dep["id"], "RUNNING")] = [
                    task(f"{dep['id'][-4:]}-{i}", dep["id"], dep["taskDefinition"], ip=f"10.0.9.{i}")
                    for i in range(dep["runningCount"])]
        return derived

    def list_tasks(self, cluster, startedBy, desiredStatus, nextToken=None):
        self.calls.append(("list_tasks", startedBy, desiredStatus))
        if "ListTasks" in self.deny:
            raise access_denied("ListTasks")
        tasks = self.frame_tasks().get((startedBy, desiredStatus), [])
        return {"taskArns": [t["taskArn"] for t in tasks]}

    def describe_tasks(self, cluster, tasks):
        self.calls.append(("describe_tasks", len(tasks)))
        if "DescribeTasks" in self.deny:
            raise access_denied("DescribeTasks")
        everything = [t for ts in self.frame_tasks().values() for t in ts]
        return {"tasks": [copy.deepcopy(t) for t in everything if t["taskArn"] in tasks], "failures": []}

    def describe_target_health(self, TargetGroupArn, Targets):
        self.calls.append(("describe_target_health", TargetGroupArn, len(Targets)))
        if "DescribeTargetHealth" in self.deny:
            raise access_denied("DescribeTargetHealth")
        states = self.frame.get("targets", {}).get(TargetGroupArn, {})
        out = []
        for target in Targets:
            state = states.get((target["Id"], target["Port"]))
            if state is None:
                out.append({"Target": target, "TargetHealth": {"State": "unused", "Reason": "Target.NotRegistered"}})
            else:
                out.append({"Target": dict(target, AvailabilityZone="us-east-1a"), "HealthCheckPort": str(target["Port"]),
                            "TargetHealth": {"State": state}})
        return {"TargetHealthDescriptions": out}

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


def check(aws, svc_name="api", health_check=False, clock=None):
    """The wall clock starts at T0 and follows the fake clock."""
    return ew.ServingCheck(aws, aws, CLUSTER, svc_name, NEW_ARN, health_check,
                           now=lambda: T0 + datetime.timedelta(seconds=clock.now if clock else 0))


def expect_wait_error(fn, *needles):
    try:
        fn()
    except ew.WaitError as error:
        for needle in needles:
            assert needle in str(error), f"{needle!r} not in error:\n{error}"
        return str(error)
    raise AssertionError("expected WaitError")


# --------------------------------------------------------------------------- healthy: one service

def test_single_task_no_lb_no_health_check():
    """minimumHealthyPercent 0 / maximumPercent 100, one task: done as soon as the new task runs (old one is gone)."""
    frames = [
        {"services": {"api": service("api", [
            deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0),
            deployment(OLD_DEP, OLD_ARN, "ACTIVE", 0, 0, rollout="COMPLETED")], 1)}},
        {"services": {"api": service("api", [
            deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0),
            deployment(OLD_DEP, OLD_ARN, "ACTIVE", 0, 0, rollout="COMPLETED")], 1)}},
        # RUNNING, while ECS still reports IN_PROGRESS for minutes: must not wait for that.
        {"services": {"api": service("api", [
            deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1),
            deployment(OLD_DEP, OLD_ARN, "ACTIVE", 0, 0, rollout="COMPLETED")], 1)}},
    ]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 10, clock.now          # two 5 s polls
    assert clock.sleeps == [5, 5]
    assert aws.count("describe_target_health") == 0


def test_rolling_returns_while_old_task_drains():
    """100/200 worker pool: new tasks up, old deployment still has its tasks (draining, stopTimeout): return."""
    frames = [{"services": {"worker": service("worker", [
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 2, 2),
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 2, 2, rollout="COMPLETED")], 2)}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        status = ew.wait_until_serving(check(aws, "worker"), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 0
    assert "2/2" in status


def test_partial_rollout_keeps_waiting():
    frames = [
        {"services": {"worker": service("worker", [
            deployment(NEW_DEP, NEW_ARN, "PRIMARY", 3, 1),
            deployment(OLD_DEP, OLD_ARN, "ACTIVE", 3, 3, rollout="COMPLETED")], 3)}},
        {"services": {"worker": service("worker", [
            deployment(NEW_DEP, NEW_ARN, "PRIMARY", 3, 3),
            deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 3)}},
    ]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        ew.wait_until_serving(check(aws, "worker"), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 5


def alb_service(new_running=1):
    lbs = [{"targetGroupArn": TG_ARN, "containerName": "app", "containerPort": 8080}]
    return service("api", [
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, new_running),
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 1, lbs=lbs)


def test_alb_target_initial_then_healthy():
    new_task = task("new1", NEW_DEP, NEW_ARN, ip="10.0.1.20")
    running = {(NEW_DEP, "RUNNING"): [new_task]}
    frames = [
        {"services": {"api": alb_service()}, "tasks": running, "targets": {TG_ARN: {}}},                      # not registered yet
        {"services": {"api": alb_service()}, "tasks": running, "targets": {TG_ARN: {("10.0.1.20", 8080): "initial"}}},
        {"services": {"api": alb_service()}, "tasks": running, "targets": {TG_ARN: {("10.0.1.20", 8080): "initial"}}},
        {"services": {"api": alb_service()}, "tasks": running, "targets": {TG_ARN: {
            ("10.0.1.20", 8080): "healthy", ("10.0.1.10", 8080): "draining"}}},
    ]
    aws, clock = FakeAWS(frames), Clock()
    with quiet() as out:
        ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 15, clock.now
    assert "unused (Target.NotRegistered)" in out.getvalue() and "0/1 healthy (initial)" in out.getvalue()
    # Only the new deployment's task is asked about, explicitly by IP and container port.
    assert all(c[2] == 1 for c in aws.calls if c[0] == "describe_target_health")
    assert ("list_tasks", NEW_DEP, "RUNNING") in aws.calls


def test_alb_target_health_denied_falls_back():
    frames = [{"services": {"api": alb_service()}, "tasks": {(NEW_DEP, "RUNNING"): [task("new1", NEW_DEP, NEW_ARN)]}}]
    aws, clock = FakeAWS(frames, deny={"DescribeTargetHealth"}), Clock()
    with quiet() as out:
        ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 0
    assert "::warning::elasticloadbalancing:DescribeTargetHealth denied" in out.getvalue()


def test_list_tasks_denied_falls_back_to_two_polls():
    frames = [{"services": {"api": alb_service()}}]
    aws, clock = FakeAWS(frames, deny={"ListTasks"}), Clock()
    with quiet() as out:
        ew.wait_until_serving(check(aws, health_check=True), 1800, clock=clock, sleep=clock.sleep)
    assert "::warning::ecs:ListTasks/ecs:DescribeTasks denied" in out.getvalue()
    assert out.getvalue().count("::warning::") == 1
    assert clock.now == 5                            # running == desired confirmed on a second poll


def test_list_tasks_denied_resets_on_a_drop():
    up = {"services": {"api": service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1)], 1)}}
    down = {"services": {"api": service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0)], 1)}}
    aws, clock = FakeAWS([up, down, up, up], deny={"ListTasks"}), Clock()
    with quiet():
        ew.wait_until_serving(check(aws, clock=clock), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 15


def test_crash_on_start_is_not_serving():
    """Seen live: a container that exits at once is RUNNING for an instant, and runningCount can catch it."""
    svc = service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1),
                          deployment(OLD_DEP, OLD_ARN, "ACTIVE", 0, 0, rollout="COMPLETED")], 1)
    crashed = service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0, failed=1),
                              deployment(OLD_DEP, OLD_ARN, "ACTIVE", 0, 0, rollout="COMPLETED")], 1)
    rolled_back = rollback_frames()[1]
    frames = [
        {"services": {"api": svc}, "tasks": {(NEW_DEP, "RUNNING"): [task("bad1", NEW_DEP, NEW_ARN, started=0)]}},
        {"services": {"api": crashed}, "tasks": {(NEW_DEP, "STOPPED"): [
            task("bad1", NEW_DEP, NEW_ARN, last="STOPPED", stopped_reason="Essential container in task exited",
                 exit_code=1)]}},
        rolled_back,
    ]
    aws, clock = FakeAWS(frames), Clock()
    with quiet() as out:
        expect_wait_error(lambda: ew.wait_until_serving(check(aws, clock=clock), 1800, clock=clock, sleep=clock.sleep),
                          "FAILED")
    assert "up for less than 10 s" in out.getvalue()


def test_young_task_counts_once_up_for_ten_seconds():
    svc = service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1)], 1)
    frames = [{"services": {"api": svc}, "tasks": {(NEW_DEP, "RUNNING"): [task("new1", NEW_DEP, NEW_ARN, started=-2)]}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        ew.wait_until_serving(check(aws, clock=clock), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 10                           # started 2 s before: up 2 s, 7 s (pending), 12 s (serving)


def test_bridge_tasks_without_ip_skip_target_health():
    frames = [{"services": {"api": alb_service()}, "tasks": {(NEW_DEP, "RUNNING"): [task("new1", NEW_DEP, NEW_ARN, ip=None)]}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet() as out:
        ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep)
    assert "no ENI private IP" in out.getvalue()
    assert aws.count("describe_target_health") == 0


def test_container_health_check_waits_for_healthy():
    svc = service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 2, 2)], 2)
    frames = [
        {"services": {"api": svc}, "tasks": {(NEW_DEP, "RUNNING"): [
            task("a", NEW_DEP, NEW_ARN, health="HEALTHY"), task("b", NEW_DEP, NEW_ARN, health="UNKNOWN")]}},
        {"services": {"api": svc}, "tasks": {(NEW_DEP, "RUNNING"): [
            task("a", NEW_DEP, NEW_ARN, health="HEALTHY"), task("b", NEW_DEP, NEW_ARN, health="HEALTHY")]}},
    ]
    aws, clock = FakeAWS(frames), Clock()
    with quiet() as out:
        ew.wait_until_serving(check(aws, health_check=True), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 5
    assert "1/2 HEALTHY" in out.getvalue()


def test_desired_zero_is_done_at_once():
    frames = [{"services": {"idle": service("idle", [
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 0, 0),
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 0, 0, rollout="COMPLETED")], 0)}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        status = ew.wait_until_serving(check(aws, "idle", health_check=True), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 0 and "desired count is 0" in status


def test_desired_from_service_not_from_new_deployment():
    """The new deployment may briefly report desiredCount 0; the service's desiredCount decides."""
    frames = [{"services": {"api": service("api", [
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 0, 0),
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 1)}},
        {"services": {"api": service("api", [
            deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1),
            deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 1)}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep)
    assert clock.now == 5


def rollback_frames():
    stopped = [task("bad1", NEW_DEP, NEW_ARN, last="STOPPED", stopped_reason="Essential container in task exited",
                    exit_code=1)]
    return [
        {"services": {"api": service("api", [
            deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0, failed=1),
            deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 1)},
         "tasks": {(NEW_DEP, "STOPPED"): stopped}},
        # Circuit breaker: rollback creates a PRIMARY deployment of the old revision; ours is FAILED.
        {"services": {"api": service("api", [
            deployment("ecs-svc/3333333333333333333", OLD_ARN, "PRIMARY", 1, 1),
            deployment(NEW_DEP, NEW_ARN, "ACTIVE", 1, 0, failed=3, rollout="FAILED",
                       reason="ECS deployment circuit breaker: tasks failed to start.")], 1, events=[
            {"id": "e9", "createdAt": T0, "message": "(service api) rolling back to deployment ecs-svc/3333333333333333333."}])},
         "tasks": {(NEW_DEP, "STOPPED"): stopped}},
    ]


def test_circuit_breaker_rollback_fails():
    aws, clock = FakeAWS(rollback_frames()), Clock()
    with quiet() as out:
        error = expect_wait_error(lambda: ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep),
                                  "FAILED", "circuit breaker: tasks failed to start", "Essential container in task exited",
                                  "rolling back")
    # failedTasks 1 with the breaker on: a warning, not a failure; the FAILED rollout is the failure.
    assert "::warning::" in out.getvalue() and "1 failed task" in out.getvalue()
    assert clock.now == 5


def test_not_primary_any_more_fails():
    frames = [{"services": {"api": service("api", [
        deployment("ecs-svc/3333333333333333333", OLD_ARN, "PRIMARY", 1, 1),
        deployment(NEW_DEP, NEW_ARN, "ACTIVE", 1, 0)], 1)}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        expect_wait_error(lambda: ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep),
                          "no longer PRIMARY", "ecs-svc/3333333333333333333")


def test_failed_tasks_without_breaker_fails_at_once():
    frames = rollback_frames()[:1]
    frames[0]["services"]["api"]["deploymentConfiguration"]["deploymentCircuitBreaker"] = {"enable": False,
                                                                                          "rollback": False}
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        expect_wait_error(lambda: ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep),
                          "1 failed task", "no circuit breaker", "bad1: Essential container in task exited",
                          "app: Essential container in task exited")
    assert clock.now == 0


def test_failed_tasks_with_breaker_then_success():
    frames = rollback_frames()[:1] + [{"services": {"api": service("api", [
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1, failed=1),
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 1)}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet() as out:
        ew.wait_until_serving(check(aws), 1800, clock=clock, sleep=clock.sleep)
    assert out.getvalue().count("::warning::") == 1    # warned once for failedTasks 1, not on every poll


def test_timeout():
    frames = [{"services": {"api": service("api", [
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0),
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 1)}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        expect_wait_error(lambda: ew.wait_until_serving(check(aws), 60, clock=clock, sleep=clock.sleep),
                          "timed out after 1 min", "running 0/1", "has reached a steady state")
    assert clock.now == 60
    assert aws.count("describe_services") == 13      # t = 0, 5, ..., 60


def test_code_deploy_controller_rejected():
    frames = [{"services": {"api": service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0)], 1,
                                           controller="CODE_DEPLOY")}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        expect_wait_error(lambda: ew.wait_until_serving(check(aws), 60, clock=clock, sleep=clock.sleep),
                          "CODE_DEPLOY", "use wait_for: stable")


def test_has_container_health_check():
    assert not ew.has_container_health_check({"containerDefinitions": [{"name": "app"}]})
    assert ew.has_container_health_check({"containerDefinitions": [{"name": "app", "healthCheck": {"command": ["x"]}}]})
    # A non-essential sidecar's health check doesn't make the task's healthStatus meaningful.
    assert not ew.has_container_health_check({"containerDefinitions": [
        {"name": "app"}, {"name": "otel", "essential": False, "healthCheck": {"command": ["x"]}}]})


# --------------------------------------------------------------------------- run_service: modes and outputs

class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(cluster=CLUSTER, service="api", task_definition_arn=NEW_ARN,
                                  timeout_minutes=30, task_definition_file="", region=REGION), **kw)


@contextlib.contextmanager
def github_files():
    with tempfile.TemporaryDirectory() as tmp:
        out, summary = os.path.join(tmp, "out"), os.path.join(tmp, "summary")
        old = {k: os.environ.get(k) for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
        os.environ["GITHUB_OUTPUT"], os.environ["GITHUB_STEP_SUMMARY"] = out, summary
        try:
            yield lambda: Path(out).read_text() if os.path.exists(out) else "", \
                lambda: Path(summary).read_text() if os.path.exists(summary) else ""
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_none_mode_outputs_deployment_id_without_waiting():
    frames = [{"services": {"api": service("api", [
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0),
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED")], 1)}}]
    aws, clock = FakeAWS(frames), Clock()
    with github_files() as (output, _), quiet():
        assert ew.run_service(Args(wait_for="none"), aws, aws, clock=clock, sleep=clock.sleep) == 0
        assert output() == f"deployment_id={NEW_DEP}\n"
    assert aws.count("describe_services") == 1 and clock.now == 0


def test_none_mode_checks_by_arn_not_position():
    """The old deployment listed first must not matter; the new one being PRIMARY does."""
    frames = [{"services": {"api": service("api", [
        deployment(OLD_DEP, OLD_ARN, "ACTIVE", 1, 1, rollout="COMPLETED"),
        deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 0)], 1)}}]
    aws = FakeAWS(frames)
    with github_files() as (output, _), quiet():
        ew.run_service(Args(wait_for="none"), aws, aws)
        assert NEW_DEP in output()
    frames = [{"services": {"api": service("api", [deployment(OLD_DEP, OLD_ARN, "PRIMARY", 1, 1)], 1)}}]
    with quiet():
        expect_wait_error(lambda: ew.run_service(Args(wait_for="none"), FakeAWS(frames), None),
                          "not on the service", OLD_DEP)


def test_healthy_mode_reads_health_check_from_task_definition_file():
    svc = service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1)], 1)
    frames = [{"services": {"api": svc}, "tasks": {(NEW_DEP, "RUNNING"): [task("a", NEW_DEP, NEW_ARN, health="UNKNOWN")]}},
              {"services": {"api": svc}, "tasks": {(NEW_DEP, "RUNNING"): [task("a", NEW_DEP, NEW_ARN, health="HEALTHY")]}}]
    aws, clock = FakeAWS(frames), Clock()
    with tempfile.TemporaryDirectory() as tmp, github_files() as (output, _), quiet():
        path = os.path.join(tmp, "task-definition.json")
        Path(path).write_text(json.dumps({"containerDefinitions": [{"name": "app", "healthCheck": {"command": ["x"]}}]}))
        ew.run_service(Args(wait_for="healthy", task_definition_file=path), aws, aws, clock=clock, sleep=clock.sleep)
        assert output() == f"deployment_id={NEW_DEP}\n"
    # resolve (frame 0), first poll (frame 1, HEALTHY): done without sleeping.
    assert clock.now == 0


def test_stable_mode_never_fails_on_lookup():
    """stable keeps v0.6.0's behaviour: the lookup only adds the output and can only warn."""
    class Broken:
        def describe_services(self, **kw):
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "DescribeServices")
    with github_files() as (output, _), quiet() as out:
        assert ew.run_service(Args(wait_for="stable"), Broken(), None) == 0
        assert output() == "deployment_id=\n"
    assert "::warning::" in out.getvalue()
    frames = [{"services": {"api": service("api", [deployment(NEW_DEP, NEW_ARN, "PRIMARY", 1, 1, "COMPLETED")], 1)}}]
    with github_files() as (output, _), quiet():
        assert ew.run_service(Args(wait_for="stable"), FakeAWS(frames), None) == 0
        assert output() == f"deployment_id={NEW_DEP}\n"


# --------------------------------------------------------------------------- deployment_type: wait

def test_parse_services():
    assert ew.parse_services("a=ecs-svc/1\n b \n\nc, d=ecs-svc/2,") == [
        ("a", "ecs-svc/1"), ("b", None), ("c", None), ("d", "ecs-svc/2")]
    for bad in ("", " \n ,", "a\na", "=ecs-svc/1"):
        try:
            ew.parse_services(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def svc_dep(name, n, rollout, status="PRIMARY", created=0, updated=0, extra=()):
    dep = deployment(f"ecs-svc/{n}", f"{NEW_ARN[:-3]}:{n}", status, 1, 1, rollout=rollout, created=created,
                     updated=updated)
    return service(name, [dep, *extra], 1)


def test_deployments_all_complete_batches_of_ten_and_summary():
    names = [f"svc{i:02d}" for i in range(12)]
    in_progress = {n: svc_dep(n, i, "IN_PROGRESS") for i, n in enumerate(names)}
    done = {n: svc_dep(n, i, "COMPLETED", updated=60 + i) for i, n in enumerate(names)}
    # Poll 1 = 2 calls (10 + 2), poll 2 = 2 calls; frames advance per call.
    frames = [{"services": in_progress}] * 2 + [{"services": done}]
    aws, clock = FakeAWS(frames), Clock()
    entries = [(n, f"ecs-svc/{i}") for i, n in enumerate(names)]
    with github_files() as (_, summary), quiet():
        ew.wait_for_deployments(aws, CLUSTER, entries, 1800, clock=clock, sleep=clock.sleep)
        table = summary()
    sizes = [len(c[1]) for c in aws.calls if c[0] == "describe_services"]
    assert sizes == [10, 2, 10, 2], sizes
    assert clock.sleeps == [10]
    assert "| svc00 | `ecs-svc/0` | ✅ COMPLETED | 1m00s |" in table, table
    assert "| svc11 | `ecs-svc/11` | ✅ COMPLETED | 1m11s |" in table


def test_deployments_completed_services_are_not_polled_again():
    frames = [{"services": {"a": svc_dep("a", 1, "COMPLETED"), "b": svc_dep("b", 2, "IN_PROGRESS")}},
              {"services": {"a": svc_dep("a", 1, "COMPLETED"), "b": svc_dep("b", 2, "COMPLETED")}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        ew.wait_for_deployments(aws, CLUSTER, [("a", "ecs-svc/1"), ("b", "ecs-svc/2")], 1800, clock=clock,
                                sleep=clock.sleep)
    assert [c[1] for c in aws.calls] == [("a", "b"), ("b",)]


def test_deployments_name_only_uses_current_primary():
    frames = [{"services": {"a": svc_dep("a", 7, "IN_PROGRESS")}}, {"services": {"a": svc_dep("a", 7, "COMPLETED")}}]
    aws, clock = FakeAWS(frames), Clock()
    with quiet():
        tracked = ew.wait_for_deployments(aws, CLUSTER, [("a", None)], 1800, clock=clock, sleep=clock.sleep)
    assert tracked[0].deployment_id == "ecs-svc/7" and tracked[0].state == "COMPLETED"


def test_deployments_fail_fast_on_rollback():
    rolled_back = service("b", [
        deployment("ecs-svc/9", OLD_ARN, "PRIMARY", 1, 1),
        deployment("ecs-svc/2", NEW_ARN, "ACTIVE", 1, 0, rollout="FAILED", failed=3,
                   reason="ECS deployment circuit breaker: tasks failed to start.")], 1, events=[
        {"id": f"e{i}", "createdAt": T0, "message": f"event {i}"} for i in range(8)])
    frames = [{"services": {"a": svc_dep("a", 1, "IN_PROGRESS"), "b": rolled_back}}]
    aws, clock = FakeAWS(frames), Clock()
    with github_files() as (_, summary), quiet():
        error = expect_wait_error(
            lambda: ew.wait_for_deployments(aws, CLUSTER, [("a", "ecs-svc/1"), ("b", "ecs-svc/2")], 1800,
                                            clock=clock, sleep=clock.sleep),
            "b (ecs-svc/2)", "circuit breaker: tasks failed to start", "event 4")
        table = summary()
    assert "event 5" not in error                    # the last 5 events only
    assert clock.now == 0                            # at once, not when "a" finishes
    assert "❌ FAILED" in table and "⏳ IN_PROGRESS" in table


def test_deployments_replaced_primary_fails():
    replaced = service("a", [deployment("ecs-svc/5", OLD_ARN, "PRIMARY", 1, 1),
                             deployment("ecs-svc/1", NEW_ARN, "ACTIVE", 1, 1)], 1)
    gone = service("b", [deployment("ecs-svc/6", OLD_ARN, "PRIMARY", 1, 1, rollout="COMPLETED")], 1)
    frames = [{"services": {"a": replaced, "b": gone}}]
    with quiet():
        expect_wait_error(lambda: ew.wait_for_deployments(FakeAWS(frames), CLUSTER, [("a", "ecs-svc/1"), ("b", "ecs-svc/2")],
                                                          1800, clock=Clock(), sleep=lambda s: None),
                          "a (ecs-svc/1): no longer PRIMARY", "b (ecs-svc/2): deployment is no longer on the service")


def test_deployments_missing_service_fails():
    frames = [{"services": {}}]
    with quiet():
        expect_wait_error(lambda: ew.wait_for_deployments(FakeAWS(frames), CLUSTER, [("nope", None)], 1800,
                                                          clock=Clock(), sleep=lambda s: None),
                          "nope", "service not found")


def test_deployments_timeout():
    frames = [{"services": {"a": svc_dep("a", 1, "COMPLETED"), "b": svc_dep("b", 2, "IN_PROGRESS")}}]
    aws, clock = FakeAWS(frames), Clock()
    with github_files() as (_, summary), quiet():
        expect_wait_error(lambda: ew.wait_for_deployments(aws, CLUSTER, [("a", "ecs-svc/1"), ("b", "ecs-svc/2")],
                                                          60, clock=clock, sleep=clock.sleep),
                          "timed out after 1 min", "b (ecs-svc/2): still IN_PROGRESS")
        assert "✅ COMPLETED" in summary()
    assert clock.now == 60


def test_main_rejects_bad_input_before_aws():
    with quiet(), contextlib.redirect_stderr(io.StringIO()):
        assert ew.main(["deployments", "--cluster", CLUSTER, "--services", "a\na"]) == 1
        try:
            ew.main(["service", "--cluster", CLUSTER, "--service", "api", "--task-definition-arn", NEW_ARN,
                     "--wait-for", "healthy", "--timeout-minutes", "0"])
        except SystemExit as exit_:
            assert exit_.code == 2
        else:
            raise AssertionError("timeout 0 accepted")


# --------------------------------------------------------------------------- action.yml: defaults unchanged

def action():
    import yaml
    return yaml.safe_load((ROOT / "action.yml").read_text())


def steps():
    return {s.get("id"): s for s in action()["runs"]["steps"]}


def test_action_defaults_reproduce_previous_behaviour():
    inputs = action()["inputs"]
    assert inputs["wait_for"]["default"] == "stable"
    assert inputs["wait_timeout_minutes"]["default"] == "30"       # the upstream wait-for-minutes default
    deploy = steps()["ecs-deploy-service"]["with"]
    # With the default, the expression renders 'true', as the old hard-coded value.
    assert deploy["wait-for-service-stability"] == "${{ inputs.wait_for == 'stable' }}"
    assert deploy["wait-for-minutes"] == "${{ inputs.wait_timeout_minutes }}"
    check = steps()["check-service-deployment"]
    assert check["if"] == "${{ inputs.deployment_type == 'service' && inputs.dry_run == 'false' && inputs.wait_for == 'stable' }}"
    assert "services[0].deployments[0].taskDefinition" in check["run"]   # the stable check itself is untouched


def test_action_new_steps_gated():
    s = steps()
    assert s["validate-wait"]["if"] == ("${{ inputs.wait_for != 'stable' || inputs.wait_timeout_minutes != '30'"
                                        " || inputs.deployment_type == 'wait' }}")
    assert s["ecs-wait"]["if"] == "${{ inputs.deployment_type == 'service' && inputs.dry_run == 'false' }}"
    assert s["wait-deployments"]["if"] == "${{ inputs.deployment_type == 'wait' }}"
    for step_id in ("login_ecr", "define-registry", "determine-name"):
        assert s[step_id]["if"] == "${{ inputs.deployment_type != 'wait' }}", step_id
    for step_id in ("validate-wait", "ecs-wait", "wait-deployments"):
        assert "${{" not in s[step_id]["run"].replace("${{ github.action_path }}", ""), step_id


def test_action_outputs():
    outputs = action()["outputs"]
    assert outputs["deployment_id"]["value"] == "${{ steps.ecs-wait.outputs.deployment_id }}"
    assert "steps.ecs-deploy-service.outputs.task-definition-arn" in outputs["task_definition_arn"]["value"]
    assert "steps.register-task-def.outputs.task_definition_arn" in outputs["task_definition_arn"]["value"]


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(error).__name__}: {error}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
