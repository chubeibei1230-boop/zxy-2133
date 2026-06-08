from flask import Blueprint, request, jsonify
from models import (db, ServicePointDeactivation, DeactivationAffectedShift,
                    DutyAssignment, ServicePoint, User, ScheduleNotification,
                    ConflictLog)
from auth import login_required, role_required
from conflict import (detect_all_conflicts, log_conflicts, check_cross_point_conflict,
                      check_over_limit_conflict, intervals_overlap, time_to_minutes)
from datetime import datetime

deact_bp = Blueprint('deactivations', __name__, url_prefix='/api/deactivations')


@deact_bp.route('', methods=['POST'])
@role_required('admin')
def create():
    data = request.get_json()
    if not data or not data.get('service_point_id') or not data.get('start_date') \
            or not data.get('end_date') or not data.get('reason'):
        return jsonify({'error': '服务点ID、开始日期、结束日期和停用原因为必填项'}), 400

    sp = ServicePoint.query.get(data['service_point_id'])
    if not sp:
        return jsonify({'error': '服务点不存在'}), 404

    if data['start_date'] > data['end_date']:
        return jsonify({'error': '开始日期不能晚于结束日期'}), 400

    overlapping = ServicePointDeactivation.query.filter(
        ServicePointDeactivation.service_point_id == data['service_point_id'],
        ServicePointDeactivation.status == 'active',
        ServicePointDeactivation.start_date <= data['end_date'],
        ServicePointDeactivation.end_date >= data['start_date']
    ).first()
    if overlapping:
        return jsonify({'error': '该服务点在指定时间范围内已有活跃的停用记录'}), 409

    deactivation = ServicePointDeactivation(
        service_point_id=data['service_point_id'],
        start_date=data['start_date'],
        end_date=data['end_date'],
        reason=data['reason'],
        status='active'
    )
    db.session.add(deactivation)
    db.session.flush()

    affected_assignments = DutyAssignment.query.filter(
        DutyAssignment.service_point_id == data['service_point_id'],
        DutyAssignment.date >= data['start_date'],
        DutyAssignment.date <= data['end_date'],
        DutyAssignment.status != 'cancelled'
    ).order_by(DutyAssignment.date, DutyAssignment.start_time).all()

    affected_list = []
    for a in affected_assignments:
        existing = DeactivationAffectedShift.query.filter(
            DeactivationAffectedShift.duty_assignment_id == a.id,
            DeactivationAffectedShift.handle_status != 'cancelled'
        ).join(ServicePointDeactivation).filter(
            ServicePointDeactivation.status == 'active'
        ).first()
        if existing:
            continue

        das = DeactivationAffectedShift(
            deactivation_id=deactivation.id,
            duty_assignment_id=a.id,
            handle_status='pending'
        )
        db.session.add(das)
        affected_list.append({
            'duty_assignment_id': a.id,
            'user_id': a.user_id,
            'user_name': a.user.name if a.user else None,
            'date': a.date,
            'start_time': a.start_time,
            'end_time': a.end_time,
            'is_cross_day': a.is_cross_day,
            'status': a.status
        })

    db.session.commit()

    return jsonify({
        'message': '服务点停用登记成功',
        'id': deactivation.id,
        'affected_count': len(affected_list),
        'affected_shifts': affected_list
    }), 201


@deact_bp.route('', methods=['GET'])
@role_required('admin')
def list_all():
    query = ServicePointDeactivation.query

    service_point_id = request.args.get('service_point_id')
    if service_point_id:
        query = query.filter(ServicePointDeactivation.service_point_id == service_point_id)

    status = request.args.get('status')
    if status:
        query = query.filter(ServicePointDeactivation.status == status)

    start_date = request.args.get('start_date')
    if start_date:
        query = query.filter(ServicePointDeactivation.start_date >= start_date)

    end_date = request.args.get('end_date')
    if end_date:
        query = query.filter(ServicePointDeactivation.end_date <= end_date)

    deactivations = query.order_by(ServicePointDeactivation.created_at.desc()).all()
    return jsonify([_serialize_deactivation(d) for d in deactivations]), 200


@deact_bp.route('/<int:d_id>', methods=['GET'])
@role_required('admin')
def get_one(d_id):
    deactivation = ServicePointDeactivation.query.get(d_id)
    if not deactivation:
        return jsonify({'error': '停用记录不存在'}), 404

    result = _serialize_deactivation(deactivation)
    affected = deactivation.affected_shifts.all()
    result['affected_shifts'] = [_serialize_affected_shift(das) for das in affected]
    return jsonify(result), 200


