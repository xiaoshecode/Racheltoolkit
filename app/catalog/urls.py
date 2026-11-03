from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="catalog_home"),
    path("api/public/courses/", views.public_index, name="catalog_api"),
    path("courses/<int:course_id>/", views.course, name="catalog_course"),
    path("courses/<int:course_id>/chapters/<uuid:chapter_key>/", views.course, name="catalog_chapter"),
    path("courses/<int:course_id>/files/<uuid:key>/<uuid:blob>/<str:mode>/", views.serve, name="catalog_file"),
    path("teacher/courses/", views.teacher_courses, name="catalog_teacher_courses"),
    path("teacher/courses/<int:course_id>/", views.editor, name="catalog_editor"),
    path("teacher/courses/<int:course_id>/preview/", views.draft_preview, name="catalog_preview"),
    path("teacher/courses/<int:course_id>/files/<uuid:key>/<uuid:blob>/<str:mode>/",
         views.serve, {"draft": True}, name="catalog_draft_file"),
]
