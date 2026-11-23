"""Orchestration for late-reading make-up bills and idempotent settlement."""

from sqlalchemy import select

from src.constants.enums import AdjustmentStatus, SettlementOutcome
from src.domain import billing
from src.domain.billing import BillingError, LatePoint
from src.models.base import utc_now
from src.models.workflow import (
    Adjustment, AnalysisRun, AuditEvent, Bill, LateReading, Observation, SettlementAttempt,
)
from src.services.errors import BusinessError


def _bill_or_404(db, bill_id, *, lock=False):
    bill = db.get(Bill, bill_id, with_for_update=lock)
    if bill is None:
        raise BusinessError('账单不存在', 404)
    return bill


def _late_points(readings):
    return [
        LatePoint(meter_code=item.meter_code, point=item.point, reading_liters=item.reading_liters,
                  source_key=item.source_key, note=item.note)
        for item in readings
    ]


def _reading_view(item):
    return {
        'id': item.id, 'meter_code': item.meter_code, 'pool_zone': item.pool_zone,
        'reading_date': item.reading_date, 'reading_minute': item.reading_minute,
        'reading_m3': item.reading_liters / 1000, 'note': item.note,
        'adjustment_id': item.adjustment_id,
    }


def _recompute(bill, readings):
    points = _late_points(readings)
    _meters, _zones, view = billing.recompute(bill.original_rows, bill.rates, points)
    return view


def _refresh_pending(db, bill, all_readings):
    """Keep the single live pending adjustment aligned with every unfrozen bottom.

    Frozen bottoms belong to submitted or settled adjustments and never change;
    the pending delta is measured against the perspective that excludes them.
    """
    unfrozen = [item for item in all_readings if item.adjustment_id is None]
    if not unfrozen:
        stale = db.scalar(select(Adjustment).where(
            Adjustment.bill_id == bill.id, Adjustment.status == AdjustmentStatus.PENDING))
        if stale is not None:
            db.delete(stale)
        return None
    frozen_readings = [item for item in all_readings if item.adjustment_id is not None]
    before_view = _recompute(bill, frozen_readings)
    latest_view = _recompute(bill, all_readings)
    delta = billing.build_delta(before_view, latest_view, [_reading_view(item) for item in unfrozen])
    pending = db.scalar(select(Adjustment).where(
        Adjustment.bill_id == bill.id, Adjustment.status == AdjustmentStatus.PENDING))
    if pending is None:
        seq = (db.scalar(select(Adjustment.seq).where(Adjustment.bill_id == bill.id)
                         .order_by(Adjustment.seq.desc()).limit(1)) or 0) + 1
        pending = Adjustment(bill_id=bill.id, seq=seq, status=AdjustmentStatus.PENDING,
                             delta=delta, amount_cents=delta['total_cost_delta_cents'])
        db.add(pending)
        db.flush()
    else:
        pending.delta = delta
        pending.amount_cents = delta['total_cost_delta_cents']
    for item in unfrozen:
        item.adjustment_id = pending.id
    bill.latest_view = billing.snapshot_view(latest_view)
    return pending


def create_bill(db, request, actor):
    run = db.get(AnalysisRun, request.run_id)
    if run is None:
        raise BusinessError('分析不存在', 404)
    rows = list(db.scalars(select(Observation.payload).where(Observation.id.in_(run.observation_ids))
                           .order_by(Observation.id)))
    if not rows:
        raise BusinessError('该分析没有可入账的观测记录', 409)
    try:
        rates = billing.parse_rates(run.parameters)
        _meters, _zones, view = billing.recompute(rows, rates, [])
    except BillingError as exc:
        raise BusinessError(str(exc)) from exc
    bill = Bill(title=request.title.strip() or f'热水账单 · 分析 #{run.id}', run_id=run.id, owner=actor,
                rates=rates, original_rows=rows,
                original_view=billing.snapshot_view(view), latest_view=billing.snapshot_view(view))
    db.add(bill)
    db.flush()
    db.add(AuditEvent(actor=actor, action='bill.created', detail=f'账单 #{bill.id} 入账 {len(rows)} 条区间，原费率已冻结'))
    return bill


