from enum import StrEnum


class Role(StrEnum):
    VIEWER = 'viewer'
    ANALYST = 'analyst'
    SUPERVISOR = 'supervisor'


class RunStatus(StrEnum):
    QUEUED = 'queued'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'


class Decision(StrEnum):
    APPROVED = 'approved'
    RETURNED = 'returned'


class AdjustmentStatus(StrEnum):
    PENDING = 'pending'
    SUBMITTED = 'submitted'
    SETTLED = 'settled'


class SettlementOutcome(StrEnum):
    SUCCESS = 'success'
    FAILED = 'failed'
