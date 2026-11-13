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


def assistant_access(user):
    return bool(user.is_authenticated and user.is_active and (
        user.is_staff or student_classes(user).exists() or teacher_classes(user).exists()
        or editable_courses(user).exists()
    ))