@deact_bp.route('/<int:d_id>/cancel', methods=['POST'])
@role_required('admin')
def cancel_deactivation(d_id):
    deactivation = ServicePointDeactivation.query.get(d_id)
    if not deactivation:
        return jsonify({'error': '停用记录不存在'}), 404

    if deactivation.status != 'active':
        return jsonify({'error': '只能取消活跃状态的停用记录'}), 400

    deactivation.status = 'cancelled'
    deactivation.cancelled_at = datetime.utcnow()

    pending_shifts = deactivation.affected_shifts.filter(
        DeactivationAffectedShift.handle_status == 'pending'
    ).all()
    for das in pending_shifts:
        das.handle_status = 'cancelled'
        das.handle_type = 'deactivation_cancelled'
        das.handle_remark = '停用记录已取消，受影响班次自动关闭'
        das.handled_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        'message': '停用记录已取消',
        'auto_closed_pending': len(pending_shifts)
    }), 200


@deact_bp.route('/<int:d_id>/affected-shifts', methods=['GET'])
@role_required('admin')
def list_affected_shifts(d_id):
    deactivation = ServicePointDeactivation.query.get(d_id)
    if not deactivation:
        return jsonify({'error': '停用记录不存在'}), 404

    query = deactivation.affected_shifts

    handle_status = request.args.get('handle_status')
    if handle_status:
        query = query.filter(DeactivationAffectedShift.handle_status == handle_status)

    shifts = query.order_by(DeactivationAffectedShift.created_at).all()

    summary = {
        'total': deactivation.affected_shifts.count(),
        'pending': deactivation.affected_shifts.filter_by(handle_status='pending').count(),
        'reassigned_sp': deactivation.affected_shifts.filter_by(handle_status='reassigned_sp').count(),
        'reassigned_user': deactivation.affected_shifts.filter_by(handle_status='reassigned_user').count(),
        'cancelled': deactivation.affected_shifts.filter_by(handle_status='cancelled').count()
    }

    return jsonify({
        'deactivation_id': d_id,
        'summary': summary,
        'shifts': [_serialize_affected_shift(das) for das in shifts]
    }), 200


@deact_bp.route('/affected-shifts/<int:shift_id>/handle', methods=['POST'])
@role_required('admin')
def handle_affected_shift(shift_id):
    das = DeactivationAffectedShift.query.get(shift_id)
    if not das:
        return jsonify({'error': '受影响班次记录不存在'}), 404

    if das.handle_status != 'pending':
        return jsonify({'error': f'该班次已处理，当前状态: {das.handle_status}'}), 400

    deactivation = das.deactivation
    if deactivation.status != 'active':
        return jsonify({'error': '关联的停用记录已取消，无需处理'}), 400

    data = request.get_json()
    if not data or not data.get('handle_type'):
        return jsonify({'error': '处理方式为必填项'}), 400

    handle_type = data['handle_type']
    if handle_type not in ('reassign_sp', 'reassign_user', 'cancel'):
        return jsonify({'error': '无效处理方式，可选: reassign_sp, reassign_user, cancel'}), 400

    assignment = das.duty_assignment
    if not assignment or assignment.status == 'cancelled':
        das.handle_status = 'cancelled'
        das.handle_type = 'auto_cancelled'
        das.handle_remark = '原排班已取消，自动关闭'
        das.handled_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'message': '原排班已取消，受影响班次已自动关闭'}), 200

    if handle_type == 'reassign_sp':
        return _handle_reassign_sp(das, assignment, data)
    elif handle_type == 'reassign_user':
        return _handle_reassign_user(das, assignment, data)
    else:
        return _handle_cancel(das, assignment, data)


