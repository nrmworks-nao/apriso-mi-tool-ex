# Claude Code 作業指示書: Apriso MI設定ツール (AprisoMIToolEx)

## 0. 作業ルール（厳守事項）

- **コードの実行・動作確認は行わないこと。** ファイルの作成・編集のみを行い、`python main.py` 等のアプリ起動コマンドは実行しない。
- **DBへ接続するコマンド（sqlcmd、pyodbc を使うスクリプト等）は一切実行しないこと。** 接続先は稼働中の MES 本番DBである。
- **実装はすべてのファイルが完成するまで一気に進めること。** 途中で確認を求めず、全モジュールを作成して完了させる。
- 不明点がある場合は、推測で進めず質問すること。特に「10. 未確認事項」に挙げた項目は、記載した既定の扱いで実装し、勝手に仕様を広げない。
- **本書に書かれていないテーブル・列への書き込みは実装しないこと。** 書き込み対象は `MI_ACTION_GROUP_POINT` の1テーブルのみである。

## 1. プロジェクト概要

DELMIA Apriso の Machine Integrator（以下 MI）の設定を、Apriso のDBを直接参照して閲覧・編集する Windows デスクトップアプリケーションを Python + PySide6 で開発する。

Apriso 標準の「マシンインテグレータ設定」画面には次の不満がある。

- スクリプト編集ウィンドウが小さく、大きくできない（18行程度しか表示されない）
- アクショングループのポイント設定で、ポイントを1件ずつ選択しなければならない
- 設備ごとに複製されたスクリプトやポイント構成を横断して検索・比較する手段がない

本アプリはこれを補う。**第1版の範囲は次のとおり。**

| 対象 | 第1版の範囲 |
|---|---|
| スクリプト | **閲覧のみ**（大きなエディタ表示、横断検索、差分比較、ファイル出力）。DBへの書き戻しは行わない |
| アクショングループのポイント設定 | 閲覧と**編集**（一覧・フィルタ、一括編集、CSV/Excel入出力、他グループからの複製、連番生成、各種チェック） |
| ポイント本体（MI_POINT）・データソース | **閲覧のみ** |

## 2. 開発環境

- **OS**: Windows（実行環境）
- **Python**: 3.12
- **仮想環境**: `.venv`（`C:\dev\AprisoMIToolEx\.venv`）
- **GUIフレームワーク**: PySide6（Qt for Python）6.11.1
- **外部パッケージ**:
  - `PySide6` — GUI
  - `pyodbc` — SQL Server 接続（ODBC Driver 17 / 18 for SQL Server）
  - `openpyxl` — Excel 入出力
  - `keyring` — SQL Server 認証のパスワード保存（Windows 資格情報マネージャー）
  - `pyinstaller` — exe 化
- **設定ファイル**: `config.json`（アプリ直下、初回起動時に自動生成）
- **接続先DB**: SQL Server、データベース名 `FLX24M`（Apriso 2024）

既存アプリ `mani-mes-file-sync-app`（ファイル連携アプリ）と同じ構成・同じ見た目にする。`core/`（GUI非依存のロジック）と `gui/`（PySide6）を分け、QSS は `gui/styles.py` に集約する。

## 3. プロジェクト構成

```
C:\dev\AprisoMIToolEx\
├── .venv\
├── config.json               # 設定ファイル（初回起動時に自動生成）
├── requirements.txt
├── build.bat                 # PyInstaller ビルド
├── AprisoMIToolEx.spec
├── README.txt
├── main.py                   # エントリーポイント
├── backup\                   # 更新前データの退避先（自動作成）
├── log\                      # 操作ログ（自動作成）
├── core/
│   ├── __init__.py
│   ├── config_manager.py     # 設定の読み書き
│   ├── db.py                 # 接続管理（pyodbc）
│   ├── models.py             # データクラス
│   ├── mi_repository.py      # 読み取りクエリ
│   ├── script_parser.py      # SCRIPT.Script / MI_POINT.Parameters の XML 解析
│   ├── point_service.py      # ポイント設定の変更計画（ChangeSet）生成
│   ├── change_executor.py    # ChangeSet の退避・実行・ログ
│   ├── check_service.py      # 突合チェック・無効参照検出・構成比較
│   └── import_export.py      # CSV / Excel 入出力
└── gui/
    ├── __init__.py
    ├── styles.py             # QSS（既存アプリの配色を流用・拡張）
    ├── main_window.py        # メインウィンドウ（ツリー＋タブ）
    ├── connection_dialog.py  # 接続設定
    ├── code_viewer.py        # 行番号・C#ハイライト付きビューア
    ├── script_tab.py         # スクリプトタブ
    ├── point_tab.py          # ポイントタブ（グループ単位）
    ├── all_points_view.py    # 全ポイント一覧
    ├── script_search_view.py # スクリプト横断検索
    ├── diff_dialog.py        # 差分比較
    ├── bulk_edit_dialog.py   # 一括編集
    ├── copy_group_dialog.py  # 他グループから複製
    ├── sequence_dialog.py    # 連番生成
    └── preview_dialog.py     # 変更プレビュー（実行確認）
```

