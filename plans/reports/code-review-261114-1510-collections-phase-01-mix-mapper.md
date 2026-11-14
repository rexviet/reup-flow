# Code review: collections phase 1, mix_info and nickname mapper

Scope: `services/downloader/src/parser/aweme.mapper.ts` (diff), `src/parser/display-name.ts` (new),
`test/lanes/aweme.mapper.spec.ts` (diff), `test/parser/display-name.spec.ts` (new), fixtures
`test/fixtures/aweme/user-post-mix-page.json`, `video-detail-mix.json`. Read-only review.

## Verdict

**Ready for phase 1, with one Medium to fix before phase 3 writes `mix_episode`.** The requirements
are met as written. Extra keys are safe in `upsertMany`, and the fixtures contain no secrets. The
weak spot is `episode`. The mapper accepts any positive integer, but phase 2 stores it as int4.

## Findings

### Medium: `episode` is unbounded, but the target column is `integer`
`aweme.mapper.ts:122,127`. `numeric()` plus `Number.isInteger(e) && e > 0` accepts `1e300`, `2**40`,
`'0x10'` (gives 16) and `'1e1'` (gives 10). All four were probed and confirmed. Phase 2 defines
`videos.mix_episode integer NULL CHECK (mix_episode > 0)` (`phase-02-...md:49`). In phase 3, one
hostile or broken `current_episode` above 2^31-1 raises `integer out of range`. That error fails the
whole multi-row `upsertMany`/assignment statement, so the entire enumerate page fails, not just
that video. The "a bad field nulls, never drops" rule then breaks in the worst direction.
Fix: `Number.isSafeInteger(e) && e > 0 && e <= 2_147_483_647 ? e : null`. Optionally accept only
`/^\d+$/` strings for the string form. Add tests for `2**31` and `1e300`.

### Medium (product question): `authorNickname` is not tied to the channel being enumerated
`aweme.mapper.ts:136-139`. The nickname comes from `aweme.author` without checking
`author.sec_uid === secUserId`. In the enumerate lane, `secUserId` is `job.secUserId`, the profile
being listed. A co-created (共创) or reposted item can list a different author. Phase 3 plans to
name the channel collection from the item's nickname and to write `profiles.nickname`
(`phase-03-...md:32-33`). The wrong channel name would then be persisted. The fallback has the
same flaw: it returns the profile's `secUserId` for a name that may belong to another author.
Fix: when `author.sec_uid` is present and differs from `secUserId`, return `null`. This costs one
comparison, and `videos.service.ts:51` already derives `secUserId` from the same `author`, so
nothing changes on that path.

