#!/usr/bin/env python3
"""
Auto-generate README documentation with full YAML examples and task definitions.
This script analyzes generate_task_def.py to dynamically discover ALL possible 
configuration options and creates comprehensive examples.
"""

import os
import json
import yaml
import subprocess
import tempfile
import ast
import re
from pathlib import Path
from typing import Dict, Any, List, Set, Union

def analyze_generate_script():
    """Analyze task definition modules to discover YAML configuration fields."""

    module_paths = sorted(Path('scripts/task_def').rglob('*.py'))
    discovered_fields = {}

    for module_path in module_paths:
        with open(module_path, 'r') as f:
            content = f.read()

        tree = ast.parse(content)

        # Find all config.get() calls with their default values
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if (isinstance(node.func, ast.Attribute) and
                    isinstance(node.func.value, ast.Name) and
                    node.func.value.id == 'config' and
                    node.func.attr == 'get'):

                    if node.args and isinstance(node.args[0], ast.Constant):
                        field_name = node.args[0].value

                        # Get default value if provided
                        default_value = None
                        if len(node.args) > 1:
                            default_value = ast.literal_eval(node.args[1]) if isinstance(node.args[1], (ast.Constant, ast.List, ast.Dict)) else None

                        discovered_fields[field_name] = default_value

        # Also find direct config['field'] access
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                if (isinstance(node.value, ast.Name) and
                    node.value.id == 'config' and
                    isinstance(node.slice, ast.Constant)):

                    field_name = node.slice.value
                    if field_name not in discovered_fields:
                        discovered_fields[field_name] = None
    
    return discovered_fields

