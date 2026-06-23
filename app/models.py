from pydantic import BaseModel, Field
from typing import List, Optional

# --- Module G: Aggregation Models ---
class CodeComment(BaseModel):
    file: str = Field(..., description="The relative path to the file being reviewed.")
    position: int = Field(..., description="The exact 1-based diff position of the changed line.")
    severity: str = Field(..., description="Severity level: P0, P1, P2, or P3.")
    role: str = Field(..., description="The subagent role that generated the finding (Security, Architecture, Logic, Maintainability, or Multiple).")
    comment: str = Field(..., description="Constructive code review feedback. Explain what is wrong and provide a concrete solution.")
    references_specific_identifier: bool = Field(..., description="True if feedback references a specific function name, variable, or class.")

class CodeReviewResponse(BaseModel):
    comments: List[CodeComment] = Field(default_factory=list)

# --- Module E: Subagent Return Structures ---
class SubagentFinding(BaseModel):
    position: int = Field(..., description="1-based diff position of the line needing feedback.")
    severity: str = Field(..., description="Severity level: P0, P1, P2, or P3.")
    comment: str = Field(..., description="Critical analysis finding text.")
    references_specific_identifier: bool = Field(..., description="True if a specific variable or class name is referenced.")
    escalated_symbol: Optional[str] = Field(None, description="Provide symbol name (function/class) if additional file definitions are needed.")

class SubagentResponse(BaseModel):
    status: str = Field(..., description="Return status: 'SUCCESS' if review is completed, or 'NEEDS_CONTEXT' if symbols must be resolved.")
    confidence: float = Field(..., description="Confidence score from 0.0 to 1.0.")
    findings: List[SubagentFinding] = Field(default_factory=list)