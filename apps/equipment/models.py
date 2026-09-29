import uuid
from django.db import models
from apps.organizations.models import Organization
from apps.labor.models import Labor


class EquipmentGroup(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Optional link to an RMCT manufacturing model so that
    # equipment groups can be scoped per RMCMModel.
    model = models.ForeignKey(
        "rmct.RMCMModel",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="equipment_groups",
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="equipment_groups"
    )

    name = models.CharField(max_length=255)

    count = models.IntegerField(default=1)

    mttf_minutes = models.IntegerField(default=0)
    mttr_minutes = models.IntegerField(default=0)

    overtime_percent = models.FloatField(default=0)

    labor_group = models.ForeignKey(
        Labor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_index=True,
        related_name="equipment_groups"
    )

    comments = models.TextField( blank=True)

    equipment_type = models.CharField(
        max_length=50,
        default="Standard"
    )

    percent_time_unavailable = models.FloatField(default=0)

    setup_factor = models.FloatField(default=1)
    run_factor = models.FloatField(default=1)
    variability_factor = models.FloatField(default=1)

    department_area = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    out_of_area_equipment = models.BooleanField(default=False)

    eq1 = models.FloatField(default=0)
    eq2 = models.FloatField(default=0)
    eq3 = models.FloatField(default=0)
    eq4 = models.FloatField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:

        db_table = "equipment_groups"

        constraints = [
            models.UniqueConstraint(
                fields=["organization", "model", "name"],
                name="unique_equipment_group_per_org_model",
            )
        ]

        indexes = [
            models.Index(fields=["organization", "department_area"]),
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["model", "created_at"]),
        ]

    def __str__(self):
        return self.name