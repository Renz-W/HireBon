from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify, session
from flask_login import login_required, current_user
from models import (
    db, Job, User, Application, JobImage, HRFeedback,
    RecruiterNotification, ApplicantNotification, RecruiterEducation,
    JobTeamMember, HRProfile, RecruiterProfile, Employee
)
from werkzeug.security import generate_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, date
from models import (
    db, Job, User, Application, JobImage, HRFeedback,
    RecruiterNotification, ApplicantNotification, RecruiterEducation,
    JobTeamMember, HRProfile, RecruiterProfile, Employee, get_ph_time
)
from PIL import Image
import base64
import io
import secrets
import string
import os
import uuid
import json

# FIX: shared deletion logic (see services/deletion_service.py)
# replaces the ~120-line duplicated raw-SQL block that used to live in
# force_delete_job() below.
from services.deletion_service import delete_job_completely


recruiter_bp = Blueprint('recruiter', __name__, url_prefix="/recruiter")

# ==============================================================
# FIX #2 — God Object / Large File
#
# BEFORE: this file also defined generate_temp_password() and the
#   full HR-account lifecycle — hr_accounts(), create_hr(),
#   soft_delete_hr(), soft_delete_all_hr(), undo_delete_hr(),
#   undo_delete_all_hr(), commit_delete_hr(), commit_delete_all_hr()
#   — about 230 unrelated lines mixed in with job posting, job
#   editing, and application review logic.
#
# AFTER: all of that now lives in routes/recruiter_hr_accounts.py,
#   which attaches its routes to THIS SAME recruiter_bp object (see
#   that file's docstring for why — it keeps every existing
#   url_for('recruiter.hr_accounts') call in your templates working
#   with zero template changes). app.py must import that module so
#   its @recruiter_bp.route(...) registrations actually run — see
#   the note at the bottom of recruiter_hr_accounts.py.
# ==============================================================


# ===============================
# HELPER — ban check
# ===============================
def check_banned():
    if current_user.is_authenticated and current_user.is_banned:
        from flask import render_template as rt
        return rt("account_banned.html", user=current_user)
    return None


# ===============================
# HELPER — recruiter profile completion check
# ===============================
def is_recruiter_profile_complete(profile):
    if not profile:
        return False
    return all([
        profile.first_name,
        profile.surname,
        profile.phone_number,
        profile.company_name,
        profile.company_industry,
        profile.country,
        profile.city,
    ])


# ===============================
# HELPER — allowed image extensions
# ===============================
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


# ===============================
# RECRUITER PROFILE / DASHBOARD
# ===============================
@recruiter_bp.route('/profile')
@login_required
def profile():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    from models import Follow

    jobs = Job.query.filter_by(company_id=current_user.id).all()
    hrs = User.query.filter_by(created_by=current_user.id, role='hr').all()
    rec_profile = RecruiterProfile.query.filter_by(user_id=current_user.id).first()

    educations = []
    if rec_profile:
        educations = RecruiterEducation.query.filter_by(
            profile_id=rec_profile.id
        ).order_by(RecruiterEducation.created_at.desc()).all()

    follower_rows  = Follow.query.filter_by(followed_id=current_user.id).all()
    following_rows = Follow.query.filter_by(follower_id=current_user.id).all()
    followers = [User.query.get(r.follower_id) for r in follower_rows]
    following = [User.query.get(r.followed_id) for r in following_rows]
    followers = [u for u in followers if u and not u.is_banned and not u.is_deleted and not u.is_deactivated]
    following = [u for u in following if u and not u.is_banned and not u.is_deleted and not u.is_deactivated]

    profile_complete = is_recruiter_profile_complete(rec_profile)

# ── Determine verify_state ──────────────────────────────────────
    # Order matters: check most specific states first.

    if current_user.is_verified and current_user.verification_status == "Approved":
        verify_state = "approved"

    elif current_user.verification_status == "Rejected":
        verify_state = "rejected"

    elif rec_profile and rec_profile.submitted_for_review:
        verify_state = "pending"

    elif profile_complete:
        # Profile fields are done — check whether company proof is uploaded
        if rec_profile and rec_profile.company_proof:
            # Everything ready → show Submit button
            verify_state = "complete_unsubmitted"
        else:
            # Fields done but no proof yet → show proof upload step
            verify_state = "complete_needs_proof"

    else:
        verify_state = "incomplete"

    return render_template(
        'recruiter/profile.html',
        jobs=jobs,
        hrs=hrs,
        profile=rec_profile,
        educations=educations,
        follower_count=len(followers),
        following_count=len(following),
        followers=followers,
        following=following,
        profile_complete=profile_complete,
        verify_state=verify_state,
        today=date.today(),
    )


# ===============================
# SUBMIT PROFILE FOR ADMIN REVIEW
# ===============================
@recruiter_bp.route('/submit-for-review', methods=['POST'])
@login_required
def submit_for_review():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    profile = current_user.recruiter_profile

    if not is_recruiter_profile_complete(profile):
        flash("Please complete all required profile fields before submitting for review.", "warning")
        return redirect(url_for('recruiter.profile'))

    if current_user.is_verified:
        flash("Your account is already verified.", "info")
        return redirect(url_for('recruiter.profile'))

    if profile.submitted_for_review and current_user.verification_status == "Pending":
        flash("Your account is already pending review.", "info")
        return redirect(url_for('recruiter.profile'))

    profile.submitted_for_review = True
    current_user.verification_status = "Pending"

    try:
        from routes.admin import push_admin_notif
        push_admin_notif(
            'account_request',
            f'Recruiter <strong>{current_user.username}</strong> has submitted their profile for verification.',
            user_id=current_user.id
        )
    except Exception:
        current_app.logger.exception(
            f"[submit_for_review] failed to push admin notif for user_id={current_user.id}"
        )

    db.session.commit()

    flash("Your profile has been submitted for admin review. You'll be notified once verified.", "success")
    return redirect(url_for('recruiter.profile'))


