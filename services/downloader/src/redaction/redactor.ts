/**
 * The single place secrets are scrubbed (plan §0 principle 3). Redaction
 * lives at this layer, and ONLY this layer, deliberately: sprinkling
 * `redact()` calls at every place a cookie might get logged means the one
 * call site everybody forgets is exactly the one that leaks. Every log/
 * stdout/stderr path in this service is expected to funnel through this
 * module (see src/redaction/stdio-redaction.ts and
 * src/logging/redact.logger.ts) instead of scrubbing anything itself.
 *
 * Two independent mechanisms, both required, because they catch different
 * shapes of leak:
 *
 * 1. Pattern -- known Douyin/TikTok session-cookie field names. Catches a
 *    cookie nobody thought to register, e.g. one that arrived inside an
 *    error body this process never touched directly.
 * 2. Exact-value registry -- a literal string, remembered once known (an
 *    API key, a DB password, a cookie value under a field name Douyin
 *    renames tomorrow). Catches secrets with no recognisable shape at all,
 *    which the pattern can never be extended to cover because there is
 *    nothing generic to match against.
 */

/**
 * Field list intentionally kept in lockstep with the gitleaks rule
 * `douyin-session-cookie` in the repo root `.gitleaks.toml`, so the CI
 * scanner (which looks at what got COMMITTED) and this runtime redactor
 * (which looks at what gets RUN) cannot drift apart and disagree about what
 * counts as a cookie field. `ttwid` and `msToken` are appended here but are
 * deliberately absent from the gitleaks rule: that rule is tuned against
 * committed files, and both fields are present in real Douyin cookies.
 */
const COOKIE_FIELD_NAMES = [
  'sessionid',
  'sessionid_ss',
  'sid_guard',
  'sid_tt',
  'uid_tt',
  'odin_tt',
  'passport_csrf_token',
  'passport_auth_status',
  'passport_auth_mix_state',
  'ttwid',
  'msToken',
] as const;

/**
 * Minimum value length for the pattern match, deliberately LOWER than
 * gitleaks' 16 (see the CI rule). The two scanners have asymmetric costs: a
 * false-positive redaction turns a log line's value into `[redacted]` for
 * nothing, which costs an operator a moment of squinting. A false negative
 * here is a live session cookie sitting in a log file. Bias hard toward the
 * former.
 */
const MIN_PATTERN_VALUE_LENGTH = 8;

/**
 * Charset Douyin actually uses for these cookie values (base64url-ish +
 * percent-encoding), WIDENED to also cover standard base64 (`+`, `/`, `=`):
 * a raw cookie value pushed through `POST /internal/cookie` and later
 * embedded in some other field is base64 more often than not, and the
 * original charset excluded exactly those three characters -- meaning a
 * base64 value would only ever get PARTIALLY redacted, up to its first `+`
 * or `/`, leaving the rest sitting in plain text right next to
 * `[redacted]`. `=` doubles as this pattern's own field/value separator, but
 * that is harmless: the separator is consumed by the `[:=]` alternation
 * below BEFORE the value charset ever starts matching, so an `=` inside the
 * class only ever matches base64 padding, never re-splits the field/value
 * boundary.
 */
const VALUE_CHARSET = 'A-Za-z0-9%._+/=-';

/**
 * Matches a cookie field paired with its value across every shape this
 * codebase can produce or receive:
 *
 *   - `field=value`        -- a literal Cookie/Set-Cookie header
 *   - `field="value"`      -- a quoted header value
 *   - `field: value`       -- a colon-separated log line
 *   - `"field":"value"`    -- JSON, specifically what redact.logger.ts's
 *                             `JSON.stringify(value)` branch emits for a
 *                             plain object argument (see redact.logger.ts)
 *
 * `"?` around both the separator and the value is what lets one pattern
 * cover all four: the field name's own closing quote (JSON keys are
 * quoted), and the value's surrounding quotes, are each optional and
 * consumed together with everything up to that point so the callback below
 * can normalise every shape to a plain `field=[redacted]`.
 *
 * The value charset intentionally excludes whitespace, `"`, `;`, `,`, and
 * `}` -- none of those ever appear inside a cookie value, so each is a safe,
 * unambiguous terminator that stops the match from swallowing whatever
 * comes after the value (a `; Path=/` attribute, a following JSON field, a
 * closing brace).
 */
/**
 * Capture groups exist so the replacement can put the ORIGINAL delimiters
 * back rather than normalising every hit to `field=[redacted]`.
 *
 * That normalisation is not cosmetic. A structured log line
 * `{"sessionid":"..."}` rewritten to `{"sessionid=[redacted]}` is no longer
 * parseable JSON, so redacting one field would silently destroy the whole
 * record for any aggregator ingesting it -- trading a leak for a different
 * kind of blindness. Preserving the quoting and the separator keeps the line
 * valid and keeps the redaction obvious to a human reading it.
 *
 *   1: field name   2: opening quote (or empty)   3: separator (: or =)
 *   4: quote before the value (or empty); the closing quote is optional so a
 *      TRUNCATED line (a cut-off log record, or a flush at the line-buffer
 *      cap) is still redacted rather than sailing through unmatched
 *   4: quote before the value (or empty)          5: the value itself
 */