## 4. データベース仕様

以下は実DBで確認済みの内容である。ここに書かれていない列の意味を推測して使わないこと。

### 4.1 テーブルの関係

```
MI_CONNECTOR (1行のみ: Alias="Connector")
  ├─ MI_DATA_SOURCE            (ConnectorID)          例: HN_DS01, HN_DS02
  │    └─ MI_POINT_GROUP       (DataSourceID, ParentID で階層)   例: OPCUA_CAO_HJM1_OPC_019_R
  │         └─ MI_POINT        (PointGroupID)         例: _PALETTE_INFO_A_EQUIP_NO
  └─ MI_ACTION_GROUP           (ConnectorID)          例: AG_HJMG56501101_PaletteInquiry
       ├─ MI_ACTION            (ActionGroupID)        例: AC_PaletteInquiry
       │    └─ SCRIPT          (MI_ACTION.ScriptId = SCRIPT.ID)
       └─ MI_ACTION_GROUP_POINT (ActionGroupID, PointID → MI_POINT.ID)
```

- `MI_ACTION.ScriptId` は `SCRIPT.ID` を**直接**参照する。`MI_SCRIPT` テーブルは経由しない（本アプリでは使わない）。
- スクリプトは共有されておらず、アクションごとに別の `SCRIPT` レコードを持つ（設備ごとの複製）。
- 現在、スクリプトを持つ有効なアクションは124件。アクショングループ1つにアクション1つの構成。

### 4.2 共通事項

| 項目 | 内容 |
|---|---|
| ID | 全テーブル `int IDENTITY(10000000,1)`。INSERT 時に指定しない |
| `FUID` | `nvarchar(36) NOT NULL`、一意制約あり、既定値なし（`MI_ACTION_GROUP_POINT` には列がない） |
| `Active` | `bit NOT NULL`、既定値1。読み取りは原則 `Active = 1` で絞る |
| `RowVersionStamp` | `int`、既定値1。同時更新検出用 |
| 監査列 | `CreatedOn` / `CreatedBy` / `LastUpdateOn` / `LastUpdatedBy`。日時は **UTC**。ユーザー列は表示名（例: `岸 隼人(ALSI)`）、`nvarchar(50)` |
| 日時の表示 | 画面表示は JST（UTC+9）に変換する |

### 4.3 MI_ACTION_GROUP_POINT（書き込み対象）

| 列 | 型 | 内容 |
|---|---|---|
| ID | int IDENTITY | PK |
| ActionGroupID | int NOT NULL | FK → MI_ACTION_GROUP.ID（ON DELETE CASCADE） |
| PointID | int NOT NULL | FK → MI_POINT.ID |
| TriggersExecution | bit NULL | 画面の「トリガーの実行」。1 のときポイント変化でグループを実行 |
| LocalName | nvarchar(255) NULL | 画面の「ローカル名」。スクリプトの変数名と対応する（例: `sEquipmentNo`） |
| Active | bit NOT NULL | 既定値1 |
| RowVersionStamp | int NULL | 既定値1 |

- **一意制約**: (`ActionGroupID`, `PointID`)。同じグループに同じポイントは1回だけ登録できる。

### 4.4 MI_POINT（読み取りのみ）

| 列 | 型 | 内容 |
|---|---|---|
| ID | int IDENTITY | PK |
| PointGroupID | int NOT NULL | FK → MI_POINT_GROUP.ID |
| Alias | nvarchar(255) | 例: `_PALETTE_INFO_A_RECIPE011`。一意制約 (`Alias`, `PointGroupID`) |
| Parameters | nvarchar(max) | データソース種別ごとの XML（4.7 参照） |
| DataType | int | **1 = 文字、2 = 整数**（確認済みはこの2つ。他の値はコード値をそのまま表示） |
| Access_ | int | 0 = ReadOnly、1 = WriteOnly、2 = ReadWrite |
| Enabled | bit | 1 = 有効 |
| InitialValue | nvarchar(max) | |

### 4.5 MI_POINT_GROUP / MI_DATA_SOURCE（読み取りのみ）

- `MI_POINT_GROUP`: `ID`, `DataSourceID`, `Alias`, `ParentID`（自己参照で階層）, `Active`。一意制約 (`DataSourceID`, `Alias`, `ParentID`)。
- `MI_DATA_SOURCE`: `ID`, `ConnectorID`, `Alias`, `Enabled`, `Active`。

**エイリアスのフルパス**は次の形で組み立てる（Apriso 画面の「エイリアス」列と同じ並び）。

```
<MI_DATA_SOURCE.Alias>\<MI_POINT_GROUP.Alias（ParentID を辿って親→子の順に \ で連結）>\<MI_POINT.Alias>

例: HN_DS02\OPCUA_CAO_HJM1_OPC_019_R\_PALETTE_INFO_A_EQUIP_NO
```

確認した範囲では `ParentID` は NULL（1階層）だが、階層がある場合にも対応すること。

### 4.6 MI_ACTION_GROUP / MI_ACTION / SCRIPT（読み取りのみ）

