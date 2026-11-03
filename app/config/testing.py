from django.conf import settings
from django.test.runner import DiscoverRunner


class ProjectRunner(DiscoverRunner):
    def build_suite(self, test_labels=None, **kwargs):
        if not test_labels:
            self.top_level = str(settings.BASE_DIR / "app")
            test_labels = [self.top_level]
        return super().build_suite(test_labels, **kwargs)
