# ================================================================
# FIX: "God Object / Large File": HR-account lifecycle
#   (create / soft-delete / undo / commit-delete, single + bulk
#   versions — ~230 lines) used to live inside the same 1000-line
#   routes/recruiter.py as job posting, job editing, and application
#   review. Pulled out here so each file only changes when ITS
#   concern changes.
# ================================================================

import secrets
import string
from flask import render_template, redirect, url_for, flash, request, session, jsonify
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from models import db, User, HRProfile, get_ph_time

# Reuse the SAME blueprint object recruiter.py already created —
# do NOT do `Blueprint('recruiter', __name__, ...)` again here.
from routes.recruiter import recruiter_bp, check_banned


def generate_temp_password(length=10):
    characters = string.ascii_letters + string.digits
    return ''.join(secrets.choice(characters) for _ in range(length))


@recruiter_bp.route('/hr-accounts')
@login_required
def hr_accounts():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    if not current_user.is_verified:
        flash("Your account must be verified to manage HR accounts.", "warning")
        return redirect(url_for('recruiter.profile'))

    temp_password = session.pop('temp_password', None)

    hrs = User.query.filter_by(
        created_by=current_user.id, role="hr", is_deleted=False, is_banned=False
    ).all()

    return render_template("recruiter/hr_accounts.html", hrs=hrs, temp_password=temp_password)


@recruiter_bp.route('/create-hr', methods=['POST'])
@login_required
def create_hr():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    if not current_user.is_verified:
        flash("Your account must be verified to create HR accounts.", "warning")
        return redirect(url_for('recruiter.profile'))

    username = request.form.get('username')
    email    = request.form.get('email')

    existing_username = User.query.filter_by(username=username, is_deleted=False).first()
    if existing_username:
        flash("Username already exists. Please choose another.", "danger")
        return redirect(url_for('recruiter.hr_accounts'))

    existing_email = User.query.filter_by(email=email, is_deleted=False).first()
    if existing_email:
        flash("Email is already registered.", "danger")
        return redirect(url_for('recruiter.hr_accounts'))

    temp_password = generate_temp_password()

    # Reuse soft-deleted row if username or email matches one
    ghost = User.query.filter(
        User.is_deleted == True,
        db.or_(User.username == username, User.email == email)
    ).first()

    if ghost:
        ghost.username             = username
        ghost.email                = email
        ghost.password             = generate_password_hash(temp_password)
        ghost.role                 = 'hr'
        ghost.created_by           = current_user.id
        ghost.must_change_password = True
        ghost.is_deleted           = False
        ghost.deleted_at           = None
        ghost.deleted_by           = None
        ghost.is_banned            = False
        ghost.ban_reason           = None
        ghost.banned_at            = None
        ghost.ban_until            = None
        ghost.is_verified          = False
        ghost.verification_status  = 'Pending'
        ghost.profile_completed    = False
        ghost.profile_picture      = None
        ghost.reset_token          = None
        ghost.reset_token_expiry   = None
        ghost.created_at           = get_ph_time()

        old_profile = HRProfile.query.filter_by(user_id=ghost.id).first()
        if old_profile:
            db.session.delete(old_profile)
            db.session.flush()

        db.session.commit()
    else:
        new_hr = User(
            username=username,
            email=email,
            password=generate_password_hash(temp_password),
            role="hr",
            created_by=current_user.id,
            must_change_password=True
        )
        db.session.add(new_hr)
        db.session.commit()

    session['temp_password'] = temp_password
    return redirect(url_for('recruiter.hr_accounts'))


# ================================================================
# SOFT-DELETE / UNDO-DELETE / COMMIT-DELETE HR (single + bulk)
# ================================================================

