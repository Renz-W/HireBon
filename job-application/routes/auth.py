from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_mail import Message
from authlib.integrations.flask_client import OAuth
from models import db, User, RecruiterProfile, Job, ApplicantProfile, get_ph_time
import os
import uuid
import secrets
import random
from datetime import date, datetime, timedelta

# FIX: centralized config instead of a hardcoded string
# buried in an f-string ("http://127.0.0.1:5000/login").
from config.constants import SiteConfig

auth_bp = Blueprint("auth", __name__)


# =========================
# CONFIG
# =========================
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "doc", "docx"}


# =========================
# HELPERS
# =========================
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file, upload_folder):
    if file and file.filename:
        filename = f"{uuid.uuid4()}_{secure_filename(file.filename)}"
        filepath = os.path.join(upload_folder, filename)
        file.save(filepath)
        return filename
    return None


def redirect_by_role(user):
    if user.role == "applicant":
        return redirect(url_for("applicant.profile"))
    if user.role == "recruiter":
        return redirect(url_for("recruiter.profile"))
    if user.role == "hr":
        return redirect(url_for("hr.profile"))
    if user.role == "admin":
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("auth.index"))


# ==============================================================
# FIX: safe wrapper for admin notifications
#
# BEFORE: every call site that pushes an admin notification during
#   registration wrapped it in a bare `try: ... except: pass`,
#   meaning a bug in push_admin_notif() itself was invisible — no
#   log line, no trace, nothing. This same block was copy-pasted at
#   three separate spots in this file alone.
#
# AFTER: one helper that still never blocks the primary action
#   (registration must succeed even if the notification insert
#   fails) but logs the failure with a full traceback so it shows
#   up wherever the app's logging is actually configured to go.
# ==============================================================
def push_admin_notif_safe(notif_type, message, user_id=None):
    try:
        from routes.admin import push_admin_notif
        push_admin_notif(notif_type, message, user_id=user_id)
    except Exception:
        current_app.logger.exception(
            f"[push_admin_notif_safe] failed to push notif type={notif_type} user_id={user_id}"
        )


# =========================
# SEND VERIFICATION EMAIL
# =========================
def send_verification_email(user):
    """
    FIX:
      - BEFORE: caught Exception and used print(), so a broken mail
        server failed completely silently in production.
      - BEFORE: hardcoded "http://127.0.0.1:5000/login" in the email
        body, which would still point recruiters at localhost after
        a real deployment.
      - AFTER: logs with current_app.logger.exception() (keeps the
        traceback), and builds the login URL from SiteConfig.BASE_URL,
        which reads the SITE_BASE_URL environment variable in
        production and only falls back to localhost for local dev.
    """
    if user.role != "recruiter":
        return

    try:
        from app import mail

        login_url = f"{SiteConfig.BASE_URL}/login"

        subject = "Your Recruiter Account Has Been Verified – Job Portal"
        body = f"""Hello {user.username},

Great news! Your recruiter account has been reviewed and approved by our admin team.

You can now log in and start posting jobs at: {login_url}

Welcome to Job Portal!

– The Job Portal Team"""

        msg = Message(
            subject=subject,
            recipients=[user.email],
            body=body
        )
        mail.send(msg)

    except Exception:
        current_app.logger.exception(
            f"Failed to send verification email to user_id={user.id} email={user.email}"
        )


