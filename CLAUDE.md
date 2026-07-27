# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

A **composite GitHub Action** (`action.yml`) that deploys to Amazon ECS. There is no build
step and nothing is compiled or published — consumers reference the repo by tag
(`delivops/ecs-deploy-action@v1`), so `action.yml` and `scripts/` *are* the shipped artifact.

The real logic lives in one Python script: `scripts/generate_task_def.py` (~1600 lines). It turns a
simplified YAML config into a full ECS task definition JSON. Everything else in `action.yml` is AWS
plumbing around it.

## Layout

| Path | Role |
|---|---|
| `action.yml` | composite action: assume role → ECR login → pip install → generate task def → deploy |
| `scripts/generate_task_def.py` | YAML → task definition JSON. All validation and business logic. |
| `scripts/generate_readme_docs.py` | **Stale. Do not run.** See "Dead code" below. |
| `examples/*.yaml` | one YAML per feature; doubles as the test corpus |
| `tests/expected_outputs/*.json` | golden output, one per example, same stem |
| `tests/test.py` | golden-file runner over `examples/` |
| `tests/test_roles.py` | unit tests for `RoleResolver` + `emit_replica_count` (stubs boto3) |
| `docs/*.md` | per-feature documentation, linked from `README.md` and `docs/examples.md` |

## Commands

```bash
pip install -r requirements.txt

python3 tests/test_roles.py    # 39 unit tests, no AWS needed
python3 tests/test.py          # 31 golden-file comparisons

# Regenerate goldens after an intentional output change, then review the diff:
UPDATE_EXPECTED=1 python3 tests/test.py
```

Both suites run on every PR via `.github/workflows/test-and-update.yml`. There is no linter or
formatter configured.

## The golden-file contract

This is the single most important thing to know before touching `generate_task_def.py`.

- Every `examples/<name>.yaml` **must** have a committed `tests/expected_outputs/<name>.json`.
  A missing golden is a hard failure, deliberately: a new example that generated its own
  expectation on first run would pass vacuously and could never catch a regression.
- **CI never sets `UPDATE_EXPECTED`.** Regenerate locally, read the diff, commit it.
- Any change to generated output — even reordering JSON keys — breaks all 31 goldens. If a diff
  is larger than you expected, that is the signal to re-check the change, not to bulk-regenerate.
- Adding a feature means: code + `examples/<feature>.yaml` + regenerated golden + a `docs/` page.
- The workflow also runs `stefanzweifel/git-auto-commit-action` on `tests/expected_outputs/*.json`,
  so a golden drift can land as an auto-commit on the PR branch. Don't rely on it — commit goldens
  yourself.

`tests/test.py` parses the JSON the script prints to stdout between `----- Task Definition -----`
markers. Keep stdout clean: **all logging goes to stderr** (`setup_logging`), and stdout carries
only the task definition. Printing anything else to stdout breaks the test runner.

## Deployment types

`deployment_type` selects which half of `action.yml` runs:

- `service` — `amazon-ecs-deploy-task-definition` deploys and waits for stability, then
  `check-service-deployment` re-reads the service and fails if the live task def ARN doesn't match.
- `scheduled_task` — registers the task def, then rewrites **only**
  `EcsParameters.TaskDefinitionArn` on the EventBridge target `<cluster>-<task_name>`.
- `triggerable_task` — registers the task def and stops. Nothing runs it; the caller does.

Infrastructure (EventBridge rules, networking, IAM roles, tags) is owned by Terraform —
specifically `delivops/terraform-aws-ecs-service`. This action only ever updates task definitions.
Preserving Terraform's settings is a recurring constraint, not an incidental one:

- `propagate-tags` is passed explicitly on service deploys because the upstream action always
  sends `propagateTags`, and an unset value resets the service to `NONE`.
- The EventBridge step uses a read-modify-write on the existing target so `TagList` and
  `PropagateTags` survive.

## Conventions in `generate_task_def.py`

- **Two exception types, on purpose.** `ValidationError` = the config is wrong.
  `RoleResolutionError` = the config may be fine but the environment isn't (missing SSM parameter,
  no credentials). Do not make one a subclass of the other; the distinction is what keeps
  environmental faults from being reported as "validation failed".
- **`RoleResolver` never falls back to a mock value.** Any unresolved slot or AWS fault is a hard
  failure — a task definition registered with the wrong IAM role is worse than a failed deploy.
  `SecretManager.discover_secret_keys` *does* fall back to mock keys so local/offline runs work.
  That asymmetry is intentional; don't "fix" it in either direction.
- **boto3 is imported lazily** inside `RoleResolver._resolve_from_ssm`. A fully YAML-configured
  deploy must make no AWS calls and need no credentials — `test_yaml_only_stays_offline` asserts
  boto3 was never imported. Keep boto3 imports out of module scope.
- **`emit_replica_count` writes to `$GITHUB_OUTPUT`**, not `::set-output` (disabled by GitHub in
  2023). Its integer check is deliberately stricter than `int()`: `int()` accepts `"5_0"` as 50 and
  non-ASCII digits, and a newline would let a config forge extra step outputs.
- Merge semantics for `services_overrides` are driven by two module-level sets, `ARRAY_FIELDS`
  (appended) and `OBJECT_FIELDS` (replaced). A new array-valued YAML field must be added to
  `ARRAY_FIELDS` or it will silently replace instead of append.
- Explicit YAML `null` in an override *removes* the key so resolution falls through. Watch the
  YAML 1.1 boolean trap: bare `no`/`off` parse as `False`, which is why `normalize_role_value`
  rejects booleans with a specific message.
- Sidecar images (OTEL, Fluent Bit) **always** come from the ECR `registry` argument. Only the main
  app image honors the `ecr_registry` input via `container_registry`.

## Known issues

- **`environment` is a required action input that no step reads.** It is documentation-only today.
  Removing it would break callers; leaving it is confusing. Don't wire it into anything without
  being asked.
- **`examples/full-example-available.yaml` sets `otel_collector.image_name` twice**, so YAML
  last-wins produces the nonsensical
  `…dkr.ecr.us-east-1.amazonaws.com/public.ecr.aws/aws-observability/aws-otel-collector:v0.30.0`
  in its committed golden. Fixing it means regenerating that golden.

## Dead code

`scripts/generate_readme_docs.py` regenerates a "Complete YAML Configuration Example" section in
`README.md` between `<start dynamic>` markers. Those markers and that section were **deliberately
removed** in #42 ("Remove broken dynamic section from README"). No workflow calls the script, and
running it would re-append the removed section. Treat it as unused; don't run it to "update" docs.

## Docs

`docs/` is hand-written. When changing behavior, update the matching page — several pages had
drifted from the code before (Fargate CPU tiers, `ecr_registry` semantics for sidecars). Cross-doc
links are relative and within `docs/`, so link siblings as `roles.md`, not `docs/roles.md`.