def _handle_reassign_sp(das, assignment, data):
    new_sp_id = data.get('new_service_point_id')
    if not new_sp_id:
        return jsonify({'error': '改派到其他服务点需提供新服务点ID'}), 400

    new_sp = ServicePoint.query.get(new_sp_id)
    if not new_sp:
        return jsonify({'error': '目标服务点不存在'}), 404

    active_deact = ServicePointDeactivation.query.filter(
        ServicePointDeactivation.service_point_id == new_sp_id,
        ServicePointDeactivation.status == 'active',
        ServicePointDeactivation.start_date <= assignment.date,
        ServicePointDeactivation.end_date >= assignment.date
    ).first()
    if active_deact:
        return jsonify({'error': f'目标服务点在{assignment.date}也处于停用状态，无法改派'}), 409

    conflicts = detect_all_conflicts(
        user_id=assignment.user_id,
        service_point_id=new_sp_id,
        date=assignment.date,
        start_time=assignment.start_time,
        end_time=assignment.end_time,
        is_cross_day=assignment.is_cross_day,
        exclude_id=assignment.id
    )

    force = data.get('force', False)
    if conflicts and not force:
        return jsonify({
            'error': '改派到目标服务点存在冲突',
            'conflicts': conflicts,
            'hint': '如需强制改派，请设置force=true'
        }), 409

    old_sp_id = assignment.service_point_id
    assignment.service_point_id = new_sp_id

    das.handle_status = 'reassigned_sp'
    das.handle_type = 'reassign_sp'
    das.new_service_point_id = new_sp_id
    das.handle_remark = data.get('remark', f'由服务点ID={old_sp_id}改派至服务点ID={new_sp_id}')
    das.handled_at = datetime.utcnow()

    if conflicts:
        log_conflicts(conflicts, assignment.user_id, new_sp_id, assignment.date, assignment.id)

    _notify_assignment_change(assignment, 'service_point_deactivation_reassign_sp')

    db.session.commit()

    result = {
        'message': '已改派到其他服务点',
        'assignment_id': assignment.id,
        'new_service_point_id': new_sp_id,
        'new_service_point_name': new_sp.name
    }
    if conflicts:
        result['warnings'] = [c.get('description', '') for c in conflicts]
    return jsonify(result), 200


def _handle_reassign_user(das, assignment, data):
    new_user_id = data.get('new_user_id')
    if not new_user_id:
        return jsonify({'error': '改派到其他人员需提供新人员ID'}), 400

    new_user = User.query.get(new_user_id)
    if not new_user:
        return jsonify({'error': '目标人员不存在'}), 404

    conflicts = detect_all_conflicts(
        user_id=new_user_id,
        service_point_id=assignment.service_point_id,
        date=assignment.date,
        start_time=assignment.start_time,
        end_time=assignment.end_time,
        is_cross_day=assignment.is_cross_day,
        exclude_id=assignment.id
    )

    force = data.get('force', False)
    if conflicts and not force:
        return jsonify({
            'error': '改派给目标人员存在冲突',
            'conflicts': conflicts,
            'hint': '如需强制改派，请设置force=true'
        }), 409

    old_user_id = assignment.user_id
    assignment.user_id = new_user_id

    das.handle_status = 'reassigned_user'
    das.handle_type = 'reassign_user'
    das.new_user_id = new_user_id
    das.handle_remark = data.get('remark', f'由人员ID={old_user_id}改派至人员ID={new_user_id}')
    das.handled_at = datetime.utcnow()

    if conflicts:
        log_conflicts(conflicts, new_user_id, assignment.service_point_id, assignment.date, assignment.id)

    old_notifications = ScheduleNotification.query.filter(
        ScheduleNotification.duty_assignment_id == assignment.id,
        ScheduleNotification.user_id == old_user_id,
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm', 'confirmed'])
    ).all()
    for n in old_notifications:
        n.status = 'expired'
        n.expired_at = datetime.utcnow()

    _notify_assignment_change(assignment, 'service_point_deactivation_reassign_user')

    db.session.commit()

    result = {
        'message': '已改派到其他人员',
        'assignment_id': assignment.id,
        'new_user_id': new_user_id,
        'new_user_name': new_user.name
    }
    if conflicts:
        result['warnings'] = [c.get('description', '') for c in conflicts]
    return jsonify(result), 200


def _handle_cancel(das, assignment, data):
    assignment.status = 'cancelled'

    pending_notifications = ScheduleNotification.query.filter(
        ScheduleNotification.duty_assignment_id == assignment.id,
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
    ).all()
    for n in pending_notifications:
        n.status = 'expired'
        n.expired_at = datetime.utcnow()

    das.handle_status = 'cancelled'
    das.handle_type = 'cancel'
    das.handle_remark = data.get('remark', '因服务点停用，班次直接取消')
    das.handled_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        'message': '班次已取消',
        'assignment_id': assignment.id
    }), 200


