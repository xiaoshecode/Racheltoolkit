"""Teacher-scoped invitations create student identities without claiming existing users."""

import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .accounts import _digest
from .models import ClassInvitation, Enrollment, TeachingAssignment, TeachingClass
from .permissions import teacher_classes


INVALID_INVITATION = "邀请码无效、已撤销或已过期，请向任课老师索取新的邀请码。"
FULL_INVITATION = "邀请码的可用名额已用完，请联系任课老师。"
EXISTING_ACCOUNT = "该账号已存在，请登录；首次使用请向老师索取专属激活凭证。"


def _teacher_scope(teacher, teaching_class_id):
    if not getattr(teacher, "is_authenticated", False):
        raise ValidationError("你无权管理这个班级的邀请码。")
    actor = get_user_model().objects.filter(pk=teacher.pk, is_active=True).first()
    if actor is None or not teacher_classes(actor).filter(pk=teaching_class_id).exists():
        raise ValidationError("你无权管理这个班级的邀请码。")
    return actor


@transaction.atomic
def issue_invitation(teacher, teaching_class, valid_days=7, max_uses=30):
    if type(valid_days) is not int or not 1 <= valid_days <= 30:
        raise ValidationError("邀请码有效期须为 1 至 30 天。")
    if type(max_uses) is not int or not 1 <= max_uses <= 500:
        raise ValidationError("邀请码名额须为 1 至 500 人。")
    class_id = getattr(teaching_class, "pk", teaching_class)
    actor = _teacher_scope(teacher, class_id)
    # Serialize rotation of the same class even when using a row-locking database.
    group = TeachingClass.objects.select_for_update().get(pk=class_id)
    now = timezone.now()
    ClassInvitation.objects.filter(teaching_class=group, revoked_at__isnull=True).update(revoked_at=now)
    code = secrets.token_urlsafe(24)
    invitation = ClassInvitation.objects.create(teaching_class=group, created_by=actor,
        digest=_digest(code), expires_at=now + timedelta(days=valid_days), max_uses=max_uses)
    return code, invitation


@transaction.atomic
def revoke_invitation(teacher, invitation_id):
    invitation = ClassInvitation.objects.filter(pk=invitation_id).first()
    if invitation is None:
        raise ValidationError("邀请码不存在或无权管理。")
    _teacher_scope(teacher, invitation.teaching_class_id)
    ClassInvitation.objects.filter(pk=invitation.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())


def _load_invitation(code, *, lock=False, require_capacity=True):
    if not isinstance(code, str) or not 1 <= len(code.strip()) <= 128:
        raise ValidationError(INVALID_INVITATION)
    invitations = ClassInvitation.objects.all()
    if lock:
        invitations = invitations.select_for_update()
    invitation = invitations.filter(digest=_digest(code.strip()), revoked_at__isnull=True,
        expires_at__gt=timezone.now()).first()
    if invitation is None or not TeachingAssignment.objects.filter(
        teaching_class_id=invitation.teaching_class_id, teacher_id=invitation.created_by_id,
        active=True, teacher__is_active=True,
    ).exists():
        raise ValidationError(INVALID_INVITATION)
    if require_capacity and invitation.uses >= invitation.max_uses:
        raise ValidationError(FULL_INVITATION)
    return invitation


def _consume(invitation):
    if ClassInvitation.objects.filter(pk=invitation.pk, revoked_at__isnull=True,
        expires_at__gt=timezone.now(), uses__lt=F("max_uses")).update(uses=F("uses") + 1) != 1:
        raise ValidationError(FULL_INVITATION)


def _new_student(username, name, password):
    User = get_user_model()
    if not isinstance(username, str) or not isinstance(name, str):
        raise ValidationError("请填写有效的学号和姓名。")
    username = User.normalize_username(username.strip())
    name = name.strip()
    User._meta.get_field("username").clean(username, None)
    if not name or len(name) > 150:
        raise ValidationError("姓名须为 1 至 150 个字符。")
    if User.objects.filter(username=username).exists():
        raise ValidationError(EXISTING_ACCOUNT)
    student = User(username=username, first_name=name, is_active=True, is_staff=False, is_superuser=False)
    if not isinstance(password, str) or len(password) > 256:
        raise ValidationError("请填写有效密码，最长 256 个字符。")
    validate_password(password, student)
    # Hash outside the write transaction; validate the invitation again before committing.
    student.set_password(password)
    return student


def register_student(code, username, name, password):
    _load_invitation(code)
    student = _new_student(username, name, password)
    try:
        with transaction.atomic():
            invitation = _load_invitation(code, lock=True)
            if get_user_model().objects.filter(username=student.username).exists():
                raise ValidationError(EXISTING_ACCOUNT)
            student.save(force_insert=True)
            Enrollment.objects.create(student=student, teaching_class_id=invitation.teaching_class_id)
            _consume(invitation)
    except IntegrityError:
        # A concurrent signup for this identifier must never update the winning account.
        if get_user_model().objects.filter(username=student.username).exists():
            raise ValidationError(EXISTING_ACCOUNT) from None
        raise
    return student


@transaction.atomic
def join_class(user, code):
    if not getattr(user, "is_authenticated", False):
        raise ValidationError("请先登录学生账号。")
    student = get_user_model().objects.select_for_update().filter(pk=user.pk, is_active=True).first()
    if student is None or not student.has_usable_password() or student.is_staff or student.is_superuser or (
        student.teaching_assignments.exists() or student.course_editorships.exists()
    ):
        raise ValidationError("请使用有效的学生账号加入班级。")
    invitation = _load_invitation(code, lock=True, require_capacity=False)
    enrollment = Enrollment.objects.filter(student=student, teaching_class_id=invitation.teaching_class_id).first()
    if enrollment is not None:
        if not enrollment.active:
            raise ValidationError("你在该班的选课已停用，请联系任课老师处理。")
        return enrollment
    _consume(invitation)
    return Enrollment.objects.create(student=student, teaching_class_id=invitation.teaching_class_id)
