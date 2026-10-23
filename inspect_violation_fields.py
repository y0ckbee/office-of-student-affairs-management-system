from violations.models import Violation

print("VIOLATION MODEL FIELDS")
for field in Violation._meta.fields:
    print(
        field.name,
        "| required:", not field.null and not field.blank and not field.has_default(),
        "| type:", field.get_internal_type()
    )

print("\nSTUDENT 1 EXISTING CASE")
v = Violation.objects.filter(student_id=1).first()
if v:
    for field in Violation._meta.fields:
        print(field.name, "=", getattr(v, field.attname))
