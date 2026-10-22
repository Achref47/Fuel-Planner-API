"""One structured error format for every failure the API can produce.

{"error": {"code": "<machine_readable>", "message": "<human readable>", "details": {...}}}
"""

import logging

from django.http import JsonResponse
from rest_framework import exceptions as drf
from rest_framework.response import Response
from rest_framework.views import exception_handler

from ..errors import PlannerError

logger = logging.getLogger(__name__)

STATUS_CODES = {
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    415: "unsupported_media_type",
}


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _plain(detail):
    """DRF ErrorDetail structures -> plain JSON-able lists / dicts / strings."""
    if isinstance(detail, dict):
        return {key: _plain(value) for key, value in detail.items()}
    if isinstance(detail, list | tuple):
        return [_plain(item) for item in detail]
    return str(detail)


def api_exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        response = Response(error_body(exc.code, exc.message, exc.details), status=exc.http_status)
        retry_after = exc.details.get("retry_after_seconds")
        if retry_after is not None and str(retry_after).isdigit():
            response["Retry-After"] = str(retry_after)
        return response

    response = exception_handler(exc, context)  # sets headers such as Allow / Retry-After
    if response is None:
        logger.error("unhandled error in %s", context["view"].__class__.__name__, exc_info=exc)
        return Response(
            error_body("internal_error", "An unexpected error occurred. Please try again."),
            status=500,
        )

    if isinstance(exc, drf.ValidationError):
        details = _plain(exc.detail)
        if not isinstance(details, dict):
            details = {"non_field_errors": details}
        response.data = error_body("validation_error", "The request is invalid.", details)
    elif isinstance(exc, drf.ParseError):
        response.data = error_body("invalid_json", "The request body is not valid JSON.")
    elif isinstance(exc, drf.Throttled):
        wait = int(exc.wait) + 1 if exc.wait else None
        response.data = error_body(
            "rate_limited",
            "Too many requests. Please slow down.",
            {"retry_after_seconds": wait} if wait else {},
        )
    else:
        code = STATUS_CODES.get(response.status_code, f"http_{response.status_code}")
        response.data = error_body(code, str(exc.detail))
    return response


def json_404(request, exception=None):
    return JsonResponse(error_body("not_found", "No such endpoint."), status=404)


def json_500(request):
    return JsonResponse(error_body("internal_error", "An unexpected error occurred."), status=500)
