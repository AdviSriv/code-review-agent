import time
import threading
from app.config import get_config

class RateGatekeeper:
    def __init__(self):
        self.call_history = []
        self.daily_count = 0
        self.last_day_timestamp = time.time()
        # Shared lock to synchronize rate tracking across subagent worker threads
        self._lock = threading.Lock()

    def update_window(self):
        now = time.time()
        self.call_history = [call for call in self.call_history if now - call[0] < 60]
        
        if now - self.last_day_timestamp >= 86400:
            self.daily_count = 0
            self.last_day_timestamp = now

    def can_consume(self, estimated_tokens: int) -> tuple:
        with self._lock:  # Mutex protection
            self.update_window()
            config = get_config()
            
            max_rpm = config.get("MAX_RPM", 12)
            max_tpm = config.get("MAX_TPM", 200000)
            max_rpd = config.get("MAX_RPD", 400)
            
            if self.daily_count >= max_rpd:
                return False, 3600.0
                
            current_rpm = len(self.call_history)
            current_tpm = sum(call[1] for call in self.call_history)
            
            if current_rpm >= max_rpm or (current_tpm + estimated_tokens) >= max_tpm:
                if self.call_history:
                    oldest_timestamp = self.call_history[0][0]
                    sleep_duration = max(0.1, 60.1 - (time.time() - oldest_timestamp))
                    return False, sleep_duration
                return False, 5.0
                
            return True, 0.0

    def record_call(self, tokens_used: int):
        with self._lock:  # Mutex protection
            self.update_window()
            self.call_history.append((time.time(), tokens_used))
            self.daily_count += 1