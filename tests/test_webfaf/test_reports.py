#!/usr/bin/python3
# -*- encoding: utf-8 -*-
import os
import json
import unittest

import sys
from io import BytesIO as StringIO

from webfaftests import WebfafTestCase

from pyfaf.storage import InvalidUReport, Report, BzBug, ReportBz, Bugtracker, BzUser
from pyfaf import config, ureport
from pyfaf.queries import *
from pyfaf.common import ensure_dirs
from pyfaf.config import paths

class ReportTestCase(WebfafTestCase):
    """
    Tests for webfaf.reports
    """

    def setUp(self):
        super(ReportTestCase, self).setUp()
        self.basic_fixtures()
        self.db.session.commit()
        ensure_dirs([paths["reports_incoming"]])
        ensure_dirs([paths["reports_saved"]])
        ensure_dirs([paths["reports_deferred"]])
        ensure_dirs([paths["attachments_incoming"]])

    def post_file(self, url, contents):
        r = self.app.post(url, buffered=True,
                          headers={"Accept": "application/json"},
                          content_type="multipart/form-data",
                          data={"file": (StringIO(contents.encode("utf-8")), "lala.txt")})

        return r

    def create_bugzilla_bug(self, privacy=False):
        #get last report id

        bztracker = Bugtracker()
        bztracker.name = "centos-bugzilla"

        bzuser = BzUser()
        bzuser.email = "test@redhat.com"
        bzuser.name = "test@redhat.com"
        bzuser.real_name = "Test User"
        bzuser.can_login = True

        bzbug = BzBug()
        bzbug.opsysrelease_id = 1
        bzbug.summary = "summary"
        bzbug.status = "NEW"
        bzbug.creation_time = "2016-02-12 11:44:10"
        bzbug.last_change_time = "2016-02-12 11:44:10"
        bzbug.tracker_id = 1
        bzbug.component_id = 1
        bzbug.whiteboard = 1
        bzbug.creator_id = 1
        bzbug.private = privacy

        self.db.session.add(bztracker)
        self.db.session.add(bzuser)
        self.db.session.add(bzbug)
        self.db.session.flush()

        reports = self.db.session.query(Report).all()
        for rep in reports:
            reportbz = ReportBz()
            reportbz.report_id = str(rep.id)
            reportbz.bzbug_id = 1

            self.db.session.add(reportbz)
            self.db.session.flush()

    def post_report(self, contents):
        return self.post_file("/reports/new/", contents)

    def post_attachment(self, contents):
        return self.post_file("/reports/attach/", contents)

    def clear_reports(self):
        self.db.session.query(Report).delete()
        self.db.session.commit()
        self.db.session.flush()

    def test_new_report_ureport1(self):
        """
        Test saving of ureport version 1
        """
        path = os.path.join(self.reports_path, 'ureport1')
        with open(path, "r", encoding="utf-8") as file:
            r = self.post_report(file.read())

        js = json.loads(r.data)
        self.assertEqual(js["result"], False)
        self.assertEqual(js["bthash"],
                         "38faad7cb921ee2a19f42ff01a8f9c2066133f8d")

    def test_new_report_ureport2(self):
        """
        Test saving of ureport version 2
        """

        cases = [
            [
                os.path.join(self.reports_path, "ureport2"),
                "2dd542ba1f1e074216196b6c0bd548609bf38ebc",
            ],
            [
                os.path.join(self.reports_path,
                             "d856f816-6fad-46a3-baea-53673646bb72"),
                "ae74a26cff6e6bb36b8997c0e4748cf4688dca2e",
            ],
            [
                os.path.join(self.reports_path, "unikhod_failnejm"),
                "e221c0322c5dfbf634d3609161f80efac97946bc",
            ],
        ]
        for case in cases:
            with open(case[0], "r", encoding="utf-8") as file:
                r = self.post_report(file.read())

            js = json.loads(r.data)
            self.assertEqual(js["result"], False)
            self.assertEqual(js["bthash"], case[1])

    def test_new_report_prefilter_solution(self):
        """
        Test prefilter solutions of ureport version 2
        """

        self.assertEqual(self.call_action("sf-prefilter-soladd", {
            "CAUSE": "TestSolution",
            "NOTE": "TestSolution",
            "note-html": "<html><b>HTML</b><html>",
            "url": "http://www.fedoraproject.org",
        }), 0)

        self.assertEqual(self.call_action("sf-prefilter-patadd", {
            "SOLUTION": "TestSolution",
            "opsys": "fedora",
            "pkgname": "^faf.*$",
        }), 0)
        self.db.session.commit()

        path = os.path.join(self.reports_path, 'ureport2')
        with open(path, "r", encoding="utf-8") as file:
            r = self.post_report(file.read())

        js = json.loads(r.data)

        self.assertEqual(js["result"], True)
        self.assertEqual(js["bthash"],
                         "2dd542ba1f1e074216196b6c0bd548609bf38ebc")
        self.assertIn("Your problem seems to be caused by TestSolution",
                      js["message"])
        self.assertEqual(js["solutions"][0]["url"],
                         "http://www.fedoraproject.org")

    def test_invalid_report(self):
        """
        Test error handling of invalid reports
        """

        r = self.post_report("invalid")
        self.assertEqual(json.loads(r.data)["error"], u"Couldn't parse JSON data.")
        self.assertEqual(self.db.session.query(InvalidUReport).count(), 1)

        r = self.post_report('{"invalid":"json"}')
        self.assertEqual(json.loads(r.data)["error"], u"uReport data is invalid.")
        self.assertEqual(self.db.session.query(InvalidUReport).count(), 2)

    def test_attach(self):
        """
        Test attach functionality
        """

        path = os.path.join(self.reports_path, 'bugzilla_attachment')
        with open(path, "r", encoding="utf-8") as file:
            r = self.post_attachment(file.read())
        self.assertEqual(json.loads(r.data)["result"], True)

    def test_attach_invalid(self):
        """
        Test error handling of invalid attachments
        """

        r = self.post_attachment("invalid")
        self.assertEqual(json.loads(r.data)["error"], u"Invalid JSON file")

    def test_report_duplicates(self):
        """
        Test reports duplicates
        CONSTANT WHICH IS TESTED: EQUAL_UREPORT_EXISTS
        """
        self.clear_reports()
        config.config['ureport.known'] = "EQUAL_UREPORT_EXISTS"

        source = [{'file_name': 'ureport_duplicate', 'result': False},
                  {'file_name': 'ureport_duplicate', 'result': True},
                  {'file_name': 'ureport_duplicate2', 'result': False}]

        for item in source:
            path = os.path.join(self.reports_path, item['file_name'])

            with open(path, "r", encoding="utf-8") as file:
                data = self.post_report(file.read())

            self.assertEqual(self.call_action("save-reports"), 0)

            self.db.session.commit()
            self.db.session.flush()

            data_js = json.loads(data.data)

            if item['result'] is True:
                self.assertTrue(data_js['result'])
            else:
                self.assertFalse(data_js['result'])

    def test_report_with_private_bugs(self):
        """
        Test that reports with known bugs are unknown.
        """
        self.clear_reports()
        config.config['ureport.known'] = ""

        source = [{'file_name': 'ureport_duplicate', 'result': False},
                  {'file_name': 'ureport_duplicate', 'result': False}]

        i = 0
        for item in source:
            path = os.path.join(self.reports_path, item['file_name'])

            with open(path, "r", encoding="utf-8") as file:
                data = self.post_report(file.read())

            self.assertEqual(self.call_action("save-reports"), 0)

            self.db.session.commit()
            self.db.session.flush()

            if i == 0:
                self.create_bugzilla_bug(True)
                self.db.session.commit()
                self.db.session.flush()

            data_js = json.loads(data.data)

            if item['result'] is True:
                self.assertTrue(data_js['result'])
            else:
                self.assertFalse(data_js['result'])

            i += 1



    def test_report_duplicate_os_minor(self):
        """
        Test reports duplicates
        CONSTANT WHICH IS TESTED: BUG_OS_MINOR_VERSION
        """
        self.clear_reports()
        config.config['ureport.known'] = "BUG_OS_MINOR_VERSION"

        source = [{'file_name': 'ureport_duplicate', 'result': False},
                  {'file_name': 'ureport_duplicate', 'result': True},
                  {'file_name': 'ureport_duplicate2', 'result': False},
                  {'file_name': 'ureport_duplicate3', 'result': False},
                  {'file_name': 'ureport_duplicate4', 'result': False}]

        i = 0
        for item in source:
            path = os.path.join(self.reports_path, item['file_name'])

            with open(path, "r", encoding="utf-8") as file:
                data = self.post_report(file.read())

            self.assertEqual(self.call_action("save-reports"), 0)

            self.db.session.commit()
            self.db.session.flush()

            if i == 0:
                self.create_bugzilla_bug()
                self.db.session.commit()
                self.db.session.flush()

            data_js = json.loads(data.data)

            if item['result'] is True:
                self.assertTrue(data_js['result'])
            else:
                self.assertFalse(data_js['result'])

            i += 1

    def test_report_duplicate_os_major(self):
        """
        Test reports duplicates
        CONSTANT WHICH IS TESTED: BUG_OS_MAJOR_VERSION
        """
        self.clear_reports()
        config.config['ureport.known'] = "BUG_OS_MAJOR_VERSION"

        source = [{'file_name': 'ureport_duplicate', 'result': False},
                  {'file_name': 'ureport_duplicate', 'result': True},
                  {'file_name': 'ureport_duplicate2', 'result': True},
                  {'file_name': 'ureport_duplicate3', 'result': False},
                  {'file_name': 'ureport_duplicate4', 'result': False}]

        i = 0
        for item in source:
            path = os.path.join(self.reports_path, item['file_name'])

            with open(path, "r", encoding="utf-8") as file:
                data = self.post_report(file.read())

            self.assertEqual(self.call_action("save-reports"), 0)

            self.db.session.commit()
            self.db.session.flush()

            if i == 0:
                self.create_bugzilla_bug()
                self.db.session.commit()
                self.db.session.flush()

            data_js = json.loads(data.data)

            if item['result'] is True:
                self.assertTrue(data_js['result'])
            else:
                self.assertFalse(data_js['result'])

            i += 1

    def test_get_report(self):
        self.clear_reports()

        path = os.path.join(self.reports_path, 'ureport_duplicate')
        with open(path, "r", encoding="utf-8") as file:
            first = self.post_report(file.read())

        self.assertEqual(self.call_action("save-reports"), 0)
        self.db.session.commit()
        self.db.session.flush()

        first_js = json.loads(first.data)

        report = get_report(self.db, first_js['bthash'])

        report1 = get_report(self.db, first_js['bthash'], os_name='centos',
                              os_version='6.7', os_arch='x86_64')

        report2 = get_report(self.db, first_js['bthash'], os_name='centos',
                              os_version='7.1', os_arch='x86_64')

        report3 = get_report(self.db, first_js['bthash'], os_name='centos',
                              os_version='6.7', os_arch='noarch')

        report4 = get_report(self.db, first_js['bthash'], os_name='centos',
                              os_version='6.8', os_arch='x86_64')

        report5 = get_report(self.db, first_js['bthash'], os_name='centos')

        report6 = get_report(self.db, first_js['bthash'], os_version='6.7')

        report7 = get_report(self.db, first_js['bthash'], os_arch='x86_64')

        report8 = get_report(self.db, first_js['bthash'], os_name='centos',
                              os_arch='x86_64')

        report9 = get_report(self.db, first_js['bthash'], os_version='6.7',
                              os_arch='x86_64')

        self.assertIsNotNone(report)
        self.assertIsNotNone(report1)
        self.assertIsNone(report2)
        self.assertIsNone(report3)
        self.assertIsNone(report4)
        self.assertIsNotNone(report5)
        self.assertIsNotNone(report6)
        self.assertIsNotNone(report7)
        self.assertIsNotNone(report8)
        self.assertIsNotNone(report9)

    def test_known_type(self):
        result = ureport.valid_known_type("EQUAL_UREPORT_EXISTS".split(" "))
        result1 = ureport.valid_known_type("BUG_OS_MINOR_VERSION".split(" "))
        result2 = ureport.valid_known_type("BUG_OS_MAJOR_VERSION".split(" "))
        result3 = ureport.valid_known_type("EQUAL_UREPORT_EXISTS "
                                           "BUG_OS_MINOR_VERSION".split(" "))
        result4 = ureport.valid_known_type("  ".strip().split(" "))
        result5 = ureport.valid_known_type("BUGS_OS_MAJOR_VERSION".split(" "))
        result6 = ureport.valid_known_type("EQUAL_UREPORT_EXISTS   "
                                            "BUG_OS_MINOR_VERSION".split(" "))

        self.assertTrue(result)
        self.assertTrue(result1)
        self.assertTrue(result2)
        self.assertTrue(result3)
        self.assertTrue(result4)
        self.assertFalse(result5)
        self.assertTrue(result6)

if __name__ == "__main__":
    unittest.main()
