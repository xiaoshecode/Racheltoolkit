from django.urls import path
from . import views
from .models import ActivationOrReset

app_name = "school"
urlpatterns = [
    path("login/", views.SchoolLogin.as_view(), name="login"),
    path("logout/", views.SchoolLogout.as_view(), name="logout"),
    path("activate/", views.credential, {"purpose": ActivationOrReset.ACTIVATE}, name="activate"),
    path("reset/", views.credential, {"purpose": ActivationOrReset.RESET}, name="reset"),
    path("me/", views.me, name="me"),
    path("teacher/roster/", views.roster, name="roster"),
]
