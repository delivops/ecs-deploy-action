#!/usr/bin/env python3
"""
Waiting on ECS service deployments, for the action's `wait_for` input and `deployment_type: wait`.

Two commands:

  service      One service, right after the deploy step updated it with a new task definition. Finds the ECS
               deployment of that revision by ARN (never by position), writes its id to $GITHUB_OUTPUT as
               deployment_id, and with --wait-for healthy polls every 5 s until that deployment is serving:
               PRIMARY, not FAILED, runningCount == the service's desiredCount, every task HEALTHY when the task
               definition has a container health check, and every task healthy in each target group. It does not
               wait for the old tasks to drain or for ECS's "steady state"; the circuit breaker keeps guarding the
               rollout after this returns.

  deployments  Several services at once, each optionally pinned to a deployment id (name=ecs-svc/...). Succeeds
               when every deployment's rolloutState is COMPLETED; fails as soon as one is FAILED or is no longer
               the service's PRIMARY (rolled back or replaced), or at the timeout. One DescribeServices call per
               10 services per poll, every 10 s. Writes a table to $GITHUB_STEP_SUMMARY.

Only stdlib + boto3. The checks take the clients, a clock and a sleep function as arguments so that the tests can
drive them with recorded responses and a fake clock.
"""

import argparse
import json
import os
import sys
import time

SERVICE_POLL_SECONDS = 5
DEPLOYMENTS_POLL_SECONDS = 10
DESCRIBE_SERVICES_MAX = 10   # DescribeServices takes at most 10 services
DESCRIBE_TASKS_MAX = 100     # DescribeTasks takes at most 100 tasks
EVENTS_SHOWN = 5

ACCESS_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException"}


class WaitError(Exception):
    """The deployment failed, was rolled back, or the wait timed out. The message says which and why."""


# --------------------------------------------------------------------------- output helpers

def warn(message):
    print(f"::warning::{message}", flush=True)


def info(message):
    print(message, flush=True)