@deact_bp.route('/daily-overview', methods=['GET'])
@role_required('admin')
def daily_overview():
    date = request.args.get('date')
    if not date:
        return jsonify({'error': '日期为必填项'}), 400

    deactivations = ServicePointDeactivation.query.filter(
        ServicePointDeactivation.status == 'active',
        ServicePointDeactivation.start_date <= date,
        ServicePointDeactivation.end_date >= date
    ).all()

    overview = []
    total_affected = 0
    total_unhandled = 0

    for d in deactivations:
        affected = d.affected_shifts.all()
        pending_count = sum(1 for s in affected if s.handle_status == 'pending')
        handled_count = sum(1 for s in affected if s.handle_status != 'pending')
        total_affected += len(affected)
        total_unhandled += pending_count

        overview.append({
            'deactivation_id': d.id,
            'service_point_id': d.service_point_id,
            'service_point_name': d.service_point.name if d.service_point else None,
            'deactivation_start_date': d.start_date,
            'deactivation_end_date': d.end_date,
            'reason': d.reason,
            'total_affected': len(affected),
            'pending_count': pending_count,
            'handled_count': handled_count,
            'pending_shifts': [_serialize_affected_shift(s) for s in affected if s.handle_status == 'pending']
        })

    return jsonify({
        'date': date,
        'deactivated_service_point_count': len(deactivations),
        'total_affected_shifts': total_affected,
        'total_unhandled_shifts': total_unhandled,
        'deactivations': overview
    }), 200


@deact_bp.route('/check', methods=['GET'])
@login_required
def check_deactivation():
    service_point_id = request.args.get('service_point_id')
    date = request.args.get('date')
    if not service_point_id or not date:
        return jsonify({'error': 'service_point_id和date为必填项'}), 400

    active = ServicePointDeactivation.query.filter(
        ServicePointDeactivation.service_point_id == service_point_id,
        ServicePointDeactivation.status == 'active',
        ServicePointDeactivation.start_date <= date,
        ServicePointDeactivation.end_date >= date
    ).first()

    if active:
        return jsonify({
            'is_deactivated': True,
            'deactivation': {
                'id': active.id,
                'service_point_id': active.service_point_id,
                'service_point_name': active.service_point.name if active.service_point else None,
                'start_date': active.start_date,
                'end_date': active.end_date,
                'reason': active.reason
            }
        }), 200

    return jsonify({'is_deactivated': False}), 200


def _notify_assignment_change(assignment, notify_type_str):
    notify_type = 'assignment'
    valid_types = ScheduleNotification.valid_notify_types()
    if notify_type_str in valid_types:
        notify_type = notify_type_str

    existing = ScheduleNotification.query.filter(
        ScheduleNotification.duty_assignment_id == assignment.id,
        ScheduleNotification.user_id == assignment.user_id,
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
    ).first()

    if existing:
        existing.notify_type = notify_type
        existing.status = 'pending_confirm'
        existing.notified_at = datetime.utcnow()
    else:
        notification = ScheduleNotification(
            duty_assignment_id=assignment.id,
            user_id=assignment.user_id,
            notify_type=notify_type,
            status='unnotified'
        )
        db.session.add(notification)


def _serialize_deactivation(d):
    total = d.affected_shifts.count()
    pending = d.affected_shifts.filter_by(handle_status='pending').count()
    handled = total - pending
    return {
        'id': d.id,
        'service_point_id': d.service_point_id,
        'service_point_name': d.service_point.name if d.service_point else None,
        'start_date': d.start_date,
        'end_date': d.end_date,
        'reason': d.reason,
        'status': d.status,
        'total_affected': total,
        'pending_count': pending,
        'handled_count': handled,
        'created_at': d.created_at.isoformat() if d.created_at else None,
        'cancelled_at': d.cancelled_at.isoformat() if d.cancelled_at else None
    }


def _serialize_affected_shift(das):
    a = das.duty_assignment
    result = {
        'id': das.id,
        'deactivation_id': das.deactivation_id,
        'duty_assignment_id': das.duty_assignment_id,
        'handle_status': das.handle_status,
        'handle_type': das.handle_type,
        'new_service_point_id': das.new_service_point_id,
        'new_service_point_name': das.new_service_point.name if das.new_service_point else None,
        'new_user_id': das.new_user_id,
        'new_user_name': das.new_user.name if das.new_user else None,
        'handle_remark': das.handle_remark,
        'handled_at': das.handled_at.isoformat() if das.handled_at else None,
        'assignment': {
            'id': a.id,
            'user_id': a.user_id,
            'user_name': a.user.name if a and a.user else None,
            'service_point_id': a.service_point_id,
            'service_point_name': a.service_point.name if a and a.service_point else None,
            'date': a.date,
            'start_time': a.start_time,
            'end_time': a.end_time,
            'is_cross_day': a.is_cross_day,
            'status': a.status
        } if a else None
    }
    return result
