from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Quality(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    QUALITY_UNSPECIFIED: _ClassVar[Quality]
    GOOD: _ClassVar[Quality]
    BAD_COMM: _ClassVar[Quality]
    BAD_CONFIG: _ClassVar[Quality]
    BAD_DECODE: _ClassVar[Quality]
    STALE: _ClassVar[Quality]
QUALITY_UNSPECIFIED: Quality
GOOD: Quality
BAD_COMM: Quality
BAD_CONFIG: Quality
BAD_DECODE: Quality
STALE: Quality

class Variant(_message.Message):
    __slots__ = ("bool_value", "int_value", "uint_value", "float_value", "string_value", "list_value")
    BOOL_VALUE_FIELD_NUMBER: _ClassVar[int]
    INT_VALUE_FIELD_NUMBER: _ClassVar[int]
    UINT_VALUE_FIELD_NUMBER: _ClassVar[int]
    FLOAT_VALUE_FIELD_NUMBER: _ClassVar[int]
    STRING_VALUE_FIELD_NUMBER: _ClassVar[int]
    LIST_VALUE_FIELD_NUMBER: _ClassVar[int]
    bool_value: bool
    int_value: int
    uint_value: int
    float_value: float
    string_value: str
    list_value: VariantList
    def __init__(self, bool_value: _Optional[bool] = ..., int_value: _Optional[int] = ..., uint_value: _Optional[int] = ..., float_value: _Optional[float] = ..., string_value: _Optional[str] = ..., list_value: _Optional[_Union[VariantList, _Mapping]] = ...) -> None: ...

class VariantList(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[Variant]
    def __init__(self, items: _Optional[_Iterable[_Union[Variant, _Mapping]]] = ...) -> None: ...

class TagValue(_message.Message):
    __slots__ = ("value", "quality", "source_ts_us")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    QUALITY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TS_US_FIELD_NUMBER: _ClassVar[int]
    value: Variant
    quality: Quality
    source_ts_us: int
    def __init__(self, value: _Optional[_Union[Variant, _Mapping]] = ..., quality: _Optional[_Union[Quality, str]] = ..., source_ts_us: _Optional[int] = ...) -> None: ...

class DriverError(_message.Message):
    __slots__ = ("kind", "message")
    class Kind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        KIND_UNSPECIFIED: _ClassVar[DriverError.Kind]
        COMM: _ClassVar[DriverError.Kind]
        CONNECTION_LOST: _ClassVar[DriverError.Kind]
        BLOCK_CONFIG: _ClassVar[DriverError.Kind]
        UNSUPPORTED: _ClassVar[DriverError.Kind]
        OTHER: _ClassVar[DriverError.Kind]
        NOT_OPEN: _ClassVar[DriverError.Kind]
    KIND_UNSPECIFIED: DriverError.Kind
    COMM: DriverError.Kind
    CONNECTION_LOST: DriverError.Kind
    BLOCK_CONFIG: DriverError.Kind
    UNSUPPORTED: DriverError.Kind
    OTHER: DriverError.Kind
    NOT_OPEN: DriverError.Kind
    KIND_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    kind: DriverError.Kind
    message: str
    def __init__(self, kind: _Optional[_Union[DriverError.Kind, str]] = ..., message: _Optional[str] = ...) -> None: ...

class HelloRequest(_message.Message):
    __slots__ = ("protocol", "engine")
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    ENGINE_FIELD_NUMBER: _ClassVar[int]
    protocol: str
    engine: str
    def __init__(self, protocol: _Optional[str] = ..., engine: _Optional[str] = ...) -> None: ...

class HelloReply(_message.Message):
    __slots__ = ("protocol", "host", "drivers")
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    HOST_FIELD_NUMBER: _ClassVar[int]
    DRIVERS_FIELD_NUMBER: _ClassVar[int]
    protocol: str
    host: str
    drivers: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, protocol: _Optional[str] = ..., host: _Optional[str] = ..., drivers: _Optional[_Iterable[str]] = ...) -> None: ...

class DeviceConfig(_message.Message):
    __slots__ = ("json",)
    JSON_FIELD_NUMBER: _ClassVar[int]
    json: str
    def __init__(self, json: _Optional[str] = ...) -> None: ...

class PlanRequest(_message.Message):
    __slots__ = ("device", "tags")
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    device: DeviceConfig
    tags: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, device: _Optional[_Union[DeviceConfig, _Mapping]] = ..., tags: _Optional[_Iterable[str]] = ...) -> None: ...

