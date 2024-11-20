# app/search_state.py
import json
import time
from dataclasses import dataclass

@dataclass
class SearchProgress:
    total_urls: int = 0
    processed_urls: int = 0
    found_results: int = 0
    current_status: str = "waiting"
    start_time: float = 0
    
    def to_json(self):
        return json.dumps({
            "total_urls": self.total_urls,
            "processed_urls": self.processed_urls,
            "found_results": self.found_results,
            "current_status": self.current_status,
            "progress": round((self.processed_urls / max(self.total_urls, 1)) * 100, 2),
            "elapsed_time": round(time.time() - self.start_time, 2) if self.start_time else 0
        })

search_states = {}
