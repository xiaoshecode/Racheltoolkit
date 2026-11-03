from django import forms
from .models import Assignment

class AssignmentForm(forms.ModelForm):
    class Meta:
        model = Assignment
        fields = ["title","instructions","chapter_key","due_at","max_points"]
        labels = {"title":"作业标题","instructions":"作业要求","chapter_key":"关联章节编号（可选）",
                  "due_at":"北京时间截止","max_points":"满分"}
        widgets = {"due_at":forms.DateTimeInput(attrs={"type":"datetime-local"})}

class ExtensionForm(forms.Form):
    until = forms.DateTimeField(label="新截止时间（北京时间）",widget=forms.DateTimeInput(attrs={"type":"datetime-local"}))
    reason = forms.CharField(label="延期原因",max_length=300)

