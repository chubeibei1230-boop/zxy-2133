from flask import Blueprint, request, jsonify
from models import (db, LeaveRequest, LeaveAffectedAssignment, DutyAssignment,
                    User, ServicePoint, Substitution, ScheduleNotification, ConflictLog)
from auth import login_required, role_required
from conflict import (detect_all_conflicts, log_conflicts, get_effective_assignments,
                      check_substitution_overlap, check_over_limit_conflict,
                      get_substitute_user_id, intervals_overlap, time_to_minutes)
from datetime import datetime

leave_bp = Blueprint('leave_requests', __name__, url_prefix='/api/leave-requests')


@leave_bp.route('', methods=['POST'])
@login_required
def create():
    user = request.current_user
    data = request.get_json()
    if not data or not data.get('reason') or not data.get('start_date') or not data.get('end_date'):
        return jsonify({'error': '请假原因、开始日期和结束日期为必填项'}), 400

    if data['start_date'] > data['end_date']:
        return jsonify({'error': '开始日期不能晚于结束日期'}), 400

    affected_ids = data.get('affected_assignment_ids', [])
    assignments = DutyAssignment.query.filter(
        DutyAssignment.id.in_(affected_ids),
        DutyAssignment.user_id == user.id,
        DutyAssignment.date >= data['start_date'],
        DutyAssignment.date <= data['end_date'],
        DutyAssignment.status != 'cancelled'
    ).all() if affected_ids else []

    if not assignments:
        assignments = DutyAssignment.query.filter(
            DutyAssignment.user_id == user.id,
            DutyAssignment.date >= data['start_date'],
            DutyAssignment.date <= data['end_date'],
            DutyAssignment.status != 'cancelled'
        ).order_by(DutyAssignment.date, DutyAssignment.start_time).all()

    leave = LeaveRequest(
        user_id=user.id,
        reason=data['reason'],
        start_date=data['start_date'],
        end_date=data['end_date'],
        status='pending'
    )
    db.session.add(leave)
    db.session.flush()

    for a in assignments:
        la = LeaveAffectedAssignment(
            leave_request_id=leave.id,
            duty_assignment_id=a.id,
            fill_status='pending'
        )
        db.session.add(la)

    db.session.commit()

    return jsonify({
        'message': '请假申请已提交',
        'id': leave.id,
        'affected_count': len(assignments)
    }), 201


@leave_bp.route('/my', methods=['GET'])
@login_required
def my_leaves():
    user = request.current_user
    query = LeaveRequest.query.filter(LeaveRequest.user_id == user.id)

    status = request.args.get('status')
    if status:
        query = query.filter(LeaveRequest.status == status)

    leaves = query.order_by(LeaveRequest.created_at.desc()).all()
    return jsonify([_serialize_leave(l) for l in leaves]), 200


@leave_bp.route('/my/<int:leave_id>', methods=['GET'])
@login_required
def my_leave_detail(leave_id):
    user = request.current_user
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    if leave.user_id != user.id and user.role != 'admin':
        return jsonify({'error': '无权查看该请假记录'}), 403

    return jsonify(_serialize_leave_detail(leave)), 200


@leave_bp.route('/my/<int:leave_id>/cancel', methods=['POST'])
@login_required
def cancel_leave(leave_id):
    user = request.current_user
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    if leave.user_id != user.id:
        return jsonify({'error': '只能取消自己的请假申请'}), 403
    if leave.status != 'pending':
        return jsonify({'error': '只能取消待审批的请假申请'}), 400

    leave.status = 'cancelled'
    db.session.commit()
    return jsonify({'message': '请假申请已取消'}), 200


@leave_bp.route('', methods=['GET'])
@role_required('admin')
def list_all():
    query = LeaveRequest.query

    user_id = request.args.get('user_id')
    if user_id:
        query = query.filter(LeaveRequest.user_id == user_id)

    status = request.args.get('status')
    if status:
        query = query.filter(LeaveRequest.status == status)

    start_date = request.args.get('start_date')
    if start_date:
        query = query.filter(LeaveRequest.start_date >= start_date)

    end_date = request.args.get('end_date')
    if end_date:
        query = query.filter(LeaveRequest.end_date <= end_date)

    service_point_id = request.args.get('service_point_id')
    if service_point_id:
        query = query.join(LeaveAffectedAssignment).join(DutyAssignment).filter(
            DutyAssignment.service_point_id == service_point_id
        )

    leaves = query.order_by(LeaveRequest.created_at.desc()).all()
    return jsonify([_serialize_leave_admin(l) for l in leaves]), 200


