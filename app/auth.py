from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings


security = HTTPBasic()


def require_admin(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    settings = get_settings()
    username_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid sysadmin username or password.",
            headers={"WWW-Authenticate": "Basic realm=\"Machine Research Sysadmin\""},
        )
    return credentials.username


def require_collector_token(
    x_collector_token: Annotated[str | None, Header(alias="X-Collector-Token")] = None,
) -> str:
    settings = get_settings()
    expected = settings.collector_api_token or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Collector API token is not configured on the hub.",
        )
    if not x_collector_token or not secrets.compare_digest(x_collector_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing collector API token.",
        )
    return x_collector_token
