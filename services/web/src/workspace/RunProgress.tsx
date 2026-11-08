import type { RunDetail } from '../api/types';
import { deskLabel } from './workspace.layout';

type Step = RunDetail['steps'][number];
const LABELS: Record<string, string> = {
  check_voice: 'Kiểm tra giọng nói', ingest: 'Chuẩn bị video', voice_rate: 'Đo tốc độ đọc',
  transcribe: 'Nhận dạng lời nói', detect_subtitles: 'Tìm phụ đề gốc', classify_video: 'Kiểm tra quảng cáo',
  assemble_script: 'Lưu kịch bản dịch', voice_profile: 'Chọn cấu hình giọng', plan_synthesis: 'Chia lượt tạo giọng',
  synthesize_segments: 'Tạo giọng đọc', measure_fit: 'Kiểm tra câu có vừa thời lượng',
  write_dubbed_script: 'Lưu lời thoại đã chỉnh', commit_dub: 'Chốt các đoạn thoại',
  assemble_audio: 'Ghép âm thanh', layout_captions: 'Dàn phụ đề',
  review_before_render: 'Kiểm duyệt trước render · gồm JEV', review_render: 'Kiểm duyệt video',
  render: 'Render video', publish: 'Xuất bản',
};
export function activityLabel(activity: string): string {
  if (activity.startsWith('agent__localizer')) return 'Dịch kịch bản';
  if (activity.startsWith('agent__dubber')) return 'Chỉnh câu cho vừa thời lượng';
  return LABELS[activity] ?? activity;
}

/** Planned possibilities only; completed steps always come from Temporal history. */
function nextStep(step: Step | undefined): string {
  if (!step) return 'Chờ dữ liệu bước đầu tiên';
  if (step.state === 'failed') return 'Chờ workflow xử lý lỗi hoặc chuyển người xem';
  if (step.state === 'cancelled') return 'Bước đã dừng, chờ kết quả workflow';
  if (step.state === 'retrying') return 'Thử lại bước hiện tại';
  const name = step.activity;
  if (name === 'check_voice') return 'Chuẩn bị video; có thể kết thúc sớm nếu không có tiếng/hình';
  if (name === 'ingest' || name === 'voice_rate') return 'Nhận dạng lời nói + tìm phụ đề (có thể song song)';
  if (name === 'transcribe' || name === 'detect_subtitles') return 'Kiểm tra quảng cáo sau khi chuẩn bị xong';
  if (name === 'classify_video') return 'Localizer Agent, nếu video vượt qua kiểm tra quảng cáo';
  if (name.startsWith('agent__localizer') || name === 'assemble_script') return 'Tạo giọng đọc rồi kiểm tra thời lượng';
  if (name === 'measure_fit') return 'Dubber Agent nếu có câu dài; nếu vừa thì ghép thoại';
  if (name.startsWith('agent__dubber') || name === 'write_dubbed_script') return 'Tạo lại giọng cho các câu đã sửa';
  if (name === 'commit_dub' || name === 'assemble_audio') return 'Dàn phụ đề → Reviewer Agent';
  if (name === 'layout_captions') return 'Reviewer Agent';
  if (name === 'review_before_render') return 'Render nếu đạt; có thể quay lại sửa hoặc cần người xem';
  if (name === 'review_render') return 'Xuất bản nếu đạt; có thể quay lại sửa hoặc cần người xem';
  if (name === 'render') return 'Xuất bản hoặc chuyển người xem theo kết quả review';
  if (name === 'publish') return 'Chờ kết quả kết thúc workflow';
  return 'Workflow quyết định theo kết quả bước hiện tại';
}

const STATES = { done: 'Đã xong', running: 'Đang chạy', retrying: 'Đang thử lại', failed: 'Lỗi', cancelled: 'Đã dừng' };
const RESULTS: Record<string, string> = {
  published: 'Đã xuất bản', published_original: 'Đã đăng bản gốc',
  needs_human: 'Cần người xem', needs_review: 'Cần người xem', failed: 'Workflow đã lỗi',
  skipped_ad: 'Đã bỏ qua quảng cáo', skipped_no_video: 'Đã bỏ qua do không có video',
  cancelled: 'Workflow đã dừng',
};

