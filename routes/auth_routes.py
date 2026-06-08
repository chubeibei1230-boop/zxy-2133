from flask import Blueprint, request, jsonify
from flask_bcrypt import Bcrypt
from models import db, User
from auth import generate_token, login_required

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')
bcrypt = Bcrypt()


@auth_bp.route('/register', methods=['POST'])
@login_required
def register():
    user = request.current_user
    if user.role != 'admin':
        return jsonify({'error': '仅管理员可注册新用户'}), 403

    data = request.get_json()
    if not data or not data.get('username') or not data.get('password') or not data.get('name'):
        return jsonify({'error': '用户名、密码和姓名为必填项'}), 400

    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': '用户名已存在'}), 409

    role = data.get('role', 'staff')
    if role not in User.valid_roles():
        return jsonify({'error': f'无效角色，可选: {",".join(User.valid_roles())}'}), 400

    new_user = User(
        username=data['username'],
        password_hash=bcrypt.generate_password_hash(data['password']).decode('utf-8'),
        name=data['name'],
        phone=data.get('phone'),
        role=role
    )
    db.session.add(new_user)
    db.session.commit()

    return jsonify({'message': '注册成功', 'user_id': new_user.id}), 201


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'error': '用户名和密码为必填项'}), 400

    user = User.query.filter_by(username=data['username']).first()
    if not user or not bcrypt.check_password_hash(user.password_hash, data['password']):
        return jsonify({'error': '用户名或密码错误'}), 401

    token = generate_token(user.id, user.role)
    return jsonify({
        'token': token,
        'user': {
            'id': user.id,
            'username': user.username,
            'name': user.name,
            'role': user.role
        }
    }), 200


@auth_bp.route('/me', methods=['GET'])
@login_required
def me():
    user = request.current_user
    return jsonify({
        'id': user.id,
        'username': user.username,
        'name': user.name,
        'role': user.role,
        'phone': user.phone
    }), 200


@auth_bp.route('/users', methods=['GET'])
@login_required
def list_users():
    users = User.query.all()
    return jsonify([{
        'id': u.id,
        'username': u.username,
        'name': u.name,
        'role': u.role,
        'phone': u.phone
    } for u in users]), 200
