import os

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "student_violation_system.settings",
)
os.environ.setdefault("USE_SQLITE", "True")

import django

django.setup()

from django.db import transaction
from violations.models import User, Student


START_ID = 25000101
END_ID = 25000200
PASSWORD = "Student@2026"

created = 0
existing = 0


for student_id in range(START_ID, END_ID + 1):
    student_id = str(student_id)

    if Student.objects.filter(student_id=student_id).exists():
        existing += 1
        continue

    username = f"student{student_id}"
    email = f"{username}@dummy.udm.local"

    # Make sure this User is also new.
    if User.objects.filter(username=username).exists():
        existing += 1
        continue

    with transaction.atomic():
        user = User.objects.create_user(
            username=username,
            email=email,
            password=PASSWORD,
            first_name=f"Student{student_id[-4:]}",
            last_name="Test",
            role=User.Role.STUDENT,
        )

        Student.objects.create(
            user=user,
            student_id=student_id,
            suffix="",
            program=Student.College.CIT,
            year_level=3,
            department="CIT",
            enrollment_status="Active",
            contact_number=f"0999{student_id[-7:]}",
            guardian_name="Test Guardian",
            guardian_contact="09998887777",
        )

        created += 1


print()
print("======================================")
print("100 ADDITIONAL STUDENTS COMPLETE")
print("======================================")
print(f"Created : {created}")
print(f"Existing: {existing}")
print(f"Range   : {START_ID} - {END_ID}")
print(f"Password: {PASSWORD}")
print("======================================")