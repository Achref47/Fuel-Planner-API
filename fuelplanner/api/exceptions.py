"""DRF exception handling (extended with the structured error format in the API stage)."""

from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    return exception_handler(exc, context)