@leave_bp.route('/<int:leave_id>', methods=['GET'])
@role_required('admin')
def get_detail(leave_id):
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    return jsonify(_serialize_leave_detail(leave)), 200


@leave_bp.route('/<int:leave_id>/approve', methods=['POST'])
@role_required('admin')
def approve_leave(leave_id):
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    if leave.status != 'pending':
        return jsonify({'error': '只能审批待审核的请假申请'}), 400

    data = request.get_json() or {}
    leave.status = 'approved'
    leave.admin_comment = data.get('admin_comment')
    leave.reviewed_at = datetime.utcnow()

    affected = leave.affected_assignments.all()
    for la in affected:
        assignment = la.assignment
        if assignment and assignment.status != 'cancelled':
            assignment.status = 'pending'
            existing_notifications = ScheduleNotification.query.filter(
                ScheduleNotification.duty_assignment_id == assignment.id,
                ScheduleNotification.user_id == leave.user_id,
                ScheduleNotification.status.in_(['unnotified', 'pending_confirm', 'confirmed'])
            ).all()
            for n in existing_notifications:
                n.status = 'expired'
                n.expired_at = datetime.utcnow()

    db.session.commit()
    return jsonify({
        'message': '请假申请已批准',
        'affected_count': len(affected)
    }), 200


@leave_bp.route('/<int:leave_id>/reject', methods=['POST'])
@role_required('admin')
def reject_leave(leave_id):
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    if leave.status != 'pending':
        return jsonify({'error': '只能审批待审核的请假申请'}), 400

    data = request.get_json() or {}
    leave.status = 'rejected'
    leave.admin_comment = data.get('admin_comment')
    leave.reviewed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'message': '请假申请已驳回'}), 200


@leave_bp.route('/<int:leave_id>/recommend-substitutes', methods=['GET'])
@role_required('admin')
def recommend_substitutes(leave_id):
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    if leave.status != 'approved':
        return jsonify({'error': '只能为已批准的请假推荐替班人员'}), 400

    affected = leave.affected_assignments.filter(
        LeaveAffectedAssignment.fill_status.in_(['pending', 'filling'])
    ).all()

    all_staff = User.query.filter(User.role == 'staff', User.id != leave.user_id).all()

    recommendations = []
    for la in affected:
        assignment = la.assignment
        if not assignment:
            continue

        candidates = []
        for staff in all_staff:
            overlapping_leaves = LeaveRequest.query.filter(
                LeaveRequest.user_id == staff.id,
                LeaveRequest.status.in_(['pending', 'approved']),
                LeaveRequest.start_date <= assignment.date,
                LeaveRequest.end_date >= assignment.date
            ).first()
            if overlapping_leaves:
                continue

            conflicts = check_substitution_overlap(
                substitute_user_id=staff.id,
                date=assignment.date,
                start_time=assignment.start_time,
                end_time=assignment.end_time,
                is_cross_day=assignment.is_cross_day,
                exclude_id=assignment.id
            )
            over_limit = check_over_limit_conflict(
                service_point_id=assignment.service_point_id,
                date=assignment.date,
                start_time=assignment.start_time,
                end_time=assignment.end_time,
                is_cross_day=assignment.is_cross_day,
                exclude_id=assignment.id
            )
            conflicts.extend(over_limit)

            same_sp_exp = DutyAssignment.query.filter(
                DutyAssignment.user_id == staff.id,
                DutyAssignment.service_point_id == assignment.service_point_id,
                DutyAssignment.status != 'cancelled'
            ).first() is not None

            candidates.append({
                'user_id': staff.id,
                'user_name': staff.name,
                'has_conflict': len(conflicts) > 0,
                'conflicts': conflicts if conflicts else None,
                'same_service_point': same_sp_exp
            })

        candidates.sort(key=lambda c: (c['has_conflict'], not c['same_service_point']))

        recommendations.append({
            'leave_affected_id': la.id,
            'duty_assignment_id': assignment.id,
            'date': assignment.date,
            'start_time': assignment.start_time,
            'end_time': assignment.end_time,
            'is_cross_day': assignment.is_cross_day,
            'service_point_id': assignment.service_point_id,
            'service_point_name': assignment.service_point.name if assignment.service_point else None,
            'fill_status': la.fill_status,
            'candidates': candidates
        })

    return jsonify({'recommendations': recommendations}), 200


