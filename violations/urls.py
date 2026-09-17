from django.urls import path, include
from django.views.generic import RedirectView
from django.contrib.auth import views as auth_views
from . import views

# Optional namespace for clarity when reverse() is used from other apps
app_name = "violations"

# Role-specific URL includes
student_patterns = [
    path("login/", views.student_login_view, name="student_login"),
    path("login/auth/", views.student_login_auth, name="student_login_auth"),
    path("dashboard/", views.student_dashboard_view, name="student_dashboard"),
    path("apology/", views.student_apology_view, name="student_apology"),
    path(
        "update-profile/",
        views.student_update_profile_view,
        name="student_update_profile",
    ),
    path(
        "message/<int:message_id>/read/",
        views.student_mark_message_read_view,
        name="student_mark_message_read",
    ),
    path(
        "message/reply/", views.student_reply_message_view, name="student_reply_message"
    ),
    path(
        "message/delete/",
        views.student_delete_message_view,
        name="student_delete_message",
    ),
    path(
        "message/restore/",
        views.student_restore_message_view,
        name="student_restore_message",
    ),
]

# Staff routes disabled.
# The redesigned UDM system uses OSA Coordinator + Student only.
staff_patterns = []

faculty_patterns = [
    path("login/", views.faculty_login_view, name="faculty_login"),
    path("login/auth/", views.credentials_login_auth, name="faculty_login_auth"),
    path("dashboard/", views.faculty_dashboard_view, name="faculty_dashboard"),
    path(
        "report-violation/",
        views.osa_report_violation_view,
        name="osa_report_violation",
    ),
    path(
        "case-management/",
        views.faculty_case_management_view,
        name="faculty_case_management",
    ),
    path(
        "case-management/update-status/",
        views.faculty_update_case_status_view,
        name="faculty_update_case_status",
    ),
    path("my-reports/", views.faculty_my_reports_view, name="faculty_my_reports"),
    path(
        "activity-logs/", views.faculty_activity_logs_view, name="faculty_activity_logs"
    ),
    path(
        "activity-logs/<int:log_id>/delete/",
        views.faculty_delete_activity_log_view,
        name="faculty_delete_activity_log",
    ),
    path("analytics/", views.faculty_analytics_view, name="faculty_analytics"),
    path("analytics/api/", views.faculty_analytics_api, name="faculty_analytics_api"),
    path(
        "students/<str:student_id>/",
        views.faculty_student_detail_view,
        name="faculty_student_detail",
    ),
    path(
        "message/<int:message_id>/read/",
        views.faculty_mark_message_read_view,
        name="faculty_mark_message_read",
    ),
    path(
        "message/delete/",
        views.faculty_delete_message_view,
        name="faculty_delete_message",
    ),
    path(
        "message/restore/",
        views.faculty_restore_message_view,
        name="faculty_restore_message",
    ),
    path(
        "message/reply/", views.faculty_reply_message_view, name="faculty_reply_message"
    ),
]

# Guard routes disabled.
# The redesigned UDM system uses OSA Coordinator + Student only.
guard_patterns = []

# Formator routes disabled.
# Apology-letter review will be handled by OSA Coordinator.
formator_patterns = []

urlpatterns = [
    # General paths (e.g., home, APIs)
    path(
        "",
        RedirectView.as_view(pattern_name="violations:login", permanent=False),
        name="home",
    ),
    path("api/welcome-tts/", views.welcome_tts_view, name="welcome_tts"),
    path("api/detect-face/", views.detect_face_view, name="detect_face"),
    path("login/", views.login_view, name="login"),  # General login page (if needed)
    # path('signup/', views.signup_view, name='signup'),
    path("dashboard/", views.legacy_dashboard_redirect, name="dashboard"),
    path("route/", views.route_dashboard_view, name="route_dashboard"),
    path(
        "auth/login/",
        auth_views.LoginView.as_view(template_name="violations/auth/login.html"),
        name="auth_login",
    ),
    path("auth/logout/", views.logout_view, name="auth_logout"),
    # Role-specific includes
    path("student/", include(student_patterns)),
    # path('staff/', include(staff_patterns)),
    path("faculty/", include(faculty_patterns)),
    # path('guard/', include(guard_patterns)),
    # path('formator/', include(formator_patterns)),
]