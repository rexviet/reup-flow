import { useCallback, useState } from 'react';
import { listReviews } from '../api/client';
import { usePolling } from '../hooks/usePolling';

const PAGE_SIZE = 20;

export function ReviewProjectsPage() {
  const [offset, setOffset] = useState(0);
  // Remount on page change so a late response cannot replace the selected page.
  return <ReviewPage key={offset} offset={offset} onPage={setOffset} />;
}

function ReviewPage({ offset, onPage }: { offset: number; onPage: (offset: number) => void }) {
  const fetcher = useCallback(() => listReviews(PAGE_SIZE, offset), [offset]);
  const { data, error, loading } = usePolling(fetcher, 5000, { pauseWhenHidden: true });
  return (
    <section aria-labelledby="review-title" className="space-y-5">
      <header>
        <h1 id="review-title" className="text-xl font-semibold text-gray-900">
          Project cần review
        </h1>
        <p className="mt-2 text-sm text-gray-600">
          Mở trên cùng máy Mac đang chạy dự án. Lưu phụ đề và tạo lại giọng trong companion;
          chỉnh style trong Studio.
        </p>
      </header>
      <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        Nếu bridge chưa chạy hoặc Studio chưa đạt kiểm tra tương thích, launcher sẽ báo lỗi.
        Chạy lệnh bên dưới từ thư mục dự án; launcher cấp phiên và mở companion cùng Studio.
      </p>
      {loading && <p role="status">Đang tải project…</p>}
      {error && (
        <p role="alert" className="text-sm text-red-700">
          Không tải được danh sách review. Kiểm tra API và migration.
          {data ? ' Đang hiện dữ liệu cũ.' : ''}
        </p>
      )}
      {data && data.items.length === 0 && (
        <p className="text-sm text-gray-600">Chưa có project ở trang này.</p>
      )}
      <ul className="space-y-3">
        {data?.items.map((project) => (
          <li
            key={`${project.awemeId}:${project.generation}`}
            className="rounded-lg border border-gray-200 bg-white p-4"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-medium text-gray-900">
                {project.awemeId} · lần {project.generation}
              </h2>
              <span className="text-sm text-gray-700">
                {project.status === 'ready_for_review' ? 'Sẵn sàng review' : 'Cần người sửa'}
              </span>
            </div>
            <p className="mt-2 text-sm text-gray-600">Chưa xuất bản. Không có MP4 cuối.</p>
            <pre className="mt-3 overflow-x-auto rounded bg-gray-100 p-3 text-xs">
              <code>{`node scripts/review-studio.mjs open ${project.awemeId} ${project.generation}`}</code>
            </pre>
          </li>
        ))}
      </ul>
      <nav aria-label="Trang review" className="flex items-center gap-3 text-sm">
        <button
          type="button"
          className="min-h-11 rounded border px-3 disabled:opacity-40"
          disabled={offset === 0}
          onClick={() => onPage(Math.max(0, offset - PAGE_SIZE))}
        >
          Trang trước
        </button>
        <span>
          {data
            ? `${offset + (data.items.length ? 1 : 0)}–${offset + data.items.length} / ${data.total}`
            : '…'}
        </span>
        <button
          type="button"
          className="min-h-11 rounded border px-3 disabled:opacity-40"
          disabled={!data || offset + PAGE_SIZE >= data.total || !!error}
          onClick={() => onPage(offset + PAGE_SIZE)}
        >
          Trang sau
        </button>
      </nav>
    </section>
  );
}
