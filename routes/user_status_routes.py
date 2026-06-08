from flask import Blueprint, request, jsonify
from models import (db, UserStatus, StatusAffectedAssignment, DutyAssignment,
                    User, ScheduleNotification, ConflictLog)
from auth import login_required, role_required
from conflict import detect_all_conflicts, log_conflicts
from datetime import datetime

us_bp = Blueprint('user_status', __name__, url_prefix='/api/user-status')


@us_bp.route('', methods=['POST'])
@role_required('admin')
def create():
    admin = request.current_user
    data = request.get_json()
    if not data or not data.get('user_id') or not data.get('status') or not data.get('reason'):
        return jsonify({'error': '用户ID、状态和原因为必填项'}), 400

    if data['status'] not in UserStatus.valid_statuses():
        return jsonify({'error': f'无效状态，可选: {",".join(UserStatus.valid_statuses())}'}), 400

    target_user = User.query.get(data['user_id'])
    if not target_user:
        return jsonify({'error': '用户不存在'}), 404

    if target_user.role == 'admin':
        return jsonify({'error': '不能修改管理员的状态'}), 400

    effective_time = data.get('effective_time')
    if effective_time:
        try:
            effective_time = datetime.fromisoformat(effective_time)
        except (ValueError, TypeError):
            return jsonify({'error': '生效时间格式错误，应为ISO格式'}), 400
    else:
        effective_time = datetime.utcnow()

    end_time = data.get('end_time')
    if end_time:
        try:
            end_time = datetime.fromisoformat(end_time)
        except (ValueError, TypeError):
            return jsonify({'error': '结束时间格式错误，应为ISO格式'}), 400

    if end_time and end_time <= effective_time:
        return jsonify({'error': '结束时间必须晚于生效时间'}), 400

    if data['status'] == 'active' and not end_time:
        pass

    user_status = UserStatus(
        user_id=data['user_id'],
        status=data['status'],
        effective_time=effective_time,
        end_time=end_time,
        reason=data['reason'],
        remark=data.get('remark'),
        created_by=admin.id
    )
    db.session.add(user_status)
    db.session.flush()

    affected_list = []
    if data['status'] in ('disabled', 'frozen'):
        today_str = datetime.utcnow().strftime('%Y-%m-%d')
        future_assignments = DutyAssignment.query.filter(
            DutyAssignment.user_id == data['user_id'],
            DutyAssignment.date >= today_str,
            DutyAssignment.status != 'cancelled'
        ).order_by(DutyAssignment.date, DutyAssignment.start_time).all()

        for a in future_assignments:
            existing = StatusAffectedAssignment.query.filter(
                StatusAffectedAssignment.duty_assignment_id == a.id,
                StatusAffectedAssignment.handle_status == 'pending'
            ).join(UserStatus).filter(
                UserStatus.status.in_(['disabled', 'frozen'])
            ).first()
            if existing:
                continue

            saa = StatusAffectedAssignment(
                user_status_id=user_status.id,
                duty_assignment_id=a.id,
                handle_status='pending'
            )
            db.session.add(saa)
            affected_list.append({
                'duty_assignment_id': a.id,
                'date': a.date,
                'start_time': a.start_time,
                'end_time': a.end_time,
                'is_cross_day': a.is_cross_day,
                'service_point_id': a.service_point_id,
                'service_point_name': a.service_point.name if a.service_point else None,
                'status': a.status
            })

        pending_notifications = ScheduleNotification.query.filter(
            ScheduleNotification.user_id == data['user_id'],
            ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
        ).all()
        for n in pending_notifications:
            n.status = 'expired'
            n.expired_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        'message': '人员状态设置成功',
        'id': user_status.id,
        'affected_count': len(affected_list),
        'affected_assignments': affected_list
    }), 201


@us_bp.route('', methods=['GET'])
@role_required('admin')
def list_all():
    query = UserStatus.query

    user_id = request.args.get('user_id')
    if user_id:
        query = query.filter(UserStatus.user_id == user_id)

    status = request.args.get('status')
    if status:
        query = query.filter(UserStatus.status == status)

    date_from = request.args.get('effective_time_from')
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from)
            query = query.filter(UserStatus.effective_time >= dt_from)
        except (ValueError, TypeError):
            pass

    date_to = request.args.get('effective_time_to')
    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to)
            query = query.filter(UserStatus.effective_time <= dt_to)
        except (ValueError, TypeError):
            pass

    records = query.order_by(UserStatus.created_at.desc()).all()
    return jsonify([_serialize_user_status(r) for r in records]), 200


@us_bp.route('/<int:us_id>', methods=['GET'])
@role_required('admin')
def get_one(us_id):
    us = UserStatus.query.get(us_id)
    if not us:
        return jsonify({'error': '状态记录不存在'}), 404

    result = _serialize_user_status(us)
    affected = us.affected_assignments.all()
    result['affected_assignments'] = [_serialize_affected(saa) for saa in affected]
    return jsonify(result), 200


