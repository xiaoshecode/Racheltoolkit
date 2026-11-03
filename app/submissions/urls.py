from django.urls import path
from . import views

urlpatterns = [
    path("assignments/",views.index,name="assignments"),
    path("assignments/<int:pk>/",views.detail,name="assignment"),
    path("teacher/classes/<int:class_id>/assignments/new/",views.create),
    path("teacher/assignments/<int:pk>/extend/",views.extension),
    path("assignments/<int:pk>/submissions/",views.upload),
    path("teacher/assignments/<int:pk>/students/<int:student_id>/submissions/",views.upload),
    path("assignments/<int:pk>/submission-status/",views.status),
    path("submissions/<uuid:pk>/",views.receipt),
    path("submissions/<uuid:pk>/download/",views.attachment),
    path("submissions/<uuid:pk>/attachments/<uuid:aid>/download/",views.attachment),
    path("submissions/<uuid:pk>/attachments/<uuid:aid>/preview/",views.attachment,{"action":"preview"}),
    path("submissions/<uuid:pk>/attachments/<uuid:aid>/preview-status/",views.attachment,{"action":"preview-status"}),
]

