import datetime

from django.template import RequestContext
from django.shortcuts import render_to_response

from sqlalchemy import func

import pyfaf
from pyfaf.storage import (Report,
                           ReportOpSysRelease,
                           ReportHistoryDaily,
                           ReportHistoryWeekly,
                           ReportHistoryMonthly,
                           OpSysComponent)
from pyfaf.hub.common.forms import DurationOsComponentFilterForm

def months_ago(count):
    day = datetime.date.today()
    day = day.replace(day=1)
    month = day.month - count
    if month > 0:
        day = day.replace(month=month)
    else:
        day = day.replace(year=day.year - 1, month = 12 + month)
    return day

def index(request, *args, **kwargs):
    db = pyfaf.storage.getDatabase()
    params = dict(request.REQUEST)
    params.update(kwargs)
    form = DurationOsComponentFilterForm(db, params)

    #pylint:disable=E1101
    # Instance of 'Database' has no 'ReportHistoryDaily' member (but
    # some types could not be inferred).
    duration_opt = form.get_duration_selection()
    component_ids = form.get_component_selection()

    reports = ((name, release_incremental_history(db, ids, component_ids,
        duration_opt)) for ids, name in form.get_release_selection())

    return render_to_response("summary/index.html",
                              { "reports": reports,
                                "form": form,
                                "duration": duration_opt },
                              context_instance=RequestContext(request))

def release_incremental_history(db, osrelease_ids, component_ids, duration):
    if duration == 'd':
        hist_table = ReportHistoryDaily
        history_query = (db.session.query(ReportHistoryDaily.day,
            func.sum(ReportHistoryDaily.count)).
            filter(ReportHistoryDaily.day > datetime.date.today() -
                datetime.timedelta(days=15)).
            group_by(ReportHistoryDaily.day).
            order_by(ReportHistoryDaily.day))
    elif duration == 'w':
        hist_table = ReportHistoryWeekly
        history_query = (db.session.query(ReportHistoryWeekly.week,
            func.sum(ReportHistoryWeekly.count)).
            filter(ReportHistoryWeekly.week > datetime.date.today() -
                datetime.timedelta(weeks=9)).
            group_by(ReportHistoryWeekly.week).
            order_by(ReportHistoryWeekly.week))
    else:
        hist_table = ReportHistoryMonthly
        # duration == 'm'
        history_query = (db.session.query(ReportHistoryMonthly.month,
            func.sum(ReportHistoryMonthly.count)).
            filter(ReportHistoryMonthly.month >= months_ago(12)).
            group_by(ReportHistoryMonthly.month).
            order_by(ReportHistoryMonthly.month))

    if osrelease_ids:
        #FIXME : correct selection of OS release !!
        #Missing table RepostOpSysReleaseHistory(Daily|Weekly|Monthly)
        history_query = (history_query.join(ReportOpSysRelease,
                    ReportOpSysRelease.report_id==hist_table.report_id).
            filter(ReportOpSysRelease.opsysrelease_id.in_(osrelease_ids)))

    if component_ids:
        # Selected Component
        history_query = (history_query.join(Report, OpSysComponent).
            filter(OpSysComponent.id.in_(component_ids)))

    history_dict = dict(history_query.all())

    if duration == 'd':
        for i in range(0, 14):
            day = datetime.date.today() - datetime.timedelta(days=i)
            if day not in history_dict:
                history_dict[day] = 0
    elif duration == 'w':
        for i in range(0, 8):
            day = datetime.date.today()
            day -= (datetime.timedelta(days=day.weekday()) +
                datetime.timedelta(weeks=i))
            if day not in history_dict:
                history_dict[day] = 0
    else:
        # duration == 'm'
        for i in range(0, 12):
            day = months_ago(i)
            if day not in history_dict:
                history_dict[day] = 0

    return sorted(history_dict.items(), key=lambda x: x[0])