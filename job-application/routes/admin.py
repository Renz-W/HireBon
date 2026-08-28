from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app, session, make_response
from flask_login import login_required, current_user, login_user
from werkzeug.security import check_password_hash
from models import AdminNotification, db, User, ApplicantProfile
from routes.auth import send_verification_email
from flask import jsonify
from datetime import datetime, timedelta
from models import AdminNotification, db, User, ApplicantProfile, get_ph_time

# ── FIX #1 / #3: shared deletion logic instead of ~150 lines of
# inline nested helper functions duplicated across admin.py,
# settings.py, and recruiter.py. See services/deletion_service.py.
from services.deletion_service import delete_user_completely

admin_bp = Blueprint('admin', __name__, url_prefix="/admin")


# ==============================
# Secret Admin Login Page
# ==============================
@admin_bp.route('/login/<token>', methods=['GET', 'POST'], endpoint='admin_login')
def admin_login(token):

    if token != current_app.config.get("ADMIN_TOKEN"):
        flash("Invalid or expired admin link.", "danger")
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        email    = request.form.get("email")
        password = request.form.get("password")

        admin = User.query.filter_by(email=email, role="admin").first()

        if not admin or not check_password_hash(admin.password, password):
            flash("Invalid admin credentials.", "danger")
            return render_template("admin/login.html", token=token)

        login_user(admin)
        flash("Logged in as admin.", "success")
        return redirect(url_for('admin.dashboard'))

    return render_template("admin/login.html", token=token)


# ==============================
# Admin Dashboard
# ==============================
@admin_bp.route('/dashboard')
@login_required
def dashboard():

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    from models import RecruiterProfile, Application, Job
    from collections import defaultdict

    pending_recruiter_ids = db.session.query(RecruiterProfile.user_id).filter_by(
        submitted_for_review=True
    ).subquery()

    pending_recruiters = User.query.filter(
        User.role == 'recruiter',
        User.verification_status == 'Pending',
        User.id.in_(pending_recruiter_ids)
    ).all()

    banned_users     = User.query.filter_by(is_banned=True).all()
    total_applicants = User.query.filter_by(role='applicant').count()
    total_recruiters = User.query.filter_by(role='recruiter').count()
    total_hr         = User.query.filter_by(role='hr').count()
    total_jobs       = Job.query.count()

    # ==============================
    # Chart Data: Last 7 Days
    # ==============================
    today = datetime.now().date()
    days_data = {}

    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_key = day.strftime('%b %d')
        days_data[day_key] = {
            'applications': 0,
            'signups': 0,
            'jobs': 0
        }

    applications = Application.query.filter(
        Application.created_at >= datetime.combine(today - timedelta(days=7), datetime.min.time())
    ).all()

    for app in applications:
        day_key = app.created_at.strftime('%b %d')
        if day_key in days_data:
            days_data[day_key]['applications'] += 1

    users = User.query.filter(
        User.role.in_(['applicant', 'recruiter', 'hr']),
        User.created_at >= datetime.combine(today - timedelta(days=7), datetime.min.time())
    ).all()

    for user in users:
        if user.created_at:
            day_key = user.created_at.strftime('%b %d')
            if day_key in days_data:
                days_data[day_key]['signups'] += 1

    jobs = Job.query.filter(
        Job.created_at >= datetime.combine(today - timedelta(days=7), datetime.min.time())
    ).all()

    for job in jobs:
        day_key = job.created_at.strftime('%b %d')
        if day_key in days_data:
            days_data[day_key]['jobs'] += 1

    chart_labels       = list(days_data.keys())
    chart_applications = [days_data[label]['applications'] for label in chart_labels]
    chart_signups      = [days_data[label]['signups'] for label in chart_labels]
    chart_jobs         = [days_data[label]['jobs'] for label in chart_labels]

    response = make_response(render_template(
        'admin/dashboard.html',
        pending_recruiters=pending_recruiters,
        banned_users=banned_users,
        total_applicants=total_applicants,
        total_recruiters=total_recruiters,
        total_hr=total_hr,
        total_jobs=total_jobs,
        chart_labels=chart_labels,
        chart_applications=chart_applications,
        chart_signups=chart_signups,
        chart_jobs=chart_jobs,
    ))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response

