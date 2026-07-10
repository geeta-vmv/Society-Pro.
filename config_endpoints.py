"""
Backend API Endpoints - App Configuration
Add these to backend/app.py

Serves configuration to mobile and web apps based on environment
"""

# Add to imports section
from config_loader import get_config

# ══════════════════════════════════════════════════════════════════════════════
# APP CONFIGURATION ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/config/mobile", methods=["GET"])
def get_mobile_config():
    """
    Get configuration for mobile app
    Returns: App settings, feature flags, API endpoints
    """
    config_loader = get_config()
    
    # Get mobile-specific configuration
    mobile_config = config_loader.get_mobile_config()
    
    # Add dynamic values
    mobile_config.update({
        'server_time': datetime.utcnow().isoformat(),
        'min_app_version': config_loader.get('mobile.android.min_version', '1.0.0'),
        'force_update': False,  # Can be set based on business logic
        'maintenance_mode': config_loader.get('mobile.maintenance_mode', False),
    })
    
    return ok(mobile_config)


@app.route("/api/config/web", methods=["GET"])
def get_web_config():
    """
    Get configuration for web app
    Returns: App settings, feature flags, payment gateway keys
    """
    config_loader = get_config()
    
    # Get web-specific configuration
    web_config = config_loader.get_web_config()
    
    # Add dynamic values
    web_config.update({
        'server_time': datetime.utcnow().isoformat(),
        'maintenance_mode': False,
    })
    
    return ok(web_config)


@app.route("/api/config/features", methods=["GET"])
def get_feature_flags():
    """
    Get feature flags for current society
    Used by both mobile and web apps
    """
    sid = int(request.args.get("society_id", 1))
    
    # Get society plan
    plan = _get_society_plan(sid)
    current_plan = plan.get("current_plan", "FREE")
    
    # Get global features from config
    config_loader = get_config()
    global_features = config_loader.get('features', {})
    
    # Determine available features based on plan
    available_features = {
        # Basic features (always available)
        'flats': True,
        'visitors': True,
        'announcements': True,
        'feedback': True,
        'guards': True,
        'dashboard_basic': True,
        'profile': True,
        
        # Paid features (only if society has PAID plan)
        'maintenance': current_plan == 'PAID',
        'payments': current_plan == 'PAID',
        'rent_management': current_plan == 'PAID',
        'bank_account': current_plan == 'PAID',
        'payment_history': current_plan == 'PAID',
        'dashboard_full': current_plan == 'PAID',
        'service_directory': current_plan == 'PAID',
        'service_requests': current_plan == 'PAID',
        'expense_tracking': current_plan == 'PAID',
        'reports': current_plan == 'PAID',
        
        # Notification preferences
        'visitor_sms': global_features.get('visitor_approval_sms', False),
        'visitor_push': global_features.get('visitor_approval_push', False),
        'maintenance_sms': global_features.get('maintenance_due_sms', False),
        'rent_sms': global_features.get('rent_due_sms', False),
    }
    
    return ok({
        'society_id': sid,
        'plan': current_plan,
        'features': available_features,
        'plan_status': plan.get('request_status', 'NONE'),
    })


@app.route("/api/config/payment-keys", methods=["GET"])
@auth_required
def get_payment_keys():
    """
    Get payment gateway public keys
    Only returns public/publishable keys (safe for client-side)
    """
    config_loader = get_config()
    
    gateway = config_loader.get('payment.gateway', 'simulate')
    
    payment_config = {
        'gateway': gateway,
        'currency': config_loader.get('payment.currency', 'INR'),
    }
    
    if gateway == 'razorpay':
        payment_config.update({
            'razorpay': {
                'key_id': config_loader.get('payment.razorpay.key_id', ''),
                'checkout_url': config_loader.get('payment.razorpay.checkout_url', ''),
            }
        })
    elif gateway == 'stripe':
        payment_config.update({
            'stripe': {
                'publishable_key': config_loader.get('payment.stripe.publishable_key', ''),
            }
        })
    
    return ok(payment_config)


@app.route("/api/config/endpoints", methods=["GET"])
def get_api_endpoints():
    """
    Get list of all API endpoints with their methods
    Useful for mobile/web app to verify API compatibility
    """
    config_loader = get_config()
    base_url = config_loader.get('api.base_url', request.host_url.rstrip('/'))
    
    endpoints = config_loader.get('runtime', {}).get('api_endpoints', {})
    
    # Add base URL to each endpoint
    full_endpoints = {
        key: f"{base_url}{value}" if value.startswith('/') else value
        for key, value in endpoints.items()
    }
    
    return ok({
        'base_url': base_url,
        'endpoints': full_endpoints,
        'version': 'v1',
    })


@app.route("/api/health", methods=["GET"])
def health_check():
    """
    Health check endpoint for monitoring
    Returns: Server status, database status, config status
    """
    config_loader = get_config()
    
    # Check database
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    
    # Check Redis (if enabled)
    redis_status = "disabled"
    if config_loader.get('redis.enabled', False):
        try:
            # Add Redis health check here
            redis_status = "healthy"
        except:
            redis_status = "unhealthy"
    
    return ok({
        'status': 'healthy',
        'environment': config_loader.get('app.environment', 'unknown'),
        'version': '2.0.0',
        'database': db_status,
        'redis': redis_status,
        'timestamp': datetime.utcnow().isoformat(),
    })


# ══════════════════════════════════════════════════════════════════════════════
# USAGE IN APP.PY
# ══════════════════════════════════════════════════════════════════════════════
"""
1. Add import at top:
   from config_loader import get_config

2. Replace the old CFG loading with:
   config_loader = get_config()
   CFG = config_loader.config

3. Add these new routes before the catch-all route at the end
"""
