"""LT contracts on the official edx-search 5 Elasticsearch transport.

Document types become an indexed field and namespaced IDs. Permissions,
targeting and missing-field rules execute inside the real Elasticsearch query.
"""
from copy import deepcopy
from datetime import datetime
from django.conf import settings
from elasticsearch import exceptions
from elasticsearch.helpers import bulk, BulkIndexError
from .api import QueryParseError
from .upstream_elastic import ElasticSearchEngine as UpstreamElasticSearchEngine
from .upstream_elastic import RESERVED_CHARACTERS, _get_filter_field

TYPE = 'lt_document_type'


def build_elasticsearch_query_dict(query_strings=None, field_dictionary=None,
                                   filter_dictionary=None, exclude_dictionary=None,
                                   exclude_ids=None, use_field_match=False, targeting_gate=None):
    """Preserve LT AND-within-field matching and conjunctive permission filters."""
    terms = [query_strings] if isinstance(query_strings, str) else list(query_strings or [])
    filters = [_get_filter_field(k, v) for k, v in (field_dictionary or {}).items()]
    for field, spec in (filter_dictionary or {}).items():
        if not isinstance(spec, dict) or 'value' not in spec:
            spec = {'value': spec, 'missing_included': True}
        choices = [_get_filter_field(field, spec['value'])]
        if spec.get('missing_included', True):
            choices.append({'bool': {'must_not': [{'exists': {'field': field}}]}})
        filters.append({'bool': {'should': choices, 'minimum_should_match': 1}})
    excluded = [_get_filter_field(k, v) for k, v in (exclude_dictionary or {}).items()]
    if exclude_ids:
        excluded.append({'terms': {'id': list(exclude_ids)}})
    if targeting_gate:
        flag, key = targeting_gate.get('flag_field'), targeting_gate.get('key_field')
        if not flag or not key:
            raise ValueError('Targeting gate requires flag_field and key_field')
        choices = [{'bool': {'must_not': [{'term': {flag: True}}]}}]
        allowed = list(targeting_gate.get('allowed') or [])
        if allowed:
            choices.append({'terms': {key: allowed}})
        filters.append({'bool': {'should': choices, 'minimum_should_match': 1}})
    query = {'filter': filters, 'must_not': excluded}
    terms = [term for term in terms if len(term) > 1]
    if terms:
        query['must'] = [{'bool': {
            'should': [{'bool': {'must': [
                {'match': {field: {'query': term, 'operator': 'AND', 'analyzer': 'standard'}}}
                for term in terms
            ]}} for field in ['content.display_name', 'content.title', 'content.course_id']],
            'minimum_should_match': 1,
        }}]
    return {'bool': query}


def _aggregations(facets):
    return {name: {'filter': {'match_all': {}},
                   'aggs': {'values': {'terms': {'field': name, **options}},
                            'missing_values': {'missing': {'field': name}}}}
            for name, options in (facets or {}).items()}


def _sort_fields(sort):
    result = []
    date_fields = {'start', 'end', 'start_date', 'enrollment_start', 'enrollment_end',
                   'created', 'modified', 'new_flag_expired_date'}
    for entry in deepcopy(sort or []):
        if isinstance(entry, str):
            entry = {entry: {}}
        for field, options in entry.items():
            if isinstance(options, str):
                options = {'order': options}
            options.pop('ignore_unmapped', None)
            default_type = ('date' if field in date_fields else
                            'boolean' if field == 'new_course_flag' else 'keyword')
            options.setdefault('unmapped_type', default_type)
            if options['unmapped_type'] == 'string':
                options['unmapped_type'] = 'keyword'
            result.append({field: options})
    return result


def _translate_hits(response):
    hits = response['hits']
    results = []
    for hit in hits['hits']:
        data = dict(hit['_source'])
        doc_type = data.pop(TYPE, '_doc')
        results.append({'_id': str(data.get('id', hit['_id'])), '_index': hit['_index'],
                        '_type': doc_type, 'score': hit['_score'], 'data': data})
    translated = {'took': response['took'], 'max_score': hits['max_score'],
                  'total': hits['total']['value'] if isinstance(hits['total'], dict) else hits['total'],
                  'results': results, 'facets': {}}
    for name, agg in response.get('aggregations', {}).items():
        if name == 'total_records':
            translated['doc_count'] = agg['authorized']['doc_count']
        else:
            values = agg['values']
            terms = {b.get('key_as_string', b['key']): b['doc_count'] for b in values['buckets']}
            translated['facets'][name] = {'terms': terms, 'total': sum(terms.values()),
                'other': values.get('sum_other_doc_count', 0),
                'missing': agg['missing_values']['doc_count']}
    return translated


