from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import os
from dotenv import load_dotenv
from app import db, login_manager

# Load environment variables first
load_dotenv()

# Generate or load encryption key
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if not ENCRYPTION_KEY:
    # If no key is set, generate one and save it for consistency
    ENCRYPTION_KEY = Fernet.generate_key()
    print(f"WARNING: No ENCRYPTION_KEY found in .env file. Generated new key. Please add to .env file:")
    print(f"ENCRYPTION_KEY={ENCRYPTION_KEY.decode()}")
    os.environ['ENCRYPTION_KEY'] = ENCRYPTION_KEY.decode()
else:
    ENCRYPTION_KEY = ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY

cipher_suite = Fernet(ENCRYPTION_KEY)


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    
    # Relationships
    accounts = db.relationship('TradingAccount', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    logs = db.relationship('ActivityLog', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def get_active_accounts(self):
        return self.accounts.filter_by(is_active=True).all()
    
    def get_primary_account(self):
        return self.accounts.filter_by(is_active=True, is_primary=True).first()
    
    def __repr__(self):
        return f'<User {self.username}>'

class TradingAccount(db.Model):
    __tablename__ = 'trading_accounts'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    account_name = db.Column(db.String(100), nullable=False)
    broker_name = db.Column(db.String(100), nullable=False)
    
    # OpenAlgo connection details (encrypted)
    host_url = db.Column(db.String(500), nullable=False)
    websocket_url = db.Column(db.String(500), nullable=False)
    api_key_encrypted = db.Column(db.Text, nullable=False)
    
    # Account status
    is_active = db.Column(db.Boolean, default=True)
    is_primary = db.Column(db.Boolean, default=False)
    last_connected = db.Column(db.DateTime)
    connection_status = db.Column(db.String(50), default='disconnected')
    
    # Account metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Cached account data
    last_funds_data = db.Column(db.JSON)
    last_positions_data = db.Column(db.JSON)
    last_holdings_data = db.Column(db.JSON)
    last_data_update = db.Column(db.DateTime)
    
    # Unique constraint for user and account name
    __table_args__ = (
        db.UniqueConstraint('user_id', 'account_name', name='_user_account_uc'),
    )
    
    def set_api_key(self, api_key):
        """Encrypt and store API key"""
        encrypted = cipher_suite.encrypt(api_key.encode())
        self.api_key_encrypted = encrypted.decode()
    
    def get_api_key(self):
        """Decrypt and return API key"""
        if self.api_key_encrypted:
            decrypted = cipher_suite.decrypt(self.api_key_encrypted.encode())
            return decrypted.decode()
        return None
    
    def __repr__(self):
        return f'<TradingAccount {self.account_name} - {self.broker_name}>'

class ActivityLog(db.Model):
    __tablename__ = 'activity_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=True)
    
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))
    
    status = db.Column(db.String(50), default='success')
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
    account = db.relationship('TradingAccount', backref='logs')
    
    def __repr__(self):
        return f'<ActivityLog {self.action} - {self.created_at}>'

class Order(db.Model):
    __tablename__ = 'orders'
    
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False)
    
    order_id = db.Column(db.String(100), nullable=False)
    symbol = db.Column(db.String(50), nullable=False)
    exchange = db.Column(db.String(20), nullable=False)
    action = db.Column(db.String(10), nullable=False)  # BUY/SELL
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float)
    order_type = db.Column(db.String(20))  # MARKET/LIMIT
    product = db.Column(db.String(20))  # MIS/CNC/NRML
    status = db.Column(db.String(50))
    
    placed_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    account = db.relationship('TradingAccount', backref='orders')
    
    # Unique constraint for account and order_id
    __table_args__ = (
        db.UniqueConstraint('account_id', 'order_id', name='_account_order_uc'),
    )
    
    def __repr__(self):
        return f'<Order {self.order_id} - {self.symbol}>'

class Position(db.Model):
    __tablename__ = 'positions'
    
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False)
    
    symbol = db.Column(db.String(50), nullable=False)
    exchange = db.Column(db.String(20), nullable=False)
    product = db.Column(db.String(20))
    quantity = db.Column(db.Integer, nullable=False)
    average_price = db.Column(db.Float)
    ltp = db.Column(db.Float)
    pnl = db.Column(db.Float)
    
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationship
    account = db.relationship('TradingAccount', backref='positions')
    
    # Unique constraint for account, symbol, exchange, and product
    __table_args__ = (
        db.UniqueConstraint('account_id', 'symbol', 'exchange', 'product', name='_account_position_uc'),
    )
    
    def __repr__(self):
        return f'<Position {self.symbol} - Qty: {self.quantity}>'

