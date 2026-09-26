from django.contrib import messages
from django.contrib.auth import login, authenticate, logout as auth_logout
from django.db import IntegrityError, transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.text import slugify
from django.http import HttpResponse, JsonResponse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Count, Sum, Case, When, IntegerField, Q
from django.utils import timezone
from django.db import models
from datetime import datetime
from io import BytesIO
import tempfile
import os
import csv
import base64
import json

from .models import (
    User,
    Student as StudentModel,
    OSACoordinator as OSACoordinatorModel,
    Violation,
    ViolationType,
    LoginActivity,
    ViolationDocument,
    ActivityLog,
    ApologyLetter,
    Message,
    StaffAlert,
)
from .decorators import login_required, role_required

############################################
# Welcome TTS (server-side via Python library)
############################################


def welcome_tts_view(request):
    """Generate welcome audio using a Python TTS library and return it as audio.

    Priority: gTTS -> pyttsx3. Returns MP3 if gTTS available, else WAV.
    Query params:
      - text: optional explicit text to synthesize
      - name: optional user name to include (used if text missing)
      - role: optional role label (Student/OSA Coordinator) used for default text
    """
    # Build text
    text = (request.GET.get("text") or "").strip()
    if not text:
        name = (
            request.GET.get("name")
            or getattr(request.user, "first_name", "")
            or getattr(request.user, "username", "")
            or "there"
        ).strip() or "there"
        role_raw = (
            request.GET.get("role")
            or getattr(
                getattr(request.user, "role", None),
                "lower",
                lambda: str(getattr(request.user, "role", "")),
            )()
            or ""
        ).lower()
        if role_raw in {"osa_coordinator", "faculty_admin", "faculty"}:
            role_label = "Manila Campus OSA Coordinator"
        else:
            role_label = "Manila Campus Student"
        text = f"Welcome back, {name}. You are now on your {role_label} dashboard."

    # Try gTTS first (MP3)
    try:
        from gtts import gTTS  # type: ignore

        mp3_bytes = BytesIO()
        tts = gTTS(text=text, lang="en")
        tts.write_to_fp(mp3_bytes)
        mp3_bytes.seek(0)
        return HttpResponse(mp3_bytes.getvalue(), content_type="audio/mpeg")
    except ImportError:
        pass
    except Exception:
        # Fall through to next engine
        pass

    # Fallback: pyttsx3 (WAV via SAPI5 on Windows)
    try:
        import pyttsx3  # type: ignore

        fd, tmp_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            engine = pyttsx3.init()
            # Try pick a professional female voice
            target = None
            for v in engine.getProperty("voices"):
                n = (getattr(v, "name", "") or "").lower()
                if any(
                    k in n
                    for k in [
                        "female",
                        "zira",
                        "aria",
                        "samantha",
                        "victoria",
                        "karen",
                        "tessa",
                        "serena",
                    ]
                ):
                    target = v.id
                    break
            if target:
                engine.setProperty("voice", target)
            # Calm pace
            try:
                rate = engine.getProperty("rate")
                if isinstance(rate, int):
                    engine.setProperty("rate", max(120, min(190, int(rate * 0.95))))
            except Exception:
                pass
            engine.setProperty("volume", 1.0)
            engine.save_to_file(text, tmp_path)
            engine.runAndWait()
            with open(tmp_path, "rb") as f:
                data = f.read()
            return HttpResponse(data, content_type="audio/wav")
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    except ImportError:
        pass
    except Exception:
        pass

    return JsonResponse(
        {
            "error": "No Python TTS engine available",
            "hint": "Install one of: gTTS (pip install gTTS) or pyttsx3 (pip install pyttsx3).",
        },
        status=501,
    )


############################################
# Face Detection API (for webcam head size detection)
############################################

