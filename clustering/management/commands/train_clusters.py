from django.core.management.base import BaseCommand

from clustering.ml.pipeline import run_pipeline


class Command(BaseCommand):
    help = "Stages 2-4: pre-process district data, run K-Means + GMM clustering, and score safety tiers."

    def handle(self, *args, **options):
        run_pipeline(stdout=self.stdout)
        self.stdout.write(self.style.SUCCESS("Clustering + scoring complete."))
