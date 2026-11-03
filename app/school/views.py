import hashlib

from django import forms
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .accounts import redeem_code
from .models import ActivationOrReset
from .permissions import editable_courses, student_classes, teacher_classes
from .roster import check_roster, import_roster


class SchoolLogin(LoginView):
    template_name = "school/login.html"
    next_page = "/me/"


class SchoolLogout(LogoutView):
    next_page = "/login/"


class CodeForm(forms.Form):
    username = forms.CharField(label="学号或教师账号", max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "username"}))
    code = forms.CharField(label="一次性凭证", max_length=128,
        widget=forms.PasswordInput(attrs={"autocomplete": "one-time-code"}))
    password = forms.CharField(label="新密码", strip=False, max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    confirmation = forms.CharField(label="再次输入新密码", strip=False, max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("confirmation"):
            self.add_error("confirmation", "两次密码输入不一致。")
        return cleaned


@never_cache
@require_http_methods(["GET", "POST"])
def credential(request, purpose):
    form = CodeForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            redeem_code(form.cleaned_data["username"], purpose, form.cleaned_data["code"], form.cleaned_data["password"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect(reverse("school:login") + "?updated=1")
    return render(request, "school/credential.html", {"form": form,
        "title": "首次激活" if purpose == ActivationOrReset.ACTIVATE else "重设密码"})


@never_cache
@login_required
def me(request):
    return render(request, "school/me.html", {
        "classes": student_classes(request.user).select_related("term", "course"),
        "teaching": teacher_classes(request.user).select_related("term", "course"),
        "editing": editable_courses(request.user),
    })


class RosterForm(forms.Form):
    contents = forms.CharField(label="名册 CSV", max_length=100_000, strip=False,
        widget=forms.Textarea(attrs={"rows": 10}))


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def roster(request):
    classes = teacher_classes(request.user).select_related("term", "course")
    if not classes.exists():
        raise Http404
    form = RosterForm(request.POST if request.method == "POST" else None)
    rows, summary = [], ""
    if request.method == "POST" and form.is_valid():
        contents = form.cleaned_data["contents"]
        digest = hashlib.sha256(contents.encode()).hexdigest()
        try:
            if request.POST.get("action") == "apply":
                if digest != request.session.get("roster_preview"):
                    raise ValidationError("名册有改动，请重新预检。")
                accounts, enrollments = import_roster(contents, request.user)
                request.session.pop("roster_preview", None)
                summary = f"已导入，新增 {accounts} 个账号、{enrollments} 条选课关系。"
            else:
                rows = check_roster(contents, request.user)
                request.session["roster_preview"] = digest
        except ValidationError as error:
            form.add_error(None, error)
    return render(request, "school/roster.html", {"classes": classes, "form": form,
        "rows": rows, "summary": summary})
