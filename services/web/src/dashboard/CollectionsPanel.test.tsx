import { fireEvent, render, screen, act, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as client from '../api/client';
import { CollectionsPanel } from './CollectionsPanel';
import { collection, COLLECTION_ID } from './test-fixtures';

vi.mock('../api/client');

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** What the API client throws for a 400: each zod issue as `<field>: <message>`. */
function badRequest(...messages: string[]): Error {
  return Object.assign(new Error(messages.join('; ')), { status: 400, messages });
}

const mix = collection({
  name: 'Phim A',
  kind: 'mix',
  video_count: 10,
  caption_text_color: '#ffff00',
  save_subdir: 'phim/a',
});

beforeEach(() => {
  vi.mocked(client.getGlossary).mockResolvedValue({ items: [] });
  vi.mocked(client.updateCollection).mockResolvedValue(mix);
});

afterEach(() => {
  vi.resetAllMocks();
});

describe('CollectionsPanel list', () => {
  it('shows name, kind, video count and save folder of each collection', () => {
    const manual = collection({ id: 'c-2', name: 'Tự gom', kind: 'manual', channel: null, video_count: 0 });
    render(<CollectionsPanel collections={[mix, manual]} onChanged={() => {}} />);
    const [first, second] = screen.getAllByTestId('collection-row');
    expect(first).toHaveTextContent('Phim A');
    expect(first).toHaveTextContent('合集');
    expect(first).toHaveTextContent('10 video');
    expect(first).toHaveTextContent('phim/a');
    expect(second).toHaveTextContent('Tạo tay');
    expect(second).toHaveTextContent('Chưa đặt thư mục');
  });

  it('creates a collection by name', async () => {
    const onChanged = vi.fn();
    vi.mocked(client.createCollection).mockResolvedValue(collection({ id: 'c-new', name: 'Mới' }));
    render(<CollectionsPanel collections={[]} onChanged={onChanged} />);
    await userEvent.type(screen.getByLabelText('Tên collection mới'), '  Mới ');
    await userEvent.click(screen.getByRole('button', { name: 'Tạo collection' }));
    await flush();
    expect(client.createCollection).toHaveBeenCalledWith({ name: 'Mới' });
    expect(onChanged).toHaveBeenCalled();
  });
});

describe('CollectionsPanel settings', () => {
  async function openEditor() {
    render(<CollectionsPanel collections={[mix]} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: 'Sửa Phim A' }));
    await flush();
  }

  it('an emptied colour is sent as null (the shared default); the slider sends a whole percentage', async () => {
    await openEditor();
    expect(screen.getByTestId('swatch-caption_text_color')).toHaveStyle({ backgroundColor: '#ffff00' });
    await userEvent.clear(screen.getByLabelText('Màu chữ'));
    await userEvent.click(screen.getByLabelText('Dùng độ mờ chung'));
    fireEvent.change(screen.getByLabelText('Độ mờ hộp nền'), { target: { value: '40' } });
    await userEvent.type(screen.getByLabelText('Độ lệch nhạc nền (dB)'), '-3.5');
    await userEvent.click(screen.getByRole('button', { name: 'Lưu setting' }));
    await flush();
    // Only what changed, so an edit made elsewhere since the form opened is not reverted.
    expect(client.updateCollection).toHaveBeenCalledWith(COLLECTION_ID, {
      bgm_offset_db: -3.5,
      caption_text_color: null,
      cover_opacity_pct: 40,
    });
  });

  it('sends nothing when nothing changed', async () => {
    await openEditor();
    await userEvent.click(screen.getByRole('button', { name: 'Lưu setting' }));
    await flush();
    expect(client.updateCollection).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Không có thay đổi.');
  });

  it('shows the save folder under the Mac root prefix', async () => {
    await openEditor();
    expect(screen.getByTestId('save-root-prefix')).toHaveTextContent('Thư mục gốc trên Mac/');
    expect(screen.getByLabelText('Thư mục lưu')).toHaveValue('phim/a');
  });

  it('a colour that is not #rrggbb is caught beside its field before anything is sent', async () => {
    await openEditor();
    await userEvent.clear(screen.getByLabelText('Màu hộp nền'));
    await userEvent.type(screen.getByLabelText('Màu hộp nền'), 'vàng');
    await userEvent.click(screen.getByRole('button', { name: 'Lưu setting' }));
    expect(client.updateCollection).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Màu hộp nền')).toHaveAccessibleDescription(/#rrggbb/);
  });

  it('a validation error from the server appears beside the field it names', async () => {
    vi.mocked(client.updateCollection).mockRejectedValue(badRequest('save_subdir: must be a relative path whose every segment is a name'));
    await openEditor();
    await userEvent.clear(screen.getByLabelText('Thư mục lưu'));
    await userEvent.type(screen.getByLabelText('Thư mục lưu'), '/abs');
    await userEvent.click(screen.getByRole('button', { name: 'Lưu setting' }));
    await flush();
    expect(screen.getByLabelText('Thư mục lưu')).toHaveAccessibleDescription(/relative path/);
    expect(screen.getByLabelText('Màu chữ')).not.toHaveAccessibleDescription(/relative path/);
  });

  it('opens in a dialog named after the collection, which the close button and Escape both close', async () => {
    await openEditor();
    expect(screen.getByRole('dialog', { name: 'Sửa «Phim A»' })).toBeInTheDocument();
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Đóng' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Sửa Phim A' }));
    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('a click on the backdrop closes the dialog; a click inside it does not', async () => {
    await openEditor();
    await userEvent.click(screen.getByLabelText('Tên collection'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('dialog'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('has a Glossary tab', async () => {
    await openEditor();
    await userEvent.click(screen.getByRole('tab', { name: 'Glossary' }));
    await flush();
    expect(client.getGlossary).toHaveBeenCalledWith(COLLECTION_ID);
    expect(within(screen.getByRole('tabpanel')).getByLabelText('Lọc thuật ngữ')).toBeInTheDocument();
  });
});
