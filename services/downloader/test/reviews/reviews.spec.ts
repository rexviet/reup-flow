import { INestApplication } from '@nestjs/common';
import { APP_GUARD } from '@nestjs/core';
import { Test } from '@nestjs/testing';
import request from 'supertest';
import { ApiKeyGuard } from '../../src/auth/api-key.guard';
import { ENV } from '../../src/config/config.module';
import { ReviewsController } from '../../src/reviews/reviews.controller';
import { ReviewsRepository } from '../../src/reviews/reviews.repository';
import { reviewQuerySchema, reviewsSchema } from '../../src/reviews/reviews.schemas';

const item = { awemeId: '7001', generation: 2, status: 'ready_for_review',
  projectKey: 'review/7001/g2/head.json', updatedAt: '2026-11-10T00:00:00.000Z' };

describe('review discovery boundary', () => {
  let app: INestApplication;
  const list = jest.fn();
  beforeAll(async () => {
    const ref = await Test.createTestingModule({
      controllers: [ReviewsController], providers: [
        { provide: ReviewsRepository, useValue: { list } },
        { provide: ENV, useValue: { API_KEY: 'review-test-key-only' } },
        { provide: APP_GUARD, useClass: ApiKeyGuard },
      ],
    }).compile();
    app = ref.createNestApplication();
    await app.init();
  });
  afterAll(async () => app.close());
  beforeEach(() => list.mockReset());
  it('requires the global API key', async () => {
    await request(app.getHttpServer()).get('/api/reviews').expect(401);
    await request(app.getHttpServer()).get('/api/reviews').set('X-Api-Key', 'wrong').expect(401);
    expect(list).not.toHaveBeenCalled();
  });
  it('validates pagination before querying', async () => {
    for (const query of ['limit=101', 'offset=-1', 'limit=1.5', 'offset=no', 'key=review/other']) {
      await request(app.getHttpServer()).get(`/api/reviews?${query}`).set('X-Api-Key', 'review-test-key-only').expect(400);
    }
    expect(list).not.toHaveBeenCalled();
  });
  it('returns both review terminal types without signing a URL', async () => {
    list.mockResolvedValue({ items: [item, { ...item, generation: 3, projectKey: 'review/7001/g3/head.json', status: 'needs_human' }], total: 2, limit: 20, offset: 0 });
    const res = await request(app.getHttpServer()).get('/api/reviews').set('X-Api-Key', 'review-test-key-only').expect(200);
    expect(reviewsSchema.parse(res.body).items).toHaveLength(2);
    expect(list).toHaveBeenCalledWith({ limit: 20, offset: 0 });
    expect(JSON.stringify(res.body)).not.toMatch(/https?:|secret|token|grant/);
  });
  it('reports unavailable storage without leaking errors', async () => {
    list.mockRejectedValue(new Error('private database details'));
    const res = await request(app.getHttpServer()).get('/api/reviews').set('X-Api-Key', 'review-test-key-only').expect(503);
    expect(res.text).not.toContain('private database details');
  });
  it('rejects a locator from another generation', () => {
    expect(() => reviewsSchema.parse({ items: [{ ...item, projectKey: 'review/7001/g1/head.json' }], total: 1, limit: 20, offset: 0 })).toThrow();
    expect(reviewQuerySchema.parse({})).toEqual({ limit: 20, offset: 0 });
  });
});
