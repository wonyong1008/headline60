const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const page = document.body.dataset.page;
const token = document.querySelector('meta[name="headline-token"]').content;
const labels = {researching:'원문·근거 자동 확인 중',needs_evidence:'자료 확인 보류',generating:'대본 생성 중',review:'대본 검토 중',approved:'현재 버전 검토 승인',rendering:'영상 제작 중',completed:'렌더링 완료',failed:'실패',queued:'대기 중',running:'제작 중',cancelled:'취소됨',interrupted:'중단됨'};
const stageNames = {script:'대본',tts:'한국어 음성·자막',cards:'장면 카드',render:'영상 합성',verify:'규격·디코딩 검사'};
let currentDraft = null, dirty = false, newsGroups = [], selected = new Set(), lastJobs = '';
let mediaAssets = [], mediaBusy = false;
let voiceProfiles = [], sampleBusy = false, samplePollTimer = null;
function notice(text, error=false) { $('notice').textContent=text; $('notice').className=error?'error':''; }
async function api(path, method='GET', body) {
  const response = await fetch(path, {method, headers:{'X-Headline-Token':token,...(body?{'Content-Type':'application/json'}:{})}, ...(body?{body:JSON.stringify(body)}:{})});
  let data;
  try { data=await response.json(); } catch { throw new Error(`서버 응답을 확인하지 못했습니다 (HTTP ${response.status}). 잠시 후 다시 시도하세요.`); }
  if (!response.ok) throw new Error(Array.isArray(data.detail)?data.detail.map(x=>`${x.loc?.join('.')}: ${x.msg}`).join('\n'):(data.detail||`HTTP ${response.status}`));
  return data;
}
function on(id, fn) { const element=$(id); if(element) element.addEventListener('click', async e=>{e.preventDefault(); element.disabled=true; try {await fn();} catch(error){notice(error.message,true);} finally{element.disabled=false; updateButtons();if(id==='create-draft')element.disabled=!selected.size;}}); }
function formatDate(value) { return value?new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'}):'발행일 미확인'; }
function badge(status) { return `<span class="badge ${status==='completed'||status==='approved'?'ok':''}">${esc(labels[status]||status)}</span>`; }
const contentModeLabels={news:'실제 뉴스',myth_check:'속설 검증',adult_satire:'허구·성인 공감 풍자'};
function draftMode(draft) { return draft.content_mode||'news'; }
function draftTitle(draft) { return draft.script?.title||draft.topic||draft.articles?.[0]?.title||'제목 없는 콘텐츠'; }
function modeBadge(mode) { return `<span class="badge ${mode==='adult_satire'?'fiction-badge':''}">${esc(contentModeLabels[mode]||'실제 뉴스')}</span>`; }
function reviewRequirements(draft) {
  const flags=draft.review_requirements;
  return flags?{medical:Boolean(flags.medical_reviewed),sexual:Boolean(flags.sexual_reviewed)}:{medical:draftMode(draft)==='myth_check',sexual:false};
}
function reviewPayload() {
  const required=reviewRequirements(currentDraft),content=$('review-check').checked,medical=$('medical-review-check').checked,sexual=$('sexual-review-check').checked;
  if(!content)throw new Error('전체 대본과 근거 또는 허구·풍자 표시를 직접 검토한 뒤 확인란을 선택하세요.');
  if(required.medical&&!medical)throw new Error('필수 건강·의학 검토 확인란을 선택하세요. 근거가 없으면 자료를 보완하거나 주장을 삭제하세요.');
  if(required.sexual&&!sexual)throw new Error('필수 성인 관계·외모 표현 검토 확인란을 선택하세요.');
  return {revision:currentDraft.revision,content_reviewed:content,medical_reviewed:medical,sexual_reviewed:sexual};
}
function updateNewContentMode() {
  const mode=$('new-content-mode').value,isNews=mode==='news',fiction=mode==='adult_satire';
  $('new-content-topic').disabled=isNews;$('new-content-topic').required=!isNews;
  $('new-fiction-label').hidden=!fiction;$('new-fiction-acknowledged').required=fiction;
  $('create-content').textContent=isNews?'뉴스 탐색으로 이동':'이 주제로 제작 준비';
  $('new-content-guide').textContent=isNews?'실제 뉴스는 뉴스 탐색에서 기사를 선택하고 근거를 확인합니다. 주제만으로 실제 뉴스를 만들지 않습니다.':fiction?'허구·성인 공감 풍자입니다. 실제 사건·실제 인물의 뉴스로 표현하지 않습니다. 의료·성 소재가 있으면 해당 검토도 필요합니다.':'속설 검증 모드입니다. 건강·의약품 효과는 공식 근거로 확인하며 근거가 부족하면 단정하지 않습니다. 의료·성 소재가 있으면 해당 검토도 필요합니다.';
}
async function createContent(event) {
  event.preventDefault();const mode=$('new-content-mode').value;
  if(mode==='news'){location.href='/news';return;}
  const topic=$('new-content-topic').value.trim(),fiction=$('new-fiction-acknowledged').checked;
  if(topic.length<2)throw new Error('주제를 2자 이상 입력하세요.');
  if(mode==='adult_satire'&&!fiction)throw new Error('허구·성인 공감 풍자 표시 동의가 필요합니다.');
  const draft=await api('/api/drafts','POST',{article_ids:[],topic,content_mode:mode,fiction_acknowledged:mode==='adult_satire'&&fiction,category:$('new-content-category').value,seconds:30,resolution:720,template:'story_explainer',edit_style:'reels_v3',narration_speed:1.18,subtitle_style:'dynamic'});
  location.href='/studio?draft='+encodeURIComponent(draft.id);
}
function jobVoiceLabel(profileId) {
  const profile=voiceProfiles.find(p=>p.voice_profile_id===profileId);
  if(profile)return profile.name;
  return ({'macos-yuna-news':'macOS Yuna · 기본 뉴스','female-news':'성숙한 Sohee · 뉴스','calm-economy':'Sohee · 차분한 경제 뉴스','bright-lifestyle':'Sohee · 밝은 생활 뉴스','energetic-trending':'Sohee · 활기 있는 이슈 뉴스'})[profileId]||profileId||'기존 macOS 음성';
}
function jobHTML(job) {
  const d=job.details||{}, isNews=Boolean(d.draft_id), done=job.status==='completed';
  return `<article class="job" data-job="${esc(job.id)}">${badge(job.status)} ${d.content_mode?modeBadge(d.content_mode):''} <span class="badge">${d.review_status==='sample_pending_user_review'?'검증 샘플 · 게시 전 검토 필요':isNews?'사용자 검토 버전':'이전 설명형 테스트 · 대본 미검토'}</span>
  <h3>${esc(job.topic)}</h3><p class="muted">${esc(job.id)} ${d.revision?`· 버전 ${d.revision}`:''} · ${d.resolution||'?'}p · ${d.actual_seconds?`실제 ${d.actual_seconds}초 · 목표 ${d.seconds}초`:`목표 ${d.seconds||'?'}초`} · ${esc(stageNames[job.stage]||job.stage)}</p><p class="muted">목소리: ${esc(jobVoiceLabel(d.voice_profile_id))}</p>
  ${['running','queued'].includes(job.status)?`<progress max="5" value="${(d.completed_stages||[]).length}"></progress><p class="muted">완료 단계 ${(d.completed_stages||[]).length}/5 · 시간 추정치가 아닙니다.</p>`:''}
  ${job.error?`<p class="error">${esc(job.error)}</p>`:''}
  ${done?`<video aria-label="${esc(job.topic)} 미리보기" controls preload="metadata" poster="/jobs/${esc(job.id)}/poster.png" src="/jobs/${esc(job.id)}/output.mp4"></video><nav><a download href="/jobs/${esc(job.id)}/output.mp4">MP4 저장 ↓</a><a href="/jobs/${esc(job.id)}/package.zip">게시 자료 ZIP ↓</a><a href="/jobs/${esc(job.id)}/script">대본 보기</a><a href="/jobs/${esc(job.id)}/sources.json">출처</a><a href="/jobs/${esc(job.id)}/subtitles.srt">자막 저장</a>${d.has_phrase_captions?`<a href="/jobs/${esc(job.id)}/captions">자막 편집</a>`:''}</nav>`:''}
  <div class="actions">${['failed','cancelled','interrupted'].includes(job.status)?`<button class="secondary" data-resume="${esc(job.id)}">실패 단계부터 재개</button>`:''}${['queued','running'].includes(job.status)?`<button class="danger" data-cancel="${esc(job.id)}">제작 취소</button>`:''}${isNews?`<a href="/studio?draft=${esc(d.draft_id)}">대본 버전 확인 →</a>`:''}</div>
  ${d.timings?.run_total?`<p class="muted">이번 실행 ${d.timings.run_total}초 · 렌더링 ${d.timings.render??'재사용'}초</p>`:''}</article>`;
}
async function refreshJobs() {
  const jobs=await api('/api/jobs');
  const signature=JSON.stringify(jobs);
  if(signature===lastJobs) return;
  lastJobs=signature;
  if($('history-jobs')) {
    const openDrafts=new Set(Array.from(document.querySelectorAll('details[data-history-draft][open]')).map(element=>element.dataset.historyDraft));
    const groups=new Map();
    for(const job of jobs.filter(j=>j.details?.draft_id)){const key=job.details.draft_id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(job);}
    $('history-jobs').innerHTML=Array.from(groups,([key,versions])=>{
      versions.sort((a,b)=>(Date.parse(b.details.created_at||b.updated_at)||0)-(Date.parse(a.details.created_at||a.updated_at)||0)||String(b.id).localeCompare(String(a.id)));
      const [latest,...older]=versions;
      return `<section class="panel">${jobHTML(latest)}${older.length?`<details data-history-draft="${esc(key)}" ${openDrafts.has(key)?'open':''}><summary>이전 버전 ${older.length}개</summary>${older.map(jobHTML).join('')}</details>`:''}</section>`;
    }).join('')||'<section class="panel">뉴스 영상 제작 내역이 없습니다.</section>';
    $('legacy-jobs').innerHTML=jobs.filter(j=>!j.details?.draft_id).map(jobHTML).join('')||'<p>이전 테스트 없음</p>';
  }
  if($('studio-jobs')&&currentDraft) $('studio-jobs').innerHTML=jobs.filter(j=>j.details?.draft_id===currentDraft.id).map(jobHTML).join('')||'<p class="muted">대본 검토 승인 후 영상 생성을 시작하세요.</p>';
}
async function dashboard() {
  const [drafts,jobs]=await Promise.all([api('/api/drafts'),api('/api/jobs')]);
  const completed=jobs.filter(j=>j.details?.draft_id&&j.status==='completed');
  const completedIssues=new Set(completed.map(j=>j.details.draft_id)).size;
  $('dashboard').innerHTML=[['저장한 이슈',drafts.length,''],['완성 영상',completedIssues,`제작 ${completed.length}회 · 이전 버전 포함`],['진행 중',jobs.filter(j=>['queued','running'].includes(j.status)).length,'']].map(([label,n,note])=>`<div class="stat">${label}<strong>${n}</strong>${note?`<small class="muted">${note}</small>`:''}</div>`).join('');
  $('draft-list').innerHTML=drafts.map(d=>`<article class="job">${badge(d.status)}${modeBadge(draftMode(d))}<h3><a href="/studio?draft=${esc(d.id)}">${esc(draftTitle(d))}</a></h3><p class="muted">버전 ${d.revision} · ${formatDate(d.updated_at)}</p></article>`).join('')||'<p>아직 선택한 뉴스가 없습니다. 뉴스 탐색부터 시작하세요.</p>';
}
function renderNews() {
  $('news-results').innerHTML=newsGroups.map(g=>`<section class="news-group"><div class="heading"><h2>${esc(g.title)}</h2><span class="score">자체 추천 ${g.score}점</span></div><p class="muted">기사 ${g.article_count}건 · 출처명 ${g.publisher_count}개 · 원문 검증 전</p>${g.articles.map(a=>`<div class="news-item"><label><input type="checkbox" data-article="${esc(a.id)}" ${selected.has(a.id)?'checked':''}>${esc(a.title)}</label><small>${esc(a.publisher)} · ${formatDate(a.published_at)} · <a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">원문 열기 ↗</a></small></div>`).join('')}</section>`).join('')||'<section class="panel">조건에 맞는 뉴스가 없습니다. 검색어나 기간을 바꿔 보세요.</section>';
  $('selected-count').textContent=`선택한 기사 ${selected.size}건`;
  $('create-draft').disabled=!selected.size;
}
function mediaKind(asset) {
  const type=asset.media_type||'';
  return type.startsWith('image')?'image':type.startsWith('video')?'video':type.startsWith('audio')?'audio':type;
}
function mediaOptions(selectedId,kind) {
  const allowed=mediaAssets.filter(a=>a.usage_status==='approved'&&(kind==='audio'?mediaKind(a)==='audio':['image','video'].includes(mediaKind(a))));
  const options=[`<option value="">${kind==='audio'?'음악 없음':'관련 자료 자동 선택 · 없으면 그래픽'}</option>`,...allowed.map(a=>`<option value="${esc(a.asset_id)}" ${selectedId===a.asset_id?'selected':''}>${esc(a.name)} · ${mediaKind(a)==='video'?'영상':mediaKind(a)==='audio'?'음악':'사진'}</option>`)];
  if(selectedId&&!allowed.some(a=>a.asset_id===selectedId))options.push(`<option value="${esc(selectedId)}" selected disabled>저장된 자료 · 현재 승인 목록에 없음</option>`);
  return options.join('');
}
function sceneMediaPreview(assetId) {
  if(!assetId)return '<div class="scene-media-empty"><span class="badge">자동 선택</span><p class="muted">장면과 관련된 승인 자료를 렌더링할 때 선택합니다. 일치하는 자료가 없으면 텍스트·그래픽으로 구성합니다.</p></div>';
  const asset=mediaAssets.find(a=>a.asset_id===assetId&&a.usage_status==='approved'&&['image','video'].includes(mediaKind(a)));
  if(!asset)return '<div class="scene-media-empty"><p class="muted">저장된 자료는 현재 승인 목록에 없습니다. 사용 권한과 자료 상태를 확인하세요.</p></div>';
  const url=`/api/media/${encodeURIComponent(asset.asset_id)}/file`;
  return `<figure class="scene-media-figure">${mediaKind(asset)==='image'?`<img src="${url}" alt="${esc(asset.name)} · 자료화면 미리보기" loading="lazy">`:`<video src="${url}" controls muted playsinline preload="metadata" aria-label="${esc(asset.name)} · 자료화면 미리보기"></video>`}<figcaption><span class="badge">자료화면</span> ${esc(asset.name)}</figcaption></figure>`;
}
function automaticMediaPreview(item) {
  const note='<p class="muted">저장된 대본 기준입니다. 저장 전 변경은 반영하지 않으며, 렌더링할 때 자료가 다시 선택될 수 있습니다.</p>';
  if(item.mode==='graphic'||!item.asset_id)return '<div class="scene-media-empty"><span class="badge">자동 선택 · 그래픽</span><p class="muted">관련된 승인 자료가 없어 텍스트·그래픽으로 구성합니다.</p>'+note+'</div>';
  const kind=mediaKind(item);
  if(item.mode!=='automatic'||!['image','video'].includes(kind))return sceneMediaPreview(null);
  const url=`/api/media/${encodeURIComponent(item.asset_id)}/file`;
  return `<figure class="scene-media-figure">${kind==='image'?`<img src="${url}" alt="${esc(item.name)} · 자동 선택 자료화면" loading="lazy">`:`<video src="${url}" controls muted playsinline preload="metadata" aria-label="${esc(item.name)} · 자동 선택 자료화면"></video>`}<figcaption><span class="badge">자동 선택 · 저장된 대본</span> ${esc(item.name)}${note}</figcaption></figure>`;
}
function updateSceneMediaPreview(select) {
  const row=select.closest('.scene'),preview=row.querySelector('.scene-media-preview');
  if(preview)preview.innerHTML=!select.value&&row.dataset.autoPreview?automaticMediaPreview(JSON.parse(row.dataset.autoPreview)):sceneMediaPreview(select.value);
}
async function refreshStoryboardMedia(draft) {
  if(!draft.script)return;
  const draftId=draft.id,revision=draft.revision,rows=Array.from(document.querySelectorAll('#scene-list .scene'));
  try{
    const results=await api(`/api/drafts/${encodeURIComponent(draftId)}/storyboard-media`);
    if(currentDraft?.id!==draftId||currentDraft?.revision!==revision||!Array.isArray(results))return;
    for(const item of results){
      if(!Number.isInteger(item.scene)||item.scene<0||!['automatic','graphic'].includes(item.mode))continue;
      const row=rows[item.scene];if(!row?.isConnected)continue;
      row.dataset.autoPreview=JSON.stringify(item);
      const select=row.querySelector('[data-scene="asset_id"]');if(select&&!select.value)updateSceneMediaPreview(select);
    }
  }catch{/* Automatic preview is optional; manual media selection remains available. */}
}
function draftQualitySettings(draft) {
  return {edit_style:draft.edit_style||'classic',narration_speed:draft.narration_speed??1.0};
}
function qualityFromForm() {
  const speed=Number($('narration-speed').value),style=$('edit-style').value;
  if(!Number.isFinite(speed)||speed<1||speed>1.35)throw new Error('내레이션 속도는 1.00~1.35 사이로 입력하세요.');
  if(!['classic','reels_v3'].includes(style))throw new Error('영상 편집 스타일을 선택하세요.');
  return {edit_style:style,narration_speed:speed};
}
function updateTemplateGuide() { document.querySelectorAll('[data-template-guide]').forEach(card=>card.classList.toggle('selected',card.dataset.templateGuide===$('template').value)); }
async function refreshMedia() {
  mediaAssets=await api('/api/media');
  $('media-list').innerHTML=mediaAssets.map(a=>{const kind=mediaKind(a),url=`/api/media/${encodeURIComponent(a.asset_id)}/file`;return `<article class="media-card">${kind==='image'?`<img src="${url}" alt="${esc(a.name)} · 자료화면" loading="lazy">`:kind==='video'?`<video src="${url}" controls preload="none" aria-label="${esc(a.name)} · 자료화면"></video>`:kind==='audio'?`<audio src="${url}" controls preload="none" aria-label="${esc(a.name)}"></audio>`:''}<div><span class="badge">${kind==='audio'?'배경 음악':'자료화면'}</span><span class="badge ${a.usage_status==='approved'?'ok':''}">${a.usage_status==='approved'?'사용 승인':'검토 필요'}</span><h3>${esc(a.name)}</h3><p class="muted">${esc(a.license_type)}${a.attribution?' · '+esc(a.attribution):''}</p>${a.source_url&&/^https?:\/\//i.test(a.source_url)?`<a class="text-link" href="${esc(a.source_url)}" target="_blank" rel="noopener noreferrer">출처 확인 ↗</a>`:''}</div></article>`;}).join('')||'<p class="muted">등록한 자료가 없습니다. 사진·영상·음악을 업로드하거나 자료 없이 텍스트·그래픽으로 제작하세요.</p>';
  document.querySelectorAll('[data-scene="asset_id"]').forEach(select=>{const selectedId=select.value;select.innerHTML=mediaOptions(selectedId,'visual');updateSceneMediaPreview(select);});
  if($('music-asset')){const selectedId=$('music-asset').value;$('music-asset').innerHTML=mediaOptions(selectedId,'audio');}
}
async function uploadMedia(event) {
  event.preventDefault();if(mediaBusy)return;
  const file=$('media-file').files[0];if(!file){notice('업로드할 파일을 선택하세요.',true);return;}
  if(!$('media-rights').checked){notice('사용 권한과 출처 표시 조건을 확인하세요.',true);return;}
  const query=new URLSearchParams({name:$('media-name').value.trim()||file.name,category:$('media-category').value,license_type:$('media-license').value,attribution:$('media-attribution').value.trim(),source_url:$('media-source-url').value.trim(),rights_confirmed:'true'});
  mediaBusy=true;$('upload-media').disabled=true;$('media-upload-status').textContent='파일 업로드·검사 중…';
  try{
    const response=await fetch('/api/media/upload?'+query,{method:'POST',headers:{'X-Headline-Token':token,'Content-Type':file.type||'application/octet-stream'},body:file});
    const result=await response.json();if(!response.ok)throw new Error(Array.isArray(result.detail)?result.detail.map(x=>x.msg).join(' · '):result.detail||`HTTP ${response.status}`);
    await refreshMedia();$('media-upload').reset();$('media-upload-status').textContent='업로드 완료. 승인된 자료를 장면 또는 음악에서 선택하세요.';notice('자료를 등록했습니다. 자료 선택은 대본 설정을 저장해야 반영됩니다.');
  }catch(error){$('media-upload-status').textContent='업로드 실패';notice(error.message,true);}finally{mediaBusy=false;$('upload-media').disabled=false;}
}
function today() { return new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Seoul'}); }
function evidenceHTML(e) {
  const fields=[['url','자료 URL','url'],['publisher','출처명','text'],['title','자료 제목','text'],['published_date','발표일','date'],['accessed_date','원문 확인일','date']];
  return `<article class="evidence-row" data-evidence="${esc(e.id)}" data-evidence-base="${esc(JSON.stringify(e))}"><div class="heading"><h3>${e.verification==='source_matched'?'자동 원문 발췌':'근거'} ${esc(e.id)}</h3><button class="secondary remove-evidence" type="button">삭제</button></div><div class="form-grid">${fields.map(([key,label,type])=>`<label>${label}<input data-field="${key}" type="${type}" value="${esc(e[key])}" required></label>`).join('')}</div><label>이 자료로 확인한 핵심 주장 (직접 요약)<textarea data-field="fact" maxlength="300" rows="2">${esc(e.fact)}</textarea></label><label>짧은 근거 문구 (최대 600자, 원문 전체 저장 금지)<textarea data-field="excerpt" maxlength="600" rows="3">${esc(e.excerpt)}</textarea></label><label>기준일·적용 조건·한계<input data-field="conditions" maxlength="300" value="${esc(e.conditions)}" placeholder="예: 발표 계획이며 실제 성과가 아님"></label><label>자료 사용 구분<select data-field="permission"><option value="official" ${e.permission==='official'?'selected':''}>공식 기관·제조사 자료</option><option value="allowed" ${e.permission==='allowed'?'selected':''}>접근·사용이 허용된 자료 (직접 확인)</option></select></label><label class="check-label"><input data-field="reviewed" type="checkbox" ${e.reviewed?'checked':''}>원문을 열어 이 주장과 근거·날짜·적용 조건이 일치함을 확인했습니다.</label></article>`;
}
function evidenceFromForm() {
  return Array.from(document.querySelectorAll('[data-evidence]')).map(row=>{
    const evidence={...JSON.parse(row.dataset.evidenceBase||'{}'),id:row.dataset.evidence};
    row.querySelectorAll('[data-field]').forEach(input=>evidence[input.dataset.field]=input.type==='checkbox'?input.checked:input.value);
    return evidence;
  });
}
function sceneHTML(s,index,evidence) {
  return `<article class="scene" data-scene-base="${esc(JSON.stringify({...s,scene_id:s.scene_id||crypto.randomUUID()}))}" data-captions="${esc(JSON.stringify(s.caption_overrides||[]))}"><div class="heading"><h3>장면 <span class="scene-number">${index+1}</span></h3><div class="scene-controls"><button type="button" class="secondary move-up" aria-label="장면 위로">↑</button><button type="button" class="secondary move-down" aria-label="장면 아래로">↓</button></div></div><label>장면 제목<input data-scene="headline" maxlength="30" value="${esc(s.headline)}"></label><label>내레이션 원문 · 읽는 말대본<textarea data-scene="narration" maxlength="120" rows="3">${esc(s.narration)}</textarea></label><details class="pronunciation-editor"><summary>발음용 대본 확인·수정</summary><p class="muted">화면에 표시할 원문은 유지합니다. 비워 두면 자동 보정하고, 직접 입력할 때는 구절마다 한 줄로 작성하세요. 날짜·금액·단위·고유명사는 직접 확인하세요.</p><div class="actions"><button type="button" class="secondary" data-normalize="preview">자동 발음 미리보기</button><button type="button" class="secondary" data-normalize="apply">자동 발음을 편집칸에 채우기</button></div><pre class="spoken-auto" aria-live="polite">자동 발음 미리보기를 누르면 표시용 구절과 발음용 문장을 확인할 수 있습니다.</pre><label>발음용 대본 (구절마다 한 줄)<textarea data-scene="tts_text" rows="4" maxlength="700" placeholder="비워 두면 표시용 내레이션에서 자동 보정합니다.">${esc(s.tts_text||'')}</textarea></label></details><label>장면 설명<input data-scene="explanation" maxlength="300" value="${esc(s.explanation)}"></label><div class="form-grid"><label>목표 장면 길이 (초)<input type="number" data-scene="duration_seconds" min="1" max="20" step="0.01" value="${s.duration_seconds||5}"></label><label>시각 자료<select data-scene="visual_type">${[['stock_video','자료 영상'],['stock_image','자료 사진'],['uploaded_media','업로드 자료화면'],['animated_headline','움직이는 헤드라인'],['data_chart','데이터 차트'],['comparison_card','비교 카드'],['key_point','핵심 요점'],['summary_card','정리 카드'],['card','기존 뉴스 텍스트 카드'],['concept','개념 그래픽 (실제 통계 아님)'],['quote','출처 설명 카드']].map(([v,t])=>`<option value="${v}" ${s.visual_type===v?'selected':''}>${t}</option>`).join('')}</select></label><label>전달 구분<select data-scene="kind"><option value="fact" ${s.kind==='fact'?'selected':''}>자료로 확인한 사실</option><option value="analysis" ${s.kind==='analysis'?'selected':''}>자료 기반 해설·해석</option></select></label></div><details class="storyboard-editor"><summary>스토리보드 · 화면과 움직임</summary><div class="form-grid"><label><span class="scene-headline-label">${index===0?'첫 화면 큰 후킹 문구':'화면 헤드라인'}</span><input data-scene="headline_text" maxlength="90" value="${esc(s.headline_text||'')}" placeholder="비워 두면 장면 제목 사용"></label><label>장면 자막<input data-scene="subtitle" maxlength="120" value="${esc(s.subtitle||'')}" placeholder="표시할 자막"></label><label class="grow">자료화면 검색 메모<input data-scene="visual_query" maxlength="150" value="${esc(s.visual_query||'')}" placeholder="예: 서울 아파트 전경 · 사용 가능한 자료 자동 검색"></label></div><div class="form-grid"><label>텍스트 움직임<select data-scene="animation_type">${[['fade','페이드'],['slide_up','아래에서 위로'],['slide_down','위에서 아래로'],['scale','크기 변화'],['pop','팝'],['words','단어 순서대로'],['number','숫자 강조']].map(([v,t])=>`<option value="${v}" ${(s.animation_type||'slide_up')===v?'selected':''}>${t}</option>`).join('')}</select></label><label>장면 전환<select data-scene="transition_type">${[['cut','컷'],['fade','페이드'],['wipe','와이프']].map(([v,t])=>`<option value="${v}" ${(s.transition_type||'cut')===v?'selected':''}>${t}</option>`).join('')}</select></label><label>강조할 단어<input data-scene="emphasis_words" value="${esc((s.emphasis_words||[]).join(', '))}" placeholder="쉼표로 구분"></label></div><div class="scene-media-choice"><label>사용할 사진·영상<select data-scene="asset_id">${mediaOptions(s.asset_id,'visual')}</select></label><div class="scene-media-preview" aria-live="polite">${sceneMediaPreview(s.asset_id)}</div></div><p class="muted">선택한 자료화면은 뉴스 사실을 입증하는 근거와 구분합니다. 자료화면이 없으면 텍스트·그래픽을 사용합니다.</p><label>장면 출처 ID<input data-scene="source_refs" value="${esc((s.source_refs||[]).join(', '))}" placeholder="아래 근거 ID를 쉼표로 구분"></label><details><summary>차트 데이터 JSON</summary><p class="muted">데이터 차트·비교용입니다. 확인한 숫자만 입력하고 출처와 기준일을 기록하세요. 비우면 차트를 사용하지 않습니다.</p><label>차트 설정<textarea data-scene="chart" class="chart-json" rows="5" placeholder='{"labels":["항목 A","항목 B"],"values":[10,20],"unit":"%","source_label":"공식 출처","as_of":"2026-10-30","source_refs":["근거 ID"]}'>${esc(s.chart?JSON.stringify(s.chart,null,2):'')}</textarea></label></details></details><p class="muted">이 장면의 핵심 주장과 연결된 근거</p>${evidence.map(e=>`<label class="claim"><input type="checkbox" data-claim="${esc(e.id)}" ${(s.claim_ids||[]).includes(e.id)?'checked':''}>${esc(e.id)} · ${esc(e.fact)}</label>`).join('')}</article>`;
}
function clearSpokenOverride(row) {
  row.querySelector('[data-scene="tts_text"]').value='';
  row.dataset.captions='[]';
  if(row.querySelector('[data-scene="subtitle"]'))row.querySelector('[data-scene="subtitle"]').value=row.querySelector('[data-scene="narration"]').value;
  row.querySelector('.spoken-auto').textContent='표시용 내레이션이 바뀌었습니다. 자동 발음을 다시 확인하세요.';
}
function selectedVoiceProfile() { return voiceProfiles.find(p=>p.voice_profile_id===$('voice-profile')?.value); }
function updateVoiceNote() {
  if(!$('voice-profile'))return;
  const profile=selectedVoiceProfile(), key=$('voice-profile').value;
  $('legacy-voice-label').hidden=Boolean(key)||$('tts-engine').value==='qwen3_mlx';
  if(!key&&$('tts-engine').value==='qwen3_mlx') $('voice-profile-note').textContent='설치·검증된 MLX 프로필이 없습니다. macOS 엔진을 직접 선택해 전환할 수 있습니다.';
  else if(!key) $('voice-profile-note').textContent='기존 대본에 저장된 macOS 목소리를 유지합니다. 새 엔진으로 바꾸려면 프로필을 직접 선택하세요.';
  else if(!profile?.available) $('voice-profile-note').textContent='이 프로필은 현재 설치·검증되지 않았습니다. 사용 가능한 프로필을 직접 선택하세요.';
  else $('voice-profile-note').textContent=`${profile.engine==='qwen3_mlx'?'MLX 한국어 뉴스 음성':'macOS 기본 음성'} · 화자 ${profile.speaker} · 설정 속도 ${profile.speed} · 자연스러운 말속도를 유지하고 실제 음성 길이에 맞춰 영상 시간을 정합니다. 실패 시 다른 음성으로 자동 변경하지 않습니다.`;
}
function fillVoiceProfiles(engine, selectedKey='') {
  const available=voiceProfiles.filter(p=>p.engine===engine&&p.available);
  const options=engine==='macos'?['<option value="">기존 macOS 음성 유지</option>']:[];
  options.push(...available.map(p=>`<option value="${esc(p.voice_profile_id)}">${esc(p.name)}</option>`));
  if(selectedKey&&!available.some(p=>p.voice_profile_id===selectedKey)) options.push(`<option value="${esc(selectedKey)}" disabled>저장된 프로필 · 현재 사용 불가</option>`);
  if(!options.length)options.push('<option value="" disabled>설치·검증된 프로필 없음</option>');
  $('voice-profile').innerHTML=options.join('');
  if(selectedKey||engine==='macos')$('voice-profile').value=selectedKey;
  updateVoiceNote();
}
function setDraftVoice(draft) {
  const profile=voiceProfiles.find(p=>p.voice_profile_id===draft.voice_profile_id);
  const engine=profile?.engine||'macos';
  $('tts-engine').value=engine;
  fillVoiceProfiles(engine,draft.voice_profile_id||'');
  if(profile?.engine==='macos')$('voice').value=profile.speaker;
  $('voice-sample-player').pause();$('voice-sample-player').hidden=true;
  $('voice-sample-status').textContent='';
}
async function playPresetSample() {
  const profile=selectedVoiceProfile();
  if(!profile?.available)throw new Error('설치·검증된 음성 프로필을 먼저 선택하세요.');
  if(!profile.sample_path)throw new Error('저장된 샘플이 없습니다. 샘플 문장으로 먼저 생성하세요.');
  const player=$('voice-sample-player');player.src=`/api/voice-profiles/${encodeURIComponent(profile.voice_profile_id)}/sample`;player.hidden=false;
  $('voice-sample-status').textContent=`${profile.name} 저장된 샘플입니다. 직접 듣고 말투·발음을 확인하세요.`;
  try{await player.play();}catch{$('voice-sample-status').textContent+=' 재생 버튼을 눌러 주세요.';}
}
async function generateVoiceSample() {
  const profile=selectedVoiceProfile();
  if(!profile?.available)throw new Error('설치·검증된 음성 프로필을 먼저 선택하세요.');
  const result=await api('/api/voice-samples','POST',{voice_profile_id:profile.voice_profile_id,text:$('voice-sample-text').value});
  sampleBusy=true;updateButtons();
  $('voice-sample-status').textContent=`${profile.name} 샘플 생성 대기 중…`;
  if(samplePollTimer)clearTimeout(samplePollTimer);
  const poll=async()=>{
    try{
      const record=await api('/api/voice-samples/'+result.id);
      if(record.status==='completed'){
        sampleBusy=false;const player=$('voice-sample-player');player.src=`/api/voice-samples/${result.id}/audio`;player.hidden=false;
        $('voice-sample-status').textContent=`${profile.name} 샘플 생성 완료. 직접 들어 발음과 말투를 확인하세요.`;updateButtons();
      }else if(record.status==='failed'){
        sampleBusy=false;$('voice-sample-status').textContent=record.error||'샘플 생성 실패';notice(record.error||'샘플 생성 실패',true);updateButtons();
      }else{$('voice-sample-status').textContent=`${profile.name} 샘플 ${record.status==='running'?'생성 중':'대기 중'}…`;samplePollTimer=setTimeout(poll,1800);}
    }catch(error){sampleBusy=false;notice(error.message,true);updateButtons();}
  };
  await poll();
}
function commaList(value) { return value.split(',').map(v=>v.trim()).filter(Boolean); }
function parseChart(value,index) {
  if(!value.trim())return null;
  let chart;try{chart=JSON.parse(value);}catch{throw new Error(`장면 ${index+1}: 차트 JSON 형식을 확인하세요.`);}
  if(!chart||Array.isArray(chart)||typeof chart!=='object'||!Array.isArray(chart.labels)||!Array.isArray(chart.values)||chart.labels.length<2||chart.labels.length>6||chart.labels.length!==chart.values.length||!chart.labels.every(v=>typeof v==='string'&&v.trim()&&v.length<=24)||!chart.values.every(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0))throw new Error(`장면 ${index+1}: 차트는 2~6개 항목(24자 이내)과 같은 개수의 0 이상 유한한 숫자가 필요합니다.`);
  if(typeof chart.unit!=='string'||!chart.unit.trim()||chart.unit.length>12)throw new Error(`장면 ${index+1}: 차트 단위를 1~12자로 입력하세요.`);
  if(!Array.isArray(chart.source_refs)||!chart.source_refs.every(v=>typeof v==='string')||!chart.source_refs.length||!String(chart.source_label||'').trim()||!/^\d{4}-\d{2}-\d{2}$/.test(chart.as_of||''))throw new Error(`장면 ${index+1}: 차트 출처명·출처 ID·기준일(YYYY-MM-DD)을 입력하세요.`);
  return chart;
}
function scriptFromForm() {
  if(!currentDraft.script) return null;
  const scenes=Array.from(document.querySelectorAll('.scene')).map((row,index)=>{
    const scene={...JSON.parse(row.dataset.sceneBase||'{}'),claim_ids:Array.from(row.querySelectorAll('[data-claim]:checked')).map(x=>x.dataset.claim),caption_overrides:JSON.parse(row.dataset.captions||'[]')};
    row.querySelectorAll('[data-scene]').forEach(input=>{
      const key=input.dataset.scene,value=input.value;
      scene[key]=key==='duration_seconds'?Number(value):key==='tts_text'||key==='asset_id'?(value.trim()||null):key==='emphasis_words'||key==='source_refs'?commaList(value):key==='chart'?parseChart(value,index):value;
    });
    if(scene.emphasis_words.length>8||scene.source_refs.length>20)throw new Error(`장면 ${index+1}: 강조 단어는 8개, 출처 ID는 20개 이내로 입력하세요.`);
    return scene;
  });
  return {...currentDraft.script,title:$('script-title').value,description:$('script-description').value,hook:$('script-hook').value,hashtags:commaList($('script-hashtags').value).map(s=>s.replace(/^#/,'')),scenes};
}
function markDirty() { dirty=true; for(const id of ['review-check','medical-review-check','sexual-review-check'])if($(id))$(id).checked=false; updateButtons(); }
function updateButtons() {
  if(!currentDraft||!$('studio'))return;
  const active=['researching','generating','rendering'].includes(currentDraft.status);
  for(const id of ['save-evidence','save-script','add-evidence','generate-script','quick-prepare']) if($(id)) $(id).disabled=active;
  if($('approve-script')) $('approve-script').disabled=active||dirty||!currentDraft.script;
  if($('quick-render'))$('quick-render').disabled=active||dirty||!currentDraft.script;
  if($('quick-prepare'))$('quick-prepare').textContent=active?'자동 준비 중…':currentDraft.script?'대본 다시 준비':'대본·배경 자동 준비';
  const profileKey=$('voice-profile')?.value, unavailable=Boolean(profileKey&&!selectedVoiceProfile()?.available)||($('tts-engine')?.value==='qwen3_mlx'&&!profileKey);
  if($('render-video')) $('render-video').disabled=active||dirty||unavailable||currentDraft.approved_revision!==currentDraft.revision;
  document.querySelectorAll('#studio input,#studio textarea,#studio select,#studio [data-normalize],#studio .scene-controls button,#studio .remove-evidence').forEach(input=>input.disabled=active);
  for(const id of ['tts-engine','voice-profile','voice'])if($(id))$(id).disabled=active||sampleBusy;
  for(const id of ['play-voice-sample','generate-voice-sample'])if($(id))$(id).disabled=active||sampleBusy||!selectedVoiceProfile()?.available;
  if($('approval-note')) $('approval-note').textContent=dirty?'저장하지 않은 변경이 있습니다. 저장 후 다시 검토하세요.':currentDraft.validation_sample?'개발 검증 샘플입니다. 게시 전 대본과 출처를 직접 검토하세요.':currentDraft.approved_revision===currentDraft.revision?`버전 ${currentDraft.revision} 검토 승인됨. 같은 버전의 중복 생성은 기존 결과를 반환합니다.`:'저장한 대본과 근거를 검토한 뒤 승인하세요.';
  if($('duration-summary')) { const values=Array.from(document.querySelectorAll('[data-scene="duration_seconds"]')).map(x=>Number(x.value)); $('duration-summary').textContent=`장면 합계 ${values.reduce((a,b)=>a+b,0).toFixed(2)}초 / 목표 ${$('seconds').value}초 · 첫 장면 목표 3초. 실제 음성 길이에 맞춰 장면 시간을 정하며 목표에 맞추기 위해 음성을 감속하지 않습니다.`; }
}
function showDraft(draft) {
  currentDraft=draft;dirty=false;$('studio').hidden=false;$('studio-empty').hidden=true;
  $('draft-heading').textContent=draftTitle(draft);
  $('draft-state').textContent=`${labels[draft.status]||draft.status} · 버전 ${draft.revision}`;
  $('draft-error').textContent=draft.error||'';
  const mode=draftMode(draft),required=reviewRequirements(draft);
  $('quick-review').hidden=!draft.script;
  $('quick-script').value=draft.script?draft.script.scenes.map(s=>s.narration).join('\n\n'):'';
  $('quick-review-check').checked=false;
  $('quick-review-label').textContent='대본·출처'+(required.medical?'·건강/의료 근거와 표현':'')+(required.sexual?'·성인 관계/성 관련 표현':'')+'을 검토했습니다.';
  $('quick-progress-note').textContent=['researching','generating'].includes(draft.status)?'원문 → 관련 근거 → 한국어 대본 → 사용 가능한 배경 순서로 준비하고 있어요. 화면을 열어 두면 자동 갱신됩니다.':draft.status==='rendering'?'음성·자막·720p 영상을 만들고 있어요.':draft.script?'준비됐어요. 아래 대본을 확인한 뒤 영상 만들기를 눌러 주세요.'+(draft.media_search?.errors?.length?' 외부 자료 검색이 제한되어 기존 관련 자료 또는 그래픽을 사용합니다.':''):draft.error||'버튼을 누르면 근거와 대본, 배경을 자동으로 준비합니다.';
  $('content-mode-badge').textContent=draft.source_url?'글 요약':contentModeLabels[mode]||mode;$('content-mode-badge').className='badge'+(mode==='adult_satire'?' fiction-badge':'');
  $('content-mode-notice').className='content-mode-notice'+(mode==='adult_satire'?' fiction-notice':'');
  $('content-mode-notice').textContent=draft.source_url?'입력한 글의 주제로 쇼츠를 준비합니다. 원문 날짜와 출처는 아래에서 확인할 수 있어요.':mode==='adult_satire'?'허구·성인 공감 풍자 · 성인 등장인물의 창작 상황입니다. 실제 뉴스로 오인시키지 않으며 완성 영상의 허구 표시를 확인하세요. 의료·성 소재가 있으면 해당 검토도 필요합니다.':mode==='myth_check'?'속설 검증 · 건강·의학 효과는 공식 근거로 확인한 범위만 설명합니다. 근거가 부족한 주장은 넣지 않으며 검토는 진료를 대신하지 않습니다.':'실제 뉴스 · 원문과 공식 자료에서 주장·숫자·기준일·출처를 확인합니다.';
  $('medical-review-label').hidden=!required.medical;$('sexual-review-label').hidden=!required.sexual;
  $('medical-review-check').required=required.medical;$('sexual-review-check').required=required.sexual;
  $('selected-articles').innerHTML=(draft.articles||[]).map(a=>`<p class="muted"><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a> · ${esc(a.publisher)} · ${formatDate(a.published_at)}</p>`).join('')||`<p class="muted">주제: ${esc(draft.topic||draftTitle(draft))}${mode==='adult_satire'?' · 기사 기반 뉴스가 아닌 창작 콘텐츠입니다.':''}</p>`;
  const option=Array.from($('draft-picker').options).find(o=>o.value===draft.id);if(option)option.textContent=(draftTitle(draft))+' · '+(labels[draft.status]||draft.status);
  for(const key of ['seconds','resolution','voice']) $(key).value=draft[key];
  const quality=draftQualitySettings(draft);$('edit-style').value=quality.edit_style;$('narration-speed').value=Number(quality.narration_speed).toFixed(2);
  $('template').value=draft.template||'headline_news';$('subtitle-style').value=draft.subtitle_style||'clean';
  $('music-asset').innerHTML=mediaOptions(draft.music_asset_id,'audio');updateTemplateGuide();
  setAnchorSelection($('studio-anchor'),draft.anchor_id||'');
  setDraftVoice(draft);
  $('evidence-list').innerHTML=draft.evidence.map(evidenceHTML).join('');
  $('script-editor').hidden=!draft.script;
  if(draft.script) {
    $('script-title').value=draft.script.title;$('script-hook').value=draft.script.hook;
    $('full-script').value=draft.script.scenes.map(s=>s.narration).join('\n');
    $('script-description').value=draft.script.description;$('script-hashtags').value=draft.script.hashtags.join(', ');
    $('scene-list').innerHTML=draft.script.scenes.map((s,i)=>sceneHTML(s,i,draft.evidence)).join('');
    refreshStoryboardMedia(draft);
  }
  for(const id of ['review-check','medical-review-check','sexual-review-check'])$(id).checked=false;updateButtons();lastJobs='';refreshJobs().catch(e=>notice(e.message,true));
}
async function saveDraft() {
  if(!currentDraft) throw new Error('먼저 이슈를 선택하세요.');
  if($('tts-engine').value==='qwen3_mlx'&&!selectedVoiceProfile()?.available)throw new Error('사용 가능한 MLX 프로필이 없습니다. 자동으로 다른 음성으로 바꾸지 않습니다. macOS를 직접 선택하거나 모델 설정을 확인하세요.');
  const body={...qualityFromForm(),content_mode:draftMode(currentDraft),topic:currentDraft.topic||'',fiction_acknowledged:Boolean(currentDraft.fiction_acknowledged),revision:currentDraft.revision,script:scriptFromForm(),evidence:evidenceFromForm(),seconds:Number($('seconds').value),resolution:Number($('resolution').value),voice:$('voice').value,voice_profile_id:$('voice-profile').value||null,template:$('template').value,subtitle_style:$('subtitle-style').value,music_asset_id:$('music-asset').value||null,anchor_id:$('studio-anchor')?.value||null};
  const draft=await api(`/api/drafts/${currentDraft.id}`,'PUT',body);showDraft(draft);return draft;
}
async function studio() {
  const [drafts,config,profiles]=await Promise.all([api('/api/drafts'),api('/api/settings'),api('/api/voice-profiles')]);
  await loadAnchorPickers();
  updateNewContentMode();
  $('new-content-mode').addEventListener('change',()=>{const mode=$('new-content-mode').value;$('new-content-category').value=mode==='myth_check'?'health':mode==='adult_satire'?'trending':'economy';updateNewContentMode();});
  $('new-content').addEventListener('submit',async e=>{const button=$('create-content');button.disabled=true;try{await createContent(e);}catch(error){notice(error.message,true);}finally{button.disabled=false;}});
  await refreshMedia();
  on('refresh-media',refreshMedia);
  $('media-upload').addEventListener('submit',uploadMedia);
  $('template').addEventListener('change',updateTemplateGuide);
  voiceProfiles=profiles;
  $('voice').innerHTML=config.voices.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
  $('draft-picker').innerHTML='<option value="">제작할 이슈 선택</option>'+drafts.map(d=>`<option value="${d.id}">${esc(draftTitle(d))} · ${esc(labels[d.status]||d.status)}</option>`).join('');
  const id=new URLSearchParams(location.search).get('draft');
  if(id){document.body.classList.add('has-draft');$('draft-picker').value=id;showDraft(await api('/api/drafts/'+id));}
  $('draft-picker').addEventListener('change',()=>{if($('draft-picker').value)location.href='/studio?draft='+encodeURIComponent($('draft-picker').value);});
  $('tts-engine').addEventListener('change',()=>{fillVoiceProfiles($('tts-engine').value);markDirty();});
  $('voice-profile').addEventListener('change',()=>{const p=selectedVoiceProfile();if(p?.engine==='macos')$('voice').value=p.speaker;$('voice-sample-player').pause();$('voice-sample-player').hidden=true;updateVoiceNote();markDirty();});
  on('play-voice-sample',playPresetSample);on('generate-voice-sample',generateVoiceSample);
  $('studio').addEventListener('change',e=>{if(e.target.matches('[data-scene="asset_id"]'))updateSceneMediaPreview(e.target);});
  $('studio').addEventListener('input',e=>{
    if(['review-check','medical-review-check','sexual-review-check','quick-review-check','preview-url','voice-sample-text'].includes(e.target.id))return;
    if(e.target.matches('[data-scene="narration"]'))clearSpokenOverride(e.target.closest('.scene'));
    const row=e.target.closest('[data-evidence]');if(row&&e.target.dataset.field!=='reviewed')row.querySelector('[data-field="reviewed"]').checked=false;
    if(e.target.matches('[data-scene="narration"]')&&e.target.closest('.scene')===$('scene-list').firstElementChild)$('script-hook').value=e.target.value;
    if(e.target.id==='script-hook'&&$('scene-list').firstElementChild){const first=$('scene-list').firstElementChild;first.querySelector('[data-scene="narration"]').value=e.target.value;clearSpokenOverride(first);}
    markDirty();
  });
  on('add-evidence',()=>{const evidence={id:crypto.randomUUID().replaceAll('-','').slice(0,12),url:'',title:'',publisher:'',fact:'',excerpt:'',conditions:'',published_date:today(),accessed_date:today(),reviewed:false,permission:'official'};$('evidence-list').insertAdjacentHTML('beforeend',evidenceHTML(evidence));markDirty();});
  on('preview-source',async()=>{const result=await api('/api/evidence/preview','POST',{url:$('preview-url').value});$('source-preview').textContent=result.title+'\n\n'+result.preview+'\n\n'+result.notice;});
  on('save-evidence',async()=>{await saveDraft();notice('근거와 설정을 저장했습니다.');});
  on('save-script',async()=>{await saveDraft();notice('편집한 대본을 새 버전으로 저장했습니다.');});
  on('quick-prepare',async()=>{const d=await saveDraft();showDraft(await api(`/api/drafts/${d.id}/prepare`,'POST',{revision:d.revision}));notice('원문·근거·대본·자료화면을 자동으로 준비합니다.');});
  on('quick-render',async()=>{if(!$('quick-review-check').checked)throw new Error('대본과 표시된 검토 항목을 확인한 뒤 체크해 주세요.');const required=reviewRequirements(currentDraft);await api(`/api/drafts/${currentDraft.id}/approve`,'POST',{revision:currentDraft.revision,content_reviewed:true,medical_reviewed:required.medical,sexual_reviewed:required.sexual});await api(`/api/drafts/${currentDraft.id}/render`,'POST',{revision:currentDraft.revision});showDraft(await api('/api/drafts/'+currentDraft.id));notice('720p 영상 제작을 시작했습니다.');});
  on('generate-script',async()=>{const draft=await saveDraft();showDraft(await api(`/api/drafts/${draft.id}/prepare`,'POST',{revision:draft.revision}));notice(draftMode(draft)==='adult_satire'?'로컬 Qwen3로 허구·성인 공감 풍자 초안을 생성합니다. 허구 표시와 내용을 검토하세요.':'로컬 Qwen3로 근거 기반 대본을 생성합니다. 완료 후 검토하세요.');});
  on('approve-script',async()=>{showDraft(await api(`/api/drafts/${currentDraft.id}/approve`,'POST',reviewPayload()));notice('현재 버전의 필수 항목을 검토 승인했습니다.');});
  on('render-video',async()=>{const job=await api(`/api/drafts/${currentDraft.id}/render`,'POST',{revision:currentDraft.revision});notice('영상 작업 '+job.id+'를 확인하세요.');showDraft(await api('/api/drafts/'+currentDraft.id));});
  on('apply-full-script',()=>{const lines=$('full-script').value.split('\n').map(s=>s.trim()).filter(Boolean),rows=Array.from(document.querySelectorAll('.scene'));if(lines.length!==rows.length)throw new Error(`현재 장면 ${rows.length}개와 같은 줄 수로 입력하세요.`);rows.forEach((row,i)=>{if(row.querySelector('[data-scene="narration"]').value!==lines[i]){row.querySelector('[data-scene="narration"]').value=lines[i];clearSpokenOverride(row);}});$('script-hook').value=lines[0];markDirty();notice('전체 대본을 장면에 적용했습니다. 저장 후 다시 검토하세요.');});
  setInterval(async()=>{if(!currentDraft)return;try{const d=await api('/api/drafts/'+currentDraft.id);if(!dirty&&(d.revision!==currentDraft.revision||d.status!==currentDraft.status))showDraft(d);await refreshJobs();}catch(e){notice(e.message,true);}},2000);
}
function experimentNumber(value, unit='', digits=2) {
  return typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('ko-KR',{maximumFractionDigits:digits})+unit:'미측정';
}
async function anchorExperimentsPage() {
  const results=await api('/api/anchor-experiments');
  $('anchor-experiments').innerHTML=results.length?results.map(item=>`<section class="panel anchor-experiment"><div class="heading"><div><p class="eyebrow">LIP-SYNC BENCHMARK</p><h2>${experimentNumber(item.seconds,'초',0)} 입 모양 합성 실험</h2></div><span class="badge ${item.status==='completed'?'ok':''}">${item.status==='completed'?'기술 실험 완료':esc(labels[item.status]||'상태 확인 필요')}</span></div><div class="anchor-experiment-body">${item.video_url?`<video class="anchor-experiment-video" aria-label="${experimentNumber(item.seconds,'초',0)} 입 모양 실험 미리보기" controls preload="metadata" width="${item.width||512}" height="${item.height||512}" src="${esc(item.video_url)}"></video>`:'<p class="muted">아직 재생할 수 있는 결과 영상이 없습니다.</p>'}<div class="anchor-experiment-info"><p><span class="badge">임시 가상 얼굴</span><span class="badge">${item.static_portrait_mouth_only?'정지 영상 · 입 주변 합성':'실험 영상'}</span><span class="badge">진행자 조합 미검증</span></p><dl class="experiment-metrics"><div><dt>출력 규격</dt><dd>${experimentNumber(item.width,'',0)} × ${experimentNumber(item.height,'',0)} · ${experimentNumber(item.fps,'fps',0)}</dd></div><div><dt>총 처리 시간</dt><dd>${experimentNumber(item.total_seconds,'초')}</dd></div><div><dt>모델 로딩</dt><dd>${experimentNumber(item.model_load_seconds,'초')}</dd></div><div><dt>입 모양 추론</dt><dd>${experimentNumber(item.inference_seconds,'초')}</dd></div><div><dt>프로세스 RSS 피크</dt><dd>${experimentNumber(item.peak_rss_bytes===null?null:item.peak_rss_bytes/1073741824,' GiB')}</dd></div><div><dt>MPS 드라이버 관측 최대</dt><dd>${experimentNumber(item.mps_observed_driver_bytes===null?null:item.mps_observed_driver_bytes/1073741824,' GiB')}</dd></div></dl><p class="muted">메모리는 서로 다른 관측 지표입니다. 합산해 시스템 전체 통합 메모리 사용량으로 해석하지 않습니다.</p><p class="muted">${item.body_motion_generated?'원본 영상 움직임을 포함하는 실험입니다. 별도 몸짓 생성 검증과 구분해야 합니다.':'몸짓 생성 미구현 · 방송 품질과 자연스러움은 직접 재생해 확인해야 합니다.'}</p>${item.video_url?`<a class="text-link" href="${esc(item.video_url)}" download>실험 영상 저장 ↓</a>`:''}</div></div><p class="muted">실험 ID ${esc(item.id)}</p></section>`).join(''):'<section class="panel"><h2>아직 저장된 실험 결과가 없습니다.</h2><p>실험이 완료되면 영상과 실제 측정값을 여기서 확인할 수 있습니다.</p></section>';
}
async function settingsPage() {
  const data=await api('/api/diagnostics');$('summary').textContent=`${data.ready?'제작 준비 완료':'확인이 필요한 항목이 있습니다'} · Python ${data.python} · 여유 ${data.free_disk_gib}GiB`;
  $('checks').innerHTML=Object.entries(data.checks).map(([name,c])=>`<div class="check"><strong class="${c.ok?'ok':'fail'}">${c.ok?'✓':'!'} ${esc(name)}</strong><small>${esc(c.error||c.version||c.selected||c.url||c.path||c.fix)}</small></div>`).join('');
  const config=await api('/api/settings');$('settings-info').textContent=JSON.stringify(config,null,2);
  await refreshYouTubeStatus();
}
document.addEventListener('click',async e=>{
  const resume=e.target.closest('[data-resume]'),cancel=e.target.closest('[data-cancel]');
  try {
    if(resume){await api(`/api/jobs/${resume.dataset.resume}/resume`,'POST');notice('남은 단계부터 재개합니다.');lastJobs='';await refreshJobs();}
    if(cancel){await api(`/api/jobs/${cancel.dataset.cancel}/cancel`,'POST');notice('취소 요청을 전달했습니다. 실행 중인 미디어 프로세스를 종료합니다.');}
    const normalize=e.target.closest('[data-normalize]');
    if(normalize){const row=normalize.closest('.scene'),text=row.querySelector('[data-scene="narration"]').value;normalize.disabled=true;try{const result=await api('/api/tts/normalize','POST',{texts:[text]});const parts=result[0];row.querySelector('.spoken-auto').textContent=parts.map((part,i)=>`${i+1}. 표시: ${part.text}\n   발음: ${part.spoken}`).join('\n\n');if(normalize.dataset.normalize==='apply'){row.querySelector('[data-scene="tts_text"]').value=parts.map(part=>part.spoken).join('\n');markDirty();}}finally{normalize.disabled=false;}}
    if(e.target.closest('.remove-evidence')){e.target.closest('.evidence-row').remove();markDirty();}
    const move=e.target.closest('.move-up,.move-down');
    if(move){const row=move.closest('.scene'),other=move.classList.contains('move-up')?row.previousElementSibling:row.nextElementSibling;if(other){const a=row.querySelector('[data-scene="duration_seconds"]'),b=other.querySelector('[data-scene="duration_seconds"]');[a.value,b.value]=[b.value,a.value];if(move.classList.contains('move-up'))other.before(row);else other.after(row);document.querySelectorAll('.scene-number').forEach((n,i)=>{n.textContent=i+1;const label=n.closest('.scene').querySelector('.scene-headline-label');if(label)label.textContent=i===0?'첫 화면 큰 후킹 문구':'화면 헤드라인';});$('script-hook').value=$('scene-list').firstElementChild.querySelector('[data-scene="narration"]').value;markDirty();}}
  }catch(error){notice(error.message,true);}
});
let anchorProfiles = [], submissions = [], submissionSignature = '', submissionRefreshing = false;
const submissionLabels = {queued:'대기 중',preparing:'대본·자료 준비 중',rendering:'영상 제작 중',uploading:'YouTube 업로드 중',completed:'완료',needs_auth:'YouTube 연결 필요',needs_review:'내용 확인 필요',failed:'실패',cancelled:'취소됨'};
const submissionStages = {source_and_script:'원문·대본 준비',render_validation:'대본·제작 설정 확인',youtube_upload:'YouTube 비공개 업로드',queued:'등록 대기',source:'원문 읽기',research:'자료 확인',researching:'자료 확인',prepare:'대본·자료 준비',preparing:'대본·자료 준비',script:'대본 생성',generating:'대본 생성',tts:'음성·자막 제작',cards:'장면 준비',render:'영상 합성',rendering:'영상 제작',verify:'영상 검사',upload:'YouTube 업로드',uploading:'YouTube 업로드',completed:'비공개 업로드 완료',needs_auth:'YouTube 연결 필요',needs_review:'내용 확인 필요',failed:'실패',cancelled:'취소됨'};
function safeWebURL(value) {
  try { const url=new URL(value); return ['http:','https:'].includes(url.protocol)?url.href:''; } catch { return ''; }
}
function setAnchorSelection(select, value='') {
  if(!select)return;
  select.innerHTML='<option value="">미포함</option>'+anchorProfiles.map(profile=>`<option value="${esc(profile.anchor_id)}" ${profile.available?'':'disabled'}>포함 · 실험${profile.available?'':' · 현재 사용 불가'}</option>`).join('');
  if(value&&!anchorProfiles.some(profile=>profile.anchor_id===value))select.insertAdjacentHTML('beforeend',`<option value="${esc(value)}" disabled>저장된 아나운서 · 현재 사용 불가</option>`);
  select.value=value;
}
async function loadAnchorPickers() {
  try {
    const result=await api('/api/anchor-profiles');
    anchorProfiles=(Array.isArray(result)?result:result.profiles||[]).filter(profile=>profile.anchor_id==='headline60-brown-adult-01').slice(0,1);
    document.querySelectorAll('[data-anchor-picker]').forEach(select=>setAnchorSelection(select,select.value));
  } catch {
    document.querySelectorAll('[data-anchor-picker]').forEach(select=>{
      if(!select.value)select.innerHTML='<option value="">미포함</option><option disabled>포함 · 실험 · 상태 확인 필요</option>';
    });
  }
}
function submissionRow(row, position) {
  const url=safeWebURL(row.url),youtube=safeWebURL(row.youtube_url);
  const canPreview=row.job_id&&(['uploading','completed','needs_auth'].includes(row.status)||row.stage==='youtube_upload');
  const status=submissionLabels[row.status]||row.status;
  const stage=submissionStages[row.stage]||row.stage||'';
  const retry=['failed','needs_auth','needs_review','cancelled'].includes(row.status);
  return `<tr data-submission="${esc(row.id)}"><td class="submission-order">${row.status==='queued'?`<strong>${position}</strong>`:['preparing','rendering','uploading'].includes(row.status)?'<span class="submission-active">진행 중</span>':'—'}<small>등록 #${esc(row.sequence)}</small></td><td class="submission-title"><strong>${esc(row.title||'제목 확인 중')}</strong>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`:''}<small>${row.anchor_id?'아나운서 포함 · 실험':'아나운서 미포함'} · ${esc(formatDate(row.created_at))}</small></td><td><span class="badge ${row.status==='completed'?'ok':''} ${row.status==='failed'?'submission-failed':''}">${esc(status)}</span>${stage&&stage!==status?`<small>${esc(stage)}</small>`:''}${row.error?`<details class="submission-error"><summary>상세 내용</summary><p>${esc(row.error)}</p></details>`:''}${row.status==='needs_auth'?'<a href="/settings">YouTube 연결 →</a>':''}${row.status==='needs_review'&&row.draft_id?`<a href="/studio?draft=${encodeURIComponent(row.draft_id)}">대본 확인 →</a>`:''}</td><td>${canPreview?`<button type="button" class="secondary" data-submission-preview="${esc(row.id)}">미리보기</button>`:'<span class="muted">준비 중</span>'}</td><td>${youtube?`<a href="${esc(youtube)}" target="_blank" rel="noopener noreferrer">영상 열기 ↗</a><small>비공개</small>`:'—'}</td><td>${retry?`<button type="button" class="secondary" data-submission-retry="${esc(row.id)}">다시 시도</button>`:row.status==='completed'?`<button type="button" class="secondary" data-submission-rebuild="${esc(row.id)}">다시 만들기</button>`:'—'}</td></tr>`;
}
async function refreshSubmissions() {
  if(submissionRefreshing)return;
  submissionRefreshing=true;
  try {
    const result=await api('/api/submissions');
    const rows=Array.isArray(result)?result:result.rows||result.submissions;
    if(!Array.isArray(rows))throw new Error('등록 목록 응답을 확인할 수 없습니다.');
    submissions=[...rows].sort((a,b)=>Number(a.sequence)-Number(b.sequence));
    const signature=JSON.stringify(submissions);
    if(signature!==submissionSignature){
      let position=0;
      $('submission-list').innerHTML=submissions.map(row=>submissionRow(row,row.status==='queued'?++position:0)).join('')||'<tr><td colspan="6" class="submission-empty">아직 등록한 글이 없습니다. 위에 URL을 입력하세요.</td></tr>';
      $('submission-count').textContent=`${submissions.length}건`;
      submissionSignature=signature;
    }
    $('submission-sync').textContent='자동 갱신 중';
  } catch(error) {
    $('submission-sync').textContent='연결 확인 중 · 자동으로 다시 시도합니다';
    if(!submissionSignature)$('submission-list').innerHTML='<tr><td colspan="6" class="submission-empty">등록 목록을 불러오지 못했습니다. 잠시 후 다시 확인합니다.</td></tr>';
  } finally { submissionRefreshing=false; }
}
async function refreshYouTubeStatus() {
  const statusElement=$('youtube-status')||$('submission-youtube');
  if(!statusElement)return;
  try {
    const result=await api('/api/youtube/status');
    statusElement.textContent=result.connected?`YouTube 연결됨${result.channel?.title?' · '+result.channel.title:''}`:result.client_configured?'YouTube 계정 연결이 필요합니다.':'YouTube 연결 설정이 필요합니다.';
    if(page==='/'){
      if(!result.connected){const link=document.createElement('a');link.href='/settings';link.textContent=' 설정에서 연결 →';statusElement.append(link);}
    }else{
      $('youtube-connect').textContent=result.connected?'다른 계정으로 연결':'YouTube 연결';
      $('youtube-connect').disabled=!result.client_configured;
      $('youtube-client-form').hidden=Boolean(result.client_configured);
      if(result.error){const error=document.createElement('span');error.textContent=' · '+result.error;statusElement.append(error);}
    }
  } catch { statusElement.textContent='YouTube 연결 상태를 확인하지 못했습니다. 잠시 후 다시 확인합니다.'; }
}
async function submissionPage() {
  $('submission-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=$('submission-register');if(button.disabled)return;
    const input=$('submission-url'),url=input.value.trim();button.disabled=true;button.textContent='등록 중…';
    try {
      const row=await api('/api/submissions','POST',{url,anchor_id:$('submission-anchor').value||null});
      if(input.value.trim()===url)input.value='';
      input.focus();notice(`등록했습니다${row.sequence?' · 등록 #'+row.sequence:''}. 다음 URL을 계속 입력하세요.`);
      await refreshSubmissions();
    } catch(error) { notice(error.message,true); }
    finally { button.disabled=false;button.textContent='등록'; }
  });
  $('submission-list').addEventListener('click',async event=>{
    const retry=event.target.closest('[data-submission-retry]'),preview=event.target.closest('[data-submission-preview]'),rebuild=event.target.closest('[data-submission-rebuild]');
    if(rebuild){
      if(rebuild.disabled||!confirm('대본과 영상을 새로 만들어 새 비공개 영상으로 올립니다. 기존 YouTube 영상은 그대로 남습니다. 진행할까요?'))return;rebuild.disabled=true;
      try {await api(`/api/submissions/${encodeURIComponent(rebuild.dataset.submissionRebuild)}/rebuild`,'POST');notice('다시 만들기를 등록했습니다.');await refreshSubmissions();}
      catch(error){notice(error.message,true);}finally{rebuild.disabled=false;}
      return;
    }
    if(retry){
      if(retry.disabled)return;retry.disabled=true;
      try {await api(`/api/submissions/${encodeURIComponent(retry.dataset.submissionRetry)}/retry`,'POST');notice('다시 시도하도록 등록했습니다.');await refreshSubmissions();}
      catch(error){notice(error.message,true);}finally{retry.disabled=false;}
    }
    if(preview){
      const row=submissions.find(item=>item.id===preview.dataset.submissionPreview);if(!row?.job_id)return;
      $('submission-preview-title').textContent=row.title||'영상 미리보기';
      $('submission-preview-video').src=`/jobs/${encodeURIComponent(row.job_id)}/output.mp4`;
      $('submission-preview').showModal();
    }
  });
  $('submission-preview-close').addEventListener('click',()=>$('submission-preview').close());
  $('submission-preview').addEventListener('close',()=>{$('submission-preview-video').pause();$('submission-preview-video').removeAttribute('src');$('submission-preview-video').load();});
  await Promise.allSettled([loadAnchorPickers(),refreshSubmissions(),refreshYouTubeStatus()]);
  setInterval(refreshSubmissions,2500);
  setInterval(refreshYouTubeStatus,10000);
}
function youtubeSettings() {
  on('youtube-connect',async()=>{
    const result=await api('/api/youtube/connect','POST');
    const url=safeWebURL(result.authorization_url);
    if(!url||new URL(url).hostname!=='accounts.google.com')throw new Error('Google 연결 주소를 확인하지 못했습니다.');
    $('youtube-auth-link').href=url;$('youtube-auth').hidden=false;
    $('youtube-status').textContent='아래 Google 연결 링크를 열어 계정 연결을 승인하세요.';
    $('youtube-auth-link').focus();
  });
  $('youtube-client-form').addEventListener('submit',async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try {
      const file=$('youtube-client-file').files[0];if(!file)throw new Error('JSON 설정 파일을 선택하세요.');
      let client;try {client=JSON.parse(await file.text());}catch {throw new Error('올바른 JSON 파일을 선택하세요.');}
      await api('/api/youtube/client','POST',{client});$('youtube-client-form').reset();
      await refreshYouTubeStatus();notice('YouTube 연결 설정을 저장했습니다. 계정을 연결하세요.');
    } catch(error){notice(error.message,true);}finally{button.disabled=false;}
  });
  setInterval(refreshYouTubeStatus,5000);
}

function quickStart() {
  if($('quick-url'))$('quick-url').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;notice('공개 글을 읽고 자동으로 제작을 준비합니다…');try{const d=await api('/api/source-drafts','POST',{url:$('quick-url-input').value});location.href='/studio?draft='+d.id;}catch(error){notice(error.message,true);b.disabled=false;}});
  if($('quick-search'))$('quick-search').addEventListener('submit',e=>{e.preventDefault();location.href='/news?keyword='+encodeURIComponent($('quick-search-input').value);});
}
(async()=>{
  quickStart();
  if(page==='/')await submissionPage();
  if(page==='/news'){
    await loadAnchorPickers();
    newsGroups=(await api('/api/news')).groups;renderNews();
    $('news-search').addEventListener('submit',async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;notice('Google News RSS에서 후보를 수집하고 있습니다…');try{const result=await api('/api/news/collect','POST',{category:$('category').value,keyword:$('keyword').value,days:Number($('days').value)});newsGroups=result.groups;selected.clear();renderNews();notice(`${result.collected}건 수집 · ${result.groups.length}개 묶음. ${result.notice}`);}catch(error){notice(error.message,true);}finally{button.disabled=false;}});
    $('news-results').addEventListener('change',e=>{if(e.target.dataset.article){if(e.target.checked)selected.add(e.target.dataset.article);else selected.delete(e.target.dataset.article);$('selected-count').textContent=`선택한 기사 ${selected.size}건`;$('create-draft').disabled=!selected.size;}});
    on('create-draft',async()=>{await api('/api/submissions/selected','POST',{article_ids:[...selected],category:$('category').value,anchor_id:$('news-anchor').value||null});selected.clear();renderNews();notice('선택한 기사를 등록했습니다. 홈의 등록 목록에서 진행 상황을 확인할 수 있습니다.');});
    $('manual-news').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/news/manual','POST',{url:$('manual-url').value,title:$('manual-title').value,publisher:$('manual-publisher').value,published_date:$('manual-date').value||null,category:$('category').value});newsGroups=(await api('/api/news')).groups;renderNews();notice('직접 입력한 URL을 후보로 저장했습니다. 원문 검증은 별도입니다.');}catch(error){notice(error.message,true);}});
  }
  if(page==='/news'){const q=new URLSearchParams(location.search).get('keyword');if(q){$('keyword').value=q;$('news-search').requestSubmit();}}
  if(page==='/studio')await studio();
  if(page==='/history'){await refreshJobs();setInterval(()=>refreshJobs().catch(e=>notice(e.message,true)),2000);}
  if(page==='/anchors'){await anchorExperimentsPage();on('refresh-anchor-experiments',anchorExperimentsPage);}
  if(page==='/settings'){youtubeSettings();await settingsPage();on('refresh',settingsPage);}
})().catch(error=>notice(error.message,true));
