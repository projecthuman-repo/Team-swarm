from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AgentEvent(_message.Message):
    __slots__ = ("task_id", "agent", "kind", "detail", "ts_unix")
    class Kind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        CLAIMED: _ClassVar[AgentEvent.Kind]
        PROGRESS: _ClassVar[AgentEvent.Kind]
        PR_OPENED: _ClassVar[AgentEvent.Kind]
        CHECKS_PASSED: _ClassVar[AgentEvent.Kind]
        FAILED: _ClassVar[AgentEvent.Kind]
        LESSON: _ClassVar[AgentEvent.Kind]
    CLAIMED: AgentEvent.Kind
    PROGRESS: AgentEvent.Kind
    PR_OPENED: AgentEvent.Kind
    CHECKS_PASSED: AgentEvent.Kind
    FAILED: AgentEvent.Kind
    LESSON: AgentEvent.Kind
    TASK_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    DETAIL_FIELD_NUMBER: _ClassVar[int]
    TS_UNIX_FIELD_NUMBER: _ClassVar[int]
    task_id: str
    agent: str
    kind: AgentEvent.Kind
    detail: str
    ts_unix: int
    def __init__(self, task_id: _Optional[str] = ..., agent: _Optional[str] = ..., kind: _Optional[_Union[AgentEvent.Kind, str]] = ..., detail: _Optional[str] = ..., ts_unix: _Optional[int] = ...) -> None: ...