@leave_bp.route('/<int:leave_id>/auto-fill', methods=['POST'])
@role_required('admin')
def auto_fill(leave_id):
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    if leave.status != 'approved':
        return jsonify({'error': '只能为已批准的请假进行自动补位'}), 400

    affected = leave.affected_assignments.filter(
        LeaveAffectedAssignment.fill_status.in_(['pending', 'filling'])
    ).all()

    all_staff = User.query.filter(User.role == 'staff', User.id != leave.user_id).all()

    used_substitutes = {}
    results = []
    warnings = []
    filled_count = 0
    unfilled_count = 0
    conflict_count = 0

    for la in affected:
        assignment = la.assignment
        if not assignment:
            continue

        candidates = []
        for staff in all_staff:
            overlapping_leaves = LeaveRequest.query.filter(
                LeaveRequest.user_id == staff.id,
                LeaveRequest.status.in_(['pending', 'approved']),
                LeaveRequest.start_date <= assignment.date,
                LeaveRequest.end_date >= assignment.date
            ).first()
            if overlapping_leaves:
                continue

            conflicts = check_substitution_overlap(
                substitute_user_id=staff.id,
                date=assignment.date,
                start_time=assignment.start_time,
                end_time=assignment.end_time,
                is_cross_day=assignment.is_cross_day,
                exclude_id=assignment.id
            )
            over_limit = check_over_limit_conflict(
                service_point_id=assignment.service_point_id,
                date=assignment.date,
                start_time=assignment.start_time,
                end_time=assignment.end_time,
                is_cross_day=assignment.is_cross_day,
                exclude_id=assignment.id
            )
            conflicts.extend(over_limit)

            same_sp_exp = DutyAssignment.query.filter(
                DutyAssignment.user_id == staff.id,
                DutyAssignment.service_point_id == assignment.service_point_id,
                DutyAssignment.status != 'cancelled'
            ).first() is not None

            candidates.append({
                'user_id': staff.id,
                'user_name': staff.name,
                'has_conflict': len(conflicts) > 0,
                'conflict_count': len(conflicts),
                'same_service_point': same_sp_exp
            })

        candidates.sort(key=lambda c: (c['has_conflict'], not c['same_service_point']))

        best = None
        for c in candidates:
            date_key = assignment.date
            if c['user_id'] not in used_substitutes.get(date_key, set()):
                best = c
                break

        if best and not best['has_conflict']:
            _fill_assignment(la, assignment, best['user_id'], leave)
            filled_count += 1
            results.append({
                'leave_affected_id': la.id,
                'duty_assignment_id': assignment.id,
                'status': 'filled',
                'substitute_user_id': best['user_id'],
                'substitute_user_name': best['user_name']
            })
            if assignment.date not in used_substitutes:
                used_substitutes[assignment.date] = set()
            used_substitutes[assignment.date].add(best['user_id'])
        elif best and best['has_conflict']:
            la.fill_status = 'conflict'
            la.substitute_user_id = best['user_id']
            conflict_count += 1
            log_conflicts(
                [{'type': 'leave_conflict',
                  'description': f'请假ID={leave.id}自动补位：推荐替班人员ID={best["user_id"]}({best["user_name"]})存在排班冲突，班次ID={assignment.id}'}],
                best['user_id'], assignment.service_point_id, assignment.date, assignment.id
            )
            results.append({
                'leave_affected_id': la.id,
                'duty_assignment_id': assignment.id,
                'status': 'conflict',
                'substitute_user_id': best['user_id'],
                'substitute_user_name': best['user_name'],
                'warning': f'推荐替班人员{best["user_name"]}存在排班冲突，请手动确认或改派'
            })
            warnings.append(f'班次{assignment.date} {assignment.start_time}-{assignment.end_time}的推荐替班人员存在冲突')
        else:
            la.fill_status = 'unfilled'
            unfilled_count += 1
            log_conflicts(
                [{'type': 'leave_unfilled',
                  'description': f'请假ID={leave.id}(员工ID={leave.user_id})自动补位：班次ID={assignment.id}无可用替班人员'}],
                leave.user_id, assignment.service_point_id, assignment.date, assignment.id
            )
            results.append({
                'leave_affected_id': la.id,
                'duty_assignment_id': assignment.id,
                'status': 'unfilled',
                'warning': '无可用替班人员，该班次暂无人顶替'
            })
            warnings.append(f'班次{assignment.date} {assignment.start_time}-{assignment.end_time}无可用替班人员')

    db.session.commit()

    return jsonify({
        'message': f'自动补位处理完成：已补位{filled_count}个，冲突{conflict_count}个，无人可替{unfilled_count}个',
        'filled_count': filled_count,
        'conflict_count': conflict_count,
        'unfilled_count': unfilled_count,
        'results': results,
        'warnings': warnings
    }), 200