- `MI_ACTION_GROUP`: `ID`, `ConnectorID`, `Name`（一意: `ConnectorID`+`Name`）, `Enabled`, `Comment_`, `OnStartupExecution`, `OnShutdownExecution`, `PeriodicalExecution`, `PeriodicalInterval`, `PointChangeExecution`, `PointChangeExpression`, `OnRefreshExecution`, `Active`。
  - グループ名は `AG_<設備>_<処理>` の形式（例: `AG_HJMG56501101_PaletteInquiry`、`AG_HOM0001_WorkEnd`、`AG_BATEY0080_S`）。処理名には `PaletteInquiry` / `WorkStart` / `WorkEnd` / `MaterialInquiry` と、それぞれに `Finished` を付けたものがある。
- `MI_ACTION`: `ID`, `Name`（例: `AC_WorkEnd`）, `ActionGroupID`, `ActionType`, `Enabled`, `SequenceNo`, `ScriptId`, `Parameters`, `Active`。
  - スクリプトアクションの `ActionType` は **5**。
- `SCRIPT`: `ID`, `Name`（全件 `"Script"` で識別には使えない）, `Language`（`CSharp`）, `ScriptType`（2）, `Script`（nvarchar(max)、XML）, `Author`, `LastUpdateOn`, `LastUpdatedBy`, `Active`。

### 4.7 XML の構造

**SCRIPT.Script**（1行のXML。C#ソースは `<Source>` 内にあり、XMLエスケープされている。ソース内の改行は保持されている）

```xml
<?xml version="1.0" encoding="utf-8"?>
<Script entityXmlVersion="24000000">
  <Fuid>...</Fuid>
  <Source>（C# ソース）</Source>
  <SupplementaryMethods />
  <Inputs>
    <ScriptVariable Name="sPaletteNoIn" Type="System.String">...</ScriptVariable>
    ...
  </Inputs>
  <Outputs />
  <References />
  <ScriptPlugins><string>...</string></ScriptPlugins>
  <OperationDependencies>
    <ScriptDependency><Name>MANI_BATEY0030_Main</Name>
      <Inputs><PropertyBagItem Key="InContainerNo"><Value>System.String</Value></PropertyBagItem>...</Inputs>
    </ScriptDependency>
  </OperationDependencies>
  <ScriptLanguage>CSharp</ScriptLanguage>
  <ScriptUsings>using System; </ScriptUsings>
  <ScriptId>（32桁の16進文字列）</ScriptId>
</Script>
```

- `<Inputs>` のうち次の5つは MI 標準の変数で、ポイントではない: `MachineConnectorName`, `ActionGroupName`, `TriggerItemAlias`, `TriggerTime`, `ActionContext`。
- それ以外の入力変数名が、グループのポイントの `LocalName` と対応する。

**MI_POINT.Parameters**（外側XMLの `<SerializedData>` に、エスケープされた内側XMLが入る二重構造）

```xml
<CustomPointProperties ...>
  <SerializedData>（エスケープされた下記XML）</SerializedData>
</CustomPointProperties>

<OPCUaPointProperties ...>
  <Namespace>urn:Controller:DeviceId=31</Namespace>
  <Key>s=[PALETTE_INFO]A_RECIPE011</Key>
  <PublishingInterval>500</PublishingInterval>
  <SamplingInterval>0</SamplingInterval>
  <Priority>0</Priority>
  <QueueSize>0</QueueSize>
  <OriginalPointType>System.UInt32, mscorlib, ...</OriginalPointType>
  <RegisterUaNode>false</RegisterUaNode>
</OPCUaPointProperties>
```

解析に失敗した場合（OPC UA 以外のデータソース等）は例外にせず、各項目を空として扱う。

### 4.8 読み取りクエリ

起動時（および「再読込」時）に次を一括で読み、メモリ上に保持する。件数はグループ数百、ポイント数千の規模である。

```sql
-- データソース
SELECT ID, Alias, Enabled FROM MI_DATA_SOURCE WHERE Active = 1;

-- ポイントグループ
SELECT ID, DataSourceID, Alias, ParentID FROM MI_POINT_GROUP WHERE Active = 1;

-- ポイント
SELECT ID, PointGroupID, Alias, DataType, Access_, Enabled, InitialValue, Parameters
FROM MI_POINT WHERE Active = 1;

-- アクショングループ
SELECT ID, Name, Enabled, Comment_, OnStartupExecution, OnShutdownExecution,
       PeriodicalExecution, PeriodicalInterval, PointChangeExecution,
       PointChangeExpression, OnRefreshExecution
FROM MI_ACTION_GROUP WHERE Active = 1;

-- アクション
SELECT ID, Name, ActionGroupID, ActionType, Enabled, SequenceNo, ScriptId
FROM MI_ACTION WHERE Active = 1;

-- スクリプト（アクションから参照されているもののみ）
SELECT s.ID, s.Language, s.Script, s.Author, s.LastUpdateOn, s.LastUpdatedBy
FROM SCRIPT s
WHERE s.ID IN (SELECT ScriptId FROM MI_ACTION WHERE Active = 1 AND ScriptId IS NOT NULL);

-- グループのポイント設定（Active = 0 の行も読む。無効参照の検出と一意制約の判定に使う）
SELECT ID, ActionGroupID, PointID, TriggersExecution, LocalName, Active, RowVersionStamp,
       CreatedOn, CreatedBy, LastUpdateOn, LastUpdatedBy
FROM MI_ACTION_GROUP_POINT;
```