@us_bp.route('/users', methods=['GET'])
@role_required('admin')
def list_users_status():
    role = request.args.get('role')
    status_filter = request.args.get('status')
    keyword = request.args.get('keyword')

    query = User.query
    if role:
        query = query.filter(User.role == role)
    if keyword:
        query = query.filter(
            db.or_(User.name.contains(keyword), User.username.contains(keyword), User.phone.contains(keyword))
        )

    users = query.all()
    result = []
    for u in users:
        current_status_info = _get_user_current_status(u.id)
        if status_filter and current_status_info.get('status') != status_filter:
            continue

        affected_count = _get_affected_assignment_count(u.id)

        result.append({
            'user_id': u.id,
            'username': u.username,
            'name': u.name,
            'phone': u.phone,
            'role': u.role,
            'current_status': current_status_info.get('status', 'active'),
            'status_effective_time': current_status_info.get('effective_time'),
            'status_end_time': current_status_info.get('end_time'),
            'status_reason': current_status_info.get('reason'),
            'last_status_change_at': current_status_info.get('created_at'),
            'affected_assignment_count': affected_count
        })

    return jsonify(result), 200


@us_bp.route('/users/<int:user_id>', methods=['GET'])
@role_required('admin')
def get_user_status_detail(user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': '用户不存在'}), 404

    current_status_info = _get_user_current_status(user_id)
    affected_count = _get_affected_assignment_count(user_id)

    recent_statuses = UserStatus.query.filter(
        UserStatus.user_id == user_id
    ).order_by(UserStatus.created_at.desc()).limit(10).all()

    affected_assignments = StatusAffectedAssignment.query.filter(
        StatusAffectedAssignment.handle_status == 'pending'
    ).join(UserStatus).filter(
        UserStatus.user_id == user_id,
        UserStatus.status.in_(['disabled', 'frozen'])
    ).all()

    return jsonify({
        'user_id': user.id,
        'username': user.username,
        'name': user.name,
        'phone': user.phone,
        'role': user.role,
        'current_status': current_status_info.get('status', 'active'),
        'status_effective_time': current_status_info.get('effective_time'),
        'status_end_time': current_status_info.get('end_time'),
        'status_reason': current_status_info.get('reason'),
        'last_status_change_at': current_status_info.get('created_at'),
        'affected_assignment_count': affected_count,
        'recent_status_changes': [_serialize_user_status(s) for s in recent_statuses],
        'pending_affected_assignments': [_serialize_affected(saa) for saa in affected_assignments]
    }), 200


@us_bp.route('/<int:us_id>/affected-assignments', methods=['GET'])
@role_required('admin')
def list_affected_assignments(us_id):
    us = UserStatus.query.get(us_id)
    if not us:
        return jsonify({'error': '状态记录不存在'}), 404

    query = us.affected_assignments

    handle_status = request.args.get('handle_status')
    if handle_status:
        query = query.filter(StatusAffectedAssignment.handle_status == handle_status)

    affected = query.order_by(StatusAffectedAssignment.created_at).all()

    total = us.affected_assignments.count()
    pending = us.affected_assignments.filter_by(handle_status='pending').count()
    reassigned = us.affected_assignments.filter_by(handle_status='reassigned').count()
    cancelled = us.affected_assignments.filter_by(handle_status='cancelled').count()

    return jsonify({
        'user_status_id': us_id,
        'summary': {
            'total': total,
            'pending': pending,
            'reassigned': reassigned,
            'cancelled': cancelled
        },
        'affected_assignments': [_serialize_affected(saa) for saa in affected]
    }), 200


@us_bp.route('/affected-assignments/<int:saa_id>/handle', methods=['POST'])
@role_required('admin')
def handle_affected_assignment(saa_id):
    saa = StatusAffectedAssignment.query.get(saa_id)
    if not saa:
        return jsonify({'error': '受影响排班记录不存在'}), 404

    if saa.handle_status != 'pending':
        return jsonify({'error': f'该班次已处理，当前状态: {saa.handle_status}'}), 400

    user_status = saa.user_status
    if user_status.status == 'active':
        saa.handle_status = 'cancelled'
        saa.handle_remark = '人员状态已恢复为在岗，自动关闭'
        saa.handled_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'message': '人员已恢复在岗，受影响班次已自动关闭'}), 200

    data = request.get_json()
    if not data or not data.get('handle_type'):
        return jsonify({'error': '处理方式为必填项'}), 400

    handle_type = data['handle_type']
    if handle_type not in ('reassign', 'cancel'):
        return jsonify({'error': '无效处理方式，可选: reassign, cancel'}), 400

    assignment = saa.duty_assignment
    if not assignment or assignment.status == 'cancelled':
        saa.handle_status = 'cancelled'
        saa.handle_remark = '原排班已取消，自动关闭'
        saa.handled_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'message': '原排班已取消，受影响班次已自动关闭'}), 200

    if handle_type == 'reassign':
        return _handle_reassign(saa, assignment, data)
    else:
        return _handle_cancel(saa, assignment, data)