### Low: NFC runs before stripping, so the output can be non-NFC
`display-name.ts:47-49`. `cleanDisplayName('e​̂')` returns `'ê'`, which fails
`=== .normalize('NFC')`. That defeats the documented goal ("a decomposed and a composed spelling
are the same string"). Identity is keyed by `mixId`, so the impact is display and comparison only.
Fix: add `.normalize('NFC')` after the Cf/Cc strip. Keeping the first call is harmless.

### Low: `mix_id` is not bounded in length and accepts `"0"`
`aweme.mapper.ts:119`. `'9'.repeat(5000)` and `'0'` both pass. The spec only says "non-empty
digit string", so this conforms. Still, Douyin ids are at most 19-20 digits, and `"0"` is a common
"none" sentinel on that API. Consider `/^[1-9]\d{0,24}$/`.

### Low (accepted trade-off, note only): stripping Cf also removes ZWJ/ZWNJ and tag characters
`display-name.ts:9`. Emoji ZWJ sequences split (a family emoji becomes three separate emoji), and
ZWNJ-dependent scripts may render differently. The spec requires stripping Cf, so this is noted,
not flagged. Lone surrogates (Cs) also pass through. node-postgres encodes them as U+FFFD, so
there is no crash.

### Info: test location differs from the spec
The phase file says the mapper test lives under `test/parser/`. It was extended in place at
`test/lanes/aweme.mapper.spec.ts`, where it already lived. This is the right call.

## Verified (no action)

- **`upsertMany` with extra keys, at runtime.** `videos.repository.ts:152-153` spreads `...row`.
  In drizzle-orm 0.45.2, `insert.js:34-41` wraps every key in a `Param`, but `dialect.js:359,374`
  builds the column list and values only from `table[Columns]`. `mix` and `authorNickname` are
  therefore never serialised. `schema.ts` has no `videos` column under either key. A future column
  with the same TS key would be written silently, so keep these names distinct from phase 2
  columns (`mixEpisode` and `collectionId` do not collide).
- **Type level.** `MappedAweme extends VideoUpsert` is assignable to `VideoUpsert[]`. The spread is
  inside `rows.map` on a `VideoUpsert`-typed value, so no excess-property check applies. The
  callers (`enumerate.lane.ts:314-320`, `videos.service.ts:52-62`) compile unchanged.
- **Fixtures.** No cookie, token, msToken, ttwid, X-Bogus or query string. URLs are
  `https://<host>/scrubbed`. They contain `uid`, `sec_uid` and `log_pb.impr_id`, which are public
  and per-request identifiers, not credentials. The envelope shape is preserved, including the
  item with no `mix_info` (`7696076126451518735`).
- **Requirements.** `mixId` is a digit string and a numeric `mix_id` is refused. Episode is null
  for 0, negative, fractional, text or missing values. `cleanDisplayName` applies NFC, strips Cf
  then collapses whitespace before stripping Cc (so a newline does not glue words), cuts to 200
  code points, and falls back when empty. Its doc comment explicitly says it is not a file-name
  sanitiser, and there are no file-name callers (grep).
- **Tests are not vacuous.** `pageItems()` throws on an empty list. The real-fixture tests assert
  exact counts (11 rows, 10 in the mix) and the episode set 1..10. The edge tables cover every
  rejection branch. This is consistent with the reported 17/17 mutations killed.

## Recommended actions
1. Bound `episode` to the int4 range with `isSafeInteger`, and add tests (before phase 3).
2. Decide whether `authorNickname` is null when `author.sec_uid` differs from `secUserId`; this
   changes what phase 3 writes to `profiles.nickname`.
3. Re-normalise NFC after stripping in `cleanDisplayName`.
4. Optional: tighten the `mix_id` regex.

## Unresolved questions
- Can Douyin's `fetch_user_post_videos` return items whose `author` is not the listed profile
  (共创 / co-creation)? The answer decides finding 2.

## Resolution (implementer, same session)

All four recommended actions were applied test-first (each new test was red before the fix):

1. `episode` is now read by `readEpisode`: a JSON number or a plain decimal digit string only
   (`'0x10'` and `'1e1'` are refused), `Number.isSafeInteger`, `> 0` and `<= 2_147_483_647`.
   Tests cover 2^31, 1e300, hex and exponent text, and the int4 maximum itself.
2. `authorNickname` is null when `author.sec_uid` is present and differs from the channel being
   read. This is not a product choice: it only declines to write another account's name; a null
   nickname already falls back to the channel id downstream. An item with no `sec_uid` is still
   read, because being on the channel's post list is the evidence that it is the channel's post.
3. `cleanDisplayName` applies NFC once, AFTER stripping (the earlier NFC became redundant and was
   removed); `'e​̂'` now returns `'ê'`.
4. `mix_id` must match `^[1-9]\d*$`; `"0"` and zero-padded ids are refused.

The ZWJ-emoji trade-off is accepted as specified (Cf is stripped by requirement).

Re-verified: 56/56 targeted tests, tsc and eslint clean, DB suites through `upsertMany`
(enumerate, image-post-skip, video-enqueue) 82/82, and a 22-mutation sweep with every mutation
killed.
