import { Body, Controller, Inject, NotFoundException, Param, Patch } from '@nestjs/common';
import type { NodePgDatabase } from 'drizzle-orm/node-postgres';
import { z } from 'zod';
import { DB } from '../db/db.module';
import * as schema from '../db/schema';
import { ZodValidationPipe } from '../jobs/zod-validation.pipe';
import { setProfileAutoLocalize } from './intake.repository';

export const patchProfileSchema = z.object({ auto_localize: z.boolean() });
export type PatchProfileDto = z.infer<typeof patchProfileSchema>;

/** Channel-level switch the dashboard flips; only affects videos that finish later. */
@Controller('api/profiles')
export class ProfilesController {
  constructor(@Inject(DB) private readonly db: NodePgDatabase<typeof schema>) {}

  @Patch(':secUserId')
  async patch(
    @Param('secUserId') secUserId: string,
    @Body(new ZodValidationPipe(patchProfileSchema)) body: PatchProfileDto,
  ): Promise<{ sec_user_id: string; auto_localize: boolean }> {
    const found = await setProfileAutoLocalize(this.db, secUserId, body.auto_localize);
    if (!found) throw new NotFoundException(`profile ${secUserId} not found`);
    return { sec_user_id: secUserId, auto_localize: body.auto_localize };
  }
}