# ===============================
# UPLOAD PROFILE PICTURE
# ===============================
@recruiter_bp.route('/upload-profile-picture', methods=['POST'])
@login_required
def upload_profile_picture():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    cropped_data = request.form.get('cropped_image')

    if not cropped_data:
        flash("No image data received.", "danger")
        return redirect(url_for('recruiter.profile'))

    try:
        header, encoded = cropped_data.split(',', 1)
        image_data = base64.b64decode(encoded)

        image = Image.open(io.BytesIO(image_data)).convert('RGB')
        image = image.resize((400, 400), Image.LANCZOS)

        upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'profile_pictures')
        os.makedirs(upload_folder, exist_ok=True)

        if current_user.profile_picture and not current_user.profile_picture.startswith('http'):
            old_path = os.path.join(upload_folder, current_user.profile_picture)
            if os.path.exists(old_path):
                os.remove(old_path)

        filename = f"pfp_{current_user.id}_{uuid.uuid4().hex[:8]}.jpg"
        image.save(os.path.join(upload_folder, filename), 'JPEG', quality=90)

        current_user.profile_picture = filename
        db.session.commit()

        flash("Profile picture updated!", "success")

    except Exception as e:
        flash(f"Upload failed: {str(e)}", "danger")

    return redirect(url_for('recruiter.profile'))


# ===============================
# UPLOAD COMPANY LOGO
# ===============================
@recruiter_bp.route('/upload-company-logo', methods=['POST'])
@login_required
def upload_company_logo():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    cropped_data = request.form.get('cropped_image')

    if not cropped_data:
        flash("No image data received.", "danger")
        return redirect(url_for('recruiter.profile'))

    try:
        header, encoded = cropped_data.split(',', 1)
        image_data = base64.b64decode(encoded)

        image = Image.open(io.BytesIO(image_data)).convert('RGBA')
        image.thumbnail((800, 800), Image.LANCZOS)

        upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'company_logos')
        os.makedirs(upload_folder, exist_ok=True)

        profile = current_user.recruiter_profile

        if profile is None:
            flash("Recruiter profile not found.", "danger")
            return redirect(url_for('recruiter.profile'))

        if profile.company_logo and not profile.company_logo.startswith('http'):
            old_path = os.path.join(upload_folder, profile.company_logo)
            if os.path.exists(old_path):
                os.remove(old_path)

        filename = f"logo_{current_user.id}_{uuid.uuid4().hex[:8]}.png"
        image.save(os.path.join(upload_folder, filename), 'PNG')

        profile.company_logo = filename
        db.session.commit()

        flash("Company logo updated!", "success")

    except Exception as e:
        flash(f"Upload failed: {str(e)}", "danger")

    return redirect(url_for('recruiter.profile'))


# ===============================
# RECRUITER UPDATE PROFILE
# ===============================
@recruiter_bp.route('/update-profile', methods=['POST'])
@login_required
def update_profile():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    section = request.form.get('section')
    profile = current_user.recruiter_profile

    if not profile:
        profile = RecruiterProfile(user_id=current_user.id)
        db.session.add(profile)

    if section == 'personal':
        profile.first_name   = request.form.get('first_name', '').strip()
        profile.middle_name  = request.form.get('middle_name', '').strip()
        profile.surname      = request.form.get('surname', '').strip() or request.form.get('last_name', '').strip()
        profile.gender       = request.form.get('gender', '').strip()
        profile.phone_number = request.form.get('phone_number', '').strip()
        profile.home_address = request.form.get('home_address', '').strip()
        profile.headline     = request.form.get('headline', '').strip()
        profile.bio          = request.form.get('bio', '').strip()
        dob_str = request.form.get('date_of_birth')
        if dob_str:
            try:
                profile.date_of_birth = datetime.strptime(dob_str, "%Y-%m-%d").date()
            except ValueError:
                pass

    elif section == 'company':
        profile.company_name         = request.form.get('company_name', '').strip()
        profile.company_industry     = request.form.get('industry', '').strip()
        profile.country              = request.form.get('country', '').strip()
        profile.city                 = request.form.get('city', '').strip()
        profile.company_address      = request.form.get('company_address', '').strip()
        profile.company_email_domain = request.form.get('company_website', '').strip()
        profile.company_description  = request.form.get('company_description', '').strip()

    elif section == 'account':
        new_username = request.form.get('username', '').strip()
        if new_username and new_username != current_user.username:
            existing = User.query.filter_by(username=new_username).first()
            if existing:
                flash("That username is already taken.", "danger")
                return redirect(url_for('recruiter.profile'))
            current_user.username = new_username

    db.session.flush()

    user_row = db.session.get(User, current_user.id)
    if not getattr(user_row, 'profile_completed', False):
        if is_recruiter_profile_complete(profile):
            user_row.profile_completed = True

    db.session.commit()
    flash("Profile updated successfully!", "success")
    return redirect(url_for('recruiter.profile'))


