# ================================================================
# BEFORE: status strings like 'pending' / 'Pending' / 'accepted' /
#   'Job Removed' were typed out independently in admin.py, hr.py,
#   recruiter.py, applicant.py, employment.py — with inconsistent
#   casing "defensively" handled by listing BOTH cases in tuples,
#   e.g. _ACTIVE_STATUSES = ('pending', 'Pending', 'interview', ...).
#   That's a strong signal the casing bug already happened in
#   production. File-size limits (5MB/10MB) and allowed extensions
#   were also re-declared in five different route files, and the
#   verification email hardcoded http://127.0.0.1:5000/login.
#
# AFTER: one source of truth. Every route imports from here instead
#   of re-typing literals. Status values are normalized to a single
#   lowercase form, so the "list both cases" workaround in
#   applicant.py / hr.py / recruiter.py is no longer needed.
# ================================================================

import os


# ----------------------------------------------------------------
# Application status — single canonical (lowercase) value per state.
# Old code did things like:
#     Application.status.in_(['Pending', 'pending', 'Interview', 'interview', ...])
# because two different code paths had written both 'Pending' and
# 'pending' into the DB over time. Using this enum for all *new*
# writes stops the drift from getting worse, and ACTIVE/ARCHIVED
# groupings live in one place instead of being copy-pasted per file.
# ----------------------------------------------------------------
class ApplicationStatus:
    PENDING      = 'pending'
    INTERVIEW    = 'interview'
    WAITLISTED   = 'waitlisted'
    UNDER_REVIEW = 'under_review'
    ACCEPTED     = 'accepted'
    EMPLOYED     = 'employed'
    REJECTED     = 'rejected'
    RESIGNED     = 'resigned'
    FIRED        = 'fired'
    JOB_REMOVED  = 'job_removed'

    ACTIVE = (PENDING, INTERVIEW, WAITLISTED, UNDER_REVIEW, ACCEPTED, EMPLOYED)
    ARCHIVED = (REJECTED, RESIGNED, FIRED, JOB_REMOVED)

    @classmethod
    def normalize(cls, value):
        """Map any legacy-cased value ('Pending', 'PENDING', 'Job Removed')
        onto the canonical lowercase constant, so old rows and new code
        can be compared safely without a duplicated tuple of both cases."""
        if not value:
            return value
        return value.strip().lower().replace(' ', '_')


class EmploymentStatus:
    ACTIVE               = 'active'
    RESIGNATION_PENDING  = 'resignation_pending'
    RENDERING            = 'rendering'
    RESIGNED             = 'resigned'
    FIRED                = 'fired'


class ResignationStatus:
    PENDING             = 'pending'
    REVISION_REQUESTED  = 'revision_requested'
    APPROVED            = 'approved'
    REJECTED             = 'rejected'


# ----------------------------------------------------------------
# Upload config — was copy-pasted (with slightly different values
# each time!) in applicant.py, hr.py, recruiter.py, employment.py,
# report_block.py. Centralizing means changing the resume size limit
# once here changes it everywhere instead of hunting through 5 files.
# ----------------------------------------------------------------
class UploadLimits:
    MAX_RESUME_SIZE_BYTES      = 5 * 1024 * 1024    # 5 MB
    MAX_DOCUMENT_SIZE_BYTES    = 10 * 1024 * 1024   # 10 MB (portfolio, proof, evidence)
    MAX_EVIDENCE_FILES         = 5

    IMAGE_EXTENSIONS      = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
    DOCUMENT_EXTENSIONS   = {'.pdf', '.jpg', '.jpeg', '.png'}
    LETTER_EXTENSIONS     = {'.pdf', '.jpg', '.jpeg', '.png', '.doc', '.docx'}
    EVIDENCE_EXTENSIONS   = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.pdf'}
    RESUME_EXTENSIONS     = {'.pdf'}


# ----------------------------------------------------------------
# Site config — was hardcoded as a literal string inside
# routes/auth.py:send_verification_email(), so switching to a real
# domain in production meant hunting for a string buried in an
# f-string instead of changing one env var.
# ----------------------------------------------------------------
class SiteConfig:
    # Falls back to localhost for local dev, but production sets
    # SITE_BASE_URL in the environment (or app.config) instead of
    # editing source code.
    BASE_URL = os.environ.get('SITE_BASE_URL', 'http://127.0.0.1:5000')


# ----------------------------------------------------------------
# Notification "types" — also scattered as raw strings
# ('account_verified', 'job_takedown', 'new_message', ...) across
# admin.py, chat.py, employment.py, hr.py, recruiter.py. Not every
# one is enumerated here (there are ~20), but centralizing the most
# reused ones stops typos like 'account_verifed' from silently
# creating a notification type the frontend never matches on.
# ----------------------------------------------------------------
class NotifType:
    ACCOUNT_VERIFIED   = 'account_verified'
    ACCOUNT_REJECTED   = 'account_rejected'
    ACCOUNT_BANNED     = 'account_banned'
    JOB_TAKEDOWN       = 'job_takedown'
    JOB_RESTORED       = 'job_restored'
    JOB_DELETED        = 'job_deleted'
    NEW_MESSAGE        = 'new_message'
    NEW_FOLLOW         = 'new_follow'
    FOLLOW_REQUEST     = 'follow_request'
    NEW_APPLICATION    = 'new_application'
    USER_DELETED       = 'user_deleted'