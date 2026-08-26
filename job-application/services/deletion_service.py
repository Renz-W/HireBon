# ================================================================
# services/deletion_service.py
#
# FIX #1 — "Duplicate Code": the exact same "delete every DB row
#   that references this user/job, in strict FK order" logic used
#   to live in THREE places:
#     - routes/admin.py       -> delete_user()
#     - routes/settings.py    -> delete_account()
#     - routes/recruiter.py   -> force_delete_job() / _delete_job_rows()
#   Proof this was already dangerous: admin.py has an inline comment
#   admitting a past bug — "_delete_job_image_files() incorrectly
#   referenced `target_uid` (undefined in that scope), causing a
#   NameError crash" — which was patched in ONE of the three copies.
#   Nothing guaranteed the other copies got the same fix.
#
# FIX #3 — "Long Function": each copy was a single 150-250 line
#   route handler with helper functions defined *inline*. Here the
#   same logic is broken into small, independently testable
#   functions, each responsible for one FK-chain concern.
#
# USAGE:
#   from services.deletion_service import delete_user_completely, delete_job_completely
#
#   ok, error = delete_user_completely(user_id)
#   if not ok:
#       flash(error, "danger")
# ================================================================

import os
import json
from sqlalchemy import text
from flask import current_app
from models import db, User, Job


# ----------------------------------------------------------------
# Small helpers (previously redefined inline in every route)
# ----------------------------------------------------------------