- 読み取りはワーカースレッド（QThread）で行い、Signal/Slot で GUI に通知する。読み込み中はステータスバーに進捗を表示する。
- 読み取り用の接続は `SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED` とし、稼働中の MES をロックで待たせない。

## 5. 機能仕様

### 5.1 スクリプト閲覧

- ツリーでアクションを選ぶと、スクリプトタブに `<Source>` の内容を表示する。
- ビューアは**読み取り専用**。行番号、C# のシンタックスハイライト（キーワード・文字列・コメント・数値）、現在行の強調、等幅フォントを備える。
- ビューア内検索（Ctrl+F、次へ F3 / 前へ Shift+F3、大文字小文字の区別、正規表現）。
- フォントサイズ変更（Ctrl+ホイール、Ctrl+0 で既定に戻す）。
- 「別ウィンドウで開く」で、スクリプトだけの独立ウィンドウ（最大化可能）を開ける。複数同時に開ける。
- スクリプトタブの下部に次を折りたたみ可能なパネルで表示する。
  - 入力変数（名前・型。標準5変数は薄く表示）
  - 呼び出しオペレーション（`<OperationDependencies>` の `Name`）
  - 作成者、最終更新者、最終更新日時（JST）
- 「ファイルに保存」で `.cs` として保存する（UTF-8、改行 CRLF）。「すべて書き出し」で全スクリプトを `<グループ名>__<アクション名>.cs` の形でフォルダへ一括出力する。

### 5.2 スクリプト横断検索

- メニュー「表示 → スクリプト横断検索」で開くビュー。
- 全スクリプトの `<Source>` を対象に、文字列または正規表現で検索する。
- 結果は表で表示する（グループ名、アクション名、行番号、該当行のテキスト）。行をダブルクリックすると、そのスクリプトを開いて該当行へ移動する。
- グループ名のフィルタ（部分一致、例: `WorkEnd`）と併用できる。

### 5.3 スクリプト差分比較

- 2つのスクリプトを選んで左右に並べ、差分を色分け表示する（`difflib` を使用）。
- 「同種グループと比較」: 選択中のグループと処理名が同じグループ（例: すべての `*_WorkEnd`）を一覧し、各スクリプトとの一致／不一致と差分行数を表で示す。行を選ぶと左右比較を開く。
- 比較時のオプション: 空白の違いを無視、設備固有の文字列（グループ名中の設備コード）を正規化して比較。

### 5.4 ポイント設定（グループ単位）

ツリーでアクショングループを選ぶと、ポイントタブにそのグループの `MI_ACTION_GROUP_POINT`（`Active = 1`）を表で表示する。

| 列 | 元データ | 編集 |
|---|---|---|
| ローカル名 | `MI_ACTION_GROUP_POINT.LocalName` | 可 |
| エイリアス | フルパス（4.5） | 可（ポイント選択ダイアログ、または一括置換） |
| トリガーの実行 | `TriggersExecution` | 可 |
| データタイプ | `MI_POINT.DataType`（文字／整数） | 不可 |
| アクセス | `MI_POINT.Access_` | 不可 |
| ポイント有効 | `MI_POINT.Enabled` | 不可 |
| OPC UA キー | Parameters の `Key` | 不可 |
| 公開間隔 | Parameters の `PublishingInterval` | 不可 |

- 列ヘッダーでの並べ替え、列ごとのフィルタ（テキスト部分一致）、**複数行選択**（Shift / Ctrl、Ctrl+A）。
- 選択範囲のコピー（Ctrl+C、タブ区切り。Excel へ貼り付け可能）。
- 編集はすぐにDBへ書かず、**未保存の変更**として表に保持する（変更セルは背景色を変え、追加行・削除行も色で区別）。「保存」でプレビュー（5.10）を経てDBへ反映する。「元に戻す」で未保存の変更を破棄する。
- 未保存の変更があるままグループを切り替える、またはアプリを終了する場合は確認する。

### 5.5 一括編集

複数行を選択して「一括編集」を開く。

- **エイリアスの置換**: フルパスに対して「検索文字列 → 置換文字列」（正規表現可）を適用し、置換後のフルパスに一致する `MI_POINT` を探して `PointID` を差し替える。一致するポイントがない行は「未解決」としてエラー表示し、保存対象から外す。
- **ローカル名の置換**: 検索・置換、接頭辞／接尾辞の付与。
- **トリガーの実行**: 一括で ON / OFF。
- 適用前に、行ごとの「変更前 → 変更後」を表で確認できる。

### 5.6 他グループから複製

