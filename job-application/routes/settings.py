"""
routes/settings.py
Settings routes for Applicant, HR, and Recruiter roles.
"""

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import login_required, current_user, logout_user
from models import db, UserSettings
from werkzeug.security import check_password_hash, generate_password_hash
import json

# ── FIX #1 / #3: shared deletion logic instead of ~250 lines of
# duplicated helper functions (nearly identical to admin.py's copy).
# See services/deletion_service.py for the single source of truth.
from services.deletion_service import delete_user_completely

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')


# ─────────────────────────────────────────────────────────────
#  Allowed roles helper
# ─────────────────────────────────────────────────────────────
ALLOWED_ROLES = {'applicant', 'hr', 'recruiter'}

def _role_allowed():
    return current_user.is_authenticated and current_user.role in ALLOWED_ROLES


# ─────────────────────────────────────────────────────────────
#  GET  /settings
# ─────────────────────────────────────────────────────────────
@settings_bp.route('/', methods=['GET'])
@login_required
def settings_page():
    if not _role_allowed():
        flash('Access denied.', 'error')
        return redirect(url_for('auth.login'))

    user_settings = UserSettings.query.filter_by(user_id=current_user.id).first()
    if not user_settings:
        user_settings = UserSettings(user_id=current_user.id)
        db.session.add(user_settings)
        db.session.commit()

    # Parse profile_audience JSON → Python list
    if user_settings.profile_audience_json:
        try:
            user_settings.profile_audience = json.loads(user_settings.profile_audience_json)
        except Exception:
            user_settings.profile_audience = []
    else:
        user_settings.profile_audience = []

    # Load blocked users
    from models import UserBlock, User
    blocked_rows  = UserBlock.query.filter_by(blocker_id=current_user.id).all()
    blocked_users = []
    for row in blocked_rows:
        u = User.query.get(row.blocked_id)
        if u:
            pic = None
            if u.profile_picture:
                pic = u.profile_picture if u.profile_picture.startswith('http') \
                      else '/static/uploads/profile_pictures/' + u.profile_picture
            blocked_users.append({
                'id':         u.id,
                'username':   u.username,
                'role':       u.role,
                'pic':        pic,
                'blocked_at': row.created_at.strftime('%b %d, %Y'),
            })

    return render_template('settings.html', settings=user_settings, blocked_users=blocked_users)


# ─────────────────────────────────────────────────────────────
#  POST /settings/save
# ─────────────────────────────────────────────────────────────
@settings_bp.route('/save', methods=['POST'])
@login_required
def save_settings():
    if not _role_allowed():
        return jsonify({'success': False, 'message': 'Access denied.'}), 403

    data    = request.get_json(silent=True) or {}
    section = data.get('section', '')

    user_settings = UserSettings.query.filter_by(user_id=current_user.id).first()
    if not user_settings:
        user_settings = UserSettings(user_id=current_user.id)
        db.session.add(user_settings)

    # ── Privacy ──────────────────────────────────────────────
    if section == 'privacy':
        if 'show_name' in data:
            user_settings.show_name = data['show_name']
        if 'show_profile' in data:
            user_settings.show_profile = data['show_profile']
        if 'profile_audience' in data:
            user_settings.profile_audience_json = json.dumps(data['profile_audience'])
        if 'show_follow_list' in data:
            user_settings.show_follow_list = data['show_follow_list']
        if 'show_follow_count' in data:
            user_settings.show_follow_count = data['show_follow_count']
        if 'who_can_message' in data:
            user_settings.who_can_message = data['who_can_message']

        # Auto-accept pending follow requests if profile is now open
        if 'show_profile' in data or 'profile_audience' in data:
            try:
                from models import FollowRequest, Follow, User as UserModel

                new_show = user_settings.show_profile or 'everyone'
                try:
                    new_audience = json.loads(user_settings.profile_audience_json) \
                                   if user_settings.profile_audience_json else []
                except Exception:
                    new_audience = []

                pending = FollowRequest.query.filter_by(
                    receiver_id=current_user.id, status='pending'
                ).all()

                for req in pending:
                    sender = UserModel.query.get(req.sender_id)
                    if not sender:
                        continue
                    auto = False
                    if new_show == 'everyone':
                        auto = True
                    elif new_show == 'specific':
                        if sender.role == 'recruiter' and 'recruiter' in new_audience:
                            auto = True
                        elif sender.role == 'hr' and 'hr' in new_audience:
                            auto = True
                    if auto:
                        req.status = 'accepted'
                        exists = Follow.query.filter_by(
                            follower_id=req.sender_id,
                            followed_id=current_user.id
                        ).first()
                        if not exists:
                            db.session.add(Follow(
                                follower_id=req.sender_id,
                                followed_id=current_user.id
                            ))
            except Exception:
                # FIX #5: was `except Exception as e: print(...)` — now
                # goes through the app logger with a full traceback so
                # it's visible in production monitoring, not just a
                # local console that nobody's watching.
                current_app.logger.exception(
                    f"[settings.save_settings] follow-request auto-accept error for user_id={current_user.id}"
                )

    # ── Notifications ─────────────────────────────────────────
    elif section == 'notifications':
        user_settings.notif_app_status = bool(data.get('notif_app_status', False))
        user_settings.notif_messages   = bool(data.get('notif_messages',   False))
        user_settings.notif_followers  = bool(data.get('notif_followers',  False))
        user_settings.notif_jobs       = bool(data.get('notif_jobs',       False))

    # ── Security (2FA toggle) ─────────────────────────────────
    elif section == 'security':
        if 'two_factor' in data:
            user_settings.two_factor        = bool(data['two_factor'])
            user_settings.two_factor_code   = None   # clear any leftover PIN
            user_settings.two_factor_expiry = None

    # ── Email ─────────────────────────────────────────────────
    elif section == 'email':
        new_email = data.get('new_email', '').strip()
        if not new_email:
            return jsonify({'success': False, 'message': 'Email cannot be empty.'}), 400
        from models import User
        existing = User.query.filter_by(email=new_email).first()
        if existing and existing.id != current_user.id:
            return jsonify({'success': False, 'message': 'That email is already in use.'}), 409
        current_user.email = new_email
        db.session.add(current_user)

    # ── Password ─────────────────────────────────────────────
    elif section == 'password':
        current_pass = data.get('current_password', '')
        new_pass     = data.get('new_password', '')
        confirm_pass = data.get('confirm_password', '')

        if not current_user.password:
            return jsonify({'success': False, 'message': 'Password change not available for Google accounts.'}), 400
        if not check_password_hash(current_user.password, current_pass):
            return jsonify({'success': False, 'message': 'Current password is incorrect.'}), 400
        if new_pass != confirm_pass:
            return jsonify({'success': False, 'message': 'New passwords do not match.'}), 400
        if len(new_pass) < 8:
            return jsonify({'success': False, 'message': 'Password must be at least 8 characters.'}), 400

        current_user.password = generate_password_hash(new_pass)
        db.session.add(current_user)

    # ── Appearance ────────────────────────────────────────────
    elif section == 'appearance':
        if 'theme' in data:
            user_settings.theme = data['theme']
        if 'density' in data:
            user_settings.density = data['density']

    # ── Language ─────────────────────────────────────────────
    elif section == 'language':
        if 'language' in data:
            user_settings.language = data['language']
        if 'timezone' in data:
            user_settings.timezone = data['timezone']

    else:
        return jsonify({'success': False, 'message': f'Unknown section: {section}'}), 400

    try:
        db.session.commit()
        return jsonify({'success': True})
    except Exception:
        db.session.rollback()
        # FIX #5: was print(traceback) via `import traceback; traceback.print_exc()`
        # — now logged through current_app.logger so it goes wherever the
        # rest of the app's logging is actually configured to go.
        current_app.logger.exception(
            f"[settings.save_settings] DB error saving section={section} for user_id={current_user.id}"
        )
        return jsonify({'success': False, 'message': 'Database error. Please try again.'}), 500


