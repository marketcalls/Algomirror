from flask import Blueprint

strategy_bp = Blueprint('strategy', __name__, url_prefix='/strategy')

from app.strategy import routes