- 複製元のアクショングループを選び、そのポイント構成を現在のグループへ取り込む。
- 置換ルールを複数指定できる（例: `HJM1_OPC_019` → `HJM1_OPC_020`）。置換はエイリアスのフルパスに適用し、5.5 と同じ方法でポイントを解決する。
- 取り込み方法を選ぶ: 「追加のみ（既存行は残す）」／「置き換え（既存行を削除して入れ替え）」。
- ローカル名と `TriggersExecution` は複製元の値を引き継ぐ。
- 未解決の行、および一意制約 (`ActionGroupID`, `PointID`) に抵触する行は一覧で示し、取り込み対象から外す。

### 5.7 連番ポイントの一括生成

- ローカル名とエイリアスのパターンから、グループのポイント行をまとめて生成する。
  - 例: ローカル名 `iRecipe{n:03}`、エイリアス `HN_DS02\OPCUA_CAO_HJM1_OPC_019_W\_PALETTE_INFO_A_RECIPE{n:03}`、開始1、終了11
- 生成するのは `MI_ACTION_GROUP_POINT` の行のみ。`MI_POINT` は作成しない。エイリアスに一致する既存ポイントがない番号は「未解決」として対象から外す。

### 5.8 CSV / Excel 入出力

- **エクスポート**: 現在のグループ、または全グループのポイント設定を CSV（UTF-8 BOM 付き）／Excel（.xlsx）に書き出す。列は `グループ名, ローカル名, エイリアス(フルパス), トリガーの実行, データタイプ, アクセス, OPC UA キー, 公開間隔`。
- **インポート**: 同じ列構成のファイルを読み、`グループ名` + `エイリアス` をキーに現在のDBと突き合わせて、追加・更新・削除の差分を作る。
  - 編集可能なのは `ローカル名`、`エイリアス`、`トリガーの実行` のみ。他の列は無視する。
  - ファイルにない行を削除するかどうかは、インポート時に選ぶ（既定は「削除しない」）。
  - 存在しないグループ名、未解決のエイリアスはエラー行として一覧表示し、対象から外す。
- インポート結果もそのままDBへは書かず、プレビュー（5.10）を経て反映する。

### 5.9 全ポイント一覧とチェック

**全ポイント一覧**（メニュー「表示 → 全ポイント一覧」）

- 全グループの `MI_ACTION_GROUP_POINT` を1つの表で表示する（5.4 の列に「グループ名」を加える）。フィルタ・並べ替え・複数選択・コピー・一括編集が使える。

**チェック機能**（メニュー「ツール → チェック」。結果は表で表示し、CSV 出力できる）

| チェック | 内容 |
|---|---|
| スクリプトとの突合 | グループごとに、スクリプトの入力変数（標準5変数を除く）とポイントの `LocalName` を照合する。「スクリプトにあるがポイントにない」「ポイントにあるがスクリプトにない」を列挙する。あわせて、ソース中の `MI.GetPointAlias("...")` と `MI.SetPointValue` 等で文字列指定された名前も抽出して照合する |
| 無効参照の検出 | グループのポイントが参照する `MI_POINT` が `Active = 0` または `Enabled = 0`、あるいは所属データソースが `Enabled = 0` のものを列挙する |
| 設備間の構成比較 | 処理名が同じグループ（例: すべての `*_PaletteInquiry`）で、`LocalName` の集合と、`LocalName` ごとのデータタイプ・アクセス・トリガーを比較し、基準グループとの差を示す |
| ローカル名の重複 | 同一グループ内で `LocalName` が重複している行を列挙する |

### 5.10 変更プレビューと実行

DBへの書き込みは、すべてこの手順を通す。

1. **編集モード**: 起動時は閲覧モード（書き込み操作のボタンは無効）。ツールバーの「編集モード」を ON にしたときだけ編集できる。ON の間はステータスバーに「編集モード」と警告色で表示する。
2. **プレビュー**: 「保存」を押すと、変更内容を ChangeSet（追加・更新・削除の一覧）として表示する。行ごとに「グループ名、操作、変更前、変更後」を示し、実行される SQL 文も確認できる。
3. **退避**: 実行直前に、更新・削除対象の行（全列）を `backup\YYYYMMDD_HHMMSS_<操作名>.json` に書き出す。あわせて、同じ内容を元に戻すための SQL（`.sql`）も出力する。退避に失敗した場合は実行しない。
4. **実行**: 1つのトランザクションで実行する。1行でも失敗したら全体をロールバックする。
5. **同時更新の検出**: UPDATE / DELETE は `WHERE ID = ? AND RowVersionStamp = ?` とし、影響行数が1でなければ「他で更新された」としてロールバックする。
6. **ログ**: `log\AprisoMIToolEx_YYYYMM.log` に、日時、実行者、操作、対象件数、退避ファイル名、結果を記録する。
7. **実行後**: DBを再読込して表示を更新し、「MI側への反映には、MI のリフレッシュまたは再起動が必要な場合があります」と案内する。

**書き込み SQL**（すべてパラメータ化クエリ。文字列連結で SQL を組み立てないこと）

