# MIDI–Arduino CLIクライアント

MIDIキーボードのNote OnをArduinoの`PUMP`コマンドへ変換する常駐CLIです。送信コマンド、Arduinoからの全応答、音ごとの状態遷移を日時付きで標準出力へ記録します。

## セットアップ

[uv](https://docs.astral.sh/uv/)をインストールした状態で、次を実行します。

```sh
cd script
uv sync --dev
```

接続可能なデバイス名を確認します。

```sh
uv run shishiodoshi-client --list-devices
```

`config.json`の`midiInput`と`serialPort`へ、表示された名前をそのまま設定します。音ごとに次の項目を変更できます。

- `midiNote`: MIDIノート番号（0〜127、重複不可）
- `arduinoPin`: Arduinoピン（2〜9、重複不可）
- `pumpDurationMs`: ポンプ作動時間（10〜5000ms）
- `cooldownMs`: `DONE`受信後の待機時間
- `enabled`: 入力の有効・無効

## 実行

```sh
uv run shishiodoshi-client
```

デバイス設定は起動時に上書きできます。

```sh
uv run shishiodoshi-client \
  --midi-input "USB MIDI Keyboard" \
  --serial-port /dev/cu.usbmodem1101 \
  --log-level DEBUG
```

別の設定ファイルを使う場合は`--config path/to/config.json`を指定します。ログを保存する場合は標準出力をリダイレクトします。

```sh
uv run shishiodoshi-client | tee client.log
```

終了時はCtrl+Cを押してください。クライアントは可能な限り`STOP_ALL`を送信してからMIDI入力とシリアルポートを閉じます。MIDIまたはArduinoが切断された場合も同じ安全停止を試み、非ゼロで終了します。

## 状態と異常復旧

音ごとに`READY → ACTIVE → COOLDOWN → READY`を管理します。同じ音はACK待ち、ACTIVE、COOLDOWNの間には再送されませんが、異なる音はArduino側で並行動作できます。

ACKが1秒以内に届かない場合や`BUSY`、`ERROR`を受信した場合は対象音を`ERROR`にし、`STATUS`で実機状態を確認します。対象ピンがOFFなら`READY`、ONなら`ACTIVE`へ復旧します。STATUSもタイムアウトした場合は`ERROR`を維持します。

起動時はArduinoからの`READY`を待つと同時に`STATUS`を送信します。ポート接続時にリセットされないArduino Uno R4などでも、D2〜D9の完全なSTATUS応答を受信すれば接続済みとして動作を開始します。

## テスト

```sh
uv run pytest
```

自動テストはシリアル通信と時刻を差し替えて実行するため、ArduinoやMIDIキーボードは不要です。

## 実機確認

1. Arduinoへ`arduino/shishiodoshi_controller/shishiodoshi_controller.ino`を書き込む。
2. ポンプを外した状態でCLIを起動し、`RX READY`が表示されることを確認する。
3. MIDIノート48を押し、`TX PUMP 2 300`、`RX ACK PUMP 2 300`、`RX DONE 2`の順に出ることを確認する。
4. 複数の白鍵を押し、異なるピンのACKとDONEが記録されることを確認する。
5. 黒鍵、同じ音のCOOLDOWN中の再入力、velocity 0がPUMP送信されないことを確認する。
6. Ctrl+Cで`TX STOP_ALL`が出力され、全ポンプが停止することを確認する。

最初の通電試験は1チャンネルだけ接続し、ポンプとMOSFETモジュールに異常発熱がないことを確認してからチャンネルを増やしてください。
