import csv
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_safe

from grading.models import ReleasedGrade
from school.permissions import teacher_classes
from submissions.models import Assignment, Submission


STATUS = {"missing": "未交", "submitted": "已交待批", "draft": "草稿待发布", "released": "已发布"}


def number(value):
    return format(Decimal(value).quantize(Decimal(".01")), "f").rstrip("0").rstrip(".")


def identifier(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        raise Http404 from None


def report_data(request):
    groups = teacher_classes(request.user).select_related("term", "course").order_by("pk")
    choice = request.GET.get("class")
    group = get_object_or_404(groups, pk=identifier(choice)) if choice else groups.first()
    if group is None:
        raise Http404
    all_work = Assignment.objects.filter(teaching_class=group).order_by("-published_at", "-pk")
    task_id = request.GET.get("assignment", "")
    work = [get_object_or_404(all_work, pk=identifier(task_id))] if task_id else list(all_work)
    students = get_user_model().objects.filter(is_active=True, enrollments__teaching_class=group,
                                                enrollments__active=True).order_by("username")
    receipts = Submission.objects.filter(assignment__in=work, student__in=students, state="confirmed").select_related(
        "grade_draft").order_by("version")
    latest = {(r.student_id, r.assignment_id): r for r in receipts}
    grades = ReleasedGrade.objects.filter(release__assignment__in=work, student__in=students).select_related(
        "release", "submission").order_by("release_id")
    published = {(g.student_id, g.release.assignment_id): g for g in grades}
    query = request.GET.get("q","").strip()[:160]
    selected_status = request.GET.get("status","")
    if selected_status and selected_status not in STATUS:
        raise Http404
    columns = []
    for task in work:
        countable = [g for (_, aid), g in published.items() if aid == task.pk]
        values = {}
        if countable:
            total = sum(g.score for g in countable)
            values = {"mean": number(total/len(countable)),
                      "percent": number(100*total/sum(g.max_points for g in countable)), "count": len(countable)}
        columns.append({"task":task, **values})
    rows = []
    for student in students:
        if query and query != student.username and query.casefold() not in student.first_name.casefold():
            continue
        cells = []
        for task in work:
            receipt, grade = latest.get((student.pk,task.pk)), published.get((student.pk,task.pk))
            draft = getattr(receipt,"grade_draft",None) if receipt else None
            if receipt is None:
                status = "missing"
            elif grade and grade.submission_id == receipt.pk and (not draft or draft.edit_version == grade.draft_version):
                status = "released"
            elif draft:
                status = "draft"
            else:
                status = "submitted"
            cells.append({"task":task, "receipt":receipt, "grade":grade, "status":status, "label":STATUS[status],
                          "score":number(grade.score) if grade else None,
                          "score_percent":number(100 * grade.score / grade.max_points) if grade else None,
                          "score_band":min(3, int(4 * grade.score / grade.max_points)) if grade else None})
        if selected_status and not any(c["status"]==selected_status for c in cells):
            continue
        rows.append({"student": student, "cells":cells})
    filters = {"class":group.pk, "q":query, "assignment":task_id, "status":selected_status}
    page = Paginator(rows, 25).get_page(request.GET.get("page"))
    return {"groups":groups, "group":group, "choices":all_work, "columns":columns, "rows":rows, "page":page,
            "student_count":students.count(), "column_count":len(columns), "table_colspan":len(columns) + 1,
            "filters":filters, "statuses":STATUS, "querystring":urlencode(filters),
            "back_query":urlencode({**filters,"page":page.number})}


@login_required
@require_safe
def dashboard(request):
    return render(request, "dashboard/report.html", report_data(request))


@login_required
@require_safe
def student_detail(request, pk):
    data = report_data(request)
    student = get_object_or_404(get_user_model(), pk=pk, is_active=True,
                                enrollments__teaching_class=data["group"], enrollments__active=True)
    row = next((item for item in data["rows"] if item["student"] == student), None)
    if row is None:
        raise Http404
    return render(request,"dashboard/student.html",{**data,"row":row})


def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


@login_required
@require_safe
def export(request):
    data = report_data(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="class-gradebook.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["班级","学号","姓名","作业","当前状态","最新提交版本","已发布版本","得分","满分","已发布评语"])
    for row in data["rows"]:
        user = row["student"]
        for cell in row["cells"]:
            task, grade = cell["task"], cell["grade"]
            writer.writerow(map(safe_cell,[data["group"].code, user.username, user.first_name, task.title, cell["label"],
                                cell["receipt"].version if cell["receipt"] else "",
                                grade.submission.version if grade else "",cell["score"] if grade else "",
                                grade.max_points if grade else task.max_points,grade.feedback if grade else ""]))
    return response