```sql
-- 追加
INSERT INTO MI_ACTION_GROUP_POINT
    (ActionGroupID, PointID, TriggersExecution, LocalName,
     CreatedOn, CreatedBy, LastUpdateOn, LastUpdatedBy)
VALUES (?, ?, ?, ?, GETUTCDATE(), ?, GETUTCDATE(), ?);
-- Active と RowVersionStamp は既定値（1）に任せる

-- 更新
UPDATE MI_ACTION_GROUP_POINT
SET PointID = ?, TriggersExecution = ?, LocalName = ?,
    LastUpdateOn = GETUTCDATE(), LastUpdatedBy = ?,
    RowVersionStamp = ISNULL(RowVersionStamp, 0) + 1
WHERE ID = ? AND RowVersionStamp = ?;

-- 削除（物理削除）
DELETE FROM MI_ACTION_GROUP_POINT
WHERE ID = ? AND RowVersionStamp = ?;
```

- `CreatedBy` / `LastUpdatedBy` には、設定の `updated_by` の値を入れる（50文字以内）。未設定の場合は書き込みを実行させず、設定を促す。
- 一意制約 (`ActionGroupID`, `PointID`) は、`Active = 0` の行も含めて実行前にアプリ側で検査する。
- 1つの ChangeSet 内で、置き換えのために同じ (`ActionGroupID`, `PointID`) を削除して追加する場合は、DELETE → UPDATE → INSERT の順で実行する。

## 6. GUI設計

### 6.1 基本方針

- 既存アプリ `mani-mes-file-sync-app` と同じ **DELMIA Apriso 風の配色・フォント**にする。`gui/styles.py` に QSS を集約し、各ウィジェットファイルには `objectName` の設定のみを書く（スタイルをハードコードしない）。
- 配置は Apriso 標準の「マシンインテグレータ設定」画面に合わせる（**左にツリー、右にタブ**）。ただしスプリッターで幅を変えられ、ウィンドウは自由にリサイズ・最大化できること。
- フォントサイズは pt 単位で指定する（px は DPI 依存で exe 化時に崩れるため）。

### 6.2 配色・フォント（既存アプリの `gui/styles.py` と同じ値）

| 用途 | 色 |
|---|---|
| メニューバー・ステータスバー背景（濃紺） | `#1a3a5c` |
| メニュー選択・ホバー | `#2c5f8a` |
| アクセント・ボタン背景 | `#2980b9`（ホバー `#3498db`、押下 `#1a6da0`） |
| 無効ボタン | 背景 `#95a5a6`、文字 `#d0d0d0` |
| メイン背景 | `#f5f5f5` |
| パネル背景 | `#ffffff` |
| ボーダー | `#cccccc` |
| テーブルヘッダー・ツールバー背景 | `#e8eef4`（文字 `#333333`、太字） |
| テーブル交互行 | `#f9fbfd` |
| 基本文字 / 補助文字 | `#333333` / `#666666` |
| 成功 / 警告 / エラー | `#27ae60` / `#f39c12` / `#e74c3c` |

- フォント: `"Meiryo UI", "Yu Gothic UI", sans-serif`、標準 9pt、見出し 10pt。
- 等幅フォント（コードビューア）: `"Consolas", "MS Gothic", monospace`、既定 10pt。

既存の `styles.py` は `COLORS` 辞書と `get_app_stylesheet()` で構成されている。同じ構成を踏襲し、次のウィジェット用のスタイルを追加する: `QTreeView`、`QTabWidget` / `QTabBar`、`QSplitter::handle`、`QTableView`（既存の `QTableWidget` と同じ見た目）、コードビューア、変更セルの強調色。

追加する色:

| 用途 | 色 |
|---|---|
| 未保存の変更セル | `#fff4d6` |
| 追加行 | `#e3f4e8` |
| 削除行 | `#fbe3e0`（文字に取り消し線） |
| 差分: 追加 / 削除 / 変更 | `#e3f4e8` / `#fbe3e0` / `#fff4d6` |
| コード: キーワード / 文字列 / コメント / 数値 | `#1a3a5c`（太字）/ `#a31515` / `#3a7d44` / `#2980b9` |

### 6.3 メインウィンドウ

```
┌──────────────────────────────────────────────────────────────┐
│ ファイル(F)  表示(V)  ツール(T)  ヘルプ(H)            ← 濃紺 │
├──────────────────────────────────────────────────────────────┤
│ [再読込] [編集モード] [保存] [元に戻す]               ← 薄青 │
├───────────────────┬──────────────────────────────────────────┤
│ [フィルタ入力欄 ] │ [概要] [ポイント] [スクリプト]           │
│ ▼ Connector       │                                          │
│   ▶ HN_DS01       │   （選択したノードに応じた内容）         │
│   ▶ HN_DS02       │                                          │
│   ▼ AG_HJMG5650.. │                                          │
│       AC_Palette..│                                          │
│   ▶ AG_HJMG5650.. │                                          │
├───────────────────┴──────────────────────────────────────────┤
│ 接続: <サーバー>/FLX24M │ 閲覧モード │ グループ 250 / ポイント 2015 │
└──────────────────────────────────────────────────────────────┘
```

