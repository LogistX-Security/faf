# Copyright (C) 2013  ABRT Team
# Copyright (C) 2013  Red Hat, Inc.
#
# This file is part of faf.
#
# faf is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# faf is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with faf.  If not, see <http://www.gnu.org/licenses/>.

from string import ascii_uppercase #pylint: disable=deprecated-module
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Union

from sqlalchemy.orm import backref, relationship, synonym
from sqlalchemy.sql.schema import Column, ForeignKey, UniqueConstraint, Index
from sqlalchemy.types import Boolean, Date, DateTime, Enum, Integer, String

from pyfaf.utils.storage import format_reason, most_common_crash_function
from pyfaf.utils.parse import signal2name

from .bugzilla import BzBug
from .externalfaf import ExternalFafInstance
from .custom_types import Semver
from .generic_table import GenericTable
from .mantisbt import MantisBug
from .opsys import Arch, OpSysComponent, OpSysRelease, Package
from .problem import Problem
from .symbol import SymbolSource
from .user import User


class Report(GenericTable):
    __tablename__ = "reports"
    __lobs__ = {"oops": 1 << 16}

    id = Column(Integer, primary_key=True)
    type = Column(String(64), nullable=False, index=True)
    first_occurrence = Column(DateTime)
    last_occurrence = Column(DateTime)
    # Watch out, there's a "set" event handler on count that can send out fedmsg
    # notifications.
    count = Column(Integer, nullable=False)
    errname = Column(String(256), nullable=True)
    component_id = Column(Integer, ForeignKey("{0}.id".format(OpSysComponent.__tablename__)),
                          nullable=False, index=True)
    problem_id = Column(Integer, ForeignKey("{0}.id".format(Problem.__tablename__)), nullable=True, index=True)
    component = relationship(OpSysComponent)
    problem = relationship(Problem, backref="reports")
    max_certainty = Column(Integer, nullable=True)

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "bugs": [bug.url for bug in self.bugs],
            "component": self.component,
            "count": self.count,
            "first_occurrence": self.first_occurrence,
            "last_occurrence": self.last_occurrence,
            "problem_id": self.problem_id,
            "comments": self.comments,
        }

    @property
    def bugs(self) -> List[BzBug]:
        # must be imported here to avoid dependency circle
        from pyfaf.bugtrackers import report_backref_names # pylint: disable=cyclic-import
        my_bugs = []

        for br in report_backref_names:
            for reportbug in getattr(self, br):
                my_bugs.append(reportbug.bug)

        return my_bugs

    @property
    def oops(self) -> Optional[str]:
        result = self.get_lob('oops')
        if result:
            return result.decode('utf-8')
        return result

    @property
    def sorted_backtraces(self) -> List[Problem]:
        '''
        List of all backtraces assigned to this report
        sorted by quality.
        '''
        return sorted(self.backtraces, key=lambda bt: bt.quality, reverse=True)

    @property
    def quality(self) -> int:
        '''
        Return quality metric for this report
        which equals to the quality of its best backtrace.
        '''

        bts = self.sorted_backtraces
        if not bts:
            return -1000

        return self.sorted_backtraces[0].quality

    @property
    def tainted(self) -> bool:
        if self.type.lower() != "kerneloops":
            return False

        return all(bt.tainted for bt in self.backtraces)

    @property
    def crash_function(self) -> str:
        """
        Return the most common crash function among all backtraces of this
        report
        """

        return most_common_crash_function(self.backtraces)

    @property
    def error_name(self) -> Optional[str]:
        if self.type == "core":
            return signal2name(self.errname, with_number=True)
        if self.type == "python":
            if self.errname and (self.errname[0] in ascii_uppercase
                                 or "." in self.errname):
                # A lot of python reports contain "reason" or "error" as errname
                # so we only show the ones beginning with an uppercase letter or
                # containing a "." (lowercase module.Exception)
                return self.errname

            return None
        return self.errname

    @property
    def archived(self) -> bool:
        if self.archive and self.archive.active:
            return True

        return False


class ReportHash(GenericTable):
    __tablename__ = "reporthashes"

    hash = Column(String(64), nullable=False, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       nullable=False, index=True, primary_key=True)

    report = relationship(Report, backref="hashes")


