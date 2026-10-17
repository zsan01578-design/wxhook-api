# Third-party dependencies

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

Other dependencies retain their respective licenses in their installed packages.
