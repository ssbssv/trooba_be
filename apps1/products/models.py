import uuid
from django.db import models
from apps.organizations.models import Organization


class Product(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Optional link to an RMCT manufacturing model so that
    # products are scoped per RMCMModel rather than globally
    # per organization.
    model = models.ForeignKey(
        "rmct.RMCMModel",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="products",
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="products"
    )

    name = models.CharField(max_length=255)

    end_demand = models.IntegerField(default=0)

    lot_size = models.IntegerField(default=1)

    transfer_batch = models.IntegerField(default=-1)

    department_area = models.CharField(
        max_length=255,
        
        blank=True
    )

    demand_factor = models.FloatField(default=1)

    lot_factor = models.FloatField(default=1)

    variability_factor = models.FloatField(default=1)

    make_to_stock = models.BooleanField(default=False)

    gather_transfer_batches = models.BooleanField(default=False)

    prod1 = models.FloatField(default=0)
    prod2 = models.FloatField(default=0)
    prod3 = models.FloatField(default=0)
    prod4 = models.FloatField(default=0)

    comments = models.TextField( blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:

        db_table = "products"

        constraints = [
            models.UniqueConstraint(
                fields=["organization", "model", "name"],
                name="unique_product_per_org_model",
            )
        ]

        indexes = [
            models.Index(fields=["organization", "department_area"]),
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["model", "created_at"]),
        ]

    def __str__(self):
        return self.name