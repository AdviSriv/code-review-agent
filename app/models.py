from pydantic import BaseModel, Field
from typing import List

class CodeComment(BaseModel):
    file: str = Field(..., description="The relative path to the file being reviewed.")
    position: int = Field(..., description="The exact 1-based diff position of the line needing feedback.")
    severity: str = Field(..., description="Severity of the issue: P0, P1, P2, or P3.")
    comment: str = Field(..., description="Clear feedback describing the issue and recommending a specific fix.")
    references_specific_identifier: bool = Field(..., description="True if the comment refers to a specific variable, function name, or class in the diff.")

class CodeReviewResponse(BaseModel):
    comments: List[CodeComment] = Field(default_factory=list, description="A structured list of code review findings.")