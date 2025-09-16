# AlgoMirror - Security Implementation Guide

## Security Architecture Overview

AlgoMirror implements defense-in-depth security with multiple layers of protection, zero-trust architecture, and comprehensive audit logging to ensure enterprise-grade security for multi-account trading operations.

## Core Security Principles

### 1. Zero-Trust Architecture
- **No Default Accounts**: System has NO pre-configured admin or user accounts
- **First User = Admin**: First registered user automatically becomes administrator
- **Runtime Detection**: Admin privileges determined dynamically at runtime
- **No Hardcoded Credentials**: All credentials are user-generated

### 2. Defense in Depth
- **Multiple Security Layers**: Application, network, and data security
- **Fail-Secure Design**: System fails to a secure state
- **Principle of Least Privilege**: Minimal access rights
- **Separation of Duties**: Role-based access control

## Authentication System

### 1. User Registration

```python
# app/auth/routes.py
@auth_bp.route('/register', methods=['GET', 'POST'])
@auth_rate_limit()
def register():
    form = RegistrationForm()
    
    if form.validate_on_submit():
        # Check if first user (becomes admin)
        is_first_user = User.query.count() == 0
        
        # Create user with hashed password
        user = User(
            username=form.username.data,
            email=form.email.data,
            is_admin=is_first_user  # First user becomes admin
        )
        user.set_password(form.password.data)
        
        # Log registration
        activity = ActivityLog(
            user_id=user.id,
            action='user_registration',
            details=f'New user registered: {user.username}',
            ip_address=request.remote_addr
        )
        
        db.session.add(user)
        db.session.add(activity)
        db.session.commit()
```

### 2. Password Security

#### Password Policy Implementation
```python
# app/auth/forms.py
def validate_password_strength(password):
    """
    Enforce strong password policy:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    - Not a common password
    """
    if len(password) < 8:
        raise ValidationError('Password must be at least 8 characters')
    
    if not re.search(r'[A-Z]', password):
        raise ValidationError('Password must contain uppercase letter')
    
    if not re.search(r'[a-z]', password):
        raise ValidationError('Password must contain lowercase letter')
    
    if not re.search(r'\d', password):
        raise ValidationError('Password must contain at least one digit')
    
    if not re.search(r'[!@#$%^&*()_+\-=\[\]{}|;:,.<>?]', password):
        raise ValidationError('Password must contain special character')
    
    # Check common passwords
    common_passwords = [
        'password', '12345678', 'qwerty', 'abc123',
        'password123', 'admin', 'letmein'
    ]
    
    if password.lower() in common_passwords:
        raise ValidationError('Password is too common')
```

#### Password Hashing
```python
# app/models.py
from werkzeug.security import generate_password_hash, check_password_hash

class User(UserMixin, db.Model):
    password_hash = db.Column(db.String(256))
    
    def set_password(self, password):
        """Hash password using pbkdf2:sha256"""
        self.password_hash = generate_password_hash(
            password,
            method='pbkdf2:sha256',
            salt_length=16
        )
    
    def check_password(self, password):
        """Verify password against hash"""
        return check_password_hash(self.password_hash, password)
```

### 3. Session Management

```python
# config.py
class Config:
    # Session configuration
    SESSION_TYPE = 'filesystem'  # Default: filesystem sessions (single-user)
    # Alternative: 'sqlalchemy' for database sessions (multi-user)
    SESSION_COOKIE_SECURE = True  # HTTPS only
    SESSION_COOKIE_HTTPONLY = True  # No JavaScript access
    SESSION_COOKIE_SAMESITE = 'Lax'  # CSRF protection
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)

    # For SQLAlchemy sessions (when SESSION_TYPE='sqlalchemy')
    SESSION_SQLALCHEMY_TABLE = 'flask_sessions'
    SESSION_SQLALCHEMY = None  # Set to db instance at runtime

    # Remember me configuration
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = timedelta(days=30)
```

#### Session Storage Options

**Filesystem Sessions (Default)**
- Suitable for single-user applications
- Sessions stored in server filesystem
- Simple configuration, no external dependencies

**Database Sessions (Alternative)**
- Better for multi-user applications
- Sessions stored in database table
- Requires additional database table

```python
# app/__init__.py - Session initialization
def create_app():
    # Configure session storage
    if app.config.get('SESSION_TYPE') == 'sqlalchemy':
        app.config['SESSION_SQLALCHEMY'] = db

    sess.init_app(app)
```

### 4. Login Security