class Block(_message.Message):
    __slots__ = ("payload", "tags", "wire_requests", "describe")
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    WIRE_REQUESTS_FIELD_NUMBER: _ClassVar[int]
    DESCRIBE_FIELD_NUMBER: _ClassVar[int]
    payload: bytes
    tags: _containers.RepeatedScalarFieldContainer[str]
    wire_requests: int
    describe: str
    def __init__(self, payload: _Optional[bytes] = ..., tags: _Optional[_Iterable[str]] = ..., wire_requests: _Optional[int] = ..., describe: _Optional[str] = ...) -> None: ...

class DriverInfo(_message.Message):
    __slots__ = ("reports_connection", "connect_timeout_ms", "stale_by_read_time")
    REPORTS_CONNECTION_FIELD_NUMBER: _ClassVar[int]
    CONNECT_TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    STALE_BY_READ_TIME_FIELD_NUMBER: _ClassVar[int]
    reports_connection: bool
    connect_timeout_ms: int
    stale_by_read_time: bool
    def __init__(self, reports_connection: _Optional[bool] = ..., connect_timeout_ms: _Optional[int] = ..., stale_by_read_time: _Optional[bool] = ...) -> None: ...

class PlanReply(_message.Message):
    __slots__ = ("error", "blocks", "invalid", "info")
    class InvalidEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ERROR_FIELD_NUMBER: _ClassVar[int]
    BLOCKS_FIELD_NUMBER: _ClassVar[int]
    INVALID_FIELD_NUMBER: _ClassVar[int]
    INFO_FIELD_NUMBER: _ClassVar[int]
    error: DriverError
    blocks: _containers.RepeatedCompositeFieldContainer[Block]
    invalid: _containers.ScalarMap[str, str]
    info: DriverInfo
    def __init__(self, error: _Optional[_Union[DriverError, _Mapping]] = ..., blocks: _Optional[_Iterable[_Union[Block, _Mapping]]] = ..., invalid: _Optional[_Mapping[str, str]] = ..., info: _Optional[_Union[DriverInfo, _Mapping]] = ...) -> None: ...

class OpenRequest(_message.Message):
    __slots__ = ("device_id", "device")
    DEVICE_ID_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FIELD_NUMBER: _ClassVar[int]
    device_id: str
    device: DeviceConfig
    def __init__(self, device_id: _Optional[str] = ..., device: _Optional[_Union[DeviceConfig, _Mapping]] = ...) -> None: ...

class OpenReply(_message.Message):
    __slots__ = ("error", "info")
    ERROR_FIELD_NUMBER: _ClassVar[int]
    INFO_FIELD_NUMBER: _ClassVar[int]
    error: DriverError
    info: DriverInfo
    def __init__(self, error: _Optional[_Union[DriverError, _Mapping]] = ..., info: _Optional[_Union[DriverInfo, _Mapping]] = ...) -> None: ...

class DeviceRef(_message.Message):
    __slots__ = ("device_id",)
    DEVICE_ID_FIELD_NUMBER: _ClassVar[int]
    device_id: str
    def __init__(self, device_id: _Optional[str] = ...) -> None: ...

class StatusReply(_message.Message):
    __slots__ = ("error", "connected", "info")
    ERROR_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    INFO_FIELD_NUMBER: _ClassVar[int]
    error: DriverError
    connected: bool
    info: DriverInfo
    def __init__(self, error: _Optional[_Union[DriverError, _Mapping]] = ..., connected: _Optional[bool] = ..., info: _Optional[_Union[DriverInfo, _Mapping]] = ...) -> None: ...

