from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    phone = db.Column(db.String(20))
    role = db.Column(db.String(20), nullable=False, default='staff')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assignments = db.relationship('DutyAssignment', backref='user', lazy='dynamic')
    original_substitutions = db.relationship(
        'Substitution', foreign_keys='Substitution.original_user_id', backref='original_user', lazy='dynamic'
    )
    substitute_substitutions = db.relationship(
        'Substitution', foreign_keys='Substitution.substitute_user_id', backref='substitute_user', lazy='dynamic'
    )
    inspections = db.relationship('AttendanceRecord', backref='inspector', lazy='dynamic')

    @staticmethod
    def valid_roles():
        return ['admin', 'staff', 'inspector']


class ServicePoint(db.Model):
    __tablename__ = 'service_points'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(255))
    max_persons = db.Column(db.Integer, nullable=False, default=1)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assignments = db.relationship('DutyAssignment', backref='service_point', lazy='dynamic')
    templates = db.relationship('ShiftTemplate', backref='service_point', lazy='dynamic')


class ShiftTemplate(db.Model):
    __tablename__ = 'shift_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    is_cross_day = db.Column(db.Boolean, default=False)
    service_point_id = db.Column(db.Integer, db.ForeignKey('service_points.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def validate_time_format(time_str):
        try:
            parts = time_str.split(':')
            if len(parts) != 2:
                return False
            h, m = int(parts[0]), int(parts[1])
            return 0 <= h <= 23 and 0 <= m <= 59
        except (ValueError, IndexError):
            return False


class DutyAssignment(db.Model):
    __tablename__ = 'duty_assignments'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    service_point_id = db.Column(db.Integer, db.ForeignKey('service_points.id'), nullable=False)
    date = db.Column(db.String(10), nullable=False)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='pending')
    is_cross_day = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    substitutions = db.relationship('Substitution', backref='assignment', lazy='dynamic')
    attendance = db.relationship('AttendanceRecord', backref='assignment', lazy='joined', uselist=False)

    @staticmethod
    def valid_statuses():
        return ['pending', 'confirmed', 'cancelled']


class Substitution(db.Model):
    __tablename__ = 'substitutions'

    id = db.Column(db.Integer, primary_key=True)
    original_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    substitute_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    duty_assignment_id = db.Column(db.Integer, db.ForeignKey('duty_assignments.id'), nullable=False)
    reason = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)

    @staticmethod
    def valid_statuses():
        return ['pending', 'approved', 'rejected']


class AttendanceRecord(db.Model):
    __tablename__ = 'attendance_records'

    id = db.Column(db.Integer, primary_key=True)
    duty_assignment_id = db.Column(db.Integer, db.ForeignKey('duty_assignments.id'), nullable=False, unique=True)
    check_in_time = db.Column(db.DateTime)
    status = db.Column(db.String(20), nullable=False, default='absent')
    inspector_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    inspector_comment = db.Column(db.Text)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def valid_statuses():
        return ['on_time', 'late', 'absent']


class ConflictLog(db.Model):
    __tablename__ = 'conflict_logs'

    id = db.Column(db.Integer, primary_key=True)
    duty_assignment_id = db.Column(db.Integer, db.ForeignKey('duty_assignments.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    service_point_id = db.Column(db.Integer, db.ForeignKey('service_points.id'), nullable=True)
    date = db.Column(db.String(10), nullable=False)
    conflict_type = db.Column(db.String(30), nullable=False)
    description = db.Column(db.Text)
    detected_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='conflict_logs')
    service_point = db.relationship('ServicePoint', backref='conflict_logs')

    @staticmethod
    def valid_conflict_types():
        return ['cross_point', 'over_limit', 'substitution_overlap']