# ==============================
# All Users Page
# ==============================
@admin_bp.route('/users')
@login_required
def all_users():

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    role_filter = request.args.get('role', 'all')

    query = User.query.filter(
        User.role != 'admin',
        User.is_banned == False,
    )
    if role_filter != 'all':
        query = query.filter_by(role=role_filter)

    users = query.order_by(User.id.desc()).all()

    return render_template(
        'admin/all_users.html',
        users=users,
        role_filter=role_filter,
    )


# ==============================
# Banned Users Page
# ==============================
@admin_bp.route('/banned')
@login_required
def banned_users():

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    banned = User.query.filter_by(is_banned=True).all()

    return render_template(
        'admin/banned_users.html',
        banned_users=banned,
    )


# ==============================
# Rejected Recruiters Page
# ==============================
@admin_bp.route('/scammers')
@login_required
def scammers():

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    rejected_recruiters = User.query.filter_by(
        role="recruiter",
        verification_status="Rejected"
    ).all()

    return render_template(
        "admin/rejected_recruiters.html",
        recruiters=rejected_recruiters
    )


# ==============================
# Recruiter Review Page
# ==============================
@admin_bp.route('/review/<int:user_id>')
@login_required
def review_recruiter(user_id):

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    recruiter = db.session.get(User, user_id)

    if not recruiter or recruiter.role != 'recruiter':
        flash("Recruiter not found!", "danger")
        return redirect(url_for('admin.dashboard'))

    profile = recruiter.recruiter_profile

    return render_template(
        'admin/review_recruiter.html',
        recruiter=recruiter,
        profile=profile
    )


# ==============================
# Verify Recruiter
# ==============================
@admin_bp.route('/verify/<int:user_id>', methods=['POST'])
@login_required
def verify(user_id):

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('admin.dashboard'))

    user = db.session.get(User, user_id)

    if not user:
        flash("User not found!", "danger")
        return redirect(url_for('admin.dashboard'))

    if user.role != 'recruiter':
        flash("Only recruiter accounts require verification!", "danger")
        return redirect(url_for('admin.dashboard'))

    user.verification_status  = "Approved"
    user.verification_remarks = None
    user.is_verified          = True

    from models import RecruiterNotification
    notif = RecruiterNotification(
        recruiter_id=user.id,
        type='account_verified',
        message='🎉 Your recruiter account has been <strong>verified and approved</strong> by the admin. You can now post jobs and manage HR accounts.',
    )
    db.session.add(notif)
    db.session.commit()

    push_admin_notif(
        'account_approved',
        f'{user.role.capitalize()} account <strong>{user.username}</strong> was approved',
        user_id=user.id
    )

    send_verification_email(user)

    flash(f"{user.username} has been verified successfully!", "success")
    return redirect(url_for('admin.dashboard'))


# ==============================
# Reject Recruiter
# ==============================
@admin_bp.route('/reject/<int:user_id>', methods=['POST'])
@login_required
def reject(user_id):

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    user = db.session.get(User, user_id)

    if not user:
        flash("User not found!", "danger")
        return redirect(url_for('admin.dashboard'))

    if user.role != 'recruiter':
        flash("Only recruiter accounts can be rejected!", "danger")
        return redirect(url_for('admin.dashboard'))

    remarks = request.form.get("remarks") or "Account rejected due to suspicious or invalid information."

    user.verification_status  = "Rejected"
    user.verification_remarks = remarks
    user.is_verified          = False
    db.session.commit()

    from routes.auth import send_verification_email
    push_admin_notif(
        'account_rejected',
        f'{user.role.capitalize()} account <strong>{user.username}</strong> was rejected',
        user_id=user.id
    )

    flash(f"{user.username} has been rejected.", "warning")
    return redirect(url_for('admin.dashboard'))


