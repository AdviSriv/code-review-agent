import time
from app.config import get_config

class RateGatekeeper:
    def __init__(self):
        # Sliding log formatted as: (timestamp, token_count)
        self.call_history = []
        self.daily_count = 0
        self.last_day_timestamp = time.time()

    def update_window(self):
        now = time.time()
        # Filter out calls older than 60 seconds
        self.call_history = [call for call in self.call_history if now - call[0] < 60]
        
        # Reset daily counter every 24 hours
        if now - self.last_day_timestamp >= 86400:
            self.daily_count = 0
            self.last_day_timestamp = now

    def can_consume(self, estimated_tokens: int) -> tuple:
        """
        Determines whether the request can proceed under the safety margins.
        Returns: (can_proceed: bool, sleep_required: float)
        """
        self.update_window()
        config = get_config()
        
        max_rpm = config.get("MAX_RPM", 12)
        max_tpm = config.get("MAX_TPM", 200000)
        max_rpd = config.get("MAX_RPD", 400)
        
        # Check daily quota
        if self.daily_count >= max_rpd:
            print("[Gatekeeper] Hard daily quota limit reached. Delaying review.")
            return False, 3600.0
            
        current_rpm = len(self.call_history)
        current_tpm = sum(call[1] for call in self.call_history)
        
        # Verify if request exceeds active RPM or TPM window limits
        if current_rpm >= max_rpm or (current_tpm + estimated_tokens) >= max_tpm:
            # Calculate sleep duration until the oldest request in the window expires
            if self.call_history:
                oldest_timestamp = self.call_history[0][0]
                sleep_duration = max(0.1, 60.1 - (time.time() - oldest_timestamp))
                return False, sleep_duration
            return False, 5.0
            
        return True, 0.0

    def record_call(self, tokens_used: int):
        self.update_window()
        self.call_history.append((time.time(), tokens_used))
        self.daily_count += 1