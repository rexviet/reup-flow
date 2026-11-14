import { render, screen, act, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as client from '../api/client';
import type { GlossaryTerm } from '../api/types';
import { CollectionGlossary } from './CollectionGlossary';
import { COLLECTION_ID } from './test-fixtures';

vi.mock('../api/client');

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function badRequest(...messages: string[]): Error {
  return Object.assign(new Error(messages.join('; ')), { status: 400, messages });
}

const mine: GlossaryTerm = { term_zh: '成名', term_vi: 'Nổi tiếng', note: 'tên phim', source: 'dashboard', source_aweme_id: null, created_at: '2026-11-14T00:00:00Z' };
const agent: GlossaryTerm = { term_zh: '代价', term_vi: 'Cái giá', note: null, source: 'agent', source_aweme_id: '7001', created_at: '2026-11-14T00:00:00Z' };

beforeEach(() => {
  vi.mocked(client.getGlossary).mockResolvedValue({ items: [mine, agent] });
  vi.mocked(client.putGlossaryTerm).mockResolvedValue(mine);
  vi.mocked(client.deleteGlossaryTerm).mockResolvedValue(undefined);
});

afterEach(() => {
  vi.resetAllMocks();
});

describe('CollectionGlossary', () => {
  it('lists terms with their source; an agent term links to the video it came from', async () => {
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    const [first, second] = screen.getAllByTestId('glossary-row');
    expect(first).toHaveTextContent('成名');
    expect(first).toHaveTextContent('Nổi tiếng');
    expect(first).toHaveTextContent('tên phim');
    expect(first).toHaveTextContent('Tự đặt');
    expect(second).toHaveTextContent('Agent');
    expect(within(second).getByRole('link', { name: '7001' })).toHaveAttribute('href', '#/video/7001');
  });

  it('filters on the client across both terms and the note', async () => {
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.type(screen.getByLabelText('Lọc thuật ngữ'), 'cái');
    expect(screen.getAllByTestId('glossary-row')).toHaveLength(1);
    expect(screen.getByTestId('glossary-row')).toHaveTextContent('代价');
    expect(client.getGlossary).toHaveBeenCalledTimes(1);
  });

  it('adds a term with PUT under its Chinese key; a blank note is null', async () => {
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.type(screen.getByLabelText('中文 mới'), '薛珍珠');
    await userEvent.type(screen.getByLabelText('Tiếng Việt mới'), 'Tiết Trân Châu');
    await userEvent.click(screen.getByRole('button', { name: 'Thêm thuật ngữ' }));
    await flush();
    expect(client.putGlossaryTerm).toHaveBeenCalledWith(COLLECTION_ID, '薛珍珠', { term_vi: 'Tiết Trân Châu', note: null });
    expect(client.getGlossary).toHaveBeenCalledTimes(2);
  });

  it('edits a term in place and PUTs it under that row\'s key', async () => {
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Sửa 代价' }));
    const vi_ = screen.getByLabelText('Tiếng Việt của 代价');
    await userEvent.clear(vi_);
    await userEvent.type(vi_, 'Cái giá phải trả');
    await userEvent.type(screen.getByLabelText('Ghi chú của 代价'), 'tập 1');
    await userEvent.click(screen.getByRole('button', { name: 'Lưu 代价' }));
    await flush();
    expect(client.putGlossaryTerm).toHaveBeenCalledWith(COLLECTION_ID, '代价', { term_vi: 'Cái giá phải trả', note: 'tập 1' });
  });

  it('a validation error appears beside the field it names', async () => {
    vi.mocked(client.putGlossaryTerm).mockRejectedValue(badRequest('term_vi: term_vi contains digits that term_zh does not: 3'));
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.type(screen.getByLabelText('中文 mới'), '三');
    await userEvent.type(screen.getByLabelText('Tiếng Việt mới'), 'ba 3');
    await userEvent.click(screen.getByRole('button', { name: 'Thêm thuật ngữ' }));
    await flush();
    expect(screen.getByLabelText('Tiếng Việt mới')).toHaveAccessibleDescription(/digits that term_zh does not: 3/);
    expect(screen.getByLabelText('中文 mới')).not.toHaveAccessibleDescription(/digits/);
    expect(screen.getByLabelText('Tiếng Việt mới')).toHaveValue('ba 3');
  });

  it('an edit error appears beside the edited row\'s field', async () => {
    vi.mocked(client.putGlossaryTerm).mockRejectedValue(badRequest('note: note contains a URL, domain name, e-mail address or phone number'));
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Sửa 成名' }));
    await userEvent.type(screen.getByLabelText('Ghi chú của 成名'), ' x.com');
    await userEvent.click(screen.getByRole('button', { name: 'Lưu 成名' }));
    await flush();
    expect(screen.getByLabelText('Ghi chú của 成名')).toHaveAccessibleDescription(/URL/);
    expect(screen.getByLabelText('Tiếng Việt của 成名')).not.toHaveAccessibleDescription(/URL/);
  });

  it('deletes a term after confirmation', async () => {
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Xóa 成名' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(client.deleteGlossaryTerm).toHaveBeenCalledWith(COLLECTION_ID, '成名');
  });

  it('an edit error about the Chinese key is shown in that row', async () => {
    vi.mocked(client.putGlossaryTerm).mockRejectedValue(badRequest('term_zh: term_zh is longer than 64 characters'));
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Sửa 成名' }));
    await userEvent.click(screen.getByRole('button', { name: 'Lưu 成名' }));
    await flush();
    const row = screen.getAllByTestId('glossary-row')[0];
    expect(within(row).getByText('term_zh is longer than 64 characters')).toBeInTheDocument();
  });

  it('asks before overwriting a term that only differs by Unicode form or spaces', async () => {
    const cafe: GlossaryTerm = { ...agent, term_zh: 'caf\u00e9', term_vi: 'quán' };
    vi.mocked(client.getGlossary).mockResolvedValue({ items: [cafe] });
    render(<CollectionGlossary collectionId={COLLECTION_ID} />);
    await flush();
    await userEvent.type(screen.getByLabelText('中文 mới'), ' cafe\u0301\u3000');
    await userEvent.type(screen.getByLabelText('Tiếng Việt mới'), 'cà phê');
    await userEvent.click(screen.getByRole('button', { name: 'Thêm thuật ngữ' }));
    expect(screen.getByRole('alertdialog')).toHaveTextContent('Ghi đè?');
    expect(client.putGlossaryTerm).not.toHaveBeenCalled();
  });
});
