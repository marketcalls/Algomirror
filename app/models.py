from datetime import datetime
from flask_login import UserMixin
from sqlalchemy import or_, update as sa_update
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import os
import pytz
from dotenv import load_dotenv
from app import db, login_manager

# IST timezone for storing timestamps
IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    """Get current time in IST (naive datetime for DB storage)"""
    return datetime.now(IST).replace(tzinfo=None)

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
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    account_name = db.Column(db.String(100), nullable=False)
    broker_name = db.Column(db.String(100), nullable=False)

    # OpenAlgo connection details (encrypted)
    host_url = db.Column(db.String(500), nullable=False)
    websocket_url = db.Column(db.String(500), nullable=False)
    api_key_encrypted = db.Column(db.Text, nullable=False)

    # Account status
    is_active = db.Column(db.Boolean, default=True, index=True)
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
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)  # Allow NULL for failed login attempts
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
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)

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
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    market_condition = db.Column(db.String(50))  # 'non_expiry', 'expiry', 'any'
    risk_profile = db.Column(db.String(50))  # 'fixed_lots' (default), 'balanced', 'conservative', 'aggressive'
    is_active = db.Column(db.Boolean, default=True, index=True)
    is_template = db.Column(db.Boolean, default=False)

    # Timing settings
    entry_time = db.Column(db.Time)
    exit_time = db.Column(db.Time)
    square_off_time = db.Column(db.Time)

    # Risk management
    max_loss = db.Column(db.Float)
    max_profit = db.Column(db.Float)
    trailing_sl = db.Column(db.Float)

    # Risk monitoring configuration
    risk_monitoring_enabled = db.Column(db.Boolean, default=True)
    risk_check_interval = db.Column(db.Integer, default=1)  # Seconds between checks
    auto_exit_on_max_loss = db.Column(db.Boolean, default=True)
    auto_exit_on_max_profit = db.Column(db.Boolean, default=True)
    trailing_sl_type = db.Column(db.String(20), default='amount')  # 'amount' (rupees, default), 'percentage', 'points' (legacy)

    # Trailing SL tracking state (AFL-style ratcheting stop)
    # Logic: stop_level = peak_pnl * (1 - trailing_pct/100)
    # Stop only moves UP (ratchets), never down
    # Exit when current_pnl < trailing_stop
    trailing_sl_active = db.Column(db.Boolean, default=False)  # Is TSL currently tracking
    trailing_sl_peak_pnl = db.Column(db.Float, default=0.0)  # Highest P&L reached (like "High" in AFL)
    trailing_sl_initial_stop = db.Column(db.Float)  # First stop level when TSL activated
    trailing_sl_trigger_pnl = db.Column(db.Float)  # Current trailing stop (ratchets up, like trailARRAY in AFL)
    trailing_sl_triggered_at = db.Column(db.DateTime)  # When TSL was triggered (if ever)
    trailing_sl_exit_reason = db.Column(db.String(200))  # Stores TSL exit reason

    # Max Loss/Profit exit tracking
    max_loss_triggered_at = db.Column(db.DateTime)  # When max loss was triggered
    max_loss_exit_reason = db.Column(db.String(200))  # Stores max loss exit reason
    max_profit_triggered_at = db.Column(db.DateTime)  # When max profit was triggered
    max_profit_exit_reason = db.Column(db.String(200))  # Stores max profit exit reason

    # Supertrend-based exit
    supertrend_exit_enabled = db.Column(db.Boolean, default=False)
    supertrend_exit_type = db.Column(db.String(20))  # 'breakout' or 'breakdown'
    supertrend_period = db.Column(db.Integer, default=10)
    supertrend_multiplier = db.Column(db.Float, default=3.0)
    supertrend_timeframe = db.Column(db.String(10), default='10m')
    supertrend_exit_triggered = db.Column(db.Boolean, default=False)  # Track if exit was already executed
    supertrend_exit_reason = db.Column(db.String(200))  # Stores the exit reason (e.g., "Breakout at Close: 150.25, ST: 145.50")
    supertrend_exit_triggered_at = db.Column(db.DateTime)  # When the exit was triggered

    # Order settings
    product_order_type = db.Column(db.String(10), default='NRML')  # 'NRML' or 'MIS'

    # Multi-account settings
    selected_accounts = db.Column(db.JSON)  # List of account IDs
    allocation_type = db.Column(db.String(50))  # 'equal', 'proportional', 'custom'

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    legs = db.relationship('StrategyLeg', backref='strategy', lazy='dynamic', cascade='all, delete-orphan')
    executions = db.relationship('StrategyExecution', backref='strategy', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def total_pnl(self):
        """
        Calculate total P&L for this strategy (realized + unrealized)

        Returns:
            float: Total P&L from all executions
        """
        total = 0.0

        # Get all executions for this strategy
        all_executions = self.executions.all()

        for execution in all_executions:
            # Skip failed, rejected, or cancelled executions
            if execution.status == 'error' or execution.status == 'failed':
                continue
            if hasattr(execution, 'broker_order_status') and execution.broker_order_status in ['rejected', 'cancelled']:
                continue

            # Add realized P&L (from closed positions)
            if execution.realized_pnl is not None:
                total += execution.realized_pnl

            # Add unrealized P&L (from open positions)
            if execution.unrealized_pnl is not None and execution.status == 'entered':
                total += execution.unrealized_pnl

        return total

    @property
    def realized_pnl(self):
        """
        Calculate total realized P&L for this strategy (only from closed positions)

        Returns:
            float: Total realized P&L
        """
        total = 0.0

        # Get all executions for this strategy
        all_executions = self.executions.all()

        for execution in all_executions:
            # Skip failed, rejected, or cancelled executions
            if execution.status == 'error' or execution.status == 'failed':
                continue
            if hasattr(execution, 'broker_order_status') and execution.broker_order_status in ['rejected', 'cancelled']:
                continue

            # Add only realized P&L
            if execution.realized_pnl is not None:
                total += execution.realized_pnl

        return total

    @property
    def unrealized_pnl(self):
        """
        Calculate total unrealized P&L for this strategy (only from open positions)

        Returns:
            float: Total unrealized P&L
        """
        total = 0.0

        # Get all executions with open positions
        all_executions = self.executions.filter_by(status='entered').all()

        for execution in all_executions:
            # Skip rejected or cancelled executions
            if hasattr(execution, 'broker_order_status') and execution.broker_order_status in ['rejected', 'cancelled']:
                continue

            # Add unrealized P&L
            if execution.unrealized_pnl is not None:
                total += execution.unrealized_pnl

        return total

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

    # Execution status
    is_executed = db.Column(db.Boolean, default=False)  # True if leg has been executed (orders placed)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<StrategyLeg {self.instrument} {self.action}>'

class StrategyExecution(db.Model):
    __tablename__ = 'strategy_executions'

    id = db.Column(db.Integer, primary_key=True)
    strategy_id = db.Column(db.Integer, db.ForeignKey('strategies.id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)
    leg_id = db.Column(db.Integer, db.ForeignKey('strategy_legs.id'), nullable=False)

    # Order details
    order_id = db.Column(db.String(100))  # Entry order ID
    exit_order_id = db.Column(db.String(100))  # Exit order ID from OpenAlgo
    symbol = db.Column(db.String(100))  # Actual traded symbol
    exchange = db.Column(db.String(20))
    product = db.Column(db.String(10))  # Order product type: MIS, NRML, CNC
    entry_price = db.Column(db.Float)
    exit_price = db.Column(db.Float)
    quantity = db.Column(db.Integer)

    # Status tracking
    status = db.Column(db.String(50), index=True)  # 'pending', 'entered', 'exited', 'stopped', 'error'
    broker_order_status = db.Column(db.String(50))  # Actual status from broker: 'complete', 'open', 'rejected', etc.
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

    # Real-time monitoring (WebSocket optimization)
    last_price = db.Column(db.Float)  # Latest price from WebSocket
    last_price_updated = db.Column(db.DateTime)  # When price was last updated
    websocket_subscribed = db.Column(db.Boolean, default=False)  # Is this position being monitored via WebSocket?
    trailing_sl_triggered = db.Column(db.Float)  # Price at which trailing SL was triggered

    # Risk event capture (persists once triggered)
    sl_hit_at = db.Column(db.DateTime)  # When SL was hit
    sl_hit_price = db.Column(db.Float)  # Price when SL was hit
    tp_hit_at = db.Column(db.DateTime)  # When TP was hit
    tp_hit_price = db.Column(db.Float)  # Price when TP was hit

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
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)  # Fixed table name
    symbol = db.Column(db.String(50), nullable=False, index=True)  # 'NIFTY', 'BANKNIFTY', 'SENSEX'
    lot_size = db.Column(db.Integer, nullable=False, default=25)  # Current month lot size
    next_month_lot_size = db.Column(db.Integer, nullable=True)  # Next month lot size (for new contracts with different lot size)
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
        return f'<TradingSettings {self.symbol} - Lot: {self.lot_size}, NextLot: {self.next_month_lot_size}, Freeze: {self.freeze_quantity}>'
    
    @staticmethod
    def get_or_create_defaults(user_id):
        """Create default settings for NIFTY, BANKNIFTY, and SENSEX if they don't exist"""
        # Lot sizes: current month and next month (Jan 2025 onwards for NSE)
        # BSE (SENSEX) has no lot size change
        # Freeze quantities are based on exchange rules
        # Updated freeze quantities as per NSE circular effective Dec 1, 2025
        defaults = [
            {'symbol': 'NIFTY', 'lot_size': 65, 'next_month_lot_size': 65, 'freeze_quantity': 1755, 'max_lots_per_order': 27},
            {'symbol': 'BANKNIFTY', 'lot_size': 30, 'next_month_lot_size': 30, 'freeze_quantity': 600, 'max_lots_per_order': 20},
            {'symbol': 'SENSEX', 'lot_size': 20, 'next_month_lot_size': 20, 'freeze_quantity': 1000, 'max_lots_per_order': 50},
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
                    next_month_lot_size=default.get('next_month_lot_size'),
                    freeze_quantity=default['freeze_quantity'],
                    max_lots_per_order=default['max_lots_per_order']
                )
                db.session.add(setting)

        db.session.commit()

class MarginRequirement(db.Model):
    __tablename__ = 'margin_requirements'

    # Default values as class constants (for reference in other modules)
    DEFAULT_OPTION_BUYING_PREMIUM = 20000  # Rs 20,000 per lot for NIFTY/BANKNIFTY
    DEFAULT_SENSEX_OPTION_BUYING_PREMIUM = 20000  # Rs 20,000 per lot for SENSEX

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    instrument = db.Column(db.String(50), nullable=False)  # 'NIFTY', 'BANKNIFTY', 'SENSEX'

    # Option Selling - Margin values for different trade types (in INR per lot)
    ce_pe_sell_expiry = db.Column(db.Float, default=205000)  # CE/PE Sell on Expiry
    ce_pe_sell_non_expiry = db.Column(db.Float, default=250000)  # CE/PE Sell on Non-Expiry
    ce_and_pe_sell_expiry = db.Column(db.Float, default=250000)  # CE & PE Sell on Expiry
    ce_and_pe_sell_non_expiry = db.Column(db.Float, default=320000)  # CE & PE Sell on Non-Expiry
    futures_expiry = db.Column(db.Float, default=215000)  # Futures on Expiry
    futures_non_expiry = db.Column(db.Float, default=215000)  # Futures on Non-Expiry

    # Option Buying - Premium per lot (used to calculate lot size from cash margin)
    option_buying_premium = db.Column(db.Float, default=DEFAULT_OPTION_BUYING_PREMIUM)  # Premium per lot for NIFTY/BANKNIFTY

    # SENSEX specific margins (Option Selling)
    sensex_ce_pe_sell_expiry = db.Column(db.Float, default=180000)
    sensex_ce_pe_sell_non_expiry = db.Column(db.Float, default=220000)
    sensex_ce_and_pe_sell_expiry = db.Column(db.Float, default=225000)
    sensex_ce_and_pe_sell_non_expiry = db.Column(db.Float, default=290000)
    sensex_futures_expiry = db.Column(db.Float, default=185000)
    sensex_futures_non_expiry = db.Column(db.Float, default=185000)

    # SENSEX Option Buying - Premium per lot
    sensex_option_buying_premium = db.Column(db.Float, default=DEFAULT_SENSEX_OPTION_BUYING_PREMIUM)

    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = db.relationship('User', backref='margin_requirements')

    # Unique constraint for user and instrument
    __table_args__ = (
        db.UniqueConstraint('user_id', 'instrument', name='_user_instrument_margin_uc'),
    )

    def __repr__(self):
        return f'<MarginRequirement {self.instrument} - User {self.user_id}>'

    @staticmethod
    def get_or_create_defaults(user_id):
        """Create default margin requirements if they don't exist"""
        defaults = [
            {
                'instrument': 'NIFTY',
                'ce_pe_sell_expiry': 205000,
                'ce_pe_sell_non_expiry': 250000,
                'ce_and_pe_sell_expiry': 250000,
                'ce_and_pe_sell_non_expiry': 320000,
                'futures_expiry': 215000,
                'futures_non_expiry': 215000,
                'option_buying_premium': 20000
            },
            {
                'instrument': 'BANKNIFTY',
                'ce_pe_sell_expiry': 205000,
                'ce_pe_sell_non_expiry': 250000,
                'ce_and_pe_sell_expiry': 250000,
                'ce_and_pe_sell_non_expiry': 320000,
                'futures_expiry': 215000,
                'futures_non_expiry': 215000,
                'option_buying_premium': 20000
            },
            {
                'instrument': 'SENSEX',
                'ce_pe_sell_expiry': 180000,
                'ce_pe_sell_non_expiry': 220000,
                'ce_and_pe_sell_expiry': 225000,
                'ce_and_pe_sell_non_expiry': 290000,
                'futures_expiry': 185000,
                'futures_non_expiry': 185000,
                'sensex_option_buying_premium': 20000
            }
        ]

        for default in defaults:
            margin = MarginRequirement.query.filter_by(
                user_id=user_id,
                instrument=default['instrument']
            ).first()

            if not margin:
                margin = MarginRequirement(
                    user_id=user_id,
                    **default
                )
                db.session.add(margin)

        db.session.commit()

class TradeQuality(db.Model):
    __tablename__ = 'trade_qualities'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    quality_grade = db.Column(db.String(10), nullable=False)  # 'A', 'B', 'C'
    margin_percentage = db.Column(db.Float, nullable=False)  # 95%, 65%, 36%
    risk_level = db.Column(db.String(20))  # 'conservative', 'moderate', 'aggressive'
    description = db.Column(db.Text)
    # Margin source: 'available' (cash + collateral) for sellers/hedgers
    #                'cash' (cash only) for option buyers
    margin_source = db.Column(db.String(20), default='available')  # 'available' or 'cash'
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = db.relationship('User', backref='trade_qualities')

    # Unique constraint
    __table_args__ = (
        db.UniqueConstraint('user_id', 'quality_grade', name='_user_quality_uc'),
    )

    def __repr__(self):
        return f'<TradeQuality {self.quality_grade} - {self.margin_percentage}%>'

    @staticmethod
    def get_or_create_defaults(user_id):
        """Create default trade qualities if they don't exist"""
        defaults = [
            {
                'quality_grade': 'A',
                'margin_percentage': 95.0,
                'risk_level': 'aggressive',
                'description': 'Aggressive approach - Uses 95% of available margin (higher risk)'
            },
            {
                'quality_grade': 'B',
                'margin_percentage': 65.0,
                'risk_level': 'moderate',
                'description': 'Moderate approach - Uses 65% of available margin'
            },
            {
                'quality_grade': 'C',
                'margin_percentage': 36.0,
                'risk_level': 'conservative',
                'description': 'Conservative approach - Uses 36% of available margin (lower risk)'
            }
        ]

        for default in defaults:
            quality = TradeQuality.query.filter_by(
                user_id=user_id,
                quality_grade=default['quality_grade']
            ).first()

            if not quality:
                quality = TradeQuality(
                    user_id=user_id,
                    **default
                )
                db.session.add(quality)
            else:
                # Fix existing incorrect labels (Grade A was 'conservative', Grade C was 'aggressive')
                if quality.quality_grade == 'A' and quality.risk_level == 'conservative':
                    quality.risk_level = 'aggressive'
                    quality.description = default['description']
                elif quality.quality_grade == 'C' and quality.risk_level == 'aggressive':
                    quality.risk_level = 'conservative'
                    quality.description = default['description']

        db.session.commit()

class MarginTracker(db.Model):
    __tablename__ = 'margin_trackers'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False)

    # Available margins
    total_available_margin = db.Column(db.Float, default=0)
    used_margin = db.Column(db.Float, default=0)
    free_margin = db.Column(db.Float, default=0)

    # F&O specific margins
    span_margin = db.Column(db.Float, default=0)
    exposure_margin = db.Column(db.Float, default=0)
    option_premium = db.Column(db.Float, default=0)

    # Trade-wise margin allocation
    allocated_margins = db.Column(db.JSON)  # {"trade_id": margin_amount, ...}

    # Real-time tracking
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)
    update_count = db.Column(db.Integer, default=0)

    # Relationship
    account = db.relationship('TradingAccount', backref='margin_tracker')

    def update_margins(self, funds_data):
        """Update margins from funds API response"""
        # OpenAlgo returns 'availablecash' which is already net of used margin
        self.total_available_margin = funds_data.get('availablecash', 0)
        # OpenAlgo returns 'utiliseddebits' for margin currently in use
        self.used_margin = funds_data.get('utiliseddebits', 0)
        # availablecash is already the free margin (broker has deducted utiliseddebits)
        self.free_margin = self.total_available_margin
        self.span_margin = funds_data.get('spanmargin', 0)
        self.exposure_margin = funds_data.get('exposuremargin', 0)
        self.option_premium = funds_data.get('optionpremium', 0)
        self.last_updated = datetime.utcnow()
        # Handle None case for update_count
        if self.update_count is None:
            self.update_count = 1
        else:
            self.update_count += 1

    def allocate_margin(self, trade_id, margin_amount):
        """Allocate margin to a specific trade"""
        if not self.allocated_margins:
            self.allocated_margins = {}
        self.allocated_margins[str(trade_id)] = margin_amount
        # Handle None cases
        if self.used_margin is None:
            self.used_margin = margin_amount
        else:
            self.used_margin += margin_amount
        if self.free_margin is None:
            self.free_margin = -margin_amount
        else:
            self.free_margin -= margin_amount

    def release_margin(self, trade_id):
        """Release margin from a completed trade"""
        if self.allocated_margins and str(trade_id) in self.allocated_margins:
            margin_amount = self.allocated_margins.pop(str(trade_id))
            # Handle None cases
            if self.used_margin is not None:
                self.used_margin -= margin_amount
            if self.free_margin is not None:
                self.free_margin += margin_amount

    def __repr__(self):
        return f'<MarginTracker Account {self.account_id} - Free: {self.free_margin}>'

