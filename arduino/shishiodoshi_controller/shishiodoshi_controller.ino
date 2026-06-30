#include <Arduino.h>
#include <stdint.h>
#include <string.h>

// MOSFETモジュールの入力論理が逆の場合は、この2つを入れ替える。
constexpr uint8_t PUMP_ON_LEVEL = HIGH;
constexpr uint8_t PUMP_OFF_LEVEL = LOW;

constexpr uint8_t PUMP_COUNT = 8;
constexpr uint8_t FIRST_PUMP_PIN = 2;
constexpr uint32_t MIN_DURATION_MS = 10;
constexpr uint32_t MAX_DURATION_MS = 5000;
constexpr unsigned long SERIAL_BAUD_RATE = 115200UL;

// 終端文字を含めて64バイト。長すぎる行は改行まで読み捨てる。
constexpr size_t RECEIVE_BUFFER_SIZE = 64;
char receiveBuffer[RECEIVE_BUFFER_SIZE];
size_t receiveLength = 0;
bool discardingLongLine = false;

struct PumpState {
  uint8_t pin;
  bool active;
  uint32_t startedAt;
  uint32_t durationMs;
};

PumpState pumps[PUMP_COUNT];

int8_t pumpIndexForPin(long pin) {
  if (pin < FIRST_PUMP_PIN || pin >= FIRST_PUMP_PIN + PUMP_COUNT) {
    return -1;
  }
  return static_cast<int8_t>(pin - FIRST_PUMP_PIN);
}

// 数字以外やlongの範囲を超える値を、部分的に受理しないための変換処理。
bool parseLongStrict(const char *text, long &value) {
  if (text == nullptr || *text == '\0') {
    return false;
  }

  bool negative = false;
  if (*text == '-') {
    negative = true;
    ++text;
    if (*text == '\0') {
      return false;
    }
  }

  // Unoのlongに合わせて、ホスト環境でも明示的に32ビット範囲へ制限する。
  const uint32_t positiveLimit = INT32_MAX;
  const uint32_t limit = negative ? positiveLimit + 1UL : positiveLimit;
  uint32_t magnitude = 0;

  while (*text != '\0') {
    if (*text < '0' || *text > '9') {
      return false;
    }

    const uint8_t digit = static_cast<uint8_t>(*text - '0');
    if (magnitude > (limit - digit) / 10UL) {
      return false;
    }
    magnitude = magnitude * 10UL + digit;
    ++text;
  }

  if (negative) {
    if (magnitude == positiveLimit + 1UL) {
      value = -2147483647L - 1L;
    } else {
      value = -static_cast<long>(magnitude);
    }
  } else {
    value = static_cast<long>(magnitude);
  }
  return true;
}

void setPumpOff(uint8_t index) {
  digitalWrite(pumps[index].pin, PUMP_OFF_LEVEL);
  pumps[index].active = false;
  pumps[index].startedAt = 0;
  pumps[index].durationMs = 0;
}

void printDone(uint8_t index) {
  Serial.print(F("DONE "));
  Serial.println(pumps[index].pin);
}

// 差分で比較することで、millis()が約49日で周回しても停止できる。
void updatePumps() {
  const uint32_t now = millis();
  for (uint8_t i = 0; i < PUMP_COUNT; ++i) {
    if (pumps[i].active &&
        static_cast<uint32_t>(now - pumps[i].startedAt) >= pumps[i].durationMs) {
      setPumpOff(i);
      printDone(i);
    }
  }
}

void printInvalidFormat() {
  Serial.println(F("ERROR INVALID_FORMAT"));
}

void handlePumpCommand(char *savePointer) {
  char *pinText = strtok_r(nullptr, " \t", &savePointer);
  char *durationText = strtok_r(nullptr, " \t", &savePointer);
  char *extra = strtok_r(nullptr, " \t", &savePointer);
  if (pinText == nullptr || durationText == nullptr || extra != nullptr) {
    printInvalidFormat();
    return;
  }

  long pin;
  long duration;
  if (!parseLongStrict(pinText, pin) || !parseLongStrict(durationText, duration)) {
    printInvalidFormat();
    return;
  }

  const int8_t index = pumpIndexForPin(pin);
  if (index < 0) {
    Serial.print(F("ERROR INVALID_PIN "));
    Serial.println(pin);
    return;
  }
  if (duration < static_cast<long>(MIN_DURATION_MS) ||
      duration > static_cast<long>(MAX_DURATION_MS)) {
    Serial.print(F("ERROR INVALID_DURATION "));
    Serial.println(duration);
    return;
  }
  if (pumps[index].active) {
    Serial.print(F("BUSY "));
    Serial.println(pin);
    return;
  }

  pumps[index].startedAt = millis();
  pumps[index].durationMs = static_cast<uint32_t>(duration);
  pumps[index].active = true;
  digitalWrite(pumps[index].pin, PUMP_ON_LEVEL);

  Serial.print(F("ACK PUMP "));
  Serial.print(pin);
  Serial.print(' ');
  Serial.println(duration);
}

