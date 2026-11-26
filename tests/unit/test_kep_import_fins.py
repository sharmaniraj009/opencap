"""KEPServerEX "Omron FINS Ethernet" devices import as `omron_fins` (#54): UDP (KEPServerEX's
only FINS transport), the model family, and the node/request-size properties found by key
suffix. The property keys are not verified against a real export, so every device is flagged."""

from __future__ import annotations

from opencap.config import kep_import
from opencap.config.models import Device


def _dev(**props) -> dict:
    return {"servermain.DEVICE_ID_STRING": "192.0.2.20", **props}


def test_fins_devices_map_to_omron_fins():
    assert kep_import.DRIVER_MAPPERS[kep_import.KEP_FINS] is kep_import._map_fins
    m = kep_import._map_fins({}, _dev(**{
        "servermain.DEVICE_MODEL": "CJ2M CPU", "omron_fins_ethernet.DEVICE_DESTINATION_NODE": 20,
        "omron_fins_ethernet.DEVICE_SOURCE_NODE": 7, "omron_fins_ethernet.DEVICE_REQUEST_SIZE": 256,
        "omron_fins_ethernet.DEVICE_PORT_NUMBER": 9601}), "Plant A/PLC1")
    assert (m.driver, m.host, m.port) == ("omron_fins", "192.0.2.20", 9601)
    assert m.params == {"transport": "udp", "model": "CJ2", "dest_node": 20, "src_node": 7, "request_size": 256}
    assert any("not verified" in p for p in m.problems)
    Device(id="Plant A/PLC1", channel="Plant A", name="PLC1", driver=m.driver, host=m.host, port=m.port,
           params=m.params, tags=[])                          # the config model accepts it


def test_unknown_model_defaults_to_cj1_and_is_flagged():
    m = kep_import._map_fins({}, _dev(**{"servermain.DEVICE_MODEL": 3}), "Plant A/PLC2")
    assert (m.port, m.params["model"]) == (9600, "CJ1")
    assert any("not recognised" in p for p in m.problems)
