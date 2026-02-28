from dotenv import load_dotenv
from flask import Flask
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

db = SQLAlchemy()
migrate = Migrate()


def create_app():
    app = Flask(__name__)
    app.config.from_object('app.config.Config')

    if not app.config.get('CANVAS_API_TOKEN'):
        raise RuntimeError(
            'CANVAS_API_TOKEN is not set. Add it to your .env file.'
        )

    db.init_app(app)
    migrate.init_app(app, db)

    from app.routes import dashboard
    app.register_blueprint(dashboard.bp)

    return app