export function RunProgress({ detail, stale = false }: { detail: RunDetail; stale?: boolean }) {
  const steps = detail.steps ?? [];
  const pending = steps.filter((s) => s.state === 'running' || s.state === 'retrying');
  const result = pending.length ? undefined : RESULTS[detail.outcome ?? detail.status ?? ''];
  const last = steps.at(-1);
  const completed = steps.filter((s) => s.state === 'done');
  const latestDone = [...completed].sort((a, b) => Date.parse(b.endedAt ?? b.startedAt) - Date.parse(a.endedAt ?? a.startedAt))[0];
  const unknown = stale || detail.sources.temporal === 'down';
  const current = pending.length ? pending : last?.state === 'failed' ? [last] : [];
  const visible = steps.slice(-5);
  const stepView = (step: Step, index: number) => <li key={step.id} className="flex items-start gap-3 py-2 text-sm">
    <span aria-hidden="true" className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${step.state === 'done' ? 'bg-green-100 text-green-800' : step.state === 'failed' ? 'bg-red-100 text-red-800' : step.state === 'retrying' ? 'bg-amber-100 text-amber-900' : 'bg-blue-100 text-blue-800'}`}>{step.state === 'done' ? '✓' : index + 1}</span>
    <div className="min-w-0 flex-1"><span className="font-medium text-gray-900">{activityLabel(step.activity)}</span><span className="block text-xs text-gray-600">{deskLabel(step.agentId)}</span></div>
    <span className="shrink-0 text-xs text-gray-700">{STATES[step.state]}{step.attempt && step.attempt > 1 ? ` · lần ${step.attempt}` : ''}</span>
  </li>;
  return <section aria-label="Hành trình video" className="flex flex-col gap-4">
    <div className="grid gap-3 md:grid-cols-3">
      <div className="rounded-lg border border-gray-200 bg-gray-50 p-4"><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-600">Vừa hoàn thành</p><p className="font-medium text-gray-900">{latestDone ? activityLabel(latestDone.activity) : 'Chưa có bước hoàn thành'}</p>{latestDone && <p className="mt-1 text-sm text-gray-600">{deskLabel(latestDone.agentId)}</p>}</div>
      <div className="rounded-lg border border-blue-300 bg-blue-50 p-4"><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-blue-800">{unknown ? 'Trạng thái chưa rõ' : current.length > 1 ? 'Đang chạy song song' : 'Đang ở bước'}</p><p className="font-semibold text-gray-900">{unknown ? 'Chờ kết nối dữ liệu mới' : result ?? (current.length ? current.map((s) => activityLabel(s.activity)).join(' + ') : 'Đang chờ workflow quyết định')}</p>{!unknown && current.map((s) => <p key={s.id} className="mt-1 text-sm text-blue-900">{deskLabel(s.agentId)} · {STATES[s.state]}{s.attempt && s.attempt > 1 ? ` lần ${s.attempt}` : ''}</p>)}</div>
      <div className="rounded-lg border border-dashed border-gray-300 p-4"><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-600">Tiếp theo · dự kiến</p><p className="text-sm text-gray-700">{unknown ? 'Chưa xác định' : result ? 'Không còn bước tiếp theo trong lượt chạy này.' : nextStep(current[0] ?? last)}</p></div>
    </div>
    <div><div className="flex flex-wrap items-baseline justify-between gap-2"><h3 className="text-sm font-semibold text-gray-900">Đường đi thực tế</h3><p className="text-xs text-gray-600">{completed.length}/{steps.length} bước đã xong · thứ tự được lên lịch</p></div>
      {steps.length ? <ol className="mt-2 divide-y divide-gray-100">{visible.map((s, i) => stepView(s, steps.length - visible.length + i))}</ol> : <p className="mt-2 text-sm text-gray-600">Chưa có lịch sử hoạt động.</p>}
      {steps.length > 5 && <details className="mt-2"><summary className="cursor-pointer py-3 text-sm font-medium text-blue-800 focus-visible:outline focus-visible:outline-2">Xem {steps.length - 5} bước trước</summary><ol className="divide-y divide-gray-100">{steps.slice(0, -5).map(stepView)}</ol></details>}
    </div>
  </section>;
}
