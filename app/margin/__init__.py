from flask import Blueprint

margin_bp = Blueprint('margin', __name__, url_prefix='/margin')

from app.margin import routes