from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, Dict, Iterable, Optional, Set

from .config import NoteConfig


class NoteState(str, Enum):
    READY = "READY"
    ACTIVE = "ACTIVE"
    COOLDOWN = "COOLDOWN"
    ERROR = "ERROR"


@dataclass
class RuntimeNote:
    config: NoteConfig
    state: NoteState = NoteState.READY
    cooldown_until: Optional[float] = None


@dataclass(frozen=True)
class Response:
    kind: str
    pin: Optional[int] = None
    duration_ms: Optional[int] = None
    pump_state: Optional[str] = None
    error_code: Optional[str] = None


def _integer(text: str) -> Optional[int]:
    try:
        return int(text)
    except ValueError:
        return None


def parse_response(line: str) -> Response:
    parts = line.split()
    if parts == ["READY"]:
        return Response("READY")
    if len(parts) == 4 and parts[:2] == ["ACK", "PUMP"]:
        pin = _integer(parts[2])
        duration = _integer(parts[3])
        if pin is not None and duration is not None:
            return Response("ACK_PUMP", pin=pin, duration_ms=duration)
    if len(parts) == 3 and parts[:2] == ["ACK", "STOP"]:
        pin = _integer(parts[2])
        if pin is not None:
            return Response("ACK_STOP", pin=pin)
    if parts == ["ACK", "STOP_ALL"]:
        return Response("ACK_STOP_ALL")
    if len(parts) == 2 and parts[0] in {"DONE", "BUSY"}:
        pin = _integer(parts[1])
        if pin is not None:
            return Response(parts[0], pin=pin)
    if len(parts) in {2, 3} and parts[0] == "ERROR":
        return Response("ERROR", error_code=" ".join(parts[1:]))
    if len(parts) == 3 and parts[0] == "STATUS" and parts[2] in {"ON", "OFF"}:
        pin = _integer(parts[1])
        if pin is not None:
            return Response("STATUS", pin=pin, pump_state=parts[2])
    if parts == ["STATUS_END"]:
        return Response("STATUS_END")
    return Response("UNKNOWN")