def infer_field_types_and_examples():
    """Infer appropriate example values for each field based on code analysis."""

    module_paths = sorted(Path('scripts/task_def').rglob('*.py'))
    content = ""
    for module_path in module_paths:
        with open(module_path, 'r') as f:
            content += f.read() + "\n"
    
    field_examples = {}
    
    # Define intelligent defaults based on field names and common patterns
    intelligent_defaults = {
        # Core ECS settings
        'replica_count': 3,
        'cpu': 1024,
        'memory': 2048,
        'cpu_arch': 'X86_64',
        'role_arn': 'arn:aws:iam::123456789012:role/ecsTaskExecutionRole',
        
        # Container settings
        'port': 8080,
        'additional_ports': [{'metrics': 9090}, {'health': 8081}],
        'command': ['npm', 'start'],
        'entrypoint': ['/usr/local/bin/docker-entrypoint.sh'],
        
        # Health check
        'health_check': {
            'command': 'curl -f http://localhost:8080/health || exit 1',
            'interval': 30,
            'timeout': 5,
            'retries': 3,
            'start_period': 60
        },
        
        # Environment variables
        'envs': [
            {'NODE_ENV': 'production'},
            {'API_VERSION': 'v1'},
            {'LOG_LEVEL': 'info'},
            {'MAX_CONNECTIONS': 100},
            {'ENABLE_METRICS': True}
        ],
        
        # Secrets - classic format
        'secrets': [
            {'DATABASE_PASSWORD': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:db-password'},
            {'API_KEY': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:api-key'}
        ],
        
        # Secrets - new grouped format
        'secrets_envs': [
            {
                'id': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:app-secrets-abc123',
                'values': ['DATABASE_PASSWORD', 'API_KEY', 'JWT_SECRET']
            },
            {
                'name': 'app-credentials',
                'auto_parse_keys_to_envs': False,
                'env_name': 'APP_CREDENTIALS'
            }
        ],
        
        # Secret files
        'secret_files': ['ssl-certificate', 'private-key', 'config-file'],
        
        # OpenTelemetry
        'otel_collector': {
            'image_name': 'my-custom-otel-collector:v1.0.0',
            'extra_config': 'otel-config.yaml',
            'ssm_name': 'my-app-otel-config.yaml',
            'metrics_port': 8888,
            'metrics_path': '/metrics'
        },
        
        # Fluent Bit
        'fluent_bit_collector': {
            'image_name': 'fluent-bit:2.1.0',
            'extra_config': 'custom-fluent-bit.conf',
            'ecs_log_metadata': 'true'
        }
    }
    
    # Look for field validation patterns in the code to understand expected types
    if 'cpu' in content and 'memory' in content:
        # Look for CPU/memory validation
        cpu_pattern = re.search(r'cpu.*?(\d+)', content)
        memory_pattern = re.search(r'memory.*?(\d+)', content)
        
    # Look for environment variable handling patterns
    if 'str(' in content and 'envs' in content:
        # The script converts env values to strings, so we should show mixed types
        pass
    
    return intelligent_defaults

def create_comprehensive_yaml():
    """Create a comprehensive YAML example with ALL discovered fields."""
    
    discovered_fields = analyze_generate_script()
    field_examples = infer_field_types_and_examples()
    
    # Fields that are legacy or not needed in normal usage
    excluded_fields = {
        'name'  # Only used as fallback when service_name is not provided, but service_name is always provided by the action
    }
    
    print(f"🔍 Discovered {len(discovered_fields)} configuration fields from scripts/task_def modules")
    
    # Create comprehensive example
    yaml_example = {}
    
    # Add all discovered fields with intelligent examples
    for field_name in sorted(discovered_fields.keys()):
        if field_name in excluded_fields:
            print(f"  ⏭️  {field_name}: Skipped (legacy/unused)")
            continue
            
        if field_name in field_examples:
            yaml_example[field_name] = field_examples[field_name]
            print(f"  ✅ {field_name}: Added with example")
        else:
            # Try to infer a reasonable default
            if 'port' in field_name.lower():
                yaml_example[field_name] = 8080
            elif 'name' in field_name.lower():
                yaml_example[field_name] = f'example-{field_name.replace("_", "-")}'
            elif 'arn' in field_name.lower():
                yaml_example[field_name] = f'arn:aws:iam::123456789012:role/example-role'
            elif field_name.endswith('_config'):
                yaml_example[field_name] = f'{field_name.replace("_", "-")}.yaml'
            else:
                yaml_example[field_name] = f'example-{field_name.replace("_", "-")}'
            print(f"  ⚠️  {field_name}: Added with inferred example")
    
    print(f"📝 Generated comprehensive example with {len(yaml_example)} fields")
    
    return yaml_example

def create_full_example_yaml():
    """Create a comprehensive YAML example with all available options discovered dynamically."""
    return create_comprehensive_yaml()

def yaml_to_clean_string(data):
    """Convert dict to clean YAML string."""
    return yaml.dump(data, default_flow_style=False, sort_keys=False, indent=2)

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

    # Define dynamic section markers
    start_marker = '<start dynamic>'
    end_marker = '<end dynamic>'

    # Prepare dynamic content
    dynamic_content = f"## 📋 Complete YAML Configuration Example\n```yaml\n{yaml_content.strip()}\n```\n\n## 🔧 Generated Task Definition\n```json\n{task_definition_json.strip()}\n```"

    # Replace content between markers
    start_idx = readme_content.find(start_marker)
    end_idx = readme_content.find(end_marker)
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        new_readme = readme_content[:start_idx + len(start_marker)] + '\n' + dynamic_content + '\n' + readme_content[end_idx:]
    else:
        # If markers not found, append them at the end
        new_readme = readme_content.strip() + f"\n\n{start_marker}\n{dynamic_content}\n{end_marker}\n"

    # Write updated README
    with open(readme_path, 'w') as f:
        f.write(new_readme)

    print("✅ README updated with latest dynamic section")

def replace_section(content, start_marker, end_marker, new_section):
    """Replace content between markers, or append if markers don't exist."""
    start_idx = content.find(start_marker)
    end_idx = content.find(end_marker)

    # Remove duplicate headings before the marker
    def remove_heading_before_marker(text, heading, marker):
        idx = text.find(marker)
        if idx == -1:
            return text
        # Find heading before marker
        heading_idx = text.rfind(heading, 0, idx)
        if heading_idx != -1 and heading_idx + len(heading) == idx:
            # Remove heading
            text = text[:heading_idx] + text[idx:]
        return text

    if start_marker == '<!-- AUTO-GENERATED-YAML-START -->':
        content = remove_heading_before_marker(content, '## 📋 Complete YAML Configuration Example\n', start_marker)
    if start_marker == '<!-- AUTO-GENERATED-TASK-DEF-START -->':
        content = remove_heading_before_marker(content, '## 🔧 Generated Task Definition\n', start_marker)

    if start_idx != -1 and end_idx != -1:
        # Replace existing section
        end_idx += len(end_marker)
        return content[:start_idx] + new_section + content[end_idx:]
    else:
        # Append new section
        return content + '\n\n' + new_section + '\n'

if __name__ == '__main__':
    update_readme()
