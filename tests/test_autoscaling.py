#!/usr/bin/env python3
"""
Tests for autoscaling config publishing functionality.
"""

import unittest
import json
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
import tempfile
import yaml

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import publish_autoscaling


class TestAutoscalingConfigValidation(unittest.TestCase):
    """Test autoscaling config validation"""
    
    def setUp(self):
        """Load schema for tests"""
        self.schema = publish_autoscaling.load_json_schema()
    
    def test_load_json_schema(self):
        """Test that JSON schema loads correctly"""
        self.assertIsInstance(self.schema, dict)
        self.assertIn('properties', self.schema)
        self.assertIn('provider', self.schema['properties'])
    
    def test_valid_sqs_config(self):
        """Test valid SQS-only configuration"""
        config = {
            "provider": {
                "type": "sqs",
                "sqs": {
                    "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
                }
            },
            "min_tasks": 2,
            "max_tasks": 50
        }
        
        # Should not raise
        publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_valid_time_config(self):
        """Test valid time-only configuration"""
        config = {
            "provider": {
                "type": "time",
                "time": {
                    "timezone": "America/New_York",
                    "mode": "floor",
                    "rules": [
                        {
                            "days": ["mon", "tue", "wed"],
                            "start": "09:00",
                            "end": "17:00",
                            "min_desired": 10
                        }
                    ]
                }
            },
            "min_tasks": 1,
            "max_tasks": 20
        }
        
        # Should not raise
        publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_valid_sqs_time_combined(self):
        """Test valid SQS+Time combined configuration"""
        config = {
            "provider": {
                "type": "sqs+time",
                "sqs": {
                    "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
                },
                "time": {
                    "timezone": "UTC",
                    "mode": "floor",
                    "rules": [
                        {"days": ["mon"], "min_desired": 5}
                    ]
                }
            },
            "min_tasks": 2,
            "max_tasks": 50
        }
        
        # Should not raise
        publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_invalid_sqs_url_format(self):
        """Test invalid SQS URL format"""
        config = {
            "provider": {
                "type": "sqs",
                "sqs": {
                    "queue_url": "invalid-url"
                }
            },
            "min_tasks": 2,
            "max_tasks": 50
        }
        
        with self.assertRaises(publish_autoscaling.ConfigValidationError):
            publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_missing_sqs_block_when_required(self):
        """Test missing SQS block when type is 'sqs'"""
        config = {
            "provider": {
                "type": "sqs"
            },
            "min_tasks": 2,
            "max_tasks": 50
        }
        
        with self.assertRaises(publish_autoscaling.ConfigValidationError):
            publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_max_tasks_less_than_min_tasks(self):
        """Test validation fails when max_tasks < min_tasks"""
        config = {
            "provider": {
                "type": "sqs",
                "sqs": {
                    "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
                }
            },
            "min_tasks": 50,
            "max_tasks": 10
        }
        
        with self.assertRaises(publish_autoscaling.ConfigValidationError):
            publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_invalid_time_format(self):
        """Test invalid time format in rules"""
        config = {
            "provider": {
                "type": "time",
                "time": {
                    "mode": "floor",
                    "rules": [
                        {
                            "days": ["mon"],
                            "start": "9:00",  # Invalid: should be 09:00
                            "end": "17:00",
                            "min_desired": 10
                        }
                    ]
                }
            },
            "min_tasks": 1,
            "max_tasks": 20
        }
        
        with self.assertRaises(publish_autoscaling.ConfigValidationError):
            publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_override_mode_requires_desired(self):
        """Test that override mode requires at least one rule with 'desired'"""
        config = {
            "provider": {
                "type": "time",
                "time": {
                    "mode": "override",
                    "rules": [
                        {
                            "days": ["mon"],
                            "min_desired": 10  # Should be 'desired' for override mode
                        }
                    ]
                }
            },
            "min_tasks": 1,
            "max_tasks": 20
        }
        
        with self.assertRaises(publish_autoscaling.ConfigValidationError):
            publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_low_latency_guard_requires_fields(self):
        """Test that low_latency mode requires specific fields"""
        config = {
            "provider": {
                "type": "sqs",
                "sqs": {
                    "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue"
                }
            },
            "min_tasks": 2,
            "max_tasks": 50,
            "scale_in_guard": {
                "mode": "low_latency"
                # Missing age_below_seconds and visible_below
            }
        }
        
        with self.assertRaises(publish_autoscaling.ConfigValidationError):
            publish_autoscaling.validate_autoscaling_config(config, self.schema)
    
    def test_start_time_after_end_time(self):
        """Test validation fails when start time is after end time"""
        config = {
            "provider": {
                "type": "time",
                "time": {
                    "mode": "floor",
                    "rules": [
                        {
                            "days": ["mon"],
                            "start": "17:00",
                            "end": "09:00",  # End before start
                            "min_desired": 10
                        }
                    ]
                }
            },
            "min_tasks": 1,
            "max_tasks": 20
        }
        
        with self.assertRaises(publish_autoscaling.ConfigValidationError):
            publish_autoscaling.validate_autoscaling_config(config, self.schema)