# ==============================
# BAN User (any role)
# ==============================
@admin_bp.route('/ban/<int:user_id>', methods=['POST'])
@login_required
def ban_user(user_id):
    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    user = db.session.get(User, user_id)
    if not user or user.role == 'admin':
        flash("User not found or cannot ban admin!", "danger")
        return redirect(url_for('admin.dashboard'))

    reason   = request.form.get("ban_reason") or "Banned due to suspicious activity."
    duration = request.form.get("ban_duration", "permanent")

    if duration == 'permanent':
        ban_until = None
    else:
        try:
            ban_until = get_ph_time() + timedelta(days=int(duration))
        except ValueError:
            ban_until = None

    user.is_banned  = True
    user.ban_reason = reason
    user.banned_at  = get_ph_time()
    user.ban_until  = ban_until
    db.session.commit()

    flash(f"{user.username} has been banned.", "warning")
    next_url = request.form.get("next") or url_for('admin.dashboard')
    return redirect(next_url)


# ==============================
# UNBAN User
# ==============================
@admin_bp.route('/unban/<int:user_id>', methods=['POST'])
@login_required
def unban_user(user_id):

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    user = db.session.get(User, user_id)

    if not user:
        flash("User not found!", "danger")
        return redirect(url_for('admin.banned_users'))

    user.is_banned  = False
    user.ban_reason = None
    user.banned_at  = None
    user.ban_until  = None
    db.session.commit()

    flash(f"{user.username} has been unbanned.", "success")
    return redirect(url_for('admin.banned_users'))


# ==============================================================
# DELETE Banned User permanently
#
# ── FIX #1 / #3 ──────────────────────────────────────────────
# BEFORE: this route contained ~230 lines defining
#   _delete_job_image_files(), _delete_job_rows(), and an inline
#   delete_user() body with _delete_upload(), _delete_user_files(),
#   _delete_user_data() nested inside it — duplicated almost
#   verbatim in routes/settings.py and partially in
#   routes/recruiter.py's force_delete_job().
#
# AFTER: all of that logic now lives once in
#   services/deletion_service.py. This route is now just the
#   HTTP-facing wrapper: check permissions, call the service,
#   flash the result. Changing the FK-cleanup order now only
#   requires editing one file instead of three.
# ==============================================================
@admin_bp.route('/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    ok, error = delete_user_completely(user_id)

    if ok:
        push_admin_notif('user_deleted', 'User account permanently deleted by admin.')
        flash("User and all associated data have been permanently deleted.", "warning")
    else:
        flash(f"Deletion failed: {error}", "danger")

    return redirect(url_for('admin.all_users'))


# ==============================
# Restore Rejected Recruiter
# ==============================
@admin_bp.route('/restore/<int:user_id>', methods=['POST'])
@login_required
def restore(user_id):

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    user = db.session.get(User, user_id)

    if not user or user.role != "recruiter":
        flash("Recruiter not found!", "danger")
        return redirect(url_for('admin.scammers'))

    user.verification_status  = "Pending"
    user.verification_remarks = None
    user.is_verified          = False
    db.session.commit()

    flash(f"{user.username} has been restored to pending verification.", "success")
    return redirect(url_for('admin.scammers'))


# ==============================
# Restore Rejected Applicant
# ==============================
@admin_bp.route('/restore-applicant/<int:user_id>', methods=['POST'])
@login_required
def restore_applicant(user_id):

    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    user = db.session.get(User, user_id)

    if not user or user.role != "applicant":
        flash("Applicant not found!", "danger")
        return redirect(url_for('admin.rejected_applicants'))

    user.verification_status  = "Pending"
    user.verification_remarks = None
    user.is_verified          = False
    db.session.commit()

    flash(f"{user.username} has been restored to pending verification.", "success")
    return redirect(url_for('admin.rejected_applicants'))


# ==============================
# Reports Tab
# ==============================
@admin_bp.route('/reports')
@login_required
def reports():
    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    from models import UserReport
    status_filter = request.args.get('status', 'pending')

    query = UserReport.query
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)

    all_reports     = query.order_by(UserReport.created_at.desc()).all()
    pending_count   = UserReport.query.filter_by(status='pending').count()
    reviewed_count  = UserReport.query.filter_by(status='reviewed').count()
    dismissed_count = UserReport.query.filter_by(status='dismissed').count()

    return render_template(
        'admin/reports.html',
        reports=all_reports,
        status_filter=status_filter,
        pending_count=pending_count,
        reviewed_count=reviewed_count,
        dismissed_count=dismissed_count,
    )

