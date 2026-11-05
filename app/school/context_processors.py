from django.conf import settings

from .permissions import editable_courses, student_classes, teacher_classes


def workspace(request):
    """Navigation follows independent membership scopes; views still enforce access."""
    user = request.user
    trial = {"teaching_trial": getattr(settings, "CODESPACES_TRIAL", False)}
    if not user.is_authenticated:
        return trial
    path = request.path
    section = "home"
    for prefix, key in (("/teacher/courses/", "editor"), ("/teacher/roster/", "roster"),
                        ("/teacher/invitations/", "invitations"), ("/join/", "join"), ("/help/", "help"),
                        ("/dashboard/", "reports"), ("/teacher/grades/", "reports"),
                        ("/teacher/submissions/", "assignments"), ("/teacher/assignments/", "assignments"),
                        ("/me/grades/", "grades"), ("/me/mistakes/", "mistakes"),
                        ("/assignments/", "assignments"), ("/submissions/", "assignments"),
                        ("/courses/", "catalog")):
        if path.startswith(prefix):
            section = key
            break
    if path == "/":
        section = "catalog"
    elif path.startswith("/teacher/students/"):
        section = "reports"
    elif path.startswith("/teacher/classes/"):
        section = "assignments"
    return {**trial, "workspace_teacher": teacher_classes(user).exists(),
            "workspace_editor": editable_courses(user).exists(),
            "workspace_student": student_classes(user).exists(), "workspace_section": section}
