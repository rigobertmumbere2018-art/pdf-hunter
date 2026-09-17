const EXT=/\.(pdf|jpe?g|png|webp|gif|svg)(?:[?#].*)?$/i;
let files=[];
const $=id=>document.getElementById(id);
function render(){
  $('status').textContent=`${files.length} fichier(s) détecté(s)`;
  $('zip').disabled=!files.length;
  if(!files.length){$('list').innerHTML='<div class="empty">Aucun PDF ou fichier image détecté.</div>';return;}
  $('list').innerHTML=files.map((u,i)=>{
    let n=decodeURIComponent(new URL(u).pathname.split('/').pop()||`fichier-${i+1}`);
    let ext=(n.match(/\.[^.]+$/)||[''])[0].toUpperCase().replace('.','');
    return `<div class="item"><div class="name">${escapeHtml(n)}</div><div class="meta">${ext||'FICHIER'} · ${escapeHtml(u)}</div><button class="open" data-i="${i}">OUVRIR</button></div>`;
  }).join('');
  document.querySelectorAll('.open').forEach(b=>b.onclick=()=>chrome.tabs.create({url:files[+b.dataset.i]}));
}
function escapeHtml(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function getTab(){return (await chrome.tabs.query({active:true,currentWindow:true}))[0];}
$('scan').onclick=async()=>{
  $('status').textContent='Inspection de la page en cours…';
  try{
    const tab=await getTab();
    const r=await chrome.tabs.sendMessage(tab.id,{type:'PDF_HUNTER_SCAN_REQUEST'});
    files=[...(r?.files||[])].filter(u=>EXT.test(u));
    files=[...new Set(files)];
    render();
  }catch(e){
    try{
      const tab=await getTab();
      const r=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>[...new Set([...document.querySelectorAll('a,img,source,video,audio,link,object,embed')].flatMap(e=>['href','src','data-src','data-original','data-url'].map(a=>e.getAttribute(a)).filter(Boolean)).map(x=>{try{return new URL(x,location.href).href}catch(_){return null}}).filter(Boolean))]});
      files=(r[0]?.result||[]).filter(u=>EXT.test(u));
      render();
    }catch(err){$('status').textContent='Cette page ne permet pas l’inspection par extension.';}
  }
};
$('zip').onclick=async()=>{
  if(!files.length)return;
  $('status').textContent='Préparation du ZIP…';
  for(const u of files){chrome.downloads.download({url:u,saveAs:false});}
  $('status').textContent=`Téléchargement lancé pour ${files.length} fichier(s).`;
};
