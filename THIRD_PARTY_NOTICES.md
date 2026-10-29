# Third-party dependencies

## goutoujunshi

- Project: https://github.com/shengjidaguai-china/goutoujunshi
- Pinned revision: `6db7354a4002dc7c448a9c87ffdad8132570c9d3`
- Copyright (c) 2026 powerycy
- License: MIT; complete license is bundled at `src/wxhook_api/resources/relationship_advisor/LICENSE`.

The relationship advisor bundles the 20 knowledge documents and six selected
practical guides from this revision. Document content is retained from upstream;
the application selects bounded excerpts at runtime. The workflow prompt is
adapted in `services/relationship_advisor.py` for explicit WeChat commands,
member-scoped temporary conversations and the existing model pipeline. The
upstream memory script, Codex metadata and ChatLab integration are not installed.

Updating these resources requires reviewing the upstream changes, replacing the
bundled documents and license, and updating the pinned revision here and in the
service. There is no runtime download or automatic update.

## parse-video-py

- Project: https://github.com/wujunwei928/parse-video-py
- Pinned revision: `5fcf87256edb5ffcdebf0e4aac2a5a41745da76e`
- Source archive: GitHub's official `codeload.github.com` endpoint, verified with SHA-256 `cb16d6f6f94415167907e0f5bf808ac88b38157ccab32eb196c325e5647e94a3`.
- License: [MIT](https://github.com/wujunwei928/parse-video-py/blob/5fcf87256edb5ffcdebf0e4aac2a5a41745da76e/LICENSE)

Installed as a Python dependency, without its separate web service. The integration
in `src/wxhook_api/services/parse_video.py` normalizes results and applies a
context-local HTTP policy through the pinned SDK's client factory. SDK source is
not vendored or modified. Recheck the factory integration and platform tests when
updating the pinned revision.

## media-parser

- Project: https://github.com/ucmao/media-parser
- Tested revision: `19496f8c253ee0c18a2e2e27e23267c76aecaa63`
- License: MIT

Cloned and run as a separate local service; it is neither installed as a
dependency nor vendored into this repository. The launcher in
`scripts/media-parser-server.py` only sets up a loopback-bound process and does
not modify upstream files. Users clone and update the upstream repository
themselves.

## jieba (optional)

- Project: https://github.com/fxsjy/jieba
- Version: `>=0.42.1,<1`
- License: MIT (included in the installed package).

Used locally when installed to segment Chinese text for group word frequencies.
The application includes an offline fallback and does not require this package.
Chat contents are not sent to a third-party service for this feature.

Other dependencies retain their respective licenses in their installed packages.
