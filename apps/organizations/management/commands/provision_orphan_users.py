"""
One-time / ops backfill for organization membership and model scoping.

Production deploy (run once after release, not on every request):
  python manage.py provision_orphan_users
  python manage.py provision_orphan_users --repair-model-links

Login uses ensure_user_organization_if_needed() with cheap EXISTS checks only.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.organizations.provisioning import (
    ensure_user_organization,
    user_ids_with_mislinked_models,
    users_needing_organization,
)


class Command(BaseCommand):
    help = (
        "Backfill personal organizations and relink RMCT models. "
        "Run once on deploy; login does not scan all users."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List affected users without writing to the DB.",
        )
        parser.add_argument(
            "--repair-model-links",
            action="store_true",
            help=(
                "Also repair users who have membership but models on Default/null org. "
                "Heavier than the default orphan-only pass."
            ),
        )
        parser.add_argument(
            "--all-users",
            action="store_true",
            help="Deprecated alias for --repair-model-links (runs repair pass only).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Iterator chunk size when scanning users (default 200).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        repair_links = (
            options["repair_model_links"]
            or options["all_users"]
        )
        batch_size = max(1, options["batch_size"])

        orphan_ids, repair_ids, target_ids = self._collect_target_user_ids(
            repair_links,
            batch_size,
        )

        self.stdout.write(
            f"Targets: {len(orphan_ids)} without membership, "
            f"{len(repair_ids)} with model-link repair, "
            f"{len(target_ids)} total."
        )

        if dry_run:
            self._handle_dry_run(target_ids)
            return

        created = 0
        repaired = 0
        models_total = 0
        errors = 0

        for uid in target_ids:
            user = User.objects.filter(id=uid).first()

            if user is None:
                continue

            try:
                was_created, linked = self._process_user(
                    user,
                    repair_ids,
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(
                    f"ERROR {user.email}: {exc}"
                )
                continue

            models_total += linked

            if was_created:
                created += 1
            else:
                repaired += 1

        summary = (
            f"Done. created={created} "
            f"repaired={repaired} "
            f"models_linked={models_total} "
            f"errors={errors}"
        )

        if errors:
            self.stderr.write(self.style.ERROR(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))

    def _collect_target_user_ids(
            self,
            repair_links: bool,
            batch_size: int,
        ):
        orphan_ids = {user.id for user in users_needing_organization()}

        repair_ids = set()
        if repair_links:
            repair_ids = {
                user.id
                for user in user_ids_with_mislinked_models().iterator(
                    chunk_size=batch_size
                )
            }

        target_ids = sorted(orphan_ids | repair_ids)

        return orphan_ids, repair_ids, target_ids

    def _handle_dry_run(self, target_ids):
        for uid in target_ids:
            user = User.objects.filter(id=uid).only("id", "email").first()
            if user:
                self.stdout.write(
                    f"  would provision: {user.id} {user.email}"
                )
    def _process_user(
            self,
            user,
            repair_ids,
        ):
        org, created, linked = ensure_user_organization(
            user,
            force_model_relink=user.id in repair_ids,
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created org '{org.name}' for {user.email} "
                    f"(linked {linked} model(s))"
                )
            )

        elif linked:
            self.stdout.write(
                f"Repaired {user.email} -> {org.name} "
                f"(linked {linked} model(s))"
            )

        return created, linked
    