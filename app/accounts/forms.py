from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, SubmitField, BooleanField
from wtforms.validators import DataRequired, URL, Length, Optional, ValidationError
from app.models import TradingAccount
from flask_login import current_user

class AddAccountForm(FlaskForm):
    account_name = StringField('Account Name', validators=[
        DataRequired(),
        Length(min=3, max=100, message='Account name must be between 3 and 100 characters.')
    ])
    broker_name = SelectField('Broker', choices=[
        ('5paisa', '5paisa'),
        ('5paisa (XTS)', '5paisa (XTS)'),
        ('Aliceblue', 'Aliceblue'),
        ('AngelOne', 'AngelOne'),
        ('Compositedge (XTS)', 'Compositedge (XTS)'),
        ('Definedge', 'Definedge'),
        ('Dhan', 'Dhan'),
        ('Firstock', 'Firstock'),
        ('Flattrade', 'Flattrade'),
        ('Fyers', 'Fyers'),
        ('Groww', 'Groww'),
        ('IIFL (XTS)', 'IIFL (XTS)'),
        ('IndiaBulls', 'IndiaBulls'),
        ('IndMoney', 'IndMoney'),
        ('Kotak Securities', 'Kotak Securities'),
        ('Motilal Oswal', 'Motilal Oswal'),
        ('Paytm', 'Paytm'),
        ('Pocketful', 'Pocketful'),
        ('Shoonya', 'Shoonya'),
        ('Tradejini', 'Tradejini'),
        ('Upstox', 'Upstox'),
        ('Wisdom Capital (XTS)', 'Wisdom Capital (XTS)'),
        ('Zebu', 'Zebu'),
        ('Zerodha', 'Zerodha')
    ], validators=[DataRequired()])

    host_url = StringField('OpenAlgo Host URL', validators=[
        DataRequired(),
        URL(message='Please enter a valid URL.')
    ], default='http://127.0.0.1:5000')
    
    websocket_url = StringField('WebSocket URL', validators=[
        DataRequired(),
        Length(max=500, message='WebSocket URL is too long.')
    ], default='ws://127.0.0.1:8765')
    
    api_key = StringField('OpenAlgo API Key', validators=[
        DataRequired(),
        Length(min=10, message='API Key seems too short.')
    ])
    
    is_primary = BooleanField('Set as Primary Account')
    
    submit = SubmitField('Add Account')
    
    def validate_account_name(self, account_name):
        account = TradingAccount.query.filter_by(
            user_id=current_user.id,
            account_name=account_name.data
        ).first()
        if account:
            raise ValidationError('You already have an account with this name.')

class EditAccountForm(FlaskForm):
    account_name = StringField('Account Name', validators=[
        DataRequired(),
        Length(min=3, max=100, message='Account name must be between 3 and 100 characters.')
    ])
    broker_name = SelectField('Broker', choices=[
        ('5paisa', '5paisa'),
        ('5paisa (XTS)', '5paisa (XTS)'),
        ('Aliceblue', 'Aliceblue'),
        ('AngelOne', 'AngelOne'),
        ('Compositedge (XTS)', 'Compositedge (XTS)'),
        ('Definedge', 'Definedge'),
        ('Dhan', 'Dhan'),
        ('Firstock', 'Firstock'),
        ('Flattrade', 'Flattrade'),
        ('Fyers', 'Fyers'),
        ('Groww', 'Groww'),
        ('IIFL (XTS)', 'IIFL (XTS)'),
        ('IndiaBulls', 'IndiaBulls'),
        ('IndMoney', 'IndMoney'),
        ('Kotak Securities', 'Kotak Securities'),
        ('Motilal Oswal', 'Motilal Oswal'),
        ('Paytm', 'Paytm'),
        ('Pocketful', 'Pocketful'),
        ('Shoonya', 'Shoonya'),
        ('Tradejini', 'Tradejini'),
        ('Upstox', 'Upstox'),
        ('Wisdom Capital (XTS)', 'Wisdom Capital (XTS)'),
        ('Zebu', 'Zebu'),
        ('Zerodha', 'Zerodha')
    ], validators=[DataRequired()])

    host_url = StringField('OpenAlgo Host URL', validators=[
        DataRequired(),
        URL(message='Please enter a valid URL.')
    ])
    
    websocket_url = StringField('WebSocket URL', validators=[
        DataRequired(),
        Length(max=500, message='WebSocket URL is too long.')
    ])
    
    # Optional() has to come first. Length() runs on an empty box as well as a
    # filled one, so on its own it rejected the very thing the label tells the
    # admin to do - leave the box empty to keep the current key - and the whole
    # form refused to save. Optional() stops the chain when the box is empty;
    # the length check still applies to a key that is actually typed. The route
    # already treats a blank key as "keep the existing one".
    api_key = StringField('OpenAlgo API Key', validators=[
        Optional(),
        Length(min=10, message='API Key seems too short.')
    ])
    
    is_primary = BooleanField('Set as Primary Account')
    is_active = BooleanField('Account Active')
    
    update = SubmitField('Update Account')
    
    def __init__(self, original_name, *args, **kwargs):
        super(EditAccountForm, self).__init__(*args, **kwargs)
        self.original_name = original_name
        self._offer_current_broker(original_name)

    def _offer_current_broker(self, original_name):
        """
        Make sure the account's own broker is one of the choices.

        The list above holds display names, but broker_name on the account is
        whatever OpenAlgo reports through its ping response - an identifier such
        as "dhan_sandbox". Those never match, so the dropdown fell back to
        showing the first entry in the list, and saving the form quietly
        rewrote the account's broker to that first entry. Adding the real value
        as a choice keeps the dropdown honest and stops an edit to some other
        field from changing the broker behind the admin's back.
        """
        try:
            account = TradingAccount.query.filter_by(
                user_id=current_user.id,
                account_name=original_name
            ).first()
        except Exception:
            return

        if account is None or not account.broker_name:
            return

        current = account.broker_name
        if any(value == current for value, _label in self.broker_name.choices):
            return

        self.broker_name.choices = list(self.broker_name.choices) + [(current, current)]

    def validate_account_name(self, account_name):
        if account_name.data != self.original_name:
            account = TradingAccount.query.filter_by(
                user_id=current_user.id,
                account_name=account_name.data
            ).first()
            if account:
                raise ValidationError('You already have an account with this name.')