from django.db import migrations, models


def migrate_old_violation_data(apps, schema_editor):
    Violation = apps.get_model("violations", "Violation")

    # The old "type" field actually stored severity.
    # Severity was already copied in migration 0030.
    # Convert the old type values to the neutral category because
    # the original violation category was not recorded.
    Violation.objects.filter(type="minor").update(type="other_general")
    Violation.objects.filter(type="major").update(type="other_general")


class Migration(migrations.Migration):

    dependencies = [
        ("violations", "0030_migrate_violation_severity"),
    ]

    operations = [
        migrations.RunPython(
            migrate_old_violation_data,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="violation",
            name="severity",
            field=models.CharField(
                choices=[
                    ("minor", "Minor"),
                    ("major", "Major"),
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="violation",
            name="type",
            field=models.CharField(
                choices=[
                    ("academic_dishonesty", "Academic Dishonesty"),
                    ("bullying_harassment", "Bullying / Harassment"),
                    ("disrespectful_conduct", "Disrespectful Conduct"),
                    (
                        "fighting_physical_altercation",
                        "Fighting / Physical Altercation",
                    ),
                    ("threatening_behavior", "Threatening Behavior"),
                    ("unauthorized_entry", "Unauthorized Entry"),
                    (
                        "unauthorized_school_property_use",
                        "Unauthorized Use of School Property",
                    ),
                    ("property_damage", "Property Damage"),
                    ("theft_unauthorized_taking", "Theft / Unauthorized Taking"),
                    (
                        "fraud_document_falsification",
                        "Fraud / Falsification of Documents",
                    ),
                    ("smoking_vaping", "Smoking / Vaping"),
                    ("alcohol_related", "Alcohol-Related Violation"),
                    ("prohibited_substance", "Prohibited Substance Violation"),
                    ("improper_uniform_dress_code", "Improper Uniform / Dress Code"),
                    ("id_identification", "ID / Identification Violation"),
                    ("disruptive_behavior", "Disruptive Behavior"),
                    (
                        "unauthorized_recording_photography",
                        "Unauthorized Recording / Photography",
                    ),
                    (
                        "online_social_media_misconduct",
                        "Online / Social Media Misconduct",
                    ),
                    ("gambling", "Gambling"),
                    ("other_general", "Other / General Violation"),
                ],
                max_length=50,
            ),
        ),
    ]