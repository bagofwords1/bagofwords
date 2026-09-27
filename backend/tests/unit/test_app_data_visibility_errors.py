"""Layer-1 visibility failures map onto the app-data error contract.

`report_service._check_visibility` signals with HTTPException: 401 and 403
become the app-data codes, 404 (a private report the caller cannot see) is
`artifact.not_found`, and any other status is not a visibility decision at
all, so it propagates unchanged instead of being disguised as a 404.
"""
import pytest
from fastapi import HTTPException

from app.errors import AppError, ErrorCode
from app.services.app_data_service import visibility_error


@pytest.mark.parametrize("status, code", [
    (401, ErrorCode.APP_DATA_UNAUTHENTICATED),
    (403, ErrorCode.APP_DATA_FORBIDDEN),
    (404, ErrorCode.ARTIFACT_NOT_FOUND),
])
def test_visibility_decisions_become_app_data_errors(status, code):
    err = visibility_error(HTTPException(status_code=status, detail="x"))
    assert isinstance(err, AppError)
    assert err.error_code == code
    assert err.status_code == status


@pytest.mark.parametrize("status", [400, 409, 422, 500, 503])
def test_other_statuses_propagate_unchanged(status):
    original = HTTPException(status_code=status, detail="upstream")
    assert visibility_error(original) is original