class WebSocketSession(db.Model):
    """
    Tracks active WebSocket sessions for option chain viewing
    Used for on-demand option chain loading
    """
    __tablename__ = 'websocket_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    session_id = db.Column(db.String(64), unique=True, nullable=False)
    underlying = db.Column(db.String(20), nullable=False)  # NIFTY, BANKNIFTY, SENSEX
    expiry = db.Column(db.String(20), nullable=False)
    subscribed_symbols = db.Column(db.JSON)  # List of subscribed symbols
    is_active = db.Column(db.Boolean, default=True)
    last_heartbeat = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)  # Auto-cleanup old sessions

    # Relationship
    user = db.relationship('User', backref='websocket_sessions')

    def __repr__(self):
        return f'<WebSocketSession {self.session_id} - {self.underlying} {self.expiry}>'

    def update_heartbeat(self):
        """Update last heartbeat timestamp"""
        self.last_heartbeat = datetime.utcnow()
        # Extend expiry by 5 minutes from last heartbeat
        from datetime import timedelta
        self.expires_at = datetime.utcnow() + timedelta(minutes=5)

    def is_expired(self):
        """Check if session has expired"""
        if not self.expires_at:
            return False
        return datetime.utcnow() > self.expires_at

class RiskEvent(db.Model):
    """
    Audit log for risk threshold triggers
    Tracks Max Loss, Max Profit, Trailing SL, and Supertrend exits
    """
    __tablename__ = 'risk_events'

    id = db.Column(db.Integer, primary_key=True)
    strategy_id = db.Column(db.Integer, db.ForeignKey('strategies.id'), nullable=False)
    execution_id = db.Column(db.Integer, db.ForeignKey('strategy_executions.id'), nullable=True)
    event_type = db.Column(db.String(50), nullable=False)  # 'max_loss', 'max_profit', 'trailing_sl', 'supertrend'
    threshold_value = db.Column(db.Float)  # The threshold that was breached
    current_value = db.Column(db.Float)  # Current P&L or price
    action_taken = db.Column(db.String(50))  # 'close_all', 'close_partial', 'alert_only'
    exit_order_ids = db.Column(db.JSON)  # List of exit orders placed
    triggered_at = db.Column(db.DateTime, default=get_ist_now)
    notes = db.Column(db.Text)

    # Relationships - cascade delete when Strategy/Execution is deleted
    strategy = db.relationship('Strategy', backref=db.backref('risk_events', cascade='all, delete-orphan'))
    execution = db.relationship('StrategyExecution', backref=db.backref('execution_risk_events', cascade='all, delete-orphan'))

    def __repr__(self):
        return f'<RiskEvent {self.event_type} - Strategy {self.strategy_id} at {self.triggered_at}>'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------------------------------------------------------------------------
# Equity Trading Module
#
# Product code note: OpenAlgo uses CNC for equity delivery and NRML for F&O
# carry forward. The equity module is delivery only, so every equity order and
# every cost calculation uses CNC. Import EQUITY_PRODUCT_CNC instead of writing
# the literal string anywhere else.
# ---------------------------------------------------------------------------

EQUITY_PRODUCT_CNC = 'CNC'

# Intraday. Used for ONE thing and nothing else: the part of a sell that the
# account does not own, which would be a short delivery as CNC. It is not a
# product the admin chooses; it appears only where a sell has run out of
# shares, and every MIS position this application opens must be bought back
# the same day. See EquityIntradayShort.
EQUITY_PRODUCT_MIS = 'MIS'
EQUITY_PRODUCTS = (EQUITY_PRODUCT_CNC, EQUITY_PRODUCT_MIS)

# Order side values
EQUITY_SIDE_BUY = 'BUY'
EQUITY_SIDE_SELL = 'SELL'

# Parent order type values
EQUITY_ORDER_TYPE_MARKET = 'MARKET'
EQUITY_ORDER_TYPE_LIMIT = 'LIMIT'
EQUITY_ORDER_TYPE_GTT = 'GTT'

# Stop loss market: rests at the exchange until the trigger, then goes at
# market. Used for ONE thing - the protective buy that sits behind an intraday
# short - and never offered as a choice on the order form.
#
# SL-M rather than SL on purpose. SL becomes a LIMIT once triggered and can sit
# unfilled while the price runs away from it, which on a short is the exact
# scenario the stop exists for. SL-M gets out.
EQUITY_ORDER_TYPE_SL_M = 'SL-M'

# Parent order status values
EQUITY_ORDER_STATUS_PENDING = 'PENDING'
EQUITY_ORDER_STATUS_PARTIAL = 'PARTIAL'
EQUITY_ORDER_STATUS_COMPLETED = 'COMPLETED'
EQUITY_ORDER_STATUS_CANCELLED = 'CANCELLED'

# Holding exit mode values
EQUITY_EXIT_MODE_AUTO = 'AUTO'
EQUITY_EXIT_MODE_CONFIRM = 'CONFIRM'

# What created a parent equity order. MANUAL is an admin action from Place
# Order, Watch List or a Holdings row action. STOP_LOSS and TARGET are raised
# by the stop loss / target monitor, either automatically (AUTO exit mode) or
# after the admin approved the alert (CONFIRM exit mode).
EQUITY_ORDER_SOURCE_MANUAL = 'MANUAL'
EQUITY_ORDER_SOURCE_STOP_LOSS = 'STOP_LOSS'
EQUITY_ORDER_SOURCE_TARGET = 'TARGET'

# Why a holding is being exited. Shared by EquityHolding.exit_reason and by the
# claim helpers, and mapped onto EQUITY_ORDER_SOURCE_* on the order that
# carries the exit.
EQUITY_EXIT_REASON_STOP_LOSS = 'STOP_LOSS'
EQUITY_EXIT_REASON_TARGET = 'TARGET'
EQUITY_EXIT_REASON_MANUAL = 'MANUAL'

