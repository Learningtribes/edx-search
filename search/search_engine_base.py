""" Abstract SearchEngine with factory method """
# This will get called by tests, but pylint thinks that it is not used
from django.conf import settings

from .utils import _load_class


class SearchEngine(object):

    """ Base abstract SearchEngine object """

    index_name = "courseware"

    def __init__(self, index=None):
        if index:
            self.index_name = index

    def index(self, doc_type, sources, **kwargs):
        """ This operation is called to add documents of given type to the search index """
        raise NotImplementedError

    def remove(self, doc_type, doc_ids, **kwargs):
        """ This operation is called to remove documents of given type from the search index """
        raise NotImplementedError

    def search(self,
               query_strings=None,
               field_dictionary=None,
               filter_dictionary=None,
               exclude_dictionary=None,
               facet_terms=None,
               **kwargs):  # pylint: disable=too-many-arguments
        """ This operation is called to search for matching documents within the search index """
        raise NotImplementedError

    def search_string(self, query_strings, **kwargs):
        """ Helper function when primary search is for a query string """
        return self.search(query_strings=query_strings, **kwargs)

    def search_fields(self, field_dictionary, **kwargs):
        """ Helper function when primary search is for a set of matching fields """
        return self.search(field_dictionary=field_dictionary, **kwargs)

    @staticmethod
    def get_search_engine(index=None, index_mappings=None, alias=None):
        """Returns the desired implementor (defined in settings)

            @param index:           index name of ES
            @type index:            string
            @param index_mappings:  index mappings of ES
            @type index_mappings:   dict
            @param alias:           index alias name of ES
            @type alias:            string
            @return:                search engine obj.
            @rtype:                 python ElasticSearch Engine wrapper class.

            Note: support get index name by `index alias`
        """
        search_engine_class = _load_class(getattr(settings, "SEARCH_ENGINE", None), None)

        return search_engine_class(
            index=index, 
            index_mappings=index_mappings if index_mappings else None,
            alias=alias
        ) if search_engine_class else None
