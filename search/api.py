""" search business logic implementations """
from datetime import datetime, timedelta
import dateutil.parser
from django.conf import settings
from collections import defaultdict

from .filter_generator import SearchFilterGenerator
from .search_engine_base import SearchEngine
from .result_processor import SearchResultProcessor
from .utils import DateRange

# Default filters that we support, override using COURSE_DISCOVERY_FILTERS setting if desired
DEFAULT_FILTER_FIELDS = ["org", "modes", "language"]
#from xmodule.course_module import CATALOG_VISIBILITY_CATALOG_AND_ABOUT
CATALOG_VISIBILITY_CATALOG_AND_ABOUT = "both"

def course_discovery_filter_fields():
    """ look up the desired list of course discovery filter fields """
    return getattr(settings, "COURSE_DISCOVERY_FILTERS", DEFAULT_FILTER_FIELDS)


def course_discovery_facets():
    """ Discovery facets to include, by default we specify each filter field with unspecified size attribute """
    return getattr(settings, "COURSE_DISCOVERY_FACETS", {field: {'size': 100} for field in course_discovery_filter_fields()})

class NoSearchEngineError(Exception):
    """ NoSearchEngineError exception to be thrown if no search engine is specified """
    pass


class QueryParseError(Exception):
    """QueryParseError will be thrown if the query is malformed.

    If a query has mismatched quotes (e.g. '"some phrase', return a
    more specific exception so the view can provide a more helpful
    error message to the user.

    """
    pass


def perform_search(
        search_term,
        user=None,
        size=10,
        from_=0,
        course_id=None):
    """ Call the search engine with the appropriate parameters """
    # field_, filter_ and exclude_dictionary(s) can be overridden by calling application
    # field_dictionary includes course if course_id provided
    (field_dictionary, filter_dictionary, exclude_dictionary) = SearchFilterGenerator.generate_field_filters(
        user=user,
        course_id=course_id
    )

    searcher = SearchEngine.get_search_engine(getattr(settings, "COURSEWARE_INDEX_NAME", "courseware_index"))
    if not searcher:
        raise NoSearchEngineError("No search engine specified in settings.SEARCH_ENGINE")

    filter_dictionary = {key:_format_filter(value) for key, value in filter_dictionary.items()}

    results = searcher.search_string(
        search_term,
        field_dictionary=field_dictionary,
        filter_dictionary=filter_dictionary,
        exclude_dictionary=exclude_dictionary,
        size=size,
        from_=from_,
        doc_type="courseware_content",
        include_content=True
    )

    # post-process the result
    for result in results["results"]:
        result["data"] = SearchResultProcessor.process_result(result["data"], search_term, user)

    results["access_denied_count"] = len([r for r in results["results"] if r["data"] is None])
    results["results"] = [r for r in results["results"] if r["data"] is not None]

    return results


def _format_filter(filter, missing_included=True):
    """This is used to apply filters missing or existing search according to specific value.
    """
    return {'value': filter, 'missing_included': missing_included}


def process_range_data(results):
    """Mainly used for processing range datetime data, including `start` property and combined `status` property(`start` and `end`).
    """
    # For LMS usage
    if "start" in course_discovery_filter_fields():
        start_terms = results.get('facets', {}).get('start', {}).get('terms', {})
        if start_terms:
            new_start_terms = defaultdict(int)

            for key, value in start_terms.items():
                if not isinstance(key, (str, unicode, bytes, bytearray)):
                    continue
                key = dateutil.parser.parse(key, ignoretz=True)
                now = datetime.utcnow()
                new_key = 'future'

                if key < now - timedelta(days=30):
                    new_key = 'current'
                elif key <= now:
                    new_key = 'new'
                elif key < now + timedelta(days=30):
                    new_key = 'soon'

                new_start_terms[new_key] += value

            results['facets']['start']['terms'] = new_start_terms
            results['facets']['start']['total'] = sum(new_start_terms.values())

    # For Studio usage
    elif "status" in course_discovery_filter_fields():
        status_terms = defaultdict(int)
        for course in results.get('results', []):
            start_term = course.get('data', {}).get('start', None)
            end_term = course.get('data', {}).get('end', None)
            now = datetime.utcnow()
            # start property always has value(not None)
            if not isinstance(start_term, (str, unicode, bytes, bytearray)):
                continue
            if start_term and dateutil.parser.parse(start_term, ignoretz=True) <= now:
                if not isinstance(end_term, (str, unicode, bytes, bytearray)):
                    continue
                if end_term and dateutil.parser.parse(end_term, ignoretz=True) <= now:
                    status_terms['past'] += 1
                else:
                    status_terms['current'] += 1
            else:
                status_terms['future'] += 1
                
        results['facets']['status']['terms'] = status_terms
        results['facets']['status']['total'] = sum(status_terms.values())

    return results


