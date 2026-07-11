from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class Task(_message.Message):
    __slots__ = ("task_id", "role", "title", "spec_ref", "branch", "attempt", "budget_tokens", "deadline_unix")
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    SPEC_REF_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    ATTEMPT_FIELD_NUMBER: _ClassVar[int]
    BUDGET_TOKENS_FIELD_NUMBER: _ClassVar[int]
    DEADLINE_UNIX_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    role: str
    title: str
    spec_ref: str
    branch: str
    attempt: int
    budget_tokens: int
    deadline_unix: int
    def __init__(self, task_id: _Optional[str] = ..., role: _Optional[str] = ..., title: _Optional[str] = ..., spec_ref: _Optional[str] = ..., branch: _Optional[str] = ..., attempt: _Optional[int] = ..., budget_tokens: _Optional[int] = ..., deadline_unix: _Optional[int] = ...) -> None: ...
