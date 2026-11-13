from django.urls import path
from . import views
from .chat_uploads import upload_attachments
from .models import ActivationOrReset

app_name = "school"
urlpatterns = [
    path("login/", views.SchoolLogin.as_view(), name="login"),
    path("register/", views.register, name="register"),
    path("join/", views.join, name="join"),
    path("help/", views.help_page, name="help"),
    path("logout/", views.SchoolLogout.as_view(), name="logout"),
    path("activate/", views.credential, {"purpose": ActivationOrReset.ACTIVATE}, name="activate"),
    path("reset/", views.credential, {"purpose": ActivationOrReset.RESET}, name="reset"),
    path("me/", views.me, name="me"),
    path("assistant/", views.assistant, name="assistant"),
    path("api/assistant/chat/", views.chat_completion, name="chat_completion"),
    path("api/assistant/files/", upload_attachments, name="chat_uploads"),
    path("teacher/roster/", views.roster, name="roster"),
    path("teacher/invitations/", views.invitations, name="invitations"),
]
