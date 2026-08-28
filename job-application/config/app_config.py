import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = "supersecretkey"

    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False 

    DB_NAME = "job_portal"

    

    SQLALCHEMY_DATABASE_URI = "mysql+pymysql://root:@localhost/job_portal"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Gmail SMTP
    MAIL_SERVER = "smtp.gmail.com"
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_USERNAME")

    # Google OAuth
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")