@leave_bp.route('/<int:leave_id>/assignments/<int:la_id>/assign', methods=['POST'])
@role_required('admin')
def manual_assign(leave_id, la_id):
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404
    if leave.status != 'approved':
        return jsonify({'error': '只能为已批准的请假进行手动改派'}), 400

    la = LeaveAffectedAssignment.query.get(la_id)
    if not la or la.leave_request_id != leave_id:
        return jsonify({'error': '受影响班次记录不存在'}), 404

    if la.fill_status == 'filled':
        return jsonify({'error': '该班次已完成补位，如需更改请先取消原补位'}), 400

    data = request.get_json()
    if not data or not data.get('substitute_user_id'):
        return jsonify({'error': '替班人员ID为必填项'}), 400

    sub_user = User.query.get(data['substitute_user_id'])
    if not sub_user:
        return jsonify({'error': '替班人员不存在'}), 404
    if sub_user.id == leave.user_id:
        return jsonify({'error': '不能指定请假人自己为替班人员'}), 400

    assignment = la.assignment
    if not assignment:
        return jsonify({'error': '排班记录不存在'}), 404

    overlapping_leaves = LeaveRequest.query.filter(
        LeaveRequest.user_id == sub_user.id,
        LeaveRequest.status.in_(['pending', 'approved']),
        LeaveRequest.start_date <= assignment.date,
        LeaveRequest.end_date >= assignment.date
    ).first()
    if overlapping_leaves:
        return jsonify({'error': f'替班人员{sub_user.name}在{assignment.date}也有请假申请，无法替班'}), 409

    conflicts = check_substitution_overlap(
        substitute_user_id=sub_user.id,
        date=assignment.date,
        start_time=assignment.start_time,
        end_time=assignment.end_time,
        is_cross_day=assignment.is_cross_day,
        exclude_id=assignment.id
    )
    over_limit = check_over_limit_conflict(
        service_point_id=assignment.service_point_id,
        date=assignment.date,
        start_time=assignment.start_time,
        end_time=assignment.end_time,
        is_cross_day=assignment.is_cross_day,
        exclude_id=assignment.id
    )
    conflicts.extend(over_limit)

    force = data.get('force', False)
    if conflicts and not force:
        return jsonify({
            'error': '替班人员存在排班冲突',
            'conflicts': conflicts,
            'hint': '如需强制指派，请设置force=true'
        }), 409

    _fill_assignment(la, assignment, sub_user.id, leave)

    if conflicts:
        la.fill_status = 'conflict'
        log_conflicts(
            [{'type': 'leave_conflict',
              'description': f'请假ID={leave.id}手动改派：替班人员ID={sub_user.id}存在排班冲突（强制指派）'}],
            sub_user.id, assignment.service_point_id, assignment.date, assignment.id
        )

    db.session.commit()

    result = {
        'message': '手动改派成功',
        'leave_affected_id': la.id,
        'substitute_user_id': sub_user.id,
        'substitute_user_name': sub_user.name,
        'fill_status': la.fill_status
    }
    if conflicts:
        result['warnings'] = [c.get('description', '') for c in conflicts]

    return jsonify(result), 200