# ===============================
# UPDATE SOCIAL LINKS
# ===============================
@recruiter_bp.route('/update-social', methods=['POST'])
@login_required
def update_social():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    profile = current_user.recruiter_profile

    if not profile:
        profile = RecruiterProfile(user_id=current_user.id)
        db.session.add(profile)

    profile.facebook  = request.form.get('facebook', '').strip()
    profile.github    = request.form.get('github', '').strip()
    profile.portfolio = request.form.get('portfolio', '').strip()

    db.session.commit()
    flash("Links updated successfully!", "success")
    return redirect(url_for('recruiter.profile'))


# ===============================
# ADD EDUCATION  (recruiter-specific)
# ===============================
@recruiter_bp.route('/add-education', methods=['POST'])
@login_required
def add_education():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    profile = current_user.recruiter_profile
    if not profile:
        profile = RecruiterProfile(user_id=current_user.id)
        db.session.add(profile)
        db.session.flush()

    is_current = request.form.get('is_current') == '1'

    edu = RecruiterEducation(
        profile_id     = profile.id,
        school         = request.form.get('school', '').strip(),
        education_level= request.form.get('education_level', '').strip(),
        degree         = request.form.get('degree', '').strip(),
        field_of_study = request.form.get('field_of_study', '').strip(),
        start_date     = request.form.get('start_date', '').strip(),
        end_date       = '' if is_current else request.form.get('end_date', '').strip(),
        is_current     = is_current,
        description    = request.form.get('description', '').strip(),
    )

    db.session.add(edu)
    db.session.commit()

    flash("Education added!", "success")
    return redirect(url_for('recruiter.profile'))


# ===============================
# DELETE EDUCATION  (recruiter-specific)
# ===============================
@recruiter_bp.route('/delete-education/<int:edu_id>', methods=['POST'])
@login_required
def delete_education(edu_id):
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    edu = RecruiterEducation.query.get_or_404(edu_id)

    profile = current_user.recruiter_profile
    if not profile or edu.profile_id != profile.id:
        flash("Unauthorized action!", "danger")
        return redirect(url_for('recruiter.profile'))

    db.session.delete(edu)
    db.session.commit()

    flash("Education removed.", "success")
    return redirect(url_for('recruiter.profile'))

# ===============================
# UPLOAD PORTFOLIO (RECRUITER)
# ===============================
@recruiter_bp.route('/profile/upload-portfolio', methods=['POST'])
@login_required
def upload_portfolio():
    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    prof = RecruiterProfile.query.filter_by(user_id=current_user.id).first()
    if not prof:
        flash("Profile not found. Please complete your profile first.", "danger")
        return redirect(url_for('recruiter.profile'))

    file = request.files.get('portfolio_file')

    if not file or file.filename == '':
        flash("No file selected.", "danger")
        return redirect(url_for('recruiter.profile'))

    allowed = {'.pdf', '.jpg', '.jpeg', '.png'}
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in allowed:
        flash("Portfolio must be a PDF, JPG, or PNG file.", "danger")
        return redirect(url_for('recruiter.profile'))

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 10 * 1024 * 1024:
        flash("Portfolio file exceeds the 10MB limit.", "danger")
        return redirect(url_for('recruiter.profile'))

    folder = os.path.join(current_app.root_path, 'static', 'uploads', 'recruiter_portfolios')
    os.makedirs(folder, exist_ok=True)

    if prof.portfolio_file:
        old = os.path.join(folder, prof.portfolio_file)
        if os.path.exists(old):
            os.remove(old)

    filename = f"recruiter_portfolio_{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    file.save(os.path.join(folder, filename))
    prof.portfolio_file = filename
    db.session.commit()
    flash("Portfolio uploaded successfully!", "success")
    return redirect(url_for('recruiter.profile'))


# ===============================
# DELETE PORTFOLIO (RECRUITER)
# ===============================
@recruiter_bp.route('/profile/delete-portfolio', methods=['POST'])
@login_required
def delete_portfolio():
    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    prof = RecruiterProfile.query.filter_by(user_id=current_user.id).first()
    if prof and prof.portfolio_file:
        path = os.path.join(current_app.root_path, 'static', 'uploads', 'recruiter_portfolios', prof.portfolio_file)
        if os.path.exists(path):
            os.remove(path)
        prof.portfolio_file = None
        db.session.commit()
        flash("Portfolio removed successfully.", "success")
    else:
        flash("No portfolio file found to delete.", "warning")
    
    return redirect(url_for('recruiter.profile'))