def write_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def is_access_denied(error):
    response = getattr(error, "response", None) or {}
    return response.get("Error", {}).get("Code") in ACCESS_DENIED


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def format_events(service, count=EVENTS_SHOWN):
    events = (service or {}).get("events") or []
    if not events:
        return "  (no service events)"
    lines = []
    for event in events[:count]:
        created = event.get("createdAt", "")
        if hasattr(created, "isoformat"):
            created = created.isoformat()
        lines.append(f"  {created} {event.get('message', '')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- ECS reads

def describe_service(ecs, cluster, name):
    response = ecs.describe_services(cluster=cluster, services=[name])
    services = response.get("services") or []
    if not services:
        reasons = ", ".join(f.get("reason", "?") for f in response.get("failures") or [])
        raise WaitError(f"service {name} not found in cluster {cluster} ({reasons or 'no reason given'})")
    service = services[0]
    if service.get("status") != "ACTIVE":
        raise WaitError(f"service {name} is {service.get('status')}, not ACTIVE")
    return service


def deployment_by_arn(service, task_definition_arn):
    for deployment in service.get("deployments") or []:
        if deployment.get("taskDefinition") == task_definition_arn:
            return deployment
    return None


def deployment_by_id(service, deployment_id):
    for deployment in service.get("deployments") or []:
        if deployment.get("id") == deployment_id:
            return deployment
    return None


def primary_deployment(service):
    for deployment in service.get("deployments") or []:
        if deployment.get("status") == "PRIMARY":
            return deployment
    return None


def breaker_enabled(service):
    config = (service.get("deploymentConfiguration") or {}).get("deploymentCircuitBreaker") or {}
    return bool(config.get("enable"))


def list_deployment_tasks(ecs, cluster, deployment_id, desired_status):
    """Tasks a deployment started (ECS sets startedBy to the deployment id), described."""
    arns = []
    kwargs = {"cluster": cluster, "startedBy": deployment_id, "desiredStatus": desired_status}
    while True:
        response = ecs.list_tasks(**kwargs)
        arns.extend(response.get("taskArns") or [])
        token = response.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    tasks = []
    for batch in chunks(arns, DESCRIBE_TASKS_MAX):
        tasks.extend(ecs.describe_tasks(cluster=cluster, tasks=batch).get("tasks") or [])
    return tasks


def stopped_reasons(ecs, cluster, deployment_id, limit=5):
    """Why the deployment's tasks stopped, best effort (stopped tasks stay visible for about an hour)."""
    try:
        tasks = list_deployment_tasks(ecs, cluster, deployment_id, "STOPPED")
    except Exception as error:  # noqa: BLE001 - only used to enrich an error message
        return [f"(could not list stopped tasks: {error})"]
    reasons = []
    for task in tasks[:limit]:
        task_id = task.get("taskArn", "").rsplit("/", 1)[-1]
        reason = task.get("stoppedReason") or "no stoppedReason"
        containers = "; ".join(
            f"{c.get('name')}: {c.get('reason') or 'exit ' + str(c.get('exitCode'))}"
            for c in task.get("containers") or []
            if c.get("reason") or c.get("exitCode") not in (None, 0)
        )
        reasons.append(f"{task_id}: {reason}" + (f" ({containers})" if containers else ""))
    return reasons


def task_private_ip(task):
    """The task's ENI private IP (awsvpc network mode), or None (bridge/host)."""
    for attachment in task.get("attachments") or []:
        if attachment.get("type") != "ElasticNetworkInterface":
            continue
        for detail in attachment.get("details") or []:
            if detail.get("name") == "privateIPv4Address" and detail.get("value"):
                return detail["value"]
    for container in task.get("containers") or []:
        for interface in container.get("networkInterfaces") or []:
            if interface.get("privateIpv4Address"):
                return interface["privateIpv4Address"]
    return None


def has_container_health_check(task_definition):
    """True when an essential container has a health check, i.e. when the task's healthStatus means something."""
    for container in (task_definition or {}).get("containerDefinitions") or []:
        if container.get("essential", True) and container.get("healthCheck"):
            return True
    return False


# --------------------------------------------------------------------------- `service`: one service, healthy

class ServingCheck:
    """One poll of "is the new revision serving?". Keeps the warnings it has already given across polls."""

    def __init__(self, ecs, elbv2, cluster, service_name, task_definition_arn, health_check):
        self.ecs = ecs
        self.elbv2 = elbv2
        self.cluster = cluster
        self.service_name = service_name
        self.task_definition_arn = task_definition_arn
        self.health_check = health_check
        self.warned = set()
        self.failed_tasks_seen = 0
        self.last_service = None

    def warn_once(self, key, message):
        if key not in self.warned:
            self.warned.add(key)
            warn(message)

    def fail(self, message, deployment=None):
        details = [message]
        if deployment:
            reasons = stopped_reasons(self.ecs, self.cluster, deployment["id"])
            if reasons:
                details.append("Stopped tasks:\n" + "\n".join(f"  {r}" for r in reasons))
        details.append(f"Last service events:\n{format_events(self.last_service)}")
        raise WaitError("\n".join(details))

    def __call__(self):
        """Returns (serving, status line). Raises WaitError when the deployment can no longer succeed."""
        service = describe_service(self.ecs, self.cluster, self.service_name)
        self.last_service = service
        controller = (service.get("deploymentController") or {}).get("type", "ECS")
        if controller != "ECS":
            raise WaitError(f"service {self.service_name} uses the {controller} deployment controller; "
                            "wait_for: healthy supports the ECS (rolling) controller only, use wait_for: stable")

        deployment = deployment_by_arn(service, self.task_definition_arn)
        if deployment is None:
            primary = primary_deployment(service) or {}
            self.fail(f"no deployment of {self.task_definition_arn} on service {self.service_name}; "
                      f"PRIMARY is {primary.get('id')} ({primary.get('taskDefinition')})")
        if deployment.get("rolloutState") == "FAILED":
            self.fail(f"deployment {deployment['id']} FAILED: {deployment.get('rolloutStateReason', '')}", deployment)
        if deployment.get("status") != "PRIMARY":
            primary = primary_deployment(service) or {}
            self.fail(f"deployment {deployment['id']} is {deployment.get('status')}, no longer PRIMARY: "
                      f"{primary.get('id')} ({primary.get('taskDefinition')}) replaced it "
                      "(circuit breaker rollback or a newer deploy)", deployment)

        failed = deployment.get("failedTasks") or 0
        if failed > self.failed_tasks_seen:
            self.failed_tasks_seen = failed
            reasons = stopped_reasons(self.ecs, self.cluster, deployment["id"])
            if not breaker_enabled(service):
                self.fail(f"deployment {deployment['id']} has {failed} failed task(s) and the service has no "
                          "circuit breaker to stop the rollout", deployment)
            warn(f"deployment {deployment['id']} has {failed} failed task(s); the circuit breaker decides whether "
                 f"to roll back, still waiting. " + " | ".join(reasons))

        desired = service.get("desiredCount") or 0
        running = deployment.get("runningCount") or 0
        if desired == 0:
            return True, "desired count is 0"
        if running < desired:
            return False, f"running {running}/{desired}"

        needs_tasks = self.health_check or bool(service.get("loadBalancers"))
        if not needs_tasks:
            return True, f"running {running}/{desired}"

        try:
            tasks = list_deployment_tasks(self.ecs, self.cluster, deployment["id"], "RUNNING")
        except Exception as error:  # noqa: BLE001
            if not is_access_denied(error):
                raise
            self.warn_once("tasks", "ecs:ListTasks/ecs:DescribeTasks denied; checking runningCount only, not task "
                           f"or target health ({error})")
            return True, f"running {running}/{desired} (task health not checked)"
        tasks = [t for t in tasks if t.get("lastStatus") == "RUNNING"]
        if len(tasks) < desired:
            return False, f"running {len(tasks)}/{desired} tasks listed"

        if self.health_check:
            unhealthy = [t for t in tasks if t.get("healthStatus") != "HEALTHY"]
            if unhealthy:
                return False, f"container health {len(tasks) - len(unhealthy)}/{len(tasks)} HEALTHY"

        for lb in service.get("loadBalancers") or []:
            pending = self.target_group_pending(lb, tasks)
            if pending:
                return False, pending
        return True, f"running {running}/{desired}, healthy"

    def target_group_pending(self, lb, tasks):
        """None when every task is healthy in this load balancer's target group, else a status line."""
        target_group = lb.get("targetGroupArn")
        if not target_group:
            self.warn_once(f"clb:{lb.get('loadBalancerName')}", f"classic load balancer {lb.get('loadBalancerName')} "
                           "is not checked; checking runningCount only for it")
            return None
        targets = []
        for task in tasks:
            ip = task_private_ip(task)
            if ip is None:
                self.warn_once(f"ip:{target_group}", f"tasks have no ENI private IP (not awsvpc), so their health in "
                               f"{target_group} is not checked; checking runningCount only for it")
                return None
            targets.append({"Id": ip, "Port": lb["containerPort"]})
        try:
            descriptions = self.elbv2.describe_target_health(
                TargetGroupArn=target_group, Targets=targets).get("TargetHealthDescriptions") or []
        except Exception as error:  # noqa: BLE001
            if not is_access_denied(error):
                raise
            self.warn_once("elb", "elasticloadbalancing:DescribeTargetHealth denied; checking runningCount only, "
                           f"not target health ({error})")
            return None
        states = {}
        for description in descriptions:
            target = description.get("Target") or {}
            health = description.get("TargetHealth") or {}
            state = health.get("State")
            if state != "healthy" and health.get("Reason"):
                state = f"{state} ({health['Reason']})"
            states[(target.get("Id"), target.get("Port"))] = state
        healthy = sum(1 for t in targets if states.get((t["Id"], t["Port"])) == "healthy")
        if healthy < len(targets):
            name = target_group.rsplit("/", 2)[-2] if target_group.count("/") >= 2 else target_group
            other = sorted({states.get((t["Id"], t["Port"])) or "not registered" for t in targets} - {"healthy"})
            return f"target group {name}: {healthy}/{len(targets)} healthy ({', '.join(other)})"
        return None


def wait_until_serving(check, timeout_seconds, clock=time.monotonic, sleep=time.sleep, poll=SERVICE_POLL_SECONDS):
    """Polls check() until it reports serving. Raises WaitError at the timeout."""
    start = clock()
    deadline = start + timeout_seconds
    last = None
    while True:
        serving, status = check()
        elapsed = clock() - start
        if serving:
            info(f"[{elapsed:5.0f}s] serving: {status}")
            return status
        if status != last:
            info(f"[{elapsed:5.0f}s] waiting: {status}")
            last = status
        if clock() >= deadline:
            raise WaitError(f"timed out after {timeout_seconds / 60:g} min waiting for the new revision to serve "
                            f"({status}). The ECS deployment itself goes on.\n"
                            f"Last service events:\n{format_events(check.last_service)}")
        sleep(poll)


def resolve_deployment(ecs, cluster, service_name, task_definition_arn):
    """The id of the PRIMARY deployment of task_definition_arn. Raises WaitError when there is none."""
    service = describe_service(ecs, cluster, service_name)
    deployment = deployment_by_arn(service, task_definition_arn)
    if deployment is None or deployment.get("status") != "PRIMARY":
        primary = primary_deployment(service) or {}
        state = "not on the service" if deployment is None else deployment.get("status")
        raise WaitError(f"the deployment of {task_definition_arn} is {state}; PRIMARY is {primary.get('id')} "
                        f"({primary.get('taskDefinition')})\nLast service events:\n{format_events(service)}")
    return deployment["id"]


def run_service(args, ecs, elbv2, clock=time.monotonic, sleep=time.sleep):
    if args.wait_for == "stable":
        # The deploy step already waited and check-service-deployment already verified the revision, exactly as
        # before this command existed. Only look up the id; a problem here must never fail a deploy that
        # succeeded, so it is a warning.
        try:
            deployment_id = resolve_deployment(ecs, args.cluster, args.service, args.task_definition_arn)
        except Exception as error:  # noqa: BLE001
            warn(f"could not read the deployment id, the deployment_id output is empty: {error}")
            write_output("deployment_id", "")
            return 0
        info(f"deployment {deployment_id}")
        write_output("deployment_id", deployment_id)
        return 0

    deployment_id = resolve_deployment(ecs, args.cluster, args.service, args.task_definition_arn)
    info(f"deployment {deployment_id} of {args.task_definition_arn} is PRIMARY")
    write_output("deployment_id", deployment_id)
    if args.wait_for == "none":
        return 0

    task_definition = {}
    if args.task_definition_file and os.path.exists(args.task_definition_file):
        with open(args.task_definition_file, encoding="utf-8") as f:
            task_definition = json.load(f)
    check = ServingCheck(ecs, elbv2, args.cluster, args.service, args.task_definition_arn,
                         has_container_health_check(task_definition))
    wait_until_serving(check, args.timeout_minutes * 60, clock=clock, sleep=sleep)
    return 0


# --------------------------------------------------------------------------- `deployments`: several services

def parse_services(text):
    """'a=ecs-svc/1, b\\nc' -> [('a', 'ecs-svc/1'), ('b', None), ('c', None)]."""
    entries = []
    seen = set()
    for raw in text.replace(",", "\n").splitlines():
        item = raw.strip()
        if not item:
            continue
        name, _, deployment_id = item.partition("=")
        name, deployment_id = name.strip(), deployment_id.strip() or None
        if not name:
            raise ValueError(f"empty service name in {item!r}")
        if name in seen:
            raise ValueError(f"service {name} is listed twice")
        seen.add(name)
        entries.append((name, deployment_id))
    if not entries:
        raise ValueError("services is empty")
    return entries


def duration(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Tracked:
    def __init__(self, name, deployment_id):
        self.name = name
        self.deployment_id = deployment_id
        self.state = "PENDING"      # PENDING, IN_PROGRESS, COMPLETED, FAILED
        self.reason = ""
        self.took = None            # seconds from the deployment's creation to COMPLETED
        self.service = None


def poll_deployments(ecs, cluster, tracked):
    """One poll: updates every unfinished entry. Returns the entries that failed in this poll."""
    pending = [t for t in tracked if t.state not in ("COMPLETED", "FAILED")]
    failed = []
    for batch in chunks(pending, DESCRIBE_SERVICES_MAX):
        response = ecs.describe_services(cluster=cluster, services=[t.name for t in batch])
        by_name = {s.get("serviceName"): s for s in response.get("services") or []}
        for entry in batch:
            service = by_name.get(entry.name)
            entry.service = service
            if service is None or service.get("status") != "ACTIVE":
                entry.state, entry.reason = "FAILED", (
                    f"service not found in {cluster}" if service is None else f"service is {service.get('status')}")
                failed.append(entry)
                continue
            if entry.deployment_id is None:
                entry.deployment_id = (primary_deployment(service) or {}).get("id")
            deployment = deployment_by_id(service, entry.deployment_id)
            primary = primary_deployment(service) or {}
            if deployment is None:
                entry.state, entry.reason = "FAILED", (
                    f"deployment is no longer on the service; PRIMARY is {primary.get('id')} "
                    f"({primary.get('taskDefinition')})")
                failed.append(entry)
                continue
            rollout = deployment.get("rolloutState")
            if rollout == "COMPLETED":
                entry.state, entry.reason = "COMPLETED", deployment.get("rolloutStateReason", "")
                created, updated = deployment.get("createdAt"), deployment.get("updatedAt")
                if hasattr(created, "timestamp") and hasattr(updated, "timestamp"):
                    entry.took = updated.timestamp() - created.timestamp()
            elif rollout == "FAILED":
                entry.state, entry.reason = "FAILED", deployment.get("rolloutStateReason") or "rolloutState FAILED"
                failed.append(entry)
            elif deployment.get("status") != "PRIMARY":
                entry.state, entry.reason = "FAILED", (
                    f"no longer PRIMARY: {primary.get('id')} ({primary.get('taskDefinition')}) replaced it "
                    "(circuit breaker rollback or a newer deploy)")
                failed.append(entry)
            else:
                entry.state = "IN_PROGRESS"
                entry.reason = (f"running {deployment.get('runningCount', 0)}/{service.get('desiredCount', 0)}, "
                                f"{len(service.get('deployments') or [])} deployment(s) on the service")
    return failed


def write_summary(tracked, cluster):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    icons = {"COMPLETED": "✅", "FAILED": "❌", "IN_PROGRESS": "⏳", "PENDING": "⏳"}
    lines = [f"### ECS deployments in `{cluster}`", "", "| service | deployment | state | time to complete |",
             "|---|---|---|---|"]
    for t in tracked:
        took = duration(t.took) if t.took is not None else "—"
        state = f"{icons.get(t.state, '')} {t.state}"
        if t.state == "FAILED":
            state += f": {t.reason}".replace("|", "\\|")
        lines.append(f"| {t.name} | `{t.deployment_id or '—'}` | {state} | {took} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def failure_report(entries):
    parts = []
    for t in entries:
        parts.append(f"{t.name} ({t.deployment_id or 'no deployment'}): {t.reason}\n"
                     f"Last service events:\n{format_events(t.service)}")
    return "\n\n".join(parts)


def wait_for_deployments(ecs, cluster, entries, timeout_seconds, clock=time.monotonic, sleep=time.sleep,
                         poll=DEPLOYMENTS_POLL_SECONDS):
    tracked = [Tracked(name, deployment_id) for name, deployment_id in entries]
    start = clock()
    deadline = start + timeout_seconds
    try:
        while True:
            before = {t.name: t.state for t in tracked}
            failed = poll_deployments(ecs, cluster, tracked)
            elapsed = clock() - start
            for t in tracked:
                if t.state != before[t.name] and t.state != "IN_PROGRESS":
                    took = f" in {duration(t.took)}" if t.took is not None else ""
                    info(f"[{elapsed:5.0f}s] {t.name} {t.deployment_id}: {t.state}{took}")
            if failed:
                raise WaitError(failure_report(failed))
            remaining = [t for t in tracked if t.state != "COMPLETED"]
            if not remaining:
                info(f"all {len(tracked)} deployment(s) COMPLETED")
                return tracked
            if clock() >= deadline:
                for t in remaining:
                    t.reason = f"still {t.state} at the timeout ({t.reason})"
                raise WaitError(f"timed out after {timeout_seconds / 60:g} min; not completed:\n\n"
                                + failure_report(remaining))
            sleep(poll)
    finally:
        write_summary(tracked, cluster)


def run_deployments(args, ecs, clock=time.monotonic, sleep=time.sleep):
    entries = parse_services(args.services)
    info(f"waiting for {len(entries)} deployment(s) in {args.cluster}")
    wait_for_deployments(ecs, args.cluster, entries, args.timeout_minutes * 60, clock=clock, sleep=sleep)
    return 0


# --------------------------------------------------------------------------- CLI

def positive_minutes(value):
    try:
        minutes = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of minutes: {value!r}")
    if minutes <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0: {value!r}")
    return minutes


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    service = sub.add_parser("service", help="one service after the deploy step updated it")
    service.add_argument("--cluster", required=True)
    service.add_argument("--service", required=True)
    service.add_argument("--task-definition-arn", required=True)
    service.add_argument("--wait-for", choices=["stable", "healthy", "none"], required=True)
    service.add_argument("--timeout-minutes", type=positive_minutes, default=30)
    service.add_argument("--task-definition-file", default="task-definition.json",
                         help="the registered task definition, to know whether it has a container health check")
    service.add_argument("--region")

    deployments = sub.add_parser("deployments", help="several services, until their deployments complete")
    deployments.add_argument("--cluster", required=True)
    deployments.add_argument("--services", required=True, help="newline/comma list of name or name=deployment_id")
    deployments.add_argument("--timeout-minutes", type=positive_minutes, default=30)
    deployments.add_argument("--region")
    return parser


def clients(region):
    import boto3
    from botocore.config import Config

    # Many jobs poll at once in a parallel deploy: let botocore absorb throttling.
    config = Config(retries={"mode": "standard", "max_attempts": 10})
    return boto3.client("ecs", region_name=region, config=config), \
        boto3.client("elbv2", region_name=region, config=config)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "deployments":
            parse_services(args.services)  # a bad list fails before any AWS call
        ecs, elbv2 = clients(args.region)
        if args.command == "service":
            return run_service(args, ecs, elbv2)
        return run_deployments(args, ecs)
    except (WaitError, ValueError) as error:
        message = str(error)
        print(message, file=sys.stderr, flush=True)
        print(f"::error::{message.splitlines()[0]}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
