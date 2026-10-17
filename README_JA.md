# ComfyUI-H3-Continuum-Plus 3.4.11

> **Plus フォーク:** これは `xmarre` が保守する [ukr8b3g-cmyk/ComfyUI-H3-Continuum](https://github.com/ukr8b3g-cmyk/ComfyUI-H3-Continuum) の Plus フォークです。上流プロジェクトを基盤として維持しつつ、上流と意図的に異なる機能、統合、修正、挙動を含む場合があります。

**V3.4 Stable (v3.4.11):** Driving Audio、Video Reference、Audio / Video Seam、Run Storageを含む安定版です。

MiniMax H3を複数チャンクで連続生成し、直前チャンク末尾の**映像latent / 音声latentを直接**次チャンクへ継承するComfyUIカスタムノードです。チャンク間でVideo/Audio VAEのDecode→Encodeは行いません。

## v3.4.11 更新 (2026-10-17)

Plus フォークの Comfy Registry PublisherId を **`xmarre`** に変更し、上流とは別の公開 ID **`comfyui-h3-continuum-plus`** と表示名 **H3 Continuum Plus** を設定しました。元の作者 `ukr8b3g-cmyk` のクレジットは残します。成功した GitHub リリースのタグ・コミットと公開メタデータを確認した後、`xmarre` 発行の Registry API キー（GitHub Actions Secret `REGISTRY_ACCESS_TOKEN`）でのみ公開します。キーがない場合は Registry 公開に失敗し、GitHub リリースとは独立した状態になります。

Runtime のノード ID と既存ワークフロー設定は変更しません。上流版と Plus 版を同じ ComfyUI 環境に同時インストールしないでください。旧 Registry ID からの自動移行はありません。

## v3.4.10 更新 (2026-10-17)

[PR #49](https://github.com/xmarre/ComfyUI-H3-Continuum-Plus/pull/49) では、Continuum が内部的に生成していた継続・終端・パディング用の指示文が MiniMax H3 の音声生成で発話される問題に対処しました。保護済みプレフィックス、未指定区間、終端区間の時刻・役割はメタデータとして保持し、内部指示文を Qwen のテキスト条件へ渡さないようにしています。対話履歴も、ユーザーが記述した台詞と話者情報だけをエンコードし、JSON や内部説明文を除外します。ユーザーによる生成テストでは報告されていた不要な発話が解消したと確認されました。Python 3.10–3.13 の自動テストも通過しています。

**制限:** すべての自発的発話や文末の途切れを防止する保証ではありません。映像・音声の潜在状態、時刻マスク、既存ワークフローの設定は変更していません。詳細は [Audio prompt boundaries](docs/AUDIO_PROMPT_BOUNDARIES.md) と [Changelog](CHANGELOG.md) を参照してください。関連コンポーネントのバージョンは変更されません。Comfy Registry への公開は PublisherId の権限確認が完了するまで行いません。

## v3.4.9 更新 (2026-10-17)

今回のPlusリリースには #44（遅延フラッシュとAuto 3の補正終了処理）、#46（実験的な対話履歴のQwen因果エンコード）、#47（隣接チャンクのQwen事前エンコード）が含まれます。#47の「Qwen Lookahead」は対象となる固定形状の連続チャンクで既定有効です。`H3C_CONDITIONING_LOOKAHEAD=0` で無効化できます。#46は境界での台詞の乱れを修正したことが未実証で、`H3C_CAUSAL_DIALOGUE_HISTORY=0` で比較可能です。Auto 3は限定的な実験的補正であり、元の生成結果の色ずれを必ず解決するものではありません。

対応バージョン: Flow v0.3.14 / Sol-H3 v0.1.11 / VDN-H3-Plus v1.5.10。Keylessの変更は含まれません。Comfy Registryへの公開には上流PublisherIdの権限確認が必要です。

## V3.4 Stable

V3.4は、最大8枚のReference Image、Run Storage、Spectrum Interop、Decode後のAudio / Video Seam補正を維持し、Driving AudioとVideo Referenceを正式な入力として追加します。

Driving Audioは入力音声を絶対時間で各Chunkのガイドに使い、最終出力には元音声を維持します。Video Referenceは参照映像を全Chunkで継続使用し、`Efficient - 0.4 MP`、`Balanced - 0.6 MP`、`Match Output`から処理解像度を選択できます。

Reference入力のバイパスされたソケットは無視され、有効な画像だけがPicture 1から連続採番されます。空欄や不完全なプロンプトも停止せず、警告とFixed fallbackで生成を続行します。未知のモデル、ノード、プロンプト形式を独自判断で拒否する制限も撤廃しました。

Run Storageの保存契約にはDriving AudioとVideo Referenceのidentityを含め、入力変更後に古いChunkを誤再利用しないようにしています。

## Driving Audio / Video Reference / Seam

Driving Audioだけを使う、Video Referenceだけを使う、Video Referenceと別音声を組み合わせる、という3通りの接続に対応します。Video Referenceは24 fps入力を基準とし、異なるfpsの素材はLoad Video側で24 fpsへ設定してください。

V3.4の新規「H3 Continuum Assemble + Seam」は、`Audio Seam = Auto`、
`Video Seam = Auto 3 (Sustained Tone)`、
`Timeline Output = Preserve audible tail (recommended)`、
`Image Output Device = Auto` を既定値とします。
Auto 3は連続したシーンで静止背景に基づく限定的なRGB補正を行い、
生成済み映像の継続部分だけを補正します。ズーム・細部の損失・
不完全な台詞は修復しません。`Auto` / `Auto 2` も選択できます。
旧バージョンの保存済みワークフローは明示的に保存された設定を維持します。
音声の末尾に有意な信号がある場合は映像と音声を揃えて保持し、
無音の余分な部分は要求された長さへ切り詰めます。
標準版とTurbo版のV3.4ワークフローJSONにこれらの設定を明示しています。
参照: [標準](examples/workflows/MiniMax_H3_Continuum_V34.json) /
[Turbo](examples/workflows/MiniMax_H3_Continuum_V34_turbo.json)。

## Legacy V2.1.7 Stable Facade UI

V2.1.7では、V2.1.6の動的表示制御を廃止し、標準ComfyUI描画だけを使用する静的Facadeへ移行しました。生成処理は互換用`H3ContinuumSamplerV2`を再利用し、State、Session、Prompt Plan、Spectrum Interop、保存Schemaは変更していません。

通常表示は次を中心にします。

```text
model / clip / video_vae / audio_vae / sampler / sigmas
Sequence Prompt
first_frame
prompt_overrides（任意pack）
advanced（任意pack）

Prompt Format
chunks
chunk_seconds
width / height
continuity
base_seed
control after generate
Seam Correction
Seam Correction

outputs: images / audio / result
```

### 補助ノード

- `H3 Continuum Clip Overrides`: Clip別Promptを1本のpackへまとめます。
- `H3 Continuum Advanced`: resume/session入力と低頻度設定をまとめます。
- `H3 Continuum Result`: resultから`last_state / session / report`を展開します。
- 補助ノードを使わない通常生成では、Sampler本体だけで動作します。

### Legacy互換

旧`H3ContinuumSamplerV2`は削除せずLegacy/Coreノードとして登録を維持します。既存WorkflowのノードID、入力順、出力順は変更していません。

## Continuity

- `Auto — conservative`
- `Balanced — 22 frames`（標準）
- `Fast — 5 frames`
- `Strong — 39 frames (Experimental)`

5/22/39フレームはH3の時間latent周期`1,4,4,4,4`に対して、それぞれ2/7/12 temporal latent stepsです。

## Prompt Format

Timelineでは各Headerを独立した行に置き、その次の行からPromptを書きます。時間範囲は`chunk_seconds`の境界と一致させてください。

```text
[0-5s]
最初の区間の動作、カメラ、場面進行。

[5-10s]
前区間の最終状態から自然に継続。
```

`[0-5s] Prompt`のような同一行形式はTimeline構文ではありません。Listは`---`で区切り、Fixedは全Chunkで同じPromptを使用します。

## FL2VA terminal merge

5秒ChunkのV3.4 latent-first FL2VAでは、条件を満たす最終2 Logical Chunkを1回のPhysical H3 sampleとCore VAE decode groupとして扱います。2×5秒はContinuation Methodに関係なくCore相当の10秒初回sampleです。3 Chunk以上では、検証済みの22-frame prefixに一致する`Guide / Motion Context` + `Balanced — 22 frames`だけを対象にします。

長尺`Native Masked`は、正確なGenerated AV継続に必要な39 video frames / 65 audio stepsを守るため、従来どおりLogical ChunkごとにPhysical sampleを生成します。Terminal pairのRun Storage再利用と再生成は必ず2 Chunk単位で行い、Physical再構成前にoverlapの完全一致を検証します。

First Frame / Last FrameとReference Imageを併用する場合、Qwen presentationにも適用対象のKeyframe画像を含めます。Promptでは引き続きReference Image 1〜8を`<Picture 1>`〜`<Picture 8>`として記述でき、内部で番号を調整します。Last Frameは最終conditioningだけに含めます。

## Spectrum

Continuumは有効な前チャンクContextがある場合だけ`h3_continuum` Interop API v1をMODEL optionsへ付与し、対応Spectrumへ`min_actual_prefix_steps=2`を要求します。初回チャンクにはInterop keyを付与しません。未知・未対応Spectrumでは通常Spectrumとしてfail-openします。

## 推奨配線

```text
UNET -> SageAttention -> Sol-Attn -> LoRA -> Spectrum -> H3 Continuum Sampler
```

まず`chunks=2 / chunk_seconds=5 / Balanced 22f / Seam Correction=Off`でNative Continuityを確認し、その後`Seam Correction=Auto`を比較してください。

## インストール

ZIPを展開して、`ComfyUI-H3-Continuum`フォルダーを`ComfyUI/custom_nodes/`へ置き、ComfyUIを再起動します。

旧`ComfyUI-H3-Continuum-Join`が残っている場合は同時ロードを避けるため削除または退避してください。同梱`install_windows.bat`は旧名・新名の既存フォルダーを日時付きでバックアップします。

## 検査

```text
python -m compileall -q .
pytest -q
```

MIT License。
