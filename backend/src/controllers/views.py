from src.utils.formatting import timestamp


def review_view(item):
    return {'decision': item.decision, 'note': item.note, 'reviewer': item.reviewer,
            'at': timestamp(item.created_at)} if item else None


def run_view(item, review=None, *, include_result=False):
    view = {'id': item.id, 'title': item.title, 'owner': item.owner, 'status': item.status,
            'parameters': item.parameters, 'sample_size': item.sample_size,
            'snapshot_id': item.last_observation_id, 'created_at': timestamp(item.created_at),
            'finished_at': timestamp(item.finished_at), 'review': review_view(review), 'error': item.error}
    if include_result:
        view['result'] = item.result
    return view


def observation_view(item):
    return {'id': item.id, 'data': item.payload, 'created_at': timestamp(item.created_at)}


def schedule_view(item):
    return {'id': item.id, 'run_id': item.run_id, 'recipient': item.recipient,
            'interval_minutes': item.interval_minutes, 'next_at': timestamp(item.next_at), 'active': item.active}


def delivery_view(delivery, schedule):
    return {'id': delivery.id, 'schedule_id': schedule.id, 'recipient': schedule.recipient,
            'run_id': schedule.run_id, 'at': timestamp(delivery.delivered_at),
            'scheduled_at': timestamp(delivery.scheduled_at)}


def late_reading_view(item):
    return {'id': item.id, 'meter_code': item.meter_code, 'pool_zone': item.pool_zone,
            'reading_date': item.reading_date, 'reading_minute': item.reading_minute,
            'reading_m3': item.reading_liters / 1000, 'note': item.note,
            'adjustment_id': item.adjustment_id, 'created_at': timestamp(item.created_at)}


def bill_list_view(row):
    return row


def bill_detail_view(detail):
    data = dict(detail)
    data['created_at'] = timestamp(detail['created_at'])
    for adjustment in data['adjustments']:
        adjustment['submitted_at'] = timestamp(adjustment['submitted_at'])
        adjustment['settled_at'] = timestamp(adjustment['settled_at'])
    for attempt in data['settlement_attempts']:
        attempt['at'] = timestamp(attempt['at'])
    return data
