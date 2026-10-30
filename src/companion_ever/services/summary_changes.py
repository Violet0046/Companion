"""Compact a contiguous journal window without altering its audit records."""
from collections import OrderedDict


SEMANTIC_FIELDS = ('kind', 'content', 'reality', 'event_time', 'time_basis')
SNAPSHOT_OPERATIONS = {'create', 'bootstrap', 'update', 'archive', 'restore', 'delete'}


def effective_snapshot(snapshot):
    if not snapshot or snapshot.get('kind') != 'fact' or snapshot.get('is_archived', 0):
        return None
    return {key: snapshot.get(key) for key in SEMANTIC_FIELDS}


def coalesce_changes(events):
    """Net content/validity and supersession changes; evidence alone is not new meaning."""
    groups = OrderedDict()
    for event in events:
        group = groups.setdefault(event['note_id'], {'first': None, 'last': None, 'links': {}, 'events': []})
        operation = event['operation']
        if operation in SNAPSHOT_OPERATIONS:
            if group['first'] is None:
                group['first'] = event
            group['last'] = event
            if effective_snapshot(event['before']) != effective_snapshot(event['after']):
                group['events'].append(event)
        elif operation in ('supersedes_added', 'supersedes_removed'):
            edge = event['after'] or event['before'] or {}
            key = (edge.get('from_id'), edge.get('to_id'))
            present = operation == 'supersedes_added'
            link = group['links'].setdefault(key, {'from_id': key[0], 'to_id': key[1], 'before_present': not present})
            link['after_present'] = present
            group['events'].append(event)
    result = []
    for nid, group in groups.items():
        first, last = group['first'], group['last']
        before = effective_snapshot(first['before']) if first else None
        after = effective_snapshot(last['after']) if last else None
        links = [edge for edge in group['links'].values() if edge['before_present'] != edge['after_present']]
        if before == after and not links:
            continue
        substantive = group['events']
        latest = substantive[-1]
        operation = (last['operation'] if first and before != after else latest['operation'])
        if first and before is None and after is not None:
            operation = first['operation'] if first['operation'] in ('create','bootstrap') else 'restore'
        result.append({'id': latest['id'], 'first_change_id': substantive[0]['id'], 'note_id': nid,
                       'operation': operation, 'before': before, 'after': after,
                       'relation_changes': links, 'source_job_id': latest['source_job_id'],
                       'created_at': substantive[0]['created_at']})
    return result
