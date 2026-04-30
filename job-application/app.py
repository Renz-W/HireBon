from flask import Flask
from config import Config
from models import db, User, RecruiterProfile, UserBlock, UserReport, ResignationRequest, get_ph_time
from flask_login import LoginManager
from flask_mail import Mail
from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash
from datetime import datetime
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
import os
import pymysql
import secrets
import logging
import json

# CREATE DATABASE IF NOT EXISTS
connection = pymysql.connect(
    host="localhost",
    user="root",
    password=""
)
cursor = connection.cursor()
cursor.execute("CREATE DATABASE IF NOT EXISTS job_portal")
connection.close()

app = Flask(__name__)
app.config.from_object(Config)
app.config['WTF_CSRF_HEADERS'] = ['X-CSRFToken']

csrf = CSRFProtect()
csrf.init_app(app)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db.init_app(app)
migrate = Migrate(app, db)
mail = Mail(app)

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ============================================================
# JINJA2 FILTERS
# ============================================================

@app.template_filter('get_recruiter_profile')
def get_recruiter_profile(user_id):
    if not user_id:
        return None
    return RecruiterProfile.query.filter_by(user_id=user_id).first()

def _parse_json(value, default=None):
    if not value:
        return default if default is not None else []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default if default is not None else []

app.jinja_env.filters['fromjson']  = _parse_json
app.jinja_env.filters['from_json'] = _parse_json

# ============================================================
# CONTEXT PROCESSOR
# ============================================================

import pytz
from datetime import datetime

ph_tz = pytz.timezone("Asia/Manila")

@app.context_processor
def inject_now():
    return {'now': datetime.now(ph_tz)}

# ============================================================
# AUTO-UNBAN EXPIRED TIMED BANS
# ============================================================
@app.before_request
def auto_unban_expired():
    try:
        expired = User.query.filter(
            User.is_banned == True,
            User.ban_until != None,
            User.ban_until <= get_ph_time()
        ).all()
        if expired:
            for u in expired:
                u.is_banned  = False
                u.ban_until  = None
                u.ban_reason = None
                u.banned_at  = None
            db.session.commit()

        from models import Job
        expired_jobs = Job.query.filter(
            Job.is_taken_down == True,
            Job.takedown_until != None,
            Job.takedown_until <= get_ph_time()
        ).all()
        if expired_jobs:
            for j in expired_jobs:
                j.is_taken_down   = False
                j.takedown_until  = None
                j.takedown_reason = None
                j.taken_down_at   = None
            db.session.commit()

    except Exception:
        pass

# ============================================================
# AUTO-PROCESS EXPIRED RENDERING PERIODS
# ============================================================
@app.before_request
def auto_process_rendering_periods():
    try:
        from routes.employment import process_expired_rendering_periods
        process_expired_rendering_periods()
    except Exception:
        pass

@app.before_request
def check_user_ban():
    from flask_login import current_user
    from flask import request, render_template

    if request.endpoint and (
        request.endpoint.startswith('static') or
        request.endpoint in ('auth.login', 'auth.logout', 'auth.index', 'auth.register',
                             'auth.google_login', 'auth.google_callback',
                             'auth.forgot_password', 'auth.reset_password')
    ):
        return None

    if current_user.is_authenticated:
        db.session.expire(current_user)
        if current_user.is_banned:
            if request.endpoint == 'auth.logout':
                return None
            return render_template("account_banned.html", user=current_user), 403

# ============================================================
# REGISTER BLUEPRINTS
# ============================================================
from routes.auth import auth_bp
from routes.applicant import applicant_bp
from routes.recruiter import recruiter_bp
from routes.hr import hr_bp
from routes.admin import admin_bp
from routes.chat import chat_bp
from routes.profile_view import profile_view_bp
from routes.settings import settings_bp
from routes.report_block import report_block_bp
from routes.employment import employment_bp

app.register_blueprint(auth_bp)
app.register_blueprint(applicant_bp)
app.register_blueprint(recruiter_bp)
app.register_blueprint(hr_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(profile_view_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(report_block_bp)
app.register_blueprint(employment_bp)

# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    with app.app_context():
        db.create_all()

        if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            existing_admin = User.query.filter_by(role="admin").first()
            if not existing_admin:
                admin = User(
                    username="admin",
                    email="admin@gmail.com",
                    password=generate_password_hash("admin123"),
                    role="admin",
                    is_verified=True
                )
                db.session.add(admin)
                db.session.commit()
                print("=" * 50)
                print("Admin account created!")
                print("=" * 50)

            fresh_token = secrets.token_urlsafe(32)
            app.config['ADMIN_TOKEN'] = fresh_token

            print("\n" + "=" * 50)
            print("Flask App URL:")
            print("http://127.0.0.1:5000/")
            print("\nADMIN LOGIN URL (this session only):")
            print(f"http://127.0.0.1:5000/admin/login/{fresh_token}")
            print("\nADMIN CREDENTIALS:")
            print("Account: admin@gmail.com")
            print("Password: admin123")
            print("=" * 50 + "\n")

    app.run(debug=True)