```python
@auth_bp.route('/login', methods=['GET', 'POST'])
@auth_rate_limit()  # Rate limit login attempts
def login():
    form = LoginForm()
    
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        
        # Check login attempts
        if check_login_attempts(request.remote_addr):
            flash('Too many login attempts. Please try again later.')
            return redirect(url_for('auth.login'))
        
        if user and user.check_password(form.password.data):
            # Successful login
            login_user(user, remember=form.remember_me.data)
            
            # Log successful login
            log_activity('login_success', user.id)
            
            # Clear login attempts
            clear_login_attempts(request.remote_addr)
            
            return redirect(url_for('main.dashboard'))
        else:
            # Failed login
            record_login_attempt(request.remote_addr)
            log_activity('login_failed', request.remote_addr)
            
            flash('Invalid username or password')
```

## Data Encryption

### 1. API Key Encryption

```python
# app/models.py
from cryptography.fernet import Fernet
import os
import base64

def get_encryption_key():
    """Get or generate encryption key"""
    key = os.environ.get('ENCRYPTION_KEY')
    
    if not key:
        # Generate new key if not provided
        key = Fernet.generate_key()
        os.environ['ENCRYPTION_KEY'] = key.decode()
        logger.warning('Generated new encryption key')
    else:
        key = key.encode()
    
    return key

class TradingAccount(db.Model):
    api_key_encrypted = db.Column(db.Text)
    
    def set_api_key(self, api_key):
        """Encrypt API key for storage"""
        if not api_key:
            return
        
        cipher = Fernet(get_encryption_key())
        encrypted = cipher.encrypt(api_key.encode())
        self.api_key_encrypted = encrypted.decode()
    
    def get_api_key(self):
        """Decrypt API key for use"""
        if not self.api_key_encrypted:
            return None
        
        try:
            cipher = Fernet(get_encryption_key())
            decrypted = cipher.decrypt(self.api_key_encrypted.encode())
            return decrypted.decode()
        except Exception as e:
            logger.error(f"Failed to decrypt API key: {e}")
            return None
```

### 2. Sensitive Data Handling

```python
class SecureDataHandler:
    """Handle sensitive data securely"""
    
    @staticmethod
    def sanitize_for_logging(data):
        """Remove sensitive data before logging"""
        sensitive_fields = ['api_key', 'password', 'token', 'secret']
        
        sanitized = data.copy()
        for field in sensitive_fields:
            if field in sanitized:
                sanitized[field] = '***REDACTED***'
        
        return sanitized
    
    @staticmethod
    def mask_api_key(api_key):
        """Mask API key for display"""
        if not api_key or len(api_key) < 12:
            return '***'
        
        return f"{api_key[:4]}...{api_key[-4:]}"
```

## CSRF Protection

### 1. CSRF Token Implementation

```python
# app/__init__.py
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

def create_app(config_name='development'):
    app = Flask(__name__)
    csrf.init_app(app)
    
    # CSRF configuration
    app.config['WTF_CSRF_TIME_LIMIT'] = None  # No time limit
    app.config['WTF_CSRF_SSL_STRICT'] = True  # HTTPS required
```

### 2. Form Protection

```html
<!-- templates/forms.html -->
<form method="POST" action="{{ url_for('accounts.add') }}">
    {{ form.hidden_tag() }}  <!-- CSRF token -->
    <!-- form fields -->
</form>

<!-- AJAX requests -->
<script>
    const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
    
    fetch('/api/data', {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    });
</script>
```

## Rate Limiting

### 1. Multi-Tier Rate Limiting

```python
# app/utils/rate_limiter.py
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["1000 per minute"],
    storage_uri="redis://localhost:6379",  # Production
    strategy="fixed-window"  # Rate limiting strategy
)

# Custom rate limit decorators
def auth_rate_limit():
    """Strict rate limit for authentication endpoints"""
    return limiter.limit("10 per minute")

def api_rate_limit():
    """Standard rate limit for API endpoints"""
    return limiter.limit("100 per minute")

def heavy_rate_limit():
    """Rate limit for resource-intensive operations"""
    return limiter.limit("20 per minute")
```

### 2. Application to Routes

```python
@auth_bp.route('/login', methods=['POST'])
@auth_rate_limit()  # 10 requests per minute
def login():
    pass

@api_bp.route('/data')
@api_rate_limit()  # 100 requests per minute
def get_data():
    pass

@accounts_bp.route('/refresh')
@heavy_rate_limit()  # 20 requests per minute
def refresh_all():
    pass
```

## Input Validation & Sanitization

### 1. Form Validation

```python
# app/auth/forms.py
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField
from wtforms.validators import DataRequired, Email, Length, Regexp

class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[
        DataRequired(),
        Length(min=3, max=20),
        Regexp('^[A-Za-z0-9_]+$', message='Only letters, numbers, and underscores')
    ])
    
    email = EmailField('Email', validators=[
        DataRequired(),
        Email(message='Invalid email address')
    ])
    
    password = PasswordField('Password', validators=[
        DataRequired(),
        Length(min=8, max=128)
    ])
```