# Per-account fill status on EquityOrderSplit.
#
# The first four mirror the parent order vocabulary so a single account split
# reads the same way as the parent. The rest describe outcomes that only exist
# per account, and the difference between them is a safety rule, not cosmetic:
#
#   FAILED         the broker gave a definite placement error. Nothing reached
#                  the broker, so re-sending cannot create a duplicate.
#   REJECTED       the order reached the broker and was then rejected. There is
#                  a broker order id but no position.
#   INDETERMINATE  the request timed out or the connection dropped. The order
#                  MAY be live at the broker. This is terminal: never retry it
#                  automatically, it has to be reconciled. See
#                  EQUITY_INDETERMINATE_ERROR_TYPES.
#   SKIPPED        no broker call was made, because pre-trade validation
#                  rejected this account (for example insufficient cash under
#                  the SKIP funds policy).
#   UNSUPPORTED    the broker cannot serve this order type at all, which is how
#                  a GTT lands on a broker whose OpenAlgo build has no gtt_api.
#                  Terminal for that account, the other accounts still proceed.
EQUITY_SPLIT_STATUS_PENDING = EQUITY_ORDER_STATUS_PENDING
EQUITY_SPLIT_STATUS_PARTIAL = EQUITY_ORDER_STATUS_PARTIAL
EQUITY_SPLIT_STATUS_COMPLETED = EQUITY_ORDER_STATUS_COMPLETED
EQUITY_SPLIT_STATUS_CANCELLED = EQUITY_ORDER_STATUS_CANCELLED
EQUITY_SPLIT_STATUS_FAILED = 'FAILED'
EQUITY_SPLIT_STATUS_REJECTED = 'REJECTED'
EQUITY_SPLIT_STATUS_INDETERMINATE = 'INDETERMINATE'
EQUITY_SPLIT_STATUS_SKIPPED = 'SKIPPED'
EQUITY_SPLIT_STATUS_UNSUPPORTED = 'UNSUPPORTED'

# Splits that are still working at the broker and can be modified or cancelled
EQUITY_SPLIT_STATUSES_OPEN = (
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_PARTIAL,
)

# Splits that will never change again without a broker event
EQUITY_SPLIT_STATUSES_TERMINAL = (
    EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_FAILED,
    EQUITY_SPLIT_STATUS_REJECTED,
    EQUITY_SPLIT_STATUS_INDETERMINATE,
    EQUITY_SPLIT_STATUS_SKIPPED,
    EQUITY_SPLIT_STATUS_UNSUPPORTED,
)

# The only statuses a placement may be re-sent from. INDETERMINATE is
# deliberately absent: re-sending it is how you buy the same stock twice.
EQUITY_SPLIT_STATUSES_SAFE_TO_RETRY = (
    EQUITY_SPLIT_STATUS_FAILED,
    EQUITY_SPLIT_STATUS_REJECTED,
)

# Error types that represent a DEFINITE refusal: the request reached OpenAlgo,
# OpenAlgo answered, and the answer was no. Only these are safe to re-send.
#
#   api_error   OpenAlgo returned status 'error' with a message. The order was
#               understood and refused.
#   http_error  Safe ONLY when the status code is a 4xx. A 5xx means the request
#               was accepted and then something broke, which is ambiguous.
#
# Everything else is indeterminate. That deliberately includes json_error (the
# response arrived but could not be parsed, so the order may well be live) and
# unknown_error (an exception we did not anticipate). Enumerating the safe cases
# and defaulting to indeterminate is the right way round: a new or unforeseen
# error type then fails safe instead of being re-sent.
EQUITY_DEFINITE_REFUSAL_ERROR_TYPES = ('api_error',)

# Kept for callers that import it. These never got an answer at all.
EQUITY_INDETERMINATE_ERROR_TYPES = ('timeout_error', 'connection_error')


def equity_is_indeterminate_response(response):
    """
    Decide whether a broker response is indeterminate rather than a rejection.

    This is the single place that rule is written down. Import it instead of
    comparing error_type strings at the call site, so that every equity code
    path agrees on what "we do not know if the order was placed" means.

    Args:
        response: the dict returned by an ExtendedOpenAlgoAPI call, or None

    Returns:
        True when the outcome is unknown (timeout or connection failure), so
        the order must NOT be retried. False for a definite broker rejection
        and for a successful response.
    """
    if not isinstance(response, dict):
        # No response object at all means the call blew up before it returned.
        # Treat that as unknown, which is the safe side of this decision.
        return True

    if str(response.get('status', '')).lower() == 'success':
        return False

    error_type = response.get('error_type')

    if error_type in EQUITY_DEFINITE_REFUSAL_ERROR_TYPES:
        return False

    if error_type == 'http_error':
        # A 4xx is a refusal we can re-send. A 5xx, or a code we cannot read, is
        # ambiguous: the request may have reached the broker before it failed.
        #
        # 501 Not Implemented is the exception. It is the answer a broker without
        # GTT support gives, and it means the endpoint does not exist, so the
        # order definitely was not placed. Treating it as ambiguous would strand
        # every GTT attempt against Upstox, Fyers and Angel One as
        # INDETERMINATE instead of cleanly reporting the broker cannot do it.
        code = response.get('code')
        try:
            code = int(code)
        except (TypeError, ValueError):
            return True
        if code == 501:
            return False
        return not (400 <= code < 500)

    # Anything else, including json_error, unknown_error and an error_type this
    # code has never seen, is unknown. Never re-send it.
    return True


# Exit lifecycle for one holding. This is the claim that stops the stop loss
# monitor and a manual sell from selling the same shares twice.
#
#   ACTIVE              nothing in flight, the monitor watches this row
#   AWAITING_CONFIRM    a level was breached on a CONFIRM mode holding and the
#                       admin has not approved the sell yet
#   EXIT_PENDING        claimed, committed, the broker call is about to run or
#                       is running. No other path may touch this row.
#   EXIT_SUBMITTED      the broker accepted the sell and gave an order id
#   EXIT_INDETERMINATE  the sell request never got an answer. Terminal until a
#                       human reconciles it, never auto retried.
#   EXITED              the sell filled and the holding is flat
# An intraday short's life. OPEN means shares are owed and the clock is
# running; everything else means a buy-back is in flight or done.
EQUITY_SHORT_STATUS_OPEN = 'OPEN'
EQUITY_SHORT_STATUS_COVER_PENDING = 'COVER_PENDING'
EQUITY_SHORT_STATUS_COVER_SUBMITTED = 'COVER_SUBMITTED'
EQUITY_SHORT_STATUS_COVER_INDETERMINATE = 'COVER_INDETERMINATE'
EQUITY_SHORT_STATUS_COVERED = 'COVERED'

# The protective order's own life, kept apart from the short's.
EQUITY_STOP_STATUS_NONE = 'NONE'
EQUITY_STOP_STATUS_RESTING = 'RESTING'
EQUITY_STOP_STATUS_CANCELLED = 'CANCELLED'
EQUITY_STOP_STATUS_TRIGGERED = 'TRIGGERED'
EQUITY_STOP_STATUS_FAILED = 'FAILED'

# Claimable: the buy-back has not started. Only OPEN qualifies.
EQUITY_SHORT_STATUSES_CLAIMABLE = (EQUITY_SHORT_STATUS_OPEN,)

# In flight: a buy-back is at the broker, or may be. Nothing may claim these.
EQUITY_SHORT_STATUSES_IN_FLIGHT = (
    EQUITY_SHORT_STATUS_COVER_PENDING,
    EQUITY_SHORT_STATUS_COVER_SUBMITTED,
    EQUITY_SHORT_STATUS_COVER_INDETERMINATE,
)

EQUITY_HOLDING_STATUS_ACTIVE = 'ACTIVE'
EQUITY_HOLDING_STATUS_AWAITING_CONFIRM = 'AWAITING_CONFIRM'
EQUITY_HOLDING_STATUS_EXIT_PENDING = 'EXIT_PENDING'
EQUITY_HOLDING_STATUS_EXIT_SUBMITTED = 'EXIT_SUBMITTED'
EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE = 'EXIT_INDETERMINATE'
EQUITY_HOLDING_STATUS_EXITED = 'EXITED'

# Why a holding notice was raised. A notice is never an error: it is the
# application saying out loud that a share count moved for a reason it did not
# cause, so the admin is never surprised by their own broker.
EQUITY_NOTICE_SHARES_ARRIVED = 'SHARES_ARRIVED'
EQUITY_NOTICE_SHARES_LEFT = 'SHARES_LEFT'
EQUITY_NOTICE_HOLDING_NEW = 'HOLDING_NEW'
EQUITY_NOTICE_HOLDING_CLOSED = 'HOLDING_CLOSED'
# A stop loss or target was actually hit. Not a share movement like the four
# above, but it travels in the same feed for the same reason: it is a statement
# about a holding that the admin has to see, and it should not need a second
# place to look. Before these existed a breach reached the Holdings screen and
# nowhere else, so a stop loss hit while the admin was on any other screen said
# nothing at all.
EQUITY_NOTICE_STOP_LOSS_HIT = 'STOP_LOSS_HIT'
EQUITY_NOTICE_TARGET_HIT = 'TARGET_HIT'

EQUITY_NOTICE_KINDS = (
    EQUITY_NOTICE_SHARES_ARRIVED,
    EQUITY_NOTICE_SHARES_LEFT,
    EQUITY_NOTICE_HOLDING_NEW,
    EQUITY_NOTICE_HOLDING_CLOSED,
    EQUITY_NOTICE_STOP_LOSS_HIT,
    EQUITY_NOTICE_TARGET_HIT,
)

# The only statuses EquityHolding.claim_for_exit will claim from
EQUITY_HOLDING_STATUSES_CLAIMABLE = (
    EQUITY_HOLDING_STATUS_ACTIVE,
    EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,
)

# A sell is already on its way for these, so nothing may start another one
EQUITY_HOLDING_STATUSES_EXIT_IN_FLIGHT = (
    EQUITY_HOLDING_STATUS_EXIT_PENDING,
    EQUITY_HOLDING_STATUS_EXIT_SUBMITTED,
)

# The stop loss / target monitor only evaluates rows in these statuses
EQUITY_HOLDING_STATUSES_MONITORABLE = (
    EQUITY_HOLDING_STATUS_ACTIVE,
)

# Which side of the alert price a watch list alert fires on
EQUITY_ALERT_DIRECTION_ABOVE = 'ABOVE'
EQUITY_ALERT_DIRECTION_BELOW = 'BELOW'

# What Place Order does with an account whose cash cannot cover its share.
# SKIP is the default: that account is marked SKIPPED and every other account
# still gets its order. ABORT places nothing at all for anybody.
EQUITY_FUNDS_ACTION_SKIP = 'SKIP'
EQUITY_FUNDS_ACTION_ABORT = 'ABORT'


class EquityAccountAllocation(db.Model):
    """
    Equity fund allocation for one trading account.

    This is deliberately a separate one-row-per-account table instead of extra
    columns on TradingAccount. TradingAccount is shared with the live F&O
    module, so altering it risks that module. The equity module owns no columns
    on trading_accounts and relates back to it by id.

    The equity module does still refresh the broker payload cache that already
    exists on the shared row (last_funds_data, last_holdings_data and
    last_data_update), the same way the trading, accounts and margin blueprints
    do. See _refresh_account_cache in app/equity/routes.py for the rule that
    stops a holdings-only read from making stale F&O cash look fresh.
    """
    __tablename__ = 'equity_account_allocations'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    # Rupee amount of this account's funds earmarked for equity trading
    equity_fund_allocation = db.Column(db.Float, nullable=False, default=0.0)

    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = db.relationship('TradingAccount', backref=db.backref('equity_allocation', uselist=False))
    user = db.relationship('User', backref='equity_allocations')

    # One allocation row per trading account
    __table_args__ = (
        db.UniqueConstraint('account_id', name='_equity_allocation_account_uc'),
        db.Index('ix_equity_account_allocations_user_active', 'user_id', 'is_active'),
    )

    def __repr__(self):
        return f'<EquityAccountAllocation Account {self.account_id} - Rs {self.equity_fund_allocation}>'


