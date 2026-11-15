import { renderFailureOf } from '../../src/dashboard/render-failure';

describe('renderFailureOf', () => {
  it.each([
    ['ValidationError: 7 validation errors for ReviewProjectManifest caption_color Extra inputs are not permitted [type=extra_forbidden]', 'outdated'],
    ['Bản đã đổi sau khi duyệt, duyệt lại bản mới.', 'stale'],
    ['Quyết định duyệt đã đổi trong lúc render, kết quả bị bỏ.', 'stale'],
    ['RenderRejected: HEAD differs', 'stale'],
    ['TimeoutError: Render quá 60 phút, đã dừng.', 'timeout'],
    ['Render quá thời gian chờ sau 3 lần thử.', 'timeout'],
    ['RuntimeError: hyperframes render thoát với mã 1: chrome crashed', 'unknown'],
    ['', 'unknown'],
    [null, 'unknown'],
  ])('reads %j as %s', (error, kind) => {
    expect(renderFailureOf(error).kind).toBe(kind);
  });

  it('says what to do in plain Vietnamese', () => {
    expect(renderFailureOf('extra_forbidden')).toEqual({
      kind: 'outdated',
      reason: 'Máy render đang chạy bản cũ, không đọc được bản review mới.',
      action: 'Cập nhật máy render rồi bấm Render lại. Trên máy đó, bấm đúp file cap-nhat-may-render.bat trong thư mục reup-flow.',
    });
    expect(renderFailureOf(null).action).toBe('Bấm Render lại; nếu vẫn lỗi, xem chi tiết kỹ thuật.');
    expect(renderFailureOf('timed out').action).toBe('Bấm Render lại.');
  });
});
