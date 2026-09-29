import uuid
from django.db import models
from apps.organizations.models import Organization
from apps.products.models import Product
from apps.operations.models import Operation


class Routing(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        db_index=True
    )

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="routings"
    )

    from_operation = models.ForeignKey(
        Operation,
        on_delete=models.CASCADE,
        related_name="next_steps"
    )

    to_operation = models.ForeignKey(
        Operation,
        on_delete=models.CASCADE,
        related_name="previous_steps"
    )

    probability = models.FloatField(default=1)

    priority = models.IntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    updated_at = models.DateTimeField(auto_now=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:

        db_table = "routing"

        constraints = [
            models.UniqueConstraint(
                fields=["product", "from_operation", "to_operation"],
                name="unique_operation_path"
            )
        ]

        indexes = [
            models.Index(fields=["product"]),
            models.Index(fields=["organization", "product"]),
        ]