class EquityTradeNature(db.Model):
    """
    Admin configurable tag describing why a trade is being taken,
    for example Swing or Long Term. Shared across all accounts.
    """
    __tablename__ = 'equity_trade_natures'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(50), nullable=False, index=True)
    display_order = db.Column(db.Integer, default=0, index=True)
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = db.relationship('User', backref='equity_trade_natures')

    # Unique constraint for user and nature name
    __table_args__ = (
        db.UniqueConstraint('user_id', 'name', name='_user_trade_nature_uc'),
    )

    def __repr__(self):
        return f'<EquityTradeNature {self.name}>'

    @staticmethod
    def get_or_create_defaults(user_id):
        """Create the default trade natures for a user if they do not exist"""
        defaults = [
            {'name': 'Swing', 'display_order': 1},
            {'name': 'Short Term', 'display_order': 2},
            {'name': 'Long Term', 'display_order': 3},
            {'name': 'Momentum', 'display_order': 4},
        ]

        for default in defaults:
            nature = EquityTradeNature.query.filter_by(
                user_id=user_id,
                name=default['name']
            ).first()

            if not nature:
                nature = EquityTradeNature(
                    user_id=user_id,
                    name=default['name'],
                    display_order=default['display_order']
                )
                db.session.add(nature)

        db.session.commit()


class EquityWatchlist(db.Model):
    """
    A named watch list. A user may keep several.

    The same stock is allowed to sit in more than one list, in the manner of
    Screener, and each entry carries its own target price and its own alert.
    That is why the uniqueness rule on EquityWatchlistItem is scoped to the
    list and not to the user.

    Exactly one list per user is the default. It is the list the screen opens
    on, and the one a stock lands in when no list is named. Deleting the
    default is refused, so a user can never be left with nowhere to put a
    stock. The flag moves only when another list is made default.
    """
    __tablename__ = 'equity_watchlists'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)

    is_default = db.Column(db.Boolean, default=False, nullable=False, index=True)

    # Display order of the lists in the selector. Ties fall back to name.
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', backref='equity_watchlists')

    __table_args__ = (
        db.UniqueConstraint('user_id', 'name', name='_user_watchlist_name_uc'),
    )

    def __repr__(self):
        return f'<EquityWatchlist {self.name}>'


class EquityWatchlistItem(db.Model):
    """
    Watch list entry. Watch lists are shared at the admin level and are not
    scoped to a single trading account.

    Every entry belongs to exactly one EquityWatchlist. Uniqueness is scoped to
    the list, so the same stock may appear in several lists, each with its own
    target price and its own alert.

    Alert de-duplication. The watch list refreshes its LTP every few seconds,
    so a price that has crossed alert_price would otherwise raise an alert on
    every single refresh. alert_triggered_at is the guard: an alert is raised
    only while it is NULL, and setting it is what marks the alert as delivered.
    Re-arming is explicit. Any write that changes alert_price, alert_direction
    or price_alert_enabled must also clear alert_triggered_at and
    alert_triggered_price, otherwise the alert stays silent forever.
    """
    __tablename__ = 'equity_watchlist_items'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    watchlist_id = db.Column(
        db.Integer, db.ForeignKey('equity_watchlists.id'), nullable=False, index=True
    )
    symbol = db.Column(db.String(50), nullable=False, index=True)
    exchange = db.Column(db.String(20), nullable=False, default='NSE', index=True)
    trade_nature_id = db.Column(db.Integer, db.ForeignKey('equity_trade_natures.id'), nullable=True, index=True)

    target_price = db.Column(db.Float)
    alert_price = db.Column(db.Float)
    price_alert_enabled = db.Column(db.Boolean, default=False, index=True)

    # Which way the price has to cross alert_price for the alert to fire.
    # An alert price on its own is ambiguous, a stock at 100 with an alert at
    # 110 wants ABOVE while the same stock with an alert at 90 wants BELOW.
    # Resolve it when the alert is saved instead of guessing at alert time.
    # 'ABOVE', 'BELOW' or NULL when no alert price is set.
    alert_direction = db.Column(db.String(10))

    # Set once when the alert fires, cleared to re-arm. See the class docstring.
    alert_triggered_at = db.Column(db.DateTime)
    alert_triggered_price = db.Column(db.Float)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='equity_watchlist_items')
    trade_nature = db.relationship('EquityTradeNature', backref='watchlist_items')
    watchlist = db.relationship(
        'EquityWatchlist',
        backref=db.backref('items', cascade='all, delete-orphan', lazy='dynamic')
    )

    # Scoped to the list, not the user: the same stock may sit in several lists.
    __table_args__ = (
        db.UniqueConstraint('watchlist_id', 'symbol', 'exchange', name='_watchlist_symbol_uc'),
    )

    def __repr__(self):
        return f'<EquityWatchlistItem {self.symbol} - {self.exchange}>'


class EquityAlertEvent(db.Model):
    """
    One price alert that actually fired.

    The alert itself lives on the watch list row. This is the record of that
    alert going off, and it exists because the alert no longer depends on
    anyone watching. The background monitor evaluates alerts on its own
    schedule whether or not a screen is open, so a fired alert needs somewhere
    to wait until a browser is there to show it - and a later delivery channel,
    WhatsApp among them, reads these same rows rather than needing its own.

    notified_at is the browser guard, and works the way alert_triggered_at
    works on the watch list row: an event is offered to a screen only while it
    is NULL, and stamping it is what marks it shown. Without it every poll
    would raise the same alert again.

    The row belongs to the watch list entry and goes with it. Removing a stock
    from a watch list removes its alert and the record of that alert firing,
    which is what "removing the row removes its alert" has to mean if it is to
    be true.
    """
    __tablename__ = 'equity_alert_events'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    watchlist_item_id = db.Column(
        db.Integer,
        db.ForeignKey('equity_watchlist_items.id', ondelete='CASCADE'),
        nullable=False,
        index=True
    )

    # Copied onto the event rather than read back through the relationship. An
    # alert is a statement about a moment, so it has to keep reading correctly
    # even if the watch list row is edited a second after it fired.
    symbol = db.Column(db.String(50), nullable=False)
    exchange = db.Column(db.String(20), nullable=False, default='NSE')
    alert_price = db.Column(db.Float)
    alert_direction = db.Column(db.String(10))
    ltp = db.Column(db.Float)
    message = db.Column(db.String(255), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    notified_at = db.Column(db.DateTime, index=True)

    user = db.relationship('User', backref='equity_alert_events')
    watchlist_item = db.relationship(
        'EquityWatchlistItem',
        backref=db.backref('alert_events', cascade='all, delete-orphan', lazy='dynamic')
    )

    def __repr__(self):
        return f'<EquityAlertEvent {self.symbol} at {self.alert_price}>'


class EquityOrder(db.Model):
    """
    Parent multi-account equity order. One row per admin action, split into
    one EquityOrderSplit per participating trading account.

    The parent carries the instruction. It never carries a broker order id,
    because there is one broker order per account and those live on the splits.
    Parent status is a roll-up of the splits: PARTIAL means some accounts got
    their order and some did not, which is a normal outcome and not an error.
    """
    __tablename__ = 'equity_orders'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    symbol = db.Column(db.String(50), nullable=False, index=True)
    exchange = db.Column(db.String(20), nullable=False, default='NSE', index=True)
    side = db.Column(db.String(10), nullable=False)  # 'BUY', 'SELL'
    order_type = db.Column(db.String(20), nullable=False, default=EQUITY_ORDER_TYPE_MARKET)  # 'MARKET', 'LIMIT', 'GTT'
    product = db.Column(db.String(10), nullable=False, default=EQUITY_PRODUCT_CNC)  # Always CNC (delivery)

    total_quantity = db.Column(db.Integer, nullable=False, default=0)
    price = db.Column(db.Float)  # Limit price, used by LIMIT and GTT, NULL for MARKET
    # GTT trigger price. A GTT carries both: trigger_price is the level that
    # activates it and price is the limit the resulting order is sent at.
    # NULL for MARKET and LIMIT.
    trigger_price = db.Column(db.Float)
    stop_loss = db.Column(db.Float)
    target = db.Column(db.Float)

    # Trade nature carried from the Watch List row or picked on Place Order,
    # so a fill can stamp the same nature onto the resulting holding.
    trade_nature_id = db.Column(db.Integer, db.ForeignKey('equity_trade_natures.id'), nullable=True, index=True)

    # What raised this order: EQUITY_ORDER_SOURCE_MANUAL for an admin action,
    # STOP_LOSS or TARGET for a monitor exit. The order book uses it to explain
    # a sell nobody remembers placing.
    source = db.Column(db.String(20), nullable=False, default=EQUITY_ORDER_SOURCE_MANUAL, index=True)

    # Shares that no account could take because split_quantity_by_ratio rounds
    # every account DOWN to a whole share. Point in time, recorded once, never
    # recalculated. Place Order shows it before submit and the order book shows
    # it afterwards, so the numbers still add up when the order is reopened.
    leftover_quantity = db.Column(db.Integer, nullable=False, default=0)

    # The insufficient funds policy that was in force when this order ran,
    # EQUITY_FUNDS_ACTION_SKIP or EQUITY_FUNDS_ACTION_ABORT. Snapshotted from
    # EquitySetting so that changing the setting later cannot rewrite the
    # history of what this order actually did.
    insufficient_funds_action = db.Column(db.String(10), nullable=False, default=EQUITY_FUNDS_ACTION_SKIP)

    status = db.Column(db.String(20), nullable=False, default=EQUITY_ORDER_STATUS_PENDING, index=True)
    placed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    cancelled_at = db.Column(db.DateTime)

    # Parent level failure summary, for example the reason an ABORT policy
    # order placed nothing. Per-account errors belong on the split.
    error_message = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='equity_orders')
    trade_nature = db.relationship('EquityTradeNature', backref='orders')
    splits = db.relationship('EquityOrderSplit', backref='equity_order', lazy='dynamic', cascade='all, delete-orphan')

    __table_args__ = (
        db.Index('ix_equity_orders_user_status', 'user_id', 'status'),
        db.Index('ix_equity_orders_user_placed_at', 'user_id', 'placed_at'),
    )

    @property
    def is_open(self):
        """True while the order can still be modified or cancelled"""
        return self.status in (EQUITY_ORDER_STATUS_PENDING, EQUITY_ORDER_STATUS_PARTIAL)

    def __repr__(self):
        return f'<EquityOrder {self.side} {self.symbol} Qty: {self.total_quantity} - {self.status}>'