def course_discovery_search(search_term=None, size=20, from_=0, field_dictionary=None, **kwargs):
    """
    Course Discovery activities against the search engine index of course details
    """
    # We'll ignore the course-enrollemnt informaiton in field and filter
    # dictionary, and use our own logic upon enrollment dates for these
    use_search_fields = ["org"]
    if kwargs.get('include_course_filter', False) and kwargs.get('user', None) and not kwargs['user'].is_staff:
        use_search_fields.append("course")
    (search_fields, _, exclude_dictionary) = SearchFilterGenerator.generate_field_filters(**kwargs)
    use_field_dictionary = {}
    use_field_dictionary.update({field: search_fields[field] for field in search_fields if field in use_search_fields})
    if field_dictionary:
        use_field_dictionary.update(field_dictionary)
    if not getattr(settings, "SEARCH_SKIP_ENROLLMENT_START_DATE_FILTERING", False):
        use_field_dictionary["enrollment_start"] = DateRange(None, datetime.utcnow())

    searcher = SearchEngine.get_search_engine(getattr(settings, "COURSEWARE_INDEX_NAME", "courseware_index"))
    if not searcher:
        raise NoSearchEngineError("No search engine specified in settings.SEARCH_ENGINE")

    filter_dictionary = {}
    if kwargs.get('allow_enrollment_end_filter', False):
        filter_dictionary.update({
            "enrollment_end": _format_filter(DateRange(datetime.utcnow(), None))
        })
    start = use_field_dictionary.pop('start', None)
    if start == 'current':
        filter_dictionary.update({
            'start':
            _format_filter(
                DateRange(None,
                          datetime.utcnow() - timedelta(days=30)))
        })
    elif start == 'new':
        filter_dictionary.update({
            'start':
            _format_filter(
                DateRange(datetime.utcnow() - timedelta(days=30),
                          datetime.utcnow()))
        })
    elif start == 'soon':
        filter_dictionary.update({
            'start':
            _format_filter(
                DateRange(datetime.utcnow(),
                          datetime.utcnow() + timedelta(days=30)))
        })
    elif start == 'future':
        filter_dictionary.update({
            'start':
            _format_filter(
                DateRange(datetime.utcnow() + timedelta(days=30), None))
        })

    status = use_field_dictionary.pop('status', None)
    if status == 'past':
        filter_dictionary.update({
            'end':
            _format_filter(DateRange(None, datetime.utcnow()),
                           missing_included=False)
        })
    elif status == 'current':
        filter_dictionary.update({
            'start':
            _format_filter(
                DateRange(None, datetime.utcnow())),
            'end':
            _format_filter(DateRange(datetime.utcnow(), None))
        })
    elif status == 'future':
        filter_dictionary.update({
            'start':
            _format_filter(
                DateRange(datetime.utcnow(), None))
        })

    if getattr(settings, 'ALLOW_CATALOG_VISIBILITY_FILTER', False):
        use_field_dictionary['catalog_visibility'] = CATALOG_VISIBILITY_CATALOG_AND_ABOUT

    results = searcher.search(
        query_string=search_term,
        doc_type="course_info",
        size=size,
        from_=from_,
        # only show when enrollment start IS provided and is before now
        field_dictionary=use_field_dictionary,
        # show if no enrollment end is provided and has not yet been reached
        filter_dictionary=filter_dictionary,
        exclude_dictionary=exclude_dictionary,
        facet_terms=course_discovery_facets(),
    )

    results = process_range_data(results)
    return results
