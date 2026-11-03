from django.conf import settings
from django.db import models


class Term(models.Model):
    code = models.CharField(max_length=40, unique=True)

    def __str__(self):
        return self.code


class Course(models.Model):
    code = models.CharField(max_length=60, unique=True)
    title = models.CharField(max_length=160)

    def __str__(self):
        return self.title


class TeachingClass(models.Model):
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="classes")
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="classes")
    code = models.CharField(max_length=80)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["term", "course", "code"], name="unique_course_class")]

    def __str__(self):
        return f"{self.term} · {self.course} · {self.code}"


class Enrollment(models.Model):
    teaching_class = models.ForeignKey(TeachingClass, on_delete=models.PROTECT, related_name="enrollments")
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="enrollments")
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["teaching_class", "student"], name="unique_class_enrollment")]


class TeachingAssignment(models.Model):
    teaching_class = models.ForeignKey(TeachingClass, on_delete=models.PROTECT, related_name="teaching_assignments")
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="teaching_assignments")
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["teaching_class", "teacher"], name="unique_class_staff")]


class CourseEditor(models.Model):
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="editors")
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="course_editorships")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["course", "teacher"], name="unique_course_editor")]


class ActivationOrReset(models.Model):
    ACTIVATE = "activate"
    RESET = "reset"
    PURPOSES = [(ACTIVATE, "首次激活"), (RESET, "重设密码")]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="account_codes")
    purpose = models.CharField(max_length=10, choices=PURPOSES)
    digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)
