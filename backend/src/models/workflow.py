from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.constants.enums import AdjustmentStatus, RunStatus
from src.models.base import Base, utc_now


class Observation(Base):
    __tablename__ = 'observations'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(96), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AnalysisRun(Base):
    __tablename__ = 'analysis_runs'
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    owner: Mapped[str] = mapped_column(String(60))
    parameters: Mapped[dict] = mapped_column(JSON)
    last_observation_id: Mapped[int] = mapped_column(Integer)
    sample_size: Mapped[int] = mapped_column(Integer)
    observation_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED, index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Review(Base):
    __tablename__ = 'reviews'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), unique=True)
    decision: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Schedule(Base):
    __tablename__ = 'schedules'
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey('analysis_runs.id'), index=True)
    recipient: Mapped[str] = mapped_column(String(100))
    interval_minutes: Mapped[int] = mapped_column(Integer)
    next_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Delivery(Base):
    __tablename__ = 'deliveries'
    __table_args__ = (UniqueConstraint('schedule_id', 'scheduled_at'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('schedules.id'))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime)
    filename: Mapped[str] = mapped_column(String(120))
    delivered_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Bill(Base):
    """An immutable original hot-water bill, frozen with its own tariff rates."""
    __tablename__ = 'bills'
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    run_id: Mapped[int | None] = mapped_column(ForeignKey('analysis_runs.id'), nullable=True)
    owner: Mapped[str] = mapped_column(String(60))
    rates: Mapped[dict] = mapped_column(JSON)
    original_rows: Mapped[list] = mapped_column(JSON)
    original_view: Mapped[dict] = mapped_column(JSON)
    latest_view: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class LateReading(Base):
    """A meter bottom that arrived after the bill period was already settled."""
    __tablename__ = 'late_readings'
    __table_args__ = (UniqueConstraint('bill_id', 'meter_code', 'point'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey('bills.id'), index=True)
    meter_code: Mapped[str] = mapped_column(String(60))
    pool_zone: Mapped[str] = mapped_column(String(30))
    reading_date: Mapped[str] = mapped_column(String(10))
    reading_minute: Mapped[int] = mapped_column(Integer)
    point: Mapped[int] = mapped_column(Integer)
    reading_liters: Mapped[int] = mapped_column(Integer)
    source_key: Mapped[str] = mapped_column(String(96))
    note: Mapped[str] = mapped_column(String(200), default='')
    adjustment_id: Mapped[int | None] = mapped_column(ForeignKey('adjustments.id'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Adjustment(Base):
    """A make-up batch. Pending rows are live; submitted rows freeze the amount."""
    __tablename__ = 'adjustments'
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey('bills.id'), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=AdjustmentStatus.PENDING, index=True)
    delta: Mapped[dict] = mapped_column(JSON)
    amount_cents: Mapped[int] = mapped_column(Integer)
    submitted_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settled_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class SettlementAttempt(Base):
    """Idempotency log: a retried checkout never posts the make-up twice."""
    __tablename__ = 'settlement_attempts'
    __table_args__ = (UniqueConstraint('idempotency_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    adjustment_id: Mapped[int] = mapped_column(ForeignKey('adjustments.id'), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    outcome: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(String(200), default='')
    actor: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
