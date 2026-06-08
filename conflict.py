from models import DutyAssignment, Substitution, ServicePoint, ConflictLog, ServicePointDeactivation, db
from datetime import datetime, timedelta


def time_to_minutes(time_str):
    parts = time_str.split(':')
    return int(parts[0]) * 60 + int(parts[1])


def _previous_date(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=1)
    return dt.strftime('%Y-%m-%d')


def _next_date(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)
    return dt.strftime('%Y-%m-%d')


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


def _query_assignments(date, user_id=None, service_point_id=None, exclude_id=None):
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


def get_effective_assignments(date, user_id=None, service_point_id=None, exclude_id=None):
    same_day = _query_assignments(date, user_id=user_id,
                                  service_point_id=service_point_id, exclude_id=exclude_id)
    prev_day_cross = _query_assignments(
        _previous_date(date), user_id=user_id,
        service_point_id=service_point_id, exclude_id=exclude_id
    )
    prev_day_cross = [a for a in prev_day_cross if a.is_cross_day]
    return same_day + prev_day_cross


def get_next_day_assignments(date, user_id=None, service_point_id=None, exclude_id=None):
    return _query_assignments(
        _next_date(date), user_id=user_id,
        service_point_id=service_point_id, exclude_id=exclude_id
    )


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
        sub_user_id = get_substitute_user_id(a.id)
        effective_user_id = sub_user_id if sub_user_id else a.user_id
        if effective_user_id != user_id:
            continue

        a_is_cross = a.is_cross_day
        a_start = a.start_time
        a_end = a.end_time
        if a.date < date:
            a_start_minutes = time_to_minutes(a.start_time)
            if not a_is_cross and time_to_minutes(a.end_time) < a_start_minutes:
                a_is_cross = True
            a_start = '00:00'
            a_is_cross = False

        new_cross = is_cross_day
        new_start = start_time
        new_end = end_time
        if is_cross_day:
            if intervals_overlap(new_start, '23:59', a_start, a_end, False, a_is_cross):
                conflicts.append({
                    'type': 'cross_point',
                    'conflicting_assignment_id': a.id,
                    'service_point_id': a.service_point_id,
                    'description': (
                        f'人员ID={user_id} 在日期{a.date}已有服务点ID={a.service_point_id}的排班 '
                        f'({a.start_time}-{a.end_time})，与新增时段({start_time}-{end_time})冲突'
                    )
                })
            next_day_assignments = get_next_day_assignments(date, user_id=user_id, exclude_id=exclude_id)
            for nd in next_day_assignments:
                nd_sub = get_substitute_user_id(nd.id)
                nd_effective = nd_sub if nd_sub else nd.user_id
                if nd_effective != user_id:
                    continue
                if intervals_overlap('00:00', new_end, nd.start_time, nd.end_time, False, nd.is_cross_day):
                    conflicts.append({
                        'type': 'cross_point',
                        'conflicting_assignment_id': nd.id,
                        'service_point_id': nd.service_point_id,
                        'description': (
                            f'人员ID={user_id} 在次日{nd.date}已有服务点ID={nd.service_point_id}的排班 '
                            f'({nd.start_time}-{nd.end_time})，与跨日排班({start_time}-{end_time})次日部分冲突'
                        )
                    })
            continue

        if intervals_overlap(new_start, new_end, a_start, a_end, new_cross, a_is_cross):
            conflicts.append({
                'type': 'cross_point',
                'conflicting_assignment_id': a.id,
                'service_point_id': a.service_point_id,
                'description': (
                    f'人员ID={user_id} 在日期{a.date}已有服务点ID={a.service_point_id}的排班 '
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
        a_is_cross = a.is_cross_day
        a_start = a.start_time
        a_end = a.end_time
        if a.date < date:
            if not a_is_cross and time_to_minutes(a.end_time) < time_to_minutes(a.start_time):
                a_is_cross = True
            a_start = '00:00'
            a_is_cross = False

        if is_cross_day:
            day_part_overlap = intervals_overlap(start_time, '23:59', a_start, a_end, False, a_is_cross)
            next_day_sp = get_next_day_assignments(date, service_point_id=service_point_id, exclude_id=exclude_id)
            next_part_overlap = False
            for nd in next_day_sp:
                nd_cross = nd.is_cross_day
                nd_start = nd.start_time
                if nd.date > date and not nd_cross and time_to_minutes(nd.end_time) < time_to_minutes(nd.start_time):
                    nd_cross = True
                if intervals_overlap('00:00', end_time, nd_start, nd.end_time, False, nd_cross):
                    next_part_overlap = True
                    break
            if day_part_overlap or next_part_overlap:
                overlapping.append(a)
        else:
            if intervals_overlap(start_time, end_time, a_start, a_end, is_cross_day, a_is_cross):
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

    own_assignments = get_effective_assignments(date, user_id=substitute_user_id, exclude_id=exclude_id)
    for a in own_assignments:
        sub_for_a = get_substitute_user_id(a.id)
        effective_user = sub_for_a if sub_for_a else a.user_id
        if effective_user != substitute_user_id:
            continue

        a_is_cross = a.is_cross_day
        a_start = a.start_time
        a_end = a.end_time
        if a.date < date:
            if not a_is_cross and time_to_minutes(a.end_time) < time_to_minutes(a.start_time):
                a_is_cross = True
            a_start = '00:00'
            a_is_cross = False

        if intervals_overlap(start_time, end_time, a_start, a_end, is_cross_day, a_is_cross):
            conflicts.append({
                'type': 'substitution_overlap',
                'conflicting_assignment_id': a.id,
                'original_user_id': a.user_id,
                'description': (
                    f'替班人员ID={substitute_user_id}在日期{a.date}已有排班 '
                    f'(服务点ID={a.service_point_id}, {a.start_time}-{a.end_time})，'
                    f'与替班时段({start_time}-{end_time})冲突'
                )
            })

    approved_subs = Substitution.query.filter(
        Substitution.substitute_user_id == substitute_user_id,
        Substitution.status == 'approved'
    ).all()
    sub_assignment_ids = [s.duty_assignment_id for s in approved_subs]
    if exclude_id and exclude_id in sub_assignment_ids:
        sub_assignment_ids = [sid for sid in sub_assignment_ids if sid != exclude_id]

    if sub_assignment_ids:
        sub_assignments = DutyAssignment.query.filter(
            DutyAssignment.id.in_(sub_assignment_ids),
            DutyAssignment.status != 'cancelled'
        ).all()
        for a in sub_assignments:
            if a.user_id == substitute_user_id:
                continue

            a_is_cross = a.is_cross_day
            a_start = a.start_time
            a_end = a.end_time
            overlap_date = a.date
            if a.date < date:
                if not a_is_cross and time_to_minutes(a.end_time) < time_to_minutes(a.start_time):
                    a_is_cross = True
                a_start = '00:00'
                a_is_cross = False
            elif a.date == date:
                pass
            else:
                continue

            if is_cross_day:
                day_overlap = intervals_overlap(start_time, '23:59', a_start, a_end, False, a_is_cross)
                if day_overlap:
                    conflicts.append({
                        'type': 'substitution_overlap',
                        'conflicting_assignment_id': a.id,
                        'original_user_id': a.user_id,
                        'description': (
                            f'替班人员ID={substitute_user_id}已在日期{overlap_date}替ID={a.user_id}值班 '
                            f'(服务点ID={a.service_point_id}, {a.start_time}-{a.end_time})，'
                            f'与新增替班时段({start_time}-{end_time})冲突'
                        )
                    })
            else:
                if intervals_overlap(start_time, end_time, a_start, a_end, is_cross_day, a_is_cross):
                    conflicts.append({
                        'type': 'substitution_overlap',
                        'conflicting_assignment_id': a.id,
                        'original_user_id': a.user_id,
                        'description': (
                            f'替班人员ID={substitute_user_id}已在日期{overlap_date}替ID={a.user_id}值班 '
                            f'(服务点ID={a.service_point_id}, {a.start_time}-{a.end_time})，'
                            f'与新增替班时段({start_time}-{end_time})冲突'
                        )
                    })

    pending_subs = Substitution.query.filter(
        Substitution.substitute_user_id == substitute_user_id,
        Substitution.status == 'pending'
    ).all()
    for ps in pending_subs:
        pa = DutyAssignment.query.get(ps.duty_assignment_id)
        if not pa or pa.status == 'cancelled':
            continue
        if pa.user_id == substitute_user_id:
            continue

        pa_is_cross = pa.is_cross_day
        pa_start = pa.start_time
        pa_end = pa.end_time
        if pa.date < date:
            if not pa_is_cross and time_to_minutes(pa.end_time) < time_to_minutes(pa.start_time):
                pa_is_cross = True
            pa_start = '00:00'
            pa_is_cross = False
        elif pa.date != date:
            continue

        if intervals_overlap(start_time, end_time, pa_start, pa_end, is_cross_day, pa_is_cross):
            conflicts.append({
                'type': 'substitution_overlap',
                'conflicting_assignment_id': pa.id,
                'original_user_id': pa.user_id,
                'description': (
                    f'替班人员ID={substitute_user_id}已有待审批替班(替ID={pa.user_id}，'
                    f'服务点ID={pa.service_point_id}, {pa.start_time}-{pa.end_time})，'
                    f'与新增替班时段({start_time}-{end_time})可能冲突'
                )
            })

    return conflicts


def check_deactivation_conflict(service_point_id, date, start_time=None, end_time=None, is_cross_day=False):
    conflicts = []
    active_deactivations = ServicePointDeactivation.query.filter(
        ServicePointDeactivation.service_point_id == service_point_id,
        ServicePointDeactivation.status == 'active',
        ServicePointDeactivation.start_date <= date,
        ServicePointDeactivation.end_date >= date
    ).all()
    for active_deact in active_deactivations:
        if start_time and end_time:
            if not _shift_deactivation_overlaps(date, start_time, end_time, is_cross_day, active_deact):
                continue
        sp = ServicePoint.query.get(service_point_id)
        conflicts.append({
            'type': 'deactivation',
            'deactivation_id': active_deact.id,
            'service_point_id': service_point_id,
            'description': (
                f'服务点"{sp.name}"(ID={service_point_id})在{date}处于停用状态 '
                f'(停用ID={active_deact.id}，{active_deact.start_date} {active_deact.start_time}至'
                f'{active_deact.end_date} {active_deact.end_time}，'
                f'原因：{active_deact.reason})'
            )
        })
    return conflicts


def _shift_deactivation_overlaps(shift_date, shift_start, shift_end, shift_cross, deactivation):
    if deactivation.end_date < shift_date or deactivation.start_date > shift_date:
        return False
    if deactivation.start_date == shift_date and deactivation.end_date == shift_date:
        d_start = time_to_minutes(deactivation.start_time)
        d_end = time_to_minutes(deactivation.end_time)
        s_start = time_to_minutes(shift_start)
        s_end = time_to_minutes(shift_end)
        if shift_cross:
            return s_start < d_end + 24 * 60 and d_start < s_end + 24 * 60
        if s_end < s_start:
            s_end += 24 * 60
        if d_end < d_start:
            d_end += 24 * 60
        return s_start < d_end and d_start < s_end
    if deactivation.start_date < shift_date:
        if shift_cross:
            return True
        s_start = time_to_minutes(shift_start)
        return s_start < time_to_minutes(deactivation.end_time)
    if deactivation.end_date > shift_date:
        if shift_cross:
            return True
        s_end = time_to_minutes(shift_end)
        if s_end < time_to_minutes(shift_start):
            s_end += 24 * 60
        return time_to_minutes(deactivation.start_time) < s_end
    return True


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

    deactivation = check_deactivation_conflict(service_point_id, date, start_time, end_time, is_cross_day)
    all_conflicts.extend(deactivation)

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
