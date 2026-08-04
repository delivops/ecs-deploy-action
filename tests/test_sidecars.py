#!/usr/bin/env python3
"""
Unit tests for generic sidecar support in generate_task_def.py.

These cover what the golden-file runner in test.py cannot: configurations that
must be *rejected* (test.py has no mechanism for an expected failure), and the
isolation guarantees, which are easier to assert directly than to eyeball in a
JSON fixture.

boto3 is never imported: every secret here uses an explicit ARN, so no test
needs AWS credentials.
"""

import importlib.util
import sys
from pathlib import Path


def load_module():
    """Import generate_task_def.py by path (same approach as test_roles.py)."""
    script = Path(__file__).parent.parent / "scripts" / "generate_task_def.py"
    spec = importlib.util.spec_from_file_location("generate_task_def", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gtd = load_module()

CLUSTER = "test-cluster"
REGION = "us-east-1"
SERVICE = "test-service"
ROLE = "arn:aws:iam::123456789012:role/ecsTaskExecutionRole"


def base_config(**overrides):
    """A minimal valid config; keyword arguments are merged on top."""
    config = {'cpu': 256, 'memory': 512, 'role_arn': ROLE}
    config.update(overrides)
    return config


def generate(config):
    """Render a task definition from a config dict."""
    return gtd.generate_task_definition(
        config_dict=config,
        cluster_name=CLUSTER,
        aws_region=REGION,
        registry="123456789012.dkr.ecr.us-east-1.amazonaws.com",
        container_registry="123456789012.dkr.ecr.us-east-1.amazonaws.com",
        image_name="test-app",
        tag="latest",
        service_name=SERVICE,
    )


def containers(task_definition):
    """Container definitions keyed by name."""
    return {c['name']: c for c in task_definition['containerDefinitions']}


def volume_names(task_definition):
    return [v['name'] for v in task_definition.get('volumes', [])]


def expect_validation_error(config, *fragments):
    """Assert rendering fails with a ValidationError naming every fragment."""
    try:
        generate(config)
    except gtd.ValidationError as e:
        message = str(e)
        missing = [f for f in fragments if f not in message]
        if missing:
            return False, f"message missing {missing}: {message}"
        return True, message.split('\n')[0][:90]
    except Exception as e:  # noqa: BLE001 - wrong exception type is a failure
        return False, f"raised {type(e).__name__} instead of ValidationError: {e}"
    return False, "no error raised"


# ---------------------------------------------------------------------------
# The motivating case: read-only app + writable ECS Exec bridge
# ---------------------------------------------------------------------------

BRIDGE_CONFIG = base_config(
    port=8080,
    readonly_root_filesystem=True,
    writable_dirs=['/tmp'],
    envs=[{'NODE_ENV': 'production'}],
    secrets=[{'CLIENT_ID': 'arn:aws:secretsmanager::secret:app-prod-IhUQht'}],
    secret_files=['app-certificate'],
    sidecars=[{
        'name': 'ssm-bridge',
        'image': 'public.ecr.aws/amazonlinux/amazonlinux:2023',
        'command': ['sleep', 'infinity'],
        'readonly_root_filesystem': False,
        'linux_parameters': {'init_process_enabled': True},
    }],
)


def test_app_stays_readonly():
    app = containers(generate(BRIDGE_CONFIG))['app']
    if app.get('readonlyRootFilesystem') is not True:
        return False, f"app readonlyRootFilesystem={app.get('readonlyRootFilesystem')}"
    return True, "app readonlyRootFilesystem=True"


def test_bridge_overrides_readonly():
    bridge = containers(generate(BRIDGE_CONFIG))['ssm-bridge']
    if bridge.get('readonlyRootFilesystem') is not False:
        return False, f"ssm-bridge readonlyRootFilesystem={bridge.get('readonlyRootFilesystem')}"
    return True, "explicit false beats the top-level true"


def test_bridge_receives_nothing_from_app():
    """The bridge must not inherit the application's env, secrets or mounts."""
    bridge = containers(generate(BRIDGE_CONFIG))['ssm-bridge']
    leaks = []
    if bridge.get('environment'):
        leaks.append(f"environment={bridge['environment']}")
    if bridge.get('secrets'):
        leaks.append(f"secrets={bridge['secrets']}")
    if bridge.get('mountPoints'):
        leaks.append(f"mountPoints={bridge['mountPoints']}")
    if bridge.get('portMappings'):
        leaks.append(f"portMappings={bridge['portMappings']}")
    if bridge.get('dependsOn'):
        leaks.append(f"dependsOn={bridge['dependsOn']}")
    if leaks:
        return False, "leaked from app: " + "; ".join(leaks)
    return True, "no application env, secrets, mounts, ports or dependencies"


def test_bridge_keeps_empty_skeleton_keys():
    bridge = containers(generate(BRIDGE_CONFIG))['ssm-bridge']
    for key in ('environment', 'secrets', 'entryPoint'):
        if key not in bridge:
            return False, f"missing skeleton key '{key}'"
    return True, "environment/secrets/entryPoint present but empty"


def test_bridge_gets_own_linux_parameters():
    bridge = containers(generate(BRIDGE_CONFIG))['ssm-bridge']
    if bridge.get('linuxParameters') != {'initProcessEnabled': True}:
        return False, f"linuxParameters={bridge.get('linuxParameters')}"
    return True, "initProcessEnabled honored"


def test_app_init_container_untouched_by_sidecars():
    """Adding a sidecar must not change the application's own containers."""
    with_sidecar = containers(generate(BRIDGE_CONFIG))
    without = dict(BRIDGE_CONFIG)
    without.pop('sidecars')
    baseline = containers(generate(without))
    for name, container in baseline.items():
        if with_sidecar.get(name) != container:
            return False, f"container '{name}' changed when a sidecar was added"
    return True, f"{len(baseline)} application container(s) byte-identical"


# ---------------------------------------------------------------------------
# Isolation and per-container defaults
# ---------------------------------------------------------------------------

def test_sidecar_inherits_readonly_when_unset():
    config = base_config(
        readonly_root_filesystem=True,
        sidecars=[{'name': 'tailer', 'image': 'busybox'}],
    )
    tailer = containers(generate(config))['tailer']
    if tailer.get('readonlyRootFilesystem') is not True:
        return False, f"readonlyRootFilesystem={tailer.get('readonlyRootFilesystem')}"
    return True, "falls back to the application value"


def test_readonly_key_omitted_when_unset_everywhere():
    config = base_config(sidecars=[{'name': 'tailer', 'image': 'busybox'}])
    rendered = containers(generate(config))
    present = [n for n, c in rendered.items() if 'readonlyRootFilesystem' in c]
    if present:
        return False, f"readonlyRootFilesystem emitted for {present}"
    return True, "key omitted entirely, as before"


def test_sidecar_defaults_to_essential():
    config = base_config(sidecars=[{'name': 'tailer', 'image': 'busybox'}])
    if containers(generate(config))['tailer']['essential'] is not True:
        return False, "essential did not default to True"
    return True, "essential defaults to True (the ECS default)"


def test_sidecar_gets_own_env_secrets_files_and_mounts():
    config = base_config(
        envs=[{'APP_ONLY': 'yes'}],
        sidecars=[{
            'name': 'metrics',
            'image': 'metrics:1',
            'envs': [{'AGENT_MODE': 'push'}],
            'secrets_envs': [{
                'id': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:m-abc',
                'values': ['TOKEN'],
            }],
            'secret_files': ['client-cert'],
            'writable_dirs': ['/tmp'],
        }],
    )
    rendered = containers(generate(config))
    metrics = rendered['metrics']

    if [e['name'] for e in metrics['environment']] != ['AGENT_MODE']:
        return False, f"environment={metrics['environment']}"
    if [s['name'] for s in metrics['secrets']] != ['TOKEN']:
        return False, f"secrets={metrics['secrets']}"
    if 'metrics-secret-init' not in rendered:
        return False, "no init container generated for the sidecar's secret_files"
    mounts = {m['sourceVolume'] for m in metrics['mountPoints']}
    if mounts != {'metrics-secrets', 'metrics-writable-tmp'}:
        return False, f"mountPoints={mounts}"
    if metrics['dependsOn'] != [{'containerName': 'metrics-secret-init',
                                 'condition': 'SUCCESS'}]:
        return False, f"dependsOn={metrics['dependsOn']}"

    app = rendered['app']
    if [e['name'] for e in app['environment']] != ['APP_ONLY']:
        return False, f"app environment polluted: {app['environment']}"
    if app.get('mountPoints'):
        return False, f"app gained the sidecar's mounts: {app['mountPoints']}"
    return True, "sidecar env/secrets/files/mounts are its own"


def test_secrets_are_never_resolved_to_plaintext():
    config = base_config(sidecars=[{
        'name': 'metrics',
        'image': 'metrics:1',
        'secrets': [{'TOKEN': 'arn:aws:secretsmanager::secret:tok-AbCdEf'}],
    }])
    secrets = containers(generate(config))['metrics']['secrets']
    if secrets != [{'name': 'TOKEN',
                    'valueFrom': 'arn:aws:secretsmanager::secret:tok-AbCdEf:TOKEN::'}]:
        return False, f"secrets={secrets}"
    return True, "stays a valueFrom reference"


def test_app_writable_dirs_not_copied_into_sidecar():
    config = base_config(
        writable_dirs=['/tmp', '/var/run'],
        sidecars=[{'name': 'tailer', 'image': 'busybox'}],
    )
    rendered = generate(config)
    tailer = containers(rendered)['tailer']
    if tailer.get('mountPoints'):
        return False, f"sidecar mounted application volumes: {tailer['mountPoints']}"
    if volume_names(rendered) != ['writable-tmp', 'writable-var-run']:
        return False, f"volumes={volume_names(rendered)}"
    return True, "application writable_dirs stay with the application"


def test_two_sidecars_sharing_tmp_get_distinct_volumes():
    config = base_config(
        writable_dirs=['/tmp'],
        sidecars=[
            {'name': 'cache', 'image': 'redis', 'writable_dirs': ['/tmp']},
            {'name': 'shipper', 'image': 'shipper', 'writable_dirs': ['/tmp']},
        ],
    )
    rendered = generate(config)
    expected = ['writable-tmp', 'cache-writable-tmp', 'shipper-writable-tmp']
    if volume_names(rendered) != expected:
        return False, f"volumes={volume_names(rendered)}"

    rendered_containers = containers(rendered)
    for name, volume in (('cache', 'cache-writable-tmp'),
                         ('shipper', 'shipper-writable-tmp')):
        mounts = rendered_containers[name]['mountPoints']
        if mounts != [{'sourceVolume': volume, 'containerPath': '/tmp'}]:
            return False, f"{name} mountPoints={mounts}"
    return True, "three distinct /tmp volumes"


def test_sidecar_logs_to_service_log_group_with_name_prefix():
    config = base_config(sidecars=[
        {'name': 'tailer', 'image': 'busybox'},
        {'name': 'shipper', 'image': 'busybox', 'log_stream_prefix': 'custom'},
    ])
    rendered = containers(generate(config))
    options = rendered['tailer']['logConfiguration']['options']
    if options['awslogs-group'] != f"/ecs/{CLUSTER}/{SERVICE}":
        return False, f"awslogs-group={options['awslogs-group']}"
    if options['awslogs-stream-prefix'] != 'tailer':
        return False, f"awslogs-stream-prefix={options['awslogs-stream-prefix']}"
    custom = rendered['shipper']['logConfiguration']['options']
    if custom['awslogs-stream-prefix'] != 'custom':
        return False, f"custom prefix ignored: {custom['awslogs-stream-prefix']}"
    return True, "service log group, sidecar name as default prefix"


def test_sidecar_stays_on_awslogs_under_fluent_bit():
    config = base_config(
        fluent_bit_collector={'image_name': 'fluent-bit:latest'},
        sidecars=[{'name': 'tailer', 'image': 'busybox'}],
    )
    rendered = containers(generate(config))
    if rendered['app']['logConfiguration']['logDriver'] != 'awsfirelens':
        return False, "application did not switch to awsfirelens"
    driver = rendered['tailer']['logConfiguration']['logDriver']
    if driver != 'awslogs':
        return False, f"sidecar logDriver={driver}"
    return True, "sidecar keeps awslogs, like fluent-bit and otel do"


def test_sidecar_reservations_are_integers():
    config = base_config(sidecars=[{
        'name': 'metrics', 'image': 'metrics:1',
        'cpu': 128, 'memory': 256, 'memory_reservation': 128,
    }])
    metrics = containers(generate(config))['metrics']
    for key, expected in (('cpu', 128), ('memory', 256), ('memoryReservation', 128)):
        if metrics.get(key) != expected or not isinstance(metrics.get(key), int):
            return False, f"{key}={metrics.get(key)!r}"
    return True, "cpu/memory/memoryReservation emitted as ints"


def test_sidecar_port_mapping_name_is_unique():
    config = base_config(
        port=8080,
        sidecars=[{'name': 'metrics', 'image': 'metrics:1', 'port': 9090}],
    )
    rendered = containers(generate(config))
    app_names = [p['name'] for p in rendered['app']['portMappings']]
    sidecar_names = [p['name'] for p in rendered['metrics']['portMappings']]
    if app_names != ['default']:
        return False, f"application port names changed: {app_names}"
    if sidecar_names != ['metrics-9090-tcp']:
        return False, f"sidecar port names={sidecar_names}"
    return True, "default vs metrics-9090-tcp"


def test_sidecar_ordering():
    config = base_config(
        secret_files=['app-cert'],
        fluent_bit_collector={'image_name': 'fluent-bit:latest'},
        otel_collector={'image_name': ''},
        sidecars=[
            {'name': 'metrics', 'image': 'm:1', 'secret_files': ['cert']},
            {'name': 'tailer', 'image': 't:1'},
        ],
    )
    names = [c['name'] for c in generate(config)['containerDefinitions']]
    expected = ['init-container-for-secret-files', 'app', 'fluent-bit',
                'otel-collector', 'metrics-secret-init', 'metrics', 'tailer']
    if names != expected:
        return False, f"order={names}"
    return True, "application containers first, then sidecars in order"


# ---------------------------------------------------------------------------
# services_overrides
# ---------------------------------------------------------------------------

OVERRIDE_CONFIG = {
    'cpu': 256, 'memory': 512, 'role_arn': ROLE,
    'sidecars': [
        {'name': 'ssm-bridge', 'image': 'amazonlinux:2023',
         'command': ['sleep', 'infinity']},
        {'name': 'cache', 'image': 'redis:7', 'memory_reservation': 64,
         'envs': [{'REDIS_MAXMEMORY': '32mb'}]},
    ],
    'services_overrides': {
        SERVICE: {
            'sidecars': [
                {'name': 'ssm-bridge', 'enabled': False},
                {'name': 'cache', 'memory_reservation': 128,
                 'envs': [{'REDIS_APPENDONLY': 'no'}]},
                {'name': 'audit-tailer', 'image': 'audit:v1', 'essential': False},
            ],
        },
        'other-service': {},
    },
}


def merged_for(service):
    return gtd.apply_service_overrides(OVERRIDE_CONFIG, service)


def test_override_disables_sidecar():
    merged = merged_for(SERVICE)
    names = [s['name'] for s in gtd.enabled_sidecars(merged)]
    if 'ssm-bridge' in names:
        return False, f"ssm-bridge still enabled: {names}"
    if len(merged['sidecars']) != 3:
        return False, f"disabled sidecar was dropped from the merged config instead of flagged"
    return True, "enabled: false removes it from the task definition"


def test_override_merges_by_name_not_append():
    cache = next(s for s in merged_for(SERVICE)['sidecars'] if s['name'] == 'cache')
    if cache['memory_reservation'] != 128:
        return False, f"scalar not replaced: {cache['memory_reservation']}"
    if cache['image'] != 'redis:7':
        return False, f"base image lost: {cache['image']}"
    keys = [k for e in cache['envs'] for k in e]
    if keys != ['REDIS_MAXMEMORY', 'REDIS_APPENDONLY']:
        return False, f"envs did not extend: {keys}"
    return True, "scalars replace, arrays extend, base fields preserved"


def test_override_appends_new_sidecar():
    names = [s['name'] for s in merged_for(SERVICE)['sidecars']]
    if names != ['ssm-bridge', 'cache', 'audit-tailer']:
        return False, f"order/content={names}"
    return True, "unmatched names appended, base order preserved"


def test_other_service_keeps_base_sidecars():
    names = [s['name'] for s in gtd.enabled_sidecars(merged_for('other-service'))]
    if names != ['ssm-bridge', 'cache']:
        return False, f"base config affected: {names}"
    return True, "a service with no sidecar override is unaffected"


def test_override_null_removes_all_sidecars():
    config = dict(OVERRIDE_CONFIG)
    config['services_overrides'] = {SERVICE: {'sidecars': None}}
    if 'sidecars' in merged_for_config(config):
        return False, "sidecars survived an explicit null"
    return True, "sidecars: null is the per-service escape hatch"


def merged_for_config(config):
    return gtd.apply_service_overrides(config, SERVICE)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_rejects_non_list_sidecars():
    return expect_validation_error(base_config(sidecars='oops'),
                                   'sidecars must be a list', 'str')


def test_rejects_non_mapping_entry():
    return expect_validation_error(base_config(sidecars=['oops']),
                                   'sidecars[0] must be a mapping')


def test_rejects_missing_name():
    return expect_validation_error(base_config(sidecars=[{'image': 'busybox'}]),
                                   "'name' is required")


def test_rejects_missing_image():
    return expect_validation_error(base_config(sidecars=[{'name': 'tailer'}]),
                                   "'image' is required")


def test_rejects_unknown_key():
    config = base_config(sidecars=[{
        'name': 'tailer', 'image': 'busybox', 'readonly_root_filesytem': False,
    }])
    return expect_validation_error(config, 'unsupported key',
                                   'readonly_root_filesytem')


def test_rejects_duplicate_names():
    config = base_config(sidecars=[
        {'name': 'tailer', 'image': 'a'},
        {'name': 'tailer', 'image': 'b'},
    ])
    return expect_validation_error(config, "Duplicate container name 'tailer'")


def test_rejects_reserved_names():
    for reserved in ('app', 'fluent-bit', 'otel-collector',
                     'init-container-for-secret-files', 'default'):
        config = base_config(sidecars=[{'name': reserved, 'image': 'busybox'}])
        passed, detail = expect_validation_error(
            config, f"Duplicate container name '{reserved}'", 'reserved')
        if not passed:
            return False, f"{reserved}: {detail}"
    return True, "app, fluent-bit, otel-collector, init container and default"


def test_rejects_collision_with_generated_init_name():
    config = base_config(sidecars=[
        {'name': 'metrics', 'image': 'a', 'secret_files': ['cert']},
        {'name': 'metrics-secret-init', 'image': 'b'},
    ])
    return expect_validation_error(
        config, "Duplicate container name 'metrics-secret-init'")


def test_rejects_volume_collision_with_application():
    """A sidecar named 'writable' with secret files generates writable-secrets,
    which is also what the application's writable_dirs: [/secrets] produces."""
    config = base_config(
        writable_dirs=['/secrets'],
        sidecars=[{'name': 'writable', 'image': 'busybox',
                   'secret_files': ['cert']}],
    )
    return expect_validation_error(config, "'writable-secrets'",
                                   'application-level volume')


def test_rejects_invalid_generated_volume_name():
    config = base_config(sidecars=[{
        'name': 'cache', 'image': 'busybox', 'writable_dirs': ['/var/lib/.cache'],
    }])
    return expect_validation_error(config, 'not a valid ECS volume name',
                                   'cache-writable-var-lib-.cache')


def test_rejects_invalid_sidecar_name():
    config = base_config(sidecars=[{'name': '-bad name', 'image': 'busybox'}])
    return expect_validation_error(config, 'invalid container name')


def test_rejects_duplicate_port_mapping_name():
    config = base_config(
        port=8080,
        additional_ports=[{'admin': 8081}],
        sidecars=[{'name': 'metrics', 'image': 'm:1',
                   'additional_ports': [{'admin': 9091}]}],
    )
    return expect_validation_error(config, "Duplicate port mapping name 'admin'")


def test_rejects_memory_reservation_above_memory():
    config = base_config(sidecars=[{
        'name': 'metrics', 'image': 'm:1', 'memory': 128, 'memory_reservation': 256,
    }])
    return expect_validation_error(config, 'memory_reservation', 'must not exceed')


def test_rejects_wrong_types():
    cases = [
        ({'envs': 'NODE_ENV=x'}, "'envs' must be a list"),
        ({'health_check': ['curl']}, "'health_check' must be a mapping"),
        ({'image': 42}, "'image' is required and must be a string"),
        ({'essential': 'yes'}, "'essential' must be true or false"),
        ({'port': 0}, "'port' must be a positive integer"),
        ({'port': True}, "'port' must be a positive integer"),
        ({'cpu': '128'}, "'cpu' must be a positive integer"),
    ]
    for extra, fragment in cases:
        sidecar = {'name': 'tailer', 'image': 'busybox'}
        sidecar.update(extra)
        passed, detail = expect_validation_error(base_config(sidecars=[sidecar]),
                                                 fragment)
        if not passed:
            return False, f"{extra}: {detail}"
    return True, f"{len(cases)} type errors reported clearly"


def test_only_explicit_false_disables():
    """`enabled:` with an empty value parses as None and must not drop it."""
    config = base_config(sidecars=[
        {'name': 'kept-null', 'image': 'busybox', 'enabled': None},
        {'name': 'kept-true', 'image': 'busybox', 'enabled': True},
        {'name': 'dropped', 'image': 'busybox', 'enabled': False},
    ])
    names = [c['name'] for c in generate(config)['containerDefinitions']]
    if names != ['app', 'kept-null', 'kept-true']:
        return False, f"containers={names}"
    return True, "only enabled: false removes a sidecar"


def test_app_only_duplicate_port_names_still_allowed():
    """Pre-existing app-level duplicates are not this feature's business."""
    config = base_config(
        additional_ports=[{'admin': 8081}, {'admin': 8082}],
        sidecars=[{'name': 'tailer', 'image': 'busybox'}],
    )
    try:
        generate(config)
    except gtd.ValidationError as e:
        return False, f"rejected a pre-existing config: {e}"
    return True, "only collisions involving a sidecar are reported"


def test_rejects_sidecars_exceeding_task_memory():
    config = base_config(
        memory=512,
        sidecars=[{'name': 'a', 'image': 'i', 'memory': 256},
                  {'name': 'b', 'image': 'i', 'memory': 256}],
    )
    return expect_validation_error(config, 'Sidecars reserve 512 MiB',
                                   'a=256, b=256', 'only has 512')


def test_rejects_sidecars_exceeding_task_cpu():
    config = base_config(cpu=256, sidecars=[{'name': 'a', 'image': 'i', 'cpu': 256}])
    return expect_validation_error(config, 'CPU units', 'only has 256')


def test_resource_budget_not_enforced_on_ec2():
    config = base_config(
        launch_type='EC2', cpu=256, memory=512,
        sidecars=[{'name': 'a', 'image': 'i', 'memory': 4096}],
    )
    try:
        generate(config)
    except gtd.ValidationError as e:
        return False, f"EC2 task rejected: {e}"
    return True, "EC2 task-level values are advisory"


def test_disabled_sidecar_frees_the_resource_budget():
    config = base_config(
        memory=512,
        sidecars=[{'name': 'a', 'image': 'i', 'memory': 4096, 'enabled': False}],
    )
    try:
        generate(config)
    except gtd.ValidationError as e:
        return False, f"disabled sidecar still counted: {e}"
    return True, "a disabled sidecar reserves nothing"


# ---------------------------------------------------------------------------
# Explicit YAML nulls. `command:` with nothing after it parses as None, and
# validation treats that as "unset" - so every consumer has to agree.
# ---------------------------------------------------------------------------

def test_blank_list_keys_do_not_crash():
    for key in ('command', 'entrypoint', 'envs', 'envs_from_files', 'secrets',
                'secrets_envs', 'secret_files', 'writable_dirs', 'additional_ports'):
        sidecar = {'name': 'tailer', 'image': 'busybox', key: None}
        try:
            rendered = containers(generate(base_config(sidecars=[sidecar])))['tailer']
        except gtd.ValidationError as e:
            return False, f"{key}: rejected a blank value: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"{key}: crashed with {type(e).__name__}: {e}"
        for emitted, value in rendered.items():
            if value is None:
                return False, f"{key}: emitted null for '{emitted}'"
    return True, "9 blank list keys behave as absent"


def test_blank_command_does_not_emit_null():
    """`"command": null` is rejected by RegisterTaskDefinition, not by us."""
    config = base_config(sidecars=[{'name': 't', 'image': 'busybox', 'command': None}])
    tailer = containers(generate(config))['t']
    if tailer['command'] != [] or tailer['entryPoint'] != []:
        return False, f"command={tailer['command']!r} entryPoint={tailer['entryPoint']!r}"
    return True, "blank command renders as []"


def test_blank_essential_stays_essential():
    """bool(None) is False - a blank `essential:` must not silently disarm it."""
    config = base_config(sidecars=[{'name': 't', 'image': 'busybox', 'essential': None}])
    if containers(generate(config))['t']['essential'] is not True:
        return False, "a blank essential: made the sidecar non-essential"
    return True, "blank essential: still means essential"


def test_disabled_sidecar_env_file_is_not_resolved(tmp_yaml=None):
    """Turning a sidecar off must let its dotenv file be deleted with it."""
    import tempfile
    import textwrap
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'task.yaml'
        path.write_text(textwrap.dedent(f"""
            cpu: 256
            memory: 512
            role_arn: {ROLE}
            sidecars:
              - name: legacy-agent
                image: acme/agent:1.0
                enabled: false
                envs_from_files:
                  - ./deleted-when-the-sidecar-was.env
        """))
        try:
            config = gtd.load_and_validate_config(str(path), SERVICE)
        except gtd.ValidationError as e:
            return False, f"disabled sidecar still resolved its env file: {e}"
    if gtd.enabled_sidecars(config):
        return False, "the disabled sidecar was rendered"
    return True, "a switched-off sidecar resolves nothing"


# ---------------------------------------------------------------------------
# Port and log-stream namespaces, which are task-wide rather than per container
# ---------------------------------------------------------------------------

def test_rejects_duplicate_container_port_under_awsvpc():
    config = base_config(
        port=8080,
        sidecars=[{'name': 'proxy', 'image': 'nginx', 'port': 8080}],
    )
    return expect_validation_error(config, 'Duplicate container port 8080',
                                   'same port')


def test_allows_duplicate_container_port_under_bridge():
    """Bridge mode assigns host ports dynamically, so this is legal."""
    config = base_config(
        launch_type='EC2', network_mode='bridge', port=8080,
        sidecars=[{'name': 'proxy', 'image': 'nginx', 'port': 8080}],
    )
    try:
        generate(config)
    except gtd.ValidationError as e:
        return False, f"rejected a legal bridge-mode config: {e}"
    return True, "bridge mode allows it"


def test_rejects_port_colliding_with_otel_collector():
    config = base_config(
        otel_collector={'image_name': ''},
        sidecars=[{'name': 'tap', 'image': 't:1', 'port': 4317}],
    )
    return expect_validation_error(config, 'Duplicate container port 4317',
                                   'otel-collector')


def test_rejects_invalid_generated_port_name():
    """ECS port names are lowercase-only, unlike container names."""
    config = base_config(sidecars=[{'name': 'My_Proxy', 'image': 'n', 'port': 8080}])
    return expect_validation_error(config, 'Invalid port mapping name',
                                   'My_Proxy-8080-tcp')


def test_rejects_overlong_generated_names():
    long_name = 'a' * 250
    config = base_config(sidecars=[{'name': long_name, 'image': 'n',
                                    'secret_files': ['cert']}])
    return expect_validation_error(config, 'generated container name', 'too long')


def test_rejects_reserved_log_stream_prefix():
    config = base_config(sidecars=[{'name': 'proxy', 'image': 'n',
                                    'log_stream_prefix': 'default'}])
    return expect_validation_error(config, "log_stream_prefix 'default'", 'reserved')


def test_rejects_malformed_list_entries():
    cases = [
        ({'envs': ['FOO=bar']}, 'envs entries must be single-key mappings'),
        ({'additional_ports': [9090]}, 'additional_ports entries must be mappings'),
        ({'additional_ports': [{'admin': 'nine'}]}, 'must be a positive integer'),
    ]
    for extra, fragment in cases:
        sidecar = {'name': 'tailer', 'image': 'busybox'}
        sidecar.update(extra)
        passed, detail = expect_validation_error(base_config(sidecars=[sidecar]),
                                                 fragment)
        if not passed:
            return False, f"{extra}: {detail}"
    return True, "malformed entries rejected, not silently dropped"


def test_disabled_sidecar_is_still_validated():
    config = base_config(sidecars=[{
        'name': 'tailer', 'image': 'busybox', 'enabled': False, 'typo': 1,
    }])
    return expect_validation_error(config, 'unsupported key', 'typo')


def test_disabled_sidecar_frees_its_names():
    """Switching one off must not make its name collide with a replacement."""
    config = base_config(sidecars=[
        {'name': 'cache', 'image': 'old', 'enabled': False, 'writable_dirs': ['/tmp']},
        {'name': 'cache-writable-tmp', 'image': 'new'},
    ])
    names = [c['name'] for c in generate(config)['containerDefinitions']]
    if names != ['app', 'cache-writable-tmp']:
        return False, f"containers={names}"
    return True, "a disabled sidecar reserves nothing"


def test_unexpanded_envs_from_files_fails_loudly():
    config = base_config(sidecars=[{
        'name': 'tailer', 'image': 'busybox', 'envs_from_files': ['./x.env'],
    }])
    return expect_validation_error(config, 'envs_from_files was not expanded')


def test_boto3_never_imported():
    if 'boto3' in sys.modules:
        return False, "boto3 was imported - a test is hitting AWS"
    return True, "no AWS calls"


TESTS = [
    ("readonly bridge: app stays read-only", test_app_stays_readonly),
    ("readonly bridge: sidecar overrides to false", test_bridge_overrides_readonly),
    ("readonly bridge: sidecar inherits nothing from app", test_bridge_receives_nothing_from_app),
    ("readonly bridge: empty skeleton keys emitted", test_bridge_keeps_empty_skeleton_keys),
    ("readonly bridge: own linux_parameters", test_bridge_gets_own_linux_parameters),
    ("adding a sidecar does not touch app containers", test_app_init_container_untouched_by_sidecars),
    ("readonly: inherited when sidecar is silent", test_sidecar_inherits_readonly_when_unset),
    ("readonly: key omitted when unset everywhere", test_readonly_key_omitted_when_unset_everywhere),
    ("essential defaults to true", test_sidecar_defaults_to_essential),
    ("sidecar gets its own env/secrets/files/mounts", test_sidecar_gets_own_env_secrets_files_and_mounts),
    ("secrets stay valueFrom references", test_secrets_are_never_resolved_to_plaintext),
    ("app writable_dirs not copied into sidecars", test_app_writable_dirs_not_copied_into_sidecar),
    ("two sidecars on /tmp get distinct volumes", test_two_sidecars_sharing_tmp_get_distinct_volumes),
    ("logging: service log group, name as prefix", test_sidecar_logs_to_service_log_group_with_name_prefix),
    ("logging: sidecars stay on awslogs under fluent-bit", test_sidecar_stays_on_awslogs_under_fluent_bit),
    ("cpu/memory reservations emitted as ints", test_sidecar_reservations_are_integers),
    ("port mapping names stay unique", test_sidecar_port_mapping_name_is_unique),
    ("container ordering", test_sidecar_ordering),
    ("overrides: enabled false disables", test_override_disables_sidecar),
    ("overrides: merge by name, not append", test_override_merges_by_name_not_append),
    ("overrides: new sidecar appended", test_override_appends_new_sidecar),
    ("overrides: other services unaffected", test_other_service_keeps_base_sidecars),
    ("overrides: null removes all sidecars", test_override_null_removes_all_sidecars),
    ("reject: sidecars not a list", test_rejects_non_list_sidecars),
    ("reject: entry not a mapping", test_rejects_non_mapping_entry),
    ("reject: missing name", test_rejects_missing_name),
    ("reject: missing image", test_rejects_missing_image),
    ("reject: unknown/misspelled key", test_rejects_unknown_key),
    ("reject: duplicate names", test_rejects_duplicate_names),
    ("reject: reserved names", test_rejects_reserved_names),
    ("reject: collision with generated init name", test_rejects_collision_with_generated_init_name),
    ("reject: volume collision with application", test_rejects_volume_collision_with_application),
    ("reject: invalid generated volume name", test_rejects_invalid_generated_volume_name),
    ("reject: invalid sidecar name", test_rejects_invalid_sidecar_name),
    ("reject: duplicate port mapping name", test_rejects_duplicate_port_mapping_name),
    ("reject: memory_reservation above memory", test_rejects_memory_reservation_above_memory),
    ("reject: wrong types", test_rejects_wrong_types),
    ("only enabled: false disables", test_only_explicit_false_disables),
    ("app-only duplicate port names still allowed", test_app_only_duplicate_port_names_still_allowed),
    ("reject: sidecars exceed task memory", test_rejects_sidecars_exceeding_task_memory),
    ("reject: sidecars exceed task cpu", test_rejects_sidecars_exceeding_task_cpu),
    ("resource budget not enforced on EC2", test_resource_budget_not_enforced_on_ec2),
    ("disabled sidecar frees the resource budget", test_disabled_sidecar_frees_the_resource_budget),
    ("null: blank list keys behave as absent", test_blank_list_keys_do_not_crash),
    ("null: blank command renders as []", test_blank_command_does_not_emit_null),
    ("null: blank essential stays essential", test_blank_essential_stays_essential),
    ("null: disabled sidecar resolves no env file", test_disabled_sidecar_env_file_is_not_resolved),
    ("reject: duplicate container port (awsvpc)", test_rejects_duplicate_container_port_under_awsvpc),
    ("allow: duplicate container port (bridge)", test_allows_duplicate_container_port_under_bridge),
    ("reject: port colliding with otel-collector", test_rejects_port_colliding_with_otel_collector),
    ("reject: invalid generated port name", test_rejects_invalid_generated_port_name),
    ("reject: overlong generated container name", test_rejects_overlong_generated_names),
    ("reject: reserved log_stream_prefix", test_rejects_reserved_log_stream_prefix),
    ("reject: malformed envs/additional_ports entries", test_rejects_malformed_list_entries),
    ("disabled sidecar is still validated", test_disabled_sidecar_is_still_validated),
    ("disabled sidecar reserves no names", test_disabled_sidecar_frees_its_names),
    ("unexpanded envs_from_files fails loudly", test_unexpanded_envs_from_files_fails_loudly),
    ("no AWS calls", test_boto3_never_imported),
]


def main():
    print("=" * 60)
    print("SIDECAR TESTS")
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
    print("\n🎉 All sidecar tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