# ===============================
# POST JOB  
# ===============================
@recruiter_bp.route('/post-job', methods=['POST'])
@login_required
def post_job():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    if not current_user.is_verified:
        flash("Your account must be verified before you can post jobs.", "warning")
        return redirect(url_for('recruiter.profile'))

    title = request.form.get('title')
    description = request.form.get('description')
    field = request.form.get('field')
    job_type = request.form.get('job_type')
    location = request.form.get('location')
    salary = request.form.get('salary')
    currency = request.form.get('currency', 'PHP')
    expiration_date = request.form.get('expiration_date')

    arrangement        = request.form.get('arrangement')
    experience_level   = request.form.get('experience_level')
    years_exp          = request.form.get('years_exp')
    education          = request.form.get('education')
    required_skills    = request.form.get('required_skills')
    preferred_skills   = request.form.get('preferred_skills')
    languages          = request.form.get('languages')
    requirements_notes = request.form.get('requirements_notes')

    # Quota / toggle
    max_applications_str = request.form.get('max_applications', '').strip()
    max_applications = int(max_applications_str) if max_applications_str.isdigit() else None
    allow_applications = request.form.get('allow_applications') == 'on'

    # Company content
    about_company_raw = request.form.get('about_company', '').strip()
    about_company = about_company_raw or None

    # why_join_us arrives as a JSON string from the hidden input (built by job_posting.js)
    why_join_us_raw = request.form.get('why_join_us', '').strip()
    if why_join_us_raw:
        try:
            # Validate it's proper JSON; store as-is
            json.loads(why_join_us_raw)
            why_join_us = why_join_us_raw
        except (ValueError, TypeError):
            # Fall back: treat as plain text, wrap in JSON array
            lines = [l.strip() for l in why_join_us_raw.split('\n') if l.strip()]
            why_join_us = json.dumps(lines) if lines else None
    else:
        why_join_us = None

    expiration = None
    if expiration_date:
        expiration = datetime.strptime(expiration_date, "%Y-%m-%d").date()

    job = Job(
        title=title,
        description=description,
        company_id=current_user.id,
        field=field,
        job_type=job_type,
        location=location,
        salary=salary,
        currency=currency,
        expiration_date=expiration,
        arrangement=arrangement,
        experience_level=experience_level,
        years_exp=years_exp,
        education=education,
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        languages=languages,
        requirements_notes=requirements_notes,
        max_applications=max_applications,
        allow_applications=allow_applications,
        about_company=about_company,
        why_join_us=why_join_us,
    )

    db.session.add(job)
    db.session.flush()

    # ── Cover photo ──
    cover_file = request.files.get('cover_photo')
    if cover_file and cover_file.filename != '' and allowed_image(cover_file.filename):
        upload_folder = os.path.join(current_app.root_path, "static", "uploads", "job_covers")
        os.makedirs(upload_folder, exist_ok=True)
        filename = secure_filename(cover_file.filename)
        unique_name = f"cover_{job.id}_{uuid.uuid4().hex[:8]}_{filename}"
        cover_file.save(os.path.join(upload_folder, unique_name))
        job.cover_photo = unique_name

    # ── Gallery images ──
    poster_files = request.files.getlist("posters")
    upload_folder = os.path.join(current_app.root_path, "static", "uploads", "job_posters")
    os.makedirs(upload_folder, exist_ok=True)

    for file in poster_files:
        if file and file.filename != "":
            filename = secure_filename(file.filename)
            unique_name = f"{uuid.uuid4()}_{filename}"
            file.save(os.path.join(upload_folder, unique_name))
            image = JobImage(job_id=job.id, image_path=unique_name)
            db.session.add(image)

    db.session.commit()

    flash("Job posted successfully!", "success")
    return redirect(url_for('recruiter.job_posting'))


# ===============================
# JOB POSTING PAGE
# ===============================
@recruiter_bp.route('/job-posting')
@login_required
def job_posting():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    jobs = Job.query.filter_by(company_id=current_user.id).all()

    return render_template("recruiter/job_posting.html", jobs=jobs)


# ===============================
# MY JOB LIST
# ===============================
@recruiter_bp.route('/my-job-list')
@login_required
def my_job_list():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    jobs = Job.query.filter_by(company_id=current_user.id).all()

    # Pre-compute active counts per job
    _ACTIVE = ('pending', 'interview', 'waitlisted', 'accepted')
    job_active_counts = {}
    for job in jobs:
        count = Application.query.filter(
            Application.job_id == job.id,
            Application.status.in_(_ACTIVE)
        ).count()
        job_active_counts[job.id] = count

    return render_template(
        "recruiter/my_job_list.html",
        jobs=jobs,
        current_date=date.today(),
        job_active_counts=job_active_counts
    )


# ===============================
# VIEW JOB APPLICATIONS
# ===============================
@recruiter_bp.route('/job-applications/<int:job_id>')
@login_required
def view_job_applications(job_id):
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    job = Job.query.get_or_404(job_id)

    if job.company_id != current_user.id:
        flash("Unauthorized access!", "danger")
        return redirect(url_for('recruiter.my_job_list'))

    _ACTIVE_STATUSES = ('pending', 'interview', 'waitlisted', 'accepted')
    _ARCHIVED_STATUSES = ('rejected', 'resigned', 'fired')

    applications = (
            Application.query
            .join(User, Application.applicant_id == User.id)
            .filter(
                Application.job_id == job_id,
                Application.status.in_(_ACTIVE_STATUSES),
                User.is_banned == False
            ).all()
        )

    archived_applications = (
            Application.query
            .join(User, Application.applicant_id == User.id)
            .filter(
                Application.job_id == job_id,
                Application.status.in_(_ARCHIVED_STATUSES),
                User.is_banned == False
            ).all()
        )
    return render_template(
        "recruiter/job_applications.html",
        job=job,
        applications=applications,
        archived_applications=archived_applications,
    )

