import { z } from 'zod';

export const reviewQuerySchema = z.object({
  limit: z.coerce.number().int().min(1).max(100).default(20),
  offset: z.coerce.number().int().min(0).max(1_000_000).default(0),
}).strict();
export type ReviewQuery = z.infer<typeof reviewQuerySchema>;

const reviewItemSchema = z.object({
  awemeId: z.string().regex(/^\d{1,32}$/),
  generation: z.number().int().min(0).max(1_000_000),
  status: z.enum(['ready_for_review', 'needs_human']),
  projectKey: z.string(),
  updatedAt: z.string().datetime({ offset: true }),
}).refine((r) => r.projectKey === `review/${r.awemeId}/g${r.generation}/head.json`, {
  message: 'project locator does not match run identity',
});
export const reviewsSchema = z.object({
  items: z.array(reviewItemSchema),
  total: z.number().int().nonnegative(),
  limit: z.number().int().positive(),
  offset: z.number().int().nonnegative(),
});
export type ReviewList = z.infer<typeof reviewsSchema>;
