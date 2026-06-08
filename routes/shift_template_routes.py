from flask import Blueprint, request, jsonify
from models import db, ShiftTemplate, ServicePoint
from auth import login_required, role_required

st_bp = Blueprint('shift_templates', __name__, url_prefix='/api/shift-templates')


@st_bp.route('', methods=['POST'])
@role_required('admin')
def create():
    data = request.get_json()
    if not data or not data.get('name') or not data.get('start_time') or not data.get('end_time'):
        return jsonify({'error': '班次名称、开始时间和结束时间为必填项'}), 400

    if not ShiftTemplate.validate_time_format(data['start_time']):
        return jsonify({'error': '开始时间格式错误，应为HH:MM'}), 400
    if not ShiftTemplate.validate_time_format(data['end_time']):
        return jsonify({'error': '结束时间格式错误，应为HH:MM'}), 400

    sp_id = data.get('service_point_id')
    if sp_id and not ServicePoint.query.get(sp_id):
        return jsonify({'error': '服务点不存在'}), 404

    st = ShiftTemplate(
        name=data['name'],
        start_time=data['start_time'],
        end_time=data['end_time'],
        is_cross_day=data.get('is_cross_day', False),
        service_point_id=sp_id
    )
    db.session.add(st)
    db.session.commit()
    return jsonify({'message': '班次模板创建成功', 'id': st.id}), 201


@st_bp.route('', methods=['GET'])
@login_required
def list_all():
    query = ShiftTemplate.query
    sp_id = request.args.get('service_point_id')
    if sp_id:
        query = query.filter(ShiftTemplate.service_point_id == sp_id)
    templates = query.all()
    return jsonify([{
        'id': t.id,
        'name': t.name,
        'start_time': t.start_time,
        'end_time': t.end_time,
        'is_cross_day': t.is_cross_day,
        'service_point_id': t.service_point_id
    } for t in templates]), 200


@st_bp.route('/<int:t_id>', methods=['GET'])
@login_required
def get_one(t_id):
    t = ShiftTemplate.query.get(t_id)
    if not t:
        return jsonify({'error': '班次模板不存在'}), 404
    return jsonify({
        'id': t.id,
        'name': t.name,
        'start_time': t.start_time,
        'end_time': t.end_time,
        'is_cross_day': t.is_cross_day,
        'service_point_id': t.service_point_id
    }), 200


@st_bp.route('/<int:t_id>', methods=['PUT'])
@role_required('admin')
def update(t_id):
    t = ShiftTemplate.query.get(t_id)
    if not t:
        return jsonify({'error': '班次模板不存在'}), 404

    data = request.get_json()
    if data.get('name'):
        t.name = data['name']
    if data.get('start_time'):
        if not ShiftTemplate.validate_time_format(data['start_time']):
            return jsonify({'error': '开始时间格式错误，应为HH:MM'}), 400
        t.start_time = data['start_time']
    if data.get('end_time'):
        if not ShiftTemplate.validate_time_format(data['end_time']):
            return jsonify({'error': '结束时间格式错误，应为HH:MM'}), 400
        t.end_time = data['end_time']
    if 'is_cross_day' in data:
        t.is_cross_day = data['is_cross_day']
    if 'service_point_id' in data:
        if data['service_point_id'] and not ServicePoint.query.get(data['service_point_id']):
            return jsonify({'error': '服务点不存在'}), 404
        t.service_point_id = data['service_point_id']

    db.session.commit()
    return jsonify({'message': '班次模板更新成功'}), 200


@st_bp.route('/<int:t_id>', methods=['DELETE'])
@role_required('admin')
def delete(t_id):
    t = ShiftTemplate.query.get(t_id)
    if not t:
        return jsonify({'error': '班次模板不存在'}), 404

    db.session.delete(t)
    db.session.commit()
    return jsonify({'message': '班次模板删除成功'}), 200


@st_bp.route('/<int:t_id>/apply', methods=['POST'])
@role_required('admin')
def apply_template(t_id):
    t = ShiftTemplate.query.get(t_id)
    if not t:
        return jsonify({'error': '班次模板不存在'}), 404

    data = request.get_json()
    if not data or not data.get('user_id') or not data.get('date'):
        return jsonify({'error': '用户ID和日期为必填项'}), 400

    from models import DutyAssignment
    from conflict import detect_all_conflicts, log_conflicts

    conflicts = detect_all_conflicts(
        user_id=data['user_id'],
        service_point_id=t.service_point_id,
        date=data['date'],
        start_time=t.start_time,
        end_time=t.end_time,
        is_cross_day=t.is_cross_day
    )

    force = data.get('force', False)
    if conflicts and not force:
        return jsonify({'error': '存在排班冲突', 'conflicts': conflicts}), 409

    assignment = DutyAssignment(
        user_id=data['user_id'],
        service_point_id=t.service_point_id,
        date=data['date'],
        start_time=t.start_time,
        end_time=t.end_time,
        status='pending',
        is_cross_day=t.is_cross_day
    )
    db.session.add(assignment)
    db.session.commit()

    if conflicts:
        log_conflicts(conflicts, data['user_id'], t.service_point_id, data['date'], assignment.id)

    return jsonify({
        'message': '班次应用成功',
        'assignment_id': assignment.id,
        'conflicts': conflicts if conflicts else None
    }), 201
