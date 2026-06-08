from flask import Blueprint, request, jsonify
from models import db, ScheduleNotification, DutyAssignment, Substitution, User
from auth import login_required, role_required
from datetime import datetime, timedelta

sn_bp = Blueprint('schedule_notifications', __name__, url_prefix='/api/schedule-notifications')


@sn_bp.route('', methods=['POST'])
@role_required('admin')
def send_notification():
    data = request.get_json()
    if not data or not data.get('duty_assignment_id') or not data.get('user_id'):
        return jsonify({'error': '排班ID和用户ID为必填项'}), 400

    assignment = DutyAssignment.query.get(data['duty_assignment_id'])
    if not assignment:
        return jsonify({'error': '排班记录不存在'}), 404

    if assignment.status == 'cancelled':
        return jsonify({'error': '已取消的排班不能发送通知'}), 400

    target_user = User.query.get(data['user_id'])
    if not target_user:
        return jsonify({'error': '目标用户不存在'}), 404

    existing = ScheduleNotification.query.filter(
        ScheduleNotification.duty_assignment_id == data['duty_assignment_id'],
        ScheduleNotification.user_id == data['user_id'],
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
    ).first()
    if existing:
        if existing.status == 'unnotified':
            existing.status = 'pending_confirm'
            existing.notified_at = datetime.utcnow()
            notify_type = data.get('notify_type', 'assignment')
            if notify_type in ScheduleNotification.valid_notify_types():
                existing.notify_type = notify_type
            substitution_id = data.get('substitution_id')
            if substitution_id:
                existing.substitution_id = substitution_id
            db.session.commit()
            return jsonify({
                'message': '排班通知已发送',
                'id': existing.id,
                'status': existing.status
            }), 200
        return jsonify({'error': '该用户已有进行中的排班通知'}), 409

    notify_type = data.get('notify_type', 'assignment')
    if notify_type not in ScheduleNotification.valid_notify_types():
        return jsonify({'error': f'无效通知类型，可选: {",".join(ScheduleNotification.valid_notify_types())}'}), 400

    substitution_id = data.get('substitution_id')
    if notify_type == 'substitution' and not substitution_id:
        return jsonify({'error': '替班类型通知需提供替班记录ID'}), 400

    if substitution_id:
        sub = Substitution.query.get(substitution_id)
        if not sub:
            return jsonify({'error': '替班记录不存在'}), 404

    notification = ScheduleNotification(
        duty_assignment_id=data['duty_assignment_id'],
        substitution_id=substitution_id,
        user_id=data['user_id'],
        notify_type=notify_type,
        status='pending_confirm',
        notified_at=datetime.utcnow()
    )
    db.session.add(notification)
    db.session.commit()

    return jsonify({
        'message': '排班通知已发送',
        'id': notification.id,
        'status': notification.status
    }), 201


@sn_bp.route('/batch', methods=['POST'])
@role_required('admin')
def send_batch_notifications():
    data = request.get_json()
    if not data or not data.get('items'):
        return jsonify({'error': '通知列表为必填项'}), 400

    results = []
    for item in data['items']:
        assignment_id = item.get('duty_assignment_id')
        user_id = item.get('user_id')
        if not assignment_id or not user_id:
            results.append({'error': '排班ID和用户ID为必填项', 'item': item})
            continue

        assignment = DutyAssignment.query.get(assignment_id)
        if not assignment or assignment.status == 'cancelled':
            results.append({'error': '排班记录不存在或已取消', 'duty_assignment_id': assignment_id})
            continue

        existing = ScheduleNotification.query.filter(
            ScheduleNotification.duty_assignment_id == assignment_id,
            ScheduleNotification.user_id == user_id,
            ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
        ).first()
        if existing:
            results.append({'error': '已有进行中通知', 'duty_assignment_id': assignment_id, 'user_id': user_id})
            continue

        notify_type = item.get('notify_type', 'assignment')
        substitution_id = item.get('substitution_id')

        notification = ScheduleNotification(
            duty_assignment_id=assignment_id,
            substitution_id=substitution_id,
            user_id=user_id,
            notify_type=notify_type,
            status='pending_confirm',
            notified_at=datetime.utcnow()
        )
        db.session.add(notification)
        results.append({'duty_assignment_id': assignment_id, 'user_id': user_id, 'status': 'pending_confirm'})

    db.session.commit()
    return jsonify({'message': f'批量通知处理完成', 'results': results}), 200