const FIELD_VALUE_PATTERN = new RegExp(
  `\\b(${COOKIE_FIELD_NAMES.join(
    '|',
  )})("?)\\s*([:=])\\s*("?)[${VALUE_CHARSET}]{${MIN_PATTERN_VALUE_LENGTH},}\\4?`,
  'gi',
);

/**
 * Minimum length for a registered exact-value secret. See
 * {@link Redactor.registerSecret}.
 *
 * Exported so any boundary that ACCEPTS a secret this registry is meant to
 * protect can enforce the same floor from this one constant, rather than a
 * second hardcoded number that can silently drift out of sync -- see
 * `src/cookie/cookie.controller.ts`'s body schema, which uses this directly.
 * A schema that allowed a shorter cookie than the registry will actually
 * register would let an operator "successfully" push a cookie that then sits
 * completely unprotected by the exact-value mechanism.
 */
export const MIN_REGISTERED_SECRET_LENGTH = 8;

/**
 * Upper bound on how many exact-value secrets {@link Redactor} remembers at
 * once. FIX 9 (review): phase 9's browser extension pushes a cookie on every
 * 20-minute alarm, and Douyin rotates `msToken`/`ttwid` -- so most pushes
 * over a long-running process are a genuinely DISTINCT value, and an
 * unbounded registry grows for the lifetime of the process. Two costs, both
 * unbounded without a cap: `redact()` scans the WHOLE list on every single
 * stdout/stderr write, so per-write cost degrades with uptime; and every
 * cookie ever seen stays live in plaintext in the heap, so a heap snapshot
 * taken months into a deployment yields the complete cookie history, not
 * just the current one. Capped at a small, fixed size with FIFO eviction of
 * the OLDEST entry once full -- accepting that a very old cookie surfacing
 * in a very delayed log line would no longer be scrubbed, which is a far
 * smaller risk than unbounded memory growth and unbounded per-write cost.
 */
const MAX_REGISTERED_SECRETS = 32;

export class Redactor {
  /**
   * Longest-first so that if one registered secret is a substring of
   * another (e.g. a token and a longer token that embeds it), the longer,
   * more specific one is scrubbed first and doesn't get left half-redacted
   * by a shorter match.
   */
  private secrets: string[] = [];

  /**
   * Insertion order, oldest first -- kept SEPARATELY from `secrets` (which
   * is re-sorted longest-first on every insert and so cannot also answer
   * "which one arrived first"). This is what {@link MAX_REGISTERED_SECRETS}
   * eviction reads to find the oldest entry.
   */
  private insertionOrder: string[] = [];

  /**
   * Remembers a literal string to scrub anywhere it appears, regardless of
   * shape. This is what catches a secret the pattern mechanism was never
   * designed to recognise -- a DB password, an API key, a cookie field
   * Douyin renames tomorrow.
   *
   * Refuses values shorter than {@link MIN_REGISTERED_SECRET_LENGTH} and
   * values that are empty or whitespace-only: registering a short or blank
   * string would scrub half of every log line it happens to be a substring
   * of, which is strictly worse than not registering it at all. Registering
   * the same value twice is a no-op (backed by dedup on the internal list).
   *
   * Evicts the OLDEST registered secret once {@link MAX_REGISTERED_SECRETS}
   * would otherwise be exceeded -- see that constant's comment for why a cap
   * exists at all.
   */
  registerSecret(value: string): void {
    if (value.length < MIN_REGISTERED_SECRET_LENGTH) return;
    if (value.trim().length === 0) return;
    if (this.secrets.includes(value)) return;

    if (this.insertionOrder.length >= MAX_REGISTERED_SECRETS) {
      const oldest = this.insertionOrder.shift();
      if (oldest !== undefined) {
        const index = this.secrets.indexOf(oldest);
        if (index !== -1) this.secrets.splice(index, 1);
      }
    }

    this.secrets.push(value);
    this.insertionOrder.push(value);
    // Keep longest-first invariant after every insertion.
    this.secrets.sort((a, b) => b.length - a.length);
  }

  /** Forgets every registered secret. Tests only -- production never needs this. */
  clear(): void {
    this.secrets = [];
    this.insertionOrder = [];
  }

  /**
   * Applies both mechanisms, pattern first, then the exact-value registry.
   * Order does not matter for correctness (the two mechanisms scrub
   * disjoint concerns in practice), but pattern-first keeps the common case
   * -- an unregistered cookie field -- cheap: most calls have no registered
   * secrets to loop over at all.
   */
  redact(text: string): string {
    let result = text.replace(
      FIELD_VALUE_PATTERN,
      (_match, field: string, nameQuote: string, sep: string, valueQuote: string) =>
        `${field}${nameQuote}${sep}${valueQuote}[redacted]${valueQuote}`,
    );
    for (const secret of this.secrets) {
      if (result.includes(secret)) {
        result = result.split(secret).join('[redacted]');
      }
    }
    return result;
  }
}

/** Process-wide singleton. Every redaction layer (stdio patch, logger wrapper) shares this instance. */
export const redactor = new Redactor();
