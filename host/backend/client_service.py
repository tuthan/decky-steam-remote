"""Compatibility import for the outgoing client service.

Keeping this small alias makes the service name discoverable to integrations
that use ``client_service`` while the implementation remains in ``client``.
"""

from .client import (
    BoundedWorkQueue,
    ClientService,
    Settlement,
    default_client_name,
    settle_once,
)

__all__ = ["BoundedWorkQueue", "ClientService", "Settlement", "default_client_name", "settle_once"]
