from flask import Blueprint, request, jsonify
from models import db, AttendanceRecord, DutyAssignment
from auth import login_required, role_required
from datetime import datetime, timedelta

att_bp = Blueprint('attendance', __name__, url_prefix='/api/attendance')


@att_bp.route('/check-in', methods=['POST'])
@login_required
def check_in():
    user = request.current_user
    data = request.get_json()
    if not data or not data.get('duty_assignment_id'):
        return jsonify({'error': '排班ID为必填项'}), 400

    assignment = DutyAssignment.query.get(data['duty_assignment_id'])
    if not assignment:
        return jsonify({'error': '排班记录不存在'}), 404

    if user.role not in ('admin',) and assignment.user_id != user.id:
        sub = assignment.substitutions.filter_by(substitute_user_id=user.id, status='approved').first()
        if not sub:
            return jsonify({'error': '只能为本人排班签到'}), 403

    if assignment.status == 'cancelled':
        return jsonify({'error': '已取消的排班不能签到'}), 400

    existing = AttendanceRecord.query.filter_by(duty_assignment_id=assignment.id).first()
    if existing and existing.check_in_time:
        return jsonify({'error': '该排班已签到'}), 409

    now = datetime.utcnow()
    assignment_date = assignment.date

    try:
        shift_date = datetime.strptime(assignment_date, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': '排班日期格式异常'}), 400

    today = now.date()
    if shift_date > today:
        return jsonify({'error': f'排班日期{assignment_date}尚未到达，不能提前签到'}), 400

    if assignment.is_cross_day:
        next_day_of_shift = shift_date + timedelta(days=1)
        if now.date() > next_day_of_shift:
            return jsonify({'error': f'排班{assignment_date}(跨日)已过期，不能签到'}), 400
    else:
        if today > shift_date:
            return jsonify({'error': f'排班日期{assignment_date}已过期，不能签到'}), 400

    start_minutes = _time_to_minutes(assignment.start_time)
    check_in_minutes = now.hour * 60 + now.minute
    status = 'on_time'

    if now.date() == shift_date:
        if check_in_minutes > start_minutes + 15:
            status = 'late'
    elif assignment.is_cross_day and now.date() == shift_date + timedelta(days=1):
        end_minutes = _time_to_minutes(assignment.end_time)
        if check_in_minutes > end_minutes:
            status = 'late'

    record = AttendanceRecord(
        duty_assignment_id=assignment.id,
        check_in_time=now,
        status=status
    )

    if existing:
        existing.check_in_time = now
        existing.status = status
    else:
        db.session.add(record)

    assignment.status = 'confirmed'
    db.session.commit()

    return jsonify({
        'message': '签到成功',
        'status': status,
        'check_in_time': now.isoformat()
    }), 200


def _time_to_minutes(time_str):
    parts = time_str.split(':')
    return int(parts[0]) * 60 + int(parts[1])


@att_bp.route('/records', methods=['GET'])
@login_required
def list_records():
    query = AttendanceRecord.query.join(DutyAssignment)

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
        query = query.filter(AttendanceRecord.status == status)

    records = query.order_by(AttendanceRecord.created_at.desc()).all()
    return jsonify([{
        'id': r.id,
        'duty_assignment_id': r.duty_assignment_id,
        'user_id': r.assignment.user_id if r.assignment else None,
        'user_name': r.assignment.user.name if r.assignment and r.assignment.user else None,
        'service_point_id': r.assignment.service_point_id if r.assignment else None,
        'service_point_name': r.assignment.service_point.name if r.assignment and r.assignment.service_point else None,
        'date': r.assignment.date if r.assignment else None,
        'check_in_time': r.check_in_time.isoformat() if r.check_in_time else None,
        'status': r.status,
        'inspector_id': r.inspector_id,
        'inspector_comment': r.inspector_comment,
        'reviewed_at': r.reviewed_at.isoformat() if r.reviewed_at else None
    } for r in records]), 200


@att_bp.route('/review/<int:r_id>', methods=['POST'])
@role_required('inspector', 'admin')
def review(r_id):
    record = AttendanceRecord.query.get(r_id)
    if not record:
        return jsonify({'error': '考勤记录不存在'}), 404

    data = request.get_json()
    if not data or not data.get('status'):
        return jsonify({'error': '复核状态为必填项'}), 400

    if data['status'] not in AttendanceRecord.valid_statuses():
        return jsonify({'error': f'无效状态，可选: {",".join(AttendanceRecord.valid_statuses())}'}), 400

    record.status = data['status']
    record.inspector_id = request.current_user.id
    record.inspector_comment = data.get('comment')
    record.reviewed_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'message': '复核完成'}), 200


@att_bp.route('/absent-check/<date>', methods=['GET'])
@role_required('inspector', 'admin')
def absent_check(date):
    assignments = DutyAssignment.query.filter(
        DutyAssignment.date == date,
        DutyAssignment.status != 'cancelled'
    ).all()

    absent_list = []
    for a in assignments:
        att = AttendanceRecord.query.filter_by(duty_assignment_id=a.id).first()
        if not att or att.status == 'absent':
            absent_list.append({
                'assignment_id': a.id,
                'user_id': a.user_id,
                'user_name': a.user.name if a.user else None,
                'service_point_id': a.service_point_id,
                'service_point_name': a.service_point.name if a.service_point else None,
                'date': a.date,
                'start_time': a.start_time,
                'end_time': a.end_time
            })

    return jsonify({'date': date, 'absent_count': len(absent_list), 'absent_list': absent_list}), 200
