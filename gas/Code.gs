const SHEET_NAME = 'news';
const MAX_ROWS = 2000;

function doGet(e) {
  const sheet = getSheet();
  const rows = sheet.getDataRange().getDisplayValues();
  const header = rows[0] || ['title', 'link', 'score', 'saved_at'];
  const data = rows.slice(1).filter(row => row.some(value => value !== ''));

  const html = `<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AIニュース一覧</title>
  <style>
    body { font-family: sans-serif; margin: 2rem; line-height: 1.6; }
    .card { border: 1px solid #ddd; padding: 1rem 1.25rem; border-radius: 8px; max-width: 900px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ddd; padding: 0.5rem; text-align: left; }
    th { background: #f7f7f7; }
  </style>
</head>
<body>
  <div class="card">
    <h1>AIニュース一覧</h1>
    <p>保存件数: ${data.length}件</p>
    <table>
      <thead>
        <tr><th>タイトル</th><th>リンク</th><th>スコア</th><th>保存日時</th></tr>
      </thead>
      <tbody>
        ${data.map(row => {
          const title = escapeHtml(row[0] || '');
          const link = row[1] || '';
          const score = row[2] || '';
          const savedAt = row[3] || '';
          const linkHtml = link ? `<a href="${escapeHtml(link)}" target="_blank" rel="noreferrer">${title}</a>` : title;
          return `<tr><td>${linkHtml}</td><td>${escapeHtml(link)}</td><td>${escapeHtml(score)}</td><td>${escapeHtml(savedAt)}</td></tr>`;
        }).join('')}
      </tbody>
    </table>
  </div>
</body>
</html>`;

  return HtmlService.createHtmlOutput(html).setTitle('AIニュース一覧');
}

function doPost(e) {
  const payload = e && e.postData && e.postData.contents ? JSON.parse(e.postData.contents) : {};
  const sheet = getSheet();

  if (sheet.getLastRow() >= MAX_ROWS) {
    sheet.deleteRow(2);
  }

  const newsList = Array.isArray(payload.news) ? payload.news : [];
  const savedAt = new Date().toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' });

  newsList.forEach(item => {
    const title = item && item.title ? String(item.title) : '';
    const link = item && item.link ? String(item.link) : '';
    const score = item && item.score !== undefined ? String(item.score) : '';
    sheet.appendRow([title, link, score, savedAt]);
  });

  return ContentService.createTextOutput(JSON.stringify({ ok: true, saved: newsList.length }));
}

function getSheet() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = spreadsheet.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(SHEET_NAME);
    sheet.appendRow(['title', 'link', 'score', 'saved_at']);
  }
  return sheet;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
