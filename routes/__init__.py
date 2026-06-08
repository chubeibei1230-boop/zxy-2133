from .auth_routes import auth_bp
from .service_point_routes import sp_bp
from .shift_template_routes import st_bp
from .duty_assignment_routes import da_bp
from .substitution_routes import sub_bp
from .attendance_routes import att_bp
from .conflict_routes import conflict_bp
from .schedule_notification_routes import sn_bp
from .leave_routes import leave_bp
from .deactivation_routes import deact_bp

all_blueprints = [auth_bp, sp_bp, st_bp, da_bp, sub_bp, att_bp, conflict_bp, sn_bp, leave_bp, deact_bp]
