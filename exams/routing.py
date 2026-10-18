"""Marks routes that follow the course-exams error contract
(COURSE_EXAMS_CERTIFICATES.md section 10, rule 2): a 422 has `detail` as a
plain string plus `errors: [{loc, msg}]`, and never echoes the input.
core/error_handlers.py recognises these routes by class, not by path."""

from fastapi.routing import APIRoute


class ExamContractRoute(APIRoute):
    pass
