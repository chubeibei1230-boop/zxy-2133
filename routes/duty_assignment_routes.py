from flask import Blueprint, request, jsonify
from models import db, DutyAssignment, User, ServicePoint
from auth import login_required, role_required
from conflict import detect_all_conflicts, log_conflicts

da_bp = Blueprint('duty_assignments', __name__, url_prefix='/api/duty-assignments')


@da_bp.route('', methods=['POST'])
@login_required
def create():
    user = request.current_user
    data = request.get_json()
    if not data or not data.get('service_point_id') or not data.get('date') \
            or not data.get('start_time') or not data.get('end_time'):
        return jsonify({'error': '服务点ID、日期、开始时间和结束时间为必填项'}), 400

    if not ShiftTemplate_validate_time(data['start_time']) or not ShiftTemplate_validate_time(data['end_time']):
        return jsonify({'error': '时间格式错误，应为HH:MM'}), 400

    target_user_id = data.get('user_id', user.id)
    if user.role not in ('admin',) and target_user_id != user.id:
        return jsonify({'error': '仅管理员可为他人创建排班'}), 403

    if not User.query.get(target_user_id):
        return jsonify({'error': '用户不存在'}), 404
    if not ServicePoint.query.get(data['service_point_id']):
        return jsonify({'error': '服务点不存在'}), 404

    is_cross_day = data.get('is_cross_day', False)
    start = data['start_time']
    end = data['end_time']
    if start > end:
        is_cross_day = True

    conflicts = detect_all_conflicts(
        user_id=target_user_id,
        service_point_id=data['service_point_id'],
        date=data['date'],
        start_time=start,
        end_time=end,
        is_cross_day=is_cross_day
    )

    force = data.get('force', False)
    if conflicts and not force:
        return jsonify({'error': '存在排班冲突', 'conflicts': conflicts}), 409

    assignment = DutyAssignment(
        user_id=target_user_id,
        service_point_id=data['service_point_id'],
        date=data['date'],
        start_time=start,
        end_time=end,
        status='pending',
        is_cross_day=is_cross_day
    )
    db.session.add(assignment)
    db.session.commit()

    if conflicts:
        log_conflicts(conflicts, target_user_id, data['service_point_id'], data['date'], assignment.id)

    return jsonify({
        'message': '排班创建成功',
        'id': assignment.id,
        'conflicts': conflicts if conflicts else None
    }), 201


def ShiftTemplate_validate_time(t):
    try:
        parts = t.split(':')
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, IndexError):
        return False


@da_bp.route('', methods=['GET'])
@login_required
def list_all():
    query = DutyAssignment.query

    user_id = request.args.get('user_id')
    if user_id:
        query = query.filter(DutyAssignment.user_id == user_id)

    sp_id = request.args.get('service_point_id')
    if sp_id:
        query = query.filter(DutyAssignment.service_point_id == sp_id)

    date = request.args.get('date')
    if date:
        query = query.filter(DutyAssignment.date == date)

    date_from = request.args.get('date_from')
    if date_from:
        query = query.filter(DutyAssignment.date >= date_from)

    date_to = request.args.get('date_to')
    if date_to:
        query = query.filter(DutyAssignment.date <= date_to)

    status = request.args.get('status')
    if status:
        query = query.filter(DutyAssignment.status == status)

    assignments = query.order_by(DutyAssignment.date, DutyAssignment.start_time).all()
    return jsonify([{
        'id': a.id,
        'user_id': a.user_id,
        'user_name': a.user.name if a.user else None,
        'service_point_id': a.service_point_id,
        'service_point_name': a.service_point.name if a.service_point else None,
        'date': a.date,
        'start_time': a.start_time,
        'end_time': a.end_time,
        'status': a.status,
        'is_cross_day': a.is_cross_day
    } for a in assignments]), 200


@da_bp.route('/<int:a_id>', methods=['GET'])
@login_required
def get_one(a_id):
    a = DutyAssignment.query.get(a_id)
    if not a:
        return jsonify({'error': '排班记录不存在'}), 404
    return jsonify({
        'id': a.id,
        'user_id': a.user_id,
        'user_name': a.user.name if a.user else None,
        'service_point_id': a.service_point_id,
        'service_point_name': a.service_point.name if a.service_point else None,
        'date': a.date,
        'start_time': a.start_time,
        'end_time': a.end_time,
        'status': a.status,
        'is_cross_day': a.is_cross_day
    }), 200


@da_bp.route('/<int:a_id>', methods=['PUT'])
@login_required
def update(a_id):
    user = request.current_user
    a = DutyAssignment.query.get(a_id)
    if not a:
        return jsonify({'error': '排班记录不存在'}), 404

    if user.role not in ('admin',) and a.user_id != user.id:
        return jsonify({'error': '无权修改他人排班'}), 403

    data = request.get_json()
    new_start = data.get('start_time', a.start_time)
    new_end = data.get('end_time', a.end_time)
    new_date = data.get('date', a.date)
    new_sp_id = data.get('service_point_id', a.service_point_id)
    new_is_cross = data.get('is_cross_day', a.is_cross_day)
    new_user_id = data.get('user_id', a.user_id)

    if new_start > new_end:
        new_is_cross = True

    conflicts = detect_all_conflicts(
        user_id=new_user_id,
        service_point_id=new_sp_id,
        date=new_date,
        start_time=new_start,
        end_time=new_end,
        is_cross_day=new_is_cross,
        exclude_id=a_id
    )

    force = data.get('force', False)
    if conflicts and not force:
        return jsonify({'error': '存在排班冲突', 'conflicts': conflicts}), 409

    a.start_time = new_start
    a.end_time = new_end
    a.date = new_date
    a.service_point_id = new_sp_id
    a.is_cross_day = new_is_cross
    if 'user_id' in data:
        if user.role != 'admin':
            return jsonify({'error': '仅管理员可变更排班人员'}), 403
        if data['user_id'] != a.user_id and not User.query.get(data['user_id']):
            return jsonify({'error': '目标用户不存在'}), 404
        a.user_id = data['user_id']
    if 'status' in data:
        if data['status'] not in DutyAssignment.valid_statuses():
            return jsonify({'error': f'无效状态，可选: {",".join(DutyAssignment.valid_statuses())}'}), 400
        a.status = data['status']

    db.session.commit()

    if conflicts:
        log_conflicts(conflicts, new_user_id, new_sp_id, new_date, a.id)

    return jsonify({
        'message': '排班更新成功',
        'conflicts': conflicts if conflicts else None
    }), 200


@da_bp.route('/<int:a_id>/cancel', methods=['POST'])
@login_required
def cancel(a_id):
    user = request.current_user
    a = DutyAssignment.query.get(a_id)
    if not a:
        return jsonify({'error': '排班记录不存在'}), 404

    if user.role not in ('admin',) and a.user_id != user.id:
        return jsonify({'error': '无权取消他人排班'}), 403

    if a.status == 'cancelled':
        return jsonify({'error': '该排班已取消'}), 400

    a.status = 'cancelled'
    db.session.commit()
    return jsonify({'message': '排班已取消，占用时段已释放'}), 200
