# Documentation

Welcome to the Reup Pipeline documentation. Start here based on your role.

## For Operators (2am wake-up call)

- **[Parser Fork Operational Guide](./parser-fork.md)** — The Douyin cookie stops rotating, requests fail, or it's time for the weekly upstream pull. Covers failure modes, how to rotate the cookie at runtime, and troubleshooting.

## For Architects and Product Leads

- **[Solution Design — Rev 5](./reports/solution-design-2026-08-24-reup-pipeline.md)** — Full system design. Covers all architectural decisions (D1–D7), evaluated trade-offs, database schema, module boundaries, and risks. Read §7.2 for the parser fork deep-dive (what changed, why, and the security fix for a cookie that was leaking).

## For Engineers Implementing the System

- **[IDEA.md](../IDEA.md)** — Original specification document. Read §4 for the parser patch scope and tiered checklist.

## For Maintaining the Parser Fork

- **[PATCH_NOTES.md](../src/Douyin_TikTok_Download_API/PATCH_NOTES.md)** — The pull-upstream checklist. Kept in the fork itself so it travels with the code. Run this every time you pull from upstream.

---

## Key Architecture Points

**Three modules, connected only through database state:**

| Module | Role | Entry |
|--------|------|-------|
| 1. **Ingest** | Download all videos from a Douyin profile | `enumerate` lane reads `sec_user_id` → `download` lane fills storage |
| 2. **Dubbing** | Clone voice, dub to Vietnamese, caption, conform to 9:16 | GPU lane polls for `pending_dub` |
| 3. **Publish** | Schedule and post to Facebook pages | Reviewer approves → slot allocator assigns time → Facebook Graph API |

No direct calls between modules. A video's progression from "discovered" to "published" is written to the database, and each module reads its own stage.

---

## The Cookie Problem (Why It Matters)

The Douyin parser needs a fresh cookie every ~20 minutes. The old approach: reload the whole process. The new approach: read from Redis on every request, so the extension can push a new cookie without any downtime.

See [Parser Fork Operational Guide](./parser-fork.md) for the full story — including the security issue that was discovered (a live Douyin cookie was being committed to the fork, and how it was fixed).

---

## Quick Reference

| Component | Technology | Status |
|-----------|-----------|--------|
| **Parser fork** | Python, forked from upstream | Patched, 4 tests, ~40 lines |
| **Orchestrator API** | NestJS, TypeScript | Planned |
| **Worker** | NestJS, TypeScript | Planned |
| **Database** | PostgreSQL 16 + Drizzle ORM | Schema designed |
| **Storage** | Local disk (no MinIO) | Design rationale in §5 D5 |
| **Extension** | MV3, Chrome cookies API | Planned |
| **Frontend** | React + Vite | Planned |

---

## Decision Log

Every major architectural decision was documented in the solution design, with:
- The question asked
- Approaches evaluated (usually 2–3)
- Trade-offs accepted
- The decision made

If you're wondering "why didn't we use X instead of Y?" — chances are it's in there. The document is long because the design space is real: data on each trade-off, not guesses.

---

**Last updated:** 2026-08-24 · **Author:** Solution Architect + Implementation findings
