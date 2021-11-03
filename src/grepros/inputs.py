# -*- coding: utf-8 -*-
"""
Input sources for search content.

------------------------------------------------------------------------------
This file is part of grepros - grep for ROS bag files and live topics.
Released under the BSD License.

@author      Erki Suurjaak
@created     23.10.2021
@modified    03.11.2021
------------------------------------------------------------------------------
"""
## @namespace grepros.inputs
import copy
import collections
import datetime
import functools
try: import queue  # Py3
except ImportError: import Queue as queue  # Py2
import threading
import time

from . common import ConsolePrinter, drop_zeros, filter_dict, find_files, format_bytes, \
                     format_stamp, format_timedelta, make_bag_time, make_live_time
from . import rosapi


class SourceBase(object):
    """Message producer base class."""

    ## Template for message metainfo line
    MESSAGE_META_TEMPLATE = "{topic} {index} ({type}  {dt}  {stamp})"

    def __init__(self, args):
        """
        @param   args              arguments object like argparse.Namespace
        @param   args.START_TIME   earliest timestamp of messages to scan
                     .END_TIME     latest timestamp of messages to scan
        """
        self._args = copy.deepcopy(args)
        self._patterns = {}    # {key: [re.Pattern, ]}
        self._msgtypes = {}    # {topic: "pkg/MsgType"} in source
        self._topics   = []    # [topics searched in current source]

        ## outputs.SinkBase instance bound to this source
        self.sink = None

    def read(self):
        """Yields messages from source, as (topic, msg, ROS time)."""

    def bind(self, sink):
        """Attaches sink to source"""
        self.sink = sink

    def validate(self):
        """
        Returns whether source prerequisites are met (like ROS environment set if TopicSource).
        """
        return True

    def close(self):
        """Shuts down input, closing any files or connections."""

    def get_batch(self):
        """Returns source batch identifier if any (like bagfile name if BagSource)."""

    def get_meta(self):
        """Returns source metainfo string, for console output."""
        return ""

    def get_message_meta(self, topic, index, stamp, msg):
        """Returns message metainfo string, for console output."""
        kws = dict(topic=topic, type=self._msgtypes[topic], stamp=drop_zeros(stamp),
                   dt=drop_zeros(format_stamp(stamp)), index=index)
        return self.MESSAGE_META_TEMPLATE.format(**kws)

    def is_processable(self, topic, index, stamp):
        """Returns whether specified message in topic is in acceptable time range."""
        if self._args.START_TIME and stamp < self._args.START_TIME:
            return False
        if self._args.END_TIME and stamp > self._args.END_TIME:
            return False
        return True

    def notify(self, status):
        """Reports match status of last produced message."""

    def thread_excepthook(self, exc):
        """Handles exception, used by background threads."""
        ConsolePrinter.error(exc)


