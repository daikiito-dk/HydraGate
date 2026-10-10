# HydraGate

**データセンター液冷のためのベンダーニュートラルなテレメトリ＆安全統合ゲートウェイ**

HydraGateは、設備側の冷却機器（Modbus TCPを話すCDU）とITインフラ管理（DMTF Redfish流のREST／SSE／Prometheus）の間のプロトコル断絶を埋めるブリッジです。さらに、ほぼすべてのDCIMスタックが欠いている要素、つまり**水撃（ウォーターハンマー）を考慮した漏液時の安全遮断インターロック**を搭載しています。

```
            Modbus TCP                    Redfish流 REST / SSE / メトリクス
┌─────────────┐        ┌──────────────────────────┐        ┌──────────────┐
│  仮想／実機  │ ◄────► │         HydraGate        │ ◄────► │  DCIM・運用  │
│     CDU     │ 10 Hz  │  poller · FSM · hammer   │  HTTP  │  ダッシュボード│
│  (FWS+TCS)  │ ポーリング│  guard · event bus      │  SSE   │  アラート     │
└─────────────┘        └──────────────────────────┘        └──────────────┘
                          ▲                    │
            漏液ロープ ────┘                    ▼
            センサー          ポンプ段階減速＋弁の段階閉止
                              （ジュコフスキー制約を強制）
```

## なぜ必要か

ダイレクト・トゥ・チップ液冷は設備配管（OT）とITラックを密接に結合させますが、その周辺のソフトウェアは分断されています。CDUはModbus/BACnetを話し、ITはRedfishを話し、漏液時の安全連鎖を統括するプレイヤーがいません。

高流量のループで弁を瞬時に閉じることは「安全な遮断」ではありません。それは水撃です:

$$\Delta P = \rho \cdot c \cdot \Delta v \quad \text{（ジュコフスキーの式）}$$

DN50配管で200 L/minの流量を瞬時停止させると、**約24 bar**の圧力スパイクが発生します。継手やコールドプレートを破裂させ、漏液を被害に変えてしまいます。HydraGateのインターロックは代わりに、ポンプの段階的減速と弁の段階的閉止を協調させ、暗示されるサージ圧を設定エンベロープ（デフォルト**5 bar**）に収めながら、漏液検知から約100 ms以内に最初の是正書き込みを実行します。さらにランタイムガードは計測テレメトリを常時監査し、エンベロープを違反する者（HydraGateではない無思慮な制御器を含む）を検出・通報します。

## ステータス

プロトタイプ／MVPです。Redfishインターフェースは**Redfishインスパイア**（DMTFの`CoolingUnit`／`CoolingLoop`／`LeakDetector`スキーマを踏襲。まだDSP0158認証ではありません）で、テスト済みのデバイスは同梱の**仮想CDUシミュレーター**のみです。実機への接続には、必ず専門的な安全工学レビューを行ってください。

## クイックスタート

