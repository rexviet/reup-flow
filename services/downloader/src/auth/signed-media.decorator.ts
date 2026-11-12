import { SetMetadata } from '@nestjs/common';

/**
 * Marks the one route whose URL carries its own proof of access: `ApiKeyGuard` accepts a
 * valid media signature in the `:token` param there INSTEAD of the `X-Api-Key` header,
 * because `<video src>` and download links cannot send headers. It is not `@Public()`:
 * without a valid, unexpired signature the route answers 401 like every other.
 */
export const SIGNED_MEDIA_KEY = 'signedMedia';

export const SignedMedia = (): ReturnType<typeof SetMetadata> => SetMetadata(SIGNED_MEDIA_KEY, true);