@leave_bp.route('/<int:leave_id>/assignments/<int:la_id>/cancel-fill', methods=['POST'])
@role_required('admin')
def cancel_fill(leave_id, la_id):
    leave = LeaveRequest.query.get(leave_id)
    if not leave:
        return jsonify({'error': '请假记录不存在'}), 404

    la = LeaveAffectedAssignment.query.get(la_id)
    if not la or la.leave_request_id != leave_id:
        return jsonify({'error': '受影响班次记录不存在'}), 404

    if la.fill_status not in ('filled', 'conflict', 'filling'):
        return jsonify({'error': '该班次未完成补位，无法取消'}), 400

    if la.substitute_user_id:
        sub = Substitution.query.filter(
            Substitution.duty_assignment_id == la.duty_assignment_id,
            Substitution.substitute_user_id == la.substitute_user_id,
            Substitution.status == 'approved'
        ).first()
        if sub:
            sub.status = 'rejected'
            sub.reviewed_at = datetime.utcnow()

        notifications = ScheduleNotification.query.filter(
            ScheduleNotification.duty_assignment_id == la.duty_assignment_id,
            ScheduleNotification.user_id == la.substitute_user_id,
            ScheduleNotification.notify_type == 'leave_fill',
            ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
        ).all()
        for n in notifications:
            n.status = 'expired'
            n.expired_at = datetime.utcnow()

    la.fill_status = 'pending'
    la.substitute_user_id = None
    la.fill_confirmed_at = None
    db.session.commit()

    return jsonify({'message': '补位已取消，班次恢复为待补位状态'}), 200


@leave_bp.route('/statistics', methods=['GET'])
@role_required('admin')
def statistics():
    from sqlalchemy import func

    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    leave_query = LeaveRequest.query
    if date_from or date_to:
        leave_query = leave_query.join(LeaveAffectedAssignment).join(DutyAssignment)
        if date_from:
            leave_query = leave_query.filter(DutyAssignment.date >= date_from)
        if date_to:
            leave_query = leave_query.filter(DutyAssignment.date <= date_to)

    total = leave_query.count()
    by_status = db.session.query(
        LeaveRequest.status, func.count(LeaveRequest.id)
    ).join(LeaveAffectedAssignment).join(DutyAssignment)
    if date_from:
        by_status = by_status.filter(DutyAssignment.date >= date_from)
    if date_to:
        by_status = by_status.filter(DutyAssignment.date <= date_to)
    by_status = by_status.group_by(LeaveRequest.status).all()
    status_counts = {s: c for s, c in by_status}

    affected_query = LeaveAffectedAssignment.query
    if date_from or date_to:
        affected_query = affected_query.join(DutyAssignment)
        if date_from:
            affected_query = affected_query.filter(DutyAssignment.date >= date_from)
        if date_to:
            affected_query = affected_query.filter(DutyAssignment.date <= date_to)

    affected_total = affected_query.count()
    by_fill_status = affected_query.with_entities(
        LeaveAffectedAssignment.fill_status, func.count(LeaveAffectedAssignment.id)
    ).group_by(LeaveAffectedAssignment.fill_status).all()
    fill_status_counts = {s: c for s, c in by_fill_status}

    pending_fill = affected_query.filter(
        LeaveAffectedAssignment.fill_status == 'pending'
    ).count()
    filled = affected_query.filter(
        LeaveAffectedAssignment.fill_status == 'filled'
    ).count()
    unfilled = affected_query.filter(
        LeaveAffectedAssignment.fill_status == 'unfilled'
    ).count()
    conflict = affected_query.filter(
        LeaveAffectedAssignment.fill_status == 'conflict'
    ).count()

    sp_query = db.session.query(
        ServicePoint.id, ServicePoint.name,
        func.count(LeaveAffectedAssignment.id)
    ).join(
        DutyAssignment, DutyAssignment.service_point_id == ServicePoint.id
    ).join(
        LeaveAffectedAssignment, LeaveAffectedAssignment.duty_assignment_id == DutyAssignment.id
    )
    if date_from:
        sp_query = sp_query.filter(DutyAssignment.date >= date_from)
    if date_to:
        sp_query = sp_query.filter(DutyAssignment.date <= date_to)
    service_point_stats = sp_query.group_by(ServicePoint.id, ServicePoint.name).all()

    recent_unfilled = affected_query.filter(
        LeaveAffectedAssignment.fill_status.in_(['unfilled', 'conflict'])
    ).join(DutyAssignment).order_by(DutyAssignment.date).limit(10).all()

    return jsonify({
        'total_leave_requests': total,
        'status_counts': status_counts,
        'affected_assignments_total': affected_total,
        'fill_status_counts': fill_status_counts,
        'pending_fill': pending_fill,
        'filled': filled,
        'unfilled': unfilled,
        'conflict': conflict,
        'fill_progress': {
            'total': affected_total,
            'completed': filled,
            'completion_rate': round(filled / affected_total * 100, 1) if affected_total > 0 else 0
        },
        'service_point_breakdown': [
            {'service_point_id': sp_id, 'service_point_name': sp_name, 'affected_count': cnt}
            for sp_id, sp_name, cnt in service_point_stats
        ],
        'unfilled_or_conflict_details': [_serialize_affected(la) for la in recent_unfilled]
    }), 200