class BagSource(SourceBase):
    """Produces messages from ROS bagfiles."""

    ## Template for message metainfo line
    MESSAGE_META_TEMPLATE = "{topic} {index}/{total} ({type}  {dt}  {stamp})"
    ## Template for bag metainfo header
    META_TEMPLATE         = "\nFile {file} ({size}), {tcount} topics, {mcount:,d} messages\n" \
                            "File period {startdt} - {enddt}\n" \
                            "File span {delta} ({start} - {end})"

    def __init__(self, args):
        """
        @param   args               arguments object like argparse.Namespace
        @param   args.FILES         names of ROS bagfiles to scan if not all in directory
        @param   args.PATHS         paths to scan if not current directory
        @param   args.RECURSE       recurse into subdirectories when looking for bagfiles
        @param   args.TOPICS        ROS topics to scan if not all
        @param   args.TYPES         ROS message types to scan if not all
        @param   args.SKIP_TOPICS   ROS topics to skip
        @param   args.SKIP_TYPES    ROS message types to skip
        @param   args.START_TIME    earliest timestamp of messages to scan
        @param   args.END_TIME      latest timestamp of messages to scan
        @param   args.START_INDEX   message index within topic to start from
        @param   args.END_INDEX     message index within topic to stop at
        @param   args.AFTER         emit NUM messages of trailing context after match
        """
        super(BagSource, self).__init__(args)
        self._args0     = copy.deepcopy(args)  # Original arguments
        self._status    = None  # Match status of last produced message
        self._sticky    = False  # Scanning a single topic until all after-context emitted
        self._running   = False
        self._counts    = collections.defaultdict(int)  # {topic: count processed}
        self._msgtypes  = {}  # {topic: typename}
        self._msgtotals = {}  # {topic: total count in bag}
        self._bag       = None   # Current bag object instance
        self._filename  = None   # Current bagfile path

    def read(self):
        """Yields messages from ROS bagfiles, as (topic, msg, ROS time)."""
        self._running = True
        names, paths = self._args.FILES, self._args.PATHS
        exts, skip_exts = rosapi.BAG_EXTENSIONS, rosapi.SKIP_EXTENSIONS
        for filename in find_files(names, paths, exts, skip_exts, recurse=self._args.RECURSE):
            if not self._configure(filename) or not self._topics:
                continue  # for filename
            for topic, msg, stamp in self._produce(self._topics):
                yield topic, msg, stamp
            if not self._running:
                return
        self._running = False

    def validate(self):
        """Returns whether ROS environment is set, prints error if not."""
        return rosapi.validate()

    def close(self):
        """Closes current bag, if any."""
        self._running = False
        self._bag and self._bag.close()

    def get_batch(self):
        """Returns name of current bagfile."""
        return self._filename

    def get_meta(self):
        """Returns bagfile metainfo string, for console output."""
        start, end = self._bag.get_start_time(), self._bag.get_end_time()
        kws = dict(file=self._filename, size=format_bytes(self._bag.size),
                   mcount=self._bag.get_message_count(), tcount=len(self._msgtypes),
                   start=drop_zeros(start), startdt=drop_zeros(format_stamp(start)),
                   end=drop_zeros(end), enddt=drop_zeros(format_stamp(end)),
                   delta=format_timedelta(datetime.timedelta(seconds=end - start)))
        return self.META_TEMPLATE.format(**kws)

    def get_message_meta(self, topic, index, stamp, msg):
        """Returns message metainfo string, for console output."""
        kws = dict(topic=topic, type=self._msgtypes[topic], dt=drop_zeros(format_stamp(stamp)),
                   stamp=drop_zeros(stamp), index=index, total=self._msgtotals[topic])
        return self.MESSAGE_META_TEMPLATE.format(**kws)

    def notify(self, status):
        """Reports match status of last produced message."""
        self._status = bool(status)

    def is_processable(self, topic, index, stamp):
        """Returns whether specified message in topic is in acceptable range."""
        if self._args.START_INDEX:
            START = self._args.START_INDEX
            MIN = max(0, START + (self._msgtotals[topic] if START < 0 else 0))
            if MIN >= index:
                return False
        if self._args.END_INDEX:
            END = self._args.END_INDEX
            MAX = END + (self._msgtotals[topic] if END < 0 else 0)
            if MAX < index:
                return False
        return super(BagSource, self).is_processable(topic, index, stamp)

    def _produce(self, topics, start_time=None):
        """Yields messages from current ROS bagfile, as (topic, msg, ROS time)."""
        counts = collections.defaultdict(int)
        for topic, msg, stamp in self._bag.read_messages(topics, start_time):
            if not self._running:
                break  # for topic

            counts[topic], self._counts[topic] = counts[topic] + 1, self._counts[topic] + 1
            # Skip messages already processed during sticky
            if not self._sticky and counts[topic] != self._counts[topic]:
                continue  # for topic

            self._status = None
            yield topic, msg, stamp

            if self._status and self._args.AFTER and not self._sticky and len(self._topics) > 1:
                # Stick to one topic until trailing messages have been emitted
                self._sticky = True
                for entry in self._produce([topic], stamp + rosapi.make_duration(nsecs=1)):
                    yield entry
                self._sticky = False

    def _configure(self, filename):
        """Opens bag and populates bag-specific argument state, returns success."""
        try:
            bag = rosapi.create_bag_reader(filename)
        except Exception as e:
            ConsolePrinter.error("\nError opening %r: %s", filename, e)
            return False

        self._bag      = bag
        self._filename = filename
        self._sticky  = False
        self._counts.clear()

        topicdata = bag.get_type_and_topic_info().topics
        self._msgtypes  = {k: v.msg_type      for k, v in topicdata.items()}
        self._msgtotals = {k: v.message_count for k, v in topicdata.items()}

        dct = filter_dict(self._msgtypes, self._args.TOPICS, self._args.TYPES)
        dct = filter_dict(dct, self._args.SKIP_TOPICS, self._args.SKIP_TYPES, reverse=True)
        self._topics = list(dct)

        args = self._args = copy.deepcopy(self._args0)
        if args.START_TIME is not None:
            args.START_TIME = rosapi.make_time(make_bag_time(args.START_TIME, bag))
        if args.END_TIME is not None:
            args.END_TIME = rosapi.make_time(make_bag_time(args.END_TIME, bag))
        return True