### 2. SQL Injection Prevention

```python
# Using SQLAlchemy ORM (parameterized queries)
def get_user_by_username(username):
    # Safe - uses parameterized query
    return User.query.filter_by(username=username).first()

# Never use string formatting for queries
# UNSAFE: query = f"SELECT * FROM users WHERE username = '{username}'"

# Safe raw query example
def get_account_stats(user_id):
    query = text("""
        SELECT COUNT(*) as total, 
               SUM(CASE WHEN is_active THEN 1 ELSE 0 END) as active
        FROM trading_accounts 
        WHERE user_id = :user_id
    """)
    
    result = db.session.execute(query, {'user_id': user_id})
    return result.fetchone()
```

### 3. XSS Prevention

```python
# Automatic escaping in Jinja2 templates
# app/templates/base.html
"""
{{ user_input }}  <!-- Automatically escaped -->
{{ user_input|safe }}  <!-- Only use when absolutely sure -->
"""

# Content Security Policy
class Config:
    # CSP Headers
    CONTENT_SECURITY_POLICY = {
        'default-src': "'self'",
        'script-src': "'self' 'unsafe-inline'",  # Required for inline scripts
        'style-src': "'self' 'unsafe-inline'",   # Required for inline styles
        'img-src': "'self' data:",
        'connect-src': "'self' ws: wss:",        # WebSocket connections
    }
```

## Authorization & Access Control

### 1. Role-Based Access Control

```python
# app/utils/decorators.py
from functools import wraps
from flask_login import current_user

def admin_required(f):
    """Decorator for admin-only routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        
        if not current_user.is_admin:
            abort(403)  # Forbidden
        
        return f(*args, **kwargs)
    return decorated_function

def account_owner_required(f):
    """Ensure user owns the account"""
    @wraps(f)
    def decorated_function(account_id, *args, **kwargs):
        account = TradingAccount.query.get_or_404(account_id)
        
        if account.user_id != current_user.id:
            abort(403)
        
        return f(account_id, *args, **kwargs)
    return decorated_function
```

### 2. Resource Isolation

```python
# Ensure users can only access their own data
@accounts_bp.route('/accounts')
@login_required
def list_accounts():
    # Only show current user's accounts
    accounts = TradingAccount.query.filter_by(
        user_id=current_user.id
    ).all()
    
    return render_template('accounts/list.html', accounts=accounts)
```

## Audit Logging

### 1. Activity Logging

```python
# app/models.py
class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(200))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Indexes for efficient querying
    __table_args__ = (
        db.Index('idx_user_timestamp', 'user_id', 'timestamp'),
        db.Index('idx_action_timestamp', 'action', 'timestamp'),
    )

def log_activity(action, details=None, user_id=None):
    """Log user activity for audit trail"""
    activity = ActivityLog(
        user_id=user_id or current_user.id,
        action=action,
        details=details,
        ip_address=request.remote_addr,
        user_agent=request.user_agent.string[:200],
        timestamp=datetime.utcnow()
    )
    
    db.session.add(activity)
    db.session.commit()
```

### 2. Security Event Logging

```python
# Security events to log
SECURITY_EVENTS = [
    'login_success',
    'login_failed',
    'logout',
    'password_changed',
    'api_key_updated',
    'account_added',
    'account_deleted',
    'admin_action',
    'rate_limit_exceeded',
    'unauthorized_access'
]

@app.after_request
def log_security_events(response):
    """Log security-relevant events"""
    if response.status_code == 403:
        log_activity('unauthorized_access', request.url)
    elif response.status_code == 429:
        log_activity('rate_limit_exceeded', request.url)
    
    return response
```

## Network Security

### 1. HTTPS Configuration

```python
# Production configuration
class ProductionConfig(Config):
    # Force HTTPS
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    
    # HSTS Header
    SECURITY_HSTS_SECONDS = 31536000  # 1 year
    SECURITY_HSTS_INCLUDE_SUBDOMAINS = True
    SECURITY_HSTS_PRELOAD = True
    
    # Additional security headers
    SECURITY_CONTENT_TYPE_NOSNIFF = True
    SECURITY_BROWSER_XSS_FILTER = True
    SECURITY_X_FRAME_OPTIONS = 'DENY'
```

### 2. CORS Configuration

```python
# app/__init__.py
from flask_cors import CORS

def create_app():
    app = Flask(__name__)
    
    # Configure CORS for API endpoints only
    CORS(app, resources={
        r"/api/*": {
            "origins": os.environ.get('CORS_ORIGINS', '').split(','),
            "methods": ["GET", "POST"],
            "allow_headers": ["Content-Type", "X-CSRFToken"],
            "supports_credentials": True
        }
    })
```

## Secure Configuration

### 1. Environment Variables

