from flask import Blueprint, request, jsonify
from models import ConflictLog, DutyAssignment, User, ServicePoint, db
from auth import login_required, role_required
from conflict import detect_all_conflicts, time_to_minutes

conflict_bp = Blueprint('conflicts', __name__, url_prefix='/api/conflicts')


@conflict_bp.route('', methods=['GET'])
@login_required
def query_conflicts():
    query = ConflictLog.query

    user_id = request.args.get('user_id')
    if user_id:
        query = query.filter(ConflictLog.user_id == user_id)

    sp_id = request.args.get('service_point_id')
    if sp_id:
        query = query.filter(ConflictLog.service_point_id == sp_id)

    date = request.args.get('date')
    if date:
        query = query.filter(ConflictLog.date == date)

    date_from = request.args.get('date_from')
    if date_from:
        query = query.filter(ConflictLog.date >= date_from)

    date_to = request.args.get('date_to')
    if date_to:
        query = query.filter(ConflictLog.date <= date_to)

    conflict_type = request.args.get('conflict_type')
    if conflict_type:
        if conflict_type not in ConflictLog.valid_conflict_types():
            return jsonify({'error': f'无效冲突类型，可选: {",".join(ConflictLog.valid_conflict_types())}'}), 400
        query = query.filter(ConflictLog.conflict_type == conflict_type)

    logs = query.order_by(ConflictLog.detected_at.desc()).all()
    return jsonify([{
        'id': l.id,
        'duty_assignment_id': l.duty_assignment_id,
        'user_id': l.user_id,
        'user_name': l.user.name if l.user else None,
        'service_point_id': l.service_point_id,
        'service_point_name': l.service_point.name if l.service_point else None,
        'date': l.date,
        'conflict_type': l.conflict_type,
        'description': l.description,
        'detected_at': l.detected_at.isoformat() if l.detected_at else None
    } for l in logs]), 200


@conflict_bp.route('/check', methods=['POST'])
@login_required
def check_conflicts():
    data = request.get_json()
    if not data or not data.get('user_id') or not data.get('service_point_id') \
            or not data.get('date') or not data.get('start_time') or not data.get('end_time'):
        return jsonify({'error': 'user_id, service_point_id, date, start_time, end_time 为必填项'}), 400

    conflicts = detect_all_conflicts(
        user_id=data['user_id'],
        service_point_id=data['service_point_id'],
        date=data['date'],
        start_time=data['start_time'],
        end_time=data['end_time'],
        is_cross_day=data.get('is_cross_day', False),
        exclude_id=data.get('exclude_id'),
        is_substitution=data.get('is_substitution', False),
        substitute_user_id=data.get('substitute_user_id')
    )

    return jsonify({'conflicts': conflicts, 'conflict_count': len(conflicts)}), 200


@conflict_bp.route('/scan/<date>', methods=['POST'])
@role_required('inspector', 'admin')
def scan_date(date):
    assignments = DutyAssignment.query.filter(
        DutyAssignment.date == date,
        DutyAssignment.status != 'cancelled'
    ).all()

    all_conflicts = []
    for a in assignments:
        conflicts = detect_all_conflicts(
            user_id=a.user_id,
            service_point_id=a.service_point_id,
            date=a.date,
            start_time=a.start_time,
            end_time=a.end_time,
            is_cross_day=a.is_cross_day,
            exclude_id=a.id
        )

        for c in conflicts:
            existing = ConflictLog.query.filter(
                ConflictLog.duty_assignment_id == a.id,
                ConflictLog.conflict_type == c['type'],
                ConflictLog.date == date
            ).first()

            if not existing:
                log = ConflictLog(
                    duty_assignment_id=a.id,
                    user_id=a.user_id,
                    service_point_id=a.service_point_id,
                    date=date,
                    conflict_type=c['type'],
                    description=c.get('description', '')
                )
                db.session.add(log)
                all_conflicts.append({
                    'assignment_id': a.id,
                    'user_id': a.user_id,
                    'conflict': c
                })

    db.session.commit()
    return jsonify({
        'date': date,
        'total_conflicts': len(all_conflicts),
        'conflicts': all_conflicts
    }), 200
