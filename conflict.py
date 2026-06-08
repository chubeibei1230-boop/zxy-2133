from models import DutyAssignment, Substitution, ServicePoint, ConflictLog, db
from datetime import datetime


def time_to_minutes(time_str):
    parts = time_str.split(':')
    return int(parts[0]) * 60 + int(parts[1])


def intervals_overlap(start1, end1, start2, end2, cross1=False, cross2=False):
    s1 = time_to_minutes(start1)
    e1 = time_to_minutes(end1)
    s2 = time_to_minutes(start2)
    e2 = time_to_minutes(end2)

    if cross1:
        e1 += 24 * 60
    if cross2:
        e2 += 24 * 60

    if not cross1 and e1 < s1:
        e1 += 24 * 60
    if not cross2 and e2 < s2:
        e2 += 24 * 60

    return s1 < e2 and s2 < e1


def get_effective_assignments(date, user_id=None, service_point_id=None, exclude_id=None):
    query = DutyAssignment.query.filter(
        DutyAssignment.date == date,
        DutyAssignment.status != 'cancelled'
    )
    if user_id:
        query = query.filter(DutyAssignment.user_id == user_id)
    if service_point_id:
        query = query.filter(DutyAssignment.service_point_id == service_point_id)
    if exclude_id:
        query = query.filter(DutyAssignment.id != exclude_id)
    return query.all()


def get_substitute_user_id(assignment_id):
    sub = Substitution.query.filter(
        Substitution.duty_assignment_id == assignment_id,
        Substitution.status == 'approved'
    ).first()
    return sub.substitute_user_id if sub else None


def check_cross_point_conflict(user_id, date, start_time, end_time, is_cross_day, exclude_id=None):
    conflicts = []
    own_assignments = get_effective_assignments(date, user_id=user_id, exclude_id=exclude_id)

    for a in own_assignments:
        effective_user_id = user_id
        sub_user_id = get_substitute_user_id(a.id)
        if sub_user_id:
            effective_user_id = sub_user_id

        if effective_user_id != user_id:
            continue

        if intervals_overlap(start_time, end_time, a.start_time, a.end_time,
                             is_cross_day, a.is_cross_day):
            conflicts.append({
                'type': 'cross_point',
                'conflicting_assignment_id': a.id,
                'service_point_id': a.service_point_id,
                'description': (
                    f'人员ID={user_id} 在日期{date}已有服务点ID={a.service_point_id}的排班 '
                    f'({a.start_time}-{a.end_time})，与新增时段({start_time}-{end_time})冲突'
                )
            })

    return conflicts


def check_over_limit_conflict(service_point_id, date, start_time, end_time, is_cross_day, exclude_id=None):
    conflicts = []
    sp = ServicePoint.query.get(service_point_id)
    if not sp:
        return conflicts

    assignments = get_effective_assignments(date, service_point_id=service_point_id, exclude_id=exclude_id)

    overlapping = []
    for a in assignments:
        if intervals_overlap(start_time, end_time, a.start_time, a.end_time,
                             is_cross_day, a.is_cross_day):
            overlapping.append(a)

    current_count = len(overlapping)
    if current_count >= sp.max_persons:
        conflicts.append({
            'type': 'over_limit',
            'service_point_id': service_point_id,
            'current_count': current_count,
            'max_persons': sp.max_persons,
            'description': (
                f'服务点"{sp.name}"(ID={service_point_id})在{date} {start_time}-{end_time}时段 '
                f'已有{current_count}人排班，超出上限{sp.max_persons}人'
            )
        })

    return conflicts


def check_substitution_overlap(substitute_user_id, date, start_time, end_time, is_cross_day, exclude_id=None):
    conflicts = []
    existing = get_effective_assignments(date, user_id=substitute_user_id, exclude_id=exclude_id)

    for a in existing:
        sub_for_a = get_substitute_user_id(a.id)
        effective_user = sub_for_a if sub_for_a else a.user_id

        if effective_user != substitute_user_id:
            continue

        if intervals_overlap(start_time, end_time, a.start_time, a.end_time,
                             is_cross_day, a.is_cross_day):
            conflicts.append({
                'type': 'substitution_overlap',
                'conflicting_assignment_id': a.id,
                'original_user_id': a.user_id,
                'description': (
                    f'替班人员ID={substitute_user_id}在日期{date}已有排班 '
                    f'(服务点ID={a.service_point_id}, {a.start_time}-{a.end_time})，'
                    f'与替班时段({start_time}-{end_time})冲突'
                )
            })

    return conflicts


def detect_all_conflicts(user_id, service_point_id, date, start_time, end_time,
                         is_cross_day=False, exclude_id=None,
                         is_substitution=False, substitute_user_id=None):
    all_conflicts = []

    cross_point = check_cross_point_conflict(user_id, date, start_time, end_time,
                                             is_cross_day, exclude_id)
    all_conflicts.extend(cross_point)

    over_limit = check_over_limit_conflict(service_point_id, date, start_time, end_time,
                                           is_cross_day, exclude_id)
    all_conflicts.extend(over_limit)

    if is_substitution and substitute_user_id:
        sub_overlap = check_substitution_overlap(substitute_user_id, date, start_time, end_time,
                                                 is_cross_day, exclude_id)
        all_conflicts.extend(sub_overlap)

    return all_conflicts


def log_conflicts(conflicts, user_id, service_point_id, date, assignment_id=None):
    for c in conflicts:
        log = ConflictLog(
            duty_assignment_id=assignment_id,
            user_id=user_id,
            service_point_id=service_point_id,
            date=date,
            conflict_type=c['type'],
            description=c.get('description', '')
        )
        db.session.add(log)
    db.session.commit()
