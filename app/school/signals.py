from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import ActivationOrReset


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def revoke_disabled_account(sender, instance, raw, using, **kwargs):
    if raw or instance.is_active:
        return
    # A re-enabled account must receive a new credential. This also invalidates
    # sessions which were never used while the account was disabled.
    if instance.has_usable_password():
        invalid = make_password(None)
        sender.objects.using(using).filter(pk=instance.pk, is_active=False).update(password=invalid)
        instance.password = invalid
    ActivationOrReset.objects.using(using).filter(user=instance, used_at__isnull=True).update(used_at=timezone.now())
