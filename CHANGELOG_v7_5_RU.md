# v7.5 - forum routing + diplomacy UX

- FSM switched to `USER_IN_TOPIC`: one user's workflow in one forum topic can no longer capture messages from another topic.
- Temporary replies and workflow prompts are now explicitly sent with the originating `message_thread_id`; bot replies no longer fall into General when a command was used in another topic.
- Slash commands are excluded from group FSM text-input handlers, so `/admin`, `/set_*`, etc. are not treated as item names/comments while a workflow is open.
- Diplomacy is now faction-first: choose a faction button, then choose Alliance / Neutral / War.
- Added the main human factions from the original S.T.A.L.K.E.R. trilogy: Loners, Bandits, Duty, Freedom, Mercenaries, Monolith, Scientists, Military, Clear Sky, Renegades. Sin remains as a custom group option already used by this community, plus `Other faction`.
- Existing diplomacy records can be edited by tapping the faction and deleted with a trash button plus confirmation.
- Diplomacy comments remain optional and can be added/cleared after setting a status.
- Existing `bot.db` is compatible; no reset is required.
