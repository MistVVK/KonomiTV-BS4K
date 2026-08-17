# Bundled LibreSpeed Worker manifest

このディレクトリは LibreSpeed の測定 Worker だけを、固定 commit の無改変ファイルとして配布する。
PHP backend と LibreSpeed のゲージ UI は含めない。Vite はこれらのファイルを bundle / minify しない。

| Item | Value |
| --- | --- |
| Work | LibreSpeed `speedtest_worker.js` |
| Author | Federico Dossena |
| License | GNU LGPL-3.0 (参照される GNU GPL-3.0 全文も同梱) |
| Upstream | https://github.com/librespeed/speedtest |
| Commit | `892674a084a3dd354d823545cd0191023325b89c` |
| Source | https://github.com/librespeed/speedtest/blob/892674a084a3dd354d823545cd0191023325b89c/speedtest_worker.js |

| File | SHA-256 |
| --- | --- |
| `speedtest_worker.js` | `3dc577e830a7255eacd9335865af9c4a77ca1d7c115e8bfc7e1717d2c4ed08d0` |
| `LICENSE-LGPL-3.0.txt` | `e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118` |
| `LICENSE-GPL-3.0.txt` | `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986` |

許可されるファイルはこの MANIFEST と上表の3ファイルだけである。未知ファイル・欠落・hash 不一致はライセンス生成を失敗させる。