class TestChecksumComputation(unittest.TestCase):
    """Test checksum computation"""
    
    def test_checksum_is_deterministic(self):
        """Test that checksum is deterministic for same config"""
        config = {
            "provider": {"type": "sqs", "sqs": {"queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/q"}},
            "min_tasks": 2,
            "max_tasks": 50
        }
        
        checksum1 = publish_autoscaling.compute_checksum(config)
        checksum2 = publish_autoscaling.compute_checksum(config)
        
        self.assertEqual(checksum1, checksum2)
    
    def test_checksum_changes_with_config(self):
        """Test that checksum changes when config changes"""
        config1 = {"min_tasks": 2, "max_tasks": 50}
        config2 = {"min_tasks": 3, "max_tasks": 50}
        
        checksum1 = publish_autoscaling.compute_checksum(config1)
        checksum2 = publish_autoscaling.compute_checksum(config2)
        
        self.assertNotEqual(checksum1, checksum2)
    
    def test_checksum_is_sha256_hex(self):
        """Test that checksum is valid SHA256 hex"""
        config = {"min_tasks": 2, "max_tasks": 50}
        checksum = publish_autoscaling.compute_checksum(config)
        
        # SHA256 hex is 64 characters
        self.assertEqual(len(checksum), 64)
        
        # Should be valid hex
        try:
            int(checksum, 16)
        except ValueError:
            self.fail("Checksum is not valid hex")


class TestServiceKeyBuilding(unittest.TestCase):
    """Test service key building"""
    
    def test_service_key_format(self):
        """Test service key format"""
        service_key = publish_autoscaling.build_service_key(
            "production",
            "prod-cluster",
            "my-service"
        )
        
        self.assertEqual(service_key, "production:prod-cluster:my-service")
    
    def test_service_key_with_special_chars(self):
        """Test service key with special characters"""
        service_key = publish_autoscaling.build_service_key(
            "staging-eu",
            "cluster_01",
            "service-name-123"
        )
        
        self.assertEqual(service_key, "staging-eu:cluster_01:service-name-123")


class TestYAMLConfigExtraction(unittest.TestCase):
    """Test YAML config loading and extraction"""
    
    def test_extract_autoscaling_config_present(self):
        """Test extracting autoscaling config when present"""
        config = {
            "name": "my-app",
            "cpu": 256,
            "autoscaling_configs": {
                "provider": {"type": "sqs"},
                "min_tasks": 2,
                "max_tasks": 50
            }
        }
        
        result = publish_autoscaling.extract_autoscaling_config(config)
        self.assertIsNotNone(result)
        self.assertEqual(result["min_tasks"], 2)
    
    def test_extract_autoscaling_config_absent(self):
        """Test extracting autoscaling config when absent"""
        config = {
            "name": "my-app",
            "cpu": 256
        }
        
        result = publish_autoscaling.extract_autoscaling_config(config)
        self.assertIsNone(result)


