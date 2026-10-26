# Relationship advisor resources

Source: https://github.com/shengjidaguai-china/goutoujunshi

Revision: `6db7354a4002dc7c448a9c87ffdad8132570c9d3`

Copyright (c) 2026 powerycy. See the complete MIT license in `LICENSE`.

`knowledge/` contains the 20 upstream knowledge documents. `practical/` contains
six selected upstream guides covering replies, social calibration, initiative,
imbalanced investment, emotional support and ethical social skills.

Documents are unchanged from the pinned source, apart from line endings. The
application loads up to three bounded excerpts using fixed topic routes. The
adapted application prompt and retrieval rules live in
`wxhook_api.services.relationship_advisor`. Upstream scripts are not executed;
the application supplies its own member-scoped temporary conversation store.