from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def detect_face_view(request):
    """Detect face in image and return bounding box and head size info.

    Accepts POST with JSON body: { "image": "data:image/jpeg;base64,..." }
    Returns: { "faces": [...], "head_size": "small|medium|large|too_far|too_close" }
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        import cv2
        import numpy as np
    except ImportError:
        return JsonResponse({"error": "OpenCV not installed"}, status=501)

    try:
        data = json.loads(request.body)
        image_data = data.get("image", "")

        # Remove data URL prefix if present
        if "," in image_data:
            image_data = image_data.split(",")[1]

        # Decode base64 image
        image_bytes = base64.b64decode(image_data)
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        if img is None:
            return JsonResponse({"error": "Could not decode image"}, status=400)

        # Get image dimensions
        height, width = img.shape[:2]
        frame_area = width * height

        # Convert to grayscale for face detection
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Load Haar cascade for face detection
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        face_cascade = cv2.CascadeClassifier(cascade_path)

        # Detect faces
        faces = face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
        )

        face_list = []
        head_size = "no_face"
        head_percentage = 0

        for x, y, w, h in faces:
            face_area = w * h
            percentage = (face_area / frame_area) * 100

            face_list.append(
                {
                    "x": int(x),
                    "y": int(y),
                    "width": int(w),
                    "height": int(h),
                    "percentage": round(percentage, 2),
                }
            )

            # Determine head size based on percentage of frame
            # Ideal for ID photo: face should be 15-35% of frame
            if percentage < 5:
                head_size = "too_far"
            elif percentage < 15:
                head_size = "small"
            elif percentage <= 35:
                head_size = "ideal"
            elif percentage <= 50:
                head_size = "large"
            else:
                head_size = "too_close"

            head_percentage = percentage

        return JsonResponse(
            {
                "success": True,
                "faces": face_list,
                "face_count": len(face_list),
                "head_size": head_size,
                "head_percentage": round(head_percentage, 2),
                "frame_width": width,
                "frame_height": height,
                "guidance": get_head_size_guidance(head_size),
            }
        )

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def get_head_size_guidance(head_size):
    """Return user-friendly guidance based on head size detection."""
    guidance = {
        "no_face": "No face detected. Please position your face in front of the camera.",
        "too_far": "You are too far from the camera. Please move closer.",
        "small": "Move a bit closer to the camera for better detection.",
        "ideal": "Perfect! Your position is ideal for the photo. Stay still and smile!",
        "large": "You are a bit close. Move back slightly.",
        "too_close": "You are too close to the camera. Please move back.",
    }
    return guidance.get(head_size, "Adjust your position.")


############################################
# Authentication
# - GET /login/ renders the staff login page
# - GET /student/login/ renders the student login page
# - GET /faculty/login/ renders the OSA coordinator login page
# - POST /student/login/ authenticates Student by Student ID
# - POST /staff/login/ authenticates Staff by email/password
# - POST /faculty/login/ authenticates OSA Coordinator by email/password
############################################


def login_view(request):
    """Render the main login page for OSA Coordinator and Student."""
    # Redirect authenticated users to their dashboards
    if request.user.is_authenticated:
        return redirect("violations:route_dashboard")

    # Pull any prefill hints from session
    default_role = request.session.pop("login_prefill_role", None)
    prefill_student_id = request.session.pop("login_prefill_student_id", "")

    ctx = {
        "default_role": default_role,
        "prefill_student_id": prefill_student_id,
    }

    return render(request, "violations/osa_coordinator/login.html", ctx)


def student_login_view(request):
    """Render the student login page; redirects authenticated users to their dashboard."""
    if request.user.is_authenticated:
        return redirect("violations:route_dashboard")
    prefill_student_id = request.session.pop("login_prefill_student_id", "")
    ctx = {
        "prefill_student_id": prefill_student_id,
    }
    return render(request, "violations/student/login.html", ctx)


def faculty_login_view(request):
    """Render the OSA Coordinator login page; redirects authenticated users to their dashboard."""
    if request.user.is_authenticated:
        return redirect("violations:route_dashboard")
    return render(request, "violations/osa_coordinator/login.html", {})


def student_login_auth(request):
    """Authenticate a student using Student ID + password."""

    if request.method != "POST":
        return redirect("violations:student_login")

    student_id = (request.POST.get("student_id") or "").strip()
    password = request.POST.get("password") or ""

    # Require both Student ID and password
    if not student_id or not password:
        messages.error(request, "Please enter your Student ID and password.")
        return render(request, "violations/student/login.html", status=400)

    # Find the student account
    try:
        student = StudentModel.objects.select_related("user").get(student_id=student_id)
    except StudentModel.DoesNotExist:
        messages.error(request, "Access Denied. Invalid Student ID or password.")
        return render(request, "violations/student/login.html", status=401)

    user = student.user

    # Check whether the account is active
    if not user.is_active:
        messages.error(request, "Access Denied. Your account is inactive.")
        return render(request, "violations/student/login.html", status=403)

    # Verify the student's password
    if not user.check_password(password):
        messages.error(request, "Access Denied. Invalid Student ID or password.")
        return render(request, "violations/student/login.html", status=401)

    # Make sure this account is actually a Student account
    if user.role != User.Role.STUDENT:
        messages.error(request, "Access Denied.")
        return render(request, "violations/student/login.html", status=403)

    # Login successful
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    return redirect("violations:route_dashboard")


def credentials_login_auth(request):
    """Authenticate the OSA Coordinator using email + password.

    Students should use Student ID login instead.
    """
    if request.method != "POST":
        return redirect("violations:login")

    # Determine which login template to use based on the role
    # selected_role = (request.POST.get("role") or "faculty").strip().lower()
    login_template = "violations/osa_coordinator/login.html"
    # redirect_login = "violations:faculty_login"

    # Accept multiple possible field names from the UI
    identifier = (
        request.POST.get("username")
        or request.POST.get("faculty_username")
        or request.POST.get("email")
        or request.POST.get("faculty_email")
        or ""
    ).strip()

    password = (
        request.POST.get("password") or request.POST.get("faculty_password") or ""
    )

    if not identifier or not password:
        messages.error(request, "Please provide both email and password.")
        return render(request, login_template, status=400)

    # Try authentication in a robust way:
    # 1) Treat identifier as username directly
    user = authenticate(request, username=identifier, password=password)
    # 2) If that fails and identifier looks like an email, resolve to username by email
    if user is None and "@" in identifier:
        from .models import User as UserModel

        try:
            match = UserModel.objects.get(email__iexact=identifier)
            user = authenticate(request, username=match.username, password=password)
        except UserModel.DoesNotExist:
            user = None
    if user is None:
        messages.error(request, "Invalid email or password.")
        return render(request, login_template, status=401)

    if not user.is_active:
        messages.error(request, "Your account is inactive. Please contact support.")
        return render(request, login_template, status=403)

    # Enforce role: this endpoint is intended for Staff/Faculty (and superusers)
    role = getattr(user, "role", None)
    if not getattr(user, "is_superuser", False) and role == getattr(
        User.Role, "STUDENT", "student"
    ):
        messages.warning(
            request, "Students, please sign in using your Student ID number."
        )
        return redirect("violations:student_login")

    # OSA Coordinator is the administrator role.
    if role != User.Role.OSA_COORDINATOR and not getattr(user, "is_superuser", False):
        messages.error(request, "Access Denied.")
        return render(request, login_template, status=403)

    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect("violations:route_dashboard")


def logout_view(request):
    """Log out the current user and redirect to login (allows GET for convenience)."""
    auth_logout(request)
    return redirect("violations:login")


# def signup_view(request):
# 	"""Signup page; handles Student signup on POST, renders UI on GET."""
# 	if request.method == "POST":
# 		role = request.POST.get("role", "student")

# 		if role != getattr(User.Role, "STUDENT", "student"):
# 			messages.error(request, "Only Student signup is available right now.")
# 			return render(request, "violations/signup.html", status=400)

# 		# Collect basic fields
# 		student_id = (request.POST.get("student_id") or "").strip()
# 		# Always enforce 8 digits immediately
# 		student_id = student_id[:8]
# 		name = (request.POST.get("student_name") or "").strip()
# 		suffix = (request.POST.get("student_suffix") or "").strip()
# 		email = (request.POST.get("student_email") or "").strip().lower()

# 		# Student profile fields
# 		program = (request.POST.get("program") or "").strip()
# 		year_level_raw = (request.POST.get("student_year_level") or "").strip()
# 		department = (request.POST.get("student_department") or "").strip()
# 		enrollment_status = (request.POST.get("student_enrollment_status") or "Active").strip()
# 		guardian_name = (request.POST.get("guardian_name") or "").strip()
# 		guardian_contact = (request.POST.get("guardian_contact") or "").strip()

# 		# Basic validation
# 		missing = [
# 			k for k, v in {
# 				"Student ID": student_id,
# 				"Name": name,
# 				"Email": email,
# 				"Year Level": year_level_raw,
# 				"Program": program,
# 				"Department": department,
# 				"Guardian Name": guardian_name,
# 				"Guardian Contact": guardian_contact,
# 			}.items() if not v
# 		]
# 		if missing:
# 			messages.error(request, f"Please fill in all required fields: {', '.join(missing)}.")
# 			return render(request, "violations/signup.html", status=400)

# 		# Validate student ID is 8 digits
# 		if not student_id.isdigit() or len(student_id) != 8:
# 			messages.error(request, "Student ID must be exactly 8 digits.")
# 			return render(request, "violations/signup.html", status=400)

# 		# Validate guardian contact is 11 digits
# 		if not guardian_contact.isdigit() or len(guardian_contact) != 11:
# 			messages.error(request, "Guardian contact must be exactly 11 digits.")
# 			return render(request, "violations/signup.html", status=400)

# 		try:
# 			year_level = int(year_level_raw)
# 		except ValueError:
# 			messages.error(request, "Year Level must be a number.")
# 			return render(request, "violations/signup.html", status=400)

# 		# Create user and update student profile
# 		try:
# 			with transaction.atomic():
# 				# Generate a random password (user will need to reset via email)
# 				import secrets
# 				random_password = secrets.token_urlsafe(16)

# 				# Use email as username to keep uniqueness simple
# 				username = email or slugify(name) or student_id
# 				user = User.objects.create_user(
# 					username=username,
# 					email=email,
# 					password=random_password,
# 					role=User.Role.STUDENT,
# 				)
# 				# Store name and suffix into first_name for display
# 				full_name = f"{name} {suffix}".strip() if suffix else name
# 				if full_name:
# 					user.first_name = full_name
# 					user.save(update_fields=["first_name"])

# 				# Signals created a Student profile; update it
# 				student = getattr(user, "student_profile", None)
# 				if student is None:
# 					# Fallback if signal didn't fire for some reason
# 					from .models import Student as StudentModel

# 					student = StudentModel.objects.create(user=user, student_id=student_id[:8])

# 				# Defensive: ensure student_id is always 8 digits
# 				student.student_id = student_id[:8]
# 				student.year_level = year_level
# 				student.program = program
# 				student.department = department
# 				student.enrollment_status = enrollment_status
# 				student.guardian_name = guardian_name
# 				student.guardian_contact = guardian_contact
# 				student.save()

# 		except IntegrityError as e:
# 			# Likely duplicate email/username/student_id
# 			messages.error(request, "That email or student ID is already in use. Please try again.")
# 			return render(request, "violations/signup.html", status=400)

# 		# Auto-login and route to role dashboard
# 		login(request, user)
# 		return redirect("violations:route_dashboard")

# 	# GET: render UI
# 	return render(request, "violations/signup.html")


# Staff self-signup removed: staff accounts are created by administrators.


# Note: Faculty signup is not exposed—faculty are managed as superusers.


############################################
# Student (frontend-only)
############################################


@role_required({User.Role.STUDENT})
def student_dashboard_view(request):
    """Student dashboard (UI-only) — restricted to Student role."""
    from django.db.models import Prefetch

    student = getattr(request.user, "student_profile", None)
    # Fetch real violations for this student with prefetched apology letters (latest first)
    violations = (
        Violation.objects.select_related("reported_by", "student")
        .prefetch_related(
            Prefetch(
                "apology_letters",
                queryset=ApologyLetter.objects.order_by("-submitted_at"),
            )
        )
        .filter(student=student)
        .order_by("-created_at")
        if student
        else []
    )
    # Login history for current user
    login_history = LoginActivity.objects.filter(user=request.user).order_by(
        "-timestamp"
    )[:20]
    # Messages from staff/faculty - exclude deleted by receiver (student)
    messages_qs = (
        Message.objects.select_related("sender")
        .filter(receiver=request.user, deleted_by_receiver__isnull=True)
        .order_by("-created_at")
    )
    unread_count = messages_qs.filter(read_at__isnull=True).count()
    staff_messages = messages_qs[:20]
    # Trashed messages (deleted received messages)
    trashed_messages = (
        Message.objects.select_related("sender", "receiver")
        .filter(
            models.Q(sender=request.user, deleted_by_sender__isnull=False)
            | models.Q(receiver=request.user, deleted_by_receiver__isnull=False)
        )
        .order_by("-created_at")[:30]
    )

    # Get active meeting alerts for this student (scheduled or expired)
    scheduled_meeting = None
    if student:
        active_alert = (
            StaffAlert.objects.filter(
                student=student,
                resolved=False,
                meeting_status__in=[
                    StaffAlert.MeetingStatus.SCHEDULED,
                    StaffAlert.MeetingStatus.EXPIRED,
                ],
            )
            .select_related("triggered_violation")
            .order_by("-created_at")
            .first()
        )
        if active_alert and active_alert.scheduled_meeting:
            # Check if meeting has expired
            active_alert.check_meeting_expired()
            scheduled_meeting = {
                "alert": active_alert,
                "datetime": active_alert.scheduled_meeting,
                "notes": active_alert.meeting_notes,
                "status": active_alert.meeting_status,
                "is_expired": active_alert.meeting_status
                == StaffAlert.MeetingStatus.EXPIRED,
                "is_upcoming": (
                    active_alert.scheduled_meeting > timezone.now()
                    if active_alert.meeting_status == StaffAlert.MeetingStatus.SCHEDULED
                    else False
                ),
            }

    ctx = {
        "student": student,
        "violations": violations,
        "login_history": login_history,
        "staff_messages": staff_messages,
        "unread_count": unread_count,
        "trashed_messages": trashed_messages,
        "scheduled_meeting": scheduled_meeting,
    }
    return render(request, "violations/student/dashboard.html", ctx)


@role_required({User.Role.STUDENT})
def student_update_profile_view(request):
    """Student can update their profile photo, contact, guardian info, and email."""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"})

    try:
        student = getattr(request.user, "student_profile", None)
        if not student:
            return JsonResponse(
                {"success": False, "error": "Student profile not found"}
            )

        # Update contact number
        contact_number = request.POST.get("contact_number", "").strip()
        if contact_number:
            student.contact_number = contact_number

        # Update guardian name
        guardian_name = request.POST.get("guardian_name", "").strip()
        if guardian_name:
            student.guardian_name = guardian_name

        # Update guardian contact
        guardian_contact = request.POST.get("guardian_contact", "").strip()
        if guardian_contact:
            student.guardian_contact = guardian_contact

        # Update email (on User model)
        email = request.POST.get("email", "").strip()
        if email and email != request.user.email:
            # Check if email is already taken
            if User.objects.filter(email=email).exclude(pk=request.user.pk).exists():
                return JsonResponse(
                    {"success": False, "error": "This email is already in use"}
                )
            request.user.email = email
            request.user.save(update_fields=["email"])

        # Update profile image
        if "profile_image" in request.FILES:
            profile_image = request.FILES["profile_image"]
            # Validate file size (5MB max)
            if profile_image.size > 5 * 1024 * 1024:
                return JsonResponse(
                    {"success": False, "error": "Image must be less than 5MB"}
                )
            # Validate file type
            allowed_types = ["image/jpeg", "image/png", "image/jpg"]
            if profile_image.content_type not in allowed_types:
                return JsonResponse(
                    {"success": False, "error": "Only JPG and PNG images are allowed"}
                )
            # Delete old image if exists
            if student.profile_image:
                student.profile_image.delete(save=False)
            student.profile_image = profile_image

        student.save()

        return JsonResponse(
            {"success": True, "message": "Profile updated successfully"}
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})


############################################
# OSA Coordinator (Reporting Personnel) - frontend-only
############################################


@role_required({User.Role.OSA_COORDINATOR})
def faculty_dashboard_view(request):
    """OSA Coordinator dashboard — shows oversight stats for all violation cases."""
    from datetime import timedelta

    # Get all violations for oversight (not just reported by this user)
    all_violations_qs = Violation.objects.all()

    # Calculate overdue cases (pending more than 7 days)
    overdue_threshold = timezone.now() - timedelta(days=7)
    overdue_count = all_violations_qs.filter(
        status__in=[Violation.Status.REPORTED, Violation.Status.UNDER_REVIEW],
        created_at__lt=overdue_threshold,
    ).count()

    # Aggregate counts for dashboard stats
    total_reports = all_violations_qs.count()
    reported_count = all_violations_qs.filter(status=Violation.Status.REPORTED).count()
    under_review_count = all_violations_qs.filter(
        status=Violation.Status.UNDER_REVIEW
    ).count()
    resolved_count = all_violations_qs.filter(status=Violation.Status.RESOLVED).count()
    pending_count = reported_count + under_review_count
    latest = all_violations_qs.order_by("-created_at").first()

    # include login history for modal
    # Annotated student directory (violation counts per student)
    from django.db.models import Count, Sum, Case, When, IntegerField

    students_qs = (
        StudentModel.objects.select_related("user")
        .annotate(
            violations_count=Count("violations", distinct=True),
            minor_count=Sum(
                Case(
                    When(violations__type=Violation.Severity.MINOR, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            major_count=Sum(
                Case(
                    When(violations__type=Violation.Severity.MAJOR, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            pending_count=Sum(
                Case(
                    When(
                        violations__status__in=[
                            Violation.Status.REPORTED,
                            Violation.Status.UNDER_REVIEW,
                        ],
                        then=1,
                    ),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
            resolved_count=Sum(
                Case(
                    When(violations__status=Violation.Status.RESOLVED, then=1),
                    default=0,
                    output_field=IntegerField(),
                )
            ),
        )
        .order_by("user__first_name", "user__last_name", "student_id")
    )

    # Server-side pagination
    page = request.GET.get("page", 1)
    paginator = Paginator(students_qs, 25)  # 25 per page
    try:
        students_page = paginator.page(page)
    except PageNotAnInteger:
        students_page = paginator.page(1)
    except EmptyPage:
        students_page = paginator.page(paginator.num_pages)
    login_history = LoginActivity.objects.filter(user=request.user).order_by(
        "-timestamp"
    )[:20]
    # Messages from staff to this faculty - exclude deleted
    staff_messages_qs = (
        Message.objects.select_related("sender")
        .filter(receiver=request.user, deleted_by_receiver__isnull=True)
        .order_by("-created_at")
    )
    unread_count = staff_messages_qs.filter(read_at__isnull=True).count()
    staff_messages = staff_messages_qs[:20]

    # Count staff reports (messages containing VIOLATION REPORT SUMMARY)
    staff_reports_qs = staff_messages_qs.filter(
        content__contains="VIOLATION REPORT SUMMARY"
    )
    staff_reports_count = staff_reports_qs.count()
    unread_reports_count = staff_reports_qs.filter(read_at__isnull=True).count()

    # Trashed messages
    trashed_messages = (
        Message.objects.select_related("sender", "receiver")
        .filter(receiver=request.user, deleted_by_receiver__isnull=False)
        .order_by("-created_at")[:30]
    )
    # Staff alerts for students who reached violation threshold (exclude dismissed)
    staff_alerts = (
        StaffAlert.objects.select_related("student__user", "triggered_violation")
        .filter(resolved=False, dismissed_at__isnull=True)
        .order_by("-created_at")
    )

    # Dismissed/trashed alerts
    trashed_alerts = (
        StaffAlert.objects.select_related(
            "student__user", "triggered_violation", "dismissed_by"
        )
        .filter(dismissed_at__isnull=False)
        .order_by("-dismissed_at")[:30]
    )

    # Check for expired meetings and update status automatically
    for alert in staff_alerts:
        alert.check_meeting_expired()

    ctx = {
        "stats": {
            "total": total_reports,
            "pending": pending_count,
            "reported": reported_count,
            "under_review": under_review_count,
            "pending_for_review": reported_count
            + under_review_count,  # Combined: reported + under_review
            "resolved": resolved_count,
            "overdue": overdue_count,
            "latest_created_at": latest.created_at if latest else None,
            "staff_reports": staff_reports_count,
            "unread_reports": unread_reports_count,
        },
        "login_history": login_history,
        "students": students_page,  # page object
        "paginator": paginator,
        "staff_messages": staff_messages,
        "unread_count": unread_count,
        "trashed_messages": trashed_messages,
        "staff_alerts": staff_alerts,
        "trashed_alerts": trashed_alerts,
    }
    return render(request, "violations/osa_coordinator/dashboard.html", ctx)


@role_required({User.Role.OSA_COORDINATOR})
def faculty_student_detail_view(request, student_id: str):
    """OSA Coordinator: View a student's profile details and violations by student_id (mirrors staff detail)."""
    student = (
        StudentModel.objects.select_related("user")
        .filter(student_id__iexact=student_id)
        .first()
    )
    if not student:
        messages.error(request, "Student not found.")
        return redirect("violations:faculty_dashboard")
    vqs = (
        Violation.objects.select_related("reported_by")
        .filter(student=student)
        .order_by("-created_at")
    )
    total_v = vqs.count()
    pending_v = vqs.filter(
        status__in=[Violation.Status.REPORTED, Violation.Status.UNDER_REVIEW]
    ).count()
    resolved_v = vqs.filter(status=Violation.Status.RESOLVED).count()
    dismissed_v = vqs.filter(status=Violation.Status.DISMISSED).count()
    latest_incident = vqs.first().incident_at if total_v else None

    # CGMC (Certificate of Good Moral Character) eligibility
    cgmc = student.cgmc_eligibility
    
    expired_meetings = StaffAlert.objects.filter(
        student=student,
        meeting_status=StaffAlert.MeetingStatus.EXPIRED,
    ).count()

    pending_meetings = StaffAlert.objects.filter(
        student=student,
        meeting_status=StaffAlert.MeetingStatus.SCHEDULED,
    ).count()   

    ctx = {
        "student": student,
        "violations": vqs,
        "vstats": {
            "total": total_v,
            "pending": pending_v,
            "resolved": resolved_v,
            "dismissed": dismissed_v,
            "latest_incident": latest_incident,
            "major_count": student.major_violation_count,
            "minor_count": student.minor_violation_count,
            "effective_major": student.effective_major_violations,
            "expired_meetings": expired_meetings,
            "pending_meetings": pending_meetings,
        },
        "cgmc": cgmc,
        "good_moral": {
            "status": cgmc["status"],
            "label": cgmc["label"],
            "description": cgmc["description"],
        },
    }

    return render(request, "violations/osa_coordinator/student_detail.html", ctx)