@us_bp.route('/check', methods=['GET'])
@login_required
def check_user_status():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'error': 'user_id为必填项'}), 400

    status_info = _get_user_current_status(int(user_id))
    return jsonify({
        'user_id': int(user_id),
        'status': status_info.get('status', 'active'),
        'is_available': status_info.get('status', 'active') == 'active',
        'effective_time': status_info.get('effective_time'),
        'end_time': status_info.get('end_time'),
        'reason': status_info.get('reason')
    }), 200


def _handle_reassign(saa, assignment, data):
    new_user_id = data.get('new_user_id')
    if not new_user_id:
        return jsonify({'error': '改派需提供新人员ID'}), 400

    new_user = User.query.get(new_user_id)
    if not new_user:
        return jsonify({'error': '目标人员不存在'}), 404

    new_user_status_info = _get_user_current_status(new_user_id)
    if new_user_status_info.get('status') != 'active':
        return jsonify({'error': f'目标人员当前状态为{new_user_status_info.get("status")}，不可被指派'}), 400

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

    saa.handle_status = 'reassigned'
    saa.new_user_id = new_user_id
    saa.handle_remark = data.get('remark', f'由人员ID={old_user_id}改派至人员ID={new_user_id}')
    saa.handled_at = datetime.utcnow()

    old_notifications = ScheduleNotification.query.filter(
        ScheduleNotification.duty_assignment_id == assignment.id,
        ScheduleNotification.user_id == old_user_id,
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm', 'confirmed'])
    ).all()
    for n in old_notifications:
        n.status = 'expired'
        n.expired_at = datetime.utcnow()

    notification = ScheduleNotification(
        duty_assignment_id=assignment.id,
        user_id=new_user_id,
        notify_type='assignment',
        status='unnotified'
    )
    db.session.add(notification)

    if conflicts:
        log_conflicts(conflicts, new_user_id, assignment.service_point_id, assignment.date, assignment.id)

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


def _handle_cancel(saa, assignment, data):
    assignment.status = 'cancelled'

    all_notifications = ScheduleNotification.query.filter(
        ScheduleNotification.duty_assignment_id == assignment.id,
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm', 'confirmed'])
    ).all()
    for n in all_notifications:
        n.status = 'expired'
        n.expired_at = datetime.utcnow()

    saa.handle_status = 'cancelled'
    saa.handle_remark = data.get('remark', '因人员状态变更，班次直接取消')
    saa.handled_at = datetime.utcnow()

    db.session.commit()

    return jsonify({
        'message': '班次已取消',
        'assignment_id': assignment.id
    }), 200


def _get_user_current_status(user_id):
    latest = UserStatus.query.filter(
        UserStatus.user_id == user_id
    ).order_by(UserStatus.created_at.desc()).first()

    if not latest:
        return {'status': 'active'}

    now = datetime.utcnow()
    if latest.end_time and latest.end_time <= now:
        return {'status': 'active'}

    return {
        'status': latest.status,
        'effective_time': latest.effective_time.isoformat() if latest.effective_time else None,
        'end_time': latest.end_time.isoformat() if latest.end_time else None,
        'reason': latest.reason,
        'created_at': latest.created_at.isoformat() if latest.created_at else None
    }


def _get_affected_assignment_count(user_id):
    return StatusAffectedAssignment.query.filter(
        StatusAffectedAssignment.handle_status == 'pending'
    ).join(UserStatus).filter(
        UserStatus.user_id == user_id,
        UserStatus.status.in_(['disabled', 'frozen'])
    ).count()


def _serialize_user_status(us):
    total = us.affected_assignments.count()
    pending = us.affected_assignments.filter_by(handle_status='pending').count()
    handled = total - pending
    return {
        'id': us.id,
        'user_id': us.user_id,
        'user_name': us.user.name if us.user else None,
        'status': us.status,
        'effective_time': us.effective_time.isoformat() if us.effective_time else None,
        'end_time': us.end_time.isoformat() if us.end_time else None,
        'reason': us.reason,
        'remark': us.remark,
        'created_at': us.created_at.isoformat() if us.created_at else None,
        'created_by': us.created_by,
        'creator_name': us.creator.name if us.creator else None,
        'total_affected': total,
        'pending_count': pending,
        'handled_count': handled
    }


def _serialize_affected(saa):
    a = saa.duty_assignment
    return {
        'id': saa.id,
        'user_status_id': saa.user_status_id,
        'duty_assignment_id': saa.duty_assignment_id,
        'handle_status': saa.handle_status,
        'new_user_id': saa.new_user_id,
        'new_user_name': saa.new_user.name if saa.new_user else None,
        'handle_remark': saa.handle_remark,
        'handled_at': saa.handled_at.isoformat() if saa.handled_at else None,
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