```bash
# .env.example
# Security Configuration
SECRET_KEY=<generate-strong-random-key>
ENCRYPTION_KEY=<base64-encoded-32-byte-key>

# Session Configuration
# Options: 'filesystem' (default, single-user) or 'sqlalchemy' (multi-user)
SESSION_TYPE=filesystem

# Database
DATABASE_URL=postgresql://user:pass@localhost/algomirror

# Rate Limiting
RATELIMIT_STORAGE_URL=redis://localhost:6379/1

# CORS
CORS_ORIGINS=https://app.example.com

# Security Headers
SECURITY_HEADERS_ENABLED=true
```

### 2. Secret Key Generation

```python
# Generate secure keys
import secrets
import base64
from cryptography.fernet import Fernet

# Generate Flask secret key
def generate_secret_key():
    return secrets.token_hex(32)

# Generate encryption key
def generate_encryption_key():
    return base64.urlsafe_b64encode(Fernet.generate_key()).decode()

print(f"SECRET_KEY={generate_secret_key()}")
print(f"ENCRYPTION_KEY={generate_encryption_key()}")
```

## Security Monitoring

### 1. Intrusion Detection

```python
class SecurityMonitor:
    """Monitor for suspicious activity"""
    
    def __init__(self):
        self.suspicious_patterns = [
            r'(?i)(union|select|insert|update|delete|drop)\s',  # SQL injection
            r'<script[^>]*>.*?</script>',  # XSS attempts
            r'\.\./|\.\.\\',  # Path traversal
            r'(?i)(cmd|powershell|bash|sh)\s',  # Command injection
        ]
    
    def check_request(self, request):
        """Check request for suspicious patterns"""
        data = str(request.values)
        
        for pattern in self.suspicious_patterns:
            if re.search(pattern, data):
                log_activity('suspicious_request', f'Pattern: {pattern}')
                return True
        
        return False
```

### 2. Failed Authentication Tracking

```python
# Track failed login attempts
failed_attempts = {}  # In production, use Redis

def record_login_attempt(ip_address):
    """Record failed login attempt"""
    if ip_address not in failed_attempts:
        failed_attempts[ip_address] = []
    
    failed_attempts[ip_address].append(datetime.now())
    
    # Keep only recent attempts (last hour)
    cutoff = datetime.now() - timedelta(hours=1)
    failed_attempts[ip_address] = [
        t for t in failed_attempts[ip_address] if t > cutoff
    ]

def check_login_attempts(ip_address):
    """Check if too many failed attempts"""
    if ip_address in failed_attempts:
        return len(failed_attempts[ip_address]) >= 5
    return False
```

## API Security

### 1. API Key Validation

```python
def validate_api_request(request):
    """Validate API request"""
    # Check API key
    api_key = request.headers.get('X-API-Key')
    if not api_key:
        return False, 'Missing API key'
    
    # Validate against stored keys
    account = TradingAccount.query.filter_by(
        api_key_hash=hash_api_key(api_key)
    ).first()
    
    if not account:
        return False, 'Invalid API key'
    
    # Check rate limits
    if check_rate_limit(api_key):
        return False, 'Rate limit exceeded'
    
    return True, account
```

### 2. Request Signing

```python
import hmac
import hashlib

def sign_request(data, secret):
    """Sign request data"""
    message = json.dumps(data, sort_keys=True)
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return signature

def verify_signature(data, signature, secret):
    """Verify request signature"""
    expected = sign_request(data, secret)
    return hmac.compare_digest(signature, expected)
```

## Security Best Practices

### 1. Development Guidelines
- Never commit secrets to version control
- Use environment variables for configuration
- Implement proper error handling
- Sanitize all user inputs
- Use parameterized queries

### 2. Deployment Checklist
- [ ] Enable HTTPS with valid SSL certificate
- [ ] Set strong SECRET_KEY
- [ ] Configure secure session cookies
- [ ] Enable all security headers
- [ ] Set up rate limiting with Redis
- [ ] Configure firewall rules
- [ ] Enable audit logging
- [ ] Set up monitoring and alerting
- [ ] Regular security updates
- [ ] Backup encryption keys securely

### 3. Regular Security Tasks
- Review audit logs weekly
- Update dependencies monthly
- Rotate API keys quarterly
- Security assessment annually
- Penetration testing annually

## Compliance & Regulations

### 1. Data Protection
- Encrypt sensitive data at rest
- Use TLS for data in transit
- Implement data retention policies
- Provide data export functionality
- Support data deletion requests

### 2. Audit Requirements
- Log all authentication events
- Track all configuration changes
- Record all API access
- Maintain audit trail for 1 year
- Regular audit log reviews

This comprehensive security implementation ensures AlgoMirror maintains enterprise-grade security standards while providing a seamless trading experience.