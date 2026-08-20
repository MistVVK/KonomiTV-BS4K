# konomitv build と再配布

Docker イメージのビルド設定（CUDA / NONFREE）を変更する作業、または再配布に関わる作業の前に読むこと。

## konomitv buildについて

- CUDAバージョン
  - 12.4
  - 12.8
- NONFREE（enum、Compose は `KONOMITV_NONFREE` 1本。Intel / AMD フラグへの展開は Dockerfile が行う）
  - `nonfree`: Intel 非自由カーネル + AMD proprietary runtime（再配布不可）
  - `intel-nonfree`: Intel 非自由カーネルのみ（再配布不可）
  - `amd-nonfree`: AMD proprietary runtime のみ（再配布不可）
  - `free`: 両方なし（再配布可能）
  - 互換: `true` → `nonfree`、`false` → `free`（既存 .env を壊さない）
  - `KONOMITV_NONFREE` を空にした場合の既定値は GPU overlay に連動する（`compose.intel.yaml` → `intel-nonfree`、`compose.amd.yaml` → `amd-nonfree`、それ以外 → `nonfree`）

## 再配布について

free プロファイルのみ再配布可能とし、nonfree / intel-nonfree / amd-nonfree は再配布不可とする。
free の Docker build は ffmpeg（libx265）などの GPL3+ と両立するようにしたい。
AMD のプロプライエタリや Intel の非自由カーネルを入れる選択の場合はユーザがビルドしてるのでセーフという立場をとる。
