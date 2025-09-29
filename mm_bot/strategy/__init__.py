"""Trading strategies."""

from .smoke_test import ConnectorTestStrategy, ConnectorTestParams, ConnectorTestTask
from .connector_diagnostics import ConnectorDiagnostics, DiagnosticParams, DiagnosticTask, DiagnosticReport

__all__ = [
    # Legacy smoke test (deprecated)
    "ConnectorTestStrategy",
    "ConnectorTestParams",
    "ConnectorTestTask",
    # New diagnostic template (recommended)
    "ConnectorDiagnostics",
    "DiagnosticParams",
    "DiagnosticTask",
    "DiagnosticReport",
]