@sn_bp.route('/my-pending', methods=['GET'])
@login_required
def my_pending():
    user = request.current_user
    _expire_overdue_notifications()

    notifications = ScheduleNotification.query.filter(
        ScheduleNotification.user_id == user.id,
        ScheduleNotification.status == 'pending_confirm'
    ).order_by(ScheduleNotification.notified_at.desc()).all()

    return jsonify([_serialize(n) for n in notifications]), 200


@sn_bp.route('/my-all', methods=['GET'])
@login_required
def my_all():
    user = request.current_user
    _expire_overdue_notifications()

    query = ScheduleNotification.query.filter(ScheduleNotification.user_id == user.id)

    status = request.args.get('status')
    if status:
        query = query.filter(ScheduleNotification.status == status)

    notifications = query.order_by(ScheduleNotification.created_at.desc()).all()
    return jsonify([_serialize(n) for n in notifications]), 200


@sn_bp.route('/<int:n_id>/confirm', methods=['POST'])
@login_required
def confirm(n_id):
    user = request.current_user
    notification = ScheduleNotification.query.get(n_id)
    if not notification:
        return jsonify({'error': '通知不存在'}), 404

    if notification.user_id != user.id:
        return jsonify({'error': '只能确认自己的通知'}), 403

    if notification.status != 'pending_confirm':
        return jsonify({'error': f'当前状态为{notification.status}，无法确认'}), 400

    assignment = notification.assignment
    if not assignment or assignment.status == 'cancelled':
        notification.status = 'expired'
        notification.expired_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'error': '对应排班已取消，通知已自动失效'}), 400

    _expire_if_past_check_in(notification)
    if notification.status == 'expired':
        db.session.commit()
        return jsonify({'error': '已过签到时间，通知已自动失效'}), 400

    notification.status = 'confirmed'
    notification.confirmed_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'message': '排班通知已确认', 'id': notification.id, 'status': notification.status}), 200


@sn_bp.route('/<int:n_id>/reject', methods=['POST'])
@login_required
def reject(n_id):
    user = request.current_user
    notification = ScheduleNotification.query.get(n_id)
    if not notification:
        return jsonify({'error': '通知不存在'}), 404

    if notification.user_id != user.id:
        return jsonify({'error': '只能拒绝自己的通知'}), 403

    if notification.status != 'pending_confirm':
        return jsonify({'error': f'当前状态为{notification.status}，无法拒绝'}), 400

    data = request.get_json()
    reject_reason = data.get('reject_reason') if data else None

    assignment = notification.assignment
    if not assignment or assignment.status == 'cancelled':
        notification.status = 'expired'
        notification.expired_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'error': '对应排班已取消，通知已自动失效'}), 400

    _expire_if_past_check_in(notification)
    if notification.status == 'expired':
        db.session.commit()
        return jsonify({'error': '已过签到时间，通知已自动失效'}), 400

    notification.status = 'rejected'
    notification.reject_reason = reject_reason
    notification.confirmed_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'message': '排班通知已拒绝',
        'id': notification.id,
        'status': notification.status
    }), 200


@sn_bp.route('', methods=['GET'])
@role_required('admin')
def list_all():
    _expire_overdue_notifications()

    query = ScheduleNotification.query

    user_id = request.args.get('user_id')
    if user_id:
        query = query.filter(ScheduleNotification.user_id == user_id)

    service_point_id = request.args.get('service_point_id')
    if service_point_id:
        query = query.join(DutyAssignment).filter(DutyAssignment.service_point_id == service_point_id)

    date = request.args.get('date')
    if date:
        query = query.join(DutyAssignment).filter(DutyAssignment.date == date)

    date_from = request.args.get('date_from')
    if date_from:
        query = query.join(DutyAssignment).filter(DutyAssignment.date >= date_from)

    date_to = request.args.get('date_to')
    if date_to:
        query = query.join(DutyAssignment).filter(DutyAssignment.date <= date_to)

    status = request.args.get('status')
    if status:
        query = query.filter(ScheduleNotification.status == status)

    notify_type = request.args.get('notify_type')
    if notify_type:
        query = query.filter(ScheduleNotification.notify_type == notify_type)

    notifications = query.order_by(ScheduleNotification.created_at.desc()).all()
    return jsonify([_serialize_admin(n) for n in notifications]), 200


