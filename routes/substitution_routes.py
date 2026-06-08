from flask import Blueprint, request, jsonify
from models import db, Substitution, DutyAssignment, User, ScheduleNotification
from auth import login_required, role_required
from conflict import check_substitution_overlap, log_conflicts

sub_bp = Blueprint('substitutions', __name__, url_prefix='/api/substitutions')


@sub_bp.route('', methods=['POST'])
@login_required
def create():
    user = request.current_user
    data = request.get_json()
    if not data or not data.get('duty_assignment_id') or not data.get('substitute_user_id'):
        return jsonify({'error': '排班ID和替班人员ID为必填项'}), 400

    assignment = DutyAssignment.query.get(data['duty_assignment_id'])
    if not assignment:
        return jsonify({'error': '排班记录不存在'}), 404
    if assignment.status == 'cancelled':
        return jsonify({'error': '已取消的排班不能申请替班'}), 400

    sub_user = User.query.get(data['substitute_user_id'])
    if not sub_user:
        return jsonify({'error': '替班人员不存在'}), 404

    original_user_id = assignment.user_id
    if user.role not in ('admin',) and original_user_id != user.id:
        return jsonify({'error': '仅本人或管理员可申请替班'}), 403

    if data['substitute_user_id'] == original_user_id:
        return jsonify({'error': '不能替自己的班'}), 400

    existing = Substitution.query.filter(
        Substitution.duty_assignment_id == data['duty_assignment_id'],
        Substitution.status.in_(['pending', 'approved'])
    ).first()
    if existing:
        return jsonify({'error': '该排班已有进行中的替班申请'}), 409

    conflicts = check_substitution_overlap(
        substitute_user_id=data['substitute_user_id'],
        date=assignment.date,
        start_time=assignment.start_time,
        end_time=assignment.end_time,
        is_cross_day=assignment.is_cross_day
    )

    force = data.get('force', False)
    if conflicts and not force:
        return jsonify({'error': '替班人员时段冲突', 'conflicts': conflicts}), 409

    sub = Substitution(
        original_user_id=original_user_id,
        substitute_user_id=data['substitute_user_id'],
        duty_assignment_id=data['duty_assignment_id'],
        reason=data.get('reason'),
        status='pending'
    )
    db.session.add(sub)
    db.session.commit()

    if conflicts:
        conflict_logs = []
        for c in conflicts:
            conflict_logs.append({
                'type': 'substitution_overlap',
                'description': c.get('description', '')
            })
        log_conflicts(conflict_logs, data['substitute_user_id'],
                      assignment.service_point_id, assignment.date, assignment.id)

    return jsonify({
        'message': '替班申请已提交',
        'id': sub.id,
        'conflicts': conflicts if conflicts else None
    }), 201


@sub_bp.route('', methods=['GET'])
@login_required
def list_all():
    query = Substitution.query

    original_user_id = request.args.get('original_user_id')
    if original_user_id:
        query = query.filter(Substitution.original_user_id == original_user_id)

    substitute_user_id = request.args.get('substitute_user_id')
    if substitute_user_id:
        query = query.filter(Substitution.substitute_user_id == substitute_user_id)

    status = request.args.get('status')
    if status:
        query = query.filter(Substitution.status == status)

    subs = query.order_by(Substitution.created_at.desc()).all()
    return jsonify([{
        'id': s.id,
        'original_user_id': s.original_user_id,
        'original_user_name': s.original_user.name if s.original_user else None,
        'substitute_user_id': s.substitute_user_id,
        'substitute_user_name': s.substitute_user.name if s.substitute_user else None,
        'duty_assignment_id': s.duty_assignment_id,
        'reason': s.reason,
        'status': s.status,
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'reviewed_at': s.reviewed_at.isoformat() if s.reviewed_at else None,
        'notification_status': s.notifications[0].status if s.notifications and len(s.notifications) > 0 else None,
        'notification_id': s.notifications[0].id if s.notifications and len(s.notifications) > 0 else None
    } for s in subs]), 200


@sub_bp.route('/<int:s_id>', methods=['GET'])
@login_required
def get_one(s_id):
    s = Substitution.query.get(s_id)
    if not s:
        return jsonify({'error': '替班记录不存在'}), 404
    return jsonify({
        'id': s.id,
        'original_user_id': s.original_user_id,
        'original_user_name': s.original_user.name if s.original_user else None,
        'substitute_user_id': s.substitute_user_id,
        'substitute_user_name': s.substitute_user.name if s.substitute_user else None,
        'duty_assignment_id': s.duty_assignment_id,
        'reason': s.reason,
        'status': s.status,
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'reviewed_at': s.reviewed_at.isoformat() if s.reviewed_at else None
    }), 200


@sub_bp.route('/<int:s_id>/approve', methods=['POST'])
@role_required('admin')
def approve(s_id):
    s = Substitution.query.get(s_id)
    if not s:
        return jsonify({'error': '替班记录不存在'}), 404
    if s.status != 'pending':
        return jsonify({'error': '只能审批待审核的替班申请'}), 400

    from datetime import datetime
    s.status = 'approved'
    s.reviewed_at = datetime.utcnow()

    original_notifications = ScheduleNotification.query.filter(
        ScheduleNotification.duty_assignment_id == s.duty_assignment_id,
        ScheduleNotification.user_id == s.original_user_id,
        ScheduleNotification.status.in_(['unnotified', 'pending_confirm'])
    ).all()
    for n in original_notifications:
        n.status = 'expired'
        n.expired_at = datetime.utcnow()

    notification = ScheduleNotification(
        duty_assignment_id=s.duty_assignment_id,
        substitution_id=s.id,
        user_id=s.substitute_user_id,
        notify_type='substitution',
        status='unnotified'
    )
    db.session.add(notification)

    db.session.commit()
    return jsonify({'message': '替班申请已批准'}), 200


@sub_bp.route('/<int:s_id>/reject', methods=['POST'])
@role_required('admin')
def reject(s_id):
    s = Substitution.query.get(s_id)
    if not s:
        return jsonify({'error': '替班记录不存在'}), 404
    if s.status != 'pending':
        return jsonify({'error': '只能审批待审核的替班申请'}), 400

    from datetime import datetime
    s.status = 'rejected'
    s.reviewed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'message': '替班申请已驳回'}), 200
