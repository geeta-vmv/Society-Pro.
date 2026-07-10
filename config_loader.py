"""
SocietyPro - Universal Configuration Loader
Supports: Backend API, Mobile App, Web App
Loads: dev.conf, prod.conf based on environment
"""

import os
import yaml
import re
from typing import Any, Dict

class ConfigLoader:
    """
    Load and merge configuration from:
    1. Environment-specific config file (dev.conf or prod.conf)
    2. Environment variables (override config file)
    3. Default values (fallback)
    """
    
    def __init__(self, env: str = None):
        self.env = env or os.environ.get('FLASK_ENV', 'development')
        self.config = {}
        self.load()
    
    def load(self):
        """Load configuration from file and environment"""
        # Determine config file
        config_file = 'dev.conf' if self.env == 'development' else 'prod.conf'
        config_path = os.path.join(
            os.path.dirname(__file__), 
            '..', 
            'config', 
            config_file
        )
        
        # Load from YAML file
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                self.config = yaml.safe_load(f) or {}
        else:
            print(f"⚠️  Config file not found: {config_path}")
            self.config = self._get_defaults()
        
        # Process environment variable substitutions
        self.config = self._process_env_vars(self.config)
        
        # Add runtime values
        self._add_runtime_values()
    
    def _process_env_vars(self, config: Any) -> Any:
        """
        Recursively process config and replace ${VAR_NAME} with environment variables
        Also supports ${VAR_NAME:-default_value}
        """
        if isinstance(config, dict):
            return {k: self._process_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._process_env_vars(item) for item in config]
        elif isinstance(config, str):
            # Match ${VAR_NAME} or ${VAR_NAME:-default}
            pattern = r'\$\{([^}:]+)(?::-(.*?))?\}'
            
            def replace_var(match):
                var_name = match.group(1)
                default_value = match.group(2) if match.group(2) is not None else ''
                return os.environ.get(var_name, default_value)
            
            return re.sub(pattern, replace_var, config)
        else:
            return config
    
    def _add_runtime_values(self):
        """Add computed/runtime configuration values"""
        # API URLs for mobile/web
        base_url = self.get('api.base_url', 'http://localhost:5000')
        self.config.setdefault('runtime', {})
        self.config['runtime']['api_endpoints'] = {
            'base': base_url,
            'auth': f'{base_url}/api/auth',
            'users': f'{base_url}/api/users',
            'societies': f'{base_url}/api/societies',
            'flats': f'{base_url}/api/flats',
            'maintenance': f'{base_url}/api/maintenance',
            'payments': f'{base_url}/api/payments',
            'rent': f'{base_url}/api/rent',
            'visitors': f'{base_url}/api/visitors',
            'announcements': f'{base_url}/api/announcements',
            'feedbacks': f'{base_url}/api/feedbacks',
            'services': f'{base_url}/api/services',
            'tenants': f'{base_url}/api/tenants',
        }
        
        # Mobile app config export
        self.config['runtime']['mobile_config'] = {
            'api_base_url': base_url,
            'environment': self.env,
            'otp_enabled': self.get('otp.enabled', True),
            'payment_gateway': self.get('payment.gateway', 'simulate'),
            'features': self.get('features', {}),
            'min_app_version': {
                'ios': self.get('mobile.ios.min_version', '1.0.0'),
                'android': self.get('mobile.android.min_version', '1.0.0'),
            },
            'update_required': self.get('mobile.update_check_enabled', False),
        }
        
        # Web app config export
        self.config['runtime']['web_config'] = {
            'api_base_url': base_url,
            'environment': self.env,
            'payment_gateway': self.get('payment.gateway', 'simulate'),
            'razorpay_key': self.get('payment.razorpay.key_id', ''),
            'features': self.get('features', {}),
        }
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """
        Get config value using dot notation
        Example: get('database.host') returns config['database']['host']
        """
        keys = key_path.split('.')
        value = self.config
        
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    return default
            else:
                return default
        
        return value if value is not None else default
    
    def get_mobile_config(self) -> Dict[str, Any]:
        """Get configuration for mobile app"""
        return self.config.get('runtime', {}).get('mobile_config', {})
    
    def get_web_config(self) -> Dict[str, Any]:
        """Get configuration for web app"""
        return self.config.get('runtime', {}).get('web_config', {})
    
    def _get_defaults(self) -> Dict[str, Any]:
        """Return default configuration if no config file found"""
        return {
            'app': {
                'debug': self.env == 'development',
                'name': 'SocietyPro',
                'port': 5000,
                'secret_key': 'change-this-secret-key',
            },
            'database': {
                'type': 'sqlite',
                'path': '/data/societypro.db',
            },
            'jwt': {
                'access_token_expiry_hours': 24,
            },
            'otp': {
                'provider': 'console',
                'enabled': True,
                'expiry_minutes': 5,
            },
            'payment': {
                'gateway': 'simulate',
                'currency': 'INR',
            },
            'api': {
                'base_url': 'http://localhost:5000',
            },
        }


# Global config instance
_config_instance = None

def get_config() -> ConfigLoader:
    """Get singleton config instance"""
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigLoader()
    return _config_instance


# Convenience function
def reload_config():
    """Reload configuration (useful for testing)"""
    global _config_instance
    _config_instance = ConfigLoader()
    return _config_instance


# Export for use in app.py
if __name__ != '__main__':
    CFG = get_config().config
else:
    # CLI usage
    import json
    import sys
    
    if len(sys.argv) > 1:
        env = sys.argv[1]
    else:
        env = 'development'
    
    config = ConfigLoader(env)
    
    # Print config
    print("=" * 80)
    print(f"SocietyPro Configuration - {env.upper()}")
    print("=" * 80)
    print(json.dumps(config.config, indent=2, default=str))
    print("=" * 80)
    print("\nMobile App Config:")
    print(json.dumps(config.get_mobile_config(), indent=2))
    print("\nWeb App Config:")
    print(json.dumps(config.get_web_config(), indent=2))
