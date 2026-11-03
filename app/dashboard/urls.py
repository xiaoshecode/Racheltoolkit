from django.urls import path
from . import views

urlpatterns = [
    path("dashboard/", views.dashboard, name="teacher_dashboard"),
    path("teacher/students/", views.dashboard, name="teacher_students"),
    path("teacher/students/<int:pk>/", views.student_detail, name="teacher_student_detail"),
    path("teacher/gradebook.csv", views.export, name="teacher_gradebook_csv"),
]