@role_required({User.Role.OSA_COORDINATOR})
def faculty_case_management_view(request):
    """
    OSA Coordinator: Case Management Dashboard
    View all violation cases with comprehensive filtering and oversight capabilities.
    """
    from django.db.models import Q, Count, F
    from datetime import timedelta

    # Get filter parameters
    status_filter = request.GET.get("status", "all")
    type_filter = request.GET.get("type", "all")
    severity_filter = request.GET.get("severity", "all")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    search_query = request.GET.get("search", "")
    semester = request.GET.get("semester", "")

    # Base queryset - all violations with related data
    cases = (
        Violation.objects.select_related(
            "student__user", "reported_by", "violation_type"
        )
        .annotate(days_pending=timezone.now() - F("created_at"))
        .order_by("-created_at")
    )

    # Filter by status
    if status_filter == "pending":
        # Combined: reported + under_review
        cases = cases.filter(
            status__in=[Violation.Status.REPORTED, Violation.Status.UNDER_REVIEW]
        )
    elif status_filter == "overdue":
        # Overdue: pending more than 7 days
        overdue_threshold = timezone.now() - timedelta(days=7)
        cases = cases.filter(
            status__in=[Violation.Status.REPORTED, Violation.Status.UNDER_REVIEW],
            created_at__lt=overdue_threshold,
        )
    elif status_filter != "all":
        cases = cases.filter(status=status_filter)

    # Filter by violation type
    if type_filter != "all":
        cases = cases.filter(type=type_filter)

    # Filter by severity
    if severity_filter == "minor":
        cases = cases.filter(type=Violation.Severity.MINOR)
    elif severity_filter == "major":
        cases = cases.filter(type=Violation.Severity.MAJOR)

    # Filter by date range
    if date_from:
        try:
            from datetime import datetime

            date_from_parsed = datetime.strptime(date_from, "%Y-%m-%d")
            cases = cases.filter(created_at__date__gte=date_from_parsed)
        except ValueError:
            pass

    if date_to:
        try:
            from datetime import datetime

            date_to_parsed = datetime.strptime(date_to, "%Y-%m-%d")
            cases = cases.filter(created_at__date__lte=date_to_parsed)
        except ValueError:
            pass

    # Search filter
    if search_query:
        cases = cases.filter(
            Q(student__student_id__icontains=search_query)
            | Q(student__user__first_name__icontains=search_query)
            | Q(student__user__last_name__icontains=search_query)
            | Q(description__icontains=search_query)
            | Q(location__icontains=search_query)
        )

    # Identify overdue cases (pending more than 7 days)
    overdue_threshold = timezone.now() - timedelta(days=7)
    overdue_cases = cases.filter(
        status__in=[Violation.Status.REPORTED, Violation.Status.UNDER_REVIEW],
        created_at__lt=overdue_threshold,
    )

    # Pagination
    from django.core.paginator import Paginator

    paginator = Paginator(cases, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Summary statistics
    stats = {
        "total_cases": Violation.objects.count(),
        "pending": Violation.objects.filter(status=Violation.Status.REPORTED).count(),
        "under_review": Violation.objects.filter(
            status=Violation.Status.UNDER_REVIEW
        ).count(),
        "resolved": Violation.objects.filter(status=Violation.Status.RESOLVED).count(),
        "dismissed": Violation.objects.filter(
            status=Violation.Status.DISMISSED
        ).count(),
        "overdue": overdue_cases.count(),
        "today": Violation.objects.filter(
            created_at__date=timezone.now().date()
        ).count(),
        "this_week": Violation.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=7)
        ).count(),
        "minor_violations": Violation.objects.filter(
            type=Violation.Severity.MINOR
        ).count(),
        "major_violations": Violation.objects.filter(
            type=Violation.Severity.MAJOR
        ).count(),
    }

    context = {
        "page_obj": page_obj,
        "cases": page_obj,
        "stats": stats,
        "violation_types": Violation.Severity.choices,
        "violation_statuses": Violation.Status.choices,
        "current_filters": {
            "status": status_filter,
            "type": type_filter,
            "severity": severity_filter,
            "date_from": date_from,
            "date_to": date_to,
            "search": search_query,
            "semester": semester,
        },
        "overdue_count": overdue_cases.count(),
    }

    return render(request, "violations/osa_coordinator/case_management.html", context)


@role_required({User.Role.OSA_COORDINATOR})
def osa_report_violation_view(request):
    """OSA Coordinator: record a violation for an existing student."""

    if request.method == "POST":
        student_id = (request.POST.get("student_id") or "").strip()

        if not student_id:
            messages.error(request, "Student ID is required.")
            return render(
                request,
                "violations/osa_coordinator/report_form.html",
                status=400,
            )

        try:
            student = StudentModel.objects.select_related("user").get(
                student_id__iexact=student_id
            )
        except StudentModel.DoesNotExist:
            messages.error(
                request,
                "Access Denied. Student ID was not found.",
            )
            return render(
                request,
                "violations/osa_coordinator/report_form.html",
                status=404,
            )

        description = (request.POST.get("description") or "").strip()
        violation_type_id = (request.POST.get("violation_type_id") or "").strip()
        violation_type = (request.POST.get("type") or "").strip()
        location = (request.POST.get("location") or "").strip()

        incident_date = (request.POST.get("incident_date") or "").strip()
        incident_time = (request.POST.get("incident_time") or "").strip()

        if not description:
            messages.error(
                request,
                "Violation description is required.",
            )
            return render(
                request,
                "violations/osa_coordinator/report_form.html",
                status=400,
            )

        catalog_violation_type = None

        if violation_type_id:
            catalog_violation_type = ViolationType.objects.filter(
                id=violation_type_id,
                is_active=True,
            ).first()

        if not catalog_violation_type:
            messages.error(
                request,
                "Please select a valid violation type.",
            )
            return render(
                request,
                "violations/osa_coordinator/report_form.html",
                status=400,
            )

        incident_at = timezone.now()

        if incident_date:
            try:
                if incident_time:
                    incident_at = datetime.strptime(
                        f"{incident_date} {incident_time}",
                        "%Y-%m-%d %H:%M",
                    )
                else:
                    incident_at = datetime.strptime(
                        incident_date,
                        "%Y-%m-%d",
                    )

                incident_at = timezone.make_aware(incident_at)

            except ValueError:
                messages.error(
                    request,
                    "Invalid incident date or time.",
                )
                return render(
                    request,
                    "violations/osa_coordinator/report_form.html",
                    status=400,
                )

        violation = Violation.objects.create(
            student=student,
            reported_by=request.user,
            description=description,
            type=violation_type or Violation.Severity.MINOR,
            violation_type=catalog_violation_type,
            location=location or "Not specified",
            incident_at=incident_at,
            status=Violation.Status.REPORTED,
        )

        ActivityLog.log_activity(
            action_type=ActivityLog.ActionType.VIOLATION_REPORTED,
            description=(
                f"Violation #{violation.id} recorded for "
                f"Student ID {student.student_id}"
            ),
            request=request,
            user=request.user,
            related_violation=violation,
            related_student=student,
        )

        messages.success(
            request,
            f"Violation #{violation.id} recorded successfully.",
        )

        return redirect("violations:faculty_case_management")

    students = StudentModel.objects.select_related("user").all().order_by("student_id")

    violation_types = ViolationType.objects.filter(is_active=True).order_by(
        "category", "name"
    )

    context = {
        "students": students,
        "type_choices": Violation.Severity.choices,
        "violation_types": violation_types,
    }

    return render(
        request,
        "violations/osa_coordinator/report_form.html",
        context,
    )


@role_required({User.Role.OSA_COORDINATOR})
def faculty_update_case_status_view(request):
    """OSA Coordinator: Update violation case status via AJAX."""
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "error": "Invalid request method"}, status=405
        )

    violation_id = request.POST.get("violation_id")
    new_status = request.POST.get("status")

    if not violation_id or not new_status:
        return JsonResponse(
            {"success": False, "error": "Missing violation_id or status"}, status=400
        )

    # Validate status is a valid choice
    valid_statuses = [choice[0] for choice in Violation.Status.choices]
    if new_status not in valid_statuses:
        return JsonResponse(
            {"success": False, "error": "Invalid status value"}, status=400
        )

    try:
        violation = Violation.objects.select_related("student", "student__user").get(
            id=violation_id
        )
        old_status = violation.get_status_display()
        violation.status = new_status
        violation.save()

        # Log the activity
        from .models import ActivityLog

        student_name = (
            violation.student.user.get_full_name() or violation.student.student_id
        )
        new_status_display = violation.get_status_display()
        ActivityLog.log_activity(
            action_type=ActivityLog.ActionType.VIOLATION_UPDATED,
            description=f"Updated case #{violation.id} status: {old_status} → {new_status_display} for {student_name}",
            request=request,
            user=request.user,
            related_student=violation.student,
            related_violation=violation,
        )

        return JsonResponse(
            {
                "success": True,
                "message": f"Case #{violation.id} status updated to {new_status_display}",
                "new_status": new_status,
                "new_status_display": new_status_display,
            }
        )
    except Violation.DoesNotExist:
        return JsonResponse(
            {"success": False, "error": "Violation not found"}, status=404
        )
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# Commenting out old report view - OSA Coordinator should oversee, not report directly
# @role_required({User.Role.OSA_COORDINATOR})
# def faculty_report_view(request):
# 	\"\"\"OSA Coordinator: Report Violation form — supports GET (form) and POST (create).\"\"\"
# 	# This view is deprecated - OSA Coordinator role is for oversight, not direct reporting
# 	pass