- 初期サイズ 1400×900、最小 900×600。ウィンドウ位置・サイズ、スプリッター位置、表の列幅は `config.json` に保存して復元する。
- **ツリー**: Connector → データソース／アクショングループ → アクションの順。アクショングループは名前順。上部のフィルタ欄に入力すると部分一致で絞り込む（スペース区切りで AND）。「設備ごとにまとめる」表示切替（グループ名の `AG_<設備>_` 部分で中間ノードを作る）を用意する。
- **タブ**: 選択ノードに応じて切り替える。
  - アクショングループ → 概要（名前・有効・コメント・実行条件）／ポイント（5.4）
  - アクション → 概要（名前・有効・シーケンス番号）／スクリプト（5.1）
  - データソース → 配下のポイントグループとポイントの一覧（読み取り専用）
- アクショングループを選んだ状態でスクリプトタブを押した場合は、そのグループの先頭アクションのスクリプトを表示する。

### 6.4 メニューバー構成

| メニュー | 項目 |
|---|---|
| ファイル(F) | 接続設定… / 再読込 (F5) / スクリプトをすべて書き出し… / ポイント設定のエクスポート… / ポイント設定のインポート… / 終了 |
| 表示(V) | 全ポイント一覧 / スクリプト横断検索 (Ctrl+Shift+F) / スクリプト比較… / 設備ごとにまとめる |
| ツール(T) | チェック ▶（スクリプトとの突合 / 無効参照の検出 / 設備間の構成比較 / ローカル名の重複）/ 退避フォルダを開く / ログフォルダを開く |
| ヘルプ(H) | バージョン情報 |

### 6.5 接続設定ダイアログ（モーダル）

| 項目 | 内容 |
|---|---|
| サーバー | テキスト入力 |
| データベース | テキスト入力（既定 `FLX24M`） |
| ODBC ドライバー | コンボボックス（`pyodbc.drivers()` のうち SQL Server 用を列挙） |
| 認証方式 | ラジオボタン: Windows 認証 / SQL Server 認証 |
| ユーザー名・パスワード | SQL Server 認証のときのみ有効 |
| サーバー証明書を信頼する | チェックボックス（`TrustServerCertificate=yes`） |
| 更新者名 | 監査列に記録する表示名（50文字以内） |
| [接続テスト] [OK] [キャンセル] | 接続テストは `SELECT 1` を実行し、結果を表示する |

- パスワードは `config.json` に書かず、`keyring` で保存する（サービス名 `AprisoMIToolEx`、キーは `<サーバー>/<ユーザー名>`）。
- 初回起動時、または接続に失敗した場合は、このダイアログを自動で開く。

## 7. 設定ファイル仕様 (config.json)

```json
{
  "connection": {
    "server": "",
    "database": "FLX24M",
    "driver": "ODBC Driver 17 for SQL Server",
    "auth": "windows",
    "username": "",
    "trust_server_certificate": true,
    "timeout_seconds": 15
  },
  "updated_by": "",
  "viewer": {
    "font_family": "Consolas",
    "font_size": 10,
    "word_wrap": false
  },
  "window": {
    "geometry": "",
    "splitter": "",
    "group_by_equipment": false
  },
  "paths": {
    "backup_dir": "backup",
    "log_dir": "log",
    "last_export_dir": ""
  }
}
```

- `auth` は `"windows"` または `"sql"`。
- 相対パスはアプリのルート（exe 化時は exe のあるフォルダ）を基準に解決する。

## 8. 各モジュール実装仕様

### 8.1 core/config_manager.py — 設定管理

- データクラスで設定を表し、`load()` / `save()` を提供する。ファイルがなければ既定値で生成する。
- 未知のキーは無視し、欠けたキーは既定値で補う。

### 8.2 core/db.py — 接続管理

- `build_connection_string(config, password)` で接続文字列を組み立てる。
- `connect(readonly: bool)` — 読み取り用は `autocommit=True` で READ UNCOMMITTED を設定。書き込み用は `autocommit=False`。
- 接続は操作のたびに開いて閉じる（常時接続を保持しない）。

### 8.3 core/models.py — データクラス

`DataSource`, `PointGroup`, `Point`, `ActionGroup`, `Action`, `Script`, `GroupPoint` と、それらを保持する `MIConfigSnapshot`。`MIConfigSnapshot` は次の索引を持つ。

- ポイントID → フルパス、フルパス → ポイントID
- グループID → GroupPoint のリスト
- グループ名を「設備」「処理名」に分解した結果（`AG_` を除き、最後の `_` で分ける。`AG_BATEY0080_S_finished` のように規則に合わないものは処理名を空にせず、名前全体を保持する）

### 8.4 core/mi_repository.py — 読み取り

- 4.8 のクエリを実行し、`MIConfigSnapshot` を返す。
- 進捗を通知するコールバック（テーブルごと）を受け取る。

### 8.5 core/script_parser.py — XML 解析

- `parse_script(xml_text) -> ParsedScript`（`source`, `inputs`, `operations`, `usings`, `language`）。
- `parse_point_parameters(xml_text) -> OpcUaPointProps | None`。
- 標準ライブラリの `xml.etree.ElementTree` を使う。解析失敗時は例外を握りつぶさず、空の結果とエラーメッセージを返す。
- `extract_point_names_from_source(source) -> set[str]` — `MI.GetPointAlias("名前"` などの文字列引数を正規表現で抽出する。

