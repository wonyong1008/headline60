(() => {
  'use strict';
  const video = document.getElementById('caption-video');
  const form = document.getElementById('caption-form');
  const rows = Array.from(document.querySelectorAll('.caption-row'));
  const notice = document.getElementById('notice');
  const saveButton = document.getElementById('save-captions');
  const reviewLink = document.getElementById('review-new-version');
  const token = document.querySelector('meta[name="headline-token"]').content;
  const jobId = document.body.dataset.jobId;
  let stopAt = null;

  function message(text, error = false) {
    notice.textContent = text;
    notice.className = error ? 'error' : '';
  }

  function readCue(row) {
    return {
      scene: Number(row.dataset.scene),
      text: row.querySelector('.cue-text').value.trim(),
      start: Number(row.querySelector('.cue-start').value),
      end: Number(row.querySelector('.cue-end').value),
      highlight: row.querySelector('.cue-highlight').value.split(',').map(value => value.trim()).filter(Boolean),
    };
  }

  function sceneLimits() {
    const starts = [...new Set(rows.map(row => Number(row.dataset.sceneStart)))].sort((a, b) => a - b);
    return new Map(starts.map((start, index) => [start, starts[index + 1] ?? video.duration]));
  }

  function validate() {
    const cues = rows.map(readCue);
    const limits = sceneLimits();
    let previousEnd = 0;
    for (const [index, cue] of cues.entries()) {
      const label = `구절 ${index + 1}`;
      if (!cue.text || cue.text.length > 80) throw new Error(`${label}: 자막은 1~80자로 입력하세요.`);
      if (cue.text.split('\n').length > 2) throw new Error(`${label}: 줄바꿈은 최대 두 줄로 입력하세요.`);
      if (!Number.isFinite(cue.start) || !Number.isFinite(cue.end) || cue.start < 0 || cue.end <= cue.start) {
        throw new Error(`${label}: 종료 시간은 시작 시간보다 커야 합니다.`);
      }
      if (cue.start < previousEnd - 0.001) throw new Error(`${label}: 이전 구절과 시간이 겹칩니다.`);
      const sceneStart = Number(rows[index].dataset.sceneStart);
      const sceneEnd = limits.get(sceneStart);
      if (cue.start < sceneStart - 0.001 || (Number.isFinite(sceneEnd) && cue.end > sceneEnd + 0.001)) {
        throw new Error(`${label}: 같은 장면의 시간 범위 안에서 조정하세요.`);
      }
      if (cue.highlight.length > 6 || cue.highlight.some(word => word.length > 20)) {
        throw new Error(`${label}: 강조 단어는 각 20자 이내, 최대 6개까지 입력하세요.`);
      }
      previousEnd = cue.end;
    }
    return cues;
  }

  function updateTextHints() {
    for (const row of rows) {
      const text = row.querySelector('.cue-text').value;
      const hint = row.querySelector('.cue-length');
      hint.textContent = `${text.length}/80자 · ${text.split('\n').length}줄${text.length > 34 ? ' · 읽기 편하도록 더 짧게 나누는 것을 권장합니다.' : ''}`;
    }
  }

  function updatePreview() {
    const cue = rows.map(readCue).find(item => item.start <= video.currentTime && item.end > video.currentTime);
    document.getElementById('caption-live-preview').textContent = cue?.text || '이 시점에 표시할 구절이 없습니다.';
    if (stopAt !== null && video.currentTime >= stopAt) {
      video.pause();
      stopAt = null;
    }
  }

  video.addEventListener('loadedmetadata', () => {
    document.getElementById('video-status').textContent = `${video.videoWidth}×${video.videoHeight} · ${video.duration.toFixed(2)}초`;
  });
  video.addEventListener('error', () => {
    document.getElementById('video-status').textContent = '영상 재생 정보를 불러오지 못했습니다. 제작 내역에서 파일 상태를 확인하세요.';
  });
  video.addEventListener('timeupdate', updatePreview);
  video.addEventListener('ended', () => { stopAt = null; });

  form.addEventListener('input', () => {
    updateTextHints();
    reviewLink.hidden = true;
    message('저장하지 않은 자막 변경이 있습니다.');
    updatePreview();
  });

  form.addEventListener('click', async event => {
    const button = event.target.closest('.cue-play');
    if (!button) return;
    try {
      const row = button.closest('.caption-row');
      const cue = readCue(row);
      if (!Number.isFinite(cue.start) || cue.start < 0 || cue.end <= cue.start) throw new Error('먼저 유효한 시작·종료 시간을 입력하세요.');
      if (video.readyState < 1) throw new Error('영상 정보가 준비된 뒤 다시 재생하세요.');
      if (cue.end > video.duration) throw new Error('구절 종료 시간이 영상 길이를 넘습니다.');
      video.currentTime = cue.start;
      stopAt = cue.end;
      await video.play();
      updatePreview();
    } catch (error) {
      stopAt = null;
      message(error.message, true);
    }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    saveButton.disabled = true;
    reviewLink.hidden = true;
    try {
      const cues = validate();
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/captions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Headline-Token': token },
        body: JSON.stringify({ cues }),
      });
      const data = await response.json();
      if (!response.ok) {
        const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg).join('\n') : data.detail;
        throw new Error(detail || `저장 실패: HTTP ${response.status}`);
      }
      const url = new URL(data.url || `/studio?draft=${encodeURIComponent(data.draft_id)}`, location.origin);
      if (url.origin !== location.origin || url.pathname !== '/studio') throw new Error('검토 화면 주소를 확인할 수 없습니다. 제작 화면에서 새 버전을 확인하세요.');
      reviewLink.href = url.pathname + url.search;
      reviewLink.hidden = false;
      message(`자막 버전 ${data.revision}을 저장했습니다. 새 버전에서 대본·자막을 검토 승인한 뒤 영상을 다시 생성하세요. 기존 완료 영상은 보존됩니다.`);
    } catch (error) {
      message(error.message, true);
    } finally {
      saveButton.disabled = rows.length === 0;
    }
  });

  updateTextHints();
})();