@recruiter_bp.route('/job-applications/<int:job_id>/archived')
@login_required
def archived_applications(job_id):
    banned = check_banned()
    if banned:
        return banned
 
    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))
 
    job = Job.query.get_or_404(job_id)
 
    if job.company_id != current_user.id:
        flash("Unauthorized access!", "danger")
        return redirect(url_for('recruiter.my_job_list'))
 
    _ARCHIVED_STATUSES = ('rejected', 'resigned', 'fired')
    archived_applications = Application.query.filter(
        Application.job_id == job_id,
        Application.status.in_(_ARCHIVED_STATUSES)
    ).order_by(Application.created_at.desc()).all()
 
    return render_template(
        "shared/archived_applications.html",
        job=job,
        archived_applications=archived_applications,
        back_url=url_for('recruiter.view_job_applications', job_id=job_id),
    )


# ===============================
# UPDATE APPLICATION STATUS
# ===============================
@recruiter_bp.route('/update-application-status/<int:app_id>', methods=['POST'])
@login_required
def update_application_status(app_id):
    banned = check_banned()
    if banned:
        return banned
 
    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))
 
    application = Application.query.get_or_404(app_id)
    if Employee.query.filter_by(application_id=app_id).first():
        flash("This applicant is already a confirmed employee. Status cannot be changed.", "warning")
        return redirect(url_for('recruiter.view_job_applications', job_id=application.job_id))
 
    job = Job.query.get_or_404(application.job_id)
 
    if job.company_id != current_user.id:
        flash("Unauthorized action!", "danger")
        return redirect(url_for('recruiter.my_job_list'))
 
    new_status         = request.form.get('status')
    new_remarks        = request.form.get('recruiter_remarks')
    interview_date_str = request.form.get('interview_date')
    interview_session  = request.form.get('interview_session')
    meeting_type_val   = request.form.get('meeting_type')
    meeting_link_val   = request.form.get('meeting_link')
 
    if new_status:
        application.status = new_status
 
    if new_remarks is not None:
        application.recruiter_remarks = new_remarks
 
    if new_status == 'interview':
        if interview_date_str:
            application.interview_date = datetime.strptime(interview_date_str, "%Y-%m-%dT%H:%M")

        if interview_session == 'online':
            application.meeting_type = meeting_type_val or None
            application.meeting_link = meeting_link_val or None
        elif interview_session == 'face-to-face':
            application.meeting_type = 'face-to-face'
            application.meeting_link = None
        elif not interview_session:
            # No session type submitted — don't overwrite existing values
            pass
 
    elif new_status in ('accepted', 'waitlisted', 'rejected', 'pending'):
        application.interview_date = None
        application.meeting_type   = None
        application.meeting_link   = None
 
    db.session.commit()
 
    applicant_user = User.query.get(application.applicant_id)
    job_for_notif  = Job.query.get(application.job_id)
 
    if new_status:
        notif = RecruiterNotification(
            recruiter_id=current_user.id,
            type='new_application',
            message=f"Application status for <strong>{applicant_user.username}</strong> on <strong>{job_for_notif.title}</strong> updated to <strong>{new_status.capitalize()}</strong>.",
            application_id=application.id,
            job_id=application.job_id
        )
        db.session.add(notif)
 
        app_notif = ApplicantNotification(
            applicant_id=application.applicant_id,
            type='application_status',
            message=f"Your application for <strong>{job_for_notif.title}</strong> has been updated to <strong>{new_status.capitalize()}</strong>.",
            application_id=application.id,
            job_id=application.job_id
        )
        db.session.add(app_notif)
        db.session.commit()
 
    flash("Application status updated!", "success")
    return redirect(url_for('recruiter.view_job_applications', job_id=job.id))


# ===============================
# SCHEDULE INTERVIEW
# ===============================
@recruiter_bp.route('/schedule-interview/<int:app_id>', methods=['POST'])
@login_required
def schedule_interview(app_id):
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    application = Application.query.get_or_404(app_id)
    job = Job.query.get_or_404(application.job_id)

    if job.company_id != current_user.id:
        flash("Unauthorized action!", "danger")
        return redirect(url_for('recruiter.my_job_list'))

    interview_date_str = request.form.get('interview_date')

    if interview_date_str:
        application.interview_date = datetime.strptime(interview_date_str, "%Y-%m-%dT%H:%M")
        application.status = 'interview'
        db.session.commit()

        applicant = User.query.get(application.applicant_id)
        job_ref = Job.query.get(application.job_id)

        notif = RecruiterNotification(
            recruiter_id=current_user.id,
            type='interview_scheduled',
            message=f"Interview scheduled for <strong>{applicant.username}</strong> applying for <strong>{job_ref.title}</strong> on {application.interview_date.strftime('%b %d, %Y at %I:%M %p')}.",
            application_id=application.id,
            job_id=application.job_id
        )
        db.session.add(notif)

        app_notif = ApplicantNotification(
            applicant_id=application.applicant_id,
            type='interview_scheduled',
            message=f"An interview has been scheduled for your application to <strong>{job_ref.title}</strong> on <strong>{application.interview_date.strftime('%b %d, %Y at %I:%M %p')}</strong>.",
            application_id=application.id,
            job_id=application.job_id
        )
        db.session.add(app_notif)
        db.session.commit()

        flash("Interview scheduled successfully!", "success")
    else:
        flash("Please provide a valid date and time.", "danger")

    return redirect(url_for('recruiter.view_job_applications', job_id=job.id))