class ReportBacktrace(GenericTable):
    __tablename__ = "reportbacktraces"

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       nullable=False, index=True)
    report = relationship(Report, backref="backtraces")
    crashfn = Column(String(1024), nullable=True)
    quality = Column(Integer, nullable=False)

    @property
    def crash_function(self) -> str:
        if self.crashfn:
            return self.crashfn

        return 'unknown function'

    @property
    def frames(self) -> List[Any]:
        # there should always be exactly one crashthread
        # but the DB schema allows multiple or none, so let's
        # be ready for such case

        crashthreads = [t for t in self.threads if t.crashthread]

        if not crashthreads:
            return []

        return crashthreads[0].frames

    def compute_quality(self) -> int:
        '''
        Compute backtrace quality (0=high quality, -100=lowest)

        Frames with missing information lower the backtrace quality.
        '''
        quality = -len(self.taint_flags)

        # empty backtrace
        if not self.frames:
            quality -= 100

        for frame in self.frames:
            if not frame.symbolsource.symbol:
                quality -= 1
            elif frame.symbolsource.symbol.name == '??':
                quality -= 1

            if not frame.symbolsource.source_path:
                quality -= 1

            if not frame.symbolsource.line_number:
                quality -= 1

            if not frame.reliable:
                quality -= 1

        return quality

    @property
    def tainted(self) -> bool:
        return any(flag.taintflag.character.upper() != 'G'
                   for flag in self.taint_flags)


class ReportBtThread(GenericTable):
    __tablename__ = "reportbtthreads"

    id = Column(Integer, primary_key=True)
    backtrace_id = Column(Integer, ForeignKey("{0}.id".format(ReportBacktrace.__tablename__), ondelete="CASCADE"),
                          nullable=False, index=True)
    number = Column(Integer, nullable=True)
    crashthread = Column(Boolean, nullable=False)

    backtrace = relationship(ReportBacktrace, backref=backref("threads", order_by="ReportBtThread.number",
                                                              passive_deletes=True))


class ReportBtFrame(GenericTable):
    __tablename__ = "reportbtframes"

    thread_id = Column(Integer, ForeignKey("{0}.id".format(ReportBtThread.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    order = Column(Integer, nullable=False, primary_key=True)
    symbolsource_id = Column(Integer, ForeignKey("{0}.id".format(SymbolSource.__tablename__)),
                             nullable=False, index=True)
    inlined = Column(Boolean, nullable=False, default=False)
    reliable = Column(Boolean, nullable=False, default=True)
    thread = relationship(ReportBtThread, backref=backref('frames', order_by="ReportBtFrame.order",
                                                          passive_deletes=True))
    symbolsource = relationship(SymbolSource, backref=backref('frames'))

    def to_json(self) -> Dict[str, Union[int, str]]:
        name = " "

        if self.symbolsource.symbol is not None:
            if self.symbolsource.symbol.nice_name:
                name = self.symbolsource.symbol.nice_name
            else:
                name = self.symbolsource.symbol.name

        return {
            "frame": self.order,
            "name": name,
            "binary_path": self.symbolsource.path,
            "source_path": self.symbolsource.source_path,
            "line_numer": self.symbolsource.line_number,
        }


class ReportBtHash(GenericTable):
    __tablename__ = "reportbthashes"

    type = Column(Enum("NAMES", "HASHES", name="reportbt_hashtype"), nullable=False, primary_key=True)
    hash = Column(String(64), nullable=False, primary_key=True)
    backtrace_id = Column(Integer, ForeignKey("{0}.id".format(ReportBacktrace.__tablename__), ondelete="CASCADE"),
                          nullable=False, index=True, primary_key=True)
    backtrace = relationship(ReportBacktrace, backref="hashes")

    def __str__(self) -> str:
        return self.hash


class ReportOpSysRelease(GenericTable):
    __tablename__ = "reportopsysreleases"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    opsysrelease_id = Column(Integer, ForeignKey("{0}.id".format(OpSysRelease.__tablename__)), primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="opsysreleases")
    opsysrelease = relationship(OpSysRelease)

    def __str__(self) -> str:
        return str(self.opsysrelease)


class ReportArch(GenericTable):
    __tablename__ = "reportarchs"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    arch_id = Column(Integer, ForeignKey("{0}.id".format(Arch.__tablename__)), nullable=False, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="arches")
    arch = relationship(Arch)

    def __str__(self) -> str:
        return str(self.arch)


class ReportPackage(GenericTable):
    __tablename__ = "reportpackages"
    __table_args__ = (UniqueConstraint('report_id', 'type', 'installed_package_id'),)

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       nullable=False, index=True)
    type = Column(Enum("CRASHED", "RELATED", "SELINUX_POLICY", name="reportpackage_type"))
    installed_package_id = Column(Integer, ForeignKey("{0}.id".format(Package.__tablename__)),
                                  nullable=False, index=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="packages")
    installed_package = relationship(Package, primaryjoin="Package.id==ReportPackage.installed_package_id")


class ReportUnknownPackage(GenericTable):
    __tablename__ = "reportunknownpackages"
    __table_args__ = (
        UniqueConstraint('report_id', 'type', 'name', 'epoch',
                         'version', 'release',
                         'arch_id'),
    )

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       nullable=False)
    type = Column(Enum("CRASHED", "RELATED", "SELINUX_POLICY", name="reportpackage_type"))
    name = Column(String(256), nullable=False, index=True)
    epoch = Column(Integer, nullable=False)
    version = Column(String(64), nullable=False)
    release = Column(String(64), nullable=False)
    arch_id = Column(Integer, ForeignKey("{0}.id".format(Arch.__tablename__)), nullable=False)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="unknown_packages")
    arch = relationship(Arch, primaryjoin="Arch.id==ReportUnknownPackage.arch_id")
    semver = Column(Semver, nullable=False)  # semantic version
    semrel = Column(Semver, nullable=False)  # semantic release
    Index("ix_reportunknownpackages_semver_semrel", semver, semrel)

    def nvr(self) -> str:
        return "{0}-{1}-{2}".format(self.name, self.version, self.release)

    def nevr(self) -> str:
        if not self.epoch:
            return self.nvr()
        return "{0}-{1}:{2}-{3}".format(self.name, self.epoch, self.version, self.release)

    def evr(self) -> str:
        return "{0}:{1}-{2}".format(self.epoch, self.version, self.release)


