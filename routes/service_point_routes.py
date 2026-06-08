from flask import Blueprint, request, jsonify
from models import db, ServicePoint
from auth import login_required, role_required

sp_bp = Blueprint('service_points', __name__, url_prefix='/api/service-points')


@sp_bp.route('', methods=['POST'])
@role_required('admin')
def create():
    data = request.get_json()
    if not data or not data.get('name'):
        return jsonify({'error': '服务点名称为必填项'}), 400

    sp = ServicePoint(
        name=data['name'],
        address=data.get('address'),
        max_persons=data.get('max_persons', 1),
        description=data.get('description')
    )
    db.session.add(sp)
    db.session.commit()
    return jsonify({'message': '服务点创建成功', 'id': sp.id}), 201


@sp_bp.route('', methods=['GET'])
@login_required
def list_all():
    query = ServicePoint.query
    name = request.args.get('name')
    if name:
        query = query.filter(ServicePoint.name.contains(name))
    points = query.all()
    return jsonify([{
        'id': p.id,
        'name': p.name,
        'address': p.address,
        'max_persons': p.max_persons,
        'description': p.description
    } for p in points]), 200


@sp_bp.route('/<int:sp_id>', methods=['GET'])
@login_required
def get_one(sp_id):
    sp = ServicePoint.query.get(sp_id)
    if not sp:
        return jsonify({'error': '服务点不存在'}), 404
    return jsonify({
        'id': sp.id,
        'name': sp.name,
        'address': sp.address,
        'max_persons': sp.max_persons,
        'description': sp.description
    }), 200


@sp_bp.route('/<int:sp_id>', methods=['PUT'])
@role_required('admin')
def update(sp_id):
    sp = ServicePoint.query.get(sp_id)
    if not sp:
        return jsonify({'error': '服务点不存在'}), 404

    data = request.get_json()
    if data.get('name'):
        sp.name = data['name']
    if 'address' in data:
        sp.address = data['address']
    if 'max_persons' in data:
        sp.max_persons = data['max_persons']
    if 'description' in data:
        sp.description = data['description']

    db.session.commit()
    return jsonify({'message': '服务点更新成功'}), 200


@sp_bp.route('/<int:sp_id>', methods=['DELETE'])
@role_required('admin')
def delete(sp_id):
    sp = ServicePoint.query.get(sp_id)
    if not sp:
        return jsonify({'error': '服务点不存在'}), 404

    db.session.delete(sp)
    db.session.commit()
    return jsonify({'message': '服务点删除成功'}), 200
