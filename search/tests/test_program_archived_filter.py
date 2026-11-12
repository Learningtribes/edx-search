# -*- coding: utf-8 -*-
"""Tests for program exclude=archived ES filter (canonical released+ended)."""
from __future__ import absolute_import, division, print_function, unicode_literals

from datetime import datetime, timedelta

from django.test import TestCase
from django.test.utils import override_settings
from mock import patch

from search.api import (
    PROGRAM_FIELD_NULL_VALUE,
    mixed_content_discovery_search,
    program_not_archived_filter,
    programs_discovery_search,
)
from search.elastic import build_elasticsearch_query_dict
from search.filter_generator import SearchFilterGenerator as CourseSearchFilterGenerator
from search.tests.mock_search_engine import (
    MockSearchEngine,
    _document_matches_es_filter,
)
from search.utils import DateRange


class ExcludingArchivedCourseFilterGenerator(CourseSearchFilterGenerator):
    """Course generator that requests exclude=archived (Studio/LMS home shape)."""

    def field_dictionary(self, **kwargs):
        fields = super(
            ExcludingArchivedCourseFilterGenerator, self
        ).field_dictionary(**kwargs)
        fields['exclude'] = 'archived'
        return fields


@override_settings(SEARCH_ENGINE="search.tests.mock_search_engine.MockSearchEngine")
@override_settings(PROGRAM_INDEX_NAME="test_program_index")
class ProgramNotArchivedFilterTest(TestCase):
    """Unit coverage for program_not_archived_filter semantics."""

    def setUp(self):
        super(ProgramNotArchivedFilterTest, self).setUp()
        MockSearchEngine.destroy()
        self.now = datetime(2024, 6, 15, 12, 0, 0)
        self.es_filter = program_not_archived_filter(now=self.now)
        self.searcher = MockSearchEngine('test_program_index')

    def tearDown(self):
        MockSearchEngine.destroy()
        super(ProgramNotArchivedFilterTest, self).tearDown()

    def test_filter_shape_includes_released_and_end_keep_clauses(self):
        clauses = self.es_filter['or']
        self.assertIn({'missing': {'field': 'released_date'}}, clauses)
        self.assertIn({'term': {'released_date': PROGRAM_FIELD_NULL_VALUE}}, clauses)
        self.assertIn({'missing': {'field': 'end'}}, clauses)
        self.assertIn(
            {'range': {'end': {'gte': self.now.isoformat()}}},
            clauses,
        )

    def test_build_query_appends_additional_filters(self):
        query = build_elasticsearch_query_dict(
            filter_dictionary={},
            additional_filters=[self.es_filter],
        )
        must_filters = query['filtered']['filter']['bool']['must']
        self.assertIn(self.es_filter, must_filters)

    def test_draft_with_past_end_is_kept(self):
        doc = {
            'id': 'draft-past-end',
            'released_date': PROGRAM_FIELD_NULL_VALUE,
            'end': (self.now - timedelta(days=30)).isoformat(),
        }
        self.assertTrue(_document_matches_es_filter(doc, self.es_filter))

    def test_released_with_past_end_is_excluded(self):
        doc = {
            'id': 'archived',
            'released_date': '2023-01-01T00:00:00Z',
            'end': (self.now - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        self.assertFalse(_document_matches_es_filter(doc, self.es_filter))

    def test_released_with_future_end_is_kept(self):
        doc = {
            'id': 'running',
            'released_date': '2023-01-01T00:00:00Z',
            'end': (self.now + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        self.assertTrue(_document_matches_es_filter(doc, self.es_filter))

    def test_released_with_missing_end_is_kept(self):
        doc = {
            'id': 'open-ended',
            'released_date': '2023-01-01T00:00:00Z',
        }
        self.assertTrue(_document_matches_es_filter(doc, self.es_filter))

    def test_programs_discovery_search_excludes_only_canonical_archived(self):
        past = (self.now - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
        future = (self.now + timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
        docs = [
            {
                'id': 'kept-draft',
                'uuid': 'draft-uuid',
                'released_date': PROGRAM_FIELD_NULL_VALUE,
                'end': past,
                'content': {'title': 'Draft Past End'},
                'raw_title': 'Draft Past End',
                'start': past,
            },
            {
                'id': 'excluded-archived',
                'uuid': 'archived-uuid',
                'released_date': '2023-01-01T00:00:00Z',
                'end': past,
                'content': {'title': 'Archived LP'},
                'raw_title': 'Archived LP',
                'start': past,
            },
            {
                'id': 'kept-running',
                'uuid': 'running-uuid',
                'released_date': '2023-01-01T00:00:00Z',
                'end': future,
                'content': {'title': 'Running LP'},
                'raw_title': 'Running LP',
                'start': past,
            },
        ]
        self.searcher.index(doc_type='program_info', sources=docs)

        with patch(
            'search.api.program_not_archived_filter',
            return_value=self.es_filter,
        ):
            results = programs_discovery_search(
                size=20,
                from_=0,
                only_released_courses=False,
                field_dictionary={'exclude': 'archived'},
            )

        ids = sorted(hit['data']['id'] for hit in results['results'])
        self.assertEqual(ids, ['kept-draft', 'kept-running'])


@override_settings(
    SEARCH_FILTER_GENERATOR=(
        "search.tests.test_program_archived_filter.ExcludingArchivedCourseFilterGenerator"
    )
)
@override_settings(COURSEWARE_INDEX_NAME="courseware_index")
@override_settings(PROGRAM_INDEX_NAME="program_index")
class MixedDiscoveryArchivedFilterTest(TestCase):
    """mixed_content_discovery_search wires exclude=archived per index semantics."""

    @patch('search.api.process_range_data', side_effect=lambda results: results)
    @patch('search.elastic.search_mixed_discovery')
    @patch('search.elastic.build_elasticsearch_query_dict')
    @patch('search.api.SearchEngine.get_search_engine')
    def test_mixed_exclude_archived_uses_end_for_courses_and_canonical_for_programs(
            self, mock_get_engine, mock_build_query, mock_mixed_search, _mock_process):
        from search.elastic import ElasticSearchEngine

        mock_get_engine.return_value = object.__new__(ElasticSearchEngine)
        mock_build_query.side_effect = [{'match_all': {}}, {'match_all': {}}]
        mock_mixed_search.return_value = {
            'took': 1, 'total': 0, 'results': [], 'facets': {},
        }

        sentinel = {'or': [{'term': {'released_date': 'sentinel-keep'}}]}
        with patch(
            'search.api.program_not_archived_filter',
            return_value=sentinel,
        ) as mock_program_filter:
            mixed_content_discovery_search(
                program_field_dictionary={'exclude': 'archived'},
            )

        mock_program_filter.assert_called_once_with(now=None)
        self.assertEqual(mock_build_query.call_count, 2)

        course_call = mock_build_query.call_args_list[0]
        program_call = mock_build_query.call_args_list[1]

        course_filter_dictionary = course_call[0][2]
        self.assertIn('end', course_filter_dictionary)
        self.assertTrue(
            isinstance(course_filter_dictionary['end']['value'], DateRange)
        )
        self.assertNotIn('additional_filters', course_call[1])

        self.assertEqual(program_call[1].get('additional_filters'), [sentinel])
        program_filter_dictionary = program_call[0][2]
        self.assertNotIn('end', program_filter_dictionary)
