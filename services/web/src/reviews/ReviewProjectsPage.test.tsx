import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import * as client from '../api/client';
import { ReviewProjectsPage } from './ReviewProjectsPage';

vi.mock('../api/client');
afterEach(() => vi.resetAllMocks());
const project = { awemeId: '7001', generation: 2, status: 'ready_for_review' as const, projectKey: 'review/7001/g2/head.json', updatedAt: '2026-11-10T00:00:00Z' };
it('shows loading then empty discovery with launch prerequisites', async () => {
  vi.mocked(client.listReviews).mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  render(<ReviewProjectsPage />);
  expect(screen.getByRole('status')).toHaveTextContent('Đang tải');
  expect(await screen.findByText('Chưa có project ở trang này.')).toBeInTheDocument();
  expect(screen.getByText(/bridge chưa chạy/)).toBeInTheDocument();
});
it('shows both review statuses and a local command without granting browser authority', async () => {
  vi.mocked(client.listReviews).mockResolvedValue({ items: [project, { ...project, generation: 3, status: 'needs_human', projectKey: 'review/7001/g3/head.json' }], total: 21, limit: 20, offset: 0 });
  render(<ReviewProjectsPage />);
  expect(await screen.findByText('Sẵn sàng review')).toBeInTheDocument();
  expect(screen.getByText('Cần người sửa')).toBeInTheDocument();
  expect(screen.getByText('node scripts/review-studio.mjs open 7001 2')).toBeInTheDocument();
  expect(screen.queryByRole('link')).toBeNull();
  vi.mocked(client.listReviews).mockResolvedValue({ items: [], total: 21, limit: 20, offset: 20 });
  await userEvent.click(screen.getByRole('button', { name: 'Trang sau' }));
  expect(await screen.findByText('Chưa có project ở trang này.')).toBeInTheDocument();
  expect(client.listReviews).toHaveBeenLastCalledWith(20, 20);
});
it('shows an actionable API error', async () => {
  vi.mocked(client.listReviews).mockRejectedValue(new Error('offline'));
  render(<ReviewProjectsPage />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Kiểm tra API và migration');
});