### 8.6 core/point_service.py — 変更計画

- GUI から渡された編集内容（追加・更新・削除、置換ルール、インポートデータ）を検証し、`ChangeSet` を返す。**DBには触れない。**
- 検証内容: エイリアスの解決、一意制約、`LocalName` の長さ（255）、同一グループ内の `LocalName` 重複（警告）。
- `ChangeSet` は `inserts` / `updates` / `deletes` と、対象外にした行（理由付き）を持つ。

### 8.7 core/change_executor.py — 実行

- 5.10 の手順（退避 → トランザクション実行 → ログ）を実装する。
- `preview_sql(change_set) -> list[str]` — 表示用に、パラメータを埋め込んだ SQL 文字列を返す（実行にはパラメータ化クエリを使い、この文字列は使わない）。
- ワーカースレッドで実行し、結果を Signal で返す。

### 8.8 core/check_service.py — チェック

- 5.9 の4種のチェックを、`MIConfigSnapshot` を入力として実装する。結果は行のリスト（表にそのまま出せる形）で返す。

### 8.9 core/import_export.py — 入出力

- CSV は `csv` モジュール（UTF-8 BOM 付きで書き出し、読み込みは BOM の有無を両方受け付ける）。Excel は `openpyxl`。
- Excel 出力はヘッダー行を固定し、オートフィルターを設定する。

### 8.10 gui/ 各モジュール

- `code_viewer.py`: `QPlainTextEdit` を継承。行番号エリア、現在行の強調、`QSyntaxHighlighter` による C# ハイライト、検索バー。
- `point_tab.py` / `all_points_view.py`: `QTableView` + `QAbstractTableModel` + `QSortFilterProxyModel`。未保存の変更はモデル側で保持する。
- `diff_dialog.py`: 左右2枚の `code_viewer` を並べ、スクロールを同期する。
- `preview_dialog.py`: ChangeSet の表と SQL 表示、[実行] [キャンセル]。対象外の行がある場合は別タブで理由とともに示す。
- すべてのダイアログで、時間のかかる処理中は操作を無効化し、進捗を表示する。

### 8.11 main.py — エントリーポイント

- `QApplication` を生成し、`get_app_stylesheet()` を適用、設定を読み込み、`MainWindow` を表示する。
- 接続設定が未入力ならば接続設定ダイアログを先に開く。

## 9. 実装上の注意事項

- GUI スレッドでDBアクセスをしないこと。必ずワーカースレッドで行う。
- SQL はすべてパラメータ化する。テーブル名・列名は本書に記載した固定文字列のみを使う。
- 日時は内部では UTC のまま保持し、表示時のみ JST に変換する。
- `MI_POINT.Parameters` と `SCRIPT.Script` は大きい（最大5万文字程度）。表のセルにそのまま表示しない。
- 例外はログに記録し、利用者にはメッセージボックスで要点を示す（スタックトレースは詳細表示に入れる）。
- PyInstaller: `pyodbc`、`keyring`（Windows バックエンド `keyring.backends.Windows`）が同梱されるよう、必要に応じて `hiddenimports` を spec に指定する。`build.bat` は既存アプリと同じ形式で、出力は `dist\AprisoMIToolEx\`。
- `README.txt` に、起動方法、ODBC Driver for SQL Server が必要であること、初回の接続設定手順を書く。

## 10. 未確認事項（既定の扱い）

次の項目は実DBで未確認である。記載した既定の扱いで実装し、該当箇所にコメントで「未確認」と明記すること。

| # | 未確認の内容 | 既定の扱い |
|---|---|---|
| 1 | DBを直接更新したあと、稼働中の MI にいつ反映されるか（リフレッシュ・再起動の要否） | アプリは反映操作をしない。実行後に案内文を表示するのみ（5.10 の手順7） |
| 2 | Apriso 画面のポイント設定にある「有効」列が、どの列に対応するか | 編集対象にしない。`MI_POINT.Enabled` を「ポイント有効」として読み取り専用で表示する |
| 3 | Apriso 標準画面でポイントを外したとき、物理削除か論理削除（`Active = 0`）か | 物理削除とする。`change_executor.py` の先頭に定数 `DELETE_MODE = "physical"` を置き、切り替え箇所を1か所にまとめる |
| 4 | `MI_ACTION_GROUP_POINT` にトリガーが定義されているか | ないものとして実装する |
| 5 | `MI_POINT.DataType` の 1・2 以外のコード値 | コード値をそのまま表示する |
| 6 | `SCRIPT.Script` 内の `<ScriptId>` の用途（内容のハッシュである可能性） | 第1版はスクリプトを書き戻さないため影響しない。表示もしない |
| 7 | `MI_ACTION_GROUP_POINT.Active = 0` の行が実在するか、その場合の一意制約との関係 | `Active = 0` の行も一意制約の判定対象に含める。該当時は「無効化された同一ポイントの行が存在する」と理由を表示して対象外にする |