class TestDynamoDBFormatConversion(unittest.TestCase):
    """Test DynamoDB format conversion"""
    
    def test_convert_string(self):
        """Test converting string to DynamoDB format"""
        result = publish_autoscaling._convert_to_dynamodb_format("test")
        self.assertEqual(result, {"S": "test"})
    
    def test_convert_number(self):
        """Test converting number to DynamoDB format"""
        result = publish_autoscaling._convert_to_dynamodb_format(42)
        self.assertEqual(result, {"N": "42"})
    
    def test_convert_boolean(self):
        """Test converting boolean to DynamoDB format"""
        result = publish_autoscaling._convert_to_dynamodb_format(True)
        self.assertEqual(result, {"BOOL": True})
    
    def test_convert_list(self):
        """Test converting list to DynamoDB format"""
        result = publish_autoscaling._convert_to_dynamodb_format([1, "test", True])
        expected = {
            "L": [
                {"N": "1"},
                {"S": "test"},
                {"BOOL": True}
            ]
        }
        self.assertEqual(result, expected)
    
    def test_convert_dict(self):
        """Test converting dict to DynamoDB format"""
        result = publish_autoscaling._convert_to_dynamodb_format({
            "key1": "value1",
            "key2": 42
        })
        expected = {
            "M": {
                "key1": {"S": "value1"},
                "key2": {"N": "42"}
            }
        }
        self.assertEqual(result, expected)
    
    def test_convert_nested_structure(self):
        """Test converting nested structure"""
        result = publish_autoscaling._convert_to_dynamodb_format({
            "provider": {
                "type": "sqs",
                "sqs": {
                    "queue_url": "https://example.com"
                }
            },
            "min_tasks": 2
        })
        
        self.assertIn("M", result)
        self.assertIn("provider", result["M"])
        self.assertIn("M", result["M"]["provider"])


class TestExampleYAMLFiles(unittest.TestCase):
    """Test that example YAML files are valid"""
    
    def setUp(self):
        """Load schema for tests"""
        self.schema = publish_autoscaling.load_json_schema()
        self.examples_dir = Path(__file__).parent.parent / "examples"
    
    def test_autoscaling_sqs_simple(self):
        """Test autoscaling-sqs-simple.yaml is valid"""
        yaml_path = self.examples_dir / "autoscaling-sqs-simple.yaml"
        if not yaml_path.exists():
            self.skipTest(f"Example file not found: {yaml_path}")
        
        with open(yaml_path) as f:
            config = yaml.safe_load(f)
        
        autoscaling_config = config.get('autoscaling_configs')
        self.assertIsNotNone(autoscaling_config)
        
        # Should not raise
        publish_autoscaling.validate_autoscaling_config(autoscaling_config, self.schema)
    
    def test_autoscaling_time_based(self):
        """Test autoscaling-time-based.yaml is valid"""
        yaml_path = self.examples_dir / "autoscaling-time-based.yaml"
        if not yaml_path.exists():
            self.skipTest(f"Example file not found: {yaml_path}")
        
        with open(yaml_path) as f:
            config = yaml.safe_load(f)
        
        autoscaling_config = config.get('autoscaling_configs')
        self.assertIsNotNone(autoscaling_config)
        
        # Should not raise
        publish_autoscaling.validate_autoscaling_config(autoscaling_config, self.schema)
    
    def test_autoscaling_sqs_time_combined(self):
        """Test autoscaling-sqs-time-combined.yaml is valid"""
        yaml_path = self.examples_dir / "autoscaling-sqs-time-combined.yaml"
        if not yaml_path.exists():
            self.skipTest(f"Example file not found: {yaml_path}")
        
        with open(yaml_path) as f:
            config = yaml.safe_load(f)
        
        autoscaling_config = config.get('autoscaling_configs')
        self.assertIsNotNone(autoscaling_config)
        
        # Should not raise
        publish_autoscaling.validate_autoscaling_config(autoscaling_config, self.schema)
    
    def test_autoscaling_low_latency(self):
        """Test autoscaling-low-latency.yaml is valid"""
        yaml_path = self.examples_dir / "autoscaling-low-latency.yaml"
        if not yaml_path.exists():
            self.skipTest(f"Example file not found: {yaml_path}")
        
        with open(yaml_path) as f:
            config = yaml.safe_load(f)
        
        autoscaling_config = config.get('autoscaling_configs')
        self.assertIsNotNone(autoscaling_config)
        
        # Should not raise
        publish_autoscaling.validate_autoscaling_config(autoscaling_config, self.schema)
    
    def test_no_autoscaling_yaml(self):
        """Test no-autoscaling.yaml has no autoscaling config"""
        yaml_path = self.examples_dir / "no-autoscaling.yaml"
        if not yaml_path.exists():
            self.skipTest(f"Example file not found: {yaml_path}")
        
        with open(yaml_path) as f:
            config = yaml.safe_load(f)
        
        autoscaling_config = config.get('autoscaling_configs')
        self.assertIsNone(autoscaling_config)


if __name__ == '__main__':
    unittest.main()

