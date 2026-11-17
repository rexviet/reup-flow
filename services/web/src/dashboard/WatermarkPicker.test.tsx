import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../api/client';
import type { CollectionWatermark } from '../api/types';
import { formatTime, regionBetween, WatermarkPicker } from './WatermarkPicker';

vi.mock('../api/client');

const COLLECTION = 'c-1';

async function flush() {
  await act(async () => {
    for (let i = 0; i < 4; i++) await Promise.resolve();
  });
}

const preview = {
  aweme_id: '7001',
  generation: 1,
  width: 720,
  height: 1280,
  source_region: null,
  sample_ms: 2000,
  source_url: '/api/dashboard/media/tok',
};

const item = (over: Partial<CollectionWatermark> = {}): CollectionWatermark => ({
  id: 'w-1',
  image_url: `/api/collections/${COLLECTION}/watermarks/w-1/image`,
  image_sha256: 'a'.repeat(64),
  frame_width: 720,
  frame_height: 1280,
  aweme_id: '7001',
  time_ms: 1500,
  x: 10,
  y: 20,
  w: 200,
  h: 48,
  created_at: '2026-11-15T00:00:00Z',
  ...over,
});

const drawImage = vi.fn();
const png = new Blob(['png'], { type: 'image/png' });

beforeEach(() => {
  vi.mocked(client.getCoverPreview).mockResolvedValue(preview);
  vi.mocked(client.listWatermarks).mockResolvedValue({ items: [] });
  vi.mocked(client.getWatermarkImage).mockResolvedValue(png);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => ({ drawImage }) as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((cb: (b: Blob | null) => void) => cb(png));
  URL.createObjectURL = vi.fn(() => 'blob:wm');
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.resetAllMocks();
  vi.restoreAllMocks();
  drawImage.mockReset();
});

/** Loads a 720×1280, 10 s video shown at 360×640, as the browser would. */
async function loadVideo() {
  await flush();
  const video = screen.getByLabelText('Video để cắt ảnh watermark') as HTMLVideoElement;
  Object.defineProperty(video, 'videoWidth', { value: 720 });
  Object.defineProperty(video, 'videoHeight', { value: 1280 });
  Object.defineProperty(video, 'duration', { value: 10 });
  Object.defineProperty(video, 'currentTime', { value: 0, writable: true });
  fireEvent.loadedMetadata(video);
  const surface = screen.getByTestId('watermark-picker-surface');
  surface.getBoundingClientRect = () => ({ left: 0, top: 0, width: 360, height: 640, right: 360, bottom: 640, x: 0, y: 0, toJSON: () => ({}) });
  return { video, surface };
}

function drag(surface: HTMLElement, from: [number, number], to: [number, number]) {
  fireEvent.mouseDown(surface, { button: 0, clientX: from[0], clientY: from[1] });
  fireEvent.mouseMove(window, { clientX: to[0], clientY: to[1] });
  fireEvent.mouseUp(window, { clientX: to[0], clientY: to[1] });
}

describe('regionBetween', () => {
  it('orders the corners, rounds to pixels and keeps the box inside the frame', () => {
    expect(regionBetween({ x: 110.4, y: 60 }, { x: 10, y: 20.6 }, 720, 1280)).toEqual({ x: 10, y: 21, w: 100, h: 39 });
    expect(regionBetween({ x: -5, y: 1200 }, { x: 800, y: 1400 }, 720, 1280)).toEqual({ x: 0, y: 1200, w: 720, h: 80 });
  });
});

describe('formatTime', () => {
  it('shows minutes and tenths of a second', () => {
    expect(formatTime(0)).toBe('0:00.0');
    expect(formatTime(65_430)).toBe('1:05.4');
  });
});

