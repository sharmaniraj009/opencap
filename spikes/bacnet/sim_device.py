"""bacpypes3 device for the BACnet spike (#15): the plan for tests/helpers/bacnet_sim.py in miniature.

One BACnet/IP device on 127.0.0.1:<port> with every object type of the driver subset (AI, AO, AV,
BI, BO, BV, MI, MO, MV), optional extra analog values (to force segmentation), and a write
counter: WriteProperty / WritePropertyMultiple are refused and counted, so a test can show a client
never writes. Optionally a BBMD (accepts foreign-device registrations).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

from bacpypes3.app import Application
from bacpypes3.argparse import SimpleArgumentParser
from bacpypes3.errors import PropertyError
from bacpypes3.local.analog import AnalogInputObject, AnalogOutputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject, BinaryValueObject
from bacpypes3.local.multistate import MultiStateInputObject, MultiStateOutputObject, MultiStateValueObject

DEVICE_INSTANCE = 1001


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class SimDevice:
    app: Application
    port: int
    objects: dict[str, object] = field(default_factory=dict)
    write_attempts: int = 0

    @property
    def addr(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def close(self) -> None:
        self.app.close()


def make_device(*, port: int | None = None, instance: int = DEVICE_INSTANCE, extra_values: int = 0,
                bbmd: bool = False) -> SimDevice:
    port = port or free_udp_port()
    argv = ["--name", "SimDevice", "--instance", str(instance), "--address", f"127.0.0.1/8:{port}"]
    if bbmd:
        argv += ["--bbmd", f"127.0.0.1:{port}"]
    app = Application.from_args(SimpleArgumentParser().parse_args(argv))
    sim = SimDevice(app, port)
    ok = [0, 0, 0, 0]
    objs = {
        "ai1": AnalogInputObject(objectIdentifier=("analogInput", 1), objectName="TT-101", presentValue=21.5,
                                 statusFlags=ok, units="degreesCelsius", covIncrement=0.5, outOfService=False,
                                 reliability="noFaultDetected"),
        "ai2": AnalogInputObject(objectIdentifier=("analogInput", 2), objectName="PT-102", presentValue=0.0,
                                 statusFlags=[0, 1, 0, 0], units="kilopascals", reliability="noSensor"),
        "ao1": AnalogOutputObject(objectIdentifier=("analogOutput", 1), objectName="FCV-101", presentValue=40.0,
                                  statusFlags=ok, units="percent", relinquishDefault=0.0),
        "av1": AnalogValueObject(objectIdentifier=("analogValue", 1), objectName="SP-101", presentValue=22.0,
                                 statusFlags=[0, 0, 0, 1], units="degreesCelsius", outOfService=True),
        "bi1": BinaryInputObject(objectIdentifier=("binaryInput", 1), objectName="PS-101", presentValue="active",
                                 statusFlags=[1, 0, 0, 0]),
        "bo1": BinaryOutputObject(objectIdentifier=("binaryOutput", 1), objectName="P-101", presentValue="inactive",
                                  statusFlags=ok, relinquishDefault="inactive"),
        "bv1": BinaryValueObject(objectIdentifier=("binaryValue", 1), objectName="MODE-AUTO", presentValue="active",
                                 statusFlags=ok),
        "mi1": MultiStateInputObject(objectIdentifier=("multiStateInput", 1), objectName="STATE-101",
                                     presentValue=2, numberOfStates=3, statusFlags=ok),
        "mo1": MultiStateOutputObject(objectIdentifier=("multiStateOutput", 1), objectName="FAN-SPEED",
                                      presentValue=1, numberOfStates=3, statusFlags=ok, relinquishDefault=1),
        "mv1": MultiStateValueObject(objectIdentifier=("multiStateValue", 1), objectName="SEASON",
                                     presentValue=3, numberOfStates=4, statusFlags=ok),
    }
    for i in range(extra_values):
        objs[f"av{100 + i}"] = AnalogValueObject(objectIdentifier=("analogValue", 100 + i), objectName=f"AV-{100 + i}",
                                                 presentValue=float(i), statusFlags=ok, units="noUnits")
    for obj in objs.values():
        app.add_object(obj)
    sim.objects = objs

    async def refuse_write(apdu):                     # read-only device: count and refuse every write
        sim.write_attempts += 1
        raise PropertyError("writeAccessDenied")

    app.do_WritePropertyRequest = refuse_write
    app.do_WritePropertyMultipleRequest = refuse_write
    return sim
