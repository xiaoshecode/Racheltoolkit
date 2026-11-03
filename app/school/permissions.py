from .models import Course, TeachingClass


def student_classes(user):
    if not user.is_authenticated or not user.is_active:
        return TeachingClass.objects.none()
    return TeachingClass.objects.filter(enrollments__student=user, enrollments__active=True)


def teacher_classes(user):
    if not user.is_authenticated or not user.is_active:
        return TeachingClass.objects.none()
    return TeachingClass.objects.filter(teaching_assignments__teacher=user, teaching_assignments__active=True)


def editable_courses(user):
    if not user.is_authenticated or not user.is_active:
        return Course.objects.none()
    return Course.objects.filter(editors__teacher=user)