# ===============================
# EDIT JOB  (GET + POST)
# ===============================
@recruiter_bp.route('/edit-job/<int:job_id>', methods=['GET', 'POST'])
@login_required
def edit_job(job_id):
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    job = Job.query.get_or_404(job_id)

    if job.company_id != current_user.id:
        flash("Unauthorized action!", "danger")
        return redirect(url_for('recruiter.job_posting'))

    if request.method == "POST":

        job.title       = request.form.get('title')
        job.description = request.form.get('description')
        job.field       = request.form.get('field')
        job.job_type    = request.form.get('job_type')
        job.location    = request.form.get('location')
        job.salary      = request.form.get('salary')
        job.currency    = request.form.get('currency', 'PHP')
        job.arrangement = request.form.get('arrangement')

        job.experience_level   = request.form.get('experience_level')
        job.years_exp          = request.form.get('years_exp')
        job.education          = request.form.get('education')
        job.required_skills    = request.form.get('required_skills')
        job.preferred_skills   = request.form.get('preferred_skills')
        job.languages          = request.form.get('languages')
        job.requirements_notes = request.form.get('requirements_notes')

        expiration_date = request.form.get('expiration_date')
        if expiration_date:
            job.expiration_date = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        else:
            job.expiration_date = None

        # ── NEW fields ──
        max_applications_str = request.form.get('max_applications', '').strip()
        job.max_applications = int(max_applications_str) if max_applications_str.isdigit() else None
        job.allow_applications = request.form.get('allow_applications') == 'on'

        # ── Company content per-job override ──
        job.about_company  = request.form.get('about_company', '').strip() or None
        job.why_join_us    = request.form.get('why_join_us', '').strip() or None
        job.company_values = request.form.get('company_values', '').strip() or None

        # ── Cover photo ──
        cover_file = request.files.get('cover_photo')
        if cover_file and cover_file.filename != '' and allowed_image(cover_file.filename):
            upload_folder = os.path.join(current_app.root_path, "static", "uploads", "job_covers")
            os.makedirs(upload_folder, exist_ok=True)

            if job.cover_photo:
                old_path = os.path.join(upload_folder, job.cover_photo)
                if os.path.exists(old_path):
                    os.remove(old_path)

            filename = secure_filename(cover_file.filename)
            unique_name = f"cover_{job.id}_{uuid.uuid4().hex[:8]}_{filename}"
            cover_file.save(os.path.join(upload_folder, unique_name))
            job.cover_photo = unique_name

        # ── Gallery images ──
        poster_files = request.files.getlist("posters")
        upload_folder = os.path.join(current_app.root_path, "static", "uploads", "job_posters")
        os.makedirs(upload_folder, exist_ok=True)

        for poster_file in poster_files:
            if poster_file and poster_file.filename != "" and allowed_image(poster_file.filename):
                filename = secure_filename(poster_file.filename)
                unique_name = f"{uuid.uuid4()}_{filename}"
                poster_path = os.path.join(upload_folder, unique_name)
                poster_file.save(poster_path)
                new_image = JobImage(job_id=job.id, image_path=unique_name)
                db.session.add(new_image)

        # ── Force updated_at refresh ──
        job.updated_at = get_ph_time()

        db.session.commit()

        flash("Job updated successfully!", "success")
        return redirect(url_for('recruiter.edit_job', job_id=job.id))

    # GET — pass HR list for team assignment
    hr_list = User.query.filter_by(
        created_by=current_user.id,
        role='hr',
        is_deleted=False,
        is_banned=False
    ).all()

    assigned_hr_ids = {tm.hr_id for tm in job.team_members}

    employee_count = Employee.query.join(
        Job, Employee.job_id == Job.id
    ).filter(Job.company_id == current_user.id).count()

    return render_template(
        "recruiter/edit_job.html",
        job=job,
        hr_list=hr_list,
        assigned_hr_ids=assigned_hr_ids,
        employee_count=employee_count,
        today=date.today(),
    )


# ===============================
# UPDATE JOB COVER PHOTO  (AJAX)
# ===============================
@recruiter_bp.route('/update-job-cover/<int:job_id>', methods=['POST'])
@login_required
def update_job_cover(job_id):
    """Dedicated AJAX endpoint to change only the cover photo."""
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    job = Job.query.get_or_404(job_id)
    if job.company_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    cover_file = request.files.get('cover_photo')
    if not cover_file or cover_file.filename == '':
        return jsonify({'success': False, 'error': 'No file provided'}), 400

    if not allowed_image(cover_file.filename):
        return jsonify({'success': False, 'error': 'Invalid file type'}), 400

    upload_folder = os.path.join(current_app.root_path, "static", "uploads", "job_covers")
    os.makedirs(upload_folder, exist_ok=True)

    if job.cover_photo:
        old_path = os.path.join(upload_folder, job.cover_photo)
        if os.path.exists(old_path):
            os.remove(old_path)

    filename = secure_filename(cover_file.filename)
    unique_name = f"cover_{job.id}_{uuid.uuid4().hex[:8]}_{filename}"
    cover_file.save(os.path.join(upload_folder, unique_name))
    job.cover_photo = unique_name
    job.updated_at  = get_ph_time()
    db.session.commit()

    return jsonify({
        'success': True,
        'url': url_for('static', filename=f'uploads/job_covers/{unique_name}')
    })


