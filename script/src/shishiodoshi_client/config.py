from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigurationError(ValueError):
    """Raised when a client configuration is missing or unsafe."""


@dataclass(frozen=True)
class NoteConfig:
    name: str
    midi_note: int
    arduino_pin: int
    pump_duration_ms: int
    cooldown_ms: int
    enabled: bool


@dataclass(frozen=True)
class AppConfig:
    midi_input: str
    serial_port: str
    baud_rate: int
    ack_timeout_ms: int
    startup_timeout_ms: int
    notes: List[NoteConfig]

    def with_device_overrides(
        self, midi_input: Optional[str], serial_port: Optional[str]
    ) -> "AppConfig":
        return replace(
            self,
            midi_input=midi_input if midi_input is not None else self.midi_input,
            serial_port=serial_port if serial_port is not None else self.serial_port,
        )


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{field} must be an integer")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{field} must be a boolean")
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{field} must be a string")
    return value


def _required(mapping: Dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ConfigurationError(f"missing required field: {context}{key}")
    return mapping[key]


def _parse_note(raw: Any, index: int) -> NoteConfig:
    context = f"notes[{index}]."
    if not isinstance(raw, dict):
        raise ConfigurationError(f"notes[{index}] must be an object")

    name = _require_string(_required(raw, "name", context), context + "name")
    midi_note = _require_int(
        _required(raw, "midiNote", context), context + "midiNote"
    )
    arduino_pin = _require_int(
        _required(raw, "arduinoPin", context), context + "arduinoPin"
    )
    duration = _require_int(
        _required(raw, "pumpDurationMs", context), context + "pumpDurationMs"
    )
    cooldown = _require_int(
        _required(raw, "cooldownMs", context), context + "cooldownMs"
    )
    enabled = _require_bool(
        _required(raw, "enabled", context), context + "enabled"
    )

    if not name.strip():
        raise ConfigurationError(f"{context}name must not be empty")
    if not 0 <= midi_note <= 127:
        raise ConfigurationError(f"{context}midiNote must be between 0 and 127")
    if not 2 <= arduino_pin <= 9:
        raise ConfigurationError(f"{context}arduinoPin must be between 2 and 9")
    if not 10 <= duration <= 5000:
        raise ConfigurationError(
            f"{context}pumpDurationMs must be between 10 and 5000"
        )
    if cooldown < 0:
        raise ConfigurationError(f"{context}cooldownMs must be 0 or greater")

    return NoteConfig(
        name=name,
        midi_note=midi_note,
        arduino_pin=arduino_pin,
        pump_duration_ms=duration,
        cooldown_ms=cooldown,
        enabled=enabled,
    )


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("r", encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be an object")

    midi_input = _require_string(
        _required(raw, "midiInput", ""), "midiInput"
    )
    serial_port = _require_string(
        _required(raw, "serialPort", ""), "serialPort"
    )
    baud_rate = _require_int(raw.get("baudRate", 115200), "baudRate")
    ack_timeout = _require_int(raw.get("ackTimeoutMs", 1000), "ackTimeoutMs")
    startup_timeout = _require_int(
        raw.get("startupTimeoutMs", 5000), "startupTimeoutMs"
    )
    notes_raw = _required(raw, "notes", "")
    if not isinstance(notes_raw, list) or not notes_raw:
        raise ConfigurationError("notes must be a non-empty array")
    if len(notes_raw) > 8:
        raise ConfigurationError("notes must contain at most 8 entries")

    notes = [_parse_note(item, index) for index, item in enumerate(notes_raw)]
    midi_notes = [note.midi_note for note in notes]
    pins = [note.arduino_pin for note in notes]
    if len(midi_notes) != len(set(midi_notes)):
        raise ConfigurationError("midiNote values must be unique")
    if len(pins) != len(set(pins)):
        raise ConfigurationError("arduinoPin values must be unique")
    if baud_rate != 115200:
        raise ConfigurationError("baudRate must be 115200")
    if ack_timeout <= 0:
        raise ConfigurationError("ackTimeoutMs must be greater than 0")
    if startup_timeout <= 0:
        raise ConfigurationError("startupTimeoutMs must be greater than 0")

    return AppConfig(
        midi_input=midi_input,
        serial_port=serial_port,
        baud_rate=baud_rate,
        ack_timeout_ms=ack_timeout,
        startup_timeout_ms=startup_timeout,
        notes=notes,
    )


def require_device_settings(config: AppConfig) -> None:
    if not config.midi_input.strip():
        raise ConfigurationError(
            "midiInput is empty; set it in config.json or pass --midi-input"
        )
    if not config.serial_port.strip():
        raise ConfigurationError(
            "serialPort is empty; set it in config.json or pass --serial-port"
        )
