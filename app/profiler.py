import time
import threading
from collections import defaultdict

class PipelineProfiler:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(PipelineProfiler, cls).__new__(cls, *args, **kwargs)
                cls._instance.metrics = defaultdict(float)
                cls._instance.starts = {}
                cls._instance.lock = threading.Lock()
            return cls._instance

    def start(self, phase: str, thread_id: str = "main"):
        key = f"{phase}::{thread_id}"
        with self.lock:
            self.starts[key] = time.perf_counter()

    def stop(self, phase: str, thread_id: str = "main"):
        key = f"{phase}::{thread_id}"
        with self.lock:
            if key in self.starts:
                elapsed = time.perf_counter() - self.starts[key]
                self.metrics[phase] += elapsed
                del self.starts[key]

    def record(self, phase: str, duration: float):
        with self.lock:
            self.metrics[phase] += duration

    def report(self):
        with self.lock:
            print("\n" + "=" * 25 + " PIPELINE PROFILER METRICS " + "=" * 25)
            sorted_metrics = sorted(self.metrics.items(), key=lambda x: x[1], reverse=True)
            total = sum(self.metrics.values())
            for phase, duration in sorted_metrics:
                pct = (duration / total * 100) if total > 0 else 0
                print(f" - {phase:<45}: {duration:7.3f}s ({pct:5.1f}%)")
            print(f" - Total Profiled Execution Duration           : {total:7.3f}s")
            print("=" * 77 + "\n")