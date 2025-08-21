
import os
import glob
import yaml
import json
import pytest
from scripts.generate_task_def import generate_task_definition

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)

def load_json(path):
    with open(path) as f:
        return json.load(f)

def test_schema():
    yaml_files = glob.glob('tests/fixtures/*.yaml')
    for yaml_file in yaml_files:
        base = os.path.splitext(os.path.basename(yaml_file))[0]
        json_file = f'tests/fixtures/{base}.json'
        assert os.path.exists(json_file), f"Missing expected JSON file for {yaml_file}"
        input_yaml = load_yaml(yaml_file)
        image_name = input_yaml.get('image_name', 'my-app')
        tag = input_yaml.get('tag', 'latest')
        cluster_name = input_yaml.get('cluster_name', 'test-cluster')
        aws_region = input_yaml.get('aws_region', 'us-east-1')
        registry = input_yaml.get('registry', '123456789012.dkr.ecr.us-east-1.amazonaws.com')
        container_registry = input_yaml.get('container_registry', '123456789012.dkr.ecr.us-east-1.amazonaws.com')
        service_name = input_yaml.get('service_name', 'my-service')
        generated_json = generate_task_definition(
            config_dict=input_yaml,
            image_name=image_name,
            tag=tag,
            cluster_name=cluster_name,
            aws_region=aws_region,
            registry=registry,
            container_registry=container_registry,
            service_name=service_name
        )
        expected_json = load_json(json_file)
        assert generated_json == expected_json, f"Mismatch for {base}:\nGenerated: {generated_json}\nExpected: {expected_json}"