class Holding(db.Model):
    __tablename__ = 'holdings'
    
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False)
    
    symbol = db.Column(db.String(50), nullable=False)
    exchange = db.Column(db.String(20), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    average_price = db.Column(db.Float)
    ltp = db.Column(db.Float)
    pnl = db.Column(db.Float)
    pnl_percent = db.Column(db.Float)
    
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationship
    account = db.relationship('TradingAccount', backref='holdings')
    
    # Unique constraint for account, symbol, and exchange
    __table_args__ = (
        db.UniqueConstraint('account_id', 'symbol', 'exchange', name='_account_holding_uc'),
    )
    
    def __repr__(self):
        return f'<Holding {self.symbol} - Qty: {self.quantity}>'

class TradingHoursTemplate(db.Model):
    __tablename__ = 'trading_hours_templates'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    market = db.Column(db.String(50), default='NSE')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    sessions = db.relationship('TradingSession', backref='template', lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<TradingHoursTemplate {self.name}>'

class TradingSession(db.Model):
    __tablename__ = 'trading_sessions'
    
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('trading_hours_templates.id'), nullable=False)
    
    session_name = db.Column(db.String(100), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=Monday, 6=Sunday
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    session_type = db.Column(db.String(50))  # 'normal', 'pre_market', 'post_market'
    is_active = db.Column(db.Boolean, default=True)
    
    # Unique constraint for template, day, and session
    __table_args__ = (
        db.UniqueConstraint('template_id', 'day_of_week', 'session_name', name='_template_day_session_uc'),
    )
    
    def __repr__(self):
        return f'<TradingSession {self.session_name} - Day {self.day_of_week}>'

class Strategy(db.Model):
    __tablename__ = 'strategies'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    market_condition = db.Column(db.String(50))  # 'non_expiry', 'expiry', 'any'
    risk_profile = db.Column(db.String(50))  # 'balanced', 'conservative', 'aggressive'
    is_active = db.Column(db.Boolean, default=True)
    is_template = db.Column(db.Boolean, default=False)

    # Timing settings
    entry_time = db.Column(db.Time)
    exit_time = db.Column(db.Time)
    square_off_time = db.Column(db.Time)

    # Risk management
    max_loss = db.Column(db.Float)
    max_profit = db.Column(db.Float)
    trailing_sl = db.Column(db.Float)

    # Multi-account settings
    selected_accounts = db.Column(db.JSON)  # List of account IDs
    allocation_type = db.Column(db.String(50))  # 'equal', 'proportional', 'custom'

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    legs = db.relationship('StrategyLeg', backref='strategy', lazy='dynamic', cascade='all, delete-orphan')
    executions = db.relationship('StrategyExecution', backref='strategy', lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Strategy {self.name}>'

class StrategyLeg(db.Model):
    __tablename__ = 'strategy_legs'

    id = db.Column(db.Integer, primary_key=True)
    strategy_id = db.Column(db.Integer, db.ForeignKey('strategies.id'), nullable=False)
    leg_number = db.Column(db.Integer, nullable=False)

    # Instrument details
    instrument = db.Column(db.String(50))  # 'NIFTY', 'BANKNIFTY', 'SENSEX'
    product_type = db.Column(db.String(20))  # 'options', 'futures', 'equity'
    expiry = db.Column(db.String(50))  # 'current_week', 'next_week', 'current_month'
    action = db.Column(db.String(10))  # 'BUY', 'SELL'

    # Option specifics
    option_type = db.Column(db.String(10))  # 'CE', 'PE'
    strike_selection = db.Column(db.String(50))  # 'ATM', 'OTM', 'ITM', 'strike_price', 'premium_near'
    strike_offset = db.Column(db.Integer, default=0)
    strike_price = db.Column(db.Float)
    premium_value = db.Column(db.Float)

    # Order details
    order_type = db.Column(db.String(20))  # 'MARKET', 'LIMIT', 'SL-MKT', 'SL-LMT'
    limit_price = db.Column(db.Float)  # Price for LIMIT orders
    trigger_price = db.Column(db.Float)  # Trigger price for stop orders
    price_condition = db.Column(db.String(10))  # 'ABOVE' or 'BELOW' for LIMIT orders
    quantity = db.Column(db.Integer)
    lots = db.Column(db.Integer, default=1)

    # Exit conditions
    stop_loss_type = db.Column(db.String(20))  # 'percentage', 'points', 'premium'
    stop_loss_value = db.Column(db.Float)
    take_profit_type = db.Column(db.String(20))  # 'percentage', 'points', 'premium'
    take_profit_value = db.Column(db.Float)

    # Trailing stop loss
    enable_trailing = db.Column(db.Boolean, default=False)
    trailing_type = db.Column(db.String(20))  # 'percentage', 'points'
    trailing_value = db.Column(db.Float)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<StrategyLeg {self.instrument} {self.action}>'

class StrategyExecution(db.Model):
    __tablename__ = 'strategy_executions'

    id = db.Column(db.Integer, primary_key=True)
    strategy_id = db.Column(db.Integer, db.ForeignKey('strategies.id'), nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False)
    leg_id = db.Column(db.Integer, db.ForeignKey('strategy_legs.id'), nullable=False)

    # Order details
    order_id = db.Column(db.String(100))
    symbol = db.Column(db.String(100))  # Actual traded symbol
    exchange = db.Column(db.String(20))
    entry_price = db.Column(db.Float)
    exit_price = db.Column(db.Float)
    quantity = db.Column(db.Integer)

    # Status tracking
    status = db.Column(db.String(50))  # 'pending', 'entered', 'exited', 'stopped', 'error'
    entry_time = db.Column(db.DateTime)
    exit_time = db.Column(db.DateTime)

    # P&L tracking
    realized_pnl = db.Column(db.Float)
    unrealized_pnl = db.Column(db.Float)
    brokerage = db.Column(db.Float)

    # Exit reason
    exit_reason = db.Column(db.String(100))  # 'stop_loss', 'take_profit', 'square_off', 'manual'

    # Error tracking
    error_message = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = db.relationship('TradingAccount')
    leg = db.relationship('StrategyLeg')

    def __repr__(self):
        return f'<StrategyExecution {self.symbol} {self.status}>'

class MarketHoliday(db.Model):
    __tablename__ = 'market_holidays'
    
    id = db.Column(db.Integer, primary_key=True)
    holiday_date = db.Column(db.Date, nullable=False, unique=True)
    holiday_name = db.Column(db.String(200), nullable=False)
    market = db.Column(db.String(50), default='NSE')
    holiday_type = db.Column(db.String(50))  # 'trading', 'settlement', 'both'
    is_special_session = db.Column(db.Boolean, default=False)
    special_start_time = db.Column(db.Time)
    special_end_time = db.Column(db.Time)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<MarketHoliday {self.holiday_date} - {self.holiday_name}>'

class SpecialTradingSession(db.Model):
    __tablename__ = 'special_trading_sessions'
    
    id = db.Column(db.Integer, primary_key=True)
    session_date = db.Column(db.Date, nullable=False)
    session_name = db.Column(db.String(200), nullable=False)  # e.g., 'Muhurat Trading', 'Special Session'
    market = db.Column(db.String(50), default='NSE')
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Unique constraint for date and market
    __table_args__ = (
        db.UniqueConstraint('session_date', 'market', 'session_name', name='_date_market_session_uc'),
    )
    
    def __repr__(self):
        return f'<SpecialTradingSession {self.session_date} - {self.session_name}>'

class TradingSettings(db.Model):
    __tablename__ = 'trading_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)  # Fixed table name
    symbol = db.Column(db.String(50), nullable=False)  # 'NIFTY', 'BANKNIFTY', 'SENSEX'
    lot_size = db.Column(db.Integer, nullable=False, default=25)
    freeze_quantity = db.Column(db.Integer, nullable=False, default=1800)
    max_lots_per_order = db.Column(db.Integer, default=36)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationship
    user = db.relationship('User', backref='trading_settings')
    
    # Unique constraint for user and symbol
    __table_args__ = (
        db.UniqueConstraint('user_id', 'symbol', name='_user_symbol_uc'),
    )
    
    def __repr__(self):
        return f'<TradingSettings {self.symbol} - Lot: {self.lot_size}, Freeze: {self.freeze_quantity}>'
    
    @staticmethod
    def get_or_create_defaults(user_id):
        """Create default settings for NIFTY, BANKNIFTY, and SENSEX if they don't exist"""
        # Lot sizes from instructions.md (as of May 2025)
        # Freeze quantities are based on exchange rules
        defaults = [
            {'symbol': 'NIFTY', 'lot_size': 75, 'freeze_quantity': 1800, 'max_lots_per_order': 24},
            {'symbol': 'BANKNIFTY', 'lot_size': 35, 'freeze_quantity': 900, 'max_lots_per_order': 25},
            {'symbol': 'SENSEX', 'lot_size': 20, 'freeze_quantity': 1000, 'max_lots_per_order': 50}
        ]
        
        for default in defaults:
            setting = TradingSettings.query.filter_by(
                user_id=user_id, 
                symbol=default['symbol']
            ).first()
            
            if not setting:
                setting = TradingSettings(
                    user_id=user_id,
                    symbol=default['symbol'],
                    lot_size=default['lot_size'],
                    freeze_quantity=default['freeze_quantity'],
                    max_lots_per_order=default['max_lots_per_order']
                )
                db.session.add(setting)
        
        db.session.commit()

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))