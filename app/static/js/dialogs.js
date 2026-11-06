// The Import and Export windows, drag and drop, and clearing the workspace.
import { state } from './state.js';
import { $, toast } from './ui.js';
import { getJSON, sendJSON } from './api.js';
import { forgetImage, loadList, openFirstUnlabelled, openImage } from './images.js';
import { loadCats } from './classes.js';

const IMAGE_RE = /\.(jpe?g|png|bmp)$/i;
let workspace = null;    // GET api/workspace: where images live, can it be cleared, are we on this computer

function setStatus(id, text, isError){
  $(id).textContent = text;
  $(id).classList.toggle('err', !!isError);
}

export async function refreshWorkspace(){
  try { workspace = await getJSON('api/workspace'); } catch { return; }
  $('wsdir').textContent = 'Workspace: ' + workspace.images_dir;
  $('wsdir').title = workspace.images_dir;
  $('folderOpt').hidden = $('folderRow').hidden = !workspace.local;          // folders only make sense locally
  $('clearWs').hidden = $('keepCls').parentElement.hidden = !workspace.can_clear;
  warnSetAside(workspace.set_aside);
}

// a labels file the server couldn't read was moved aside: say so once, or the user only sees their labels gone
const warned = new Set();
function warnSetAside(paths){
  let seen = [];
  try { seen = JSON.parse(localStorage.getItem('setAsideSeen') || '[]'); } catch { /* storage blocked: warn once per visit */ }
  const fresh = paths.filter(p=>!seen.includes(p) && !warned.has(p));
  if (!fresh.length) return;
  fresh.forEach(p=>warned.add(p));
  try { localStorage.setItem('setAsideSeen', JSON.stringify([...seen, ...fresh])); } catch { /* storage blocked: warn once per visit */ }
  alert('The labels file couldn’t be read, so it was set aside as\n'+fresh.join('\n')+
        '\nand the app started without those labels. Fix the file and bring it back with Import labels, or delete it.');
}

// ---- import ------------------------------------------------------------------------
export function openImport(){
  if (!$('importDlg').open) $('importDlg').showModal();
  refreshWorkspace();
}
$('importBtn').onclick = ()=>{ setStatus('importStatus', ''); openImport(); };
$('importClose').onclick = ()=>$('importDlg').close();
$('pickDir').onclick = ()=>$('dirInput').click();
$('pickFiles').onclick = ()=>$('fileInput').click();
$('pickLabels').onclick = ()=>$('labelInput').click();
['dirInput','fileInput','labelInput'].forEach(id=>{
  $(id).onchange = (e)=>{
    const files = [...e.target.files];
    e.target.value = '';
    if (files.length) importFiles(files);
  };
});

// one import at a time: a second one (e.g. another drop) waits, so they don't share the progress bar
let importing = Promise.resolve();
function importFiles(files){
  openImport();
  importing = importing.catch(()=>{}).then(()=>runImport(files));
}

