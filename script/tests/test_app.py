import argparse
import json

import pytest

from shishiodoshi_client import app


def test_serial_disconnect_still_attempts_stop_all(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "midiInput": "Keyboard",
                "serialPort": "/dev/fake",
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
        ),
        encoding="utf-8",
    )
    sent = []

    class FakeTransport:
        def __init__(self, port, baud_rate, events):
            self.events = events
            self.is_open = True

        def start(self):
            self.events.put(("serial_line", "READY"))
            self.events.put(("serial_error", RuntimeError("disconnected")))

        def send(self, command):
            sent.append(command)

        def close(self):
            self.is_open = False

    class FakeMidiPort:
        def close(self):
            pass

    monkeypatch.setattr(app, "SerialTransport", FakeTransport)
    monkeypatch.setattr(app, "validate_connected_devices", lambda *args: None)
    monkeypatch.setattr(app.mido, "open_input", lambda *args, **kwargs: FakeMidiPort())
    monkeypatch.setattr(app.time, "sleep", lambda _: None)
    args = argparse.Namespace(
        config=config_path,
        midi_input=None,
        serial_port=None,
        log_level="INFO",
        list_devices=False,
    )

    with pytest.raises(app.ClientError, match="serial connection lost"):
        app.run(args)

    assert "STOP_ALL" in sent

