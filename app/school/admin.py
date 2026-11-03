from django.contrib import admin

from .models import Course, CourseEditor, Enrollment, TeachingAssignment, TeachingClass, Term

admin.site.register([Course, Term, TeachingClass, Enrollment, TeachingAssignment, CourseEditor])
