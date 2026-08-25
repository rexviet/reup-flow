/**
 * The two -- and only two -- things this client is able to say about a
 * failed parser call.
 *
 * F4, and the reason this file exists in phase 2 rather than phase 7:
 *
 * Every route in the fork is shaped like
 *
 *     try:
 *         data = await DouyinWebCrawler.<something>(...)
 *         return ResponseModel(code=200, ...)
 *     except Exception as e:
 *         detail = ErrorResponseModel(code=400, router=..., params=...)
 *         raise HTTPException(status_code=400, detail=detail.dict())
 *
 * -- see app/api/endpoints/douyin_web.py. Note what is missing: `e`. It is
 * bound and then never used. `ErrorResponseModel.message` is a pydantic
 * class default, the literal string "An error occurred.", identical for a
 * banned cookie, a private profile, a deleted video, a network blip, and a
 * genuine bug in the crawler. `str(e)` is never interpolated into it.
 *
 * So an HTTP 400 from the parser carries EXACTLY ZERO diagnostic
 * information. Not "little" -- zero. Anything this client claimed about the
 * cause of a 400 would be invention, and inventing here is expensive in a
 * specific way: IDEA.md §6.2 would have three consecutive errors reported as
 * `blocked`, sending an operator off to rotate a perfectly good cookie
 * because a profile happened to be private.
 *
 * Distinguishing the causes needs an ADDITIONAL observation the failing call
 * cannot supply -- a probe of a known-public profile with the same cookie.
 * That is phase 7's job, and it is deliberately not attempted here. Plan §0
 * principle 2 rules out the other obvious fix (patching the fork to
 * propagate `str(e)`): the patch budget exists so the fork can still be
 * merged with upstream when Douyin changes `a_bogus`.
 *
 * If you are about to add a third error class, or parse the 400 body for
 * hints, read plan.md §2 F4 first.
 */

/** Base class so a caller can catch every parser failure in one clause. */
export abstract class ParserError extends Error {
  protected constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

/**
 * The parser did not answer: connection refused, DNS failure, timeout, a
 * 5xx from uvicorn or anything in front of it, or a 200 whose body is not
 * the envelope this client speaks.
 *
 * The common thread is that no Douyin-level verdict was ever reached, so
 * retrying later is meaningful. Retrying is the LANE's decision, not this
 * client's -- see the note on `ParserClient.get`.
 */
export class ParserUnavailableError extends ParserError {
  constructor(
    message: string,
    readonly cause?: unknown,
  ) {
    super(message);
  }
}

/**
 * The parser answered, and the answer was a refusal.
 *
 * `status` is carried so phase 7 can tell the two very different refusals
 * apart WITHOUT this client having guessed:
 *
 *   - 400 -- the fork's catch-all. Cause unknown by construction; this is
 *     what the probe is for.
 *   - anything else (404, 422, ...) -- we and the fork disagree about the
 *     route or its parameters. That is a bug on this side, and probing for a
 *     banned cookie in response to it wastes a request and misleads whoever
 *     reads the job's error.
 *
 * Carrying the status is not the same as interpreting it: this class makes
 * no claim about WHY a 400 happened.
 */
export class ParserRejectedError extends ParserError {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}
