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

/** A 720×1280 review project whose source subtitles sit at y=1000, 120 px tall. */
const preview = {
  aweme_id: '7677937301649142961',
  generation: 1,
  width: 720,
  height: 1280,
  source_region: { x: 10, y: 1000, w: 700, h: 120 },
  sample_ms: 2000,
  source_url: '/api/dashboard/media/tok',
};

beforeEach(() => {
  vi.mocked(client.getGlossary).mockResolvedValue({ items: [] });
  vi.mocked(client.getCoverPreview).mockResolvedValue(preview);
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

  it('sends the cover geometry as whole numbers, and an emptied one as null (automatic)', async () => {
    vi.mocked(client.updateCollection).mockResolvedValue(mix);
    render(<CollectionsPanel collections={[{ ...mix, cover_width_pct: 80 }]} onChanged={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: 'Sửa Phim A' }));
    await flush();
    expect(screen.getByLabelText('Chiều rộng hộp nền (%)')).toHaveValue(80);
    await userEvent.clear(screen.getByLabelText('Chiều rộng hộp nền (%)'));
    await userEvent.type(screen.getByLabelText('Dời hộp nền theo chiều dọc (%)'), '-3');
    await userEvent.type(screen.getByLabelText('Chiều cao hộp nền (%)'), '12');
    await userEvent.click(screen.getByRole('button', { name: 'Lưu setting' }));
    await flush();
    expect(client.updateCollection).toHaveBeenCalledWith(COLLECTION_ID, {
      cover_offset_pct: -3,
      cover_width_pct: null,
      cover_height_pct: 12,
    });
  });

  it.each([
    ['Dời hộp nền theo chiều dọc (%)', '51', /từ −50 đến 50/],
    ['Chiều rộng hộp nền (%)', '19', /từ 20 đến 100/],
    ['Chiều cao hộp nền (%)', '12.5', /từ 3 đến 50/],
  ])('%s out of range (%s) is caught beside the field before anything is sent', async (label, value, message) => {
    await openEditor();
    await userEvent.type(screen.getByLabelText(label), value);
    await userEvent.click(screen.getByRole('button', { name: 'Lưu setting' }));
    expect(client.updateCollection).not.toHaveBeenCalled();
    expect(screen.getByLabelText(label)).toHaveAccessibleDescription(message);
  });

  it('previews the box on a frame of a collection video, following the values as they are typed', async () => {
    await openEditor();
    expect(client.getCoverPreview).toHaveBeenCalledWith(COLLECTION_ID);
    const video = screen.getByLabelText('Khung hình xem trước');
    expect(video).toHaveAttribute('src', '/api/dashboard/media/tok');
    expect((video as HTMLVideoElement).muted).toBe(true);
    const box = screen.getByTestId('cover-preview-box');
    // Automatic: the source band, 60 % wide, in the worker's default colour.
    expect(box.style.top).toBe('78.125%');
    expect(box.style.height).toBe('9.375%');
    expect(box.style.width).toBe('60%');
    expect(box.style.left).toBe('20%');
    expect(box).toHaveStyle({ backgroundColor: '#101010', opacity: '0.92' });

    await userEvent.type(screen.getByLabelText('Dời hộp nền theo chiều dọc (%)'), '3');
    await userEvent.type(screen.getByLabelText('Chiều rộng hộp nền (%)'), '100');
    await userEvent.clear(screen.getByLabelText('Màu hộp nền'));
    await userEvent.type(screen.getByLabelText('Màu hộp nền'), '#FF0000');
    await userEvent.click(screen.getByLabelText('Dùng độ mờ chung'));
    fireEvent.change(screen.getByLabelText('Độ mờ hộp nền'), { target: { value: '50' } });
    expect(box.style.top).toBe(`${(1038 / 1280) * 100}%`);
    expect(box.style.width).toBe('100%');
    expect(box.style.left).toBe('0%');
    expect(box).toHaveStyle({ backgroundColor: '#ff0000', opacity: '0.5' });
    expect(client.updateCollection).not.toHaveBeenCalled();
  });

  it('says so when no video of the collection has a review project to preview', async () => {
    vi.mocked(client.getCoverPreview).mockRejectedValue(Object.assign(new Error('Bộ sưu tập chưa có video'), { status: 404 }));
    await openEditor();
    expect(screen.getByTestId('cover-preview-missing')).toHaveTextContent('Chưa có video nào của collection có project review để xem trước.');
    expect(screen.queryByTestId('cover-preview-box')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Chiều cao hộp nền (%)')).toBeInTheDocument();
  });

  it('has a Glossary tab', async () => {
    await openEditor();
    await userEvent.click(screen.getByRole('tab', { name: 'Glossary' }));
    await flush();
    expect(client.getGlossary).toHaveBeenCalledWith(COLLECTION_ID);
    expect(within(screen.getByRole('tabpanel')).getByLabelText('Lọc thuật ngữ')).toBeInTheDocument();
  });
});
