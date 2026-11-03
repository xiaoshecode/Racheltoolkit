from django.urls import path
from . import views

app_name = "grading"
urlpatterns = [
    path("teacher/submissions/<uuid:pk>/review/", views.review, name="review"),
    path("teacher/assignments/<int:pk>/grades/", views.release_grades, name="publish"),
    path("teacher/assignments/<int:pk>/answer-key/", views.teacher_answers, name="teacher_answers"),
    path("assignments/<int:pk>/answer-key/", views.answers, name="answers"),
    path("me/grades/", views.grades, name="grades"),
    path("me/grades/<int:pk>/", views.grade_detail, name="grade_detail"),
    path("teacher/grades/<int:pk>/", views.grade_detail, {"teacher": True}, name="teacher_grade"),
    path("me/mistakes/", views.mistakes, name="mistakes"),
    path("me/mistakes/<int:pk>/review/", views.review_card, name="review_card"),
]
for action in ("download", "preview", "preview-status"):
    urlpatterns += [
        path(
            f"assignments/<int:pk>/answer-key/<int:revision>/{action}/", views.answer_file, {"action": action}
        ),
        path(
            f"teacher/assignments/<int:pk>/answer-key/<int:revision>/{action}/",
            views.answer_file,
            {"action": action, "teacher": True},
        ),
    ]
