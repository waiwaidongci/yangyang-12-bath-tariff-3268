from fastapi import APIRouter, Depends

from src.constants.enums import Role
from src.controllers.views import bill_detail_view, late_reading_view
from src.models.base import Session
from src.schemas.requests import BillInput, LateReadingInput, SettleInput
from src.services import billing as billing_service
from src.services.auth import current_user, roles

router = APIRouter(prefix='/api/bills')
analyst = roles(Role.ANALYST)
supervisor = roles(Role.SUPERVISOR)
editor = roles(Role.ANALYST, Role.SUPERVISOR)


@router.get('')
def index(user=Depends(current_user)):
    with Session() as db:
        return billing_service.list_bills(db)


@router.post('', status_code=201)
def create(request: BillInput, user=Depends(analyst)):
    with Session.begin() as db:
        bill = billing_service.create_bill(db, request, user['sub'])
        return {'id': bill.id, 'title': bill.title, 'run_id': bill.run_id}


@router.get('/{bill_id}')
def detail(bill_id: int, user=Depends(current_user)):
    with Session() as db:
        return bill_detail_view(billing_service.bill_detail(db, bill_id))


@router.post('/{bill_id}/late-readings', status_code=201)
def add_reading(bill_id: int, request: LateReadingInput, user=Depends(analyst)):
    with Session.begin() as db:
        item = billing_service.add_late_reading(db, bill_id, request, user['sub'])
        return late_reading_view(item)


@router.post('/{bill_id}/submit', status_code=201)
def submit(bill_id: int, user=Depends(analyst)):
    with Session.begin() as db:
        adjustment = billing_service.submit_adjustment(db, bill_id, user['sub'])
        return {'id': adjustment.id, 'seq': adjustment.seq, 'status': adjustment.status,
                'amount_yuan': adjustment.amount_cents / 100}


@router.post('/adjustments/{adjustment_id}/settle')
def settle(adjustment_id: int, request: SettleInput, user=Depends(supervisor)):
    with Session.begin() as db:
        adjustment, attempt, handled = billing_service.settle_adjustment(
            db, adjustment_id, request, user['sub'])
        return {
            'adjustment_id': adjustment.id, 'status': adjustment.status,
            'amount_yuan': adjustment.amount_cents / 100,
            'attempt_id': attempt.id, 'outcome': attempt.outcome,
            'idempotent_replay': not handled, 'detail': attempt.detail,
        }
