import hashlib

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .accounts import redeem_code
from .models import ActivationOrReset, ClassInvitation
from .invitations import issue_invitation, join_class, register_student, revoke_invitation
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
@sensitive_post_parameters("code", "password", "confirmation")
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


class InvitationCodeForm(forms.Form):
    code = forms.CharField(label="班级邀请码", max_length=128,
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
        help_text="请向任课老师领取，注意区分大小写。")


class RegistrationForm(InvitationCodeForm):
    username = forms.CharField(label="完整学号", max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "username", "autocapitalize": "none"}),
        help_text="保留开头的零；注册后使用该学号登录。")
    name = forms.CharField(label="姓名", max_length=150,
        widget=forms.TextInput(attrs={"autocomplete": "name"}))
    password = forms.CharField(label="密码", strip=False, max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="至少 8 位，请勿使用纯数字、常见密码或与学号相似的密码。")
    confirmation = forms.CharField(label="再次输入密码", strip=False, max_length=256,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("confirmation"):
            self.add_error("confirmation", "两次密码输入不一致。")
        return cleaned


@never_cache
@sensitive_post_parameters("code", "password", "confirmation")
@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("school:join")
    form = RegistrationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            register_student(form.cleaned_data["code"], form.cleaned_data["username"],
                form.cleaned_data["name"], form.cleaned_data["password"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "注册成功，已加入班级。请使用学号和刚设置的密码登录。")
            return redirect("school:login")
    return render(request, "school/register.html", {"form": form})


@never_cache
@login_required
@sensitive_post_parameters("code")
@require_http_methods(["GET", "POST"])
def join(request):
    form = InvitationCodeForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            enrollment = join_class(request.user, form.cleaned_data["code"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, f"已加入 {enrollment.teaching_class}。")
            return redirect("school:me")
    return render(request, "school/join.html", {"form": form})


class IssueInvitationForm(forms.Form):
    teaching_class = forms.ModelChoiceField(label="教学班级", queryset=None)
    valid_days = forms.IntegerField(label="有效天数", min_value=1, max_value=30, initial=7)
    max_uses = forms.IntegerField(label="最多加入人数", min_value=1, max_value=500, initial=30,
        help_text="新注册和已有账号加入班级共用此额度。")

    def __init__(self, *args, classes, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["teaching_class"].queryset = classes


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def invitations(request):
    classes = teacher_classes(request.user).select_related("term", "course")
    if not classes.exists():
        raise Http404
    issuing = request.method == "POST" and request.POST.get("action") == "issue"
    form = IssueInvitationForm(request.POST if issuing else None, classes=classes)
    code, issued, error = None, None, ""
    if request.method == "POST":
        if issuing and form.is_valid():
            try:
                code, issued = issue_invitation(request.user, **form.cleaned_data)
            except ValidationError as exc:
                form.add_error(None, exc)
        elif request.POST.get("action") == "revoke":
            try:
                revoke_invitation(request.user, int(request.POST.get("invitation", "")))
            except (ValidationError, ValueError) as exc:
                error = "；".join(exc.messages) if isinstance(exc, ValidationError) else "邀请码记录无效。"
            else:
                messages.success(request, "邀请码已停用，已加入的学生不受影响。")
                return redirect("school:invitations")
        elif not issuing:
            error = "请选择生成或停用邀请码。"
    invites = ClassInvitation.objects.filter(teaching_class__in=classes).select_related(
        "teaching_class__term", "teaching_class__course", "created_by").order_by("-created_at")[:50]
    return render(request, "school/invitations.html", {"form": form, "invitations": invites,
        "code": code, "issued": issued, "error": error, "now": timezone.now(),
        "registration_url": request.build_absolute_uri(reverse("school:register"))})


@require_http_methods(["GET"])
def help_page(request):
    return render(request, "school/help.html")