class Controller:
    """Owns note state and the Arduino request/response state machine."""

    def __init__(
        self,
        notes: Iterable[NoteConfig],
        send: Callable[[str], None],
        clock: Callable[[], float],
        log: Callable[[str, str], None],
        ack_timeout_ms: int = 1000,
    ) -> None:
        runtime_notes = [RuntimeNote(note) for note in notes]
        self.notes_by_midi: Dict[int, RuntimeNote] = {
            note.config.midi_note: note for note in runtime_notes
        }
        self.notes_by_pin: Dict[int, RuntimeNote] = {
            note.config.arduino_pin: note for note in runtime_notes
        }
        self.send = send
        self.clock = clock
        self.log = log
        self.ack_timeout_seconds = ack_timeout_ms / 1000.0
        self.arduino_ready = False

        self._pump_queue: Deque[RuntimeNote] = deque()
        self._queued_midi_notes: Set[int] = set()
        self._inflight: Optional[RuntimeNote] = None
        self._ack_deadline: Optional[float] = None

        self._recovery_pins: Set[int] = set()
        self._status_pending = False
        self._status_deadline: Optional[float] = None
        self._status_results: Dict[int, str] = {}
        self._status_establishes_ready = False

    @property
    def inflight_pin(self) -> Optional[int]:
        if self._inflight is None:
            return None
        return self._inflight.config.arduino_pin

    def state_for_midi(self, midi_note: int) -> Optional[NoteState]:
        note = self.notes_by_midi.get(midi_note)
        return note.state if note is not None else None

    def request_startup_status(self) -> None:
        """Probe an already-running Arduino that will not emit READY again."""
        if self.arduino_ready or self._status_pending:
            return
        self._recovery_pins.update(self.notes_by_pin)
        self._status_establishes_ready = True
        self._start_status_request()

    def handle_midi(self, message_type: str, midi_note: int, velocity: int) -> bool:
        if message_type != "note_on":
            self.log("DEBUG", f"MIDI ignored: type={message_type} note={midi_note}")
            return False
        if velocity <= 0:
            self.log("DEBUG", f"MIDI ignored: Note On velocity=0 note={midi_note}")
            return False

        note = self.notes_by_midi.get(midi_note)
        if note is None:
            self.log("DEBUG", f"MIDI ignored: unregistered note={midi_note}")
            return False
        if not note.config.enabled:
            self.log("INFO", f"MIDI ignored: disabled note={midi_note}")
            return False
        if note.state != NoteState.READY:
            self.log(
                "INFO", f"MIDI ignored: note={midi_note} state={note.state.value}"
            )
            return False
        if (
            self._inflight is not None
            and self._inflight.config.midi_note == midi_note
        ):
            self.log("INFO", f"MIDI ignored: note={midi_note} awaiting ACK")
            return False
        if midi_note in self._queued_midi_notes:
            self.log("INFO", f"MIDI ignored: note={midi_note} already queued")
            return False

        self._pump_queue.append(note)
        self._queued_midi_notes.add(midi_note)
        self._try_send_next_pump()
        return True

    def handle_serial_line(self, line: str) -> None:
        response = parse_response(line)
        if response.kind == "READY":
            self._reset_after_arduino_ready()
        elif response.kind == "ACK_PUMP":
            self._handle_ack_pump(response)
        elif response.kind == "DONE":
            self._handle_done(response.pin)
        elif response.kind == "BUSY":
            self._handle_busy(response.pin)
        elif response.kind == "ERROR":
            self._handle_error(response.error_code or "UNKNOWN")
        elif response.kind == "STATUS":
            if self._status_pending and response.pin is not None:
                self._status_results[response.pin] = response.pump_state or "OFF"
        elif response.kind == "STATUS_END":
            self._finish_status_recovery()
        elif response.kind in {"ACK_STOP", "ACK_STOP_ALL"}:
            return
        else:
            self.log("WARNING", f"Unrecognized Arduino response: {line!r}")
            if self._inflight is not None:
                note = self._inflight
                self._clear_inflight()
                self._set_state(note, NoteState.ERROR, "invalid response")
                self._request_status(note.config.arduino_pin)
                self._try_send_next_pump()

    def tick(self) -> None:
        now = self.clock()
        for note in self.notes_by_midi.values():
            if (
                note.state == NoteState.COOLDOWN
                and note.cooldown_until is not None
                and now >= note.cooldown_until
            ):
                note.cooldown_until = None
                self._set_state(note, NoteState.READY, "cooldown complete")

        if (
            self._inflight is not None
            and self._ack_deadline is not None
            and now >= self._ack_deadline
        ):
            note = self._inflight
            self._clear_inflight()
            self._set_state(note, NoteState.ERROR, "ACK timeout")
            self._request_status(note.config.arduino_pin)
            self._try_send_next_pump()

        if (
            self._status_pending
            and self._status_deadline is not None
            and now >= self._status_deadline
        ):
            pins = sorted(self._recovery_pins)
            self.log("ERROR", f"STATUS timeout; pins remain ERROR: {pins}")
            self._status_pending = False
            self._status_deadline = None
            self._status_results.clear()
            self._recovery_pins.clear()
            self._status_establishes_ready = False

    def _try_send_next_pump(self) -> None:
        if self._inflight is not None or not self._pump_queue:
            return
        note = self._pump_queue.popleft()
        self._queued_midi_notes.discard(note.config.midi_note)
        self.send(
            f"PUMP {note.config.arduino_pin} {note.config.pump_duration_ms}"
        )
        self._inflight = note
        self._ack_deadline = self.clock() + self.ack_timeout_seconds

    def _handle_ack_pump(self, response: Response) -> None:
        target = self.notes_by_pin.get(response.pin or -1)
        matches_inflight = (
            self._inflight is not None
            and self._inflight.config.arduino_pin == response.pin
            and self._inflight.config.pump_duration_ms == response.duration_ms
        )
        if matches_inflight:
            target = self._inflight
            self._clear_inflight()
            if target is not None:
                self._set_state(target, NoteState.ACTIVE, "ACK PUMP")
            self._try_send_next_pump()
            return

        self.log(
            "WARNING",
            f"Unexpected ACK PUMP pin={response.pin} duration={response.duration_ms}",
        )
        if target is not None:
            self._set_state(target, NoteState.ACTIVE, "late or unmatched ACK PUMP")
        if self._inflight is not None:
            inflight = self._inflight
            self._clear_inflight()
            self._set_state(inflight, NoteState.ERROR, "mismatched ACK")
            self._request_status(inflight.config.arduino_pin)
            self._try_send_next_pump()

    def _handle_done(self, pin: Optional[int]) -> None:
        note = self.notes_by_pin.get(pin or -1)
        if note is None:
            self.log("WARNING", f"DONE received for unconfigured pin={pin}")
            return
        note.cooldown_until = self.clock() + note.config.cooldown_ms / 1000.0
        self._set_state(note, NoteState.COOLDOWN, "DONE")

    def _handle_busy(self, pin: Optional[int]) -> None:
        note = self.notes_by_pin.get(pin or -1)
        if self._inflight is not None and self._inflight.config.arduino_pin == pin:
            note = self._inflight
            self._clear_inflight()
        if note is None:
            self.log("WARNING", f"BUSY received for unconfigured pin={pin}")
            return
        self._set_state(note, NoteState.ERROR, "BUSY")
        self._request_status(note.config.arduino_pin)
        self._try_send_next_pump()

    def _handle_error(self, error_code: str) -> None:
        self.log("ERROR", f"Arduino error: {error_code}")
        if self._inflight is None:
            return
        note = self._inflight
        self._clear_inflight()
        self._set_state(note, NoteState.ERROR, f"Arduino error: {error_code}")
        self._request_status(note.config.arduino_pin)
        self._try_send_next_pump()

    def _request_status(self, pin: int) -> None:
        self._recovery_pins.add(pin)
        if self._status_pending:
            return
        self._start_status_request()

    def _start_status_request(self) -> None:
        self._status_pending = True
        self._status_results.clear()
        self._status_deadline = self.clock() + self.ack_timeout_seconds
        self.send("STATUS")

    def _finish_status_recovery(self) -> None:
        if not self._status_pending:
            self.log("DEBUG", "Unexpected STATUS_END")
            return
        complete_status = all(
            pin in self._status_results for pin in self._recovery_pins
        )
        for pin in sorted(self._recovery_pins):
            note = self.notes_by_pin.get(pin)
            pump_state = self._status_results.get(pin)
            if note is None:
                continue
            if pump_state == "ON":
                note.cooldown_until = None
                self._set_state(note, NoteState.ACTIVE, "STATUS reports ON")
            elif pump_state == "OFF":
                note.cooldown_until = None
                self._set_state(note, NoteState.READY, "STATUS reports OFF")
            else:
                self.log("ERROR", f"STATUS did not include pin={pin}; remains ERROR")
        if self._status_establishes_ready and complete_status:
            self.arduino_ready = True
            self.log("INFO", "Arduino connection established by STATUS")
        self._status_pending = False
        self._status_deadline = None
        self._status_results.clear()
        self._recovery_pins.clear()
        self._status_establishes_ready = False

    def _reset_after_arduino_ready(self) -> None:
        self.arduino_ready = True
        self._pump_queue.clear()
        self._queued_midi_notes.clear()
        self._clear_inflight()
        self._recovery_pins.clear()
        self._status_pending = False
        self._status_deadline = None
        self._status_results.clear()
        self._status_establishes_ready = False
        for note in self.notes_by_midi.values():
            note.cooldown_until = None
            self._set_state(note, NoteState.READY, "Arduino READY")
        self.log("INFO", "Arduino is READY")

    def _clear_inflight(self) -> None:
        self._inflight = None
        self._ack_deadline = None

    def _set_state(
        self, note: RuntimeNote, state: NoteState, reason: str
    ) -> None:
        old_state = note.state
        note.state = state
        if old_state != state:
            self.log(
                "INFO",
                f"State {note.config.name} note={note.config.midi_note} "
                f"pin={note.config.arduino_pin}: {old_state.value} -> "
                f"{state.value} ({reason})",
            )