Python 3.11+ と [uv](https://docs.astral.sh/uv/) が必要です。

```bash
git clone https://github.com/daikiito-dk/HydraGate && cd HydraGate
uv sync                       # .venv作成＆インストール

# ターミナル1 - 仮想CDU（定常: 200 L/min、IT負荷80 kW）
uv run hydragate-sim

# ターミナル2 - プロトコルブリッジ（Redfish APIを :8080 で提供）
uv run hydragate-bridge
```

動作確認:

```bash
curl -s localhost:8080/redfish/v1/ | jq
curl -s localhost:8080/redfish/v1/CoolingUnits/cdu-01 | jq '.Status, .SecondaryLoop'
curl -s localhost:8080/metrics | grep hydragate_secondary_flow_lpm
curl -sN localhost:8080/redfish/v1/EventService/SSE   # ライブイベントストリーム
```

## デモ: 漏液応答

```bash
# ターミナル3 - ラック列Bの漏液ロープを作動
uv run hydragate-chaos leak --zone b
```

SSEストリームには、約150 ms以内に `LeakDetected` イベントが出ます。ポンプ設定値は即座に引き下げられ（運動量の逃がし）、遮断弁はサージ予算に合わせた段階で閉止し、数秒で `ISOLATED` に到達します:

```
event: LeakDetected        data: {"message":"coolant leak detected: leak-b", ...}
event: InterlockAction     data: {"detail":"pump 100% -> 70% (momentum bleed)", ...}
event: InterlockAction     data: {"detail":"valve 100% -> 55% at flow 147 lpm", ...}
...
event: IsolationComplete   data: {"message":"loop isolated in 7.4 s", ...}
event: MaintenanceRecommended  data: {"message":"de-energize affected IT racks ...", ...}
```

ネガティブコントロール — HydraGateが防ぐべき「間違った遮断」:

```bash
uv run hydragate-chaos slam      # 弁の瞬時閉止 = 誤った遮断
# -> event: UnsafeClosureDetected  data: {"message":"water-hammer surge 23.59 bar exceeds the 5.00 bar limit", ...}
```

その他の障害注入:

```bash
uv run hydragate-chaos heat --kw 320      # 熱暴走シナリオ -> ThermalWarning（遮断はしない）
uv run hydragate-chaos blackout          # センサーバスブラックアウト -> CommunicationLost・データ失効
uv run hydragate-chaos pump --pct 100     # 手動制御
uv run hydragate-chaos valve --pct 100
```

Dockerで動かす場合（単一イメージ・2ロール）:

```bash
docker compose up --build          # sim + bridge
```

## アーキテクチャ

| コンポーネント | パス | 機能 |
|---|---|---|
| 仮想CDUシミュレーター | `src/hydragate/sim/` | pymodbus TCPサーバー＋コンパクトな熱流体モデル（TCS/FWSループ、ポンプ・システム曲線、漏液ドローオフ、アクチュエータランプ、センサーノイズ、障害注入レジスタ） |
| Modbusマップ（唯一の真実源） | `spec/modbus_map.yaml` | シミュレーター・ブリッジ・カオスインジェクターが共有するワイヤ契約。両側の乖離は起き得ない |
| ポーラー | `src/hydragate/bridge/poller.py` | 10 Hzレジスタ polling、スナップショットリングバッファ、失効検知 |
| 安全遮断FSM | `src/hydragate/bridge/fsm.py` | 漏液ラッチ、段階的遮断（各段のサージ予算管理）、タイムアウトフォルト、警告監視 |
| ハンマーガード | `src/hydragate/bridge/hammer.py` | ジュコフスキー計算＋実計測フローのランタイム監査。出所を問わず危険な閉止を検出 |
| Redfish API | `src/hydragate/bridge/api.py` | CoolingUnit / CoolingLoop / LeakDetector / Manager / EventService、SSE、Prometheus `/metrics` |
| カオスインジェクター | `hydragate-chaos` | leak / heat / slam / blackout / 手動ポンプ・弁制御 |

## 検証契約（Spec-Driven）

合否判定基準は `spec/contracts/*.yaml` に機械可読で定義され、`tests/test_contracts.py` が**実際の** sim＋ブリッジのプロセススタックに対して実行します:

| 契約 | 検証内容 |
|---|---|
| `leak_isolation` | トリガー後500 ms以内のSSE `LeakDetected`、イベント後250 ms以内の最初の是正書き込み、`ISOLATED`到達（弁≤5%・ポンプ≤10%）、実測ジュコフスキー・サージ≤5 bar、`UnsafeClosureDetected`不在 |
| `communication_loss` | 6秒以内の `CommunicationLost`＋`COMMS_LOST`＋テレメトリ失効。復旧時に誤遮断なし |
| `naive_slam_negative_control` | 瞬時閉止は「危険」と検出され、インターロックは落ち着いている |
| `heat_spike_warning` | 熱暴走は `ThermalWarning` を出すが遮断はトリガーしない |

```bash
uv run pytest -q        # 20テスト: 契約4件＋物理＋ハンマー計算＋API単体
```

`config.yaml` のトポロジーは、将来のNetBox同期ワーカー（NetBox v4.7冷却モデル: CoolingSource → CoolingFeed → CoolingIntake/Outflow）の静的なプレースホルダーです。

## ロードマップ

- [ ] NetBox v4.7冷却トポロジーの同期（監視エンドポイントの自動登録）
- [ ] BACnet/IP対応とベンダー別レジスタマッププロファイル（Motivair、CoolIT、Vertiv、nVent）
- [ ] DMTF Redfishスキーマへの完全準拠（DSP0158）＋公式モックアップテスト
- [ ] インターロック部のRust/Goリライト（ハードリアルタイム向け）、エッジPLCへのポリシープッシュ
- [ ] 冷却液の健全性分析（導電率/pHトレンド、防食剤の枯渇予測）
- [ ] ラック単位の熱量課金: $\dot Q = \rho c_p Q \Delta T$ の積算（コロケーション向け）

## ライセンス

Apache-2.0。詳細は [LICENSE](LICENSE) を参照。
English README: [README.md](README.md)