def _fill_assignment(la, assignment, substitute_user_id, leave):
    sub = Substitution(
        original_user_id=leave.user_id,
        substitute_user_id=substitute_user_id,
        duty_assignment_id=assignment.id,
        reason=f'请假自动补位(请假ID={leave.id})',
        status='approved'
    )
    db.session.add(sub)
    db.session.flush()

    la.fill_status = 'filled'
    la.substitute_user_id = substitute_user_id
    la.fill_confirmed_at = datetime.utcnow()

    notification = ScheduleNotification(
        duty_assignment_id=assignment.id,
        substitution_id=sub.id,
        user_id=substitute_user_id,
        notify_type='leave_fill',
        status='unnotified'
    )
    db.session.add(notification)


def _serialize_leave(l):
    affected = l.affected_assignments.all()
    return {
        'id': l.id,
        'user_id': l.user_id,
        'user_name': l.user.name if l.user else None,
        'reason': l.reason,
        'start_date': l.start_date,
        'end_date': l.end_date,
        'status': l.status,
        'admin_comment': l.admin_comment,
        'affected_count': len(affected),
        'filled_count': sum(1 for a in affected if a.fill_status == 'filled'),
        'unfilled_count': sum(1 for a in affected if a.fill_status in ('unfilled', 'conflict')),
        'created_at': l.created_at.isoformat() if l.created_at else None,
        'reviewed_at': l.reviewed_at.isoformat() if l.reviewed_at else None
    }


def _serialize_leave_admin(l):
    result = _serialize_leave(l)
    return result


def _serialize_leave_detail(l):
    result = _serialize_leave(l)
    affected = l.affected_assignments.all()
    result['affected_assignments'] = [_serialize_affected(la) for la in affected]
    return result


def _serialize_affected(la):
    assignment = la.assignment
    return {
        'id': la.id,
        'duty_assignment_id': la.duty_assignment_id,
        'fill_status': la.fill_status,
        'substitute_user_id': la.substitute_user_id,
        'substitute_user_name': la.substitute_user.name if la.substitute_user else None,
        'fill_confirmed_at': la.fill_confirmed_at.isoformat() if la.fill_confirmed_at else None,
        'assignment': {
            'id': assignment.id,
            'service_point_id': assignment.service_point_id,
            'service_point_name': assignment.service_point.name if assignment and assignment.service_point else None,
            'date': assignment.date,
            'start_time': assignment.start_time,
            'end_time': assignment.end_time,
            'is_cross_day': assignment.is_cross_day,
            'status': assignment.status
        } if assignment else None
    }
