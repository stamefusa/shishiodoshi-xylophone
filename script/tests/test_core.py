from dataclasses import replace

from shishiodoshi_client.config import NoteConfig
from shishiodoshi_client.core import Controller, NoteState, parse_response


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def note(
    name="C",
    midi_note=60,
    pin=2,
    duration=300,
    cooldown=5000,
    enabled=True,
):
    return NoteConfig(name, midi_note, pin, duration, cooldown, enabled)


def make_controller(notes=None):
    sent = []
    logs = []
    clock = FakeClock()
    controller = Controller(
        notes=notes or [note()],
        send=sent.append,
        clock=clock,
        log=lambda level, message: logs.append((level, message)),
        ack_timeout_ms=1000,
    )
    return controller, sent, logs, clock


def test_parse_all_protocol_responses():
    assert parse_response("READY").kind == "READY"
    assert parse_response("ACK PUMP 2 300").kind == "ACK_PUMP"
    assert parse_response("ACK STOP 2").kind == "ACK_STOP"
    assert parse_response("ACK STOP_ALL").kind == "ACK_STOP_ALL"
    assert parse_response("DONE 2").kind == "DONE"
    assert parse_response("BUSY 2").kind == "BUSY"
    assert parse_response("ERROR INVALID_PIN 10").kind == "ERROR"
    assert parse_response("STATUS 2 ON").pump_state == "ON"
    assert parse_response("STATUS_END").kind == "STATUS_END"
    assert parse_response("nonsense").kind == "UNKNOWN"


def test_note_on_runs_active_cooldown_ready_cycle():
    controller, sent, _, clock = make_controller()

    assert controller.handle_midi("note_on", 60, 100)
    assert sent == ["PUMP 2 300"]
    assert controller.state_for_midi(60) == NoteState.READY

    controller.handle_serial_line("ACK PUMP 2 300")
    assert controller.state_for_midi(60) == NoteState.ACTIVE

    controller.handle_serial_line("DONE 2")
    assert controller.state_for_midi(60) == NoteState.COOLDOWN
    assert not controller.handle_midi("note_on", 60, 100)

    clock.advance(5.0)
    controller.tick()
    assert controller.state_for_midi(60) == NoteState.READY


def test_pump_requests_are_serialized_until_ack():
    notes = [note(), note("D", 62, 3)]
    controller, sent, _, _ = make_controller(notes)

    controller.handle_midi("note_on", 60, 100)
    controller.handle_midi("note_on", 62, 100)
    assert sent == ["PUMP 2 300"]

    controller.handle_serial_line("ACK PUMP 2 300")
    assert sent == ["PUMP 2 300", "PUMP 3 300"]
    controller.handle_serial_line("ACK PUMP 3 300")
    assert controller.state_for_midi(60) == NoteState.ACTIVE
    assert controller.state_for_midi(62) == NoteState.ACTIVE


def test_ignores_note_off_velocity_zero_unknown_disabled_and_queued_note():
    disabled = replace(note("D", 62, 3), enabled=False)
    controller, sent, _, _ = make_controller([note(), disabled])

    assert not controller.handle_midi("note_off", 60, 64)
    assert not controller.handle_midi("note_on", 60, 0)
    assert not controller.handle_midi("note_on", 61, 100)
    assert not controller.handle_midi("note_on", 62, 100)
    assert controller.handle_midi("note_on", 60, 100)
    assert not controller.handle_midi("note_on", 60, 100)
    assert sent == ["PUMP 2 300"]


def test_busy_recovers_active_from_status():
    controller, sent, _, _ = make_controller()
    controller.handle_midi("note_on", 60, 100)

    controller.handle_serial_line("BUSY 2")
    assert controller.state_for_midi(60) == NoteState.ERROR
    assert sent[-1] == "STATUS"

    controller.handle_serial_line("STATUS 2 ON")
    controller.handle_serial_line("STATUS_END")
    assert controller.state_for_midi(60) == NoteState.ACTIVE


def test_error_recovers_ready_from_status():
    controller, sent, _, _ = make_controller()
    controller.handle_midi("note_on", 60, 100)

    controller.handle_serial_line("ERROR INVALID_DURATION 300")
    assert sent[-1] == "STATUS"
    controller.handle_serial_line("STATUS 2 OFF")
    controller.handle_serial_line("STATUS_END")
    assert controller.state_for_midi(60) == NoteState.READY


def test_ack_timeout_requests_status_and_status_timeout_keeps_error():
    controller, sent, logs, clock = make_controller()
    controller.handle_midi("note_on", 60, 100)

    clock.advance(1.0)
    controller.tick()
    assert controller.state_for_midi(60) == NoteState.ERROR
    assert sent == ["PUMP 2 300", "STATUS"]

    clock.advance(1.0)
    controller.tick()
    assert controller.state_for_midi(60) == NoteState.ERROR
    assert any("STATUS timeout" in message for _, message in logs)


def test_unknown_response_during_ack_wait_enters_recovery():
    controller, sent, _, _ = make_controller()
    controller.handle_midi("note_on", 60, 100)

    controller.handle_serial_line("BROKEN RESPONSE")

    assert controller.state_for_midi(60) == NoteState.ERROR
    assert sent[-1] == "STATUS"


def test_ready_resets_all_states_and_pending_work():
    notes = [note(), note("D", 62, 3)]
    controller, sent, _, _ = make_controller(notes)
    controller.handle_midi("note_on", 60, 100)
    controller.handle_midi("note_on", 62, 100)
    controller.handle_serial_line("ACK PUMP 2 300")

    controller.handle_serial_line("READY")

    assert controller.arduino_ready
    assert controller.state_for_midi(60) == NoteState.READY
    assert controller.state_for_midi(62) == NoteState.READY
    assert controller.inflight_pin is None
    assert sent == ["PUMP 2 300", "PUMP 3 300"]


def test_complete_startup_status_establishes_connection_without_ready():
    notes = [note(), note("D", 62, 3)]
    controller, sent, _, _ = make_controller(notes)

    controller.request_startup_status()
    assert sent == ["STATUS"]
    controller.handle_serial_line("STATUS 2 OFF")
    controller.handle_serial_line("STATUS 3 OFF")
    controller.handle_serial_line("STATUS_END")

    assert controller.arduino_ready
    assert controller.state_for_midi(60) == NoteState.READY
    assert controller.state_for_midi(62) == NoteState.READY


def test_incomplete_startup_status_does_not_establish_connection():
    notes = [note(), note("D", 62, 3)]
    controller, _, _, _ = make_controller(notes)

    controller.request_startup_status()
    controller.handle_serial_line("STATUS 2 OFF")
    controller.handle_serial_line("STATUS_END")

    assert not controller.arduino_ready
    assert controller.state_for_midi(62) == NoteState.READY