def add_late_reading(db, bill_id, request, actor):
    bill = _bill_or_404(db, bill_id, lock=True)
    point = billing.point_from(request.reading_date, request.reading_minute)
    liters = billing.reading_to_liters(request.reading_m3)
    duplicate = db.scalar(select(LateReading).where(
        LateReading.bill_id == bill.id, LateReading.meter_code == request.meter_code, LateReading.point == point))
    if duplicate is not None:
        raise BusinessError('该表计在此时刻已补录过表底，不能重复登记', 409)
    readings = list(db.scalars(select(LateReading).where(LateReading.bill_id == bill.id).order_by(LateReading.id)))
    candidate = LatePoint(request.meter_code.strip(), point, liters,
                          f'bill-{bill.id}:{request.meter_code.strip()}:{point}', request.note or '')
    # Validate against the interval table that already includes earlier late bottoms.
    try:
        meters, zones = billing.build_intervals(bill.original_rows)
        for earlier in _late_points(readings):
            meters = billing.apply_late_reading(meters, zones, earlier)
        billing.apply_late_reading(meters, zones, candidate)
    except BillingError as exc:
        raise BusinessError(str(exc)) from exc
    item = LateReading(bill_id=bill.id, meter_code=request.meter_code.strip(), pool_zone=zones[request.meter_code.strip()],
                       reading_date=request.reading_date, reading_minute=request.reading_minute, point=point,
                       reading_liters=liters, source_key=candidate.source_key, note=request.note or '')
    db.add(item)
    db.flush()
    readings.append(item)
    _refresh_pending(db, bill, readings)
    db.add(AuditEvent(actor=actor, action='late_reading.created',
                      detail=f'账单 #{bill.id} 补录 {item.meter_code} {item.reading_date} {item.reading_minute:04d} 表底'))
    return item


def submit_adjustment(db, bill_id, actor):
    bill = _bill_or_404(db, bill_id, lock=True)
    pending = db.scalar(select(Adjustment).where(
        Adjustment.bill_id == bill.id, Adjustment.status == AdjustmentStatus.PENDING).with_for_update())
    if pending is None:
        raise BusinessError('当前没有待结补差，补录晚到表底后才能提交结算员', 409)
    pending_readings = list(db.scalars(select(LateReading).where(LateReading.adjustment_id == pending.id)
                                       .order_by(LateReading.id)))
    pending_ids = {item.id for item in pending_readings}
    all_readings = list(db.scalars(select(LateReading).where(LateReading.bill_id == bill.id)
                                   .order_by(LateReading.id)))
    before_view = _recompute(bill, [item for item in all_readings if item.id not in pending_ids])
    frozen_view = _recompute(bill, all_readings)
    # Freeze the delta now; later arrivals create a new pending row and cannot change this amount.
    pending.delta = billing.build_delta(before_view, frozen_view,
                                        [_reading_view(item) for item in pending_readings])
    pending.amount_cents = pending.delta['total_cost_delta_cents']
    pending.status = AdjustmentStatus.SUBMITTED
    pending.submitted_by = actor
    pending.submitted_at = utc_now()
    bill.latest_view = billing.snapshot_view(frozen_view)
    db.add(AuditEvent(actor=actor, action='adjustment.submitted',
                      detail=f'账单 #{bill.id} 补差 #{pending.seq} 冻结 {pending.amount_cents / 100:.2f} 元，已交结算员'))
    return pending


def settle_adjustment(db, adjustment_id, request, actor):
    adjustment = db.get(Adjustment, adjustment_id, with_for_update=True)
    if adjustment is None:
        raise BusinessError('补差记录不存在', 404)
    bill = _bill_or_404(db, adjustment.bill_id, lock=True)
    previous = db.scalar(select(SettlementAttempt).where(
        SettlementAttempt.idempotency_key == request.idempotency_key).with_for_update())
    if previous is not None:
        return adjustment, previous, False
    if adjustment.status == AdjustmentStatus.SETTLED:
        raise BusinessError('这份补差已结清，不能重复入账', 409)
    if adjustment.status != AdjustmentStatus.SUBMITTED:
        raise BusinessError('补差提交给结算员后才能结清', 409)
    if request.force_failure:
        attempt = SettlementAttempt(adjustment_id=adjustment.id, idempotency_key=request.idempotency_key,
                                    outcome=SettlementOutcome.FAILED, detail='模拟结账失败，等待重试', actor=actor)
        db.add(attempt)
        db.flush()
        return adjustment, attempt, True
    adjustment.status = AdjustmentStatus.SETTLED
    adjustment.settled_by = actor
    adjustment.settled_at = utc_now()
    attempt = SettlementAttempt(adjustment_id=adjustment.id, idempotency_key=request.idempotency_key,
                                outcome=SettlementOutcome.SUCCESS,
                                detail=f'入账 {adjustment.amount_cents / 100:.2f} 元，可追溯 {len(adjustment.delta["late_reading_ids"])} 个表底',
                                actor=actor)
    db.add(attempt)
    db.add(AuditEvent(actor=actor, action='adjustment.settled',
                      detail=f'账单 #{bill.id} 补差 #{adjustment.seq} 入账 {adjustment.amount_cents / 100:.2f} 元'))
    db.flush()
    return adjustment, attempt, True


