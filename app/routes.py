# app/routes.py
from flask import Blueprint, render_template, request
from app.search import SearchEngine
from app.utils import validate_input, rate_limit

main = Blueprint('main', __name__)

@main.route('/', methods=['GET', 'POST'])
@rate_limit(1)
def index():
    # Код маршрута из предыдущего примера
