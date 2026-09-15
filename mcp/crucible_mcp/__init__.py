"""Core building blocks for the Crucible MCP service."""

from importlib import import_module


_LAZY_EXPORTS = {
    "Job": (".models", "Job"),
    "JobConflictError": (".jobs", "JobConflictError"),
    "JobNotFoundError": (".jobs", "JobNotFoundError"),
    "JobStore": (".jobs", "JobStore"),
    "JobState": (".models", "JobState"),
    "ResultStatus": (".models", "ResultStatus"),
    "CrucibleOperations": (".operations", "CrucibleOperations"),
    "OperationError": (".operations", "OperationError"),
    "RunManager": (".runner", "RunManager"),
    "AuditLogger": (".audit", "AuditLogger"),
}

__all__ = [
    "Job",
    "JobConflictError",
    "JobNotFoundError",
    "JobState",
    "JobStore",
    "CrucibleOperations",
    "OperationError",
    "RunManager",
    "AuditLogger",
    "ResultStatus",
]


def __getattr__(name):
    """Load optional MCP components only when their exports are requested."""

    export = _LAZY_EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = export
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