void handleStopCommand(char *savePointer) {
  char *pinText = strtok_r(nullptr, " \t", &savePointer);
  char *extra = strtok_r(nullptr, " \t", &savePointer);
  if (pinText == nullptr || extra != nullptr) {
    printInvalidFormat();
    return;
  }

  long pin;
  if (!parseLongStrict(pinText, pin)) {
    printInvalidFormat();
    return;
  }

  const int8_t index = pumpIndexForPin(pin);
  if (index < 0) {
    Serial.print(F("ERROR INVALID_PIN "));
    Serial.println(pin);
    return;
  }

  const bool wasActive = pumps[index].active;
  setPumpOff(index);
  Serial.print(F("ACK STOP "));
  Serial.println(pin);
  if (wasActive) {
    printDone(index);
  }
}

void handleStopAllCommand(char *savePointer) {
  if (strtok_r(nullptr, " \t", &savePointer) != nullptr) {
    printInvalidFormat();
    return;
  }

  bool wasActive[PUMP_COUNT];
  for (uint8_t i = 0; i < PUMP_COUNT; ++i) {
    wasActive[i] = pumps[i].active;
    setPumpOff(i);
  }

  Serial.println(F("ACK STOP_ALL"));
  for (uint8_t i = 0; i < PUMP_COUNT; ++i) {
    if (wasActive[i]) {
      printDone(i);
    }
  }
}

void handleStatusCommand(char *savePointer) {
  if (strtok_r(nullptr, " \t", &savePointer) != nullptr) {
    printInvalidFormat();
    return;
  }

  for (uint8_t i = 0; i < PUMP_COUNT; ++i) {
    Serial.print(F("STATUS "));
    Serial.print(pumps[i].pin);
    Serial.print(' ');
    Serial.println(pumps[i].active ? F("ON") : F("OFF"));
  }
  Serial.println(F("STATUS_END"));
}

void processCommand(char *line) {
  char *savePointer = nullptr;
  char *command = strtok_r(line, " \t", &savePointer);
  if (command == nullptr) {
    printInvalidFormat();
    return;
  }

  if (strcmp(command, "PUMP") == 0) {
    handlePumpCommand(savePointer);
  } else if (strcmp(command, "STOP") == 0) {
    handleStopCommand(savePointer);
  } else if (strcmp(command, "STOP_ALL") == 0) {
    handleStopAllCommand(savePointer);
  } else if (strcmp(command, "STATUS") == 0) {
    handleStatusCommand(savePointer);
  } else {
    // PINGを含む未定義コマンドは、すべて同じエラーにする。
    Serial.println(F("ERROR INVALID_COMMAND"));
  }
}

void receiveSerialByte() {
  if (Serial.available() <= 0) {
    return;
  }

  const char received = static_cast<char>(Serial.read());
  if (received == '\r') {
    return;
  }

  if (received == '\n') {
    if (discardingLongLine) {
      discardingLongLine = false;
      receiveLength = 0;
      printInvalidFormat();
      return;
    }

    receiveBuffer[receiveLength] = '\0';
    processCommand(receiveBuffer);
    receiveLength = 0;
    return;
  }

  if (discardingLongLine) {
    return;
  }

  if (receiveLength < RECEIVE_BUFFER_SIZE - 1) {
    receiveBuffer[receiveLength++] = received;
  } else {
    // 以後の文字を保存せず、行全体を無効として扱う。
    discardingLongLine = true;
    receiveLength = 0;
  }
}

void setup() {
  // 出力化より先にラッチをOFFへ設定し、起動時の誤作動を避ける。
  for (uint8_t i = 0; i < PUMP_COUNT; ++i) {
    const uint8_t pin = FIRST_PUMP_PIN + i;
    digitalWrite(pin, PUMP_OFF_LEVEL);
    pinMode(pin, OUTPUT);

    pumps[i].pin = pin;
    pumps[i].active = false;
    pumps[i].startedAt = 0;
    pumps[i].durationMs = 0;
  }

  Serial.begin(SERIAL_BAUD_RATE);
  delay(1000);
  Serial.println(F("READY"));
}

void loop() {
  // 1ループ1バイトに制限し、連続受信中も必ず時間監視へ戻る。
  updatePumps();
  receiveSerialByte();
}
