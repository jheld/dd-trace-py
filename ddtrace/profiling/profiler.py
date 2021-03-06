# -*- encoding: utf-8 -*-
import logging
import os

from ddtrace.profiling import recorder
from ddtrace.profiling import scheduler
from ddtrace.vendor import attr
from ddtrace.profiling.collector import exceptions
from ddtrace.profiling.collector import memory
from ddtrace.profiling.collector import stack
from ddtrace.profiling.collector import threading
from ddtrace.profiling.exporter import file
from ddtrace.profiling.exporter import http


LOG = logging.getLogger(__name__)


def _build_default_exporters(service, env, version):
    exporters = []
    if "DD_PROFILING_API_KEY" in os.environ or "DD_API_KEY" in os.environ:
        exporters.append(http.PprofHTTPExporter(service=service, env=env, version=version))

    _OUTPUT_PPROF = os.environ.get("DD_PROFILING_OUTPUT_PPROF")
    if _OUTPUT_PPROF:
        exporters.append(file.PprofFileExporter(_OUTPUT_PPROF))

    if not exporters:
        LOG.warning("No exporters are configured, no profile will be output")

    return exporters


def _get_service_name():
    for service_name_var in ("DD_SERVICE", "DD_SERVICE_NAME", "DATADOG_SERVICE_NAME"):
        service_name = os.environ.get(service_name_var)
        if service_name is not None:
            return service_name


# This ought to use `enum.Enum`, but since it's not available in Python 2, we just use a dumb class.
@attr.s(repr=False)
class ProfilerStatus(object):
    """A Profiler status."""

    status = attr.ib()

    def __repr__(self):
        return self.status.upper()


ProfilerStatus.STOPPED = ProfilerStatus("stopped")
ProfilerStatus.RUNNING = ProfilerStatus("running")


@attr.s
class Profiler(object):
    """Run profiling while code is executed.

    Note that the whole Python process is profiled, not only the code executed. Data from all running threads are
    caught.

    If no collectors are provided, default ones are created.
    If no exporters are provided, default ones are created.

    """

    service = attr.ib(factory=_get_service_name)
    env = attr.ib(factory=lambda: os.environ.get("DD_ENV"))
    version = attr.ib(factory=lambda: os.environ.get("DD_VERSION"))
    tracer = attr.ib(default=None)
    collectors = attr.ib(default=None)
    exporters = attr.ib(default=None)
    schedulers = attr.ib(init=False, factory=list)
    status = attr.ib(init=False, type=ProfilerStatus, default=ProfilerStatus.STOPPED)

    @staticmethod
    def _build_default_collectors(tracer):
        r = recorder.Recorder(
            max_events={
                # Allow to store up to 10 threads for 60 seconds at 100 Hz
                stack.StackSampleEvent: 10 * 60 * 100,
                stack.StackExceptionSampleEvent: 10 * 60 * 100,
                # This can generate one event every 0.1s if 100% are taken — though we take 5% by default.
                # = (60 seconds / 0.1 seconds)
                memory.MemorySampleEvent: int(60 / 0.1),
            },
            default_max_events=int(os.environ.get("DD_PROFILING_MAX_EVENTS", recorder.Recorder._DEFAULT_MAX_EVENTS)),
        )
        return [
            stack.StackCollector(r, tracer=tracer),
            memory.MemoryCollector(r),
            exceptions.UncaughtExceptionCollector(r),
            threading.LockCollector(r),
        ]

    def __attrs_post_init__(self):
        if self.collectors is None:
            self.collectors = self._build_default_collectors(self.tracer)

        if self.exporters is None:
            self.exporters = _build_default_exporters(self.service, self.env, self.version)

        if self.exporters:
            for rec in self.recorders:
                self.schedulers.append(scheduler.Scheduler(recorder=rec, exporters=self.exporters))

    @property
    def recorders(self):
        return set(c.recorder for c in self.collectors)

    def start(self):
        """Start the profiler."""
        for col in self.collectors:
            try:
                col.start()
            except RuntimeError:
                # `tracemalloc` is unavailable?
                pass

        for s in self.schedulers:
            s.start()

        self.status = ProfilerStatus.RUNNING

    def stop(self, flush=True):
        """Stop the profiler.

        This stops all the collectors and schedulers, waiting for them to finish their operations.

        :param flush: Wait for the flush of the remaining events before stopping.
        """
        for col in reversed(self.collectors):
            col.stop()

        for col in reversed(self.collectors):
            col.join()

        for s in reversed(self.schedulers):
            s.stop()

        if flush:
            for s in reversed(self.schedulers):
                s.join()

        self.status = ProfilerStatus.STOPPED
