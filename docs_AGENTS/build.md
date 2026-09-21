# konomitv build と再配布

Docker イメージのビルド設定（CUDA / NONFREE）を変更する作業、または再配布に関わる作業の前に読むこと。

## konomitv buildについて

- CUDAバージョン
  - 12.4
  - 12.8
- NONFREE（プロファイル enum。Compose は `KONOMITV_NONFREE` 1本。ベンダー選択は独立の build arg `INTEL_NONFREE` / `AMD_NONFREE` が行う）
  - `nonfree`: 再配布不可側のプロファイル。実際に何を含むかは vendor flag が決める
  - `free`: 再配布可能側プロファイル。vendor flag の `true` 指定はビルドエラー（未指定は含めない = false と同じ解決）
  - 互換: `true` → `nonfree`、`false` → `free`（既存 .env を壊さない）
  - 廃止: `intel-nonfree` / `amd-nonfree` はビルドエラー。GPU overlay か build arg でフラグを直接指定する
- `INTEL_NONFREE` / `AMD_NONFREE`（各 `true` / `false` / 未指定）
  - 未指定は常に「そのベンダーを含めない」(false) と解決する。profile=nonfree で未指定がある場合は非自由なし構成になる警告を出す
  - `compose.intel.yaml` → INTEL=true / AMD=false、`compose.amd.yaml` → INTEL=false / AMD=true を既定とする。`.env` の `KONOMITV_INTEL_NONFREE` / `KONOMITV_AMD_NONFREE` は overlay 既定より環境値が優先され、基本 Compose にもそのまま伝達される
  - Intel と AMD を併用する場合は、overlay を並べても後勝ちで片方しか true にならないため、`.env` で `KONOMITV_INTEL_NONFREE=true` / `KONOMITV_AMD_NONFREE=true` の両方を明示することが必須（overlay 併用そのものでは実現しない）

## 再配布について

free プロファイルかつ vendor flag が両方 false の構成のみ再配布可能とする。
profile 名そのものではなく、実際に含まれる非自由コンポーネント（INTEL_NONFREE / AMD_NONFREE）が再配布可否を決める。
free の Docker build は ffmpeg（libx265）などの GPL3+ と両立するようにしたい。
AMD のプロプライエタリや Intel の非自由カーネルを入れる選択の場合はユーザがビルドしてるのでセーフという立場をとる。
