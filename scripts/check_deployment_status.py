#!/usr/bin/env python3
"""Utility script to report ECS deployment status in detail."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect detailed ECS deployment status and diagnostics"
    )
    parser.add_argument("cluster", help="Name of the ECS cluster")
    parser.add_argument("service", help="Name of the ECS service")
    parser.add_argument(
        "--task-definition",
        dest="task_definition",
        default=None,
        help="Specific task definition ARN to inspect (defaults to the service's primary deployment)",
    )
    parser.add_argument(
        "--region",
        dest="region",
        default=None,
        help="AWS region (falls back to environment configuration if omitted)",
    )
    parser.add_argument(
        "--max-attempts",
        dest="max_attempts",
        type=int,
        default=1,
        help="Maximum number of describe attempts before producing diagnostics",
    )
    parser.add_argument(
        "--poll-delay",
        dest="poll_delay",
        type=int,
        default=30,
        help="Delay in seconds between attempts (ignored when max-attempts is 1)",
    )
    parser.add_argument(
        "--log-lines",
        dest="log_lines",
        type=int,
        default=40,
        help="Number of log lines to fetch for failed containers",
    )
    parser.add_argument(
        "--deployment-outcome",
        dest="deployment_outcome",
        choices=["success", "failure", "cancelled", "skipped"],
        default=None,
        help="Outcome of the deployment step (if known)",
    )
    return parser.parse_args()


def create_clients(region: Optional[str]) -> Tuple:
    session = boto3.Session(region_name=region) if region else boto3.Session()
    ecs = session.client("ecs")
    logs = session.client("logs")
    return ecs, logs


def describe_service(ecs, cluster: str, service: str) -> Dict:
    response = ecs.describe_services(cluster=cluster, services=[service])
    failures = response.get("failures") or []
    if failures:
        reasons = ", ".join(f.get("reason", "unknown") for f in failures)
        raise RuntimeError(f"Failed to describe service {service}: {reasons}")

    services = response.get("services") or []
    if not services:
        raise RuntimeError(f"Service {service} not found in cluster {cluster}")

    return services[0]


def find_target_deployment(service: Dict, explicit_task_def: Optional[str]) -> Optional[Dict]:
    deployments: List[Dict] = service.get("deployments", [])
    if explicit_task_def:
        for deployment in deployments:
            if deployment.get("taskDefinition") == explicit_task_def:
                return deployment

    for deployment in deployments:
        if deployment.get("status") == "PRIMARY":
            return deployment

    return deployments[0] if deployments else None


def collect_service_events(service: Dict, limit: int = 5) -> List[str]:
    events = service.get("events", [])[:limit]
    formatted: List[str] = []
    for event in events:
        created_at: Optional[datetime] = event.get("createdAt")
        timestamp = created_at.isoformat() if isinstance(created_at, datetime) else "unknown"
        formatted.append(f"[{timestamp}] {event.get('message', 'No message provided')}")
    return formatted


def list_relevant_tasks(ecs, cluster: str, service: str, task_definition: Optional[str]) -> List[Dict]:
    kwargs: Dict[str, object] = {
        "cluster": cluster,
        "serviceName": service,
        "desiredStatus": "STOPPED",
        "maxResults": 10,
    }
    tasks: List[Dict] = []
    next_token: Optional[str] = None

    while True:
        if next_token:
            kwargs["nextToken"] = next_token
        response = ecs.list_tasks(**kwargs)
        task_arns = response.get("taskArns", [])
        if not task_arns:
            break

        described = ecs.describe_tasks(cluster=cluster, tasks=task_arns)
        for task in described.get("tasks", []):
            if task_definition and task.get("taskDefinitionArn") != task_definition:
                continue
            tasks.append(task)

        next_token = response.get("nextToken")
        if not next_token:
            break

    return tasks


def fetch_log_group_mapping(ecs, task_definition_arn: Optional[str]) -> Dict[str, str]:
    if not task_definition_arn:
        return {}

    try:
        response = ecs.describe_task_definition(taskDefinition=task_definition_arn)
    except ClientError:
        return {}

    task_definition = response.get("taskDefinition", {})
    mapping: Dict[str, str] = {}
    for container in task_definition.get("containerDefinitions", []):
        log_configuration = container.get("logConfiguration") or {}
        options = log_configuration.get("options") or {}
        log_group = options.get("awslogs-group")
        if log_group:
            mapping[container.get("name", "")] = log_group
    return mapping


def fetch_recent_logs(logs, log_group: str, log_stream: str, limit: int) -> List[str]:
    if not log_group or not log_stream:
        return []

    try:
        response = logs.get_log_events(
            logGroupName=log_group,
            logStreamName=log_stream,
            limit=limit,
            startFromHead=False,
        )
    except (ClientError, BotoCoreError):
        return []

    events = response.get("events", [])
    return [event.get("message", "") for event in events[-limit:]]


def summarize_task(task: Dict, log_group_mapping: Dict[str, str], logs_client, log_lines: int) -> None:
    task_arn = task.get("taskArn", "unknown-task")
    stopped_reason = task.get("stoppedReason") or "Unknown stopped reason"
    stop_code = task.get("stopCode") or "Unknown stop code"

    print(f"\nTask: {task_arn}")
    print(f"  Stop code: {stop_code}")
    print(f"  Stopped reason: {stopped_reason}")

    for container in task.get("containers", []):
        name = container.get("name", "<unnamed>")
        exit_code = container.get("exitCode")
        reason = container.get("reason") or ""
        last_status = container.get("lastStatus") or "unknown"
        health_status = container.get("healthStatus") or "unknown"
        log_stream = container.get("logStreamName")
        log_group = log_group_mapping.get(name, "")

        print(f"  Container: {name}")
        print(f"    Last status: {last_status} (health: {health_status})")
        if exit_code is not None:
            print(f"    Exit code: {exit_code}")
        if reason:
            print(f"    Reason: {reason}")
        if log_stream:
            print(f"    Log stream: {log_stream}")

        log_lines_output = fetch_recent_logs(logs_client, log_group, log_stream, log_lines)
        if log_lines_output:
            print("    Recent logs:")
            for line in log_lines_output:
                print(f"      {line}")
        elif log_stream:
            print("    Recent logs: (none available or unable to retrieve)")


def main() -> int:
    args = parse_args()

    try:
        ecs_client, logs_client = create_clients(args.region)
    except (ClientError, BotoCoreError) as exc:
        print(f"Failed to create AWS clients: {exc}", file=sys.stderr)
        return 1

    deployment_failed = args.deployment_outcome in {"failure", "cancelled"}

    service_info: Optional[Dict] = None
    target_deployment: Optional[Dict] = None
    attempt = 0
    while attempt < max(args.max_attempts, 1):
        attempt += 1
        try:
            service_info = describe_service(ecs_client, args.cluster, args.service)
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Error describing service: {exc}", file=sys.stderr)
            return 1

        target_deployment = find_target_deployment(service_info, args.task_definition)
        rollout_state = target_deployment.get("rolloutState") if target_deployment else None
        rollout_reason = target_deployment.get("rolloutStateReason") if target_deployment else None
        status_label = rollout_state or target_deployment.get("status") if target_deployment else "unknown"

        print(f"Deployment status attempt {attempt}: {status_label}")
        if rollout_reason:
            print(f"  Reason: {rollout_reason}")

        if rollout_state == "COMPLETED" and not deployment_failed:
            print("Deployment reported as completed by ECS.")
            return 0
        if rollout_state == "COMPLETED" and deployment_failed:
            print(
                "Deployment appears completed in ECS, but the deployment step failed. Gathering diagnostics..."
            )
            break

        if attempt < args.max_attempts:
            time.sleep(args.poll_delay)

    print("Deployment did not reach a completed state. Gathering diagnostics...")

    if not target_deployment:
        print("No active deployment information was returned for the service.")

    if service_info:
        events = collect_service_events(service_info, limit=5)
        if events:
            print("\nRecent service events:")
            for event in events:
                print(f"  - {event}")

    task_definition_arn = None
    if target_deployment:
        task_definition_arn = target_deployment.get("taskDefinition")

    log_group_mapping = fetch_log_group_mapping(ecs_client, task_definition_arn)
    failed_tasks = list_relevant_tasks(
        ecs_client,
        cluster=args.cluster,
        service=args.service,
        task_definition=task_definition_arn,
    )

    if failed_tasks:
        print("\nFailed or stopped tasks details:")
        for task in failed_tasks:
            summarize_task(task, log_group_mapping, logs_client, args.log_lines)
    else:
        print("\nNo stopped tasks were found for diagnostics.")

    expected_success = args.deployment_outcome == "success"
    if expected_success:
        print("Deployment step reported success, but ECS did not confirm completion.")
    else:
        print(f"Deployment step outcome: {args.deployment_outcome or 'unknown'}")

    return 1


if __name__ == "__main__":
    sys.exit(main())