@role_required({User.Role.OSA_COORDINATOR})
def faculty_my_reports_view(request):
    """OSA Coordinator: My Reported Violations list — restricted to OSA Coordinator."""
    my_reports = (
        Violation.objects.select_related("student__user")
        .filter(reported_by=request.user)
        .order_by("-created_at")
    )
    return render(
        request, "violations/osa_coordinator/my_reports.html", {"reports": my_reports}
    )


@role_required({User.Role.OSA_COORDINATOR})
def faculty_activity_logs_view(request):
    """
    OSA Coordinator: View all activity logs across the system.
    Shows activities from OSA Coordinator and Students.
    """
    from .models import ActivityLog

    # Get filter parameters
    user_role = request.GET.get("role", "all")
    action_filter = request.GET.get("action", "all")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    search_query = request.GET.get("search", "")

    # Base queryset - all activity logs
    logs = ActivityLog.objects.select_related(
        "user",
        "related_student",
        "related_student__user",
        "related_violation",
        "related_apology",
    ).order_by("-timestamp")

    # Filter by user role
    if user_role == "student":
        logs = logs.filter(user__role=User.Role.STUDENT)
    elif user_role == "osa_coordinator":
        logs = logs.filter(user__role=User.Role.OSA_COORDINATOR)

    # Filter by action type
    if action_filter != "all":
        logs = logs.filter(action_type=action_filter)

    # Filter by date range
    if date_from:
        try:
            from datetime import datetime

            date_from_parsed = datetime.strptime(date_from, "%Y-%m-%d")
            logs = logs.filter(timestamp__date__gte=date_from_parsed)
        except ValueError:
            pass

    if date_to:
        try:
            from datetime import datetime

            date_to_parsed = datetime.strptime(date_to, "%Y-%m-%d")
            logs = logs.filter(timestamp__date__lte=date_to_parsed)
        except ValueError:
            pass

    # Search filter
    if search_query:
        logs = logs.filter(
            Q(description__icontains=search_query)
            | Q(user__username__icontains=search_query)
            | Q(user__first_name__icontains=search_query)
            | Q(user__last_name__icontains=search_query)
            # Q(guard_code__icontains=search_query) |
        )

    # Pagination
    from django.core.paginator import Paginator

    paginator = Paginator(logs, 25)  # 25 logs per page
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Get action types for filter dropdown
    action_types = ActivityLog.ActionType.choices

    # Summary statistics
    today = timezone.now().date()
    stats = {
        "total_logs": ActivityLog.objects.count(),
        "today_logs": ActivityLog.objects.filter(timestamp__date=today).count(),
        "osa_coordinator_logs": ActivityLog.objects.filter(
            user__role=User.Role.OSA_COORDINATOR
        ).count(),
        "student_logs": ActivityLog.objects.filter(
            user__role=User.Role.STUDENT
        ).count(),
    }

    context = {
        "page_obj": page_obj,
        "logs": page_obj,
        "action_types": action_types,
        "stats": stats,
        "current_filters": {
            "role": user_role,
            "action": action_filter,
            "date_from": date_from,
            "date_to": date_to,
            "search": search_query,
        },
    }

    return render(request, "violations/osa_coordinator/activity_logs.html", context)


