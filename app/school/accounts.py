import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ActivationOrReset


def _digest(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@transaction.atomic
def issue_code(user, purpose):
    if purpose not in dict(ActivationOrReset.PURPOSES) or not user.is_active:
        raise ValidationError("账号或凭证用途无效。")
    if purpose == ActivationOrReset.ACTIVATE and user.has_usable_password():
        raise ValidationError("账号已经激活，请使用密码重设。")
    now = timezone.now()
    user.account_codes.filter(used_at__isnull=True).update(used_at=now)
    code = secrets.token_urlsafe(32)
    ActivationOrReset.objects.create(user=user, purpose=purpose, digest=_digest(code),
        expires_at=now + timedelta(hours=1))
    return code


def redeem_code(username, purpose, code, password):
    error = "凭证无效、已使用或已过期。"
    tokens = ActivationOrReset.objects.filter(user__username=username, user__is_active=True,
        purpose=purpose, digest=_digest(code), used_at__isnull=True)
    token = tokens.filter(expires_at__gt=timezone.now()).select_related("user").first()
    if token is None or purpose == ActivationOrReset.ACTIVATE and token.user.has_usable_password():
        raise ValidationError(error)
    validate_password(password, token.user)
    encoded = make_password(password)
    with transaction.atomic():
        now = timezone.now()
        if tokens.filter(pk=token.pk, expires_at__gt=now).update(used_at=now) != 1:
            raise ValidationError(error)
        token.user.__class__.objects.filter(pk=token.user_id).update(password=encoded)
        token.user.account_codes.filter(used_at__isnull=True).update(used_at=now)