class EquityOrderSplit(db.Model):
    """
    One account's share of a parent equity order.

    Every figure on this row is a point-in-time snapshot taken when the parent
    order was created, so the order book always shows what was true at order
    time. Never recalculate these values later.
    """
    __tablename__ = 'equity_order_splits'

    id = db.Column(db.Integer, primary_key=True)
    equity_order_id = db.Column(db.Integer, db.ForeignKey('equity_orders.id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)

    # Snapshot values captured at order time
    qty_ratio_at_order = db.Column(db.Float)  # This account's share of the total quantity
    quantity = db.Column(db.Integer, nullable=False, default=0)
    est_value = db.Column(db.Float)  # Estimated rupee value at order time
    cash_balance_at_order = db.Column(db.Float)

    # What the ratio produced before the admin touched it. Place Order lets the
    # Qty cell be overridden per account, and both numbers are worth keeping:
    # ratio_quantity explains the default, quantity is what was actually sent.
    ratio_quantity = db.Column(db.Integer)
    qty_overridden = db.Column(db.Boolean, nullable=False, default=False)

    # Broker response and fill tracking
    broker_order_id = db.Column(db.String(100), index=True)
    # GTT id, kept apart from broker_order_id. When a GTT triggers the broker
    # issues a fresh order id for the real order, and overwriting the GTT id
    # with it would lose the link back to the trigger that caused the trade.
    broker_gtt_id = db.Column(db.String(100), index=True)
    fill_status = db.Column(db.String(20), nullable=False, default=EQUITY_SPLIT_STATUS_PENDING, index=True)
    filled_quantity = db.Column(db.Integer, default=0)
    avg_fill_price = db.Column(db.Float)
    error_message = db.Column(db.Text)

    # Raw error_type from ExtendedOpenAlgoAPI, for example 'timeout_error'.
    # Kept verbatim so a reconciler can tell exactly why a split ended up
    # INDETERMINATE rather than trusting a status that was derived once.
    error_type = db.Column(db.String(50))

    # Raw broker order status string, for example 'open' or 'rejected'
    broker_order_status = db.Column(db.String(50))

    # When this account's request actually reached the broker. The parent
    # placed_at is when the admin clicked, these differ across accounts and
    # that difference is what reconciliation reads.
    placed_at = db.Column(db.DateTime, index=True)
    last_synced_at = db.Column(db.DateTime)

    # How many placement attempts were made for this account. A split that is
    # INDETERMINATE must never show more than one.
    attempt_count = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = db.relationship('TradingAccount', backref='equity_order_splits')
    trades = db.relationship('EquityTrade', backref='split', lazy='dynamic', cascade='all, delete-orphan')

    # One split per account within a parent order
    __table_args__ = (
        db.UniqueConstraint('equity_order_id', 'account_id', name='_equity_order_account_uc'),
        db.Index('ix_equity_order_splits_account_status', 'account_id', 'fill_status'),
    )

    @property
    def is_open(self):
        """True while this account's order is still working at the broker"""
        return self.fill_status in EQUITY_SPLIT_STATUSES_OPEN

    @property
    def is_terminal(self):
        """True when only a broker event could change this split again"""
        return self.fill_status in EQUITY_SPLIT_STATUSES_TERMINAL

    @property
    def is_safe_to_retry(self):
        """
        True only when re-sending this account's order cannot duplicate it.

        An INDETERMINATE split is never safe: the order may already be live at
        the broker even though the request never came back.
        """
        return self.fill_status in EQUITY_SPLIT_STATUSES_SAFE_TO_RETRY

    def __repr__(self):
        return f'<EquityOrderSplit Order {self.equity_order_id} Account {self.account_id} - {self.fill_status}>'


class EquityTrade(db.Model):
    """
    A single fill against an order split. One split can produce several trades
    when the broker fills it in parts.

    Fills are read by polling the broker, and the same fill comes back on every
    poll. The unique index on (split_id, broker_trade_id) is what stops a
    repeated poll from booking the same fill twice and doubling the reported
    quantity. Both SQLite and PostgreSQL allow repeated NULLs in a unique
    index, so a broker that returns no trade id still records its fills, it
    just does not get de-duplicated by the database and has to be matched on
    quantity and price instead.
    """
    __tablename__ = 'equity_trades'

    id = db.Column(db.Integer, primary_key=True)
    split_id = db.Column(db.Integer, db.ForeignKey('equity_order_splits.id'), nullable=False, index=True)

    execution_price = db.Column(db.Float)
    executed_quantity = db.Column(db.Integer)
    exchange = db.Column(db.String(20), index=True)
    executed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    broker_trade_id = db.Column(db.String(100), index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Declared as a unique index rather than a unique constraint on purpose.
    # SQLite cannot add a table constraint to an existing table, but it can
    # create a unique index, so migration 015 and db.create_all() produce the
    # same object on both databases.
    __table_args__ = (
        db.Index('ix_equity_trades_split_broker_uc', 'split_id', 'broker_trade_id', unique=True),
    )

    def __repr__(self):
        return f'<EquityTrade Split {self.split_id} Qty: {self.executed_quantity} @ {self.execution_price}>'


class EquityHolding(db.Model):
    """
    Equity delivery holding for one account and symbol, with the AlgoMirror
    side of the position (trade nature, stop loss, target, exit mode) that the
    broker does not store.

    Exit claim
    ----------
    Two things can decide to sell the same shares: the stop loss / target
    monitor running in the background scheduler, and the admin pressing Sell.
    Without a claim they both read a holding of 40 shares, both place a sell,
    and the account ends up short 40 shares it never owned.

    exit_status is that claim. The rule, and the reason every transition below
    is a classmethod that takes a row lock and commits, is:

        lock the row, re-check it is still claimable and still carries no
        broker order id, set EXIT_PENDING, COMMIT, and only then call the
        broker.

    Nothing in this module may place an equity sell against a holding without
    going through claim_for_exit first. The commit is the claim: an uncommitted
    status change is invisible to the other worker.

    Every method here commits or rolls back before it returns, and none of them
    talks to a broker. Orchestration (retries, threading, the broker call)
    lives in the shared equity exit helper, which calls these.

    Breach records
    --------------
    sl_hit_at / sl_hit_price and tp_hit_at / tp_hit_price record that a level
    was breached. They are also the de-duplication guard: the monitor raises an
    alert only while the matching timestamp is NULL. That is what lets a
    CONFIRM mode holding sit in AWAITING_CONFIRM for an hour without alerting
    again on every scheduler tick, and what stops an admin who declined an
    alert from being asked again ten seconds later. Editing stop_loss or target
    re-arms the level, through clear_breach.
    """
    __tablename__ = 'equity_holdings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)

    symbol = db.Column(db.String(50), nullable=False, index=True)
    exchange = db.Column(db.String(20), nullable=False, default='NSE', index=True)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    avg_cost = db.Column(db.Float)

    trade_nature_id = db.Column(db.Integer, db.ForeignKey('equity_trade_natures.id'), nullable=True, index=True)
    stop_loss = db.Column(db.Float)
    target = db.Column(db.Float)
    exit_mode = db.Column(db.String(10), nullable=False, default=EQUITY_EXIT_MODE_CONFIRM)  # 'AUTO', 'CONFIRM'

    pledged_quantity = db.Column(db.Integer, default=0)
    last_price = db.Column(db.Float)
    last_price_updated = db.Column(db.DateTime)

    # Shares the broker reports that AlgoMirror's own filled orders do not
    # account for: the broker's quantity minus (filled buys - filled sells)
    # placed through this application. It is normally a fixed number - shares
    # bought elsewhere, or held before AlgoMirror existed - and its VALUE is
    # uninteresting. What matters is when it CHANGES, because a change means
    # shares moved at the broker without an order from here. The baseline is
    # absorbed silently the first time a row is seen; every later change raises
    # a notice. Never used to size a sell; only the broker's own count does
    # that.
    #
    # NULL means "not measured yet" and is the whole reason no notice is raised
    # for history: the first measurement writes the figure silently, and only a
    # later change to it is worth telling anybody about.
    external_quantity = db.Column(db.Integer, nullable=True, default=None)

    # False while the shares are bought and filled but not yet delivered into
    # the broker's holdings book. A delivery buy is a POSITION on the day it is
    # bought and only becomes a HOLDING at settlement, which left a window -
    # the whole of the trading day it was bought on - in which a stop loss the
    # admin had set governed nothing at all, because the monitor works on
    # holding rows and there was no holding row yet.
    #
    # An unsettled row is created from AlgoMirror's own fills so that the levels
    # are armed from the moment the buy completes. Two rules follow from it, and
    # both matter:
    #
    #   1. The holdings sync must NOT retire an unsettled row. That loop retires
    #      anything the broker's holdings book no longer reports, and the broker
    #      will not report this stock until tomorrow.
    #   2. A sell against an unsettled row must verify its quantity against the
    #      POSITION book, not the holdings book. The holdings book would answer
    #      zero, which reads as "these shares are gone" and would withhold a
    #      legitimate exit.
    #
    # It flips to True the first time the broker's holdings book reports the
    # stock, after which the row is an ordinary holding and every normal rule
    # applies. Existing rows are settled by definition, which is why the default
    # is True.
    is_settled = db.Column(db.Boolean, nullable=False, default=True)

    # True once this stock has been put back on a watch list after being sold
    # out. Reset to False the moment shares are held again.
    #
    # The owner's rule from 6 September: one stock, one home. A held stock does
    # not appear on a watch list, and when the last share is sold it goes back
    # to one - including a stock that was never on a watch list before, which
    # gets a row on the default list.
    #
    # This flag exists for exactly one reason: so a row the owner then DELETES
    # stays deleted. Without it the return is recomputed on every read and a
    # deleted row would reappear seconds later, which would make the watch list
    # impossible to curate. A stock previously on a watch list needs none of
    # this - its row is only hidden while held and comes back by itself - so
    # this is only ever consulted for the never-watched case.
    returned_to_watchlist = db.Column(db.Boolean, nullable=False, default=False)

    # --- Exit claim state, see the class docstring ---

    # One of EQUITY_HOLDING_STATUS_*. ACTIVE means nothing is in flight.
    exit_status = db.Column(
        db.String(20), nullable=False,
        default=EQUITY_HOLDING_STATUS_ACTIVE, index=True
    )
    # Why the exit was started: EQUITY_EXIT_REASON_*
    exit_reason = db.Column(db.String(20))
    # Shares claimed for this exit, decided at claim time from the sellable
    # quantity so that a later holdings refresh cannot change it mid flight
    exit_quantity = db.Column(db.Integer)
    exit_claimed_at = db.Column(db.DateTime)
    exit_submitted_at = db.Column(db.DateTime)
    exit_completed_at = db.Column(db.DateTime)

    # Broker order id of the sell that is in flight. This is the second half of
    # the claim: EXIT_PENDING with no order id can be reverted, EXIT_PENDING
    # with an order id must never be reverted, because that order is real.
    exit_broker_order_id = db.Column(db.String(100), index=True)
    # The AlgoMirror split carrying that sell, so the holding links into the
    # order book and trade book instead of only knowing a broker id
    exit_split_id = db.Column(db.Integer, db.ForeignKey('equity_order_splits.id'), nullable=True, index=True)
    # Last exit failure or reconciliation note. Survives a reverted claim so
    # the Holdings screen can show why the previous attempt did not go through
    exit_error = db.Column(db.Text)

    # --- Breach records, see the class docstring ---
    sl_hit_at = db.Column(db.DateTime)
    sl_hit_price = db.Column(db.Float)
    tp_hit_at = db.Column(db.DateTime)
    tp_hit_price = db.Column(db.Float)

    # Last time the background monitor evaluated this row. Lets Settings show
    # that the monitor is actually running without a browser tab being open.
    last_monitored_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='equity_holdings')
    account = db.relationship('TradingAccount', backref='equity_holdings')
    trade_nature = db.relationship('EquityTradeNature', backref='holdings')

    # Unique constraint for account, symbol, and exchange
    __table_args__ = (
        db.UniqueConstraint('account_id', 'symbol', 'exchange', name='_equity_account_holding_uc'),
        db.Index('ix_equity_holdings_user_symbol', 'user_id', 'symbol'),
        db.Index('ix_equity_holdings_user_exit_status', 'user_id', 'exit_status'),
    )

    def __repr__(self):
        return f'<EquityHolding {self.symbol} Account {self.account_id} - Qty: {self.quantity}>'

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    @property
    def sellable_quantity(self):
        """
        Shares that can actually be sold today.

        Pledged shares are lying with the clearing member against a margin
        loan and cannot be delivered, so selling them would fail at the broker
        or, worse, go through and leave the pledge short.
        """
        quantity = int(self.quantity or 0)
        pledged = int(self.pledged_quantity or 0)
        return max(quantity - pledged, 0)

    @property
    def is_exit_in_flight(self):
        """True while a sell is claimed or already sitting at the broker"""
        return self.exit_status in EQUITY_HOLDING_STATUSES_EXIT_IN_FLIGHT

    @property
    def has_exit_levels(self):
        """True when the monitor has something to watch on this holding"""
        return self.stop_loss is not None or self.target is not None

    @property
    def is_monitorable(self):
        """True when the monitor should evaluate this row on its next tick"""
        return (
            self.exit_status in EQUITY_HOLDING_STATUSES_MONITORABLE
            and self.has_exit_levels
            and self.sellable_quantity > 0
        )

    # ------------------------------------------------------------------
    # Exit claim transitions
    #
    # These read the row with with_for_update(nowait=False), re-check the state
    # and then either commit the change or roll the session back. Call them
    # with no other uncommitted work pending in the session, because a rejected
    # claim rolls back.
    #
    # WITH ONE IMPORTANT CAVEAT. This application runs on SQLite, and
    # SQLAlchemy's SQLite dialect emits nothing at all for FOR UPDATE - its
    # for_update_clause returns an empty string. So on this database
    # with_for_update() is decoration, not mutual exclusion, and a
    # read-check-write pair here is NOT atomic however it reads.
    #
    # claim_for_exit does not rely on it. It settles the claim with a single
    # conditional UPDATE and treats the row count as the verdict, which is
    # correct on SQLite and on PostgreSQL alike. The others below still carry
    # the read-check-write shape; they are less dangerous, because losing a
    # race on a release or a status stamp costs a redundant write rather than a
    # duplicate sell, but they are on the list to convert. See
    # docs/equity/13-AUDIT-2026-09-01.md.
    # ------------------------------------------------------------------

    @classmethod
    def claim_for_exit(cls, holding_id, user_id, reason, quantity=None, allow_from=None):
        """
        Claim one holding for an exit, before any broker call is made.

        This is the only supported way to start an equity sell against a
        holding. The claim is settled by ONE conditional UPDATE whose WHERE
        clause repeats every precondition - still owned by this user, still in
        a claimable status, still carrying no in-flight broker order id, and
        still holding the exact quantity the claim was sized against. The
        database decides the winner; a row count of 1 means this caller took
        the claim and a row count of 0 means somebody else did.

        It reads the row first, but only to produce a useful refusal message
        and to size the claim. Nothing is decided on that read - a decision
        taken there would be a read-check-write pair, and on SQLite there is no
        row lock to make such a pair atomic (see the note above this block).
        The single UPDATE is what makes two workers unable to both claim a
        holding of 40 and sell it twice.

        Args:
            holding_id: EquityHolding.id to claim
            user_id: owner, always passed so the query stays ownership scoped
            reason: EQUITY_EXIT_REASON_STOP_LOSS, _TARGET or _MANUAL
            quantity: shares to exit, capped at sellable_quantity.
                      None (the default) claims the whole sellable quantity.
            allow_from: statuses the claim may be taken from, defaults to
                        EQUITY_HOLDING_STATUSES_CLAIMABLE. Pass
                        (EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,) to make an
                        admin approval refuse to fire on a holding that was
                        never actually alerted.

        Returns:
            (holding, None) when the claim was taken. The holding is committed
            and attached to the session.
            (None, message) when it was not, with a plain reason such as
            already in flight or nothing sellable. Losing the race is a normal
            outcome, not an error, so this never raises for it.
        """
        allowed = tuple(allow_from) if allow_from else EQUITY_HOLDING_STATUSES_CLAIMABLE

        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return None, 'Holding %s not found for this user' % holding_id

        # Read everything needed for the message before any rollback, because
        # a rollback expires the instance and re-reading it would re-query.
        current_status = holding.exit_status
        in_flight_order = holding.exit_broker_order_id
        sellable = holding.sellable_quantity
        held = int(holding.quantity or 0)
        pledged = int(holding.pledged_quantity or 0)

        if in_flight_order:
            db.session.rollback()
            return None, (
                'An exit order (%s) is already in flight for this holding'
                % in_flight_order
            )

        if current_status not in allowed:
            db.session.rollback()
            return None, (
                'Holding is %s, which cannot be claimed for exit' % current_status
            )

        if sellable <= 0:
            db.session.rollback()
            return None, (
                'Nothing sellable: quantity %d, pledged %d' % (held, pledged)
            )

        claim_qty = sellable if quantity is None else min(int(quantity), sellable)
        if claim_qty <= 0:
            db.session.rollback()
            return None, 'Requested exit quantity must be positive'

        # The claim. Every precondition checked above is repeated here as a
        # WHERE clause, because the checks above were advisory: between that
        # read and this write another worker may have claimed the row, and on
        # SQLite nothing was holding it. The quantity and pledge are included
        # because claim_qty was sized against them - if either moved, this
        # claim is for the wrong number of shares and losing is the right
        # outcome.
        table = cls.__table__

        # pledged_quantity is nullable, and a NULL there reads as zero
        # everywhere else in this class. Only zero may therefore match NULL; a
        # non-zero pledge must match exactly, or a row whose pledge was cleared
        # concurrently would satisfy the claim it should have blocked.
        if pledged == 0:
            pledge_unchanged = or_(table.c.pledged_quantity.is_(None),
                                   table.c.pledged_quantity == 0)
        else:
            pledge_unchanged = table.c.pledged_quantity == pledged

        result = db.session.execute(
            sa_update(table)
            .where(table.c.id == holding_id)
            .where(table.c.user_id == user_id)
            .where(table.c.exit_status.in_(allowed))
            .where(or_(table.c.exit_broker_order_id.is_(None),
                       table.c.exit_broker_order_id == ''))
            .where(table.c.quantity == held)
            .where(pledge_unchanged)
            .values(
                exit_status=EQUITY_HOLDING_STATUS_EXIT_PENDING,
                exit_reason=reason,
                exit_quantity=claim_qty,
                exit_claimed_at=datetime.utcnow(),
                exit_submitted_at=None,
                exit_error=None,
            )
        )
        db.session.commit()

        if result.rowcount != 1:
            # Somebody else claimed it, or the holding moved under us. Both are
            # normal outcomes of a contested exit, not errors.
            db.session.expire_all()
            return None, (
                'Another exit claimed this holding first, or its quantity '
                'changed while the claim was being prepared'
            )

        # The UPDATE went round the ORM, so the in-session copy still holds the
        # pre-claim values. Refresh before handing it back, or the caller reads
        # a holding that is EXIT_PENDING in the database and ACTIVE in memory.
        db.session.refresh(holding)
        return holding, None

    @classmethod
    def release_exit_claim(cls, holding_id, user_id, message=None, to_status=None):
        """
        Give back a claim after the broker definitely refused the order.

        Only ever call this for a definite rejection. If the outcome was
        indeterminate (a timeout or a dropped connection) the order may be live
        at the broker, and releasing the claim would let the monitor sell the
        same shares again, so use mark_exit_indeterminate instead.

        The re-check under the lock is deliberate and matches the claim: the
        claim is released only while the row is still EXIT_PENDING and still
        carries no broker order id. If an order id appeared while the retries
        were running, the order is real and the claim stands.

        Args:
            holding_id: EquityHolding.id
            user_id: owner, keeps the query ownership scoped
            message: failure text to keep on exit_error for the Holdings screen
            to_status: status to return to, defaults to
                       EQUITY_HOLDING_STATUS_ACTIVE. Pass
                       EQUITY_HOLDING_STATUS_AWAITING_CONFIRM to put a declined
                       CONFIRM mode holding back in front of the admin.

        Returns:
            True when the claim was released, False when it was not safe to.
        """
        target_status = to_status or EQUITY_HOLDING_STATUS_ACTIVE

        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        if holding.exit_status != EQUITY_HOLDING_STATUS_EXIT_PENDING:
            db.session.rollback()
            return False

        if holding.exit_broker_order_id:
            # An order exists at the broker. Never reopen this row.
            db.session.rollback()
            return False

        holding.exit_status = target_status
        holding.exit_reason = None
        holding.exit_quantity = None
        holding.exit_claimed_at = None
        holding.exit_submitted_at = None
        if message:
            holding.exit_error = str(message)[:1000]
        db.session.commit()
        return True

    @classmethod
    def mark_exit_submitted(cls, holding_id, user_id, broker_order_id, split_id=None):
        """
        Record that the broker accepted the sell and gave an order id.

        Call this the moment a broker order id is known, before anything else.
        Losing an order id is the worst failure in this module: the claim would
        later look releasable and the same shares could be sold twice. For that
        reason the order id is written whatever the current status is, and only
        the status transition is conditional.

        Args:
            holding_id: EquityHolding.id
            user_id: owner, keeps the query ownership scoped
            broker_order_id: id returned by the broker, stored as text
            split_id: EquityOrderSplit.id carrying this sell, when there is one

        Returns:
            True when the row was updated, False when it no longer exists.
        """
        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        holding.exit_broker_order_id = str(broker_order_id) if broker_order_id is not None else None
        if split_id is not None:
            holding.exit_split_id = split_id
        holding.exit_submitted_at = datetime.utcnow()
        if holding.exit_status != EQUITY_HOLDING_STATUS_EXITED:
            holding.exit_status = EQUITY_HOLDING_STATUS_EXIT_SUBMITTED
        db.session.commit()
        return True

    @classmethod
    def mark_exit_indeterminate(cls, holding_id, user_id, message):
        """
        Record a sell whose outcome is unknown, after a timeout or a dropped
        connection. See equity_is_indeterminate_response.

        EXIT_INDETERMINATE is terminal for the automated paths. The monitor
        skips it, claim_for_exit refuses it, and nothing retries it, because
        the order may be live at the broker. A human clears it through
        resolve_exit_indeterminate once the broker order book has been checked.

        The claim fields are deliberately left in place: they are the record of
        what was sent.

        Returns:
            True when the row was updated, False when it no longer exists.
        """
        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        holding.exit_status = EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE
        holding.exit_error = str(message)[:1000] if message else 'Outcome unknown, verify at the broker'
        db.session.commit()
        return True

    @classmethod
    def mark_exit_completed(cls, holding_id, user_id, remaining_quantity=0):
        """
        Record that the exit filled, and reopen the row for future exits.

        The exit fields are cleared here on purpose. exit_broker_order_id is
        half the claim, so a row that goes back to ACTIVE still carrying an old
        order id could never be exited again. The audit trail is not lost, it
        lives on the EquityOrder, EquityOrderSplit and EquityTrade rows, which
        is where it belongs.

        The breach records (sl_hit_at, tp_hit_at) are NOT cleared. After a
        partial exit the price is usually still through the level, and clearing
        them would fire a second exit on the very next monitor tick. The admin
        re-arms the level explicitly through clear_breach.

        Args:
            holding_id: EquityHolding.id
            user_id: owner, keeps the query ownership scoped
            remaining_quantity: shares still held after the fill. 0 (the
                default) means the holding is flat and becomes EXITED, anything
                positive returns it to ACTIVE with the smaller quantity.

        Returns:
            True when the row was updated, False when it no longer exists.
        """
        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        remaining = max(int(remaining_quantity or 0), 0)
        holding.quantity = remaining
        holding.exit_status = (
            EQUITY_HOLDING_STATUS_EXITED if remaining <= 0
            else EQUITY_HOLDING_STATUS_ACTIVE
        )
        holding.exit_completed_at = datetime.utcnow()
        holding.exit_broker_order_id = None
        holding.exit_split_id = None
        holding.exit_reason = None
        holding.exit_quantity = None
        holding.exit_claimed_at = None
        holding.exit_submitted_at = None
        holding.exit_error = None
        db.session.commit()
        return True

    @classmethod
    def release_rejected_exit(cls, holding_id, user_id, split_id, message=None):
        """
        Return a holding to ACTIVE after the broker refused its sell outright.

        The other release, release_exit_claim, only ever acts on EXIT_PENDING
        with no broker order id - a sell that never reached anyone. This is the
        other end: an order the broker took, gave an id for, and then rejected
        or cancelled with nothing filled. Without this the row stays
        EXIT_SUBMITTED for ever, out of the monitor's reach, on a sell that will
        never happen.

        Only for a sell where NOTHING filled. A rejection or cancellation after
        a partial fill is a settlement, not a release, and belongs to
        mark_exit_completed - releasing it here would restore the full quantity
        and lose the shares that really were sold.

        Two re-checks under the lock, because the caller decided from a broker
        read taken moments earlier:

          * the row must still be EXIT_SUBMITTED, and
          * it must still point at the same split the caller examined.

        Between those, a claim that was replaced by a newer exit while the
        caller was working cannot be reopened underneath it.

        The breach markers are deliberately left alone. The level was crossed
        and it stays crossed, so the monitor will not immediately place the sell
        again on the next tick. Why it failed is on exit_error, and re-arming is
        the admin's decision - which is the same rule D2 applies to any
        placement whose outcome was not a clean success.

        Returns:
            True when the claim was released, False when it was not safe to.
        """
        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        if holding.exit_status != EQUITY_HOLDING_STATUS_EXIT_SUBMITTED:
            db.session.rollback()
            return False

        if split_id is None or holding.exit_split_id != split_id:
            db.session.rollback()
            return False

        holding.exit_status = EQUITY_HOLDING_STATUS_ACTIVE
        holding.exit_broker_order_id = None
        holding.exit_split_id = None
        holding.exit_reason = None
        holding.exit_quantity = None
        holding.exit_claimed_at = None
        holding.exit_submitted_at = None
        holding.exit_error = str(message)[:1000] if message else None
        db.session.commit()
        return True

    @classmethod
    def resolve_exit_indeterminate(cls, holding_id, user_id, note=None):
        """
        Clear an EXIT_INDETERMINATE holding after a human checked the broker
        and confirmed no order is live, returning it to ACTIVE so the monitor
        can watch it again.

        Refuses to act on any other status, so it cannot be used to reopen a
        holding whose sell really is sitting at the broker.

        Returns:
            True when the row was reopened, False otherwise.
        """
        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        if holding.exit_status != EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE:
            db.session.rollback()
            return False

        holding.exit_status = EQUITY_HOLDING_STATUS_ACTIVE
        holding.exit_broker_order_id = None
        holding.exit_split_id = None
        holding.exit_reason = None
        holding.exit_quantity = None
        holding.exit_claimed_at = None
        holding.exit_submitted_at = None
        holding.exit_error = str(note)[:1000] if note else 'Reconciled manually, no live broker order'
        db.session.commit()
        return True

    # ------------------------------------------------------------------
    # Breach records
    # ------------------------------------------------------------------

    @classmethod
    def record_breach(cls, holding_id, user_id, kind, price):
        """
        Record that the stop loss or the target was breached, exactly once.

        The return value is the de-duplication: the first call for a level
        returns True, every later call returns False while the timestamp is
        still set. The monitor uses that to alert or act once per armed level
        instead of once per tick.

        For a CONFIRM mode holding this also moves ACTIVE to AWAITING_CONFIRM,
        which is the whole confirm queue: the Holdings screen lists holdings in
        that status and offers Approve or Decline. It does not place anything.
        For an AUTO mode holding the status is untouched and the caller goes
        straight on to claim_for_exit.

        Args:
            holding_id: EquityHolding.id
            user_id: owner, keeps the query ownership scoped
            kind: EQUITY_EXIT_REASON_STOP_LOSS or EQUITY_EXIT_REASON_TARGET
            price: the price that breached the level

        Returns:
            True when this call recorded a new breach, False when the level had
            already been recorded or the holding is gone.

        Raises:
            ValueError for an unknown kind, which is a programming error.
        """
        if kind not in (EQUITY_EXIT_REASON_STOP_LOSS, EQUITY_EXIT_REASON_TARGET):
            raise ValueError('record_breach kind must be a stop loss or target reason, got %r' % (kind,))

        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        is_stop_loss = kind == EQUITY_EXIT_REASON_STOP_LOSS
        already_recorded = (
            holding.sl_hit_at is not None if is_stop_loss
            else holding.tp_hit_at is not None
        )
        if already_recorded:
            db.session.rollback()
            return False

        now = datetime.utcnow()
        if is_stop_loss:
            holding.sl_hit_at = now
            holding.sl_hit_price = price
        else:
            holding.tp_hit_at = now
            holding.tp_hit_price = price

        holding.exit_reason = kind
        if (holding.exit_mode == EQUITY_EXIT_MODE_CONFIRM
                and holding.exit_status == EQUITY_HOLDING_STATUS_ACTIVE):
            holding.exit_status = EQUITY_HOLDING_STATUS_AWAITING_CONFIRM

        db.session.commit()
        return True

    @classmethod
    def clear_breach(cls, holding_id, user_id, kind=None):
        """
        Re-arm a level so the monitor can raise it again.

        Call this whenever stop_loss or target is edited, otherwise a level
        that has already fired stays silent for good. It also returns an
        AWAITING_CONFIRM holding to ACTIVE, since the alert it was waiting on
        no longer applies.

        Args:
            holding_id: EquityHolding.id
            user_id: owner, keeps the query ownership scoped
            kind: EQUITY_EXIT_REASON_STOP_LOSS or EQUITY_EXIT_REASON_TARGET to
                  re-arm one level, None (the default) to re-arm both

        Returns:
            True when the row was updated, False when it no longer exists.
        """
        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        if kind in (None, EQUITY_EXIT_REASON_STOP_LOSS):
            holding.sl_hit_at = None
            holding.sl_hit_price = None
        if kind in (None, EQUITY_EXIT_REASON_TARGET):
            holding.tp_hit_at = None
            holding.tp_hit_price = None

        if holding.exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM:
            holding.exit_status = EQUITY_HOLDING_STATUS_ACTIVE
            holding.exit_reason = None

        db.session.commit()
        return True

    @classmethod
    def dismiss_exit_confirm(cls, holding_id, user_id):
        """
        The admin looked at a CONFIRM mode alert and declined to sell.

        The holding goes back to ACTIVE but the breach records stay set, so the
        monitor does not put the same alert back a few seconds later while the
        price is still through the level. The admin re-arms it deliberately by
        editing the level, which goes through clear_breach.

        Returns:
            True when the alert was dismissed, False when the holding was not
            awaiting confirmation.
        """
        holding = cls.query.filter_by(
            id=holding_id, user_id=user_id
        ).with_for_update(nowait=False).first()

        if holding is None:
            db.session.rollback()
            return False

        if holding.exit_status != EQUITY_HOLDING_STATUS_AWAITING_CONFIRM:
            db.session.rollback()
            return False

        holding.exit_status = EQUITY_HOLDING_STATUS_ACTIVE
        holding.exit_reason = None
        db.session.commit()
        return True


class EquityIntradayShort(db.Model):
    """
    Shares sold that the account did not own, and must buy back today.

    Why this is its own table
    -------------------------
    Every other position in this module is something you HAVE. This is the one
    that is something you OWE, and the difference is not academic: a holding
    left alone does nothing, while a short left alone past the close is
    unlimited loss above your entry, a forced buy-back by the broker at
    whatever price is there, and a penalty. It is the only row in this
    application that MUST be acted on.

    It could have been inferred from order splits with product MIS. It is not,
    for two reasons. Inference is a query that can quietly return the wrong
    answer after a partial fill; and a thing that must be closed needs a claim,
    so that the square-off monitor and a person pressing Cover cannot both buy
    the same shares back.

    The claim
    ---------
    Identical in shape and intent to EquityHolding's exit claim, and for the
    same reason: two things can decide to close this at once. One conditional
    UPDATE, every precondition repeated in the WHERE clause, the database
    deciding the winner. See claim_for_cover.

    What squares it off
    -------------------
    app/utils/equity_intraday_monitor.py, on the shared scheduler, at the
    minute named in EquitySetting.intraday_squareoff_minute. It verifies the
    quantity against the broker's POSITION book first - a short lives there,
    never in holdings - then claims, then places a MARKET buy. Market, because
    at that point being filled matters more than the price.
    """
    __tablename__ = 'equity_intraday_shorts'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)

    symbol = db.Column(db.String(50), nullable=False, index=True)
    exchange = db.Column(db.String(20), nullable=False, default='NSE')

    # Shares owed. Reduced only by a confirmed buy-back.
    quantity = db.Column(db.Integer, nullable=False, default=0)
    entry_price = db.Column(db.Float)

    # The sell that opened it, so the two are never guessed at.
    opening_order_id = db.Column(db.Integer, db.ForeignKey('equity_orders.id'), nullable=True)
    opening_split_id = db.Column(db.Integer, db.ForeignKey('equity_order_splits.id'), nullable=True)
    opened_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # The trading day this belongs to, as a plain date. A short is a same-day
    # obligation, so "which day" is the question asked of it most often and it
    # is stored rather than derived from a UTC timestamp that crosses midnight
    # five and a half hours early in IST.
    trade_date = db.Column(db.Date, nullable=False, index=True)

    status = db.Column(
        db.String(24), nullable=False,
        default=EQUITY_SHORT_STATUS_OPEN, index=True
    )
    cover_reason = db.Column(db.String(20))
    cover_quantity = db.Column(db.Integer)
    cover_claimed_at = db.Column(db.DateTime)
    cover_submitted_at = db.Column(db.DateTime)
    cover_completed_at = db.Column(db.DateTime)
    cover_broker_order_id = db.Column(db.String(64))
    cover_order_id = db.Column(db.Integer, db.ForeignKey('equity_orders.id'), nullable=True)
    cover_price = db.Column(db.Float)
    cover_error = db.Column(db.Text)

    # The protective SL-M buy resting at the broker.
    #
    # This is the half of the protection that does NOT depend on this machine
    # being switched on. AlgoMirror's own stop loss lives in a monitor here and
    # dies with a power cut; this one sits in the exchange's stop-loss book and
    # fires regardless.
    #
    # Recorded because it must be CANCELLED before any other buy-back is
    # placed. Two things closing the same short means buying it back twice,
    # which leaves a long position nobody asked for - so the order id is not
    # optional bookkeeping, it is what makes the cancel possible.
    stop_trigger_price = db.Column(db.Float)
    stop_order_id = db.Column(db.Integer, db.ForeignKey('equity_orders.id'), nullable=True)
    stop_broker_order_id = db.Column(db.String(64))
    # RESTING, CANCELLED, TRIGGERED, or NONE when there is no protective order
    # at the broker - which is a state worth naming rather than inferring from
    # a null id.
    stop_status = db.Column(db.String(16), nullable=False, default='NONE')
    stop_error = db.Column(db.Text)

    # Raised to true once the admin has been told this could not be closed.
    # An unclosed short is the one thing in this module worth interrupting
    # somebody for, and it must not be announced on a loop.
    alerted_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account = db.relationship('TradingAccount', foreign_keys=[account_id])

    __table_args__ = (
        db.Index('ix_equity_short_open', 'user_id', 'status', 'trade_date'),
    )

    @property
    def is_open(self):
        """True while shares are still owed and nothing is in flight."""
        return self.status == EQUITY_SHORT_STATUS_OPEN and int(self.quantity or 0) > 0

    @property
    def is_cover_in_flight(self):
        """True while a buy-back is claimed or sitting at the broker."""
        return self.status in EQUITY_SHORT_STATUSES_IN_FLIGHT

    @classmethod
    def claim_for_cover(cls, short_id, user_id, reason, quantity=None):
        """
        Claim one short for a buy-back, before any broker call is made.

        The only supported way to start a cover. One conditional UPDATE, every
        precondition repeated in the WHERE clause - still this user's, still
        OPEN, still carrying no broker order id, still the exact quantity the
        claim was sized against. A row count of 1 means this caller won; 0
        means something else did, which is a skip and not an error.

        Written this way rather than as read-check-write because SQLAlchemy's
        SQLite dialect emits nothing for FOR UPDATE - with_for_update() is
        decoration on this database, and two racers would both believe they
        had the claim. That mistake is what EquityHolding.claim_for_exit was
        rewritten to remove; this is built with the answer already known.

        Returns (short, None) on success, or (None, message).
        """
        short = cls.query.filter_by(id=short_id, user_id=user_id).first()
        if short is None:
            return None, 'That intraday short is not available for this user.'

        owed = int(short.quantity or 0)
        if owed <= 0:
            return None, 'That short owes nothing.'
        if short.status != EQUITY_SHORT_STATUS_OPEN:
            return None, (
                'A buy-back is already under way on this short (%s).' % short.status
            )

        wanted = int(quantity) if quantity else owed
        wanted = max(min(wanted, owed), 0)
        if wanted <= 0:
            return None, 'Nothing to buy back.'

        table = cls.__table__
        result = db.session.execute(
            sa_update(table)
            .where(table.c.id == short_id)
            .where(table.c.user_id == user_id)
            .where(table.c.status == EQUITY_SHORT_STATUS_OPEN)
            .where(or_(table.c.cover_broker_order_id.is_(None),
                       table.c.cover_broker_order_id == ''))
            .where(table.c.quantity == owed)
            .values(
                status=EQUITY_SHORT_STATUS_COVER_PENDING,
                cover_reason=reason,
                cover_quantity=wanted,
                cover_claimed_at=datetime.utcnow(),
                cover_error=None,
                updated_at=datetime.utcnow(),
            )
        )
        db.session.commit()

        if result.rowcount != 1:
            db.session.expire_all()
            return None, (
                'Another buy-back claimed this short first. Nothing was placed.'
            )

        db.session.refresh(short)
        return short, None

    @classmethod
    def release_claim(cls, short_id, user_id, error=None):
        """
        Put a claimed short back to OPEN after a DEFINITE refusal.

        Only ever from COVER_PENDING with no broker order id - that pair is the
        only state in which we know for certain nothing reached the broker. An
        indeterminate outcome must NEVER come through here: the buy may be live,
        and releasing it would let a second one be placed on top.
        """
        table = cls.__table__
        result = db.session.execute(
            sa_update(table)
            .where(table.c.id == short_id)
            .where(table.c.user_id == user_id)
            .where(table.c.status == EQUITY_SHORT_STATUS_COVER_PENDING)
            .where(or_(table.c.cover_broker_order_id.is_(None),
                       table.c.cover_broker_order_id == ''))
            .values(
                status=EQUITY_SHORT_STATUS_OPEN,
                cover_reason=None,
                cover_quantity=None,
                cover_claimed_at=None,
                cover_error=error,
                updated_at=datetime.utcnow(),
            )
        )
        db.session.commit()
        return result.rowcount == 1


class EquityStockNote(db.Model):
    """
    What the admin thinks about a stock, in his own words.

    The only thing in this module that does not come from a broker. Every other
    row here is a fact reported by somebody else - a price, a fill, a balance.
    This one is the reasoning behind the trade, which no API returns and which
    is the first thing forgotten.

    Keyed to the STOCK, not to a watch list entry and not to a holding. A
    thesis is about the company: it has to survive the stock being dropped from
    a list, sold out of entirely, and bought back again months later. Attaching
    it to a holding would have thrown it away at the exact moment it became
    interesting - when you sold, and later wondered why you had bought.

    Three fields rather than one free box. The owner named them: the thesis,
    the risk, and what he is watching for. Written as one paragraph in a hurry,
    everything after the first thought stops getting written. Any of them may be
    left empty; all three empty means there is no note.
    """
    __tablename__ = 'equity_stock_notes'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    symbol = db.Column(db.String(50), nullable=False, index=True)
    exchange = db.Column(db.String(20), nullable=False, default='NSE')

    thesis = db.Column(db.Text)
    risk = db.Column(db.Text)
    # What would change the answer: a number, a date, a trigger being waited on.
    # Separate from risk because a risk is what could go wrong and this is what
    # would tell you it is going wrong, which is a different sentence.
    to_watch = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    versions = db.relationship(
        'EquityStockNoteVersion', backref='note', lazy='dynamic',
        cascade='all, delete-orphan'
    )

    __table_args__ = (
        # One current note per stock per person. The history lives in its own
        # table, so this row is always simply "what I think now".
        db.UniqueConstraint('user_id', 'symbol', 'exchange',
                            name='uq_equity_stock_note'),
        db.Index('ix_equity_note_lookup', 'user_id', 'symbol', 'exchange'),
    )

    @property
    def is_empty(self):
        return not any(
            (getattr(self, field) or '').strip()
            for field in ('thesis', 'risk', 'to_watch')
        )


class EquityStockNoteVersion(db.Model):
    """
    A note as it read before it was last changed.

    Kept because a thesis that is quietly rewritten to match what happened is
    worth nothing. The point of writing one down is to be able to read, in
    September, what you actually believed in March - including the parts that
    turned out wrong, which are the parts worth reading.

    Written on every save, before the current row is changed. Nothing here is
    ever edited: a version is a record of what was, and a record you can edit
    is not a record.

    symbol and exchange are repeated here rather than only reached through the
    note. A version has to stay readable if the current note is ever deleted,
    and a history that depends on the thing it outlives is not a history.
    """
    __tablename__ = 'equity_stock_note_versions'

    id = db.Column(db.Integer, primary_key=True)
    note_id = db.Column(
        db.Integer, db.ForeignKey('equity_stock_notes.id'), nullable=True, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    symbol = db.Column(db.String(50), nullable=False, index=True)
    exchange = db.Column(db.String(20), nullable=False, default='NSE')

    thesis = db.Column(db.Text)
    risk = db.Column(db.Text)
    to_watch = db.Column(db.Text)

    # When this version STOPPED being the current one.
    saved_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        db.Index('ix_equity_note_version_lookup',
                 'user_id', 'symbol', 'exchange', 'saved_at'),
    )


class EquityHoldingNotice(db.Model):
    """
    A record that a holding changed for a reason AlgoMirror did not cause.

    An emergency sell from the broker's own app, a purchase made at the
    terminal, shares transferred in - none of these pass through this
    application, and until now all of them were absorbed in silence the next
    time holdings were read. Silence is the wrong answer: a stop loss can be
    left armed on a quantity the admin no longer recognises, and nothing on any
    screen would say so.

    A notice is not an error and never blocks anything. It is delivered through
    the same feed as watch list price alerts - the same pop-up, the same
    navigation badge, the same log - because the admin should not have to learn
    a second place to look.

    Deliberately NOT attached to EquityHolding by foreign key. A notice is a
    statement about a moment and has to survive the holding row being retired
    or removed, exactly as a fired price alert keeps reading correctly after
    its watch list row is edited. The account, symbol and exchange are copied
    on for the same reason.
    """
    __tablename__ = 'equity_holding_notices'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)

    symbol = db.Column(db.String(50), nullable=False, index=True)
    exchange = db.Column(db.String(20), nullable=False, default='NSE')

    # One of EQUITY_NOTICE_*
    kind = db.Column(db.String(20), nullable=False, index=True)

    # The share counts that produced this notice, kept so the message can be
    # re-read months later without recomputing anything.
    quantity_before = db.Column(db.Integer)
    quantity_after = db.Column(db.Integer)
    quantity_delta = db.Column(db.Integer)

    # True when a stop loss or target was armed at the moment this happened,
    # which is what turns an interesting notice into an urgent one.
    had_armed_level = db.Column(db.Boolean, nullable=False, default=False)

    message = db.Column(db.String(255), nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    # Stamped when a browser has been shown this notice. An unstamped notice is
    # offered to the next screen that asks, which is what makes a notice raised
    # overnight still arrive in the morning.
    notified_at = db.Column(db.DateTime, index=True)
    # Stamped when the admin has marked the log read.
    seen_at = db.Column(db.DateTime, index=True)

    user = db.relationship('User', backref='equity_holding_notices')
    account = db.relationship('TradingAccount')

    def __repr__(self):
        return f'<EquityHoldingNotice {self.kind} {self.symbol}>'


class EquityBrokerageRate(db.Model):
    """
    Brokerage and statutory charge rates for one account, versioned by
    effective date.

    Cost changes must apply to future trades only, so a rate change is stored
    as a new row with a later effective_from. Historical rows are never edited
    in place, which keeps past cost calculations reproducible. Use
    get_effective_rate() to resolve the row that applies on a given date.
    """
    __tablename__ = 'equity_brokerage_rates'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey('trading_accounts.id'), nullable=False, index=True)
    broker_name = db.Column(db.String(100), nullable=False, index=True)

    # Flat rupee charge per executed order
    brokerage_per_order = db.Column(db.Float, nullable=False, default=0.0)

    # Percentage charges, stored as percent values (0.1 means 0.1 percent)
    stt_pct = db.Column(db.Float, nullable=False, default=0.0)
    exchange_txn_pct = db.Column(db.Float, nullable=False, default=0.0)
    sebi_pct = db.Column(db.Float, nullable=False, default=0.0)
    stamp_duty_pct = db.Column(db.Float, nullable=False, default=0.0)
    gst_pct = db.Column(db.Float, nullable=False, default=0.0)

    # Flat rupee charge applied per delivery sell (DP) or annually (AMC)
    dp_amc_charge = db.Column(db.Float, nullable=False, default=0.0)

    effective_from = db.Column(db.Date, nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='equity_brokerage_rates')
    account = db.relationship('TradingAccount', backref='equity_brokerage_rates')

    # One rate version per account per effective date
    __table_args__ = (
        db.UniqueConstraint('account_id', 'effective_from', name='_equity_account_rate_effective_uc'),
        db.Index('ix_equity_brokerage_rates_account_effective', 'account_id', 'effective_from'),
    )

    def __repr__(self):
        return f'<EquityBrokerageRate Account {self.account_id} from {self.effective_from}>'

    @staticmethod
    def get_effective_rate(user_id, account_id, on_date=None):
        """
        Resolve the rate row that applies to an account on a given date.

        Picks the active row with the latest effective_from that is not after
        on_date. Returns None when no row applies, for example when rates have
        not been configured yet or every row starts in the future. Callers
        decide how to handle a missing rate, this never raises.

        Args:
            user_id: Owner of the account, always scoped for ownership checks
            account_id: Trading account the rate belongs to
            on_date: datetime.date to resolve for, defaults to today

        Returns:
            EquityBrokerageRate or None
        """
        from datetime import date

        if on_date is None:
            on_date = date.today()
        elif isinstance(on_date, datetime):
            on_date = on_date.date()

        return EquityBrokerageRate.query.filter(
            EquityBrokerageRate.user_id == user_id,
            EquityBrokerageRate.account_id == account_id,
            EquityBrokerageRate.is_active == True,
            EquityBrokerageRate.effective_from <= on_date
        ).order_by(
            EquityBrokerageRate.effective_from.desc(),
            EquityBrokerageRate.id.desc()
        ).first()


