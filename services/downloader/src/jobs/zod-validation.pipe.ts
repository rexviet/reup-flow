import { BadRequestException, Injectable, type PipeTransform } from '@nestjs/common';
import type { ZodTypeAny } from 'zod';

/**
 * The one gate every `/api` body, query, and route param passes through
 * before it reaches a controller method.
 *
 * Shared by jobs.controller.ts and videos.controller.ts rather than one copy
 * per controller: the load-bearing behaviour -- reject with 400, never let
 * an unvalidated value fall through to a repository call -- must not drift
 * between them. The concrete case this exists for: a `:id` route param that
 * is not a UUID. Passed straight to Postgres, that raises `22P02 invalid
 * input syntax for type uuid`, which Nest has no special handling for and
 * which therefore surfaces as an opaque 500 -- a caller who typo'd an id
 * would see "internal server error" for what is unambiguously their own bad
 * input.
 */
@Injectable()
export class ZodValidationPipe implements PipeTransform {
  constructor(private readonly schema: ZodTypeAny) {}

  transform(value: unknown): unknown {
    const result = this.schema.safeParse(value);
    if (!result.success) {
      const messages = result.error.issues.map((issue) => {
        const path = issue.path.join('.') || '(root)';
        return `${path}: ${issue.message}`;
      });
      throw new BadRequestException(messages);
    }
    // Not the raw input: zod fills in defaults (e.g. `limit`/`offset`) and
    // coerces query-string numbers, and the caller needs THAT value, not
    // the untouched one it just validated.
    return result.data;
  }
}
