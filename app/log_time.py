"""Calendar dates for unchanged rsyslog timegenerated/date-rfc3339 filenames.
Both receiver and Python services must use the same host timezone (no TZ override).
Internal event timestamps remain UTC; do not rename historical files.
"""
import datetime as dt
import time


def receive_day(timestamp=None):
    return dt.datetime.fromtimestamp(time.time() if timestamp is None else timestamp).date()