@role_required({User.Role.OSA_COORDINATOR})
def faculty_delete_activity_log_view(request, log_id):
    """
    OSA Coordinator: Delete an activity log entry.
    """
    from .models import ActivityLog

    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        log = ActivityLog.objects.get(id=log_id)
        log.delete()
        return JsonResponse(
            {"success": True, "message": "Activity log deleted successfully"}
        )
    except ActivityLog.DoesNotExist:
        return JsonResponse({"error": "Activity log not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@role_required({User.Role.OSA_COORDINATOR})
def faculty_analytics_view(request):
    """
    OSA Coordinator Analytics Page - Full standalone page view.
    Renders the analytics dashboard page that loads data via AJAX.
    """
    return render(request, "violations/osa_coordinator/osa_coordinator_analytics.html")


@role_required({User.Role.OSA_COORDINATOR})
def faculty_analytics_api(request):
    """
    Analytics API for OSA Coordinator Dashboard.

    Returns JSON data for:
    1. Violation Trends Over Time (daily counts for last 30 days)
    2. Violation Type Breakdown (counts by Major/Minor)
    3. Violation Status Distribution
    4. Top Violation Types from ViolationType catalog
    5. Department Distribution

    Analytics Type: Descriptive + Basic Diagnostic
    No prediction, no complex math - just aggregation and summarization.
    """
    from django.db.models import Count
    from django.db.models.functions import TruncDate, TruncWeek
    from datetime import timedelta

    today = timezone.now().date()
    thirty_days_ago = today - timedelta(days=30)
    seven_days_ago = today - timedelta(days=7)

    # 1. Daily Violation Trends (last 30 days)
    daily_trends = (
        Violation.objects.filter(created_at__date__gte=thirty_days_ago)
        .annotate(date=TruncDate("created_at"))
        .values("date")
        .annotate(count=Count("id"))
        .order_by("date")
    )

    # Fill in missing dates with zero counts
    trends_dict = {item["date"]: item["count"] for item in daily_trends}
    trend_labels = []
    trend_data = []
    for i in range(30):
        date = thirty_days_ago + timedelta(days=i)
        trend_labels.append(date.strftime("%b %d"))
        trend_data.append(trends_dict.get(date, 0))

    # 2. Violation Type Breakdown (Major vs Minor)
    type_breakdown = (
        Violation.objects.values("type").annotate(count=Count("id")).order_by("type")
    )
    type_labels = []
    type_data = []
    type_colors = []
    for item in type_breakdown:
        if item["type"] == "major":
            type_labels.append("Major Offense")
            type_colors.append("#dc2626")  # Red
        else:
            type_labels.append("Minor Offense")
            type_colors.append("#f59e0b")  # Amber
        type_data.append(item["count"])

    # If no data, provide defaults
    if not type_labels:
        type_labels = ["Major Offense", "Minor Offense"]
        type_data = [0, 0]
        type_colors = ["#dc2626", "#f59e0b"]

    # 3. Violation Status Distribution
    status_breakdown = (
        Violation.objects.values("status")
        .annotate(count=Count("id"))
        .order_by("status")
    )
    status_labels = []
    status_data = []
    status_colors = []
    status_color_map = {
        "reported": ("#3b82f6", "Reported"),
        "under_review": ("#f59e0b", "Under Review"),
        "resolved": ("#10b981", "Resolved"),
        "dismissed": ("#6b7280", "Dismissed"),
    }
    for item in status_breakdown:
        color, label = status_color_map.get(item["status"], ("#9ca3af", item["status"]))
        status_labels.append(label)
        status_data.append(item["count"])
        status_colors.append(color)

    # 4. Top Violation Types from ViolationType catalog
    top_violation_types = (
        Violation.objects.filter(violation_type__isnull=False)
        .values("violation_type__name", "violation_type__category")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )
    vtype_labels = []
    vtype_data = []
    vtype_colors = []
    for item in top_violation_types:
        name = item["violation_type__name"]
        if len(name) > 25:
            name = name[:22] + "..."
        vtype_labels.append(name)
        vtype_data.append(item["count"])
        if item["violation_type__category"] == "major":
            vtype_colors.append("#dc2626")
        else:
            vtype_colors.append("#f59e0b")

    # 5. Department Distribution
    dept_breakdown = (
        Violation.objects.filter(student__department__isnull=False)
        .exclude(student__department="")
        .values("student__department")
        .annotate(count=Count("id"))
        .order_by("-count")[:8]
    )
    dept_labels = []
    dept_data = []
    dept_colors = []

    # Official UDM College/Department Colors
    college_colors = {
        "CAS": "#22c55e",  # Green - College of Arts and Sciences
        "CBMA": "#eab308",  # Yellow/Gold - College of Business Management and Accountancy
        "CCS": "#6b7280",  # Gray - College of Computer Studies
        "COEd": "#3b82f6",  # Blue - College of Education
        "CIT": "#ef4444",  # Red - College of Industrial Technology
        "COE": "#f97316",  # Orange - College of Engineering
        "CON": "#ec4899",  # Pink - College of Nursing
        "CTHM": "#8b5cf6",  # Purple - College of Tourism and Hospitality Management
        "CAF": "#14b8a6",  # Teal - College of Agriculture and Fishery
        "CGS": "#1a472a",  # Dark Green - College of Graduate Studies
    }
    default_color = "#64748b"  # Slate gray for unknown departments

    for item in dept_breakdown:
        dept = item["student__department"]
        display_dept = dept if len(dept) <= 15 else dept[:12] + "..."
        dept_labels.append(display_dept)
        dept_data.append(item["count"])
        # Get color based on department code
        dept_colors.append(college_colors.get(dept, default_color))

    # 6. Summary Statistics
    total_violations = Violation.objects.count()
    total_major = Violation.objects.filter(type="major").count()
    total_minor = Violation.objects.filter(type="minor").count()
    total_pending = Violation.objects.filter(
        status__in=["reported", "under_review"]
    ).count()
    total_resolved = Violation.objects.filter(status="resolved").count()
    this_week_count = Violation.objects.filter(
        created_at__date__gte=seven_days_ago
    ).count()
    this_month_count = Violation.objects.filter(
        created_at__date__gte=thirty_days_ago
    ).count()

    # 7. Weekly Comparison (this week vs last week)
    last_week_start = seven_days_ago - timedelta(days=7)
    last_week_count = Violation.objects.filter(
        created_at__date__gte=last_week_start, created_at__date__lt=seven_days_ago
    ).count()

    if last_week_count > 0:
        week_change_percent = round(
            ((this_week_count - last_week_count) / last_week_count) * 100, 1
        )
    else:
        week_change_percent = 100 if this_week_count > 0 else 0

    analytics_data = {
        "success": True,
        "generated_at": timezone.now().isoformat(),
        "summary": {
            "total_violations": total_violations,
            "total_major": total_major,
            "total_minor": total_minor,
            "total_pending": total_pending,
            "total_resolved": total_resolved,
            "this_week": this_week_count,
            "this_month": this_month_count,
            "week_change_percent": week_change_percent,
        },
        "trends": {
            "labels": trend_labels,
            "data": trend_data,
        },
        "type_breakdown": {
            "labels": type_labels,
            "data": type_data,
            "colors": type_colors,
        },
        "status_breakdown": {
            "labels": status_labels,
            "data": status_data,
            "colors": status_colors,
        },
        "top_violation_types": {
            "labels": vtype_labels,
            "data": vtype_data,
            "colors": vtype_colors,
        },
        "department_breakdown": {
            "labels": dept_labels,
            "data": dept_data,
            "colors": dept_colors,
        },
        # 7. Diagnostic Analytics - "Why did it happen?"
        "diagnostic": generate_diagnostic_analytics(
            total_violations=total_violations,
            total_major=total_major,
            total_minor=total_minor,
            total_pending=total_pending,
            total_resolved=total_resolved,
            week_change_percent=week_change_percent,
            top_violation_types=list(top_violation_types),
            dept_breakdown=list(dept_breakdown),
        ),
        # 8. Prescriptive Analytics - Rule-based recommendations
        "prescriptive": generate_prescriptive_recommendations(
            total_violations=total_violations,
            total_major=total_major,
            total_minor=total_minor,
            total_pending=total_pending,
            week_change_percent=week_change_percent,
            top_violation_types=list(top_violation_types),
            dept_breakdown=list(dept_breakdown),
        ),
    }

    return JsonResponse(analytics_data)


def generate_diagnostic_analytics(
    total_violations,
    total_major,
    total_minor,
    total_pending,
    total_resolved,
    week_change_percent,
    top_violation_types,
    dept_breakdown,
):
    """
    Generate diagnostic analytics - "Why did it happen?"

    Analyzes data to find causes, patterns, and contributing factors
    behind violation outcomes without using predictive algorithms.

    Returns a dictionary containing:
    - root_causes: Identified patterns and potential causes
    - correlations: Relationships between different factors
    - time_patterns: Temporal analysis insights
    - hotspots: Location/department concentrations
    """
    from datetime import timedelta
    from django.db.models import Count, Q
    from django.db.models.functions import ExtractHour, ExtractWeekDay, TruncMonth

    diagnostic = {
        "root_causes": [],
        "correlations": [],
        "time_patterns": [],
        "hotspots": [],
        "summary": "",
    }

    # === ROOT CAUSE ANALYSIS ===

    # Analyze top violation type patterns
    if top_violation_types:
        top_vtype = top_violation_types[0]
        vtype_name = top_vtype.get("violation_type__name", "Unknown")
        vtype_count = top_vtype.get("count", 0)
        vtype_category = top_vtype.get("violation_type__category", "minor")

        if total_violations > 0:
            vtype_percentage = (vtype_count / total_violations) * 100

            if vtype_percentage > 30:
                cause_analysis = {
                    "icon": "fa-magnifying-glass-chart",
                    "title": f"Dominant Violation Pattern: {vtype_name}",
                    "percentage": f"{vtype_percentage:.1f}%",
                    "finding": f"This single violation type accounts for {vtype_percentage:.1f}% of all cases.",
                    "possible_causes": [],
                }

                # Determine possible causes based on violation type
                vtype_lower = vtype_name.lower()
                if any(
                    kw in vtype_lower for kw in ["uniform", "dress", "attire", "id"]
                ):
                    cause_analysis["possible_causes"] = [
                        "Students may not fully understand dress code requirements",
                        "Uniform availability or affordability issues",
                        "Inconsistent enforcement creating confusion",
                        "Morning rush leading to oversight",
                    ]
                elif any(
                    kw in vtype_lower for kw in ["late", "tardy", "absent", "cutting"]
                ):
                    cause_analysis["possible_causes"] = [
                        "Transportation or commute challenges",
                        "Class schedule conflicts with personal obligations",
                        "Lack of engagement with course content",
                        "Health or personal issues affecting attendance",
                    ]
                elif any(kw in vtype_lower for kw in ["smok", "vape"]):
                    cause_analysis["possible_causes"] = [
                        "Peer influence and social pressure",
                        "Stress-coping mechanisms",
                        "Designated smoking areas may be unclear",
                        "Need for cessation support programs",
                    ]
                elif any(
                    kw in vtype_lower for kw in ["disrespect", "misconduct", "behavior"]
                ):
                    cause_analysis["possible_causes"] = [
                        "Communication or conflict resolution skill gaps",
                        "Stress or personal problems affecting behavior",
                        "Unclear behavioral expectations",
                        "Need for counseling or mediation services",
                    ]
                else:
                    cause_analysis["possible_causes"] = [
                        "Policy awareness may need reinforcement",
                        "Environmental factors in specific areas",
                        "Student orientation gaps",
                        "Need for targeted information campaigns",
                    ]

                diagnostic["root_causes"].append(cause_analysis)

    # Analyze major vs minor ratio
    if total_violations > 0:
        major_ratio = (total_major / total_violations) * 100
        minor_ratio = (total_minor / total_violations) * 100

        if major_ratio > 40:
            diagnostic["root_causes"].append(
                {
                    "icon": "fa-triangle-exclamation",
                    "title": "High Major Offense Concentration",
                    "percentage": f"{major_ratio:.1f}%",
                    "finding": "Major offenses are disproportionately high, suggesting serious behavioral concerns.",
                    "possible_causes": [
                        "Escalation from unaddressed minor violations",
                        "Insufficient preventive interventions",
                        "Possible gaps in policy understanding",
                        "Environmental factors enabling serious misconduct",
                    ],
                }
            )
        elif minor_ratio > 80:
            diagnostic["root_causes"].append(
                {
                    "icon": "fa-clipboard-check",
                    "title": "Minor Offense Predominance",
                    "percentage": f"{minor_ratio:.1f}%",
                    "finding": "Most violations are minor, indicating good overall discipline with areas for improvement.",
                    "possible_causes": [
                        "Policy compliance generally understood",
                        "Minor lapses often related to convenience or awareness",
                        "Effective deterrence against major offenses",
                    ],
                }
            )

    # === TIME PATTERN ANALYSIS ===

    # Day of week analysis
    dow_data = (
        Violation.objects.values(dow=ExtractWeekDay("incident_at"))
        .annotate(count=Count("id"))
        .order_by("dow")
    )

    dow_names = [
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    ]
    dow_counts = {item["dow"]: item["count"] for item in dow_data}

    if dow_counts:
        max_dow = max(dow_counts, key=dow_counts.get)
        max_count = dow_counts[max_dow]
        # Django ExtractDayOfWeek: 1=Sunday, 2=Monday, etc.
        max_day_name = dow_names[max_dow - 1] if 1 <= max_dow <= 7 else "Unknown"

        avg_count = sum(dow_counts.values()) / len(dow_counts) if dow_counts else 0

        if max_count > avg_count * 1.3:  # 30% above average
            diagnostic["time_patterns"].append(
                {
                    "icon": "fa-calendar-day",
                    "title": f"{max_day_name}s Show Higher Violations",
                    "finding": f"{max_day_name}s have {max_count} violations, above the daily average.",
                    "insight": f"Consider increased monitoring or awareness campaigns targeting {max_day_name}s.",
                    "data": {
                        "labels": dow_names,
                        "values": [dow_counts.get(i + 1, 0) for i in range(7)],
                    },
                }
            )

    # Hour of day analysis (common hours)
    hour_data = (
        Violation.objects.values(hour=ExtractHour("incident_at"))
        .annotate(count=Count("id"))
        .order_by("-count")[:3]
    )

    if hour_data:
        peak_hours = []
        for item in hour_data:
            hour = item["hour"]
            count = item["count"]
            if hour < 12:
                time_str = f"{hour}:00 AM" if hour > 0 else "12:00 AM"
            elif hour == 12:
                time_str = "12:00 PM"
            else:
                time_str = f"{hour-12}:00 PM"
            peak_hours.append({"time": time_str, "count": count})

        if peak_hours:
            diagnostic["time_patterns"].append(
                {
                    "icon": "fa-clock",
                    "title": "Peak Violation Hours",
                    "finding": f'Most violations occur around {peak_hours[0]["time"]}.',
                    "insight": "Deploy additional monitoring during peak hours.",
                    "peak_hours": peak_hours,
                }
            )

    # === DEPARTMENT/LOCATION HOTSPOTS ===

    if dept_breakdown:
        total_dept_violations = sum(d.get("count", 0) for d in dept_breakdown)

        for dept in dept_breakdown[:3]:  # Top 3 departments
            dept_name = dept.get("student__department", "Unknown")
            dept_count = dept.get("count", 0)

            if total_dept_violations > 0:
                dept_percentage = (dept_count / total_dept_violations) * 100

                diagnostic["hotspots"].append(
                    {
                        "icon": "fa-building",
                        "department": dept_name,
                        "count": dept_count,
                        "percentage": f"{dept_percentage:.1f}%",
                        "rank": len(diagnostic["hotspots"]) + 1,
                    }
                )

    # Location analysis
    location_data = (
        Violation.objects.exclude(location="")
        .exclude(location__isnull=True)
        .values("location")
        .annotate(count=Count("id"))
        .order_by("-count")[:5]
    )

    if location_data:
        for loc in location_data:
            if loc["count"] >= 3:  # Minimum threshold
                diagnostic["hotspots"].append(
                    {
                        "icon": "fa-location-dot",
                        "location": loc["location"][:40],
                        "count": loc["count"],
                        "type": "location",
                    }
                )

    # === CORRELATIONS ===

    # Check if certain departments have more major violations
    dept_major = (
        Violation.objects.filter(type="major", student__department__isnull=False)
        .exclude(student__department="")
        .values("student__department")
        .annotate(count=Count("id"))
        .order_by("-count")[:3]
    )

    if dept_major:
        top_major_dept = dept_major[0]
        if top_major_dept["count"] >= 3:
            diagnostic["correlations"].append(
                {
                    "icon": "fa-link",
                    "title": "Department-Severity Correlation",
                    "finding": f'{top_major_dept["student__department"]} has the highest major offense count ({top_major_dept["count"]} cases).',
                    "insight": "Targeted intervention may be needed for this department.",
                }
            )

    # Resolution rate correlation
    if total_violations > 0:
        resolution_rate = (total_resolved / total_violations) * 100
        pending_rate = (total_pending / total_violations) * 100

        if pending_rate > 30:
            diagnostic["correlations"].append(
                {
                    "icon": "fa-hourglass-half",
                    "title": "Processing Bottleneck Detected",
                    "finding": f"{pending_rate:.1f}% of cases are still pending resolution.",
                    "insight": "High pending rate may indicate staffing or process constraints.",
                }
            )

    # === SUMMARY ===

    findings_count = (
        len(diagnostic["root_causes"])
        + len(diagnostic["time_patterns"])
        + len(diagnostic["hotspots"])
        + len(diagnostic["correlations"])
    )

    if findings_count == 0:
        diagnostic["summary"] = (
            "No significant patterns or anomalies detected. Continue standard monitoring."
        )
    elif findings_count <= 2:
        diagnostic["summary"] = (
            "Limited patterns identified. Focus on the highlighted areas for improvement."
        )
    else:
        diagnostic["summary"] = (
            f"Multiple patterns identified ({findings_count} findings). Review each area for comprehensive intervention planning."
        )

    return diagnostic


def generate_prescriptive_recommendations(
    total_violations,
    total_major,
    total_minor,
    total_pending,
    week_change_percent,
    top_violation_types,
    dept_breakdown,
):
    """
    Generate prescriptive analytics recommendations based on violation data.

    This function performs rule-based analysis to provide actionable recommendations
    without using predictive algorithms or automated decision-making.

    Returns a dictionary containing:
    - priority_actions: Immediate actions needed
    - prevention_strategies: Long-term prevention recommendations
    - department_focus: Department-specific recommendations
    - violation_insights: Insights about specific violation types
    """
    recommendations = {
        "priority_actions": [],
        "prevention_strategies": [],
        "department_focus": [],
        "violation_insights": [],
        "summary_recommendation": "",
    }

    # === PRIORITY ACTIONS (Immediate concerns) ===

    # High pending cases
    if total_pending > 10:
        recommendations["priority_actions"].append(
            {
                "icon": "fa-clock",
                "severity": "high",
                "title": "High Volume of Pending Cases",
                "description": f"There are {total_pending} cases awaiting review. Consider scheduling a dedicated review session.",
                "action": "Schedule case review meeting within 3 days",
            }
        )
    elif total_pending > 5:
        recommendations["priority_actions"].append(
            {
                "icon": "fa-hourglass-half",
                "severity": "medium",
                "title": "Moderate Pending Cases",
                "description": f"{total_pending} cases are pending. Regular review recommended.",
                "action": "Review pending cases this week",
            }
        )

    # Week-over-week increase
    if week_change_percent > 50:
        recommendations["priority_actions"].append(
            {
                "icon": "fa-arrow-trend-up",
                "severity": "high",
                "title": "Significant Increase in Violations",
                "description": f"Violations increased by {week_change_percent}% compared to last week.",
                "action": "Investigate root causes and increase monitoring",
            }
        )
    elif week_change_percent > 20:
        recommendations["priority_actions"].append(
            {
                "icon": "fa-chart-line",
                "severity": "medium",
                "title": "Notable Increase in Violations",
                "description": f"Violations increased by {week_change_percent}% from last week.",
                "action": "Monitor trends and identify contributing factors",
            }
        )

    # Major violations ratio
    if total_violations > 0:
        major_ratio = (total_major / total_violations) * 100
        if major_ratio > 40:
            recommendations["priority_actions"].append(
                {
                    "icon": "fa-exclamation-triangle",
                    "severity": "high",
                    "title": "High Major Offense Ratio",
                    "description": f"{major_ratio:.1f}% of violations are major offenses. This requires immediate attention.",
                    "action": "Conduct disciplinary review and strengthen enforcement",
                }
            )
        elif major_ratio > 25:
            recommendations["priority_actions"].append(
                {
                    "icon": "fa-balance-scale",
                    "severity": "medium",
                    "title": "Elevated Major Offense Rate",
                    "description": f"{major_ratio:.1f}% of violations are major offenses.",
                    "action": "Review major offense handling procedures",
                }
            )

    # === PREVENTION STRATEGIES (Long-term recommendations) ===

    # Based on top violation types
    if top_violation_types:
        top_violation = top_violation_types[0]
        vtype_name = top_violation.get("violation_type__name", "Unknown")
        vtype_count = top_violation.get("count", 0)
        vtype_category = top_violation.get("violation_type__category", "minor")

        # Dress code related
        if any(
            kw in vtype_name.lower()
            for kw in ["uniform", "dress", "attire", "id", "identification"]
        ):
            recommendations["prevention_strategies"].append(
                {
                    "icon": "fa-tshirt",
                    "category": "Dress Code",
                    "title": "Strengthen Dress Code Awareness",
                    "description": f'"{vtype_name}" is the most common violation ({vtype_count} cases).',
                    "actions": [
                        "Post visual reminders of dress code policy at entrances",
                        "Conduct orientation on proper uniform/attire guidelines",
                        "Consider peer reminder programs before class hours",
                    ],
                }
            )

        # Attendance/tardiness related
        elif any(
            kw in vtype_name.lower()
            for kw in ["late", "tardy", "absent", "attendance", "cutting"]
        ):
            recommendations["prevention_strategies"].append(
                {
                    "icon": "fa-clock",
                    "category": "Attendance",
                    "title": "Address Attendance Issues",
                    "description": f'"{vtype_name}" accounts for {vtype_count} violations.',
                    "actions": [
                        "Review class scheduling for potential conflicts",
                        "Implement early warning system for chronic absenteeism",
                        "Engage with students showing attendance patterns",
                    ],
                }
            )

        # Behavioral issues
        elif any(
            kw in vtype_name.lower()
            for kw in ["disrespect", "misconduct", "behavior", "fight", "bully"]
        ):
            recommendations["prevention_strategies"].append(
                {
                    "icon": "fa-users",
                    "category": "Behavior",
                    "title": "Behavioral Intervention Needed",
                    "description": f'"{vtype_name}" is a recurring issue ({vtype_count} cases).',
                    "actions": [
                        "Implement conflict resolution workshops",
                        "Establish peer mediation programs",
                        "Increase counselor availability",
                    ],
                }
            )

        # Smoking/substance related
        elif any(
            kw in vtype_name.lower()
            for kw in ["smok", "vape", "alcohol", "drug", "substance"]
        ):
            recommendations["prevention_strategies"].append(
                {
                    "icon": "fa-ban-smoking",
                    "category": "Health & Safety",
                    "title": "Substance-Related Violations",
                    "description": f'"{vtype_name}" requires health-focused intervention ({vtype_count} cases).',
                    "actions": [
                        "Coordinate with health services for awareness campaigns",
                        "Strengthen enforcement in designated areas",
                        "Offer counseling resources for affected students",
                    ],
                }
            )

        # Default recommendation
        else:
            recommendations["prevention_strategies"].append(
                {
                    "icon": "fa-lightbulb",
                    "category": "General",
                    "title": (
                        f'Address "{vtype_name[:30]}..."'
                        if len(vtype_name) > 30
                        else f'Address "{vtype_name}"'
                    ),
                    "description": f"This violation type has {vtype_count} recorded cases.",
                    "actions": [
                        "Review policy clarity and student awareness",
                        "Conduct targeted information sessions",
                        "Monitor for patterns and contributing factors",
                    ],
                }
            )

    # === DEPARTMENT-SPECIFIC FOCUS ===

    if dept_breakdown:
        # Top department with most violations
        top_dept = dept_breakdown[0]
        dept_name = top_dept.get("student__department", "Unknown")
        dept_count = top_dept.get("count", 0)

        # Determine severity based on violation count and comparison
        severity = "medium"
        if len(dept_breakdown) > 1:
            second_dept = dept_breakdown[1]
            second_count = second_dept.get("count", 0)
            if dept_count > second_count * 2:
                severity = "high"
            elif dept_count <= second_count * 1.2:
                severity = "low"

        # Build recommendation
        if dept_count > 20:
            title = f"{dept_name} - High Priority Focus"
            description = f"{dept_name} has {dept_count} recorded violations, the highest among all departments. This requires targeted intervention and coordination with department administration."
            action = (
                f"Schedule meeting with {dept_name} dean/coordinator within this week"
            )
        elif dept_count > 10:
            title = f"{dept_name} - Attention Needed"
            description = f"{dept_name} has {dept_count} recorded violations. Review department-specific patterns and consider awareness programs."
            action = (
                f"Coordinate with {dept_name} administration for targeted intervention"
            )
        else:
            title = f"{dept_name} - Monitoring"
            description = f"{dept_name} has {dept_count} recorded violations. Continue monitoring for patterns."
            action = f"Review {dept_name} violation patterns and maintain awareness"

        recommendations["department_focus"].append(
            {
                "icon": "fa-building",
                "severity": severity,
                "title": title,
                "description": description,
                "action": action,
            }
        )

    # === VIOLATION INSIGHTS ===

    # Minor to major conversion risk
    if total_minor > 0:
        students_at_risk = (
            StudentModel.objects.annotate(
                minor_count=Count("violations", filter=Q(violations__type="minor"))
            )
            .filter(minor_count__gte=2)
            .count()
        )

        if students_at_risk > 0:
            recommendations["violation_insights"].append(
                {
                    "icon": "fa-user-clock",
                    "title": "Students Approaching Major Threshold",
                    "description": f"{students_at_risk} student(s) have 2+ minor violations (3 minors = 1 major).",
                    "action": "Consider preventive counseling for at-risk students",
                }
            )

    # Resolution rate insight
    if total_violations > 0:
        from .models import Violation as ViolationModel

        resolved_count = ViolationModel.objects.filter(status="resolved").count()
        resolution_rate = (resolved_count / total_violations) * 100

        if resolution_rate < 50:
            recommendations["violation_insights"].append(
                {
                    "icon": "fa-tasks",
                    "title": "Low Resolution Rate",
                    "description": f"Only {resolution_rate:.1f}% of cases are resolved.",
                    "action": "Review case processing workflow for bottlenecks",
                }
            )
        elif resolution_rate > 80:
            recommendations["violation_insights"].append(
                {
                    "icon": "fa-check-double",
                    "title": "Strong Resolution Rate",
                    "description": f"{resolution_rate:.1f}% resolution rate indicates effective case management.",
                    "action": "Maintain current practices and document best approaches",
                }
            )

    # === SUMMARY RECOMMENDATION ===

    if not recommendations["priority_actions"]:
        recommendations["summary_recommendation"] = (
            "Current violation levels are manageable. Continue regular monitoring and maintain preventive programs."
        )
    elif any(a["severity"] == "high" for a in recommendations["priority_actions"]):
        recommendations["summary_recommendation"] = (
            "Immediate attention required. Review priority actions and schedule intervention meetings within this week."
        )
    else:
        recommendations["summary_recommendation"] = (
            "Some areas need attention. Address medium-priority items within the next two weeks."
        )

    return recommendations


############################################
# OSA Office - frontend-only
############################################

############################################
# Legacy helpers (optional)
############################################


def legacy_dashboard_redirect(request):
    """Redirect old /dashboard/ to the Student dashboard for clarity."""
    return redirect("violations:student_dashboard")


@login_required
def route_dashboard_view(request):
    """Role-aware router after login: sends users to their dashboard.

    If not authenticated, send to login.
    """
    if not request.user.is_authenticated:
        return redirect("violations:login")

    # Superusers act as OSA Coordinator(Admin) for routing purposes
    if getattr(request.user, "is_superuser", False):
        return redirect("violations:faculty_dashboard")

    role = getattr(request.user, "role", None)

    if role == getattr(
        getattr(type(request.user), "Role", object), "STUDENT", "student"
    ):
        return redirect("violations:student_dashboard")

    if role == getattr(
        getattr(type(request.user), "Role", object),
        "OSA_COORDINATOR",
        "osa_coordinator",
    ):
        return redirect("violations:faculty_dashboard")

    # Fallback
    return redirect("violations:login")


############################################
# Staff Feature Views - Complete Implementation
############################################

# @role_required({User.Role.STAFF})
def staff_export_report_view(request):
    """Staff: Export violation report as CSV."""
    start_date = request.GET.get("start_date", "")
    end_date = request.GET.get("end_date", "")

    violations = Violation.objects.select_related(
        "student", "student__user", "reported_by"
    ).order_by("-incident_at")

    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            violations = violations.filter(
                incident_at__gte=timezone.make_aware(start_dt)
            )
        except ValueError:
            pass

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            violations = violations.filter(incident_at__lte=timezone.make_aware(end_dt))
        except ValueError:
            pass

    # Create CSV response
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="violations_report_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
    )

    writer = csv.writer(response)
    writer.writerow(
        [
            "ID",
            "Student ID",
            "Student Name",
            "Description",
            "Type",
            "Status",
            "Location",
            "Incident Date",
            "Reported By",
            "Created At",
        ]
    )

    for v in violations:
        writer.writerow(
            [
                v.id,
                v.student.student_id,
                v.student.user.get_full_name() or v.student.student_id,
                v.description,
                v.type,
                v.status,
                v.location,
                v.incident_at.strftime("%Y-%m-%d %H:%M") if v.incident_at else "",
                v.reported_by.get_full_name() if v.reported_by else "",
                v.created_at.strftime("%Y-%m-%d %H:%M"),
            ]
        )

    return response


# @role_required({User.Role.STAFF})
def staff_send_report_view(request):
    """Staff: Send violation report summary to OSA Coordinator."""
    if request.method != "POST":
        return redirect("violations:staff_reports")

    start_date = request.POST.get("start_date", "")
    end_date = request.POST.get("end_date", "")
    message_content = request.POST.get("message", "").strip()

    # Get statistics for the report
    violations = Violation.objects.all()
    apology_letters = ApologyLetter.objects.all()

    # Apply date filters
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            violations = violations.filter(
                incident_at__gte=timezone.make_aware(start_dt)
            )
            apology_letters = apology_letters.filter(
                submitted_at__gte=timezone.make_aware(start_dt)
            )
        except ValueError:
            pass

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            violations = violations.filter(incident_at__lte=timezone.make_aware(end_dt))
            apology_letters = apology_letters.filter(
                submitted_at__lte=timezone.make_aware(end_dt)
            )
        except ValueError:
            pass

    # Calculate statistics
    total_violations = violations.count()
    pending_count = violations.filter(status=Violation.Status.REPORTED).count()
    in_progress_count = violations.filter(status=Violation.Status.UNDER_REVIEW).count()
    resolved_count = violations.filter(status=Violation.Status.RESOLVED).count()
    resolution_rate = (
        round((resolved_count / total_violations * 100), 1)
        if total_violations > 0
        else 0
    )
    total_apologies = apology_letters.count()

    # Build the report message
    period_text = ""
    if start_date and end_date:
        period_text = f"Period: {start_date} to {end_date}"
    else:
        period_text = f"As of {timezone.now().strftime('%B %d, %Y')}"

    report_message = f"""📊 VIOLATION REPORT SUMMARY
{period_text}

📈 Statistics:
• Total Violations: {total_violations}
• Pending: {pending_count}
• In Progress: {in_progress_count}
• Resolved: {resolved_count}
• Resolution Rate: {resolution_rate}%
• Apology Letters: {total_apologies}
"""

    if message_content:
        report_message += f"""
📝 Staff Notes:
{message_content}
"""

    report_message += f"""
—
Sent by: {request.user.get_full_name() or request.user.username}
Generated: {timezone.now().strftime('%B %d, %Y at %I:%M %p')}"""

    # Find all OSA Coordinators
    coordinators = User.objects.filter(role=User.Role.OSA_COORDINATOR)

    if not coordinators.exists():
        messages.error(request, "No OSA Coordinators found in the system.")
        return redirect("violations:staff_reports")

    # Send message to all coordinators
    sent_count = 0
    for coordinator in coordinators:
        Message.objects.create(
            sender=request.user, receiver=coordinator, content=report_message
        )
        sent_count += 1

    # Log the activity
    from .models import ActivityLog

    ActivityLog.log_activity(
        action_type="report_sent",
        description=f"Sent violation report summary to {sent_count} coordinator(s). {period_text}",
        request=request,
        user=request.user,
    )

    messages.success(
        request, f"Report sent successfully to {sent_count} OSA Coordinator(s)."
    )
    return redirect("violations:staff_reports")


# @role_required({User.Role.STAFF})
def staff_delete_document_view(request, document_id):
    """Staff: Delete a violation document."""
    document = get_object_or_404(ViolationDocument, id=document_id)
    violation_id = document.violation.id

    if request.method == "POST":
        document.delete()
        messages.success(request, "Document deleted successfully.")

    return redirect("violations:staff_violation_detail", violation_id=violation_id)


# @role_required({User.Role.STAFF})
def staff_send_message_view(request):
    """Staff: Send a message to a student."""
    if request.method == "POST":
        student_id = request.POST.get("student_id", "").strip()
        message_content = request.POST.get("message", "").strip()

        if not student_id or not message_content:
            messages.error(request, "Student ID and message are required.")
            return redirect("violations:staff_dashboard")

        # Find the student
        student = (
            StudentModel.objects.select_related("user")
            .filter(student_id__iexact=student_id)
            .first()
        )
        if not student:
            messages.error(request, f"Student with ID '{student_id}' not found.")
            return redirect("violations:staff_dashboard")

        # Create the message
        Message.objects.create(
            sender=request.user, receiver=student.user, content=message_content
        )

        messages.success(
            request,
            f"Message sent to {student.user.get_full_name() or student.student_id}.",
        )
        return redirect("violations:staff_dashboard")

    return redirect("violations:staff_dashboard")


# @role_required({User.Role.STAFF})
def staff_send_faculty_message_view(request):
    """Staff: Send a message to a faculty member."""
    if request.method == "POST":
        faculty_id = request.POST.get("faculty_id")
        message_content = request.POST.get("content", "").strip()

        if not faculty_id or not message_content:
            messages.error(
                request, "Please select a faculty member and enter a message."
            )
            return redirect("violations:staff_dashboard")

        # Find the OSA Coordinator user
        faculty_user = User.objects.filter(
            id=faculty_id, role=User.Role.OSA_COORDINATOR
        ).first()
        if not faculty_user:
            messages.error(request, "OSA Coordinator not found.")
            return redirect("violations:staff_dashboard")

        # Create the message
        Message.objects.create(
            sender=request.user, receiver=faculty_user, content=message_content
        )

        messages.success(
            request,
            f"Message sent to {faculty_user.get_full_name() or faculty_user.username}.",
        )
        return redirect("violations:staff_dashboard")

    messages.error(request, "Invalid request method.")
    return redirect("violations:staff_dashboard")


@role_required({User.Role.STUDENT})
def student_mark_message_read_view(request, message_id):
    """Student: Mark a message as read."""
    message_obj = get_object_or_404(Message, id=message_id, receiver=request.user)
    message_obj.mark_read()
    return JsonResponse({"status": "ok"})


@role_required({User.Role.STUDENT})
def student_reply_message_view(request):
    """Student: Reply to a staff message (limited to 10 characters)."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "POST required"}, status=405)

    try:
        import json

        data = json.loads(request.body)
        message_id = data.get("message_id")
        reply_text = data.get("reply", "").strip()

        if not message_id or not reply_text:
            return JsonResponse(
                {"status": "error", "error": "Missing message_id or reply"}, status=400
            )

        # Limit reply to 10 characters
        if len(reply_text) > 10:
            reply_text = reply_text[:10]

        # Get the original message to find the sender (staff)
        original_message = get_object_or_404(
            Message, id=message_id, receiver=request.user
        )

        # Create a reply message from student to staff
        Message.objects.create(
            sender=request.user, receiver=original_message.sender, content=reply_text
        )

        return JsonResponse({"status": "ok"})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=500)

@role_required({User.Role.STUDENT})
def student_delete_message_view(request):
    """Student: Move a message to trash (soft delete)."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "POST required"}, status=405)

    try:
        import json

        data = json.loads(request.body)
        message_id = data.get("message_id")

        if not message_id:
            return JsonResponse(
                {"status": "error", "error": "Missing message_id"}, status=400
            )

        # Student can delete received messages (from staff) or sent messages (replies)
        msg = (
            Message.objects.filter(id=message_id)
            .filter(models.Q(sender=request.user) | models.Q(receiver=request.user))
            .first()
        )

        if not msg:
            return JsonResponse(
                {"status": "error", "error": "Message not found"}, status=404
            )

        msg.delete_for_user(request.user)
        return JsonResponse({"status": "ok"})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=500)


