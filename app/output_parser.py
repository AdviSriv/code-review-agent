import json
from typing import List
from app.models import codeComment

def parse_and_sort_comments(raw_json: str) -> List[codeComment]:
    try: 
        data = json.loads(raw_json)

        if isinstance( data, dict) and "comments" in data:
            raw_comments = data["comments"]
        elif isinstance(data, list):
            raw_comments = data
        else:
            raw_comments = []

        comments = []

        for item in raw_comments:
            try:
                comment = codeComment(**item)
                comments.append(comment)
            except Exception as e:
                print(f"Skipping malformed object: {item}. Error: {e}")
        
        severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        comments.sort(key = lambda c: severity_order.get(c.severity,99))
        return comments
    except Exception as e:
        raise ValueError(f"Parsing failed for structured JSON response: {e} \nRaw payload: {raw_json}")