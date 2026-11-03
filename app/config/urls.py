from django.http import JsonResponse
from django.contrib import admin
from django.urls import include, path

def health(request):
    return JsonResponse({"status": "ok"})

urlpatterns = [path("health/", health, name="health"), path("admin/", admin.site.urls),
               path("", include("school.urls"))]

