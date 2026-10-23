from violations.models import Violation

records = Violation.objects.select_related("violation_type").order_by(
    "student_id", "violation_type_id", "incident_at", "id"
)

print("ID | STUDENT_ID | CODE | VIOLATION | INCIDENT_DATE | STATUS | CURRENT_TYPE")

for v in records:
    vt = v.violation_type
    print(
        f"{v.id} | {v.student_id} | "
        f"{vt.code if vt else 'UNLINKED'} | "
        f"{vt.name if vt else 'UNLINKED'} | "
        f"{v.incident_at} | {v.status} | {v.type}"
    )
