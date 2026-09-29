import uuid
from django.db import models
from apps.organizations.models import Organization
from apps.products.models import Product


class BOM(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="boms"
    )

    parent_product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="components",
        db_index=True
    )

    component_product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="used_in",
        db_index=True
    )

    quantity_per_assembly = models.FloatField(default=1)

    position = models.IntegerField(default=1)

    comments = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:

        db_table = "bom"

        constraints = [
            models.UniqueConstraint(
                fields=["parent_product", "component_product"],
                name="unique_component_per_parent"
            )
        ]

        indexes = [
            models.Index(fields=["organization", "parent_product"]),
            models.Index(fields=["component_product"]),
        ]

    def __str__(self):
        return f"{self.parent_product.name} -> {self.component_product.name}"