from django import template
from django.db.models import Exists, OuterRef
from django.utils import timezone

from grading.models import ReleasedGrade
from school.permissions import student_classes
from submissions.models import Assignment, Submission

register = template.Library()


@register.inclusion_tag("dashboard/student_home.html")
def student_home(user):
    now = timezone.now()
    groups = student_classes(user)
    work = Assignment.objects.filter(teaching_class__in=groups, published_at__lte=now)
    receipts = Submission.objects.filter(student=user, assignment__in=work, state="confirmed")
    current = receipts.filter(~Exists(receipts.filter(assignment_id=OuterRef("assignment_id"),
                                                       version__gt=OuterRef("version"))))
    pending = work.filter(due_at__gt=now).exclude(pk__in=receipts.values("assignment_id")).order_by("due_at")
    last = ReleasedGrade.objects.filter(student=user, release__assignment__in=work).select_related(
        "release__assignment","submission").order_by("-release_id").first()
    return {"pending":pending[:3], "receipt":current.select_related("assignment").order_by("-received_at", "-pk").first(),
            "grade":last,"groups":groups.select_related("course", "term")}