class EquitySetting(db.Model):
    """
    Module wide equity preferences, one row per user.

    Kept apart from EquityBrokerageRate, which is per account and versioned by
    effective date, and apart from TradingSettings, which belongs to the live
    F&O module and must not be disturbed. These are the switches that change
    how the equity module behaves rather than what a trade costs.

    Use get_or_create(user_id) rather than querying directly, so a screen or a
    background job that runs before the row exists still gets defaults.
    """
    __tablename__ = 'equity_settings'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    # What Place Order does with an account whose cash cannot cover its share.
    # SKIP (the default) marks that account SKIPPED and lets every other
    # account through, ABORT places nothing at all. Snapshotted onto
    # EquityOrder.insufficient_funds_action at order time.
    insufficient_funds_action = db.Column(
        db.String(10), nullable=False, default=EQUITY_FUNDS_ACTION_SKIP
    )

    # Exit mode given to a newly created holding. CONFIRM by default, so a new
    # holding never sells itself before the admin has chosen that behaviour.
    default_exit_mode = db.Column(
        db.String(10), nullable=False, default=EQUITY_EXIT_MODE_CONFIRM
    )

    # Stop loss / target monitor. It runs in the background scheduler, not in a
    # browser tab, so these are the only controls over it.
    sl_monitor_enabled = db.Column(db.Boolean, nullable=False, default=True)
    sl_monitor_interval_seconds = db.Column(db.Integer, nullable=False, default=30)

    # Master switch for watch list price alerts
    price_alerts_enabled = db.Column(db.Boolean, nullable=False, default=True)

    # How long to wait for a broker to answer an order write, in seconds.
    #
    # Configurable because the right answer depends entirely on what is on the
    # other end. A live broker answers in a second or two, so a short wait is
    # correct there and a long one would hide a real problem. A sandbox can take
    # a minute, and every second shaved off turns a slow success into an
    # INDETERMINATE order that a person then has to reconcile by hand - which is
    # exactly what happened on 2026-08-30, when a 63 second placement was
    # reported as failed while both orders had in fact gone through.
    order_timeout_seconds = db.Column(db.Integer, nullable=False, default=30)

    # Intraday shorts. Minutes past midnight IST, so the value sorts and
    # compares without any date arithmetic.
    #
    # 15:12 by default. It has to stay IN FRONT of whoever else would close
    # the position: OpenAlgo's sandbox squares MIS off at 15:15 on NSE and BSE,
    # and a real broker has its own cut-off that varies. Being second means the
    # broker does it, at market, at whatever price is there - and it means our
    # own square-off never gets to prove it works. A setting, because those
    # cut-offs move.
    intraday_squareoff_minute = db.Column(db.Integer, nullable=False, default=15 * 60 + 12)

    # After this, a sell that would open a NEW short is refused outright. A
    # short opened at 15:18 has two minutes to work and must then be bought
    # back whatever the price, which is not a trade.
    intraday_cutoff_minute = db.Column(db.Integer, nullable=False, default=15 * 60)

    # Master switch for the square-off monitor. Turning it off does NOT make
    # shorts safe - it makes them impossible: the placement path refuses to
    # open one when the thing that closes it is not running.
    intraday_monitor_enabled = db.Column(db.Boolean, nullable=False, default=True)

    # Monitor heartbeat. A monitor that is meant to run without a browser open
    # has to be able to prove it is running, and to show why it stopped.
    monitor_last_run_at = db.Column(db.DateTime)
    monitor_last_error = db.Column(db.Text)

    # The square-off monitor's own heartbeat, kept apart from the stop loss
    # monitor's. One of them being alive says nothing about the other, and the
    # screen has to be able to prove the square-off in particular is running.
    intraday_last_run_at = db.Column(db.DateTime)
    intraday_last_error = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = db.relationship('User', backref=db.backref('equity_settings', uselist=False))

    # One settings row per user
    __table_args__ = (
        db.UniqueConstraint('user_id', name='_equity_settings_user_uc'),
    )

    def __repr__(self):
        return f'<EquitySetting User {self.user_id} - Funds: {self.insufficient_funds_action}>'

    @staticmethod
    def get_or_create(user_id):
        """
        Return this user's equity settings, creating the row with defaults the
        first time it is asked for.

        Safe to call from a request and from a scheduler job at the same time:
        if both try to create the row, the unique constraint rejects one and
        that caller re-reads the row the other one committed.

        Args:
            user_id: owner of the settings row

        Returns:
            EquitySetting, always committed and attached to the session
        """
        from sqlalchemy.exc import IntegrityError

        setting = EquitySetting.query.filter_by(user_id=user_id).first()
        if setting:
            return setting

        setting = EquitySetting(user_id=user_id)
        db.session.add(setting)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            setting = EquitySetting.query.filter_by(user_id=user_id).first()

        return setting