class ElasticSearchEngine(UpstreamElasticSearchEngine):
    """Official connection/mapping cache with explicit LT public contracts."""
    def _check_mappings(self, body):
        known = self.mappings.get('properties', {})
        properties = {}
        for name, value in body.items():
            if name in known or name == 'content' or value is None or isinstance(value, dict):
                continue
            mapping = {'type': 'keyword'}
            if isinstance(value, bool):
                mapping = {'type': 'boolean'}
            elif isinstance(value, (int, float)):
                mapping = {'type': 'double'}
            elif isinstance(value, datetime) or name in {
                'start', 'end', 'start_date', 'enrollment_start', 'enrollment_end',
                'created', 'modified', 'new_flag_expired_date',
            }:
                mapping = {'type': 'date', 'ignore_malformed': False}
            mapping.update(getattr(settings, 'ELASTIC_FIELD_MAPPINGS', {}).get(name, {}))
            properties[name] = mapping
        if properties:
            self._es.indices.put_mapping(index=self._prefixed_index_name, body={'properties': properties})
            self._clear_mapping()
        super()._check_mappings({key: value for key, value in body.items() if value is not None})

    def index(self, doc_type, sources=None, **kwargs):
        if sources is None:
            sources, doc_type = doc_type, '_doc'
        actions = []
        for source in sources:
            document = {**source, TYPE: doc_type}
            if document.get('id') is None:
                raise ValueError('An indexed LT document requires an id')
            self._check_mappings(document)
            actions.append({'_index': self._prefixed_index_name,
                '_id': str(doc_type) + ':' + str(document['id']), '_source': document})
        return bulk(self._es, actions, **kwargs)

    def remove(self, doc_type, doc_ids=None, **kwargs):
        if doc_ids is None:
            doc_ids, doc_type = doc_type, '_doc'
        actions = [{'_op_type': 'delete', '_index': self._prefixed_index_name,
            '_id': str(doc_type) + ':' + str(doc_id)} for doc_id in doc_ids]
        try:
            return bulk(self._es, actions, **kwargs)
        except BulkIndexError as error:
            if any(item['delete']['status'] != 404 for item in error.errors):
                raise

    def search_string(self, query_string, **kwargs):
        return self.search(query_strings=query_string, **kwargs)

    def search(self, query_strings=None, field_dictionary=None, filter_dictionary=None,
               exclude_dictionary=None, facet_terms=None, exclude_ids=None,
               targeting_gate=None, doc_type=None, size=10, from_=0, sort=None,
               ga_total=False, query_string=None, aggregation_terms=None,
               use_field_match=False, **kwargs):
        if kwargs:
            raise TypeError('Unsupported LT search options: ' + ', '.join(sorted(kwargs)))
        fields = dict(field_dictionary or {})
        if doc_type:
            fields[TYPE] = doc_type
        query = build_elasticsearch_query_dict(
            query_strings if query_strings is not None else query_string,
            fields, filter_dictionary, exclude_dictionary, exclude_ids, use_field_match, targeting_gate)
        body = {'query': query, 'track_total_hits': True, 'size': size, 'from': from_}
        aggs = _aggregations(facet_terms or aggregation_terms)
        if ga_total:
            # Count within the same organization/permission/targeting boundary.
            scope = fields  # All exact field restrictions remain part of the authorization boundary.
            aggs['total_records'] = {'global': {}, 'aggs': {'authorized': {
                'filter': build_elasticsearch_query_dict(field_dictionary=scope, targeting_gate=targeting_gate)}}}
        if aggs:
            body['aggs'] = aggs
        if sort:
            body['sort'] = _sort_fields(sort)
        try:
            return _translate_hits(self._es.search(index=self._prefixed_index_name, body=body))
        except exceptions.RequestError as error:
            if error.error in {'parsing_exception', 'query_shard_exception'}:
                raise QueryParseError('Malformed search query') from error
            raise


def search_mixed_discovery(engine, scoped_queries, sort, size, from_, facet_terms=None):
    """Execute cross-index discovery in one real Elasticsearch request."""
    if not scoped_queries:
        raise ValueError('scoped_queries must not be empty')
    prefix = getattr(settings, 'ELASTIC_SEARCH_INDEX_PREFIX', '')
    branches = [{'bool': {'must': [{'term': {'_index': prefix + index}}, query]}}
                for index, query in scoped_queries]
    body = {'query': {'bool': {'should': branches, 'minimum_should_match': 1}},
            'track_total_hits': True, 'size': size, 'from': from_}
    if sort:
        body['sort'] = _sort_fields(sort)
    if facet_terms:
        body['aggs'] = _aggregations(facet_terms)
    return _translate_hits(engine._es.search(
        index=','.join(prefix + index for index, _ in scoped_queries), body=body))
