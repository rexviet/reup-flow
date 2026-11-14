'use strict';
let csrf = '',
  project,
  busy = false;
const edits = new Map();
const notice = document.querySelector('#notice');
const cues = document.querySelector('#cues');
const preview = document.querySelector('#preview');
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
    const title = element('h3', `Câu #${cue.id} `);
    title.append(
      element(
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
      });
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
          project.cues.find((c) => c.id === cue.id)?.text_vi.trim()
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
    actions.append(save, voice);
    article.append(actions);
    cues.append(article);
  }
}
async function action(callback) {
  if (busy) return;
  busy = true;
  for (const button of cues.querySelectorAll('button')) button.disabled = true;
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
  if (pending) await submit(JSON.parse(pending));
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
  message(
    `Đang tạo giọng câu #${command.cueId}. Bạn có thể tiếp tục sửa phụ đề.`,
  );
  if (jobs.has(command.requestId)) return;
  async function poll() {
    try {
      const result = await api(`/api/jobs/${command.requestId}`);
      if (['running', 'accepted', 'reserved'].includes(result.status)) {
        jobs.set(command.requestId, setTimeout(poll, 1500));
        return;
      }
      jobs.delete(command.requestId);
      localStorage.removeItem(jobKey());
      if (result.status === 'completed') {
        await load();
        try {
          await api('/api/refresh', { method: 'POST', body: '{}' });
        } catch (error) {
          message(`Đã tạo giọng. Studio cần đồng bộ: ${error.message}`);
          return;
        }
        message(
          'Đã cập nhật giọng trong bản mix (câu trống thì đã bỏ giọng). Tải lại preview trong Studio để nghe.',
        );
      } else
        message(
          result.status === 'conflict'
            ? 'Có bản chỉnh mới hơn. Giọng vừa tạo chưa được áp dụng. Tải bản mới và tạo lại nếu cần.'
            : 'Giọng mới chưa dùng được hoặc dài quá 1,15×. Phụ đề mới và giọng cũ vẫn được giữ.',
        );
    } catch (error) {
      message(
        `${error.message} Tải bản mới để tiếp tục theo dõi cùng yêu cầu.`,
      );
      jobs.delete(command.requestId);
    }
  }
  jobs.set(command.requestId, setTimeout(poll, 1000));
}
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
