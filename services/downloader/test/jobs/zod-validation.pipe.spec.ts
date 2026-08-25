/**
 * Phase 3 (group B) -- the pipe that stands between every `/api` request body
 * /query/param and the service layer. Its whole reason to exist: a `:id`
 * that is not a UUID must be rejected as 400 here, not handed to Postgres,
 * which raises `22P02 invalid input syntax for type uuid` -- an opaque 500
 * with no relation to what the caller actually did wrong.
 */
import { BadRequestException } from '@nestjs/common';
import { z } from 'zod';
import { ZodValidationPipe } from '../../src/jobs/zod-validation.pipe';

describe('ZodValidationPipe', () => {
  it('returns the parsed (and default-filled) value on success', () => {
    const schema = z.object({ limit: z.coerce.number().int().min(1).max(100).default(20) });
    const pipe = new ZodValidationPipe(schema);

    expect(pipe.transform({})).toEqual({ limit: 20 });
    expect(pipe.transform({ limit: '5' })).toEqual({ limit: 5 });
  });

  it('throws BadRequestException (400) on a value that fails validation', () => {
    const schema = z.string().uuid();
    const pipe = new ZodValidationPipe(schema);

    expect(() => pipe.transform('not-a-uuid')).toThrow(BadRequestException);
  });

  it('never lets an invalid UUID reach a caller that would hand it to Postgres', () => {
    const pipe = new ZodValidationPipe(z.string().uuid());

    try {
      pipe.transform('short-link-not-an-id');
      throw new Error('expected transform to throw');
    } catch (err) {
      expect(err).toBeInstanceOf(BadRequestException);
    }
  });
});