@role_required({User.Role.STUDENT})
def student_restore_message_view(request):
    """Student: Restore a message from trash."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "POST required"}, status=405)

    try:
        import json

        data = json.loads(request.body)
        message_id = data.get("message_id")

        if not message_id:
            return JsonResponse(
                {"status": "error", "error": "Missing message_id"}, status=400
            )

        msg = (
            Message.objects.filter(id=message_id)
            .filter(models.Q(sender=request.user) | models.Q(receiver=request.user))
            .first()
        )

        if not msg:
            return JsonResponse(
                {"status": "error", "error": "Message not found"}, status=404
            )

        msg.restore_for_user(request.user)
        return JsonResponse({"status": "ok"})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=500)


@role_required({User.Role.OSA_COORDINATOR})
def faculty_mark_message_read_view(request, message_id: int):
    """OSA Coordinator: Mark a message as read."""
    message_obj = get_object_or_404(Message, id=message_id, receiver=request.user)
    message_obj.mark_read()
    return JsonResponse({"status": "ok"})


@role_required({User.Role.OSA_COORDINATOR})
def faculty_delete_message_view(request):
    """OSA Coordinator: Move a message to trash (soft delete)."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "POST required"}, status=405)

    try:
        import json

        data = json.loads(request.body)
        message_id = data.get("message_id")

        if not message_id:
            return JsonResponse(
                {"status": "error", "error": "Missing message_id"}, status=400
            )

        # Allow deletion of both sent and received messages
        msg = (
            Message.objects.filter(id=message_id)
            .filter(models.Q(sender=request.user) | models.Q(receiver=request.user))
            .first()
        )

        if not msg:
            return JsonResponse(
                {"status": "error", "error": "Message not found"}, status=404
            )

        msg.delete_for_user(request.user)
        return JsonResponse({"status": "ok"})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=500)


