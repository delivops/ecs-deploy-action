#!/usr/bin/env python3
"""
Auto-generate README documentation with full YAML examples and task definitions.
Similar to terraform-docs, this script keeps documentation in sync with code.
"""

import os
import json
import yaml
import subprocess
import tempfile
from pathlib import Path

def create_full_example_yaml():
    """Create a comprehensive YAML example with all available options."""
    return {
        "# Full Example Configuration": None,
        "# This YAML demonstrates ALL available configuration options": None,
        "replica_count": 3,
        "cpu": 1024,
        "memory": 2048,
        "cpu_arch": "X86_64",
        "role_arn": "arn:aws:iam::123456789012:role/ecsTaskExecutionRole",
        
        "# Container Configuration": None,
        "port": 8080,
        "additional_ports": [
            {"metrics": 9090},
            {"health": 8081},
            {"admin": 8082}
        ],
        "command": ["npm", "start"],
        "entrypoint": ["/usr/local/bin/docker-entrypoint.sh"],
        
        "# Health Check": None,
        "health_check": {
            "command": "curl -f http://localhost:8080/health || exit 1",
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "start_period": 60
        },
        
        "# Environment Variables": None,
        "envs": [
            {"NODE_ENV": "production"},
            {"API_VERSION": "v1"},
            {"LOG_LEVEL": "info"},
            {"MAX_CONNECTIONS": 100},
            {"TIMEOUT_SECONDS": 30},
            {"ENABLE_METRICS": True},
            {"DEBUG_MODE": False}
        ],
        
        "# Secrets Management - New Format": None,
        "secrets_envs": [
            {
                "id": "arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123",
                "values": ["DATABASE_PASSWORD", "API_KEY", "JWT_SECRET"]
            },
            {
                "id": "arn:aws:secretsmanager:us-east-1:123456789012:secret:external-services-def456",
                "values": ["STRIPE_API_KEY", "SENDGRID_API_KEY"]
            }
        ],
        
        "# Secret Files": None,
        "secret_files": [
            "ssl-certificate",
            "private-key",
            "config-file"
        ],
        
        "# Fluent Bit Logging": None,
        "fluent_bit_collector": {
            "image_name": "fluent-bit:2.1.0",
            "extra_config": "custom-fluent-bit.conf",
            "ecs_log_metadata": "true"
        },
        
        "# OpenTelemetry Collector": None,
        "otel_collector": {
            "image_name": "my-custom-otel-collector:v1.0.0",
            "extra_config": "otel-config.yaml",
            "ssm_name": "my-app-otel-config.yaml",
            "metrics_port": 8888,
            "metrics_path": "/custom/metrics"
        }
    }

def yaml_to_clean_string(data):
    """Convert dict to clean YAML string without None values (comments)."""
    clean_data = {}
    for key, value in data.items():
        if not key.startswith('#') and value is not None:
            clean_data[key] = value
    
    return yaml.dump(clean_data, default_flow_style=False, sort_keys=False, indent=2)

def generate_task_definition(yaml_content):
    """Generate task definition JSON from YAML using the actual script."""
    
    # Create temporary YAML file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as temp_yaml:
        temp_yaml.write(yaml_content)
        temp_yaml_path = temp_yaml.name
    
    try:
        # Run the actual generate_task_def.py script
        cmd = [
            'python3', 'scripts/generate_task_def.py',
            temp_yaml_path,  # yaml_file
            'production-cluster',  # cluster_name
            'us-east-1',  # aws_region
            '123456789012.dkr.ecr.us-east-1.amazonaws.com',  # registry (for sidecars)
            '123456789012.dkr.ecr.us-east-1.amazonaws.com',  # container_registry (for main app)
            'my-awesome-app',  # image_name
            'latest',  # tag
            'my-service',  # service_name
            '--output', '/tmp/task-definition.json'
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, cwd='.')
        
        if result.returncode == 0:
            # Read the generated task definition
            with open('/tmp/task-definition.json', 'r') as f:
                task_def = json.load(f)
            return json.dumps(task_def, indent=2)
        else:
            print(f"Error generating task definition: {result.stderr}")
            return "# Error generating task definition"
            
    finally:
        # Clean up temporary file
        os.unlink(temp_yaml_path)
        if os.path.exists('/tmp/task-definition.json'):
            os.unlink('/tmp/task-definition.json')

def update_readme():
    """Update README.md with generated examples."""
    
    # Generate full YAML example
    full_example = create_full_example_yaml()
    yaml_content = yaml_to_clean_string(full_example)
    
    # Generate corresponding task definition
    task_definition_json = generate_task_definition(yaml_content)
    
    # Read current README
    readme_path = 'README.md'
    with open(readme_path, 'r') as f:
        readme_content = f.read()
    
    # Define the sections to replace
    yaml_section = f"""## 📋 Complete YAML Configuration Example

<!-- AUTO-GENERATED-YAML-START -->
```yaml
{yaml_content.strip()}
```
<!-- AUTO-GENERATED-YAML-END -->"""

    task_def_section = f"""## 🔧 Generated Task Definition

<!-- AUTO-GENERATED-TASK-DEF-START -->
```json
{task_definition_json.strip()}
```
<!-- AUTO-GENERATED-TASK-DEF-END -->"""
    
    # Replace or add sections
    readme_content = replace_section(readme_content, 
                                   '<!-- AUTO-GENERATED-YAML-START -->', 
                                   '<!-- AUTO-GENERATED-YAML-END -->', 
                                   yaml_section)
    
    readme_content = replace_section(readme_content, 
                                   '<!-- AUTO-GENERATED-TASK-DEF-START -->', 
                                   '<!-- AUTO-GENERATED-TASK-DEF-END -->', 
                                   task_def_section)
    
    # Write updated README
    with open(readme_path, 'w') as f:
        f.write(readme_content)
    
    print("✅ README updated with latest examples and task definition")

def replace_section(content, start_marker, end_marker, new_section):
    """Replace content between markers, or append if markers don't exist."""
    start_idx = content.find(start_marker)
    end_idx = content.find(end_marker)
    
    if start_idx != -1 and end_idx != -1:
        # Replace existing section
        end_idx += len(end_marker)
        return content[:start_idx] + new_section + content[end_idx:]
    else:
        # Append new section
        return content + '\n\n' + new_section + '\n'

if __name__ == '__main__':
    update_readme()
