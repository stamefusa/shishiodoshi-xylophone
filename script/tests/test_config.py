import json

import pytest

from shishiodoshi_client.config import ConfigurationError, load_config


def valid_config():
    return {
        "midiInput": "Keyboard",
        "serialPort": "/dev/cu.usbmodem1",
        "baudRate": 115200,
        "ackTimeoutMs": 1000,
        "startupTimeoutMs": 5000,
        "notes": [
            {
                "name": "C",
                "midiNote": 60,
                "arduinoPin": 2,
                "pumpDurationMs": 300,
                "cooldownMs": 5000,
                "enabled": True,
            }
        ],
    }


def write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_loads_valid_config(tmp_path):
    config = load_config(write_config(tmp_path, valid_config()))

    assert config.midi_input == "Keyboard"
    assert config.serial_port == "/dev/cu.usbmodem1"
    assert config.notes[0].midi_note == 60


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("arduinoPin", 10, "arduinoPin must be between 2 and 9"),
        ("pumpDurationMs", 9, "pumpDurationMs must be between 10 and 5000"),
        ("pumpDurationMs", 5001, "pumpDurationMs must be between 10 and 5000"),
        ("midiNote", 128, "midiNote must be between 0 and 127"),
    ],
)
def test_rejects_unsafe_note_values(tmp_path, field, value, message):
    data = valid_config()
    data["notes"][0][field] = value

    with pytest.raises(ConfigurationError, match=message):
        load_config(write_config(tmp_path, data))


def test_rejects_duplicate_midi_notes(tmp_path):
    data = valid_config()
    second = dict(data["notes"][0], name="D", arduinoPin=3)
    data["notes"].append(second)

    with pytest.raises(ConfigurationError, match="midiNote values must be unique"):
        load_config(write_config(tmp_path, data))


def test_rejects_duplicate_arduino_pins(tmp_path):
    data = valid_config()
    second = dict(data["notes"][0], name="D", midiNote=62)
    data["notes"].append(second)

    with pytest.raises(ConfigurationError, match="arduinoPin values must be unique"):
        load_config(write_config(tmp_path, data))


def test_rejects_missing_required_note_field(tmp_path):
    data = valid_config()
    del data["notes"][0]["enabled"]

    with pytest.raises(ConfigurationError, match=r"notes\[0\]\.enabled"):
        load_config(write_config(tmp_path, data))