# ===============================
# UPDATE JOB COMPANY CONTENT  (AJAX)
# ===============================
@recruiter_bp.route('/update-job-company-content/<int:job_id>', methods=['POST'])
@login_required
def update_job_company_content(job_id):
    """
    AJAX endpoint to update about_company, why_join_us, company_values
    for a specific job posting.
    """
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    job = Job.query.get_or_404(job_id)
    if job.company_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json(silent=True) or {}

    section = data.get('section')

    if section == 'about':
        job.about_company = data.get('content', '').strip() or None

    elif section == 'why_join':
        items = data.get('items', [])
        job.why_join_us = json.dumps(items) if items else None

    elif section == 'values':
        items = data.get('items', [])
        job.company_values = json.dumps(items) if items else None

    else:
        return jsonify({'success': False, 'error': 'Unknown section'}), 400

    job.updated_at = get_ph_time()
    db.session.commit()

    return jsonify({'success': True})


# ===============================
# MANAGE JOB TEAM MEMBERS  (AJAX)
# ===============================
@recruiter_bp.route('/job-team/<int:job_id>/add', methods=['POST'])
@login_required
def add_job_team_member(job_id):
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    job = Job.query.get_or_404(job_id)
    if job.company_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data  = request.get_json(silent=True) or {}
    hr_id = data.get('hr_id')

    try:
        hr_id = int(hr_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid HR ID'}), 400

    hr = User.query.filter_by(id=hr_id, role='hr', created_by=current_user.id, is_deleted=False).first()
    if not hr:
        return jsonify({'success': False, 'error': 'HR member not found'}), 404

    existing = JobTeamMember.query.filter_by(job_id=job_id, hr_id=hr_id).first()
    if existing:
        return jsonify({'success': False, 'error': 'Already assigned'}), 409

    member = JobTeamMember(job_id=job_id, hr_id=hr_id)
    db.session.add(member)
    db.session.commit()

    return jsonify({
        'success': True,
        'member': {
            'id':       member.id,
            'hr_id':    hr.id,
            'username': hr.username,
            'email':    hr.email,
            'picture':  hr.profile_picture,
        }
    })


@recruiter_bp.route('/job-team/<int:job_id>/remove', methods=['POST'])
@login_required
def remove_job_team_member(job_id):
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    job = Job.query.get_or_404(job_id)
    if job.company_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data  = request.get_json(silent=True) or {}
    hr_id = data.get('hr_id')

    member = JobTeamMember.query.filter_by(job_id=job_id, hr_id=hr_id).first()
    if not member:
        return jsonify({'success': False, 'error': 'Member not found'}), 404

    db.session.delete(member)
    db.session.commit()

    return jsonify({'success': True})


# ===============================
# TOGGLE ALLOW APPLICATIONS  (AJAX)
# ===============================
@recruiter_bp.route('/job-toggle-applications/<int:job_id>', methods=['POST'])
@login_required
def toggle_allow_applications(job_id):
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    job = Job.query.get_or_404(job_id)
    if job.company_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json(silent=True) or {}
    value = data.get('allow', True)
    job.allow_applications = bool(value)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(job, 'allow_applications')
    job.updated_at = get_ph_time()
    db.session.commit()

    return jsonify({'success': True, 'allow_applications': job.allow_applications})


# ===============================
# DELETE JOB IMAGE
# ===============================
@recruiter_bp.route('/delete-job-image/<int:image_id>', methods=['POST'])
@login_required
def delete_job_image(image_id):
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    image = JobImage.query.get_or_404(image_id)
    job = Job.query.get_or_404(image.job_id)

    if job.company_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    file_path = os.path.join(
        current_app.root_path, "static", "uploads", "job_posters", image.image_path
    )

    if os.path.exists(file_path):
        os.remove(file_path)

    db.session.delete(image)
    db.session.commit()

    return jsonify({'success': True})


# ==============================================================
# DELETE JOB — with active-applications guard
#
# ── FIX #1 / #3 ──────────────────────────────────────────────
# BEFORE: manually removed gallery images / cover photo, then
#   called db.session.delete(job) directly, relying on SQLAlchemy
#   cascade config to clean up the rest — inconsistent with the
#   raw-SQL approach used everywhere else in this app, and easy to
#   get out of sync with the FK chain if cascades aren't configured
#   for every table.
# AFTER: same "block if there are active applications, unless
#   forced" guard (kept here since it's specific to the recruiter-
#   initiated delete flow), but the actual deletion now goes
#   through delete_job_completely() so it's guaranteed to match the
#   exact same FK-safe order used by admin.py and force_delete_job()
#   below.
# ==============================================================
@recruiter_bp.route('/delete-job/<int:job_id>', methods=['POST'])
@login_required
def delete_job(job_id):
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    job = Job.query.get_or_404(job_id)

    if job.company_id != current_user.id:
        flash("Unauthorized action!", "danger")
        return redirect(url_for('recruiter.job_posting'))

    force = request.form.get('force_delete') == '1'
    active_apps = Application.query.filter_by(job_id=job_id).count()

    if active_apps > 0 and not force:
        flash(
            f"This job has {active_apps} application(s). "
            "To delete it, confirm deletion from the job management page.",
            "warning"
        )
        return redirect(url_for('recruiter.edit_job', job_id=job_id))

    ok, error = delete_job_completely(job_id)
    if ok:
        flash("Job deleted successfully!", "success")
    else:
        flash(f"Deletion failed: {error}", "danger")

    return redirect(url_for('recruiter.my_job_list'))


# ==============================================================
# FORCE DELETE JOB  (AJAX)
#
# ── FIX #1 / #3 ──────────────────────────────────────────────
# BEFORE: ~120 lines of raw SQL duplicating the same 13-step FK
#   cleanup that admin.py's job-deletion path (inside delete_user())
#   already implemented independently.
# AFTER: thin wrapper around the shared service — one FK-order
#   definition for the whole app instead of two that could silently
#   drift apart.
# ==============================================================
@recruiter_bp.route('/force-delete-job/<int:job_id>', methods=['POST'])
@login_required
def force_delete_job(job_id):
    """Called when recruiter explicitly confirms deletion despite active applications."""
    if current_user.role != 'recruiter':
        return jsonify({'success': False, 'error': 'Access denied'}), 403

    job = Job.query.get_or_404(job_id)
    if job.company_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    ok, error = delete_job_completely(job_id)
    if not ok:
        return jsonify({'success': False, 'error': error}), 500

    return jsonify({'success': True})


# ===============================
# RECRUITER NOTIFICATION HISTORY PAGE
# ===============================
@recruiter_bp.route('/notification-history')
@login_required
def notification_history():
    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))
    notifs = RecruiterNotification.query.filter_by(
        recruiter_id=current_user.id
    ).order_by(RecruiterNotification.created_at.desc()).all()
    RecruiterNotification.query.filter_by(
        recruiter_id=current_user.id, is_read=False
    ).update({'is_read': True})
    db.session.commit()
    return render_template('recruiter/notification_history.html', notifications=notifs)


