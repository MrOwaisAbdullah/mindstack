"""Security middleware + decorators + lockout primitives (9.2 + 9.3 + 9.4 + 10.1)."""
from apps.core.security.admin_required import admin_required
from apps.core.security.headers import (
    SecurityHeadersMiddleware,
    build_csp_header_value,
    relax_csp,
)
from apps.core.security.honeypot import HoneypotMiddleware, is_ip_honeypot_blocked
from apps.core.security.ip_allowlist import AdminIPAllowlistMiddleware, is_ip_allowed
from apps.core.security.lockout import (
    LockoutResult,
    clear_api_key_fail,
    clear_failures,
    extract_api_key_prefix,
    force_unlock,
    is_locked,
    is_token_locked,
    record_api_key_fail,
    record_failure,
)
from apps.core.security.maintenance import MaintenanceModeMiddleware

__all__ = [
    "AdminIPAllowlistMiddleware",
    "HoneypotMiddleware",
    "LockoutResult",
    "MaintenanceModeMiddleware",
    "SecurityHeadersMiddleware",
    "admin_required",
    "build_csp_header_value",
    "clear_api_key_fail",
    "clear_failures",
    "extract_api_key_prefix",
    "force_unlock",
    "is_ip_allowed",
    "is_ip_honeypot_blocked",
    "is_locked",
    "is_token_locked",
    "record_api_key_fail",
    "record_failure",
    "relax_csp",
]