def _delete_upload_file(subfolder, filename):
    """Remove a single uploaded file from disk, if it exists locally
    (skip anything stored as an external http URL, e.g. Google profile pics)."""
    if not filename or filename.startswith('http'):
        return
    path = os.path.join(current_app.root_path, 'static', 'uploads', subfolder, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            # FIX #5 (error handling) — log instead of silently swallowing.
            current_app.logger.warning(f"[deletion_service] could not remove {path}: {e}")


def _run(sql, **params):
    """Shorthand for execute + flush, used by every cascade step below."""
    db.session.execute(text(sql), params)
    db.session.flush()


# ----------------------------------------------------------------
# FILE CLEANUP — one function per profile "family" instead of one
# giant _delete_user_files() containing all of them inline.
# ----------------------------------------------------------------

def _delete_profile_picture(uid):
    u = db.session.get(User, uid)
    if u:
        _delete_upload_file('profile_pictures', u.profile_picture)


def _delete_applicant_files(uid):
    row = db.session.execute(text(
        "SELECT resume_file, portfolio_file FROM applicant_profile WHERE user_id = :u"
    ), {"u": uid}).fetchone()
    if row:
        _delete_upload_file('resumes', row[0])
        _delete_upload_file('portfolios', row[1])

    cert_rows = db.session.execute(text("""
        SELECT wec.file_path
        FROM work_experience_certificate wec
        JOIN work_experience we ON we.id = wec.experience_id
        JOIN applicant_profile ap ON ap.id = we.profile_id
        WHERE ap.user_id = :u
    """), {"u": uid}).fetchall()
    for cert in cert_rows:
        _delete_upload_file('work_certificates', cert[0])

    # Apply-time resume (application.resume) is separate from the
    # profile resume above — the old admin.py copy had this fix
    # documented, but settings.py needed the same comment repeated.
    # Now it only needs to exist once, here.
    app_resume_rows = db.session.execute(text("""
        SELECT resume FROM application
        WHERE applicant_id = :u AND resume IS NOT NULL AND resume != ''
    """), {"u": uid}).fetchall()
    for row in app_resume_rows:
        _delete_upload_file('resumes', row[0])

    resign_rows = db.session.execute(text("""
        SELECT letter_file FROM resignation_request
        WHERE applicant_id = :u
           OR employee_id IN (SELECT id FROM employee WHERE user_id = :u)
    """), {"u": uid}).fetchall()
    for row in resign_rows:
        _delete_upload_file('resignation_letters', row[0])


def _delete_recruiter_files(uid):
    row = db.session.execute(text(
        "SELECT company_logo, company_proof, portfolio_file FROM recruiter_profile WHERE user_id = :u"
    ), {"u": uid}).fetchone()
    if row:
        _delete_upload_file('company_logos', row[0])
        _delete_upload_file('company_proofs', row[1])
        _delete_upload_file('portfolios', row[2])


def _delete_hr_files(uid):
    row = db.session.execute(text(
        "SELECT portfolio_file FROM hr_profile WHERE user_id = :u"
    ), {"u": uid}).fetchone()
    if row and row[0]:
        _delete_upload_file('portfolios', row[0])


def _delete_report_evidence_files(uid):
    rows = db.session.execute(text(
        "SELECT evidence_files FROM user_report WHERE reporter_id = :u OR reported_id = :u"
    ), {"u": uid}).fetchall()
    for r in rows:
        if not r[0]:
            continue
        try:
            files = json.loads(r[0])
            files = files if isinstance(files, list) else [r[0]]
        except (json.JSONDecodeError, TypeError):
            files = [r[0]]
        for f in files:
            _delete_upload_file('report_evidence', f)


def _delete_employment_submission_files(uid):
    rows = db.session.execute(text("""
        SELECT es.file_path
        FROM employment_submission es
        JOIN application a ON a.id = es.application_id
        WHERE a.applicant_id = :u
    """), {"u": uid}).fetchall()
    for row in rows:
        _delete_upload_file('employment_submissions', row[0])


def delete_user_files(uid):
    """Public entry point: remove every uploaded file that belongs to a user,
    across all profile types. Must run BEFORE delete_user_rows(), since it
    reads file-path columns that delete_user_rows() will wipe."""
    _delete_profile_picture(uid)
    _delete_applicant_files(uid)
    _delete_recruiter_files(uid)
    _delete_hr_files(uid)
    _delete_report_evidence_files(uid)
    _delete_employment_submission_files(uid)


def delete_job_files(job_id):
    """File cleanup for a job: gallery images, cover photo, employment
    submissions tied to the job's requirements, resignation letters."""
    img_rows = db.session.execute(text(
        "SELECT image_path FROM job_image WHERE job_id = :j"
    ), {"j": job_id}).fetchall()
    for r in img_rows:
        _delete_upload_file('job_posters', r[0])

    cover = db.session.execute(text(
        "SELECT cover_photo FROM job WHERE id = :j"
    ), {"j": job_id}).fetchone()
    if cover and cover[0]:
        _delete_upload_file('job_covers', cover[0])

    sub_rows = db.session.execute(text("""
        SELECT es.file_path FROM employment_submission es
        JOIN employment_requirement er ON er.id = es.requirement_id
        WHERE er.job_id = :j
    """), {"j": job_id}).fetchall()
    for row in sub_rows:
        _delete_upload_file('employment_submissions', row[0])

    resign_rows = db.session.execute(text(
        "SELECT letter_file FROM resignation_request WHERE job_id = :j"
    ), {"j": job_id}).fetchall()
    for row in resign_rows:
        _delete_upload_file('resignation_letters', row[0])


# ----------------------------------------------------------------
# DB ROW CLEANUP — broken into named "chain" functions instead of
# one 25-step function. Order between calls still matters (FK
# order), but each step is now readable and independently testable.
# ----------------------------------------------------------------

def _unlink_message_self_references(uid):
    _run("""UPDATE message SET reply_to_id = NULL
            WHERE reply_to_id IN (
                SELECT id FROM (
                    SELECT id FROM message WHERE sender_id = :u OR receiver_id = :u
                ) AS _m
            )""", u=uid)


def _delete_messaging_data(uid):
    _unlink_message_self_references(uid)
    _run("""DELETE FROM message_reaction
            WHERE user_id = :u
               OR message_id IN (
                   SELECT id FROM (
                       SELECT id FROM message WHERE sender_id = :u OR receiver_id = :u
                   ) AS _m2
               )""", u=uid)
    _run("DELETE FROM message WHERE sender_id = :u OR receiver_id = :u", u=uid)


def _delete_social_graph_data(uid):
    _run("DELETE FROM follow_request WHERE sender_id = :u OR receiver_id = :u", u=uid)
    _run("DELETE FROM follow WHERE follower_id = :u OR followed_id = :u", u=uid)
    _run("DELETE FROM user_block WHERE blocker_id = :u OR blocked_id = :u", u=uid)
    _run("UPDATE user_report SET reviewed_by = NULL WHERE reviewed_by = :u", u=uid)
    _run("DELETE FROM user_report WHERE reporter_id = :u OR reported_id = :u", u=uid)


def _delete_job_related_membership_data(uid):
    _run("DELETE FROM saved_job WHERE applicant_id = :u", u=uid)
    _run("DELETE FROM job_team_member WHERE hr_id = :u", u=uid)
    _run("DELETE FROM hr_feedback WHERE hr_id = :u", u=uid)


def _detach_employment_fk_references(uid):
    """NULL out FKs that point AT this user but whose owning row
    shouldn't be deleted (e.g. an employee record another recruiter
    manages, where this user was merely the one who confirmed it)."""
    _run("UPDATE employee SET confirmed_by = NULL WHERE confirmed_by = :u", u=uid)
    _run("UPDATE resignation_request SET reviewed_by = NULL WHERE reviewed_by = :u", u=uid)


def _delete_employment_data_for_applicant(uid):
    _run("DELETE FROM resignation_request WHERE applicant_id = :u", u=uid)
    _run("""DELETE FROM resignation_request
            WHERE employee_id IN (
                SELECT id FROM employee
                WHERE user_id = :u
                   OR application_id IN (SELECT id FROM application WHERE applicant_id = :u)
            )""", u=uid)
    _run("""DELETE FROM employee
            WHERE user_id = :u
               OR application_id IN (SELECT id FROM application WHERE applicant_id = :u)""", u=uid)
    _run("""DELETE FROM employment_onboarding
            WHERE application_id IN (SELECT id FROM application WHERE applicant_id = :u)""", u=uid)
    _run("""DELETE FROM employment_submission
            WHERE application_id IN (SELECT id FROM application WHERE applicant_id = :u)""", u=uid)
    _run("""DELETE FROM hr_feedback
            WHERE application_id IN (SELECT id FROM application WHERE applicant_id = :u)""", u=uid)


def _delete_notifications_for_user(uid):
    _run("""DELETE FROM applicant_notification
            WHERE applicant_id = :u OR sender_id = :u
               OR application_id IN (SELECT id FROM application WHERE applicant_id = :u)""", u=uid)
    _run("""DELETE FROM recruiter_notification
            WHERE recruiter_id = :u OR sender_id = :u
               OR application_id IN (SELECT id FROM application WHERE applicant_id = :u)""", u=uid)
    _run("""DELETE FROM hr_notification
            WHERE hr_id = :u OR sender_id = :u
               OR application_id IN (SELECT id FROM application WHERE applicant_id = :u)""", u=uid)
    _run("DELETE FROM admin_notifications WHERE user_id = :u", u=uid)


def _delete_applicant_profile_chain(uid):
    _run("""DELETE FROM work_experience_certificate
            WHERE experience_id IN (
                SELECT id FROM work_experience
                WHERE profile_id IN (SELECT id FROM applicant_profile WHERE user_id = :u)
            )""", u=uid)
    _run("""DELETE FROM work_experience
            WHERE profile_id IN (SELECT id FROM applicant_profile WHERE user_id = :u)""", u=uid)
    _run("""DELETE FROM applicant_education
            WHERE profile_id IN (SELECT id FROM applicant_profile WHERE user_id = :u)""", u=uid)
    _run("""DELETE FROM skill
            WHERE profile_id IN (SELECT id FROM applicant_profile WHERE user_id = :u)""", u=uid)
    _run("""DELETE FROM project
            WHERE profile_id IN (SELECT id FROM applicant_profile WHERE user_id = :u)""", u=uid)
    _run("""DELETE FROM certification
            WHERE profile_id IN (SELECT id FROM applicant_profile WHERE user_id = :u)""", u=uid)
    _run("DELETE FROM applicant_profile WHERE user_id = :u", u=uid)


def _delete_recruiter_profile_chain(uid):
    _run("""DELETE FROM recruiter_education
            WHERE profile_id IN (SELECT id FROM recruiter_profile WHERE user_id = :u)""", u=uid)
    _run("DELETE FROM recruiter_profile WHERE user_id = :u", u=uid)


def _delete_hr_profile_chain(uid):
    _run("""DELETE FROM hr_education
            WHERE profile_id IN (SELECT id FROM hr_profile WHERE user_id = :u)""", u=uid)
    _run("DELETE FROM hr_profile WHERE user_id = :u", u=uid)


def delete_user_rows(uid):
    """Delete every DB row that references this user, in FK-safe order.
    Does NOT delete jobs owned by a recruiter — call delete_job_rows()
    for each owned job first (see delete_user_completely below)."""
    _delete_messaging_data(uid)
    _delete_social_graph_data(uid)
    _delete_job_related_membership_data(uid)
    _detach_employment_fk_references(uid)
    _delete_employment_data_for_applicant(uid)
    _delete_notifications_for_user(uid)
    _run("DELETE FROM application WHERE applicant_id = :u", u=uid)
    _delete_applicant_profile_chain(uid)
    _delete_recruiter_profile_chain(uid)
    _delete_hr_profile_chain(uid)
    _run("DELETE FROM user_settings WHERE user_id = :u", u=uid)


def delete_job_rows(job_id):
    """Delete every DB row that references this job, in FK-safe order.
    Does NOT delete the job row itself — caller does that after."""
    _run("DELETE FROM resignation_request WHERE job_id = :j", j=job_id)
    _run("""DELETE FROM applicant_notification
            WHERE job_id = :j OR application_id IN (SELECT id FROM application WHERE job_id = :j)""", j=job_id)
    _run("""DELETE FROM recruiter_notification
            WHERE job_id = :j OR application_id IN (SELECT id FROM application WHERE job_id = :j)""", j=job_id)
    _run("""DELETE FROM hr_notification
            WHERE job_id = :j OR application_id IN (SELECT id FROM application WHERE job_id = :j)""", j=job_id)
    _run("""DELETE FROM resignation_request
            WHERE employee_id IN (
                SELECT id FROM employee
                WHERE application_id IN (SELECT id FROM application WHERE job_id = :j)
            )""", j=job_id)
    _run("""DELETE FROM employee
            WHERE job_id = :j
               OR application_id IN (SELECT id FROM application WHERE job_id = :j)""", j=job_id)
    _run("""DELETE FROM employment_onboarding
            WHERE application_id IN (SELECT id FROM application WHERE job_id = :j)""", j=job_id)
    _run("""DELETE FROM employment_submission
            WHERE application_id IN (SELECT id FROM application WHERE job_id = :j)
               OR requirement_id IN (SELECT id FROM employment_requirement WHERE job_id = :j)""", j=job_id)
    _run("""DELETE FROM hr_feedback
            WHERE application_id IN (SELECT id FROM application WHERE job_id = :j)""", j=job_id)
    _run("DELETE FROM employment_requirement WHERE job_id = :j", j=job_id)
    _run("DELETE FROM saved_job WHERE job_id = :j", j=job_id)
    _run("DELETE FROM job_team_member WHERE job_id = :j", j=job_id)
    _run("DELETE FROM application WHERE job_id = :j", j=job_id)
    _run("DELETE FROM job_image WHERE job_id = :j", j=job_id)


# ----------------------------------------------------------------
# PUBLIC ENTRY POINTS — these replace delete_user() in admin.py,
# delete_account() in settings.py, and force_delete_job()/delete_job()
# in recruiter.py. Each caller now just calls one function and
# handles the flash message / jsonify / logout for ITS OWN context;
# the actual deletion logic lives here exactly once.
# ----------------------------------------------------------------

def delete_job_completely(job_id):
    """Delete a job and everything that references it (files + DB rows).
    Returns (success: bool, error_message: str | None)."""
    job = db.session.get(Job, job_id)
    if not job:
        return False, "Job not found."

    try:
        delete_job_files(job_id)
        delete_job_rows(job_id)
        db.session.execute(text("DELETE FROM job WHERE id = :j"), {"j": job_id})
        db.session.commit()
        return True, None
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"[deletion_service] delete_job_completely({job_id}) failed")
        return False, str(e)