# =========================
# SEND 2FA EMAIL
# =========================
def _send_2fa_email(to_email: str, username: str, pin: str):
    """Send a 6-digit 2FA PIN to the user's email."""
    try:
        from app import mail

        msg = Message(
            subject="Your Login Verification Code – Job Portal",
            recipients=[to_email],
        )
        msg.html = f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;
                    padding:32px 24px;background:#f9faf9;border-radius:12px;">
          <div style="text-align:center;margin-bottom:24px;">
            <div style="display:inline-block;background:#164A41;border-radius:50%;
                        width:64px;height:64px;line-height:64px;font-size:28px;color:#fff;">
              🔐
            </div>
          </div>
          <h2 style="color:#164A41;text-align:center;margin:0 0 8px;font-size:20px;">
            Two-Factor Authentication
          </h2>
          <p style="color:#555;text-align:center;margin:0 0 28px;font-size:14px;">
            Hi <strong>{username}</strong>, here is your one-time login code.
          </p>

          <div style="background:#fff;border:2px solid #4D774E;border-radius:12px;
                      padding:28px;text-align:center;margin-bottom:24px;">
            <div style="font-size:44px;font-weight:900;letter-spacing:12px;
                        color:#164A41;font-family:monospace;">
              {pin}
            </div>
          </div>

          <p style="color:#888;font-size:12px;text-align:center;margin:0 0 4px;">
            This code expires in <strong>10 minutes</strong>.
          </p>
          <p style="color:#888;font-size:12px;text-align:center;margin:0;">
            If you did not attempt to log in, please secure your account immediately.
          </p>
        </div>
        """
        mail.send(msg)
    except Exception:
        # FIX: logged with traceback instead of print().
        current_app.logger.exception(f"[2FA] Failed to send email to {to_email}")


# =========================
# PUBLIC PAGES
# =========================
@auth_bp.route("/")
def index():
    return render_template("home.html")


@auth_bp.route("/jobs")
def jobs():
    from models import SavedJob

    # ── Fetch ALL non-taken-down jobs from non-banned recruiters.
    # ── Expired/closed jobs are intentionally included so the
    # ── frontend JS can show them when "Show closed & expired" is checked.
    jobs = (
        Job.query
        .join(User, Job.company_id == User.id)
        .filter(
            Job.is_taken_down == False,
            User.is_banned == False,
            User.is_deactivated == False,
        ).all()
    )

    saved_job_ids = set()
    if current_user.is_authenticated and current_user.role == 'applicant':
        saved_job_ids = {
            s.job_id for s in SavedJob.query.filter_by(applicant_id=current_user.id).all()
        }

    return render_template(
        "index.html",
        jobs=jobs,
        saved_job_ids=saved_job_ids,
        today=date.today()
    )


@auth_bp.route("/help")
def help_page():
    if current_user.is_authenticated and current_user.role == 'admin':
        return redirect(url_for('admin.dashboard'))
    return render_template("help.html")

@auth_bp.route("/about")
def about():
    return render_template("about.html")


# =========================
# LOGIN
# =========================
@auth_bp.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email    = request.form.get("email")
        password = request.form.get("password")

        user = User.query.filter_by(email=email).first()

        if not user:
            flash("Invalid email or password", "info")
            return redirect(url_for("auth.login"))

        if user.role == "admin":
            flash("Invalid email or password", "info")
            return redirect(url_for("auth.login"))

        if user.google_id and not user.password:
            flash("This account uses Google sign-in. Please use the 'Sign in with Google' button.", "info")
            return redirect(url_for("auth.login"))

        if not check_password_hash(user.password, password):
            flash("Invalid email or password", "login_error")
            return redirect(url_for("auth.login"))

        if user.is_banned:
            return render_template("account_banned.html", user=user)

        if user.is_deactivated:
            user.is_deactivated = False         # auto-reactivate on login
            db.session.commit()

        if user.role == "recruiter":
            profile = user.recruiter_profile
            if user.verification_status == "Rejected":
                return render_template("account_rejected.html", user=user)

        # ── 2FA check ──────────────────────────────────────────────────────────
        from models import UserSettings
        user_settings = UserSettings.query.filter_by(user_id=user.id).first()

        if user_settings and user_settings.two_factor:
            pin = str(random.randint(100000, 999999))
            user_settings.two_factor_code   = pin
            user_settings.two_factor_expiry = datetime.utcnow() + timedelta(minutes=10)
            db.session.commit()

            _send_2fa_email(user.email, user.username, pin)

            # Store user id in session — NOT logged in yet
            session['2fa_user_id'] = user.id

            flash("A 6-digit verification code has been sent to your email.", "info")
            return redirect(url_for("auth.verify_2fa"))
        # ── End 2FA check ──────────────────────────────────────────────────────

        login_user(user)

        if user.role == "hr" and user.must_change_password:
            flash("You must change your temporary password.", "hr_password_notice")
            return redirect(url_for("hr.change_password"))

        flash("Logged in successfully", "success")
        return redirect_by_role(user)

    return render_template("auth/login.html")


# =========================
# VERIFY 2FA
# =========================
@auth_bp.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    from models import UserSettings

    user_id = session.get('2fa_user_id')
    if not user_id:
        flash("Session expired. Please log in again.", "warning")
        return redirect(url_for("auth.login"))

    user = User.query.get(user_id)
    if not user:
        session.pop('2fa_user_id', None)
        flash("User not found. Please log in again.", "warning")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        entered_pin  = request.form.get("pin", "").strip()
        user_settings = UserSettings.query.filter_by(user_id=user_id).first()

        if (
            user_settings
            and user_settings.two_factor_code
            and user_settings.two_factor_expiry
            and datetime.utcnow() <= user_settings.two_factor_expiry
            and entered_pin == user_settings.two_factor_code
        ):
            # Clear the one-time code
            user_settings.two_factor_code   = None
            user_settings.two_factor_expiry = None
            db.session.commit()

            session.pop('2fa_user_id', None)
            login_user(user)

            if user.role == "hr" and user.must_change_password:
                flash("You must change your temporary password.", "hr_password_notice")
                return redirect(url_for("hr.change_password"))

            flash("Logged in successfully", "success")
            return redirect_by_role(user)

        else:
            flash("Invalid or expired code. Please try again.", "danger")

    return render_template("auth/verify_2fa.html", email=user.email)


# =========================
# RESEND 2FA CODE
# =========================
@auth_bp.route("/resend-2fa", methods=["POST"])
def resend_2fa():
    from models import UserSettings

    user_id = session.get('2fa_user_id')
    if not user_id:
        flash("Session expired. Please log in again.", "warning")
        return redirect(url_for("auth.login"))

    user          = User.query.get(user_id)
    user_settings = UserSettings.query.filter_by(user_id=user_id).first()

    if user and user_settings:
        pin = str(random.randint(100000, 999999))
        user_settings.two_factor_code   = pin
        user_settings.two_factor_expiry = datetime.utcnow() + timedelta(minutes=10)
        db.session.commit()

        _send_2fa_email(user.email, user.username, pin)
        flash("A new verification code has been sent to your email.", "success")

    return redirect(url_for("auth.verify_2fa"))


# =========================
# GOOGLE OAuth
# =========================
def get_oauth():
    from app import oauth
    return oauth


@auth_bp.route("/login/google")
def google_login():
    redirect_uri = url_for("auth.google_callback", _external=True)
    return get_oauth().google.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/google/callback")
def google_callback():
    from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError

    # User clicked "Cancel" on Google's consent screen
    if request.args.get('error') == 'access_denied':
        flash("Google sign-in was cancelled.", "info")
        return redirect(url_for("auth.login"))

    try:
        token = get_oauth().google.authorize_access_token()
    except MismatchingStateError:
        flash("Login session expired. Please try again.", "info")
        return redirect(url_for("auth.login"))
    except OAuthError:
        flash("Google sign-in failed. Please try again.", "danger")
        return redirect(url_for("auth.login"))

    info = token.get("userinfo")

    if not info:
        flash("Google login failed. Please try again.", "error")
        return redirect(url_for("auth.login"))

    google_id      = info["sub"]
    email          = info["email"]
    name           = info.get("name", email.split("@")[0])
    google_picture = info.get("picture")

    user = User.query.filter_by(google_id=google_id).first()

    if not user:
        user = User.query.filter_by(email=email).first()

        if user:
            if user.role == "admin":
                flash("Invalid login method.", "login_error")
                return redirect(url_for("auth.login"))

            user.google_id = google_id
            if google_picture and not user.profile_picture:
                user.profile_picture = google_picture
            db.session.commit()

        else:
            session["google_id"]      = google_id
            session["google_email"]   = email
            session["google_name"]    = name
            session["google_picture"] = google_picture

            return redirect(url_for("auth.google_role_select"))

    if user.role == "admin":
        flash("Invalid login method.", "login_error")
        return redirect(url_for("auth.login"))

    if user.is_banned:
        return render_template("account_banned.html", user=user)

    if user.is_deactivated:
        user.is_deactivated = False         # auto-reactivate on login
        db.session.commit()

    if user.role == "recruiter":
        profile = user.recruiter_profile
        if user.verification_status == "Rejected":
            return render_template("account_rejected.html", user=user)

    if google_picture and not user.profile_picture:
        user.profile_picture = google_picture
        db.session.commit()

    login_user(user)

    if not current_user.is_authenticated:
        flash("Login failed. Please try again.", "danger")
        return redirect(url_for("auth.login"))

    if user.role == "hr" and user.must_change_password:
        flash("You must change your temporary password.", "hr_password_notice")
        return redirect(url_for("hr.change_password"))

    flash("Logged in with Google!", "success")
    return redirect_by_role(user)

@auth_bp.route("/google-role-select", methods=["GET", "POST"])
def google_role_select():

    if not session.get("google_id"):
        flash("Session expired. Please try again.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        role = request.form.get("role")

        if role not in ["applicant", "recruiter"]:
            flash("Please select a valid role.", "error")
            return redirect(url_for("auth.google_role_select"))

        google_id      = session.pop("google_id")
        email          = session.pop("google_email")
        name           = session.pop("google_name")
        google_picture = session.pop("google_picture", None)
        session.pop("google_role", None)

        if role == "applicant":
            user = User(
                username=name,
                email=email,
                google_id=google_id,
                role="applicant",
                verification_status="Approved",
                is_verified=True,
                profile_completed=False,
                profile_picture=google_picture,
            )
            db.session.add(user)
            db.session.commit()

            applicant_profile = ApplicantProfile(user_id=user.id)
            db.session.add(applicant_profile)
            db.session.commit()

            # FIX: was a bare try/except: pass — now logs failures.
            push_admin_notif_safe(
                "account_request",
                f"New applicant account registered via Google: <strong>{user.username}</strong>",
                user_id=user.id,
            )

            login_user(user)
            flash("Account created with Google! Please complete your profile to unlock all features.", "success")
            return redirect(url_for("applicant.profile"))

        else:  # recruiter
            user = User(
                username=name,
                email=email,
                google_id=google_id,
                role="recruiter",
                verification_status="Pending",
                is_verified=False,
                profile_completed=False,
                profile_picture=google_picture,
            )
            db.session.add(user)
            db.session.commit()

            profile = RecruiterProfile(user_id=user.id)
            db.session.add(profile)
            db.session.commit()

            # FIX: was a bare try/except: pass — now logs failures.
            push_admin_notif_safe(
                "account_request",
                f"New recruiter account registered via Google: <strong>{user.username}</strong>",
                user_id=user.id,
            )

            login_user(user)
            flash("Account created! Complete your company profile, then submit for admin verification.", "info")
            return redirect(url_for("recruiter.profile"))

    return render_template("auth/google_role_select.html")


# =========================
# GOOGLE APPLICANT PROFILE  ← kept for backwards-compat but no longer used
# =========================
@auth_bp.route("/google-applicant-profile", methods=["GET", "POST"])
def google_applicant_profile():
    if not session.get("google_id"):
        return redirect(url_for("auth.login"))
    return redirect(url_for("auth.google_role_select"))


# =========================
# GOOGLE RECRUITER PROFILE  ← kept for backwards-compat but no longer used
# =========================
@auth_bp.route("/google-recruiter-profile", methods=["GET", "POST"])
def google_recruiter_profile():
    if not session.get("google_id"):
        return redirect(url_for("auth.login"))
    return redirect(url_for("auth.google_role_select"))


# =========================
# REGISTER
# =========================
@auth_bp.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        role         = request.form.get("role")
        username     = request.form.get("username")
        email        = request.form.get("email")
        password_raw = request.form.get("password")

        if role not in ["applicant", "recruiter"]:
            flash("Invalid role", "register_error")
            return redirect(url_for("auth.register"))

        if not username:
            username = email.split("@")[0]

        if User.query.filter_by(email=email, is_deleted=False).first():
            flash("Email already exists", "register_error")
            return redirect(url_for("auth.register"))

        if User.query.filter_by(username=username, is_deleted=False).first():
            flash("Username already exists", "register_error")
            return redirect(url_for("auth.register"))

        password = generate_password_hash(password_raw)

        user = User(
            username=username,
            email=email,
            password=password,
            role=role,
            verification_status="Approved" if role == "applicant" else "Pending",
            is_verified=role == "applicant",
            profile_completed=False,
        )

        db.session.add(user)
        db.session.commit()

        # FIX: was `try: ... except: pass` — now logs failures.
        push_admin_notif_safe(
            'account_request',
            f'New {user.role} account registered: <strong>{user.username}</strong>',
            user_id=user.id
        )

        # =========================
        # APPLICANT
        # =========================
        if role == "applicant":
            dob_str = request.form.get("date_of_birth")
            dob = None
            if dob_str:
                try:
                    dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
                except:
                    dob = None

            applicant_profile = ApplicantProfile(
                user_id=user.id,
                last_name=request.form.get("surname") or "",
                first_name=request.form.get("first_name") or "",
                middle_name=request.form.get("middle_name") or "",
                date_of_birth=dob,
                gender=request.form.get("gender") or "",
                phone_number=request.form.get("phone_number") or "",
                country=request.form.get("country") or "",
                city=request.form.get("city") or "",
                home_address=request.form.get("home_address") or "",
            )
            db.session.add(applicant_profile)
            db.session.commit()

            login_user(user)
            flash("Account created! Complete your profile to unlock all features.", "success")
            return redirect_by_role(user)

        # =========================
        # RECRUITER
        # =========================
        if role == "recruiter":
            upload_folder = os.path.join(current_app.root_path, "static", "uploads", "recruiter_documents")
            os.makedirs(upload_folder, exist_ok=True)

            logo_file  = request.files.get("company_logo")
            proof_file = request.files.get("company_proof")

            logo_filename  = save_uploaded_file(logo_file,  upload_folder) if logo_file  else None
            proof_filename = save_uploaded_file(proof_file, upload_folder) if proof_file else None

            profile = RecruiterProfile(
                user_id=user.id,
                surname=request.form.get("surname") or "",
                first_name=request.form.get("first_name") or "",
                middle_name=request.form.get("middle_name") or "",
                phone_number=request.form.get("phone_number") or "",
                company_name=request.form.get("company_name") or "",
                company_industry=request.form.get("company_industry") or "",
                company_description=request.form.get("company_description") or "",
                company_address=request.form.get("company_address") or "",
                country=request.form.get("country") or "",
                city=request.form.get("city") or "",
                office_address=request.form.get("office_address") or "",
                company_email_domain=request.form.get("company_email_domain") or "",
                company_logo=logo_filename,
                company_proof=proof_filename
            )
            db.session.add(profile)
            db.session.commit()

            login_user(user)
            flash("Account created! Complete your company profile, then submit for admin verification.", "info")
            return redirect_by_role(user)

    return render_template("auth/register.html")


# =========================
# CHECK EMAIL/USERNAME AVAILABILITY
# =========================
@auth_bp.route("/check-availability", methods=["POST"])
def check_availability():
    data = request.get_json()

    email    = data.get("email", "").strip()
    username = data.get("username", "").strip()

    email_taken    = User.query.filter_by(email=email, is_deleted=False).first() is not None
    username_taken = User.query.filter_by(username=username, is_deleted=False).first() is not None

    return jsonify({
        "email_taken":    email_taken,
        "username_taken": username_taken
    })


# =========================
# FORGOT PASSWORD
# =========================
@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email")
        user  = User.query.filter_by(email=email).first()

        if user:
            if user.google_id and not user.password:
                # Pure Google-only account — no password to reset
                flash("This email is linked to a Google account. Please use 'Sign in with Google' to log in.", "login_warning")
                return redirect(url_for("auth.login"))

            # Any user with a password (including Google users who later set one)
            token = secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_token_expiry = get_ph_time() + timedelta(minutes=30)
            db.session.commit()

            reset_url = url_for("auth.reset_password", token=token, _external=True)

            try:
                from app import mail
                msg = Message(
                    subject="Reset Your Password – Job Portal",
                    recipients=[email],
                    body=(
                        f"Hello {user.username},\n\n"
                        f"Click the link below to reset your password. "
                        f"It expires in 30 minutes.\n\n"
                        f"{reset_url}\n\n"
                        f"If you did not request this, you can safely ignore this email."
                    )
                )
                mail.send(msg)
            except Exception:
                # FIX: previously this send() call wasn't wrapped at
                # all, so a mail server outage would 500 the whole
                # request and leak a stack trace to the user. Now it's
                # logged server-side and the user still gets the same
                # generic "if that email is registered..." message
                # below, matching the no-leak intent of that message.
                current_app.logger.exception(
                    f"Failed to send password reset email to {email}"
                )

        # Always show the same generic message to avoid leaking whether an email exists
        flash("If that email is registered, a reset link has been sent.", "info")
        return redirect(url_for("auth.forgot_password"))

    return render_template("auth/forgot_password.html")


# =========================
# RESET PASSWORD
# =========================
@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):

    user = User.query.filter_by(reset_token=token).first()

    if not user or not user.reset_token_expiry:
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot_password"))

    expiry = user.reset_token_expiry.replace(tzinfo=None)
    if expiry < get_ph_time().replace(tzinfo=None):
        flash("This reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("password")

        user.password           = generate_password_hash(new_password)
        user.reset_token        = None
        user.reset_token_expiry = None
        db.session.commit()

        flash("Password reset successfully! Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)


# =========================
# ACCOUNT REJECTED PAGE
# =========================
@auth_bp.route("/account-rejected/<int:user_id>")
def account_rejected(user_id):
    user = User.query.get_or_404(user_id)
    return render_template("account_rejected.html", user=user)


# =========================
# LOGOUT
# =========================
@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out successfully", "info")
    return redirect(url_for("auth.index"))

@auth_bp.route("/jobs/<int:job_id>")
def job_redirect(job_id):
    return redirect(url_for("applicant.job_details", job_id=job_id))