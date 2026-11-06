document.getElementById('copy-script').addEventListener('click', async () => {
  try {
    const text = Array.from(document.querySelectorAll('#whole-narration p')).map(p => p.textContent).join('\n\n');
    await navigator.clipboard.writeText(text);
    document.getElementById('copy-status').textContent = '전체 내레이션을 복사했습니다.';
  } catch {
    document.getElementById('copy-status').textContent = '복사 권한을 사용할 수 없습니다. 텍스트를 선택하거나 TXT로 저장하세요.';
  }
});
