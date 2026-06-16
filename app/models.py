from pydantic import BaseModel, Field
from typing import List

class codeComment(BaseModel):
    file: str = Field(..., description = "The relative path to the file being reviewed.")
    line: int = Field(..., description = "The line number in the new version of the file.")
    severity: str = Field(..., description = "Severity level: P0 (security/crash), P1 (correctness), P2 (performance),P3 (style)")
    comment: str = Field(..., description = "Constructive, specific feedback. Explain the problems and suggest a concrete solution.")


class codeReviewSystem(BaseModel):
    comments : List[codeComment] = Field(default_factory=list, description="List of generated code review comments.")