# ─────────────────────────────────────────────────────────────
#  POST /settings/unblock/<id>
# ─────────────────────────────────────────────────────────────
@settings_bp.route('/unblock/<int:target_id>', methods=['POST'])
@login_required
def unblock_user(target_id):
    from models import UserBlock
    block = UserBlock.query.filter_by(
        blocker_id=current_user.id, blocked_id=target_id
    ).first()
    if block:
        db.session.delete(block)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Block not found.'}), 404


# ─────────────────────────────────────────────────────────────
#  POST /settings/deactivate
# ─────────────────────────────────────────────────────────────
@settings_bp.route('/deactivate', methods=['POST'])
@login_required
def deactivate_account():
    if not _role_allowed():
        return jsonify({'success': False}), 403

    from models import User

    user = db.session.get(User, current_user.id)
    user.is_deactivated = True
    db.session.commit()

    logout_user()
    return jsonify({'success': True})


# ==============================================================
#  POST /settings/delete-account
#
# ── FIX #1 / #3 ──────────────────────────────────────────────
# BEFORE: ~250 lines of nested helper functions
#   (_delete_upload, _delete_user_files, _delete_job_files, _run,
#   _delete_user_data) almost identical to admin.py's delete_user()
#   — including the recruiter-specific branch that deletes owned
#   jobs and HR sub-accounts.
# AFTER: the whole cascade now lives once in
#   services/deletion_service.py. This route keeps only what's
#   specific to a SELF-service deletion: verifying the caller's own
#   password before proceeding, and logging them out only after a
#   confirmed successful commit (so a failed delete doesn't lock
#   the user out of a still-existing account).
# ==============================================================
@settings_bp.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    if not _role_allowed():
        return jsonify({'success': False, 'message': 'Access denied.'}), 403

    data           = request.get_json(silent=True) or {}
    password_input = data.get('password', '')

    # Google users have no password — skip check
    if current_user.password:
        if not password_input:
            return jsonify({'success': False, 'message': 'Please enter your password to confirm.'}), 400
        if not check_password_hash(current_user.password, password_input):
            return jsonify({'success': False, 'message': 'Incorrect password.'}), 400

    user_id = current_user.id

    ok, error = delete_user_completely(user_id)

    if not ok:
        return jsonify({'success': False, 'message': f'Could not delete account: {error}'}), 500

    # Commit already happened inside the service; only log the user
    # out on confirmed success — mirrors the original ordering
    # rationale: "if commit fails, user stays logged in and can retry
    # rather than being locked out of a still-existing account."
    logout_user()
    return jsonify({'success': True})


# ─────────────────────────────────────────────────────────────
#  POST /settings/logout-all
# ─────────────────────────────────────────────────────────────
@settings_bp.route('/logout-all', methods=['POST'])
@login_required
def logout_all_devices():
    return jsonify({'success': True})