class ReportExecutable(GenericTable):
    __tablename__ = "reportexecutables"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    path = Column(String(512), nullable=False, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="executables")


class ReportUptime(GenericTable):
    __tablename__ = "reportuptimes"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    # stored as log(uptime, 10)
    uptime_exp = Column(Integer, nullable=False, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="uptimes")


class ReportReason(GenericTable):
    __tablename__ = "reportreasons"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    reason = Column(String(512), nullable=False, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="reasons")

    def __str__(self) -> str:
        crash_fn = 'unknown function'
        if self.report.backtraces:
            crash_fn = self.report.backtraces[0].crash_function

        return format_reason(self.report.type, self.reason, crash_fn)


class ReportSelinuxContext(GenericTable):
    __tablename__ = "reportselinuxcontexts"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    context = Column(String(256), nullable=False, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="selinux_contexts")


class ReportSelinuxMode(GenericTable):
    __tablename__ = "reportselinuxmodes"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    mode = Column(Enum("DISABLED", "PERMISSIVE", "ENFORCING", name="reportselinuxmode_mode"), primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="selinux_modes")

    def __str__(self) -> str:
        return self.mode.lower().capitalize()


class ReportHistoryMonthly(GenericTable):
    __tablename__ = "reporthistorymonthly"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    opsysrelease_id = Column(Integer, ForeignKey("{0}.id".format(OpSysRelease.__tablename__)), primary_key=True)
    month = Column(Date, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="history_monthly")
    unique = Column(Integer, nullable=False, default=0, server_default="0")
    opsysrelease = relationship(OpSysRelease)

    def to_json(self) -> Dict[str, Union[date, int]]:
        return {
            "date": self.month,
            "count": self.count,
        }


class ReportHistoryWeekly(GenericTable):
    __tablename__ = "reporthistoryweekly"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    opsysrelease_id = Column(Integer, ForeignKey("{0}.id".format(OpSysRelease.__tablename__)), primary_key=True)
    week = Column(Date, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="history_weekly")
    unique = Column(Integer, nullable=False, default=0, server_default="0")
    opsysrelease = relationship(OpSysRelease)

    def to_json(self) -> Dict[str, Union[date, int]]:
        return {
            "date": self.week,
            "count": self.count,
        }


class ReportHistoryDaily(GenericTable):
    __tablename__ = "reporthistorydaily"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__)), primary_key=True)
    opsysrelease_id = Column(Integer, ForeignKey("{0}.id".format(OpSysRelease.__tablename__)), primary_key=True)
    day = Column(Date, primary_key=True)
    count = Column(Integer, nullable=False)
    report = relationship(Report, backref="history_daily")
    unique = Column(Integer, nullable=False, default=0, server_default="0")
    opsysrelease = relationship(OpSysRelease)

    def to_json(self) -> Dict[str, Union[date, int]]:
        return {
            "date": self.day,
            "count": self.count,
        }


class KernelTaintFlag(GenericTable):
    __tablename__ = "kerneltaintedflags"

    id = Column(Integer, primary_key=True)
    ureport_name = Column(String(32), index=True, unique=True, nullable=False)
    nice_name = Column(String(256), nullable=False)
    character = Column(String(1), index=True, nullable=False)


class ReportBtTaintFlag(GenericTable):
    __tablename__ = "reportbttaintflags"

    backtrace_id = Column(Integer, ForeignKey("{0}.id".format(ReportBacktrace.__tablename__), ondelete="CASCADE"),
                          primary_key=True, index=True)
    taintflag_id = Column(Integer, ForeignKey("{0}.id".format(KernelTaintFlag.__tablename__)),
                          primary_key=True, index=True)

    backtrace = relationship(ReportBacktrace, backref="taint_flags")
    taintflag = relationship(KernelTaintFlag, backref="backtraces")


