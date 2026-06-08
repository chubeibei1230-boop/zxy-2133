from flask import Flask, jsonify
from flask_bcrypt import Bcrypt
from models import db, User, ServicePoint, ShiftTemplate
from routes import all_blueprints
import config


def create_app():
    app = Flask(__name__)
    app.config.from_object(config)

    db.init_app(app)
    Bcrypt(app)

    for bp in all_blueprints:
        app.register_blueprint(bp)

    @app.route('/api/health', methods=['GET'])
    def health():
        return jsonify({'status': 'ok', 'service': 'community-duty-management'}), 200

    with app.app_context():
        db.create_all()
        _seed_initial_data(app)

    return app


def _seed_initial_data(app):
    with app.app_context():
        if User.query.first():
            return

        from flask_bcrypt import Bcrypt
        bcrypt = Bcrypt(app)

        admin = User(
            username='admin',
            password_hash=bcrypt.generate_password_hash('admin123').decode('utf-8'),
            name='系统管理员',
            role='admin',
            phone='13800000001'
        )
        staff1 = User(
            username='staff1',
            password_hash=bcrypt.generate_password_hash('staff123').decode('utf-8'),
            name='张三',
            role='staff',
            phone='13800000002'
        )
        staff2 = User(
            username='staff2',
            password_hash=bcrypt.generate_password_hash('staff123').decode('utf-8'),
            name='李四',
            role='staff',
            phone='13800000003'
        )
        staff3 = User(
            username='staff3',
            password_hash=bcrypt.generate_password_hash('staff123').decode('utf-8'),
            name='王五',
            role='staff',
            phone='13800000004'
        )
        inspector = User(
            username='inspector1',
            password_hash=bcrypt.generate_password_hash('insp123').decode('utf-8'),
            name='赵质检',
            role='inspector',
            phone='13800000005'
        )
        db.session.add_all([admin, staff1, staff2, staff3, inspector])

        sp1 = ServicePoint(name='社区服务中心A', address='幸福路100号', max_persons=2, description='主服务大厅')
        sp2 = ServicePoint(name='社区服务中心B', address='和平路200号', max_persons=1, description='分服务点')
        sp3 = ServicePoint(name='社区服务站C', address='建设路300号', max_persons=3, description='便民服务站')
        db.session.add_all([sp1, sp2, sp3])

        db.session.commit()

        t1 = ShiftTemplate(name='早班', start_time='08:00', end_time='12:00', is_cross_day=False, service_point_id=sp1.id)
        t2 = ShiftTemplate(name='午班', start_time='12:00', end_time='18:00', is_cross_day=False, service_point_id=sp1.id)
        t3 = ShiftTemplate(name='晚班', start_time='18:00', end_time='22:00', is_cross_day=False, service_point_id=sp1.id)
        t4 = ShiftTemplate(name='夜班', start_time='22:00', end_time='06:00', is_cross_day=True, service_point_id=sp2.id)
        t5 = ShiftTemplate(name='全天班', start_time='09:00', end_time='17:00', is_cross_day=False, service_point_id=sp3.id)
        db.session.add_all([t1, t2, t3, t4, t5])
        db.session.commit()


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8113, debug=True)
