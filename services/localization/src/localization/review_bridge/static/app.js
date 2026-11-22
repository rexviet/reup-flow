'use strict';
let csrf = '',
  project,
  busy = false;
const edits = new Map();
const notice = document.querySelector('#notice');
const cues = document.querySelector('#cues');
const preview = document.querySelector('#preview');
const batchButton = document.querySelector('#batch');
const batchHint = document.querySelector('#batch-hint');
const jobs = new Map();
const message = (text) => {
  notice.textContent = text;
};
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Review-CSRF': csrf,
      ...options.headers,
    },
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(
      response.status === 409
        ? 'Có thay đổi mới. Bản nhập của bạn vẫn được giữ. Tải bản mới rồi kiểm tra trước khi lưu.'
        : result.error || 'Không kết nối được. Thử lại.',
    );
    error.status = response.status;
    throw error;
  }
  return result;
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function field(parent, labelText, value, type) {
  const label = element('label', labelText);
  const input = document.createElement(
    type === 'textarea' ? 'textarea' : 'input',
  );
  if (type !== 'textarea') {
    input.type = type;
    input.min = '0';
    input.step = '1';
  }
  input.value = value;
  input.required = type !== 'textarea';
  label.append(input);
  parent.append(label);
  return input;
}
// The lines the reviewer changed: an edit put back to what is saved is not one.
function changedEdits() {
  if (!project) return [];
  const changed = [];
  for (const [id, values] of edits) {
    const cue = project.cues.find((c) => c.id === id);
    if (
      cue &&
      (values.text !== cue.text_vi ||
        values.start_ms !== cue.start_ms ||
        values.end_ms !== cue.end_ms)
    )
      changed.push({ cueId: id, ...values });
  }
  return changed;
}
// A subtitles-only project has no voice: its lines are saved, never revoiced.
function subtitlesOnly() {
  return Boolean(project) && project.localizeMode === 'subtitles_only';
}
function updateBatch() {
  const count = changedEdits().length;
  const pending = project && localStorage.getItem(jobKey());
  const verb = subtitlesOnly() ? 'Lưu phụ đề' : 'Lưu & tạo lại giọng';
  batchButton.textContent = count ? `${verb} (${count} câu đã sửa)` : verb;
  batchButton.disabled = busy || !count || Boolean(pending);
  batchHint.textContent = pending
    ? 'Đang có yêu cầu tạo giọng. Chờ kết quả rồi bấm tiếp.'
    : count
      ? subtitlesOnly()
        ? 'Chỉ những câu đã sửa được lưu.'
        : 'Chỉ những câu đã sửa được lưu và tạo lại giọng.'
      : 'Sửa phụ đề bên phải, rồi bấm một lần cho tất cả.';
  for (const article of cues.querySelectorAll('.cue'))
    article.classList.toggle(
      'edited',
      changedEdits().some((e) => `cue-${e.cueId}` === article.dataset.cue),
    );
}
function jobKey() {
  return `review-job:${project.aweme_id}:g${project.generation}`;
}
function render() {
  if (!project) return;
  document.querySelector('#identity').textContent =
    `${project.aweme_id} · Lượt ${project.generation}`;
  cues.replaceChildren();
  if (!project.cues.length)
    cues.append(element('p', 'Không có câu phụ đề trong project này.'));
  for (const cue of project.cues) {
    const article = element('article', undefined, 'cue');
    article.dataset.cue = `cue-${cue.id}`;
    const title = element('h3', `Câu #${cue.id} `);
    title.append(
      subtitlesOnly()
        ? element('span', '· Chỉ phụ đề, giữ tiếng gốc', 'state')
        : element(
            'span',
            cue.mismatch ? '· Giọng chưa đồng bộ' : '· Đã đồng bộ',
            cue.mismatch ? 'state mismatch' : 'state',
          ),
    );
    article.append(title);
    const values = edits.get(cue.id) || {
      text: cue.text_vi,
      start_ms: cue.start_ms,
      end_ms: cue.end_ms,
    };
    const text = field(article, 'Nội dung phụ đề', values.text, 'textarea');
    text.maxLength = 4000;
    const timing = element('div', undefined, 'timing');
    article.append(timing);
    const start = field(timing, 'Bắt đầu (ms)', values.start_ms, 'number');
    const end = field(timing, 'Kết thúc (ms)', values.end_ms, 'number');
    for (const input of [text, start, end])
      input.addEventListener('input', () => {
        edits.set(cue.id, {
          text: text.value,
          start_ms: Number(start.value),
          end_ms: Number(end.value),
        });
        save.textContent = 'Lưu phụ đề *';
        updateBatch();
      });
    if (!subtitlesOnly())
      article.append(
        element(
          'p',
          `Giọng đang phát: ${cue.spoken_text || '(trống)'}`,
          'spoken',
        ),
      );
    if (cue.reason) article.append(element('p', cue.reason, 'reason'));
    const actions = element('div', undefined, 'actions');
    const save = element('button', 'Lưu phụ đề', 'primary');
    // A cue saved blank that still plays a clip offers to drop that voice from the mix.
    const removing = !cue.text_vi.trim();
    const voice = element('button', removing ? 'Xoá giọng' : 'Tạo lại giọng');
    save.disabled = busy;
    voice.disabled = busy || (removing && !cue.spoken_text);
    save.addEventListener('click', () =>
      action(async () => {
        if (!start.reportValidity() || !end.reportValidity()) return;
        const command = {
          expectedRevision: project.revision,
          headETag: project.headETag,
          text: text.value,
          start_ms: Number(start.value),
          end_ms: Number(end.value),
        };
        project = await api(`/api/cues/${cue.id}`, {
          method: 'PUT',
          body: JSON.stringify(command),
        });
        edits.delete(cue.id);
        preview.src = `/preview?revision=${project.revision}`;
        message(
          subtitlesOnly()
            ? 'Đã lưu phụ đề.'
            : project.cues.find((c) => c.id === cue.id)?.text_vi.trim()
            ? 'Đã lưu phụ đề. Giọng cũ được giữ cho tới khi tạo lại giọng.'
            : 'Đã lưu phụ đề trống. Giọng cũ vẫn phát cho tới khi bấm "Xoá giọng".',
        );
      }),
    );
    voice.addEventListener('click', () =>
      action(async () => {
        if (edits.has(cue.id))
          throw new Error(
            removing
              ? 'Lưu phụ đề trống của câu này trước khi xoá giọng.'
              : 'Lưu phụ đề của câu này trước khi tạo lại giọng.',
          );
        if (localStorage.getItem(jobKey()))
          throw new Error(
            'Đang có yêu cầu tạo giọng. Chờ kết quả hoặc tải bản mới để tiếp tục theo dõi.',
          );
        const command = {
          requestId: crypto.randomUUID(),
          cueId: cue.id,
          baseRevision: project.revision,
        };
        // Retain the exact command before dispatch, including when the response is lost.
        localStorage.setItem(jobKey(), JSON.stringify(command));
        await submit(command);
      }),
    );
    if (subtitlesOnly()) actions.append(save);
    else actions.append(save, voice);
    article.append(actions);
    cues.append(article);
  }
  updateBatch();
}
async function action(callback) {
  if (busy) return;
  busy = true;
  for (const button of cues.querySelectorAll('button')) button.disabled = true;
  batchButton.disabled = true;
  try {
    await callback();
  } catch (error) {
    message(error.message);
  } finally {
    busy = false;
    render();
  }
}
async function load() {
  project = await api('/api/project');
  render();
  preview.src = `/preview?revision=${project.revision}`;
  const pending = localStorage.getItem(jobKey());
  if (!pending) return;
  const job = JSON.parse(pending);
  // A batch is saved before it is sent: only its result is left to follow.
  if (job.batch) watch(job.requestId, job.label);
  else await submit(job);
}
async function submit(command) {
  try {
    await api('/api/revoice', {
      method: 'POST',
      body: JSON.stringify(command),
    });
  } catch (error) {
    if ([409, 422].includes(error.status)) localStorage.removeItem(jobKey());
    throw error;
  }
  watch(command.requestId, `câu #${command.cueId}`);
}
function watch(requestId, label) {
  message(`Đang tạo giọng ${label}. Bạn có thể tiếp tục sửa phụ đề.`);
  updateBatch();
  if (jobs.has(requestId)) return;
  async function poll() {
    try {
      const result = await api(`/api/jobs/${requestId}`);
      if (['running', 'accepted', 'reserved'].includes(result.status)) {
        jobs.set(requestId, setTimeout(poll, 1500));
        return;
      }
      jobs.delete(requestId);
      localStorage.removeItem(jobKey());
      const long = (result.overflowCues || []).map((id) => `#${id}`).join(', ');
      const kept = long
        ? ` Câu ${long}: giọng mới dài quá 1,15× nên vẫn giữ giọng cũ.`
        : '';
      if (result.status === 'completed') {
        await load();
        try {
          await api('/api/refresh', { method: 'POST', body: '{}' });
        } catch (error) {
          message(`Đã tạo giọng. Studio cần đồng bộ: ${error.message}`);
          return;
        }
        message(
          `Đã cập nhật giọng ${label} trong bản mix (câu trống thì đã bỏ giọng).${kept} Tải lại preview trong Studio để nghe.`,
        );
      } else
        message(
          result.status === 'conflict'
            ? 'Có bản chỉnh mới hơn. Giọng vừa tạo chưa được áp dụng. Tải bản mới và tạo lại nếu cần.'
            : long
              ? `Giọng mới dài quá 1,15×, chưa áp dụng.${kept} Phụ đề mới vẫn được lưu.`
              : 'Giọng mới chưa dùng được. Phụ đề mới và giọng cũ vẫn được giữ.',
        );
    } catch (error) {
      jobs.delete(requestId);
      // The server never received it: nothing to follow, nothing was saved.
      if (error.status === 422) {
        localStorage.removeItem(jobKey());
        message('Yêu cầu chưa tới máy chủ nên chưa có gì được lưu. Sửa lại rồi bấm gửi.');
        return;
      }
      message(
        `${error.message} Tải bản mới để tiếp tục theo dõi cùng yêu cầu.`,
      );
    } finally {
      updateBatch();
    }
  }
  jobs.set(requestId, setTimeout(poll, 1000));
}
batchButton.addEventListener('click', () =>
  action(async () => {
    if (localStorage.getItem(jobKey()))
      throw new Error(
        'Đang có yêu cầu tạo giọng. Chờ kết quả rồi bấm tiếp.',
      );
    const changed = changedEdits();
    if (!changed.length) return;
    const wrong = changed.find(
      (e) => !(e.start_ms >= 0 && e.end_ms > e.start_ms),
    );
    if (wrong)
      throw new Error(
        `Câu #${wrong.cueId}: thời gian bắt đầu phải nhỏ hơn kết thúc.`,
      );
    const requestId = crypto.randomUUID();
    const label = `${changed.length} câu đã sửa`;
    // Kept before dispatch, so a lost response is still followed after a reload.
    localStorage.setItem(
      jobKey(),
      JSON.stringify({ batch: true, requestId, label }),
    );
    let result;
    try {
      result = await api('/api/revoice-batch', {
        method: 'POST',
        body: JSON.stringify({
          requestId,
          expectedRevision: project.revision,
          headETag: project.headETag,
          edits: changed,
        }),
      });
    } catch (error) {
      if ([409, 413, 422].includes(error.status))
        localStorage.removeItem(jobKey());
      throw error;
    }
    for (const edit of changed) edits.delete(edit.cueId);
    project = result.project;
    preview.src = `/preview?revision=${project.revision}`;
    if (result.status !== 'accepted') {
      localStorage.removeItem(jobKey());
      message(
        `Đã lưu ${changed.length} câu. Không câu nào cần tạo lại giọng.`,
      );
      return;
    }
    watch(requestId, `${result.cueIds.length} câu đã sửa`);
  }),
);
document.querySelector('#reload').addEventListener('click', () => action(load));
window.addEventListener('beforeunload', (event) => {
  if (edits.size) {
    event.preventDefault();
    event.returnValue = '';
  }
});
(async () => {
  const grant = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  try {
    ({ csrf } = grant
      ? await api('/api/redeem', {
          method: 'POST',
          body: JSON.stringify({ grant }),
        })
      : await api('/api/session'));
    await load();
    if (!localStorage.getItem(jobKey()))
      message('Sẵn sàng duyệt. Mọi lần tạo giọng đều cần thao tác của bạn.');
  } catch (error) {
    message(error.message);
  }
})();
