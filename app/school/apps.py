from django.apps import AppConfig


class SchoolConfig(AppConfig):
    name = "school"
    verbose_name = "教学关系"

    def ready(self):
        from . import signals  # Register account deactivation handling.