describe('WatermarkPicker', () => {
  it('cuts the dragged region from the paused frame at the video resolution and uploads it', async () => {
    vi.mocked(client.addWatermark).mockResolvedValue(item());
    render(<WatermarkPicker collectionId={COLLECTION} />);
    const { video, surface } = await loadVideo();
    expect(video.currentTime).toBe(2);
    video.currentTime = 3.25;
    // Displayed at half size, so each screen pixel is two video pixels.
    drag(surface, [10, 20], [110, 60]);
    expect(screen.getByText(/vùng 200×80 tại \(20, 40\)/)).toBeInTheDocument();

    vi.mocked(client.listWatermarks).mockResolvedValue({ items: [item()] });
    await userEvent.click(screen.getByRole('button', { name: 'Lưu ảnh mẫu' }));
    await flush();

    expect(drawImage).toHaveBeenCalledWith(video, 20, 40, 200, 80, 0, 0, 200, 80);
    expect(client.addWatermark).toHaveBeenCalledWith(
      COLLECTION,
      { aweme_id: '7001', time_ms: 3250, frame_width: 720, frame_height: 1280, x: 20, y: 40, w: 200, h: 80 },
      png,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Đã lưu ảnh mẫu.');
    expect(screen.getAllByTestId('watermark-item')).toHaveLength(1);
    expect(screen.queryByTestId('watermark-picker-region')).not.toBeInTheDocument();
  });

  it('will not save a region under 8 px or with no region at all', async () => {
    render(<WatermarkPicker collectionId={COLLECTION} />);
    const { surface } = await loadVideo();
    const save = screen.getByRole('button', { name: 'Lưu ảnh mẫu' });
    expect(save).toBeDisabled();
    drag(surface, [10, 20], [13, 60]);
    expect(save).toBeDisabled();
    expect(screen.getByText('Vùng phải rộng và cao ít nhất 8 px.')).toBeInTheDocument();
  });

  it('stops offering a cut once the collection holds 10 images', async () => {
    vi.mocked(client.listWatermarks).mockResolvedValue({
      items: Array.from({ length: 10 }, (_, i) => item({ id: `w-${i}`, image_url: `/img/${i}` })),
    });
    render(<WatermarkPicker collectionId={COLLECTION} />);
    const { surface } = await loadVideo();
    drag(surface, [10, 20], [110, 60]);
    expect(screen.getByRole('button', { name: 'Lưu ảnh mẫu' })).toBeDisabled();
    expect(screen.getByText('Đã đủ 10 ảnh, xoá bớt rồi thêm.')).toBeInTheDocument();
    expect(screen.getByText('Ảnh mẫu (10/10)')).toBeInTheDocument();
  });

  it('shows why the server refused an image', async () => {
    vi.mocked(client.addWatermark).mockRejectedValue(new Error('Collection đã có 10 ảnh watermark'));
    render(<WatermarkPicker collectionId={COLLECTION} />);
    const { surface } = await loadVideo();
    drag(surface, [10, 20], [110, 60]);
    await userEvent.click(screen.getByRole('button', { name: 'Lưu ảnh mẫu' }));
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent('Không lưu được ảnh mẫu: Collection đã có 10 ảnh watermark');
  });

  it('lists the images from blob URLs and deletes one after confirmation', async () => {
    vi.mocked(client.listWatermarks).mockResolvedValue({ items: [item()] });
    vi.mocked(client.deleteWatermark).mockResolvedValue();
    render(<WatermarkPicker collectionId={COLLECTION} />);
    await flush();
    expect(client.getWatermarkImage).toHaveBeenCalledWith(item().image_url);
    expect(screen.getByRole('img', { name: 'Watermark 200×48 cắt từ video 7001' })).toHaveAttribute('src', 'blob:wm');
    expect(screen.getByText('Ảnh mẫu (1/10)')).toBeInTheDocument();

    vi.mocked(client.listWatermarks).mockResolvedValue({ items: [] });
    await userEvent.click(screen.getByRole('button', { name: 'Xoá ảnh watermark 200×48' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Xác nhận' }));
    await flush();
    expect(client.deleteWatermark).toHaveBeenCalledWith(COLLECTION, 'w-1');
    expect(screen.queryAllByTestId('watermark-item')).toHaveLength(0);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:wm');
  });

  it('keeps an image when the admin cancels', async () => {
    vi.mocked(client.listWatermarks).mockResolvedValue({ items: [item()] });
    render(<WatermarkPicker collectionId={COLLECTION} />);
    await flush();
    await userEvent.click(screen.getByRole('button', { name: 'Xoá ảnh watermark 200×48' }));
    await userEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Hủy' }));
    await flush();
    expect(client.deleteWatermark).not.toHaveBeenCalled();
  });

  it('says why no image can be cut when the collection has no review project, and still lists images', async () => {
    vi.mocked(client.getCoverPreview).mockRejectedValue(Object.assign(new Error('none'), { status: 404 }));
    vi.mocked(client.listWatermarks).mockResolvedValue({ items: [item()] });
    render(<WatermarkPicker collectionId={COLLECTION} />);
    await flush();
    expect(screen.getByTestId('watermark-picker-missing')).toBeInTheDocument();
    expect(screen.queryByTestId('watermark-picker-surface')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('watermark-item')).toHaveLength(1);
  });

  describe('uploading a file', () => {
    const bitmap = (width: number, height: number) => ({ width, height, close: vi.fn() });
    const file = new File(['x'], 'logo.jpg', { type: 'image/jpeg' });

    it('sends the file as PNG at its own size, against the video frame, with no cut position', async () => {
      globalThis.createImageBitmap = vi.fn().mockResolvedValue(bitmap(200, 48));
      vi.mocked(client.addWatermark).mockResolvedValue(item({ aweme_id: null, time_ms: null, x: null, y: null }));
      render(<WatermarkPicker collectionId={COLLECTION} />);
      await loadVideo();
      expect(screen.getByLabelText('Khung gốc rộng')).toHaveValue('720');
      expect(screen.getByLabelText('Khung gốc cao')).toHaveValue('1280');

      await userEvent.upload(screen.getByLabelText('File ảnh'), file);
      await flush();
      expect(screen.getByText('logo.jpg · 200×48')).toBeInTheDocument();
      vi.mocked(client.listWatermarks).mockResolvedValue({ items: [item({ aweme_id: null, time_ms: null, x: null, y: null })] });
      await userEvent.click(screen.getByRole('button', { name: 'Tải ảnh lên' }));
      await flush();

      expect(globalThis.createImageBitmap).toHaveBeenCalledWith(file);
      expect(client.addWatermark).toHaveBeenCalledWith(COLLECTION, { frame_width: 720, frame_height: 1280, w: 200, h: 48 }, png);
      expect(screen.getByRole('status')).toHaveTextContent('Đã tải ảnh mẫu lên.');
      const listed = screen.getByTestId('watermark-item');
      expect(listed).toHaveTextContent('200×48 · tải lên');
      expect(within(listed).getByRole('img', { name: 'Watermark 200×48 tải lên từ file' })).toBeInTheDocument();
    });

    it('keeps a frame size the admin typed, and refuses an image larger than it', async () => {
      globalThis.createImageBitmap = vi.fn().mockResolvedValue(bitmap(200, 48));
      render(<WatermarkPicker collectionId={COLLECTION} />);
      await flush();
      await userEvent.clear(screen.getByLabelText('Khung gốc rộng'));
      await userEvent.type(screen.getByLabelText('Khung gốc rộng'), '100');
      await loadVideo();
      expect(screen.getByLabelText('Khung gốc rộng')).toHaveValue('100');
      await userEvent.upload(screen.getByLabelText('File ảnh'), file);
      await flush();
      expect(screen.getByText('Ảnh lớn hơn khung gốc.')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Tải ảnh lên' })).toBeDisabled();
    });

    it('says when the file is not an image it can read', async () => {
      globalThis.createImageBitmap = vi.fn().mockRejectedValue(new Error('decode'));
      render(<WatermarkPicker collectionId={COLLECTION} />);
      await flush();
      await userEvent.upload(screen.getByLabelText('File ảnh'), file);
      await flush();
      expect(screen.getByRole('alert')).toHaveTextContent('Không đọc được file ảnh.');
      expect(client.addWatermark).not.toHaveBeenCalled();
    });
  });
});
