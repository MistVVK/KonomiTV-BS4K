# Bundled web font manifest

| Work | Version | Distribution | Input verification | License |
| --- | --- | --- | --- | --- |
| Kosugi | Fontsource 5.2.5 / Google Fonts v15 | `@fontsource/kosugi` | npm tarball SHA-256 in `update-web-fonts.py` | OFL-1.1 |
| Kosugi Maru | Fontsource 5.2.5 / Google Fonts v14 | `@fontsource/kosugi-maru` | npm tarball SHA-256 in `update-web-fonts.py` | OFL-1.1 |
| Open Sans | Fontsource 5.2.7 | `@fontsource/open-sans` | npm tarball SHA-256 in `update-web-fonts.py` | OFL-1.1 |
| Noto Sans Japanese | Fontsource 5.2.9 | `@fontsource/noto-sans-jp` | npm tarball SHA-256 in `update-web-fonts.py` | OFL-1.1 |
| Yaku Han JP | 4.1.1 | `yakuhanjp` | npm tarball SHA-256 in `update-web-fonts.py` | OFL-1.1 AND MIT |
| Material Design Icons | 7.4.47 | `@mdi/font` | npm tarball SHA-256 in `update-web-fonts.py` | Apache-2.0 |
| Twemoji Mozilla | 0.7.0 / Twemoji 14 | Mozilla release asset | official TTF SHA-256 in `update-web-fonts.py`; WOFF2 output is not fixed | Apache-2.0 AND CC-BY-4.0 |

`Twemoji.woff2` is generated from the verified official TTF with FontTools 4.59.1. Only the input is reproducibility-locked. The generated WOFF2 hash is recorded by the Docker license generator but is not an update-time acceptance condition.