class TopicSource(SourceBase):
    """Produces messages from live ROS topics."""

    ## Seconds between refreshing available topics from ROS master.
    MASTER_INTERVAL = 2

    def __init__(self, args):
        """
        @param   args                 arguments object like argparse.Namespace
        @param   args.TOPICS          ROS topics to scan if not all
        @param   args.TYPES           ROS message types to scan if not all
        @param   args.SKIP_TOPICS     ROS topics to skip
        @param   args.SKIP_TYPES      ROS message types to skip
        @param   args.START_TIME      earliest timestamp of messages to scan
        @param   args.END_TIME        latest timestamp of messages to scan
        @param   args.START_INDEX     message index within topic to start from
        @param   args.END_INDEX       message index within topic to stop at
        @param   args.QUEUE_SIZE_IN   subscriber queue size
        """
        super(TopicSource, self).__init__(args)
        self._running = False  # Whether is in process of yielding messages from topics
        self._queue   = None   # [(topic, msg, ROS time)]
        self._subs    = {}     # {topic: ROS subscriber}

        self._configure()

    def read(self):
        """Yields messages from subscribed ROS topics, as (topic, msg, ROS time)."""
        if not self._running:
            self._running = True
            self._queue = queue.Queue()
            self.refresh_topics()
            t = threading.Thread(target=self._run_refresh)
            t.daemon = True
            t.start()

        while self._running:
            topic, msg, stamp = self._queue.get()
            if topic:
                yield topic, msg, stamp
        self._queue = None
        self._running = False

    def bind(self, sink):
        """Attaches sink to source and blocks until connected to ROS live."""
        SourceBase.bind(self, sink)
        rosapi.init_node()

    def validate(self):
        """Returns whether ROS environment is set, prints error if not."""
        return rosapi.validate()

    def close(self):
        """Shuts down subscribers and stops producing messages."""
        self._running = False
        for t in list(self._subs):
            self._subs.pop(t).unregister()
        self._queue and self._queue.put((None, None, None))  # Wake up iterator
        self._queue = None
        self._msgtypes.clear()

    def is_processable(self, topic, index, stamp):
        """Returns whether specified message in topic is in acceptable range."""
        if self._args.START_INDEX:
            if max(0, self._args.START_INDEX) >= index:
                return False
        if self._args.END_INDEX:
            if 0 < self._args.END_INDEX < index:
                return False
        return super(TopicSource, self).is_processable(topic, index, stamp)

    def refresh_topics(self):
        """Refreshes topics and subscriptions from ROS live."""
        for topic, typename in rosapi.get_topic_types():
            if topic in self._msgtypes:
                continue  # for topic
            dct = filter_dict({topic: typename}, self._args.TOPICS, self._args.TYPES)
            dct = filter_dict(dct, self._args.SKIP_TOPICS, self._args.SKIP_TYPES, reverse=True)
            if dct:
                cls = rosapi.get_message_class(typename)
                handler = functools.partial(self._on_message, topic)
                sub = rosapi.create_subscriber(topic, cls, handler,
                                               queue_size=self._args.QUEUE_SIZE_IN)
                self._subs[topic] = sub
            self._msgtypes[topic] = typename

    def _configure(self):
        """Adjusts start/end time filter values to current time."""
        if self._args.START_TIME is not None:
            self._args.START_TIME = rosapi.make_time(make_live_time(self._args.START_TIME))
        if self._args.END_TIME is not None:
            self._args.END_TIME = rosapi.make_time(make_live_time(self._args.END_TIME))

    def _run_refresh(self):
        """Periodically refreshes topics and subscriptions from ROS live."""
        time.sleep(self.MASTER_INTERVAL)
        while self._running:
            try: self.refresh_topics()
            except Exception as e: self.thread_excepthook(e)
            time.sleep(self.MASTER_INTERVAL)

    def _on_message(self, topic, msg):
        """Subscription callback handler, queues message for yielding."""
        self._queue and self._queue.put((topic, msg, rosapi.get_rostime()))