class ReadRequest(_message.Message):
    __slots__ = ("device_id", "block")
    DEVICE_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCK_FIELD_NUMBER: _ClassVar[int]
    device_id: str
    block: bytes
    def __init__(self, device_id: _Optional[str] = ..., block: _Optional[bytes] = ...) -> None: ...

class ReadReply(_message.Message):
    __slots__ = ("error", "values", "connected", "driver_us")
    class ValuesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: TagValue
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[TagValue, _Mapping]] = ...) -> None: ...
    ERROR_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    DRIVER_US_FIELD_NUMBER: _ClassVar[int]
    error: DriverError
    values: _containers.MessageMap[str, TagValue]
    connected: bool
    driver_us: int
    def __init__(self, error: _Optional[_Union[DriverError, _Mapping]] = ..., values: _Optional[_Mapping[str, TagValue]] = ..., connected: _Optional[bool] = ..., driver_us: _Optional[int] = ...) -> None: ...

class IdentityReply(_message.Message):
    __slots__ = ("error", "json", "connected")
    ERROR_FIELD_NUMBER: _ClassVar[int]
    JSON_FIELD_NUMBER: _ClassVar[int]
    CONNECTED_FIELD_NUMBER: _ClassVar[int]
    error: DriverError
    json: str
    connected: bool
    def __init__(self, error: _Optional[_Union[DriverError, _Mapping]] = ..., json: _Optional[str] = ..., connected: _Optional[bool] = ...) -> None: ...

class BrowsedTag(_message.Message):
    __slots__ = ("name", "address", "dtype", "description")
    NAME_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    DTYPE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    name: str
    address: str
    dtype: str
    description: str
    def __init__(self, name: _Optional[str] = ..., address: _Optional[str] = ..., dtype: _Optional[str] = ..., description: _Optional[str] = ...) -> None: ...

class BrowseReply(_message.Message):
    __slots__ = ("error", "tags")
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TAGS_FIELD_NUMBER: _ClassVar[int]
    error: DriverError
    tags: _containers.RepeatedCompositeFieldContainer[BrowsedTag]
    def __init__(self, error: _Optional[_Union[DriverError, _Mapping]] = ..., tags: _Optional[_Iterable[_Union[BrowsedTag, _Mapping]]] = ...) -> None: ...

class SubscribeRequest(_message.Message):
    __slots__ = ("device_id", "blocks")
    DEVICE_ID_FIELD_NUMBER: _ClassVar[int]
    BLOCKS_FIELD_NUMBER: _ClassVar[int]
    device_id: str
    blocks: _containers.RepeatedScalarFieldContainer[bytes]
    def __init__(self, device_id: _Optional[str] = ..., blocks: _Optional[_Iterable[bytes]] = ...) -> None: ...

class Notification(_message.Message):
    __slots__ = ("error", "values")
    class ValuesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: TagValue
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[TagValue, _Mapping]] = ...) -> None: ...
    ERROR_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    error: DriverError
    values: _containers.MessageMap[str, TagValue]
    def __init__(self, error: _Optional[_Union[DriverError, _Mapping]] = ..., values: _Optional[_Mapping[str, TagValue]] = ...) -> None: ...

class HealthRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class HealthReply(_message.Message):
    __slots__ = ("protocol", "pid", "uptime_s", "open_devices", "reads")
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    PID_FIELD_NUMBER: _ClassVar[int]
    UPTIME_S_FIELD_NUMBER: _ClassVar[int]
    OPEN_DEVICES_FIELD_NUMBER: _ClassVar[int]
    READS_FIELD_NUMBER: _ClassVar[int]
    protocol: str
    pid: int
    uptime_s: float
    open_devices: int
    reads: int
    def __init__(self, protocol: _Optional[str] = ..., pid: _Optional[int] = ..., uptime_s: _Optional[float] = ..., open_devices: _Optional[int] = ..., reads: _Optional[int] = ...) -> None: ...