def delete_user_completely(user_id):
    """Delete a user (and, if a recruiter, their jobs + HR sub-accounts)
    along with every DB row and uploaded file that references them.
    Returns (success: bool, error_message: str | None)."""
    user = db.session.get(User, user_id)
    if not user or user.role == 'admin':
        return False, "User not found or cannot delete admin."

    try:
        # Recruiters own jobs and can have HR accounts under them —
        # both must be fully removed before the recruiter row itself.
        if user.role == 'recruiter':
            _detach_employment_fk_references(user_id)

            hr_rows = db.session.execute(text(
                "SELECT id FROM user WHERE created_by = :u AND role = 'hr'"
            ), {"u": user_id}).fetchall()
            hr_ids = [row[0] for row in hr_rows]

            job_rows = db.session.execute(text(
                "SELECT id FROM job WHERE company_id = :u"
            ), {"u": user_id}).fetchall()
            for (job_id,) in job_rows:
                delete_job_files(job_id)
                delete_job_rows(job_id)
                db.session.execute(text("DELETE FROM job WHERE id = :j"), {"j": job_id})
                db.session.flush()

            _run("DELETE FROM recruiter_notification WHERE recruiter_id = :u OR sender_id = :u", u=user_id)

            for hr_uid in hr_ids:
                delete_user_files(hr_uid)
                delete_user_rows(hr_uid)
                _run("UPDATE user SET created_by = NULL WHERE created_by = :u", u=hr_uid)
                _run("UPDATE user SET deleted_by = NULL WHERE deleted_by = :u", u=hr_uid)
                _run("DELETE FROM user WHERE id = :u", u=hr_uid)

        # Files must be read/removed BEFORE delete_user_rows() wipes
        # the profile rows that contain the file-path columns.
        delete_user_files(user_id)
        delete_user_rows(user_id)

        _run("UPDATE user SET created_by = NULL WHERE created_by = :u", u=user_id)
        _run("UPDATE user SET deleted_by = NULL WHERE deleted_by = :u", u=user_id)
        db.session.execute(text("DELETE FROM user WHERE id = :u"), {"u": user_id})

        db.session.commit()
        return True, None

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"[deletion_service] delete_user_completely({user_id}) failed")
        return False, str(e)