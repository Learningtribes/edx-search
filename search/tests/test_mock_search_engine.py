# -*- coding: utf-8 -*-
""" Tests for MockSearchEngine specific features """
from __future__ import absolute_import, division, print_function, unicode_literals

from datetime import datetime

import pytz
from django.test import TestCase
from django.test.utils import override_settings

from search.api import _format_filter
from search.tests.mock_search_engine import (
    _find_field,
    _filter_intersection,
    _unwrap_discovery_filter,
    json_date_to_datetime,
)
from search.tests.utils import SearcherMixin
from search.utils import DateRange


# Any class that inherits from TestCase will cause too-many-public-methods pylint error
# pylint: disable=too-many-public-methods


@override_settings(SEARCH_ENGINE="search.tests.mock_search_engine.MockSearchEngine")
@override_settings(ELASTIC_FIELD_MAPPINGS={"start_date": {"type": "date"}})
class MockSpecificSearchTests(TestCase, SearcherMixin):
    """ For testing pieces of the Mock Engine that have no equivalent in Elastic """

    def test_find_field_arguments(self):
        """ test that field argument validity is observed """
        field_value = _find_field(
            {
                "name": "Come and listen to my story"
            },
            "name"
        )
        self.assertEqual(field_value, "Come and listen to my story")

        field_value = _find_field(
            {
                "name": {
                    "first": "Martyn",
                    "last": "James"
                }
            },
            "name.first"
        )
        self.assertEqual(field_value, "Martyn")

        field_value = _find_field(
            {
                "name": {
                    "first": "Monica",
                    "last": {
                        "one": "Parker",
                        "two": "James"
                    }
                }
            },
            "name.last.two"
        )
        self.assertEqual(field_value, "James")

        with self.assertRaises(ValueError):
            field_value = _find_field(
                {
                    "name": "Come and listen to my story"
                },
                123
            )

        with self.assertRaises(ValueError):
            field_value = _find_field(123, "name")

    def test_filter_optimization(self):
        """ Make sure that intersection optimizes return when no filter dictionary is provided """
        test_docs = [{"A": {"X": 1, "Y": 2, "Z": 3}}, {"B": {"X": 9, "Y": 8, "Z": 7}}]
        self.assertTrue(_filter_intersection(test_docs, None), test_docs)

    def test_unwrap_discovery_filter_recognizes_format_filter_shape(self):
        wrapped = _format_filter("released", missing_included=False)
        value, include_missing = _unwrap_discovery_filter(wrapped, default_include_blanks=True)
        self.assertEqual(value, "released")
        self.assertFalse(include_missing)

    def test_unwrap_discovery_filter_ignores_literal_value_dict(self):
        literal = {"value": "x"}
        value, include_missing = _unwrap_discovery_filter(literal, default_include_blanks=True)
        self.assertEqual(value, literal)
        self.assertTrue(include_missing)

    def test_filter_intersection_applies_discovery_wrapper(self):
        docs = [
            {"id": "with-end", "end": datetime(2099, 1, 1)},
            {"id": "missing-end"},
        ]
        filtered = _filter_intersection(
            docs,
            {"end": _format_filter(DateRange(datetime.utcnow(), None), missing_included=True)},
            include_blanks=False,
        )
        self.assertEqual(
            sorted(doc["id"] for doc in filtered),
            ["missing-end", "with-end"],
        )

        filtered_no_missing = _filter_intersection(
            docs,
            {"end": _format_filter(DateRange(datetime.utcnow(), None), missing_included=False)},
            include_blanks=True,
        )
        self.assertEqual([doc["id"] for doc in filtered_no_missing], ["with-end"])

    def test_filter_intersection_literal_value_dict_is_not_unwrapped(self):
        """
        A filter value of ``{'value': 'x'}`` (no ``missing_included``) must not be
        treated as ``_format_filter('x')``.
        """
        docs = [
            {"id": "has-x", "meta": "x"},
            {"id": "missing"},
        ]
        # Discovery wrapper: match value ``x``, and keep docs missing the field.
        wrapped = _filter_intersection(
            docs,
            {"meta": _format_filter("x", missing_included=True)},
            include_blanks=False,
        )
        self.assertEqual(
            sorted(doc["id"] for doc in wrapped),
            ["has-x", "missing"],
        )

        # Literal dict: equality against ``{'value': 'x'}``, missing field excluded.
        literal = _filter_intersection(
            docs,
            {"meta": {"value": "x"}},
            include_blanks=False,
        )
        self.assertEqual(literal, [])

    def test_datetime_conversion(self):
        """ tests json_date_to_datetime with different formats """
        json_date = "2015-01-31"
        self.assertTrue(json_date_to_datetime(json_date), datetime(2015, 1, 31))

        json_datetime = "2015-01-31T07:30:28"
        self.assertTrue(json_date_to_datetime(json_datetime), datetime(2015, 1, 31, 7, 30, 28))

        json_datetime = "2015-01-31T07:30:28.65785"
        self.assertTrue(json_date_to_datetime(json_datetime), datetime(2015, 1, 31, 7, 30, 28, 65785))

        json_datetime = "2015-01-31T07:30:28Z"
        self.assertTrue(json_date_to_datetime(json_datetime), datetime(2015, 1, 31, 7, 30, 28))

        json_datetime = "2015-01-31T07:30:28.65785Z"
        self.assertTrue(json_date_to_datetime(json_datetime), datetime(2015, 1, 31, 7, 30, 28, 65785))

    def test_timezone_conversion(self):
        """
        Tests internal operation of mock when range values have timezone, and query objects do not, and vice versa
        """
        # first where index has no timezones, and query has
        low_date = datetime(2010, 1, 1)
        high_date = datetime(2100, 1, 1)
        self.searcher.index("test_doc", [{"id": "FAKE_ID_1", "test_value": "1", "start_date": low_date}])
        self.searcher.index("test_doc", [{"id": "FAKE_ID_2", "test_value": "2", "start_date": high_date}])

        response = self.searcher.search(field_dictionary={"start_date": low_date.replace(tzinfo=pytz.UTC)})
        self.assertEqual(response["total"], 1)

        response = self.searcher.search(
            field_dictionary={"start_date": DateRange(None, datetime.utcnow().replace(tzinfo=pytz.UTC))}
        )
        self.assertEqual(response["total"], 1)

        response = self.searcher.search(
            field_dictionary={"start_date": DateRange(datetime(2099, 1, 1).replace(tzinfo=pytz.UTC), None)}
        )
        self.assertEqual(response["total"], 1)

        self.searcher.destroy()
        self.searcher.index(
            "test_doc", [{"id": "FAKE_ID_1", "test_value": "1", "start_date": low_date.replace(tzinfo=pytz.UTC)}]
        )
        self.searcher.index(
            "test_doc", [{"id": "FAKE_ID_2", "test_value": "2", "start_date": high_date.replace(tzinfo=pytz.UTC)}]
        )

        response = self.searcher.search(field_dictionary={"start_date": low_date})
        self.assertEqual(response["total"], 1)

        response = self.searcher.search(field_dictionary={"start_date": DateRange(None, datetime.utcnow())})
        self.assertEqual(response["total"], 1)

        response = self.searcher.search(field_dictionary={"start_date": DateRange(datetime(2099, 1, 1), None)})
        self.assertEqual(response["total"], 1)