@recruiter_bp.route('/clear-all-notifications', methods=['POST'])
@login_required
def clear_all_notifications():
    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    RecruiterNotification.query.filter_by(recruiter_id=current_user.id).delete()
    db.session.commit()

    flash("All notifications cleared.", "success")
    return redirect(url_for('recruiter.notification_history'))


@recruiter_bp.route('/notifications')
@login_required
def get_notifications():
    if current_user.role != 'recruiter':
        return jsonify({'error': 'forbidden'}), 403
    notifs = RecruiterNotification.query.filter_by(
        recruiter_id=current_user.id
    ).order_by(RecruiterNotification.created_at.desc()).limit(50).all()
    unread_count = RecruiterNotification.query.filter_by(
        recruiter_id=current_user.id, is_read=False
    ).count()
    return jsonify({
        'unread_count': unread_count,
        'notifications': [
            {
                'id': n.id,
                'type': n.type,
                'message': n.message,
                'is_read': n.is_read,
                'created_at': n.created_at.strftime('%b %d, %Y at %I:%M %p'),
                'job_id': n.job_id,
                'sender_id': n.sender_id
            }
            for n in notifs
        ]
    })

@recruiter_bp.route('/notifications/mark-read', methods=['POST'])
@login_required
def mark_notifications_read():
    if current_user.role != 'recruiter':
        return jsonify({'error': 'forbidden'}), 403
    data = request.get_json(silent=True) or {}
    notif_id = data.get('id')
    if notif_id:
        RecruiterNotification.query.filter_by(
            id=notif_id, recruiter_id=current_user.id
        ).update({'is_read': True})
    else:
        RecruiterNotification.query.filter_by(
            recruiter_id=current_user.id, is_read=False
        ).update({'is_read': True})
    db.session.commit()
    return jsonify({'ok': True})

@recruiter_bp.route('/notifications/clear-all', methods=['POST'])
@login_required
def clear_all_notifications_api():
    if current_user.role != 'recruiter':
        return jsonify({'error': 'forbidden'}), 403
    RecruiterNotification.query.filter_by(recruiter_id=current_user.id).delete()
    db.session.commit()
    return jsonify({'ok': True})

# ===============================
# UPLOAD COMPANY PROOF  (from banner)
# ===============================
@recruiter_bp.route('/upload-company-proof', methods=['POST'])
@login_required
def upload_company_proof():
    banned = check_banned()
    if banned:
        return banned

    if current_user.role != 'recruiter':
        flash("Access denied!", "danger")
        return redirect(url_for('auth.index'))

    profile = current_user.recruiter_profile
    if not profile:
        flash("Recruiter profile not found.", "danger")
        return redirect(url_for('recruiter.profile'))

    file = request.files.get('company_proof')

    if not file or file.filename == '':
        flash("No file selected.", "danger")
        return redirect(url_for('recruiter.profile'))

    allowed = {'.pdf', '.jpg', '.jpeg', '.png'}
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in allowed:
        flash("Company proof must be a PDF, JPG, or PNG file.", "danger")
        return redirect(url_for('recruiter.profile'))

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 10 * 1024 * 1024:
        flash("File exceeds the 10MB limit.", "danger")
        return redirect(url_for('recruiter.profile'))

    upload_folder = os.path.join(current_app.root_path, 'static', 'uploads', 'recruiter_documents')
    os.makedirs(upload_folder, exist_ok=True)

    # Remove old proof file if it exists
    if profile.company_proof:
        old_path = os.path.join(upload_folder, profile.company_proof)
        if os.path.exists(old_path):
            os.remove(old_path)

    filename = f"proof_{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    file.save(os.path.join(upload_folder, filename))
    profile.company_proof = filename
    db.session.commit()

    flash("Company proof uploaded! You can now submit for review.", "success")
    return redirect(url_for('recruiter.profile'))