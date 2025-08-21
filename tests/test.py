#!/usr/bin/env python3
"""
Simplified test runner that processes each YAML file in examples/ directory,
executes the generate_task_def.py script, and compares output against expected JSON files.
If expected JSON doesn't exist, it creates it.
"""

import os
import sys
import json
import subprocess
import glob
from pathlib import Path

def get_script_dir():
    """Get the directory containing this script."""
    return Path(__file__).parent.absolute()

def get_project_root():
    """Get the project root directory."""
    return get_script_dir().parent

def get_examples_dir():
    """Get the examples directory."""
    return get_project_root() / "examples"

def get_expected_outputs_dir():
    """Get the expected outputs directory."""
    return get_script_dir() / "expected_outputs"

def get_script_path():
    """Get the path to the generate_task_def.py script."""
    return get_project_root() / "scripts" / "generate_task_def.py"

def run_script_for_yaml(yaml_file, script_path):
    """
    Execute the generate_task_def.py script for a given YAML file.
    Returns the JSON output or None if execution failed.
    """
    # Default parameters based on test_config.py
    cluster_name = "test-cluster"
    aws_region = "us-east-1"
    registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
    container_registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
    image_name = "test-app"
    tag = "latest"
    service_name = "test-service"
    
    # Use the same Python interpreter that's running this script
    python_executable = sys.executable
    
    cmd = [
        python_executable, str(script_path),
        str(yaml_file),
        cluster_name,
        aws_region,
        registry,
        container_registry,
        image_name,
        tag,
        service_name
    ]
    
    try:
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=get_project_root())
        
        if result.returncode != 0:
            print(f"Script failed with return code {result.returncode}")
            print(f"STDERR: {result.stderr}")
            print(f"STDOUT: {result.stdout}")
            return None
        
        # Parse the JSON output from stdout
        try:
            # The script prints the JSON between specific markers
            lines = result.stdout.split('\n')
            json_start = None
            json_end = None
            
            for i, line in enumerate(lines):
                if "----- Task Definition -----" in line:
                    json_start = i + 1
                elif "---------------------------" in line and json_start is not None:
                    json_end = i
                    break
            
            if json_start is not None and json_end is not None:
                json_text = '\n'.join(lines[json_start:json_end])
                return json.loads(json_text)
            else:
                # Fallback: try to parse the entire stdout as JSON
                return json.loads(result.stdout)
                
        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON output: {e}")
            print(f"Output was: {result.stdout}")
            return None
            
    except Exception as e:
        print(f"Exception running script: {e}")
        return None

def load_expected_json(json_file):
    """Load expected JSON from file, return None if file doesn't exist."""
    if not json_file.exists():
        return None
    
    try:
        with open(json_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load expected JSON from {json_file}: {e}")
        return None

def save_json_output(json_data, json_file):
    """Save JSON data to file."""
    # Ensure the directory exists
    json_file.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(json_file, 'w') as f:
            json.dump(json_data, f, indent=2)
        print(f"Created expected output file: {json_file}")
        return True
    except Exception as e:
        print(f"Failed to save JSON to {json_file}: {e}")
        return False

def compare_json(expected, actual):
    """Compare two JSON objects. Returns True if they match, False otherwise."""
    return expected == actual

def main():
    """Main test runner."""
    project_root = get_project_root()
    examples_dir = get_examples_dir()
    expected_outputs_dir = get_expected_outputs_dir()
    script_path = get_script_path()
    
    print(f"Project root: {project_root}")
    print(f"Examples directory: {examples_dir}")
    print(f"Expected outputs directory: {expected_outputs_dir}")
    print(f"Script path: {script_path}")
    
    # Find all YAML files in examples directory
    yaml_files = list(examples_dir.glob("*.yaml")) + list(examples_dir.glob("*.yml"))
    
    if not yaml_files:
        print("No YAML files found in examples directory")
        return 1
    
    print(f"Found {len(yaml_files)} YAML files")
    
    failed_tests = []
    created_files = []
    
    for yaml_file in yaml_files:
        print(f"\n{'='*60}")
        print(f"Processing: {yaml_file.name}")
        print(f"{'='*60}")
        
        # Get corresponding JSON file name
        json_filename = yaml_file.stem + ".json"
        expected_json_file = expected_outputs_dir / json_filename
        
        # Run the script
        actual_output = run_script_for_yaml(yaml_file, script_path)
        if actual_output is None:
            print(f"❌ FAILED: {yaml_file.name} - Script execution failed")
            failed_tests.append(yaml_file.name)
            continue
        
        # Load or create expected output
        expected_output = load_expected_json(expected_json_file)
        
        if expected_output is None:
            # Create the expected output file
            if save_json_output(actual_output, expected_json_file):
                print(f"✅ CREATED: {yaml_file.name} - Expected output file created")
                created_files.append(json_filename)
            else:
                print(f"❌ FAILED: {yaml_file.name} - Could not create expected output file")
                failed_tests.append(yaml_file.name)
        else:
            # Compare actual vs expected
            if compare_json(expected_output, actual_output):
                print(f"✅ PASSED: {yaml_file.name} - Output matches expected")
            else:
                print(f"❌ FAILED: {yaml_file.name} - Output does not match expected")
                print("Expected vs Actual differences found.")
                print("To see detailed differences, you can use a JSON diff tool.")
                failed_tests.append(yaml_file.name)
    
    # Print summary
    print(f"\n{'='*60}")
    print("TEST SUMMARY")
    print(f"{'='*60}")
    print(f"Total files processed: {len(yaml_files)}")
    print(f"Failed tests: {len(failed_tests)}")
    print(f"Created expected files: {len(created_files)}")
    
    if failed_tests:
        print(f"\nFailed tests:")
        for test in failed_tests:
            print(f"  - {test}")
    
    if created_files:
        print(f"\nCreated expected output files:")
        for file in created_files:
            print(f"  - {file}")
    
    return 1 if failed_tests else 0

if __name__ == "__main__":
    sys.exit(main())