def _adjustment_view(db, item):
    readings = list(db.scalars(select(LateReading).where(LateReading.adjustment_id == item.id)
                               .order_by(LateReading.id)))
    return {
        'id': item.id, 'seq': item.seq, 'status': item.status,
        'amount_yuan': item.amount_cents / 100, 'delta': item.delta,
        'submitted_by': item.submitted_by, 'submitted_at': item.submitted_at,
        'settled_by': item.settled_by, 'settled_at': item.settled_at,
        'late_readings': [_reading_view(row) for row in readings],
    }


def bill_detail(db, bill_id):
    bill = _bill_or_404(db, bill_id)
    readings = list(db.scalars(select(LateReading).where(LateReading.bill_id == bill.id).order_by(LateReading.id)))
    adjustments = list(db.scalars(select(Adjustment).where(Adjustment.bill_id == bill.id)
                                  .order_by(Adjustment.seq)))
    attempts = list(db.scalars(select(SettlementAttempt).join(
        Adjustment, Adjustment.id == SettlementAttempt.adjustment_id).where(
        Adjustment.bill_id == bill.id).order_by(SettlementAttempt.id)))
    settled_cents = sum(item.amount_cents for item in adjustments if item.status == AdjustmentStatus.SETTLED)
    pending_cents = sum(item.amount_cents for item in adjustments if item.status == AdjustmentStatus.PENDING)
    submitted_cents = sum(item.amount_cents for item in adjustments if item.status == AdjustmentStatus.SUBMITTED)
    # Snapshot totals are stored to the cent, so the check stays exact without float round-trips.
    original_cents = round(bill.original_view['total_cost_yuan'] * 100)
    latest_cents = round(bill.latest_view['total_cost_yuan'] * 100)
    return {
        'id': bill.id, 'title': bill.title, 'run_id': bill.run_id, 'owner': bill.owner,
        'created_at': bill.created_at,
        'original': bill.original_view,
        'latest': bill.latest_view,
        'original_total_yuan': bill.original_view['total_cost_yuan'],
        'latest_total_yuan': bill.latest_view['total_cost_yuan'],
        'settled_adjustment_yuan': settled_cents / 100,
        'submitted_adjustment_yuan': submitted_cents / 100,
        'pending_adjustment_yuan': pending_cents / 100,
        'balance_due_yuan': (submitted_cents + pending_cents) / 100,
        'reconciled': original_cents + settled_cents + submitted_cents + pending_cents == latest_cents,
        'late_readings': [_reading_view(item) for item in readings],
        'adjustments': [_adjustment_view(db, item) for item in adjustments],
        'settlement_attempts': [
            {'id': row.id, 'adjustment_id': row.adjustment_id, 'idempotency_key': row.idempotency_key,
             'outcome': row.outcome, 'detail': row.detail, 'actor': row.actor, 'at': row.created_at}
            for row in attempts],
    }


def list_bills(db):
    items = list(db.scalars(select(Bill).order_by(Bill.id.desc()).limit(100)))
    result = []
    for bill in items:
        adjustments = list(db.scalars(select(Adjustment).where(Adjustment.bill_id == bill.id)))
        pending = sum(item.amount_cents for item in adjustments if item.status == AdjustmentStatus.PENDING)
        submitted = sum(item.amount_cents for item in adjustments if item.status == AdjustmentStatus.SUBMITTED)
        settled = sum(item.amount_cents for item in adjustments if item.status == AdjustmentStatus.SETTLED)
        result.append({
            'id': bill.id, 'title': bill.title, 'run_id': bill.run_id,
            'original_total_yuan': bill.original_view['total_cost_yuan'],
            'latest_total_yuan': bill.latest_view['total_cost_yuan'],
            'pending_adjustment_yuan': pending / 100,
            'submitted_adjustment_yuan': submitted / 100,
            'settled_adjustment_yuan': settled / 100,
        })
    return result
