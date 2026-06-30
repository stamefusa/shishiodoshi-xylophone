from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import mido
import rtmidi
import serial
from serial.tools import list_ports

from .config import ConfigurationError, load_config, require_device_settings
from .core import Controller


LOGGER = logging.getLogger("shishiodoshi")
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.json"
Event = Tuple[str, Any]


class ClientError(RuntimeError):
    """Raised when a connected device cannot safely run the client."""


class SerialTransport:
    def __init__(
        self,
        port: str,
        baud_rate: int,
        events: "queue.Queue[Event]",
    ) -> None:
        self._events = events
        self._serial = serial.Serial(
            port=port,
            baudrate=baud_rate,
            timeout=0.1,
            write_timeout=1.0,
        )
        self._write_lock = threading.Lock()
        self._stopping = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop,
            name="arduino-serial-reader",
            daemon=True,
        )

    @property
    def is_open(self) -> bool:
        return bool(self._serial.is_open)

    def start(self) -> None:
        self._reader.start()

    def send(self, command: str) -> None:
        payload = (command + "\n").encode("ascii")
        try:
            with self._write_lock:
                self._serial.write(payload)
                self._serial.flush()
        except (OSError, serial.SerialException, serial.SerialTimeoutException) as exc:
            raise ClientError(f"serial write failed: {exc}") from exc

    def close(self) -> None:
        self._stopping.set()
        if self._serial.is_open:
            self._serial.close()
        if self._reader.is_alive():
            self._reader.join(timeout=1.0)

    def _read_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                data = self._serial.readline()
            except (OSError, serial.SerialException) as exc:
                if not self._stopping.is_set():
                    self._events.put(("serial_error", exc))
                return
            if data:
                line = data.decode("utf-8", errors="replace").rstrip("\r\n")
                self._events.put(("serial_line", line))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send Arduino pump commands from MIDI Note On events."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"configuration JSON (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("--midi-input", help="override config midiInput")
    parser.add_argument("--serial-port", help="override config serialPort")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="list MIDI inputs and serial ports, then exit",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def available_midi_inputs() -> List[str]:
    return list(mido.get_input_names())


def available_serial_ports() -> List[str]:
    return [port.device for port in list_ports.comports()]


def print_devices() -> None:
    midi_inputs = available_midi_inputs()
    ports = list(list_ports.comports())
    print("MIDI inputs:")
    if midi_inputs:
        for name in midi_inputs:
            print(f"  {name}")
    else:
        print("  (none)")
    print("Serial ports:")
    if ports:
        for port in ports:
            description = port.description or "no description"
            print(f"  {port.device}  [{description}]")
    else:
        print("  (none)")


def validate_connected_devices(midi_input: str, serial_port: str) -> None:
    midi_inputs = available_midi_inputs()
    if midi_input not in midi_inputs:
        raise ClientError(
            f"MIDI input not found: {midi_input!r}; use --list-devices"
        )
    serial_ports = available_serial_ports()
    if serial_port not in serial_ports:
        raise ClientError(
            f"serial port not found: {serial_port!r}; use --list-devices"
        )


def _core_log(level: str, message: str) -> None:
    LOGGER.log(getattr(logging, level), message)


def _send_logged(transport: SerialTransport, command: str) -> None:
    LOGGER.info("TX %s", command)
    transport.send(command)


def _handle_event(controller: Controller, event: Event) -> None:
    event_type, payload = event
    if event_type == "serial_line":
        LOGGER.info("RX %s", payload)
        controller.handle_serial_line(payload)
    elif event_type == "serial_error":
        raise ClientError(f"serial connection lost: {payload}")
    elif event_type == "midi":
        message_type, note, velocity = payload
        LOGGER.debug(
            "MIDI type=%s note=%s velocity=%s", message_type, note, velocity
        )
        controller.handle_midi(message_type, note, velocity)


def _wait_for_ready(
    controller: Controller,
    events: "queue.Queue[Event]",
    timeout_ms: int,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    controller.request_startup_status()
    while not controller.arduino_ready:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientError(
                "Arduino did not respond with READY or a complete STATUS "
                f"within {timeout_ms} ms"
            )
        try:
            event = events.get(timeout=min(0.1, remaining))
        except queue.Empty:
            controller.tick()
            controller.request_startup_status()
            continue
        _handle_event(controller, event)
        controller.tick()
        controller.request_startup_status()


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config).with_device_overrides(
        args.midi_input, args.serial_port
    )
    require_device_settings(config)
    validate_connected_devices(config.midi_input, config.serial_port)

    events: "queue.Queue[Event]" = queue.Queue()
    transport: Optional[SerialTransport] = None
    midi_port: Optional[Any] = None
    shutdown_reason = "normal exit"

    try:
        LOGGER.info(
            "Opening serial port %s at %d bps",
            config.serial_port,
            config.baud_rate,
        )
        transport = SerialTransport(config.serial_port, config.baud_rate, events)
        transport.start()

        controller = Controller(
            notes=config.notes,
            send=lambda command: _send_logged(transport, command),
            clock=time.monotonic,
            log=_core_log,
            ack_timeout_ms=config.ack_timeout_ms,
        )
        _wait_for_ready(controller, events, config.startup_timeout_ms)

        def midi_callback(message: Any) -> None:
            events.put(
                (
                    "midi",
                    (
                        message.type,
                        getattr(message, "note", -1),
                        getattr(message, "velocity", 0),
                    ),
                )
            )

        LOGGER.info("Opening MIDI input %s", config.midi_input)
        midi_port = mido.open_input(config.midi_input, callback=midi_callback)
        LOGGER.info("Client started; press Ctrl+C to stop all pumps and exit")

        next_device_check = time.monotonic() + 1.0
        while True:
            try:
                event = events.get(timeout=0.05)
            except queue.Empty:
                event = None
            if event is not None:
                _handle_event(controller, event)
            controller.tick()

            now = time.monotonic()
            if now >= next_device_check:
                if config.midi_input not in available_midi_inputs():
                    shutdown_reason = "MIDI input disconnected"
                    raise ClientError(shutdown_reason)
                if (
                    not transport.is_open
                    or config.serial_port not in available_serial_ports()
                ):
                    shutdown_reason = "serial port disconnected"
                    raise ClientError(shutdown_reason)
                next_device_check = now + 1.0
    except KeyboardInterrupt:
        shutdown_reason = "Ctrl+C"
        LOGGER.info("Ctrl+C received")
        return 0
    finally:
        LOGGER.info("Shutting down: %s", shutdown_reason)
        if transport is not None and transport.is_open:
            try:
                _send_logged(transport, "STOP_ALL")
                time.sleep(0.1)
            except ClientError as exc:
                LOGGER.warning("Could not send STOP_ALL: %s", exc)
        if midi_port is not None:
            try:
                midi_port.close()
            except Exception as exc:  # Third-party backend exceptions vary.
                LOGGER.warning("Could not close MIDI input cleanly: %s", exc)
        if transport is not None:
            transport.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    try:
        if args.list_devices:
            print_devices()
            return 0
        return run(args)
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return 2
    except (ClientError, OSError, serial.SerialException, rtmidi.RtMidiError) as exc:
        LOGGER.error("Client error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
