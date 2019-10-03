from prometheus_client import Gauge, Summary

DUPLICATE_ACTION_COUNT = Gauge(
    'duplicate_action_total',
    'Number of images per action',
    ['action']
)
DUPLICATE_ACTION_NONE_COUNT = DUPLICATE_ACTION_COUNT.labels(action="none")
DUPLICATE_ACTION_MOVE_COUNT = DUPLICATE_ACTION_COUNT.labels(action="move")
DUPLICATE_ACTION_DELETE_COUNT = DUPLICATE_ACTION_COUNT.labels(action="delete")

FILE_EVENT_COUNT = Gauge(
    'file_event_total',
    'Number of file events per event type',
    ['type']
)

ANALYSIS_TIME = Summary('analyse_file_summary', 'Time spent analysing a file')

FIND_DUPLICATES_TIME = Summary('find_duplicates_summary', 'Time spent finding duplicates of a file')
