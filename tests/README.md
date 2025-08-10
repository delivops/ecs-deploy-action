# Test Suite

This directory contains the test suite for the ECS Deploy Action.

## Overview

The test suite:

1. Reads each YAML file from the `examples/` directory
2. Executes the `scripts/generate_task_def.py` script with default parameters
3. Compares the output against expected JSON files in `tests/expected_outputs/`
4. Creates expected JSON files if they don't exist

## Files

- `test.py` - The main test script that does all the work
- `expected_outputs/` - Directory containing expected JSON outputs for each YAML example

## Usage

### Local Testing

```bash
# Run tests
python tests/test.py
```

### CI/CD Integration

The GitHub Actions workflow (`.github/workflows/test-and-update.yml`) automatically:

1. Runs on PRs that modify YAML examples or the generator script
2. Executes the tests
3. Updates expected output files if they change
4. Commits and pushes the changes back to the PR branch
5. Comments on the PR to notify about the updates

## How It Works

The test script uses these default parameters:

- `cluster_name`: "test-cluster"
- `aws_region`: "us-east-1" 
- `registry`: "123456789012.dkr.ecr.us-east-1.amazonaws.com"
- `container_registry`: "123456789012.dkr.ecr.us-east-1.amazonaws.com"
- `image_name`: "test-app"
- `tag`: "latest"
- `service_name`: "test-service"

For each YAML file in `examples/`, it runs:

```bash
python scripts/generate_task_def.py <yaml_file> test-cluster us-east-1 123456789012.dkr.ecr.us-east-1.amazonaws.com 123456789012.dkr.ecr.us-east-1.amazonaws.com test-app latest test-service
```

## Benefits

- **Direct**: No complex test frameworks or logic
- **Fast**: Direct script execution without pytest overhead
- **Clear**: Easy to understand what's being tested
- **Automatic**: Expected outputs are auto-generated and updated
- **Reliable**: If the script fails to parse a YAML, the test fails immediately
