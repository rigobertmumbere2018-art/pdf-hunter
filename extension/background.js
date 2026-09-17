chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type !== 'PDF_HUNTER_DOWNLOAD') return;
  chrome.downloads.download({url: msg.url, saveAs: false}, id => {
    sendResponse({ok: !!id, id});
  });
  return true;
});