class KernelModule(GenericTable):
    __tablename__ = "kernelmodules"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True, index=True)


class ReportBtKernelModule(GenericTable):
    __tablename__ = "reportbtkernelmodules"

    backtrace_id = Column(Integer, ForeignKey("{0}.id".format(ReportBacktrace.__tablename__), ondelete="CASCADE"),
                          primary_key=True, index=True)
    kernelmodule_id = Column(Integer, ForeignKey("{0}.id".format(KernelModule.__tablename__)),
                             primary_key=True, index=True)

    backtrace = relationship(ReportBacktrace, backref="modules")
    kernelmodule = relationship(KernelModule, backref="backtraces")


class ReportBz(GenericTable):
    __tablename__ = "reportbz"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    bzbug_id = Column(Integer, ForeignKey("{0}.id".format(BzBug.__tablename__)), primary_key=True)
    report = relationship(Report, backref="bz_bugs")
    bzbug = relationship(BzBug)
    bug = synonym("bzbug")


class ReportMantis(GenericTable):
    __tablename__ = "reportmantis"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    mantisbug_id = Column(Integer, ForeignKey("{0}.id".format(MantisBug.__tablename__)), primary_key=True)
    report = relationship(Report, backref="mantis_bugs")
    mantisbug = relationship(MantisBug)
    bug = synonym("mantisbug")


class ReportRaw(GenericTable):
    __tablename__ = "reportraw"
    __lobs__ = {"ureport": 1 << 32,}

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       index=True, nullable=False)
    origin = Column(String(256), nullable=True, index=True)

    report = relationship(Report, backref="raw_reports")


class ReportExternalFaf(GenericTable):
    __tablename__ = "reportexternalfaf"

    faf_instance_id = Column(Integer, ForeignKey("{0}.id".format(ExternalFafInstance.__tablename__)),
                             index=True, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       index=True, primary_key=True)
    external_id = Column(Integer, nullable=False, index=True)

    report = relationship(Report, backref="external_faf_reports")
    faf_instance = relationship(ExternalFafInstance, backref="reports")

    def __str__(self) -> str:
        return "{0}#{1}".format(self.faf_instance.name, self.external_id)

    def url(self) -> str:
        return "{0}/reports/{1}".format(self.faf_instance.baseurl, self.external_id)


class ReportComment(GenericTable):
    __tablename__ = "reportcomments"

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       nullable=False)
    text = Column(String(1024), nullable=False)
    saved = Column(DateTime)

    report = relationship(Report, backref="comments")

    def to_json(self) -> Dict[str, Union[datetime, str]]:
        return {
            "saved": self.saved,
            "text": self.text,
        }


class ReportReleaseDesktop(GenericTable):
    __tablename__ = "reportreleasedesktops"

    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       index=True, primary_key=True)
    release_id = Column(Integer, ForeignKey("{0}.id".format(OpSysRelease.__tablename__)),
                        index=True, primary_key=True)
    desktop = Column(String(256), nullable=False, index=True, primary_key=True)
    count = Column(Integer, nullable=False)

    report = relationship(Report, backref="desktops")
    release = relationship(OpSysRelease, backref="desktops")


class ContactEmail(GenericTable):
    __tablename__ = "contactemails"
    id = Column(Integer, primary_key=True)
    email_address = Column(String(128), nullable=False)


class ReportContactEmail(GenericTable):
    __tablename__ = "reportcontactemails"
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       primary_key=True)
    contact_email_id = Column(Integer, ForeignKey("{0}.id".format(ContactEmail.__tablename__)), primary_key=True)
    report = relationship(Report, backref="report_contact_emails")
    contact_email = relationship(ContactEmail)


class ReportURL(GenericTable):
    __tablename__ = "reporturls"

    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       nullable=False)
    url = Column(String(1024), nullable=False)
    saved = Column(DateTime)

    report = relationship(Report, backref=backref("urls", order_by="desc(ReportURL.saved)", passive_deletes=True))


class ReportArchive(GenericTable):
    __tablename__ = "reportarchive"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)
    active = Column(Boolean, nullable=False)
    report_id = Column(Integer, ForeignKey("{0}.id".format(Report.__tablename__), ondelete="CASCADE"),
                       nullable=False)
    username = Column(String(100), ForeignKey("{0}.username".format(
        User.__tablename__)), nullable=False)
    report = relationship(Report, backref=backref("archive", uselist=False, passive_deletes=True))
    user = relationship(User, backref="archives")
