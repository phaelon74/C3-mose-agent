"""Shared timing for interactive admin approvals and their callers."""

ADMIN_APPROVAL_TIMEOUT_SECONDS = 60 * 60
# Allow prompt delivery and the approval response to travel over the bridge.
APPROVAL_REQUEST_TIMEOUT_SECONDS = ADMIN_APPROVAL_TIMEOUT_SECONDS + 60