@recruiter_bp.route('/soft-delete-hr/<int:hr_id>', methods=['POST'])
@login_required
def soft_delete_hr(hr_id):
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    hr = User.query.filter_by(id=hr_id, role='hr', created_by=current_user.id, is_deleted=False).first()
    if not hr:
        return jsonify({'success': False, 'error': 'HR account not found'}), 404

    hr.is_deleted = True
    hr.deleted_at = get_ph_time()
    hr.deleted_by = current_user.id
    db.session.commit()

    return jsonify({'success': True, 'hr_id': hr_id})


@recruiter_bp.route('/soft-delete-all-hr', methods=['POST'])
@login_required
def soft_delete_all_hr():
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    hrs = User.query.filter_by(created_by=current_user.id, role='hr', is_deleted=False).all()
    if not hrs:
        return jsonify({'success': True, 'deleted_ids': []})

    now = get_ph_time()
    deleted_ids = []
    for hr in hrs:
        hr.is_deleted = True
        hr.deleted_at = now
        hr.deleted_by = current_user.id
        deleted_ids.append(hr.id)

    db.session.commit()
    return jsonify({'success': True, 'deleted_ids': deleted_ids})


@recruiter_bp.route('/undo-delete-hr/<int:hr_id>', methods=['POST'])
@login_required
def undo_delete_hr(hr_id):
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    hr = User.query.filter_by(id=hr_id, role='hr', created_by=current_user.id, is_deleted=True).first()
    if not hr:
        return jsonify({'success': False, 'error': 'HR account not found or already committed'}), 404

    hr.is_deleted = False
    hr.deleted_at = None
    hr.deleted_by = None
    db.session.commit()

    return jsonify({'success': True})


@recruiter_bp.route('/undo-delete-all-hr', methods=['POST'])
@login_required
def undo_delete_all_hr():
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    ids  = data.get('ids', [])

    if not ids:
        return jsonify({'success': False, 'error': 'No IDs provided'}), 400

    restored = 0
    for hr_id in ids:
        hr = User.query.filter_by(id=hr_id, role='hr', created_by=current_user.id, is_deleted=True).first()
        if hr:
            hr.is_deleted = False
            hr.deleted_at = None
            hr.deleted_by = None
            restored += 1

    db.session.commit()
    return jsonify({'success': True, 'restored': restored})


@recruiter_bp.route('/commit-delete-hr/<int:hr_id>', methods=['POST'])
@login_required
def commit_delete_hr(hr_id):
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    hr = User.query.filter_by(id=hr_id, role='hr', created_by=current_user.id, is_deleted=True).first()
    if not hr:
        return jsonify({'success': True, 'skipped': True})

    # NOTE: intentionally lighter than services.deletion_service — a
    # soft-deleted-but-not-yet-committed HR account has no jobs,
    # applications, or messages of its own, only feedback rows and a
    # profile. If that assumption ever changes, switch this to
    # delete_user_completely(hr.id) from services/deletion_service.py.
    from sqlalchemy import text
    try:
        db.session.execute(text("DELETE FROM hr_feedback WHERE hr_id = :hr_id"), {"hr_id": hr.id})
        db.session.flush()

        if hr.hr_profile:
            db.session.delete(hr.hr_profile)
            db.session.flush()

        db.session.delete(hr)
        db.session.commit()
        return jsonify({'success': True})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@recruiter_bp.route('/commit-delete-all-hr', methods=['POST'])
@login_required
def commit_delete_all_hr():
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    data = request.get_json(silent=True) or {}
    ids  = data.get('ids', [])

    if not ids:
        return jsonify({'success': True, 'message': 'Nothing to commit'})

    try:
        from sqlalchemy import text

        for hr_id in ids:
            hr = User.query.filter_by(id=hr_id, role='hr', created_by=current_user.id, is_deleted=True).first()
            if not hr:
                continue

            db.session.execute(text("DELETE FROM hr_feedback WHERE hr_id = :hr_id"), {"hr_id": hr_id})
            db.session.flush()

            if hr.hr_profile:
                db.session.delete(hr.hr_profile)
                db.session.flush()

            db.session.delete(hr)
            db.session.flush()

        db.session.commit()
        return jsonify({'success': True})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500