// copy images into the workspace (a few at a time), then merge any COCO labels picked with them
async function runImport(files){
  openImport();
  // names whose picture here isn't the one in this import: labels picked with it must not land on ours
  const clashed = new Set(), seen = new Set();
  const todo = [], skipped = [];
  let ignored = 0;
  for (const f of files){
    if (!IMAGE_RE.test(f.name)){ if (!/\.json$/i.test(f.name)) ignored++; continue; }
    const key = f.name.toLowerCase();
    if (seen.has(key)){ clashed.add(key); skipped.push(f.name+': another file in this import has the same name'); }
    else { seen.add(key); todo.push(f); }   // a name already here is sent too: the server says if it's the same picture
  }
  const bar = $('importBar');
  let done = 0, added = 0, already = 0, next = 0;
  bar.hidden = !todo.length; bar.max = todo.length || 1;
  const show = ()=>{ bar.value = done; setStatus('importStatus', 'Importing '+done+' / '+todo.length+'…'); };
  show();
  const worker = async ()=>{
    while (next < todo.length){
      const f = todo[next++];
      try {
        const r = await getJSON('api/images/'+encodeURIComponent(f.name), {method:'PUT', body:f});
        if (r.existing) already++; else added++;
      } catch(err){
        if (err.status===409) clashed.add(f.name.toLowerCase());
        skipped.push(f.name+': '+err.message);
      }
      done++; show();
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);

  const notes = [];
  let labelled = 0;
  for (const f of files.filter(f=>/\.json$/i.test(f.name))){
    let data;
    try { data = JSON.parse(await f.text()); } catch { notes.push(f.name+': not valid JSON'); continue; }
    if (!data || !Array.isArray(data.images)){ notes.push(f.name+': not a COCO labels file'); continue; }
    const kept = data.images.filter(im=>!clashed.has(String(im && im.file_name).split(/[\\/]/).pop().toLowerCase()));
    if (kept.length < data.images.length)
      notes.push(f.name+': labels for '+(data.images.length-kept.length)+' image(s) not used, because a different picture with that name is already in the workspace');
    data.images = kept;
    try {
      const r = await getJSON('api/import/coco', sendJSON('POST', data));
      labelled += r.images;
      notes.push('Labels from '+f.name+': '+r.images+' image(s), '+r.boxes+' box(es)' +
        (r.classes_added ? ', '+r.classes_added+' new class(es)' : '') +
        (r.skipped_images ? ' · '+r.skipped_images+' skipped (already labelled here)' : '') +
        (r.not_in_workspace ? ' · '+r.not_in_workspace+' waiting for their images' : ''));
    } catch(err){ notes.push(f.name+': '+err.message); }
  }

  bar.hidden = true;
  const lines = [];
  if (todo.length || skipped.length || !notes.length)
    lines.push('Imported '+added+' image(s)' + (already ? ', '+already+' already here' : '') + (skipped.length ? ', skipped '+skipped.length : '') + '.');
  if (ignored) lines.push(ignored+' other file(s) ignored (images must be .jpg, .png or .bmp).');
  lines.push(...notes, ...skipped.slice(0, 5));
  if (skipped.length > 5) lines.push('…and '+(skipped.length-5)+' more skipped.');
  setStatus('importStatus', lines.join('\n'));
  await Promise.all([loadList(), loadCats()]);
  refreshWorkspace();
  if (state.idx<0) openFirstUnlabelled();
  // labels may have arrived for the image on screen: show them, or the next Save would overwrite them
  // (unsaved boxes there are kept: they're the user's own edits)
  else if (labelled && !state.dirty) openImage(state.idx, true);
}

// drag and drop, folders included
let dragDepth = 0;
const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
document.addEventListener('dragenter', e=>{ if (hasFiles(e)){ dragDepth++; document.body.classList.add('dragging'); } });
document.addEventListener('dragleave', e=>{ if (hasFiles(e) && --dragDepth<=0){ dragDepth = 0; document.body.classList.remove('dragging'); } });
document.addEventListener('dragover', e=>{ if (hasFiles(e)) e.preventDefault(); });
document.addEventListener('drop', async e=>{
  if (!hasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; document.body.classList.remove('dragging');
  const files = await droppedFiles(e.dataTransfer);
  if (files.length) importFiles(files);
});
async function droppedFiles(dt){
  // grab the entries before awaiting anything: the drop data is gone after this event
  const entries = [...dt.items].map(it=>it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dt.files];
  const out = [];
  const walk = async entry=>{
    if (entry.isFile) out.push(await new Promise((ok, fail)=>entry.file(ok, fail)));
    else if (entry.isDirectory){
      const reader = entry.createReader();
      for (;;){   // a folder is handed back in batches
        const batch = await new Promise((ok, fail)=>reader.readEntries(ok, fail));
        if (!batch.length) break;
        for (const child of batch) await walk(child);
      }
    }
  };
  for (const entry of entries) await walk(entry);
  return out;
}

// ---- export ------------------------------------------------------------------------
const exportChoice = ()=>({ fmt: document.querySelector('input[name=fmt]:checked').value,
                            dest: document.querySelector('input[name=dest]:checked').value });
const useFolder = ()=>{ document.querySelector('input[name=dest][value=folder]').checked = true; };
document.querySelectorAll('input[name=fmt]').forEach(el=>el.onchange = ()=>{ $('valRow').hidden = exportChoice().fmt!=='yolo'; });
$('folder').onfocus = useFolder;
try { $('folder').value = localStorage.getItem('exportFolder') || ''; } catch { /* storage blocked: start empty */ }

$('exportBtn').onclick = ()=>{ setStatus('exportStatus', ''); $('exportDlg').showModal(); refreshWorkspace(); };
$('exportClose').onclick = ()=>$('exportDlg').close();

$('browse').onclick = async ()=>{
  useFolder();
  $('browse').disabled = true;
  setStatus('exportStatus', 'Choose a folder in the window that opened (it may be behind the browser)…');
  try {
    const d = await getJSON('api/pick-folder', sendJSON('POST', {start: $('folder').value.trim() || null}));
    if (d.path) $('folder').value = d.path;
    setStatus('exportStatus', '');
  } catch(err){ setStatus('exportStatus', err.message, true); }
  $('browse').disabled = false;
};

$('doExport').onclick = async ()=>{
  const {fmt, dest} = exportChoice();
  const val = Math.min(Math.max(parseFloat($('val').value) || 0, 0), 0.9);
  if (state.dirty && !confirm("This image has unsaved boxes, which won't be in the export. Export anyway?")) return;
  const afterwards = workspace && workspace.can_clear ? '\nFinished with this batch? Clear the workspace to free the space.' : '';
  $('doExport').disabled = $('clearWs').disabled = true;   // clearing now would pull the images from under the export
  setStatus('exportStatus', 'Exporting… (a big dataset can take a while)');
  try {
    if (dest==='folder'){
      const folder = $('folder').value.trim();
      if (!folder) throw new Error('Choose a folder first');
      const d = await getJSON('api/export/'+fmt, sendJSON('POST', {folder, val}));
      try { localStorage.setItem('exportFolder', folder); } catch { /* storage blocked: just not remembered */ }
      setStatus('exportStatus', 'Saved to '+d.path+afterwards);
    } else {
      // the server builds the file, then the browser downloads it straight to disk,
      // so even an export of many GB never has to fit in the page's memory
      const d = await getJSON('api/export/'+fmt+'/download', sendJSON('POST', {val}));
      const a = document.createElement('a');
      a.href = d.url; a.download = d.name;
      $('exportDlg').appendChild(a); a.click(); a.remove();   // inside the dialog: the page behind it is inert
      setStatus('exportStatus', 'Downloading '+d.name+afterwards);
    }
  } catch(err){ setStatus('exportStatus', 'Export failed: '+err.message, true); }
  $('doExport').disabled = $('clearWs').disabled = false;
  refreshWorkspace();
};

// ---- clear the workspace -----------------------------------------------------------
$('clearWs').onclick = async ()=>{
  await refreshWorkspace();
  if (!workspace) return;
  if (!confirm('Delete the '+workspace.images+' image(s) in the workspace and their labels?\nYour original files and your exports are not affected.')) return;
  const force = !workspace.exported;
  const risk = workspace.set_aside.length
    ? 'An unreadable labels file was set aside ('+workspace.set_aside.at(-1)+'); the images it labels would go too.'
    : 'Some labels changed since your last export and would be lost.';
  if (force && !confirm(risk+' Delete them anyway?')) return;
  try {
    const d = await getJSON('api/workspace/clear', sendJSON('POST', {keep_classes: $('keepCls').checked, force}));
    forgetImage();   // the image on screen is gone
    $('exportDlg').close();
    toast('Workspace cleared: '+d.deleted_images+' image(s) deleted');
    if (d.in_use.length) alert(d.in_use.length+' image(s) were open in another program, so they stayed in the workspace '+
      '(without labels): '+d.in_use.slice(0, 5).join(', ')+(d.in_use.length > 5 ? ', …' : '')+'\nClose them and clear again.');
    const [listed] = await Promise.all([loadList(), loadCats()]);
    if (listed && !state.images.length) openImport();   // empty again: back to importing
  } catch(err){ setStatus('exportStatus', err.message, true); }
};