# ==============================
# Helper: delete evidence files for a report
# ==============================
def _delete_report_evidence(report):
    """Remove uploaded evidence files from disk for a UserReport row."""
    import json, os
    if not report.evidence_files:
        return
    try:
        files = json.loads(report.evidence_files)
        if not isinstance(files, list):
            files = [report.evidence_files]
    except (json.JSONDecodeError, TypeError):
        files = [report.evidence_files]
    folder = os.path.join(current_app.root_path, 'static', 'uploads', 'report_evidence')
    for filename in files:
        if not filename:
            continue
        path = os.path.join(folder, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                # FIX #5: use the app logger instead of print(), so this
                # is visible wherever the app's logging/error tracking
                # is actually configured to send output.
                current_app.logger.warning(f'[DELETE FILE] report evidence: {path}: {e}')

# ==============================
# Dismiss a Report
# ==============================
@admin_bp.route('/reports/<int:report_id>/dismiss', methods=['POST'])
@login_required
def dismiss_report(report_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    from models import UserReport
    report = db.session.get(UserReport, report_id)
    if not report:
        flash("Report not found.", "danger")
        return redirect(url_for('admin.reports'))

    admin_notes        = request.form.get('admin_notes', '')
    report.status      = 'dismissed'
    report.admin_notes = admin_notes
    report.reviewed_by = current_user.id
    report.reviewed_at = get_ph_time()
    _delete_report_evidence(report)
    db.session.commit()

    flash("Report dismissed.", "success")
    return redirect(url_for('admin.reports'))


# ==============================
# Ban User FROM a Report (with duration)
# ==============================
@admin_bp.route('/reports/<int:report_id>/ban', methods=['POST'])
@login_required
def ban_from_report(report_id):
    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    from models import UserReport

    report = db.session.get(UserReport, report_id)
    if not report:
        flash("Report not found.", "danger")
        return redirect(url_for('admin.reports'))

    user = db.session.get(User, report.reported_id)
    if not user or user.role == 'admin':
        flash("Cannot ban this user.", "danger")
        return redirect(url_for('admin.reports'))

    reason      = request.form.get('ban_reason') or f'Banned following a report: {report.reason}'
    admin_notes = request.form.get('admin_notes', '')
    duration    = request.form.get('ban_duration', 'permanent')

    if duration == 'permanent':
        ban_until = None
    else:
        try:
            days      = int(duration)
            ban_until = get_ph_time() + timedelta(days=days)
        except ValueError:
            ban_until = None

    user.is_banned  = True
    user.ban_reason = reason
    user.banned_at  = get_ph_time()
    user.ban_until  = ban_until

    report.status      = 'reviewed'
    report.admin_notes = admin_notes
    report.reviewed_by = current_user.id
    report.reviewed_at = get_ph_time()
    _delete_report_evidence(report)

    UserReport.query.filter_by(
        reported_id=user.id,
        status='pending'
    ).filter(UserReport.id != report_id).update({
        'status':      'reviewed',
        'admin_notes': f'Auto-closed: admin took action on report #{report_id}',
        'reviewed_by': current_user.id,
        'reviewed_at': get_ph_time(),
    })

    db.session.commit()

    push_admin_notif(
        'account_banned',
        f'User <strong>{user.username}</strong> was banned'
        + (f' for {duration} days' if duration != 'permanent' else ' permanently')
        + f' following report #{report_id}',
        user_id=user.id
    )

    duration_str = 'permanently' if duration == 'permanent' else f'for {duration} days'
    flash(f"{user.username} has been banned {duration_str}.", "warning")
    return redirect(url_for('admin.reports'))


# ==============================
# Helper: push admin notification
# ==============================
def push_admin_notif(notif_type, message, user_id=None):
    notif = AdminNotification(type=notif_type, message=message, user_id=user_id)
    db.session.add(notif)
    db.session.commit()


# ==============================
# API: Get all admin notifications
# ==============================
@admin_bp.route('/notifications')
@login_required
def admin_notifications():
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    notifs = AdminNotification.query.order_by(
        AdminNotification.created_at.desc()
    ).limit(60).all()
    unread = AdminNotification.query.filter_by(is_read=False).count()

    def serialize(n):
        d = n.to_dict()
        if n.user_id:
            u = db.session.get(User, n.user_id)
            d['sender_role'] = u.role if u else None
        else:
            d['sender_role'] = None
        return d

    return jsonify({
        'notifications': [serialize(n) for n in notifs],
        'unread_count':  unread,
    })


# ==============================
# API: Mark notifications as read
# ==============================
@admin_bp.route('/notifications/mark-read', methods=['POST'])
@login_required
def admin_notifications_mark_read():
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json(silent=True) or {}
    notif_id = data.get('id')
    if notif_id:
        AdminNotification.query.filter_by(id=notif_id).update({'is_read': True})
    else:
        AdminNotification.query.filter_by(is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'ok': True})


# ==============================
# API: Clear all notifications
# ==============================
@admin_bp.route('/notifications/clear-all', methods=['POST'])
@login_required
def admin_notifications_clear():
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    AdminNotification.query.delete()
    db.session.commit()
    return jsonify({'ok': True})


# ==============================
# Page: Notification History
# ==============================
@admin_bp.route('/notification-history')
@login_required
def admin_notification_history():
    if current_user.role != 'admin':
        return redirect(url_for('admin.dashboard'))
    notifs = AdminNotification.query.order_by(
        AdminNotification.created_at.desc()
    ).all()
    AdminNotification.query.filter_by(is_read=False).update({'is_read': True})
    db.session.commit()
    user_roles = {}
    for n in notifs:
        if n.user_id and n.user_id not in user_roles:
            u = db.session.get(User, n.user_id)
            user_roles[n.user_id] = u.role if u else None
    return render_template('admin/notification_history.html',
                           notifications=notifs,
                           user_roles=user_roles)


# ==============================
# API: Pending reports count
# ==============================
@admin_bp.route('/reports/pending-count')
@login_required
def reports_pending_count():
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    from models import UserReport
    count = UserReport.query.filter_by(status='pending').count()
    return jsonify({'count': count})


# ==============================
# All Jobs Page
# ==============================
@admin_bp.route('/jobs')
@login_required
def all_jobs():
    if current_user.role != 'admin':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    from models import Job
    status_filter = request.args.get('status', 'all')

    query = Job.query
    if status_filter == 'active':
        query = query.filter_by(is_taken_down=False)
    elif status_filter == 'takendown':
        query = query.filter_by(is_taken_down=True)

    jobs            = query.order_by(Job.created_at.desc()).all()
    total_jobs      = Job.query.count()
    active_count    = Job.query.filter_by(is_taken_down=False).count()
    takendown_count = Job.query.filter_by(is_taken_down=True).count()

    return render_template(
        'admin/all_jobs.html',
        jobs=jobs,
        status_filter=status_filter,
        total_jobs=total_jobs,
        active_count=active_count,
        takendown_count=takendown_count,
    )


# ==============================
# API: Taken-down jobs count (for dashboard badge)
# ==============================
@admin_bp.route('/jobs/takendown-count')
@login_required
def jobs_takendown_count():
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    from models import Job
    count = Job.query.filter_by(is_taken_down=True).count()
    return jsonify({'count': count})


# ==============================
# Job Moderation: Takedown
# ==============================
@admin_bp.route('/job/<int:job_id>/takedown', methods=['POST'])
@login_required
def takedown_job(job_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    from models import (Job, RecruiterNotification, ApplicantNotification,
                        HRNotification, Application, get_ph_time)

    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    data   = request.get_json() or {}
    reason = (data.get('reason') or '').strip() or 'Violated platform guidelines.'
    days   = data.get('days')

    try:
        job.is_taken_down   = True
        job.takedown_reason = reason
        job.taken_down_at   = get_ph_time()
        job.takedown_until  = (
            get_ph_time() + timedelta(days=int(days))
            if days and str(days).isdigit() and int(days) > 0
            else None
        )

        # ── 1. Notify recruiter ──
        db.session.add(RecruiterNotification(
            recruiter_id = job.company_id,
            type         = 'job_takedown',
            message      = (
                f'Your job posting <strong>"{job.title}"</strong> has been taken down by an admin. '
                f'Reason: {reason}. '
                + (f'It will be restored after {days} day(s) if resolved.'
                   if days else 'Contact support to resolve this.')
            ),
            job_id = job.id,
        ))

        # ── 2. Active applicants ──
        try:
            active_apps = Application.query.filter(
                Application.job_id == job_id,
                Application.status.in_(['Pending', 'Interview', 'Waitlisted', 'Under Review',
                                        'pending', 'interview', 'waitlisted', 'under review'])
            ).all()
            for app in active_apps:
                app.status = 'Job Removed'
                db.session.add(ApplicantNotification(
                    applicant_id = app.applicant_id,
                    type         = 'job_update',
                    job_id       = job.id,
                    message      = (
                        f'⚠️ The job <strong>"{job.title}"</strong> you applied to has been '
                        f'removed by an admin for policy violations. '
                        f'Your application is now marked <strong>Job Removed</strong>. '
                        f'We recommend caution if you have been in contact with this employer outside the platform.'
                    ),
                ))

            # ── Employed applicants — special warning ──
            employed_apps = Application.query.filter(
                Application.job_id == job_id,
                Application.status.in_(['employed', 'Employed'])
            ).all()
            for app in employed_apps:
                db.session.add(ApplicantNotification(
                    applicant_id = app.applicant_id,
                    type         = 'job_update',
                    job_id       = job.id,
                    message      = (
                        f'⚠️ The job posting <strong>"{job.title}"</strong> you are currently employed under '
                        f'has been taken down from our platform for policy violations. '
                        f'<strong>Your employment contract is between you and the recruiter directly — '
                        f'please consult with your recruiter regarding your employment status.</strong> '
                        f'We are a job matching platform and taking down a posting does not automatically terminate your employment.'
                    ),
                ))
        except Exception as e:
            current_app.logger.warning(f'[TAKEDOWN] applicant notify error: {e}')

        # ── 3. Saved jobs ──
        try:
            from models import SavedJob
            saved = SavedJob.query.filter_by(job_id=job_id).all()
            for s in saved:
                db.session.add(ApplicantNotification(
                    applicant_id = s.applicant_id,
                    type         = 'job_update',
                    job_id       = job.id,
                    message      = (
                        f'A job you saved — <strong>"{job.title}"</strong> — '
                        f'has been removed from the platform and is no longer available.'
                    ),
                ))
                db.session.delete(s)
        except Exception as e:
            current_app.logger.warning(f'[TAKEDOWN] saved job error: {e}')

        # ── 4. HR team members ──
        try:
            from models import JobTeamMember
            hr_members = JobTeamMember.query.filter_by(job_id=job_id).all()
            for member in hr_members:
                db.session.add(HRNotification(
                    hr_id   = member.hr_id,
                    type    = 'job_update',
                    job_id  = job.id,
                    message = (
                        f'⚠️ The job <strong>"{job.title}"</strong> you were assigned to '
                        f'has been taken down by an admin. Reason: {reason}.'
                    ),
                ))
        except Exception as e:
            current_app.logger.warning(f'[TAKEDOWN] HR notify error: {e}')

        db.session.commit()

        push_admin_notif(
            'job_takedown',
            f'Job <strong>"{job.title}"</strong> taken down by admin.',
            user_id=job.company_id,
        )

        return jsonify({'ok': True, 'message': f'Job "{job.title}" taken down successfully.'})

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f'[TAKEDOWN ERROR] job_id={job_id}')
        return jsonify({'error': str(e)}), 500


# ==============================
# Job Moderation: Restore
# ==============================
@admin_bp.route('/job/<int:job_id>/restore', methods=['POST'])
@login_required
def restore_job(job_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    from models import Job, RecruiterNotification, ApplicantNotification, Application

    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    job.is_taken_down   = False
    job.takedown_reason = None
    job.takedown_until  = None
    job.taken_down_at   = None

    db.session.add(RecruiterNotification(
        recruiter_id = job.company_id,
        type         = 'job_restored',
        message      = f'Your job posting <strong>"{job.title}"</strong> has been restored and is now visible again.',
        job_id       = job.id,
    ))

    removed_apps = Application.query.filter_by(
        job_id=job_id, status='Job Removed'
    ).all()

    for app in removed_apps:
        app.status = 'Pending'
        db.session.add(ApplicantNotification(
            applicant_id = app.applicant_id,
            type         = 'job_update',
            job_id       = job.id,
            message      = (
                f'Good news! The job posting <strong>"{job.title}"</strong> '
                f'has been restored. Your application status has been reset to '
                f'<strong>Pending</strong>.'
            ),
        ))

    db.session.commit()

    return jsonify({'ok': True, 'message': f'Job "{job.title}" has been restored.'})


# ==============================================================
# Job Moderation: Permanent Delete
#
# ── FIX #1 / #3 ──────────────────────────────────────────────
# BEFORE: this route rebuilt job-deletion logic AGAIN (a third
#   copy, alongside admin.delete_user()'s internal job-deletion
#   loop and recruiter.py's force_delete_job()).
# AFTER: delegates to the same delete_job_completely() used
#   everywhere else. The resignation-letter cleanup and the
#   RecruiterNotification-after-deletion ordering are kept here
#   since those are specific to "admin takes down a job" (the
#   notification text differs from a recruiter deleting their own
#   job), not generic deletion mechanics.
# ==============================================================
@admin_bp.route('/job/<int:job_id>/admin-delete', methods=['POST'])
@login_required
def admin_delete_job(job_id):
    if current_user.role != 'admin':
        return jsonify({'error': 'Forbidden'}), 403

    from models import Job, RecruiterNotification
    from services.deletion_service import delete_job_completely

    job = db.session.get(Job, job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    title      = job.title
    company_id = job.company_id

    ok, error = delete_job_completely(job_id)
    if not ok:
        return jsonify({'error': error}), 500

    # Notification added AFTER the job is fully gone, with job_id=None
    # so there is no FK reference to the now-deleted job.
    db.session.add(RecruiterNotification(
        recruiter_id = company_id,
        type         = 'job_deleted',
        job_id       = None,
        message      = (
            f'Your job posting <strong>"{title}"</strong> has been permanently '
            f'removed by an admin for violating platform guidelines.'
        ),
    ))
    db.session.commit()

    push_admin_notif(
        'job_deleted',
        f'Job <strong>"{title}"</strong> permanently deleted by admin.',
        user_id=company_id,
    )

    return jsonify({'ok': True, 'message': f'Job "{title}" permanently deleted.'})