@role_required({User.Role.OSA_COORDINATOR})
def faculty_restore_message_view(request):
    """OSA Coordinator: Restore a message from trash."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "POST required"}, status=405)

    try:
        import json

        data = json.loads(request.body)
        message_id = data.get("message_id")

        if not message_id:
            return JsonResponse(
                {"status": "error", "error": "Missing message_id"}, status=400
            )

        # Allow restoration of both sent and received messages
        msg = (
            Message.objects.filter(id=message_id)
            .filter(models.Q(sender=request.user) | models.Q(receiver=request.user))
            .first()
        )

        if not msg:
            return JsonResponse(
                {"status": "error", "error": "Message not found"}, status=404
            )

        msg.restore_for_user(request.user)
        return JsonResponse({"status": "ok"})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=500)


@role_required({User.Role.OSA_COORDINATOR})
def faculty_reply_message_view(request):
    """OSA Coordinator: Reply to a staff message."""
    if request.method != "POST":
        return JsonResponse({"status": "error", "error": "POST required"}, status=405)

    try:
        import json

        data = json.loads(request.body)
        message_id = data.get("message_id")
        reply_text = data.get("reply", "").strip()

        if not message_id or not reply_text:
            return JsonResponse(
                {"status": "error", "error": "Missing message_id or reply"}, status=400
            )

        # Get the original message to find the sender (staff)
        original_message = get_object_or_404(
            Message, id=message_id, receiver=request.user
        )

        # Create a reply message from faculty to staff
        Message.objects.create(
            sender=request.user, receiver=original_message.sender, content=reply_text
        )

        return JsonResponse({"status": "ok", "message": "Reply sent successfully!"})
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "error": str(e)}, status=500)


############################################
# Student: Letter of Apology Submission
############################################


@role_required({User.Role.STUDENT})
def student_apology_view(request):
    """Student view to submit letter of apology for violations."""
    student = getattr(request.user, "student_profile", None)
    if not student:
        messages.error(request, "Student profile not found.")
        return redirect("violations:student_dashboard")

    # Get all violations that need apology (not resolved, or those requiring apology)
    violations_needing_apology = (
        Violation.objects.filter(student=student)
        .exclude(status=Violation.Status.RESOLVED)
        .order_by("-created_at")
    )

    # Get existing apology letters for this student
    existing_apologies = ApologyLetter.objects.filter(student=student).select_related(
        "violation"
    )
    apology_by_violation = {a.violation_id: a for a in existing_apologies}

    if request.method == "POST":
        violation_id = request.POST.get("violation_id")
        apology_file = request.FILES.get("apology_file")  # Optional now

        # Get letter form data
        letter_date = request.POST.get("letter_date", "").strip()
        letter_campus = request.POST.get("letter_campus", "").strip()
        letter_full_name = request.POST.get("letter_full_name", "").strip()
        letter_suffix = request.POST.get("letter_suffix", "").strip()  # Optional
        letter_home_address = request.POST.get("letter_home_address", "").strip()
        letter_program = request.POST.get("letter_program", "").strip()
        letter_violations = request.POST.get("letter_violations", "").strip()
        letter_printed_name = request.POST.get("letter_printed_name", "").strip()
        signature_data = request.POST.get("signature_data", "").strip()

        if not violation_id:
            messages.error(request, "Please select a violation.")
            return redirect("violations:student_apology")

        # Validate that at least form data is filled
        if not letter_full_name:
            messages.error(
                request, "Please fill out the letter form with your full name."
            )
            return redirect("violations:student_apology")

        # Validate file type if file is uploaded
        if apology_file:
            allowed_types = ["application/pdf", "image/jpeg", "image/png", "image/jpg"]
            if apology_file.content_type not in allowed_types:
                messages.error(
                    request, "Please upload a PDF or image file (JPEG, PNG)."
                )
                return redirect("violations:student_apology")

        # Get the violation
        violation = get_object_or_404(Violation, id=violation_id, student=student)

        # Check if already submitted and pending/approved
        existing = apology_by_violation.get(violation.id)
        if existing and existing.status in [
            ApologyLetter.Status.PENDING,
            ApologyLetter.Status.APPROVED,
        ]:
            messages.warning(
                request,
                f"You have already submitted an apology letter for this violation. Status: {existing.get_status_display()}",
            )
            return redirect("violations:student_apology")

        # Create or update apology letter
        if existing and existing.status in [
            ApologyLetter.Status.REJECTED,
            ApologyLetter.Status.REVISION_NEEDED,
        ]:
            # Update existing rejected/revision needed letter
            if apology_file:
                existing.file = apology_file
            existing.status = ApologyLetter.Status.PENDING
            existing.submitted_at = timezone.now()
            existing.verified_by = None
            existing.verified_at = None
            existing.remarks = ""
            # Update letter form data
            existing.letter_date = letter_date
            existing.letter_campus = letter_campus
            existing.letter_full_name = letter_full_name
            existing.letter_suffix = letter_suffix
            existing.letter_home_address = letter_home_address
            existing.letter_program = letter_program
            existing.letter_violations = letter_violations
            existing.letter_printed_name = letter_printed_name
            existing.signature_data = signature_data
            existing.save()

            # Log activity for resubmitted apology
            from .models import ActivityLog

            ActivityLog.log_activity(
                user=request.user,
                action_type=ActivityLog.ActionType.APOLOGY_RESUBMITTED,
                description=f"Resubmitted apology letter for violation #{violation.id}",
                request=request,
                related_apology=existing,
                related_student=student,
                related_violation=violation,
            )
            messages.success(
                request,
                "Your revised letter of apology has been resubmitted successfully!",
            )
        else:
            # Create new apology letter
            apology_letter = ApologyLetter(
                violation=violation,
                student=student,
                status=ApologyLetter.Status.PENDING,
                letter_date=letter_date,
                letter_campus=letter_campus,
                letter_full_name=letter_full_name,
                letter_suffix=letter_suffix,
                letter_home_address=letter_home_address,
                letter_program=letter_program,
                letter_violations=letter_violations,
                letter_printed_name=letter_printed_name,
                signature_data=signature_data,
            )
            if apology_file:
                apology_letter.file = apology_file
            apology_letter.save()

            # Log activity for new apology submission
            from .models import ActivityLog

            ActivityLog.log_activity(
                user=request.user,
                action_type=ActivityLog.ActionType.APOLOGY_SUBMITTED,
                description=f"Submitted apology letter for violation #{violation.id}",
                request=request,
                related_apology=apology_letter,
                related_student=student,
                related_violation=violation,
            )
            messages.success(
                request, "Your letter of apology has been submitted successfully!"
            )

        return redirect("violations:student_apology")

    # Prepare context with violation apology status
    violations_with_status = []
    for v in violations_needing_apology:
        apology = apology_by_violation.get(v.id)
        violations_with_status.append(
            {
                "violation": v,
                "apology": apology,
                "can_submit": apology is None
                or apology.status
                in [
                    ApologyLetter.Status.REJECTED,
                    ApologyLetter.Status.REVISION_NEEDED,
                ],
            }
        )

    # Also include resolved violations that might have apologies
    all_apologies = (
        ApologyLetter.objects.filter(student=student)
        .select_related("violation")
        .order_by("-submitted_at")
    )

    ctx = {
        "student": student,
        "violations_with_status": violations_with_status,
        "all_apologies": all_apologies,
    }
    return render(request, "violations/student/apology.html", ctx)


############################################
# OSA Alert Management Views
############################################


# @role_required({User.Role.STAFF})
def staff_schedule_meeting_view(request, alert_id):
    """OSA Coordinator: Schedule a meeting for a student alert."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        alert = StaffAlert.objects.get(id=alert_id, resolved=False)
    except StaffAlert.DoesNotExist:
        return JsonResponse({"error": "Alert not found"}, status=404)

    # Parse JSON data from request body
    try:
        data = json.loads(request.body)
        scheduled_meeting_str = data.get("scheduled_meeting")
        meeting_deadline_str = data.get("meeting_deadline")
        meeting_notes = data.get("meeting_notes", "")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON data"}, status=400)

    if not scheduled_meeting_str:
        return JsonResponse({"error": "Meeting date/time required"}, status=400)

    if not meeting_deadline_str:
        return JsonResponse({"error": "Meeting deadline required"}, status=400)

    try:
        from datetime import datetime

        # Parse datetime string from datetime-local input (format: YYYY-MM-DDTHH:MM)
        scheduled_meeting = datetime.strptime(scheduled_meeting_str, "%Y-%m-%dT%H:%M")
        meeting_deadline = datetime.strptime(meeting_deadline_str, "%Y-%m-%dT%H:%M")

        # Validate deadline is after meeting time
        if meeting_deadline <= scheduled_meeting:
            return JsonResponse(
                {"error": "Deadline must be after meeting time"}, status=400
            )

        alert.scheduled_meeting = scheduled_meeting
        alert.meeting_deadline = meeting_deadline
        alert.meeting_notes = meeting_notes
        alert.meeting_status = StaffAlert.MeetingStatus.SCHEDULED
        alert.meeting_status_updated_at = timezone.now()
        alert.save()

        # Send notification to OSA Coordinator
        faculty_users = User.objects.filter(role=User.Role.OSA_COORDINATOR)
        for faculty in faculty_users:
            Message.objects.create(
                sender=request.user,
                receiver=faculty,
                content=f"""URGENT: Meeting Scheduled with Student

A mandatory meeting has been scheduled regarding a student who has reached the violation threshold.

Student Details:
- Student ID: {alert.student.student_id}
- Name: {alert.student.user.get_full_name() or alert.student.user.username}
- Effective Major Violations: {alert.effective_major_count}

Meeting Details:
- Date & Time: {scheduled_meeting.strftime('%B %d, %Y at %I:%M %p')}
- Deadline: {meeting_deadline.strftime('%B %d, %Y at %I:%M %p')}
- Location: OSA Office
- Status: SCHEDULED
- Purpose: Review violation record and determine next steps
{f"- Additional Notes: {meeting_notes}" if meeting_notes else ""}

⚠️ Note: Meeting will automatically expire if student doesn't attend by the deadline.

Please be prepared to discuss the student's violation history and appropriate disciplinary actions.

This meeting was scheduled by: {request.user.get_full_name() or request.user.username}
""".strip(),
            )

        # Send notification to the student
        Message.objects.create(
            sender=request.user,
            receiver=alert.student.user,
            content=f"""MANDATORY MEETING NOTICE

You have been scheduled for a mandatory meeting with the OSA Coordinator due to reaching the violation threshold.

Meeting Details:
- Date & Time: {scheduled_meeting.strftime('%B %d, %Y at %I:%M %p')}
- ⚠️ DEADLINE: {meeting_deadline.strftime('%B %d, %Y at %I:%M %p')}
- Location: OSA Office
- Status: SCHEDULED
- Purpose: Review your violation record and discuss next steps
{f"- Additional Notes: {meeting_notes}" if meeting_notes else ""}

Important Notes:
- This meeting is mandatory and your attendance is required
- ⚠️ If you don't attend by the deadline, this meeting will expire and additional action may be taken
- Please arrive 10 minutes early
- Bring your Student ID
- Come prepared to discuss your recent violations

If you are unable to attend this scheduled time, please contact the OSA Office immediately to reschedule.

Violation Summary:
- Effective Major Violations: {alert.effective_major_count}
- Latest Violation: {alert.triggered_violation.description if alert.triggered_violation else 'N/A'}

For questions, contact the OSA Office.

Regards,
OSA Office
{request.user.get_full_name() or request.user.username}
""".strip(),
        )

        return JsonResponse(
            {
                "status": "success",
                "message": "Meeting scheduled successfully. Notifications sent to student and OSA Coordinator.",
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@role_required({User.Role.OSA_COORDINATOR})
def staff_mark_meeting_met_view(request, alert_id):
    """OSA Coordinator: Mark a scheduled meeting as met/completed."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        alert = StaffAlert.objects.get(id=alert_id, resolved=False)
    except StaffAlert.DoesNotExist:
        return JsonResponse({"error": "Alert not found"}, status=404)

    # Only allow marking as met if status is scheduled
    if alert.meeting_status != StaffAlert.MeetingStatus.SCHEDULED:
        return JsonResponse(
            {"error": "Meeting must be in scheduled status to mark as met"}, status=400
        )

    try:
        # Update status to met
        alert.meeting_status = StaffAlert.MeetingStatus.MET
        alert.meeting_status_updated_at = timezone.now()
        alert.save()

        # Send notification to OSA Coordinator
        faculty_users = User.objects.filter(role=User.Role.OSA_COORDINATOR)
        for faculty in faculty_users:
            if faculty != request.user:  # Don't notify yourself
                Message.objects.create(
                    sender=request.user,
                    receiver=faculty,
                    content=f"""MEETING STATUS UPDATE: Completed

The scheduled meeting has been marked as COMPLETED.

Student Details:
- Student ID: {alert.student.student_id}
- Name: {alert.student.user.get_full_name() or alert.student.user.username}
- Effective Major Violations: {alert.effective_major_count}

Meeting Details:
- Original Scheduled Time: {alert.scheduled_meeting.strftime('%B %d, %Y at %I:%M %p') if alert.scheduled_meeting else 'N/A'}
- Status: MET/COMPLETED
- Marked by: {request.user.get_full_name() or request.user.username}

Please follow up with any necessary disciplinary actions or documentation.
""".strip(),
                )

        # Send notification to the student
        Message.objects.create(
            sender=request.user,
            receiver=alert.student.user,
            content=f"""MEETING STATUS UPDATE

Your mandatory meeting with the OSA Coordinator has been marked as COMPLETED.

Meeting Details:
- Scheduled Time: {alert.scheduled_meeting.strftime('%B %d, %Y at %I:%M %p') if alert.scheduled_meeting else 'N/A'}
- Status: MET/COMPLETED

Thank you for attending the meeting. Please follow any instructions or action items discussed during the meeting.

If you have any questions, contact the OSA Office.

Regards,
OSA Office
""".strip(),
        )

        return JsonResponse(
            {
                "status": "success",
                "message": "Meeting marked as completed. Notifications sent.",
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@role_required({User.Role.OSA_COORDINATOR})
def staff_resolve_alert_view(request, alert_id):
    """OSA Coordinator: Mark a student alert as resolved."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        alert = StaffAlert.objects.get(id=alert_id, resolved=False)
        alert.resolved = True
        alert.resolved_at = timezone.now()
        alert.save()
        return JsonResponse(
            {"status": "success", "message": "Alert resolved successfully"}
        )
    except StaffAlert.DoesNotExist:
        return JsonResponse({"error": "Alert not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@role_required({User.Role.OSA_COORDINATOR})
def staff_dismiss_alert_view(request, alert_id):
    """Staff/OSA Coordinator: Dismiss (soft delete) a staff alert."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        alert = StaffAlert.objects.get(id=alert_id, dismissed_at__isnull=True)
        alert.dismiss(user=request.user)
        return JsonResponse(
            {"status": "success", "message": "Alert dismissed successfully"}
        )
    except StaffAlert.DoesNotExist:
        return JsonResponse({"error": "Alert not found"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@role_required({User.Role.OSA_COORDINATOR})
def staff_restore_alert_view(request, alert_id):
    """OSA Coordinator: Restore a dismissed student alert."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        alert = StaffAlert.objects.get(id=alert_id, dismissed_at__isnull=False)
        alert.restore()
        return JsonResponse(
            {"status": "success", "message": "Alert restored successfully"}
        )
    except StaffAlert.DoesNotExist:
        return JsonResponse({"error": "Alert not found or not dismissed"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)


@role_required({User.Role.OSA_COORDINATOR})
def staff_permanent_delete_alert_view(request, alert_id):
    """OSA Coordinator: Permanently delete a dismissed student alert."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        alert = StaffAlert.objects.get(id=alert_id, dismissed_at__isnull=False)
        alert.delete()
        return JsonResponse(
            {"status": "success", "message": "Alert permanently deleted"}
        )
    except StaffAlert.DoesNotExist:
        return JsonResponse({"error": "Alert not found or not dismissed"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)