@sn_bp.route('/expire-check', methods=['POST'])
@role_required('admin')
def trigger_expire_check():
    count = _expire_overdue_notifications()
    return jsonify({'message': f'已处理{count}条过期通知'}), 200


def _serialize(n):
    a = n.assignment
    return {
        'id': n.id,
        'duty_assignment_id': n.duty_assignment_id,
        'substitution_id': n.substitution_id,
        'notify_type': n.notify_type,
        'status': n.status,
        'reject_reason': n.reject_reason,
        'notified_at': n.notified_at.isoformat() if n.notified_at else None,
        'confirmed_at': n.confirmed_at.isoformat() if n.confirmed_at else None,
        'expired_at': n.expired_at.isoformat() if n.expired_at else None,
        'assignment': {
            'id': a.id,
            'service_point_id': a.service_point_id,
            'service_point_name': a.service_point.name if a and a.service_point else None,
            'date': a.date,
            'start_time': a.start_time,
            'end_time': a.end_time,
            'is_cross_day': a.is_cross_day,
            'status': a.status
        } if a else None,
        'substitution': {
            'id': n.substitution.id,
            'original_user_name': n.substitution.original_user.name if n.substitution and n.substitution.original_user else None,
            'substitute_user_name': n.substitution.substitute_user.name if n.substitution and n.substitution.substitute_user else None,
            'status': n.substitution.status
        } if n.substitution else None
    }


def _serialize_admin(n):
    result = _serialize(n)
    result['user_id'] = n.user_id
    result['user_name'] = n.user.name if n.user else None
    result['created_at'] = n.created_at.isoformat() if n.created_at else None
    return result


def _expire_overdue_notifications():
    now = datetime.utcnow()
    today = now.date()

    active_notifications = ScheduleNotification.query.filter(
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
    ).all()

    expired_count = 0
    for n in active_notifications:
        a = n.assignment
        if not a:
            n.status = 'expired'
            n.expired_at = now
            expired_count += 1
            continue

        if a.status == 'cancelled':
            n.status = 'expired'
            n.expired_at = now
            expired_count += 1
            continue

        if n.notify_type == 'substitution' and n.substitution_id:
            sub = Substitution.query.get(n.substitution_id)
            if sub and sub.status == 'rejected':
                n.status = 'expired'
                n.expired_at = now
                expired_count += 1
                continue

        if _is_past_check_in(a, now):
            n.status = 'expired'
            n.expired_at = now
            expired_count += 1
            continue

    if expired_count > 0:
        db.session.commit()

    return expired_count


def _expire_if_past_check_in(notification):
    a = notification.assignment
    now = datetime.utcnow()
    if a and _is_past_check_in(a, now):
        notification.status = 'expired'
        notification.expired_at = now


def _is_past_check_in(assignment, now):
    try:
        shift_date = datetime.strptime(assignment.date, '%Y-%m-%d').date()
    except ValueError:
        return False

    today = now.date()

    if assignment.is_cross_day:
        next_day = shift_date + timedelta(days=1)
        end_parts = assignment.end_time.split(':')
        end_hour, end_min = int(end_parts[0]), int(end_parts[1])
        shift_end = datetime(next_day.year, next_day.month, next_day.day, end_hour, end_min)
        if now > shift_end:
            return True
    else:
        start_parts = assignment.start_time.split(':')
        start_hour, start_min = int(start_parts[0]), int(start_parts[1])
        shift_start = datetime(shift_date.year, shift_date.month, shift_date.day, start_hour, start_min)
        if now > shift_start + timedelta(hours=1):
            return True

    return False
